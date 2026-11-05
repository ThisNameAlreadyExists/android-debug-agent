"""探索模式（Explorer）—— LLM 自主探索找 bug（设计方案 §3.3 探索模式）。

循环：UI 树 + 当前 Activity → LLM 决策下一步（JSON 动作）→ 执行 → 循环。
纪律约束（§6.4）：步数上限 + 界面指纹循环检测 + 崩溃即停。
LLM 不可用时直接报错，不做伪智能降级。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from .channel import AdbChannel
from .llm import OllamaClient, scrub
from .logcapture import LogcatCapture
from .uiautomator import UiInspector, UiNode


@dataclass
class ExploreResult:
    goal: str
    steps_taken: int = 0
    crashed: bool = False
    actions: list[str] = field(default_factory=list)
    stopped_reason: str = ""
    duration_s: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


_ACTION_PROMPT = """你在操控一台安卓手机做探索性测试。目标：{goal}

当前 Activity：{activity}
当前界面可交互控件（序号 | 文字 | 描述 | 资源ID）：
{widgets}

已执行动作：{history}

请输出下一步动作，严格 JSON（不要输出其他任何内容）：
{{"action": "tap"|"input"|"back"|"scroll_down"|"wait"|"done",
  "index": 控件序号（tap/input 必填）,
  "text": "要输入的文字（input 必填）",
  "reason": "一句话说明意图"}}
规则：优先点击未探索过的控件；输入框用合理测试数据；发现目标达成或无可探索内容时 done。
禁止点击：卸载、清除数据、删除、支付、发送短信、拨打电话类按钮。"""

# 危险动作黑名单（双保险，prompt 之外再拦一层）
_FORBIDDEN = re.compile(r"卸载|清除数据|删除|支付|付款|发送短信|拨打电话|格式化|恢复出厂", re.I)


class Explorer:
    def __init__(self, channel: AdbChannel, llm: OllamaClient,
                 serial: Optional[str] = None, package: str = "",
                 max_steps: int = 30, loop_threshold: int = 3):
        self.ch = channel
        self.llm = llm
        self.serial = serial
        self.package = package
        self.max_steps = max_steps
        self.loop_threshold = loop_threshold
        self.ui = UiInspector(channel)

    def _ui_fingerprint(self, nodes: list[UiNode]) -> str:
        sig = "|".join(f"{n.resource_id or n.text}" for n in nodes[:50])
        return hashlib.sha1(sig.encode()).hexdigest()[:12]

    def _current_activity(self) -> str:
        out = self.ch.shell(
            "dumpsys activity activities | grep -m1 ResumedActivity",
            serial=self.serial).stdout
        m = re.search(r"\s(\S+/\S+)", out)
        return m.group(1) if m else ""

    def _decide(self, nodes: list[UiNode], activity: str,
                history: list[str]) -> dict:
        widgets = []
        for i, n in enumerate(nodes):
            if n.clickable or n.text:
                widgets.append(f"{i} | {n.text[:30]} | {n.content_desc[:30]} | {n.resource_id}")
        prompt = _ACTION_PROMPT.format(
            goal=self._goal, activity=activity,
            widgets=scrub("\n".join(widgets[:40])) or "（无可交互控件）",
            history=" → ".join(history[-8:]) or "（无）")
        text = self.llm.chat(prompt, system="你是安卓探索性测试 Agent，只输出 JSON。")
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError(f"LLM 未输出 JSON: {text[:200]}")
        return json.loads(m.group(0))

    def _execute(self, decision: dict, nodes: list[UiNode]) -> str:
        act = decision.get("action", "")
        reason = decision.get("reason", "")
        idx = decision.get("index")
        if act in ("tap", "input"):
            if idx is None or not (0 <= int(idx) < len(nodes)):
                raise ValueError(f"非法控件序号: {idx}")
            node = nodes[int(idx)]
            label = node.text or node.content_desc or node.resource_id
            if _FORBIDDEN.search(label):
                raise PermissionError(f"黑名单控件已拦截: {label}")
            if act == "tap":
                self.ui.tap_node(node, self.serial)
                return f"tap[{label}] ({reason})"
            text = str(decision.get("text", "test")).replace(" ", "%s")
            self.ui.tap_node(node, self.serial)  # 先聚焦
            time.sleep(0.3)
            self.ch.shell(f"input text {text}", serial=self.serial)
            return f"input[{label}]='{decision.get('text')}' ({reason})"
        if act == "back":
            self.ch.shell("input keyevent KEYCODE_BACK", serial=self.serial)
            return f"back ({reason})"
        if act == "scroll_down":
            self.ch.shell("input swipe 360 1000 360 400 300", serial=self.serial)
            return f"scroll_down ({reason})"
        if act == "wait":
            time.sleep(2)
            return f"wait ({reason})"
        if act == "done":
            return "done"
        raise ValueError(f"未知动作: {act}")

    def explore(self, goal: str, capture: Optional[LogcatCapture] = None) -> ExploreResult:
        """主循环。崩溃即停（交给诊断层）。"""
        self._goal = goal
        result = ExploreResult(goal=goal)
        history: list[str] = []
        fp_count: dict[str, int] = {}
        t0 = time.time()
        if capture:
            capture.clear()
            capture.start()
        try:
            for step in range(self.max_steps):
                if capture and capture.has_crash():
                    result.crashed = True
                    result.stopped_reason = f"第 {step} 步后检测到崩溃"
                    break
                nodes = self.ui.dump(self.serial)
                fp = self._ui_fingerprint(nodes)
                fp_count[fp] = fp_count.get(fp, 0) + 1
                if fp_count[fp] > self.loop_threshold:
                    result.stopped_reason = f"界面循环检测：同一界面重复 {self.loop_threshold} 次"
                    break
                try:
                    decision = self._decide(nodes, self._current_activity(), history)
                except Exception as e:
                    result.stopped_reason = f"LLM 决策失败: {e}"
                    break
                try:
                    desc = self._execute(decision, nodes)
                except (ValueError, PermissionError) as e:
                    history.append(f"非法动作已拦截: {e}")
                    continue
                if desc == "done":
                    result.stopped_reason = "LLM 判断目标达成"
                    break
                history.append(desc)
                result.actions.append(desc)
                result.steps_taken += 1
                if capture:
                    capture.mark(f"explore step {step}: {desc}")
                time.sleep(1.2)  # 等界面稳定（tap_and_wait 的朴素版）
            else:
                result.stopped_reason = f"达到步数上限 {self.max_steps}"
        finally:
            result.duration_s = time.time() - t0
            if capture:
                time.sleep(0.5)
                capture.stop()
        return result
