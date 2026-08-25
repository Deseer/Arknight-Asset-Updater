import unittest

from ark_resource_service.memory_unpacker import MemoryUnpacker, parse_export_types


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
        self.assertEqual(
            MemoryUnpacker.semantic_container_path("assets/[uc]lipsync/[ucp]voice/file.bytes").as_posix(),
            "lipsync/voice/file.bytes",
        )


if __name__ == "__main__":
    unittest.main()
