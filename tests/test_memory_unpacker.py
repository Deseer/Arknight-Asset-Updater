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
