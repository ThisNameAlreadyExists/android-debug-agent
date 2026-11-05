import unittest

from ada.analyst import parse_crash, fingerprint, LogAnalyst
from ada.logcapture import LogLine, LogBundle


def L(msg, level="E", tag="AndroidRuntime"):
    return LogLine(0.0, "11-05 10:00:00.000", level, tag, 1234, msg)


NPE_LINES = [
    L("FATAL EXCEPTION: main"),
    L("Process: com.example.app, PID: 1234"),
    L("java.lang.NullPointerException: Attempt to invoke virtual method 'void android.view.View.setVisibility(int)' on a null object reference"),
    L("\tat com.example.app.LoginViewModel.onResult(LoginViewModel.kt:83)"),
    L("\tat com.example.app.LoginActivity$1.run(LoginActivity.kt:40)"),
    L("\tat android.os.Handler.handleCallback(Handler.java:942)"),
]

OOM_LINES = [
    L("FATAL EXCEPTION: main"),
    L("java.lang.OutOfMemoryError: Failed to allocate a 52428812 byte allocation"),
    L("\tat android.graphics.Bitmap.nativeCreate(Bitmap.java:-2)"),
]

NATIVE_LINES = [
    L("signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x0", tag="DEBUG"),
]


class TestParseCrash(unittest.TestCase):
    def test_npe_frames(self):
        c = parse_crash(NPE_LINES, 0)
        self.assertEqual(c.exception, "java.lang.NullPointerException")
        self.assertEqual(c.frames[0].file, "LoginViewModel.kt")
        self.assertEqual(c.frames[0].line, 83)
        self.assertGreaterEqual(len(c.frames), 3)

    def test_native_signal(self):
        c = parse_crash(NATIVE_LINES, 0)
        self.assertEqual(c.native_signal, "SIGSEGV")

    def test_no_crash(self):
        self.assertIsNone(parse_crash([L("hello world", "I", "x")], 0))


class TestFingerprint(unittest.TestCase):
    def test_stable_across_line_numbers(self):
        c1 = parse_crash(NPE_LINES, 0)
        shifted = [L(m.message.replace(":83)", ":999)").replace(":40)", ":888)"))
                   for m in NPE_LINES]
        c2 = parse_crash(shifted, 0)
        self.assertEqual(fingerprint(c1, "com.example.app"),
                         fingerprint(c2, "com.example.app"))

    def test_different_for_different_crashes(self):
        c1 = parse_crash(NPE_LINES, 0)
        c2 = parse_crash(OOM_LINES, 0)
        self.assertNotEqual(fingerprint(c1), fingerprint(c2))


class TestAnalyst(unittest.TestCase):
    def _bundle(self, lines, crash_idx, anr_idx=()):
        return LogBundle(lines=lines, crash_indices=list(crash_idx),
                         anr_indices=list(anr_idx))

    def test_npe_diagnosis(self):
        b = self._bundle(NPE_LINES, [0])
        ds = LogAnalyst().analyze(b, "com.example.app")
        self.assertEqual(len(ds), 1)
        d = ds[0]
        self.assertEqual(d.category, "npe")
        self.assertEqual(d.source_location["file"], "LoginViewModel.kt")
        self.assertEqual(d.source_location["line"], 83)
        self.assertGreaterEqual(d.confidence, 0.8)
        self.assertTrue(d.fingerprint)
        self.assertTrue(d.evidence)

    def test_oom_diagnosis(self):
        ds = LogAnalyst().analyze(self._bundle(OOM_LINES, [0]))
        self.assertEqual(ds[0].category, "oom")

    def test_anr_diagnosis(self):
        lines = [L("ANR in com.example.app (com.example.app/.MainActivity)", "E", "ActivityManager"),
                 L("Reason: executing service com.example.app/.SyncService", "E", "ActivityManager")]
        ds = LogAnalyst().analyze(self._bundle(lines, [], [0]))
        self.assertEqual(ds[0].category, "anr")

    def test_unknown_crash_low_confidence(self):
        lines = [L("FATAL EXCEPTION: main"),
                 L("java.util.concurrent.TimeoutException: x")]
        ds = LogAnalyst().analyze(self._bundle(lines, [0]))
        self.assertEqual(ds[0].category, "unknown")
        self.assertLess(ds[0].confidence, 0.5)


if __name__ == "__main__":
    unittest.main()
