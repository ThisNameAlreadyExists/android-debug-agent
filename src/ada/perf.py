"""性能基线回归（设计方案 §8.10）。

每次执行顺带采集：冷启动时长（am start -W）、内存峰值（dumpsys meminfo）、
帧耗时 P95（dumpsys gfxinfo framestats 不可用时退化为 aggregate 统计）。
与历史基线（SQLite）对比，超阈值默认 +20% 即报性能回归。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from .channel import AdbChannel


_SCHEMA = """
CREATE TABLE IF NOT EXISTS perf (
    package TEXT NOT NULL,
    device_model TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_perf ON perf(package, device_model, metric);
"""


@dataclass
class PerfSample:
    startup_ms: float = 0.0
    mem_total_kb: float = 0.0
    frame_p95_ms: float = 0.0


@dataclass
class PerfRegression:
    metric: str
    baseline: float
    current: float
    ratio: float

    @property
    def text(self) -> str:
        return (f"性能回归 [{self.metric}]：基线 {self.baseline:.0f} → 本次 {self.current:.0f}"
                f"（+{(self.ratio - 1) * 100:.0f}%）")


def collect_perf(ch: AdbChannel, serial: Optional[str], package: str) -> PerfSample:
    """顺带采集性能指标（冷启动 + 内存 + 帧耗时）。失败项记 0。"""
    s = PerfSample()
    # 先解析 launcher Activity（am start -W 只给 -p 不指定组件会静默失败）
    r0 = ch.shell(f"cmd package resolve-activity --brief {package} | tail -1",
                  serial=serial)
    comp = r0.stdout.strip() if r0.ok else ""
    # 冷启动
    ch.shell(f"am force-stop {package}", serial=serial)
    res = ch.shell(f"am start -W -n {comp} | grep TotalTime" if comp
                   else f"am start -W -p {package} | grep TotalTime", serial=serial)
    m = re.search(r"TotalTime:\s*(\d+)", res.stdout)
    if m:
        s.startup_ms = float(m.group(1))
    time.sleep(1.5)  # 等进程起来再采内存
    # 内存
    res = ch.shell(f"dumpsys meminfo {package} | grep -m1 TOTAL", serial=serial)
    m = re.search(r"TOTAL\s+(\d+)", res.stdout)
    if m:
        s.mem_total_kb = float(m.group(1))
    # 帧耗时 P95（gfxinfo 汇总行，各版本格式不同，宽松匹配）
    res = ch.shell(f"dumpsys gfxinfo {package}", serial=serial)
    nums = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)ms", res.stdout)]
    if nums:
        nums.sort()
        s.frame_p95_ms = nums[int(len(nums) * 0.95) - 1] if len(nums) >= 2 else nums[-1]
    return s


class PerfBaseline:
    def __init__(self, path: str | Path):
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(_SCHEMA)

    def close(self):
        self._conn.close()

    def record(self, package: str, model: str, sample: PerfSample) -> None:
        cur = self._conn.cursor()
        for metric, value in asdict(sample).items():
            if value > 0:
                cur.execute("INSERT INTO perf VALUES (?,?,?,?,?)",
                            (package, model, metric, value, time.time()))
        self._conn.commit()

    def baseline(self, package: str, model: str, metric: str, last_n: int = 10) -> float:
        """基线 = 最近 N 次中位数。无历史返回 0。"""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT value FROM perf WHERE package=? AND device_model=? AND metric=? "
            "ORDER BY ts DESC LIMIT ?", (package, model, metric, last_n))
        vals = sorted(r[0] for r in cur.fetchall())
        if not vals:
            return 0.0
        return vals[len(vals) // 2]

    def check(self, package: str, model: str, sample: PerfSample,
              threshold: float = 1.2) -> list[PerfRegression]:
        """对比基线，返回超阈值回归项。首次无基线不报。"""
        out = []
        for metric, value in asdict(sample).items():
            if value <= 0:
                continue
            base = self.baseline(package, model, metric)
            if base > 0 and value > base * threshold:
                out.append(PerfRegression(metric, base, value, value / base))
        return out
