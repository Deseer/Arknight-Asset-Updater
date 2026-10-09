from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


def script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


levels = script("backfill_levels")
publisher = script("publish_backfill")


class HistoricalBackfillTests(unittest.TestCase):
    def test_unsafe_and_metadata_identifiers_are_not_resource_paths(self):
        self.assertTrue(levels.valid_level_id("activities/act2break/level_act2break_15"))
        for value in ("/activities/test", "activities/../outside", "[dev]test/map", "act38side_01"):
            self.assertFalse(levels.valid_level_id(value))

    def test_simplified_map_without_routes_or_waves_is_rejected(self):
        value = {"mapData": {"map": [[0]], "tiles": [{}]}}
        with self.assertRaises(KeyError):
            levels.validate_level(json.dumps(value).encode())

    def test_publication_preserves_current_files_and_excludes_masterdata(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / "source"; output = root / "output"
            source.mkdir(); output.mkdir()
            (source / "new.bytes").write_bytes(b"new")
            (source / "existing.bytes").write_bytes(b"old")
            (output / "existing.bytes").write_bytes(b"current")
            (source / "MasterData").mkdir()
            (source / "MasterData/table.json").write_text("{}")
            report = publisher.publish([source], output, root / "report.json", True)
            self.assertEqual((output / "existing.bytes").read_bytes(), b"current")
            self.assertEqual((output / "new.bytes").read_bytes(), b"new")
            self.assertFalse((output / "MasterData").exists())
            self.assertEqual(list(report["published"]), ["new.bytes"])
            self.assertEqual(report["differentExisting"], ["existing.bytes"])
            with self.assertRaises(FileExistsError):
                publisher.publish([source], output, root / "report.json", True)
            again = publisher.publish([source], output, root / "again.json", True)
            self.assertEqual(again["published"], {})

    def test_planning_does_not_publish_files(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / "source"; source.mkdir()
            (source / "new.json").write_text("{}")
            publisher.publish([source], root / "output", root / "report.json", False)
            self.assertFalse((root / "output").exists())


if __name__ == "__main__":
    unittest.main()
