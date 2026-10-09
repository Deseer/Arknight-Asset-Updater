import tempfile
import time
import unittest
import threading
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

from ark_resource_service.app import (
    Job,
    MemoryBudget,
    OfficialClient,
    PreparedAsset,
    Service,
    dat_name,
    safe_member,
    ROGUELIKE_TOPIC_SKIP_REVISION,
    TERMINAL_SKIP_REVISION,
)


class CoreTests(unittest.TestCase):
    @staticmethod
    def record_service(root: Path) -> Service:
        service = Service.__new__(Service)
        service.root = root
        service.records = {}
        service.records_lock = threading.Lock()
        service.records_journal = root / "State" / "unpacked_records.jsonl"
        service.export_types = frozenset({"masterdata"})
        service.typetree_types = frozenset()
        unpacker = mock.Mock()
        unpacker.relative_destination.return_value = "anon/resource"
        service.get_unpacker = mock.Mock(return_value=unpacker)
        return service

    def test_schema_skip_is_current_until_skip_revision_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = self.record_service(Path(temporary))
            asset = {"name": "anon/resource.bin", "hash": "h1", "md5": "m1", "abSize": 10}
            error = type("MasterDataDecodeError", (RuntimeError,), {})("schema mismatch")

            service.mark_skipped(Job(id="skip"), asset, error)

            self.assertTrue(service.is_current(asset))
            record = service.records[asset["name"]]
            self.assertTrue(record["terminalSkip"])
            self.assertEqual(record["terminalSkipRevision"], TERMINAL_SKIP_REVISION)
            record["terminalSkipRevision"] = "older-schema"
            self.assertFalse(service.is_current(asset))

    def test_roguelike_schema_repair_retries_only_its_previous_skip(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = self.record_service(Path(temporary))
            asset = {"name": "gamedata/excel/roguelike_topic_table.ab", "hash": "h1", "md5": "m1", "abSize": 10}
            error = type("MasterDataDecodeError", (RuntimeError,), {})("schema mismatch")
            service.mark_skipped(Job(id="skip"), asset, error)
            record = service.records[asset["name"]]
            self.assertEqual(record["terminalSkipRevision"], ROGUELIKE_TOPIC_SKIP_REVISION)
            self.assertTrue(service.is_current(asset))
            record["terminalSkipRevision"] = TERMINAL_SKIP_REVISION
            self.assertFalse(service.is_current(asset))

    def test_transient_skip_is_not_marked_current(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = self.record_service(Path(temporary))
            asset = {"name": "anon/resource.bin", "hash": "h1", "md5": "m1", "abSize": 10}

            service.mark_skipped(Job(id="skip"), asset, RuntimeError("temporary failure"))

            self.assertFalse(service.is_current(asset))

    def test_only_changed_schema_invalidates_successful_asset(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = self.record_service(Path(temporary))
            asset = {"name": "anon/resource.bin", "hash": "h1", "md5": "m1"}
            service.records[asset["name"]] = {"hash": "h1", "md5": "m1", "exportProfile": service.export_profile,
                "schemaHashes": {"roguelike_topic_table": "old"}}
            service.schema_sync = SimpleNamespace(hashes={"roguelike_topic_table": "old", "item_table": "new"})
            self.assertTrue(service.is_current(asset))
            service.schema_sync.hashes["roguelike_topic_table"] = "new"
            self.assertFalse(service.is_current(asset))

    @staticmethod
    def recycle_service() -> Service:
        service = Service.__new__(Service)
        service.recycle_after_updated_job = True
        service.recycle_scheduled = False
        return service

    @mock.patch("ark_resource_service.app.threading.Timer")
    def test_resource_bearing_completed_job_schedules_one_recycle(self, timer):
        service = self.recycle_service()
        job = Job(id="updated", status="completed", total=3, completed=3)

        self.assertTrue(service.schedule_recycle_after_updated_job(job))
        self.assertFalse(service.schedule_recycle_after_updated_job(job))

        timer.assert_called_once()
        timer.return_value.start.assert_called_once_with()

    @mock.patch("ark_resource_service.app.threading.Timer")
    def test_empty_or_failed_job_does_not_schedule_recycle(self, timer):
        service = self.recycle_service()

        self.assertFalse(
            service.schedule_recycle_after_updated_job(
                Job(id="empty", status="completed", total=0)
            )
        )
        self.assertFalse(
            service.schedule_recycle_after_updated_job(
                Job(id="failed", status="failed", total=2, failed=1)
            )
        )
        self.assertFalse(
            service.schedule_recycle_after_updated_job(
                Job(id="skipped-only", status="completed", total=1, failed=1, skipped=1)
            )
        )
        timer.assert_not_called()

    def test_configured_cdn_overrides_discovered_cdn(self):
        client = OfficialClient(Path("/tmp"), cdn_root="https://configured.invalid/root/")

        class Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {
                    "content": {
                        "funcVer": "v1",
                        "configs": {
                            "v1": {
                                "network": {
                                    "hv": "https://version.invalid/{0}",
                                    "hu": "https://discovered.invalid/root",
                                }
                            }
                        },
                    }
                }

        client.session.get = lambda *args, **kwargs: Response()
        client.discover()

        self.assertEqual(client.cdn_root, "https://configured.invalid/root")
        self.assertEqual(client.version_url, "https://version.invalid/Android")

    def test_dat_name_matches_official_cdn_convention(self):
        self.assertEqual(dat_name("arts/chararts/char_002_amiya#1.ab"), "arts_chararts_char_002_amiya__1.dat")

    def test_safe_member_accepts_resource_path(self):
        self.assertEqual(safe_member("battle/enm_pfb_10.ab").as_posix(), "battle/enm_pfb_10.ab")

    def test_safe_member_rejects_traversal(self):
        with self.assertRaises(RuntimeError):
            safe_member("../escape.ab")

    def test_atomic_destination_can_live_on_external_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "Bundles" / "a.ab"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"ark")
            self.assertEqual(target.read_bytes(), b"ark")

    def test_staged_pipeline_releases_memory_and_finishes_every_asset(self):
        service = Service.__new__(Service)
        service.download_workers = 2
        service.export_workers = 2
        service.prefetch_bundles = 1
        service.memory_budget = MemoryBudget(8 * 1024 * 1024)

        def prepare(job, base, asset):
            reservation = 1024 * 1024
            service.memory_budget.acquire(reservation)
            return PreparedAsset(asset, b"bundle", reservation, time.monotonic(), 1)

        def export(job, prepared):
            service.memory_budget.release(prepared.reservation)
            return prepared.asset["name"]

        service.prepare_one = prepare
        service.export_one = export
        assets = [{"name": f"asset-{index}.ab"} for index in range(7)]
        job = Job(id="pipeline-test", total=len(assets))

        service.process_assets(job, "https://example.invalid", assets)

        self.assertEqual(job.completed, len(assets))
        self.assertEqual(service.memory_budget.snapshot()["usedBytes"], 0)
        self.assertLessEqual(service.memory_budget.snapshot()["peakBytes"], 5 * 1024 * 1024)

    def test_staged_pipeline_releases_memory_after_download_failure(self):
        service = Service.__new__(Service)
        service.download_workers = 2
        service.export_workers = 2
        service.prefetch_bundles = 1
        service.memory_budget = MemoryBudget(8 * 1024 * 1024)

        def prepare(job, base, asset):
            reservation = 1024 * 1024
            service.memory_budget.acquire(reservation)
            if asset["name"] == "broken.ab":
                service.memory_budget.release(reservation)
                raise RuntimeError("download failed")
            return PreparedAsset(asset, b"bundle", reservation, time.monotonic(), 1)

        def export(job, prepared):
            service.memory_budget.release(prepared.reservation)
            return prepared.asset["name"]

        service.prepare_one = prepare
        service.export_one = export
        job = Job(id="pipeline-failure-test", total=3)

        with self.assertRaisesRegex(RuntimeError, "download failed"):
            service.process_assets(
                job,
                "https://example.invalid",
                [{"name": "first.ab"}, {"name": "broken.ab"}, {"name": "last.ab"}],
            )

        self.assertEqual(service.memory_budget.snapshot()["usedBytes"], 0)

    def test_staged_pipeline_skips_export_failure_and_continues(self):
        service = Service.__new__(Service)
        service.download_workers = 2
        service.export_workers = 2
        service.prefetch_bundles = 1
        service.memory_budget = MemoryBudget(8 * 1024 * 1024)
        skipped = []

        def prepare(job, base, asset):
            reservation = 1024 * 1024
            service.memory_budget.acquire(reservation)
            return PreparedAsset(asset, b"bundle", reservation, time.monotonic(), 1)

        def export(job, prepared):
            service.memory_budget.release(prepared.reservation)
            if prepared.asset["name"] == "broken.ab":
                raise RuntimeError("export failed")
            return prepared.asset["name"]

        service.prepare_one = prepare
        service.export_one = export
        service.mark_skipped = lambda job, asset, error: skipped.append((asset["name"], str(error)))
        assets = [{"name": name} for name in ("first.ab", "broken.ab", "last.ab")]
        job = Job(id="pipeline-skip-test", total=len(assets))

        service.process_assets(job, "https://example.invalid", assets)

        self.assertEqual(job.completed, 2)
        self.assertEqual(job.failed, 1)
        self.assertEqual(job.skipped, 1)
        self.assertEqual(skipped, [("broken.ab", "export failed")])
        self.assertEqual(service.memory_budget.snapshot()["usedBytes"], 0)


if __name__ == "__main__":
    unittest.main()
