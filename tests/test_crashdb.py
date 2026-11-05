import tempfile
import unittest
from pathlib import Path

from ada.crashdb import CrashDB, CrashRecord


class TestCrashDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = CrashDB(Path(self.tmp.name) / "t.sqlite")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _rec(self, fp="fp1", version="1.0", model="POS1", status="open"):
        return CrashRecord(fingerprint=fp, package="com.x", app_version=version,
                           device_model=model, category="npe",
                           root_cause="空指针", status=status)

    def test_new_and_repeat(self):
        is_new, reg = self.db.record(self._rec())
        self.assertTrue(is_new)
        self.assertFalse(reg)
        is_new, _ = self.db.record(self._rec())
        self.assertFalse(is_new)
        recs = self.db.lookup("fp1")
        self.assertEqual(recs[0].hit_count, 2)

    def test_regression_alert(self):
        self.db.record(self._rec(version="1.0"))
        self.db.mark_fixed("fp1")
        is_new, reg = self.db.record(self._rec(version="1.1"))
        self.assertTrue(reg)
        # fixed 记录被重新打开
        self.assertTrue(all(r.status == "open" for r in self.db.lookup("fp1")))

    def test_aggregate_cross_device(self):
        self.db.record(self._rec(model="POS1"))
        self.db.record(self._rec(model="POS2"))
        agg = self.db.aggregate("fp1")
        self.assertEqual(agg["devices"], ["POS1", "POS2"])
        self.assertEqual(agg["total_hits"], 2)

    def test_history_filter(self):
        self.db.record(self._rec(fp="a"))
        self.db.record(CrashRecord(fingerprint="b", package="com.y"))
        self.assertEqual(len(self.db.history()), 2)
        self.assertEqual(len(self.db.history(package="com.y")), 1)


if __name__ == "__main__":
    unittest.main()
