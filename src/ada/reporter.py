"""报告模块（Reporter）—— Markdown 诊断报告。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from .analyst import Diagnosis
from .device import DeviceProfile
from .logcapture import LogBundle
from .scenario import ExecutionTrace


def render_markdown(
    trace: Optional[ExecutionTrace] = None,
    diagnoses: Optional[list[Diagnosis]] = None,
    profile: Optional[DeviceProfile] = None,
    bundle: Optional[LogBundle] = None,
    title: str = "安卓调试诊断报告",
) -> str:
    parts: list[str] = [f"# {title}\n"]

    if profile:
        p = profile
        parts.append("## 环境信息\n")
        parts.append(f"- 设备：{p.brand} {p.model}（{p.serial}）")
        parts.append(f"- 系统：Android {p.android_release} (SDK {p.sdk})，ROM: {p.rom}，语言: {p.locale or '未知'}")
        parts.append(f"- 构建指纹：{p.build_fingerprint or '未知'}")
        parts.append(f"- 增量版本：{p.incremental or '未知'}，安全补丁：{p.security_patch or '未知'}")
        parts.append(f"- 内核：{p.kernel or '未知'}，基带：{p.baseband or '未知'}")
        parts.append(f"- CPU：{p.cpu_hardware or '未知'}（{p.cpu_cores} 核），GPU：{p.gpu_renderer or '未知'}")
        parts.append(f"- 内存：{p.ram_total_mb}MB 总 / {p.ram_avail_mb}MB 可用，存储：{p.storage_free_gb}GB 可用 / {p.storage_total_gb}GB 总")
        parts.append(f"- 屏幕：{p.screen}@{p.density}dpi，刷新率 {p.refresh_rate}Hz，ABI：{p.abi}")
        parts.append(f"- 电池：{p.battery_level}% · {p.battery_temp_c}℃ · {p.battery_voltage_mv}mV · {p.battery_status or '未知'}，root: {'是' if p.rooted else '否'}\n")

    if trace:
        parts.append("## 执行过程\n")
        status = "通过" if trace.passed else f"失败（第 {trace.failure_step} 步）"
        dur = trace.finished_at - trace.started_at
        parts.append(f"- 用例：{trace.case_name}，结果：**{status}**，耗时 {dur:.1f}s\n")
        parts.append("| # | 动作 | 结果 | 详情 | 截图 |")
        parts.append("|---|---|---|---|---|")
        for s in trace.steps:
            shot = f"![]({Path(s.screenshot).name})" if s.screenshot else ""
            parts.append(
                f"| {s.index} | {s.action} | {'✅' if s.ok else '❌'} | {s.detail} | {shot} |")
        if trace.failure_reason:
            parts.append(f"\n失败原因：{trace.failure_reason}\n")

    if diagnoses:
        parts.append("## 诊断结论\n")
        for i, d in enumerate(diagnoses, 1):
            parts.append(f"### 问题 {i}：{d.category}（置信度 {d.confidence:.0%}）\n")
            parts.append(f"- 根因：{d.root_cause}")
            if d.source_location:
                loc = d.source_location
                parts.append(f"- 源码定位：`{loc.get('symbol','')}` → {loc.get('file','')}:{loc.get('line','')}")
            parts.append(f"- 崩溃指纹：`{d.fingerprint}`")
            parts.append(f"- 修复建议：{d.suggested_fix}")
            if d.evidence:
                parts.append("\n证据：\n```")
                parts.append(d.evidence[0])
                parts.append("```\n")

    if bundle and bundle.markers:
        parts.append("## 时间轴标记\n")
        for m in bundle.markers:
            ts = time.strftime("%H:%M:%S", time.localtime(m["ts"]))
            parts.append(f"- {ts} — {m['label']}")

    return "\n".join(parts) + "\n"


def write_report(md: str, path: str) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    return path
