import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock

from ark_resource_service.schema_sync import SchemaSync, load_modules


class SchemaSyncTests(unittest.TestCase):
    @staticmethod
    def archive(revision, broken=False):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as tar:
            for table in ("character_table", "roguelike_topic_table", "activity_table", "building_data"):
                data = b"invalid fbs" if broken else b"table Root { name:string; }\nroot_type Root;\n"
                info = tarfile.TarInfo(f"ArknightsFlatbuffers-{revision}/cn/{table}.fbs")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return output.getvalue()

    def configure_remote(self, sync, revision, broken=False):
        commit = Mock()
        commit.json.return_value = {"sha": revision}
        archive = Mock()
        archive.__enter__ = Mock(return_value=archive)
        archive.__exit__ = Mock(return_value=False)
        archive.iter_content.return_value = [self.archive(revision, broken)]
        sync.session = Mock()
        sync.session.get.side_effect = [commit, archive]
        sync.next_check = 0

    def test_complete_snapshot_is_compiled_and_cached_across_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sync = SchemaSync(root)
            self.configure_remote(sync, "a" * 40)
            self.assertTrue(sync.refresh())
            self.assertEqual(len(load_modules(sync.modules_dir)), 4)
            self.assertEqual(set(sync.hashes), {"character_table", "roguelike_topic_table", "activity_table", "building_data"})
            reloaded = SchemaSync(root)
            self.assertEqual(reloaded.snapshot, sync.snapshot)
            reloaded.session = Mock()
            self.assertFalse(reloaded.refresh())
            reloaded.session.get.assert_not_called()

    def test_broken_update_preserves_previous_snapshot_and_pointer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sync = SchemaSync(root)
            self.configure_remote(sync, "a" * 40)
            self.assertTrue(sync.refresh())
            previous = sync.snapshot
            self.configure_remote(sync, "b" * 40, broken=True)
            self.assertFalse(sync.refresh())
            self.assertIs(sync.snapshot, previous)
            self.assertEqual(json.loads((root / "current.json").read_text())["revision"], "a" * 40)
            self.assertFalse(list(root.glob(".staging-*")))

    def test_tampered_cache_is_not_loaded(self):
        with tempfile.TemporaryDirectory() as temporary:
            sync = SchemaSync(Path(temporary))
            self.configure_remote(sync, "a" * 40)
            self.assertTrue(sync.refresh())
            (sync.modules_dir / "character_table.py").write_text("invalid")
            reloaded = SchemaSync(Path(temporary))
            self.assertIsNone(reloaded.snapshot)
            self.assertIsNotNone(reloaded.error)


if __name__ == "__main__":
    unittest.main()
