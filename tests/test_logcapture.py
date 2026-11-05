import unittest
from unittest.mock import patch, MagicMock

from ada.logcapture import LogcatCapture, FATAL_RE, ANR_RE


class FakeChannel:
    adb_path = "/bin/true"

    def adb(self, args, serial=None, risk=None, timeout=None):
        r = MagicMock()
        r.ok = True
        return r


class TestLogParse(unittest.TestCase):
    def setUp(self):
        self.cap = LogcatCapture(FakeChannel())

    def test_threadtime_parse(self):
        line = self.cap._parse_line(
            "11-05 10:00:00.123  1234  5678 E AndroidRuntime: FATAL EXCEPTION: main", 1.0)
        self.assertEqual(line.level, "E")
        self.assertEqual(line.tag, "AndroidRuntime")
        self.assertEqual(line.pid, 1234)
        self.assertIn("FATAL", line.message)

    def test_separator_skipped(self):
        self.assertIsNone(self.cap._parse_line("--------- beginning of main", 1.0))
        self.assertIsNone(self.cap._parse_line("", 1.0))

    def test_unmatched_line_kept_as_info(self):
        line = self.cap._parse_line("some raw text", 1.0)
        self.assertEqual(line.level, "I")

    def test_fatal_regex(self):
        self.assertTrue(FATAL_RE.search("FATAL EXCEPTION: main"))
        self.assertTrue(FATAL_RE.search("signal 11 (SIGSEGV)"))
        self.assertFalse(FATAL_RE.search("normal log"))

    def test_anr_regex(self):
        self.assertTrue(ANR_RE.search("ANR in com.x"))
        self.assertFalse(ANR_RE.search("all good"))

    def test_marker(self):
        self.cap.mark("step 0")
        self.assertEqual(len(self.cap.markers), 1)
        self.assertIn("step 0", self.cap.markers[0]["label"])

    def test_snapshot_ring(self):
        with self.cap._lock:
            for i in range(100):
                self.cap.buffer.append(self.cap._parse_line(f"raw {i}", 1.0))
                self.cap._line_count += 1
        b = self.cap.snapshot()
        self.assertEqual(len(b.lines), 100)


if __name__ == "__main__":
    unittest.main()
