import unittest

from ada.replay import render_html
from ada.scenario import ExecutionTrace, StepResult
from ada.analyst import Diagnosis
from ada.logcapture import LogBundle, LogLine


class TestReplay(unittest.TestCase):
    def test_html_structure(self):
        trace = ExecutionTrace(case_name="c1", started_at=1.0, finished_at=5.0)
        trace.steps.append(StepResult(index=0, action="launch", ok=True,
                                      detail="launch com.x", duration_ms=100))
        d = Diagnosis(category="npe", root_cause="空", fingerprint="fp1",
                      confidence=0.9, suggested_fix="判空")
        bundle = LogBundle(markers=[{"ts": 1.0, "label": "step 0"}],
                           lines=[LogLine(1.5, "", "E", "T", 1, "boom")])
        html = render_html(trace, [d], bundle)
        self.assertIn("执行步骤", html)
        self.assertIn("fp1", html)
        self.assertIn("launch", html)
        self.assertIn("boom", html)  # 该步时间窗内的日志应内联展示

    def test_no_screenshot_ok(self):
        trace = ExecutionTrace(case_name="c2", started_at=0.0, finished_at=1.0)
        trace.steps.append(StepResult(index=0, action="wait", ok=True))
        html = render_html(trace, [], None)
        self.assertIn("无截图", html)

    def test_env_info_rendered(self):
        from ada.device import DeviceProfile
        trace = ExecutionTrace(case_name="c3", started_at=0.0, finished_at=1.0)
        prof = DeviceProfile(serial="S1", model="POS 1", brand="CATHAYTOP",
                             android_release="13", sdk=33, abi="arm64-v8a",
                             rom="aosp/other", rooted=False, screen="720x1440",
                             density=320, battery_level=80)
        html = render_html(trace, [], None, profile=prof)
        self.assertIn("环境信息", html)
        self.assertIn("POS 1", html)
        self.assertIn("SDK 33", html)


if __name__ == "__main__":
    unittest.main()
