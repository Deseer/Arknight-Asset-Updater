import gzip
from io import BytesIO
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from ark_resource_service.memory_unpacker import (
    MasterDataDecodeError,
    MemoryUnpacker,
    install_unitypy_gzip_fallback,
    parse_export_types,
)


class ExportConfigurationTests(unittest.TestCase):
    def test_aliases_are_normalized_and_deduplicated(self):
        self.assertEqual(parse_export_types("Sprite,Texture2D,audio,AudioClip"), {"image", "audio"})

    def test_unknown_type_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_export_types("image,shader")


class SemanticPathTests(unittest.TestCase):
    def test_internal_prefix_and_build_markers_are_removed(self):
        self.assertEqual(
            MemoryUnpacker.semantic_container_path("dyn/ui/[pack]charselect/icon.png").as_posix(),
            "ui/charselect/icon.png",
        )


class SemanticWriteTests(unittest.TestCase):
    @staticmethod
    def unpacker(output_root: Path) -> MemoryUnpacker:
        unpacker = MemoryUnpacker.__new__(MemoryUnpacker)
        unpacker.output_root = output_root
        unpacker._write_lock = threading.Lock()
        return unpacker

    def test_changed_masterdata_atomically_replaces_canonical_table(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "masterdata" / "character_table.json"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"old")
            item = types.SimpleNamespace(name="character_table", ext=".json", data=b"new")

            exported = self.unpacker(root)._write_semantic_item(
                Path("masterdata/character_table"), item, 12345
            )

            self.assertTrue(exported)
            self.assertEqual(target.read_bytes(), b"new")
            self.assertFalse((root / "masterdata" / "character_table__12345.json").exists())

    def test_changed_non_masterdata_keeps_path_id_collision_suffix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "ui" / "icon.png"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"first")
            item = types.SimpleNamespace(name="icon", ext=".png", data=b"second")

            exported = self.unpacker(root)._write_semantic_item(Path("ui/icon.png"), item, 42)

            self.assertTrue(exported)
            self.assertEqual(target.read_bytes(), b"first")
            self.assertEqual((root / "ui" / "icon__42.png").read_bytes(), b"second")


class MasterDataDecodeTests(unittest.TestCase):
    def test_known_flatbuffer_schema_failure_is_not_silently_exported_as_raw_text(self):
        class BrokenHandler:
            def __init__(self, *_args):
                pass

            def to_json_dict(self):
                raise ValueError("outdated schema")

        class BrokenAes:
            MASK_V2 = object()

            @staticmethod
            def aes_cbc_decrypt_bytes(*_args):
                raise ValueError("not encrypted json")

        module = types.SimpleNamespace(__name__="src.fbs.CN.activity_table", ROOT_TYPE=object())
        unpacker = MemoryUnpacker.__new__(MemoryUnpacker)
        unpacker.ArkFBOLibrary = types.SimpleNamespace(CN=[module])
        unpacker.FBOHandler = BrokenHandler
        unpacker.ArkAESLibrary = BrokenAes

        with self.assertRaisesRegex(MasterDataDecodeError, "activity_table"):
            unpacker._decode_masterdata(b"x" * 256, "activity_table67f0f6")


class UnityPyProbeTests(unittest.TestCase):
    def test_false_gzip_header_falls_back_to_resource_file(self):
        from UnityPy.helpers import ImportHelper

        install_unitypy_gzip_fallback()
        payload = b"\x1f\x8b\x01" + b"not-gzip" * 32
        file_type, reader = ImportHelper.check_file_type(BytesIO(payload))

        self.assertEqual(file_type, ImportHelper.FileType.ResourceFile)
        self.assertEqual(reader.Position, 0)

    def test_valid_gzip_web_file_is_still_detected(self):
        from UnityPy.helpers import ImportHelper

        install_unitypy_gzip_fallback()
        random_payload = random.Random(1).randbytes(1024)
        payload = gzip.compress(b"UnityWebData1.0\x00" + random_payload)
        file_type, _ = ImportHelper.check_file_type(BytesIO(payload))

        self.assertEqual(file_type, ImportHelper.FileType.WebFile)
        self.assertEqual(
            MemoryUnpacker.semantic_container_path("assets/[uc]lipsync/[ucp]voice/file.bytes").as_posix(),
            "lipsync/voice/file.bytes",
        )


class UsmMemoryExportTests(unittest.TestCase):
    def test_video_stream_is_piped_to_mp4_without_raw_intermediate(self):
        with tempfile.TemporaryDirectory() as temporary:
            unpacker = MemoryUnpacker.__new__(MemoryUnpacker)
            unpacker.output_root = Path(temporary)
            unpacker._write_lock = threading.Lock()
            decoder = types.SimpleNamespace(
                extract_usm_bytes=lambda *_args, **_kwargs: [
                    {"extension": "ivf", "data": b"video-stream"}
                ]
            )

            def fake_run(command, **_kwargs):
                Path(command[-1]).write_bytes(b"mp4")
                return subprocess.CompletedProcess(command, 0, b"", b"")

            with mock.patch.dict(sys.modules, {"cridecoder": decoder}), mock.patch(
                "ark_resource_service.memory_unpacker.subprocess.run", side_effect=fake_run
            ):
                exported = unpacker.unpack_usm(
                    b"CRIDpayload", "raw/video/sample.usm", "test-job"
                )

            files = [path for path in Path(temporary).rglob("*") if path.is_file()]
            self.assertEqual(exported, 1)
            self.assertEqual([path.name for path in files], ["sample.mp4"])


if __name__ == "__main__":
    unittest.main()
