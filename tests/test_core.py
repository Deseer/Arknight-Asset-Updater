import tempfile
import unittest
from pathlib import Path

from ark_resource_service.app import dat_name, safe_member


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


if __name__ == "__main__":
    unittest.main()

