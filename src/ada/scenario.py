"""场景执行模块（Scenario Runner）—— 脚本模式。

声明式 YAML 用例，确定性回放。支持动作：
  launch / tap / input / swipe / key / wait / back / home /
  clear_data / grant / assert_log / screenshot
每步执行记录 ExecutionTrace（含截图与时间轴标记），失败还原现场。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import yaml

from .appctrl import AppController
from .channel import AdbChannel
from .logcapture import LogcatCapture, LogBundle
from .uiautomator import UiInspector, find_node


@dataclass
class StepResult:
    index: int
    action: str
    ok: bool = False
    detail: str = ""
    screenshot: str = ""
    activity: str = ""
    duration_ms: int = 0


@dataclass
class ExecutionTrace:
    case_name: str
    serial: str = ""
    steps: list[StepResult] = field(default_factory=list)
    passed: bool = True
    failure_step: int = -1
    failure_reason: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class ScenarioRunner:
    def __init__(self, channel: AdbChannel,
                 workdir: Optional[Path] = None,
                 capture: Optional[LogcatCapture] = None):
        self.ch = channel
        self.app = AppController(channel)
        self.ui = UiInspector(channel)
        self.workdir = Path(workdir or "./ada_out")
        self.workdir.mkdir(parents=True, exist_ok=True)
        (self.workdir / "shots").mkdir(exist_ok=True)
        self.capture = capture  # 可为 None（不采日志）

    # ---- 基础动作 ----

    def _screenshot(self, serial: Optional[str], name: str) -> str:
        path = self.workdir / "shots" / f"{name}.png"
        # 经 shell 写设备端再 pull，避免 exec-out 文本模式损坏二进制
        remote = f"/data/local/tmp/{name}.png"
        r1 = self.ch.shell(f"screencap -p {remote}", serial=serial)
        if not r1.ok:
            return ""
        r2 = self.ch.pull(remote, str(path), serial=serial)
        self.ch.shell(f"rm -f {remote}", serial=serial)
        return str(path) if r2.ok else ""

    def _current_activity(self, serial: Optional[str]) -> str:
        import re
        out = self.ch.shell(
            "dumpsys activity activities | grep -m1 ResumedActivity",
            serial=serial).stdout
        m = re.search(r"\s(\S+/\S+)", out)
        return m.group(1) if m else ""

    def _do_step(self, step: dict, serial: Optional[str]) -> str:
        """执行单步，返回 detail。异常向上抛。"""
        if "launch" in step:
            spec = step["launch"]
            if isinstance(spec, str):
                self.app.launch(spec, serial=serial)
                return f"launch {spec}"
            self.app.launch(spec["package"], spec.get("activity"), serial=serial)
            return f"launch {spec['package']}"
        if "tap" in step:
            spec = step["tap"]
            if isinstance(spec, dict) and ("x" in spec and "y" in spec):
                self.ch.shell(f"input tap {spec['x']} {spec['y']}", serial=serial)
                return f"tap ({spec['x']},{spec['y']})"
            node = self.ui.tap(serial,
                               text=spec.get("text"),
                               resource_id=spec.get("id"),
                               content_desc=spec.get("desc"),
                               exact=spec.get("exact", False))
            return f"tap [{node.text or node.resource_id or node.content_desc}]"
        if "input" in step:
            text = str(step["input"]).replace(" ", "%s")
            self.ch.shell(f"input text {text}", serial=serial)
            return "input ***"
        if "swipe" in step:
            s = step["swipe"]
            self.ch.shell(
                f"input swipe {s['x1']} {s['y1']} {s['x2']} {s['y2']} {s.get('ms', 300)}",
                serial=serial)
            return "swipe"
        if "key" in step:
            self.ch.shell(f"input keyevent {step['key']}", serial=serial)
            return f"key {step['key']}"
        if "back" in step:
            self.ch.shell("input keyevent KEYCODE_BACK", serial=serial)
            return "back"
        if "home" in step:
            self.ch.shell("input keyevent KEYCODE_HOME", serial=serial)
            return "home"
        if "wait" in step:
            time.sleep(float(step["wait"]))
            return f"wait {step['wait']}s"
        if "clear_data" in step:
            self.app.clear_data(step["clear_data"], serial=serial)
            return f"clear_data {step['clear_data']}"
        if "grant" in step:
            g = step["grant"]
            self.app.grant(g["package"], g["permission"], serial=serial)
            return f"grant {g['permission']}"
        if "chaos" in step:
            from .chaos import run_chaos
            return "chaos: " + run_chaos(self.ch, serial, step["chaos"])
        if "assert_log" in step:
            if not self.capture:
                raise RuntimeError("assert_log 需要启用日志捕获")
            kw = step["assert_log"]
            snap = self.capture.snapshot()
            hit = any(kw in l.message for l in snap.lines)
            if not hit:
                raise AssertionError(f"日志中未出现: {kw}")
            return f"assert_log OK: {kw}"
        if "screenshot" in step:
            return "screenshot"
        raise ValueError(f"未知步骤: {step}")

    # ---- 用例执行 ----

    def run(self, case: dict, serial: Optional[str] = None,
            package: str = "") -> tuple[ExecutionTrace, Optional[LogBundle]]:
        """执行一个用例 dict，返回 (ExecutionTrace, LogBundle|None)。"""
        name = case.get("name", "unnamed")
        timeout = float(case.get("timeout", 300))
        trace = ExecutionTrace(case_name=name, serial=serial or "",
                               started_at=time.time())

        if self.capture:
            self.capture.clear()
            self.capture.start()
        try:
            for i, step in enumerate(case.get("steps", [])):
                if time.time() - trace.started_at > timeout:
                    trace.passed = False
                    trace.failure_step = i
                    trace.failure_reason = "用例超时"
                    break
                t0 = time.monotonic()
                if self.capture:
                    self.capture.mark(f"step {i}: {step}")
                sr = StepResult(index=i, action=next(iter(step)))
                try:
                    sr.detail = self._do_step(step, serial)
                    sr.ok = True
                except Exception as e:  # noqa: BLE001 —— 失败要落现场
                    sr.ok = False
                    sr.detail = str(e)
                    trace.passed = False
                    trace.failure_step = i
                    trace.failure_reason = str(e)
                sr.duration_ms = int((time.monotonic() - t0) * 1000)
                sr.activity = self._current_activity(serial)
                sr.screenshot = self._screenshot(serial, f"{name}_step{i}")
                trace.steps.append(sr)
                if not sr.ok:
                    break
        finally:
            trace.finished_at = time.time()
            if self.capture:
                self.capture.mark("case finished")
                time.sleep(0.5)  # 等崩溃日志吐完
                self.capture.stop()
        bundle = self.capture.snapshot() if self.capture else None
        return trace, bundle


def load_case(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)
