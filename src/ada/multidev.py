"""多设备对照诊断（设计方案 §8.6）。

同一用例在多台设备并行执行（每设备独立线程 + 独立通道/捕获实例，
ADB 命令按 serial 路由天然隔离），执行后 diff 三项：
1. 通过/失败差异（哪台挂了哪台过了）
2. 崩溃指纹差异（是否设备特异）
3. 执行路径差异（首个 Activity 分叉点 → 定位 UI 流程差异）
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from .analyst import LogAnalyst
from .channel import AdbChannel
from .logcapture import LogcatCapture
from .scenario import ScenarioRunner


@dataclass
class DeviceResult:
    serial: str
    passed: bool
    failure_step: int = -1
    failure_reason: str = ""
    fingerprints: list[str] = field(default_factory=list)
    activities: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class DiffReport:
    total: int
    passed_serials: list[str] = field(default_factory=list)
    failed_serials: list[str] = field(default_factory=list)
    device_specific_fingerprints: dict[str, list[str]] = field(default_factory=dict)
    first_divergence_step: int = -1
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _run_on(serial: str, case: dict, package: str, workdir: Path,
            results: dict, lock: threading.Lock) -> None:
    try:
        # 每设备独立通道（独立审计文件），ADB 层按 serial 隔离
        ch = AdbChannel(audit_log=workdir / f"audit_{serial}.jsonl")
        cap = LogcatCapture(ch, serial=serial, package=case.get("package", ""))
        runner = ScenarioRunner(ch, workdir=workdir / f"dev_{serial}", capture=cap)
        trace, bundle = runner.run(case, serial=serial)
        diagnoses = LogAnalyst().analyze(bundle, case.get("package", ""),
                                         use_llm=False) if bundle else []
        res = DeviceResult(
            serial=serial,
            passed=trace.passed,
            failure_step=trace.failure_step,
            failure_reason=trace.failure_reason,
            fingerprints=[d.fingerprint for d in diagnoses],
            activities=[s.activity for s in trace.steps],
        )
    except Exception as e:  # noqa: BLE001 —— 单设备失败不影响其他设备
        res = DeviceResult(serial=serial, passed=False, error=str(e))
    with lock:
        results[serial] = res


def run_multi(case: dict, serials: list[str], package: str,
              workdir: str | Path) -> tuple[list[DeviceResult], DiffReport]:
    """并行执行 + 对照 diff。"""
    workdir = Path(workdir)
    results: dict[str, DeviceResult] = {}
    lock = threading.Lock()
    threads = [threading.Thread(target=_run_on,
                                args=(s, case, package, workdir, results, lock))
               for s in serials]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    devs = [results[s] for s in serials]
    report = DiffReport(total=len(devs))
    report.passed_serials = [d.serial for d in devs if d.passed]
    report.failed_serials = [d.serial for d in devs if not d.passed]

    # 指纹 diff：只在部分设备出现的崩溃 = 设备特异
    all_fps: dict[str, list[str]] = {}
    for d in devs:
        for fp in d.fingerprints:
            all_fps.setdefault(fp, []).append(d.serial)
    report.device_specific_fingerprints = {
        fp: ss for fp, ss in all_fps.items() if len(ss) < len(devs)}

    # 路径分叉：逐设备比较每步 Activity
    max_len = max((len(d.activities) for d in devs), default=0)
    for i in range(max_len):
        acts = {d.activities[i] for d in devs if i < len(d.activities)}
        if len(acts) > 1:
            report.first_divergence_step = i
            report.notes.append(
                f"第 {i} 步起设备路径分叉：{dict((d.serial, d.activities[i]) for d in devs if i < len(d.activities))}")
            break

    if report.device_specific_fingerprints:
        for fp, ss in report.device_specific_fingerprints.items():
            report.notes.append(f"指纹 {fp} 仅在 {ss} 出现 → 疑似设备/ROM 特异问题")
    if not report.failed_serials:
        report.notes.append("全部设备通过")
    return devs, report
