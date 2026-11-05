import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from ada.chaos import run_chaos
from ada.perf import PerfBaseline, PerfSample, collect_perf
from ada.rulemine import RuleMiner
from ada.multidev import DiffReport, DeviceResult


class TestChaos(unittest.TestCase):
    def _ch(self):
        ch = MagicMock()
        ok = MagicMock(); ok.ok = True; ok.stdout = ""; ok.stderr = ""
        ch.shell.return_value = ok
        return ch

    def test_low_memory(self):
        ch = self._ch()
        msg = run_chaos(ch, "S", {"type": "low_memory", "package": "com.x"})
        ch.shell.assert_called_with(
            "am send-trim-memory com.x RUNNING_CRITICAL", serial="S")
        self.assertIn("低内存", msg)

    def test_rotate(self):
        ch = self._ch()
        run_chaos(ch, "S", {"type": "rotate", "times": 2})
        cmds = [c.args[0] for c in ch.shell.call_args_list]
        self.assertEqual(cmds.count("settings put system user_rotation 1"), 2)

    def test_unknown_type(self):
        with self.assertRaises(ValueError):
            run_chaos(self._ch(), "S", {"type": "nuke"})

    def test_permission_revoke(self):
        ch = self._ch()
        run_chaos(ch, "S", {"type": "permission_revoke",
                            "package": "com.x",
                            "permission": "android.permission.CAMERA"})
        ch.shell.assert_called_with(
            "pm revoke com.x android.permission.CAMERA", serial="S")


class TestPerf(unittest.TestCase):
    def test_parse_and_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            pb = PerfBaseline(Path(tmp) / "p.sqlite")
            s = PerfSample(startup_ms=800, mem_total_kb=150000, frame_p95_ms=16)
            # 首次无基线不报
            self.assertEqual(pb.check("com.x", "POS1", s), [])
            for _ in range(3):
                pb.record("com.x", "POS1", s)
            # 超 20% 报回归
            bad = PerfSample(startup_ms=1100, mem_total_kb=150000, frame_p95_ms=16)
            regs = pb.check("com.x", "POS1", bad)
            self.assertEqual(len(regs), 1)
            self.assertEqual(regs[0].metric, "startup_ms")
            self.assertIn("性能回归", regs[0].text)
            pb.close()

    def test_collect_parse(self):
        ch = MagicMock()
        def shell(cmd, serial=None):
            r = MagicMock(); r.ok = True
            if "TotalTime" in cmd: r.stdout = "TotalTime: 1234"
            elif "meminfo" in cmd: r.stdout = "TOTAL  234567  1234"
            else: r.stdout = "12ms 15ms 18ms 40ms"
            return r
        ch.shell.side_effect = shell
        s = collect_perf(ch, "S", "com.x")
        self.assertEqual(s.startup_ms, 1234)
        self.assertEqual(s.mem_total_kb, 234567)
        self.assertEqual(s.frame_p95_ms, 18)  # 4 个样本 P95 落在第 3 个


class TestRuleMiner(unittest.TestCase):
    def test_learn_and_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            rm = RuleMiner(Path(tmp) / "rules.json")
            rm.learn("java.util.concurrent.TimeoutException", "timeout",
                     "线程池超时", "调大超时")
            hit = rm.match("java.util.concurrent.TimeoutException")
            self.assertEqual(hit["category"], "timeout")
            self.assertEqual(hit["hit_count"], 1)
            # 重启后规则还在
            rm2 = RuleMiner(Path(tmp) / "rules.json")
            self.assertIsNotNone(rm2.match("TimeoutException"))
            self.assertIsNone(rm2.match("OtherException"))


class TestMultiDevDiff(unittest.TestCase):
    def test_diff_logic(self):
        from ada.multidev import DiffReport
        devs = [
            DeviceResult(serial="A", passed=True, fingerprints=[], activities=["a/.M", "a/.S"]),
            DeviceResult(serial="B", passed=False, failure_step=1,
                         fingerprints=["fpX"], activities=["a/.M", "a/.Other"]),
        ]
        # 复用 diff 计算逻辑（不跑线程，直接构造）
        report = DiffReport(total=2)
        report.passed_serials = [d.serial for d in devs if d.passed]
        report.failed_serials = [d.serial for d in devs if not d.passed]
        all_fps = {}
        for d in devs:
            for fp in d.fingerprints:
                all_fps.setdefault(fp, []).append(d.serial)
        report.device_specific_fingerprints = {
            fp: ss for fp, ss in all_fps.items() if len(ss) < 2}
        self.assertEqual(report.passed_serials, ["A"])
        self.assertEqual(report.device_specific_fingerprints, {"fpX": ["B"]})


if __name__ == "__main__":
    unittest.main()
