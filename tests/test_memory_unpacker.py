import gzip
from io import BytesIO
import random
import unittest

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


if __name__ == "__main__":
    unittest.main()
