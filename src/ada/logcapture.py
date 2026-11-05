"""日志捕获模块（Log Capture）。

- logcat 独立线程流式采集 + ring buffer（崩溃时缓冲区不丢）
- 行级结构化解析（时间戳/level/tag/pid/message）
- FATAL EXCEPTION / native crash 实时检测
- 时间轴标记：操作步骤打统一时间戳，供因果关联
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Optional

from .channel import AdbChannel


@dataclass
class LogLine:
    ts: float          # 本机时间戳（采集时刻，保证与步骤标记同一时钟）
    device_ts: str     # 设备 logcat 时间戳
    level: str
    tag: str
    pid: int
    message: str


_THREADTIME_RE = re.compile(
    r"^(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)\s+(\d+)\s+\d+\s+([VDIWEA])\s+(\S+?)\s*:\s(.*)$")

FATAL_RE = re.compile(r"FATAL EXCEPTION|FATAL SIGNAL|signal \d+ \(SIG")
ANR_RE = re.compile(r"ANR in |Application Not Responding", re.I)


@dataclass
class LogBundle:
    package: str = ""
    lines: list[LogLine] = field(default_factory=list)
    crash_indices: list[int] = field(default_factory=list)  # lines 中崩溃起点
    anr_indices: list[int] = field(default_factory=list)
    markers: list[dict] = field(default_factory=list)       # 时间轴标记 {ts, label}

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


class LogcatCapture:
    """后台线程跑 adb logcat，持续写入 ring buffer。"""

    def __init__(self, channel: AdbChannel, serial: Optional[str] = None,
                 package: str = "", level: str = "V",
                 buffer_size: int = 20000):
        self.ch = channel
        self.serial = serial
        self.package = package
        self.level = level
        self._pid: int = 0  # 目标进程 pid，start 时解析
        self.buffer: deque[LogLine] = deque(maxlen=buffer_size)
        self.crash_indices: list[int] = []
        self.anr_indices: list[int] = []
        self.markers: list[dict] = []
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._line_count = 0

    def mark(self, label: str) -> None:
        """打时间轴标记（操作步骤与日志对齐的关键）。"""
        with self._lock:
            self.markers.append({"ts": time.time(), "label": label})

    def _parse_line(self, raw: str, ts: float) -> Optional[LogLine]:
        m = _THREADTIME_RE.match(raw)
        if not m:
            if raw.startswith("---------") or not raw.strip():
                return None
            return LogLine(ts, "", "I", "", 0, raw.strip())
        return LogLine(ts, m.group(1), m.group(3), m.group(4),
                       int(m.group(2)), m.group(5))

    def _reader(self) -> None:
        assert self._proc and self._proc.stdout
        for raw in self._proc.stdout:
            if self._stop.is_set():
                break
            ts = time.time()
            line = self._parse_line(raw.rstrip("\n"), ts)
            if line is None:
                continue
            # 包过滤：已知 pid 时只保留该 pid 的行 + AndroidRuntime/DEBUG 等系统崩溃通道；
            # pid 未知（应用未启动）退化为消息含包名的行
            if self.package:
                if self._pid and line.pid != self._pid and \
                        line.tag not in ("AndroidRuntime", "DEBUG", "ActivityManager"):
                    continue
                if not self._pid and line.pid == 0 and self.package not in line.message:
                    continue
            with self._lock:
                idx = self._line_count
                self.buffer.append(line)
                if FATAL_RE.search(line.message):
                    self.crash_indices.append(idx)
                if ANR_RE.search(line.message):
                    self.anr_indices.append(idx)
                self._line_count += 1

    def start(self) -> None:
        if self.package:
            out = self.ch.shell(f"pidof {self.package}", serial=self.serial)
            first = out.stdout.split()[0] if out.ok and out.stdout.split() else ""
            self._pid = int(first) if first.isdigit() else 0
        args = [self.ch.adb_path]
        if self.serial:
            args += ["-s", self.serial]
        args += ["logcat", "-v", "threadtime", f"*:{self.level}"]
        self._proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, errors="replace", bufsize=1)
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def clear(self) -> None:
        """清设备 logcat 缓冲，保证采集从干净态开始。"""
        self.ch.adb(["logcat", "-c"], serial=self.serial)

    def stop(self) -> None:
        self._stop.set()
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._thread:
            self._thread.join(timeout=5)

    def snapshot(self, context_window: int = 200) -> LogBundle:
        """导出当前缓冲为结构化 LogBundle。"""
        with self._lock:
            lines = list(self.buffer)
            return LogBundle(
                package=self.package,
                lines=lines[-context_window * 5:] if len(lines) > context_window * 5 else lines,
                crash_indices=list(self.crash_indices),
                anr_indices=list(self.anr_indices),
                markers=list(self.markers),
            )

    def has_crash(self) -> bool:
        with self._lock:
            return bool(self.crash_indices)
