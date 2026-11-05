"""用例沉淀与修复验证闭环（设计方案 §8.4 / §8.5）。

- caselib：把复现用例固化为 YAML 入库（regression_cases/），支持全量回归
- verify_fix：装新 APK → 重跑原场景 → 指纹消失且步骤全过 = 修复确认
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import yaml


# ---- 用例库 ----

def sink_case(case: dict, fingerprint: str, caselib_dir: str | Path) -> str:
    """把用例固化入库。文件名含指纹，天然去重（同指纹覆盖最新版）。"""
    d = Path(caselib_dir)
    d.mkdir(parents=True, exist_ok=True)
    doc = dict(case)
    doc.setdefault("name", f"repro_{fingerprint}")
    doc["meta"] = {
        "fingerprint": fingerprint,
        "sunk_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "origin": "auto-sink",
    }
    path = d / f"repro_{fingerprint}.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)
    return str(path)


def load_caselib(caselib_dir: str | Path) -> list[dict]:
    d = Path(caselib_dir)
    if not d.exists():
        return []
    cases = []
    for p in sorted(d.glob("*.yaml")):
        with open(p, encoding="utf-8") as f:
            cases.append(yaml.safe_load(f))
    return cases


# ---- 修复验证闭环 ----

@dataclass
class VerifyResult:
    fixed: bool
    reason: str
    fingerprints_now: list[str] = field(default_factory=list)
    case_sunk: str = ""


def verify_fix(
    fingerprints_before: list[str],
    run_case: Callable[[dict], tuple[bool, list[str]]],
    case: dict,
    db_mark_fixed: Callable[[str], int],
    caselib_dir: str | Path,
    max_rounds: int = 3,
) -> VerifyResult:
    """验证修复：重跑场景，原指纹全部消失且用例通过 = 修复确认。

    run_case(case) -> (passed, fingerprints)
    修复确认后：CrashDB 标记 fixed + 用例沉淀入库。
    """
    passed, fps = run_case(case)
    still = [fp for fp in fingerprints_before if fp in fps]
    if still:
        return VerifyResult(False,
                            f"原崩溃指纹仍重现: {still}", fps)
    if not passed:
        return VerifyResult(False,
                            "崩溃消失但用例未通过（可能有新问题，需人工看报告）", fps)
    for fp in fingerprints_before:
        db_mark_fixed(fp)
    sunk = sink_case(case, fingerprints_before[0] if fingerprints_before else "unknown",
                     caselib_dir)
    return VerifyResult(True, "修复确认：原指纹未重现且用例全过",
                        fps, case_sunk=sunk)
