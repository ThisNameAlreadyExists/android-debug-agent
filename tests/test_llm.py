import unittest
from unittest.mock import MagicMock, patch

from ada.llm import scrub, OllamaClient, LLMError
from ada.analyst import LogAnalyst, parse_crash
from ada.logcapture import LogLine, LogBundle


def L(msg, level="E", tag="AndroidRuntime"):
    return LogLine(0.0, "", level, tag, 100, msg)


class TestScrub(unittest.TestCase):
    def test_phone_and_token(self):
        s = scrub("user 13812345678 token=abcdef123456 ip 192.168.1.1")
        self.assertNotIn("13812345678", s)
        self.assertNotIn("abcdef123456", s)
        self.assertNotIn("192.168.1.1", s)

    def test_normal_text_kept(self):
        s = scrub("NullPointerException at LoginViewModel.kt:83")
        self.assertIn("LoginViewModel", s)


class TestLLMTier(unittest.TestCase):
    UNKNOWN = [L("FATAL EXCEPTION: main"),
               L("java.util.concurrent.TimeoutException: pool timeout"),
               L("\tat com.x.Worker.run(Worker.kt:9)")]

    def test_llm_used_when_rule_misses(self):
        llm = MagicMock()
        llm.chat.return_value = '{"root_cause":"线程池超时","category":"timeout","suggested_fix":"加大超时","confidence":0.6}'
        analyst = LogAnalyst(llm=llm)
        b = LogBundle(lines=self.UNKNOWN, crash_indices=[0])
        ds = analyst.analyze(b, "com.x")
        self.assertTrue(ds[0].llm_used)
        self.assertEqual(ds[0].category, "timeout")
        self.assertEqual(ds[0].confidence, 0.6)

    def test_llm_failure_falls_back_to_rule(self):
        llm = MagicMock()
        llm.chat.side_effect = LLMError("连接失败")
        analyst = LogAnalyst(llm=llm)
        b = LogBundle(lines=self.UNKNOWN, crash_indices=[0])
        ds = analyst.analyze(b, "com.x")
        self.assertFalse(ds[0].llm_used)
        self.assertEqual(ds[0].category, "unknown")

    def test_rule_hit_skips_llm(self):
        llm = MagicMock()
        analyst = LogAnalyst(llm=llm)
        lines = [L("FATAL EXCEPTION: main"),
                 L("java.lang.NullPointerException: x"),
                 L("\tat com.x.A.m(A.kt:1)")]
        ds = analyst.analyze(LogBundle(lines=lines, crash_indices=[0]), "com.x")
        llm.chat.assert_not_called()
        self.assertEqual(ds[0].category, "npe")


class TestOllamaClient(unittest.TestCase):
    def test_failure_raises(self):
        c = OllamaClient(base="http://127.0.0.1:9", timeout=1)
        with self.assertRaises(LLMError):
            c.chat("hi")


if __name__ == "__main__":
    unittest.main()
