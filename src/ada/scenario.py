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
        self._last_shot_error = ""
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
            # 设备休眠时部分机型 screencap 静默失败（rc=1 无报错）：唤醒后重试一次
            self.ch.shell("input keyevent KEYCODE_WAKEUP", serial=serial)
            time.sleep(1.0)
            r1 = self.ch.shell(f"screencap -p {remote}", serial=serial)
        if not r1.ok:
            self._last_shot_error = "screencap 失败（已尝试唤醒重试）"
            return ""
        r2 = self.ch.pull(remote, str(path), serial=serial)
        self.ch.shell(f"rm -f {remote}", serial=serial)
        if not r2.ok:
            self._last_shot_error = f"pull 失败: {r2.stderr or r2.stdout}"
            return ""
        return str(path)

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
        if "set_switch" in step:
            # 确定性开关：检查当前状态，不符才点（可复现的关键）
            spec = step["set_switch"]
            want = bool(spec.get("on", True))
            nodes = self.ui.dump(serial)
            node = find_node(nodes, resource_id=spec.get("id"), text=spec.get("text"))
            if node is None:
                raise LookupError(f"未找到开关: {spec}")
            if node.checked != want:
                self.ui.tap_node(node, serial)
                return f"set_switch {spec.get('id') or spec.get('text')} → {'ON' if want else 'OFF'}"
            return f"set_switch 已是目标状态 {'ON' if want else 'OFF'}"
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
                # 前台守护： kiosk 类设备的后台看门狗会把系统应用顶到前台，
                # 交互类步骤前确认被测应用仍在前台，否则拉起
                if package and any(k in step for k in ("tap", "input", "set_switch", "assert_log")):
                    cur = self._current_activity(serial)
                    if cur and package not in cur:
                        self.app.launch(package, serial=serial)
                        time.sleep(1.5)
                        if self.capture:
                            self.capture.mark(f"前台守护: {cur} 抢前台，已拉回 {package}")
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
                if not sr.screenshot and self._last_shot_error:
                    sr.detail += f"（截图失败：{self._last_shot_error}）"
                    self._last_shot_error = ""
                trace.steps.append(sr)
                if not sr.ok:
                    break
        finally:
            trace.finished_at = time.time()
            if self.capture:
                # 崩溃宽限期：崩溃常滞后最后一步操作 1~2s，等它浮现再停采
                deadline = time.time() + 5.0
                while time.time() < deadline and not self.capture.has_crash():
                    time.sleep(0.5)
                self.capture.mark("case finished")
                self.capture.stop()
        bundle = self.capture.snapshot() if self.capture else None
        return trace, bundle


def load_case(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)
