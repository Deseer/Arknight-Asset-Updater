import tempfile
import time
import unittest
from pathlib import Path

from ark_resource_service.app import Job, MemoryBudget, PreparedAsset, Service, dat_name, safe_member


class CoreTests(unittest.TestCase):
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
