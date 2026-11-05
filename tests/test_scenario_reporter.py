import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from ada.scenario import ScenarioRunner, load_case
from ada.reporter import render_markdown
from ada.analyst import Diagnosis


class TestScenarioRunner(unittest.TestCase):
    def _runner(self, tmp):
        ch = MagicMock()
        ok = MagicMock(); ok.ok = True; ok.stdout = "Success"; ok.stderr = ""
        ch.shell.return_value = ok
        ch.adb.return_value = ok
        ch.pull.return_value = ok
        runner = ScenarioRunner(ch, workdir=Path(tmp))
        runner.ui.tap = MagicMock()
        runner._screenshot = MagicMock(return_value="")
        runner._current_activity = MagicMock(return_value="a/.Main")
        runner.app.launch = MagicMock()
        runner.app.clear_data = MagicMock()
        return runner

    def test_success_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = self._runner(tmp)
            case = {"name": "t1", "steps": [
                {"launch": "com.x"},
                {"tap": {"text": "登录"}},
                {"wait": 0.01},
            ]}
            trace, bundle = runner.run(case, serial="S")
            self.assertTrue(trace.passed)
            self.assertEqual(len(trace.steps), 3)
            runner.app.launch.assert_called_once()
            runner.ui.tap.assert_called_once()

    def test_failure_stops_and_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = self._runner(tmp)
            runner.ui.tap.side_effect = LookupError("未找到控件")
            case = {"name": "t2", "steps": [
                {"tap": {"text": "x"}},
                {"tap": {"text": "y"}},
            ]}
            trace, _ = runner.run(case, serial="S")
            self.assertFalse(trace.passed)
            self.assertEqual(trace.failure_step, 0)
            self.assertEqual(len(trace.steps), 1)  # 失败后不再执行后续步骤

    def test_unknown_step_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = self._runner(tmp)
            trace, _ = runner.run({"name": "t3", "steps": [{"fly": {}}]}, serial="S")
            self.assertFalse(trace.passed)
            self.assertIn("未知步骤", trace.failure_reason)

    def test_yaml_load(self):
        case = load_case(str(Path(__file__).parent.parent / "examples" / "login_flow.yaml"))
        self.assertEqual(case["name"], "login_flow")
        self.assertEqual(len(case["steps"]), 14)


class TestReporter(unittest.TestCase):
    def test_render(self):
        d = Diagnosis(category="npe", root_cause="空指针",
                      source_location={"file": "A.kt", "line": 1, "symbol": "a.b.c"},
                      evidence=["stack..."], confidence=0.9,
                      fingerprint="abc123", suggested_fix="判空")
        md = render_markdown(diagnoses=[d], title="T")
        self.assertIn("npe", md)
        self.assertIn("A.kt", md)
        self.assertIn("abc123", md)


if __name__ == "__main__":
    unittest.main()
