"""日志解析与诊断模块（Log Analyst）—— 规则层（MVP）。

三级漏斗中的前两级（MVP 版）：指纹计算 + 规则模式库。
LLM 层在二期接入；CrashDB 持久化二期接入（指纹已在此产出）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from typing import Optional

from .logcapture import LogBundle, LogLine
from .llm import LLMError, OllamaClient, scrub


@dataclass
class CrashFrame:
    class_method: str   # e.g. com.example.LoginViewModel.onResult
    file: str = ""      # e.g. LoginViewModel.kt
    line: int = 0


@dataclass
class CrashInfo:
    exception: str      # e.g. java.lang.NullPointerException
    message: str = ""
    frames: list[CrashFrame] = field(default_factory=list)
    native_signal: str = ""   # SIGSEGV / SIGABRT ...
    raw: str = ""


_FRAME_RE = re.compile(
    r"^\s*at\s+([\w.$]+\.[\w$<>]+)\(([\w.]+(?:\.kt|\.java))?(?::(\d+))?\)")
_EX_RE = re.compile(r"([\w.$]+(?:Exception|Error|Throwable))(?::\s*(.*))?")
_SIGNAL_RE = re.compile(r"signal\s+\d+\s+\((SIG\w+)\)")


def parse_crash(lines: list[LogLine], start: int) -> Optional[CrashInfo]:
    """从崩溃起点解析结构化崩溃信息。"""
    window = lines[start:start + 120]
    if not window:
        return None
    raw = "\n".join(l.message for l in window)

    sig = _SIGNAL_RE.search(raw)
    ex = _EX_RE.search(raw)
    if not ex and not sig:
        return None

    frames: list[CrashFrame] = []
    for l in window:
        m = _FRAME_RE.match(l.message)
        if m:
            frames.append(CrashFrame(
                class_method=m.group(1),
                file=m.group(2) or "",
                line=int(m.group(3)) if m.group(3) else 0))
    return CrashInfo(
        exception=ex.group(1) if ex else "",
        message=(ex.group(2) or "") if ex else "",
        frames=frames,
        native_signal=sig.group(1) if sig else "",
        raw=raw[:4000],
    )


def fingerprint(crash: CrashInfo, package: str = "") -> str:
    """崩溃指纹：异常类型 + Top3 帧（去行号）+ 包名 → 跨版本可比对。"""
    top = "|".join(f.class_method for f in crash.frames[:3])
    base = f"{package}|{crash.exception or crash.native_signal}|{top}"
    return hashlib.sha1(base.encode()).hexdigest()[:16]


@dataclass
class Diagnosis:
    category: str           # npe / oom / anr / security / native / illegal_state / unknown
    root_cause: str
    source_location: dict = field(default_factory=dict)  # {file, line, symbol}
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0
    fingerprint: str = ""
    suggested_fix: str = ""
    llm_used: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ---- 规则模式库 ----

def _rule_npe(c: CrashInfo) -> Diagnosis:
    loc = {}
    for f in c.frames:
        if f.file:
            loc = {"file": f.file, "line": f.line, "symbol": f.class_method}
            break
    return Diagnosis(
        category="npe",
        root_cause=f"空指针异常：{c.message or '对 null 对象成员访问'}",
        source_location=loc,
        evidence=[c.raw[:800]],
        confidence=0.9 if loc else 0.7,
        suggested_fix="在首个业务栈帧处补判空；检查异步回调时序（回调返回时 UI/数据可能已销毁）",
    )


def _rule_oom(c: CrashInfo) -> Diagnosis:
    return Diagnosis(
        category="oom",
        root_cause="内存溢出：堆耗尽或大对象分配失败",
        evidence=[c.raw[:800]],
        confidence=0.85,
        suggested_fix="检查大图加载（采样压缩）、内存泄漏（LeakCanary）、大缓存策略",
    )


def _rule_security(c: CrashInfo) -> Diagnosis:
    m = (re.search(r"(?:need|requires?)\s+(android\.permission\.[\w]+)", c.message)
         or re.search(r"permission\s+([\w.]+)", c.message, re.I))
    perm = m.group(1) if m else ""
    # 过滤误命中的泛词
    if perm.lower() in ("denial", "denied", ""):
        perm = ""
    return Diagnosis(
        category="security",
        root_cause=f"权限缺失：{perm or c.message}",
        evidence=[c.raw[:800]],
        confidence=0.9,
        suggested_fix=f"运行时申请权限 {perm}（或确认测试前置已 pm grant）",
    )


def _rule_native(c: CrashInfo) -> Diagnosis:
    return Diagnosis(
        category="native",
        root_cause=f"Native 崩溃 {c.native_signal}：native 层非法内存访问/断言",
        evidence=[c.raw[:800]],
        confidence=0.6,
        suggested_fix="拉取 tombstone 并用 ndk-stack / addr2line 符号化；检查 JNI 边界与 so 版本匹配",
    )


def _rule_illegal_state(c: CrashInfo) -> Diagnosis:
    loc = {}
    for f in c.frames:
        if f.file:
            loc = {"file": f.file, "line": f.line, "symbol": f.class_method}
            break
    return Diagnosis(
        category="illegal_state",
        root_cause=f"非法状态调用：{c.message}",
        source_location=loc,
        evidence=[c.raw[:800]],
        confidence=0.75,
        suggested_fix="常见于生命周期错位（Fragment commit after onSaveInstanceState 等），检查状态机与生命周期时序",
    )


_RULES: list[tuple[re.Pattern, object]] = [
    (re.compile(r"NullPointerException"), _rule_npe),
    (re.compile(r"OutOfMemoryError"), _rule_oom),
    (re.compile(r"SecurityException"), _rule_security),
    (re.compile(r"IllegalStateException"), _rule_illegal_state),
]


def _rule_anr(lines: list[LogLine], idx: int) -> Diagnosis:
    window = lines[idx:idx + 40]
    raw = "\n".join(l.message for l in window)
    m = re.search(r"ANR in (\S+).*?Reason:\s*(.+)", raw, re.S)
    reason = m.group(2).strip().splitlines()[0] if m else "主线程阻塞"
    return Diagnosis(
        category="anr",
        root_cause=f"ANR：{reason}",
        evidence=[raw[:800]],
        confidence=0.75,
        suggested_fix="主线程存在耗时操作（IO/网络/锁等待），移到后台线程；检查 broadcast/service 超时路径",
    )


class LogAnalyst:
    """三级漏斗：规则层（本类内置）→ LLM 层（可选注入）。指纹由 CrashDB 层处理。"""

    def __init__(self, llm: Optional[OllamaClient] = None):
        self.llm = llm

    def analyze(self, bundle: LogBundle, package: str = "",
                use_llm: bool = True,
                context: str = "") -> list[Diagnosis]:
        lines = bundle.lines
        out: list[Diagnosis] = []
        for idx in bundle.crash_indices:
            local = min(max(0, idx), len(lines) - 1)
            crash = parse_crash(lines, local)
            if not crash:
                continue
            diag = self._match(crash, package)
            if diag.category == "unknown" and use_llm and self.llm:
                diag = self._llm_diagnose(crash, package, diag, context)
            out.append(diag)
        for idx in bundle.anr_indices:
            local = min(idx, len(lines) - 1)
            out.append(_rule_anr(lines, local))
        return out

    def _llm_diagnose(self, crash: CrashInfo, package: str,
                      fallback: Diagnosis, context: str = "") -> Diagnosis:
        """LLM 层：规则未命中时泛化推断。输入先脱敏；失败降级回规则结论。"""
        prompt = (
            "分析以下安卓崩溃日志，输出严格的 JSON（不要输出其他任何内容）：\n"
            '{"root_cause": "根因一句话", "category": "英文小写分类", '
            '"suggested_fix": "修复建议", "confidence": 0.0到1.0}\n\n'
            f"应用包名：{package}\n{f'场景上下文：{context}' if context else ''}\n"
            f"崩溃日志：\n{scrub(crash.raw)}")
        try:
            import json as _json
            text = self.llm.chat(prompt)  # type: ignore
            m = re.search(r"\{.*\}", text, re.S)
            data = _json.loads(m.group(0)) if m else {}
            if not data.get("root_cause"):
                raise ValueError("LLM 输出缺 root_cause")
            return Diagnosis(
                category=str(data.get("category", "unknown")),
                root_cause=str(data["root_cause"]),
                evidence=[crash.raw[:800]],
                confidence=min(max(float(data.get("confidence", 0.5)), 0.0), 1.0),
                fingerprint=fingerprint(crash, package),
                suggested_fix=str(data.get("suggested_fix", "")),
                llm_used=True,
            )
        except (LLMError, ValueError, KeyError):
            return fallback

    def _match(self, crash: CrashInfo, package: str) -> Diagnosis:
        text = crash.exception + " " + crash.message
        if crash.native_signal and not crash.exception:
            diag = _rule_native(crash)
        else:
            diag = None
            for pat, fn in _RULES:
                if pat.search(text):
                    diag = fn(crash)  # type: ignore
                    break
            if diag is None:
                diag = Diagnosis(
                    category="unknown",
                    root_cause=f"未匹配规则：{crash.exception} {crash.message}",
                    evidence=[crash.raw[:800]],
                    confidence=0.3,
                    suggested_fix="规则库未覆盖，建议接入 LLM 层诊断（二期）",
                )
        diag.fingerprint = fingerprint(crash, package or "")
        return diag
