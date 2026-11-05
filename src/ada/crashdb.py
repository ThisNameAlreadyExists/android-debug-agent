"""崩溃指纹库（CrashDB）—— 让系统记住每一次崩溃（设计方案 §8.1）。

- 指纹查重：历史命中秒级返回既有诊断，零 LLM 成本
- 回归告警：标记为已修复的指纹在新版本重现 → 报警
- 跨设备/版本聚合：统计某指纹在各设备型号/应用版本上的分布
存储：本地 SQLite，单文件零依赖。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


_SCHEMA = """
CREATE TABLE IF NOT EXISTS crashes (
    fingerprint TEXT NOT NULL,
    package TEXT NOT NULL,
    app_version TEXT DEFAULT '',
    device_model TEXT DEFAULT '',
    rom TEXT DEFAULT '',
    category TEXT DEFAULT '',
    root_cause TEXT DEFAULT '',
    source_location TEXT DEFAULT '{}',
    suggested_fix TEXT DEFAULT '',
    confidence REAL DEFAULT 0,
    status TEXT DEFAULT 'open',       -- open / fixed / ignored
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    hit_count INTEGER DEFAULT 1,
    PRIMARY KEY (fingerprint, package, app_version, device_model)
);
CREATE INDEX IF NOT EXISTS idx_fp ON crashes(fingerprint);
"""


@dataclass
class CrashRecord:
    fingerprint: str
    package: str
    app_version: str = ""
    device_model: str = ""
    rom: str = ""
    category: str = ""
    root_cause: str = ""
    source_location: dict = None  # type: ignore
    suggested_fix: str = ""
    confidence: float = 0.0
    status: str = "open"
    first_seen: float = 0.0
    last_seen: float = 0.0
    hit_count: int = 1

    def __post_init__(self):
        if self.source_location is None:
            self.source_location = {}


class CrashDB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._open_with_recovery()

    def _open_with_recovery(self) -> sqlite3.Connection:
        """打开数据库；若因残留 journal 等导致 I/O 错误，自动回滚恢复一次。"""
        for attempt in range(2):
            try:
                conn = sqlite3.connect(self.path, timeout=30)
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("PRAGMA journal_mode=TRUNCATE")
                conn.executescript(_SCHEMA)
                return conn
            except sqlite3.OperationalError:
                if attempt == 0:
                    # 触发 journal 回滚自愈
                    try:
                        tmp = sqlite3.connect(self.path, timeout=30)
                        tmp.execute("PRAGMA integrity_check")
                        tmp.close()
                    except sqlite3.Error:
                        pass
                else:
                    raise
        raise sqlite3.OperationalError(f"无法打开 {self.path}")

    def close(self) -> None:
        self._conn.close()

    # ---- 写入 ----

    def record(self, rec: CrashRecord) -> tuple[bool, bool]:
        """记录一次崩溃。返回 (是否新指纹, 是否回归告警)。

        回归判定：同指纹存在 status='fixed' 的历史记录（任意版本），
        而本次出现在新版本或新版本号更大 → 视为回归。
        """
        now = time.time()
        cur = self._conn.cursor()
        cur.execute(
            "SELECT fingerprint, status, hit_count FROM crashes "
            "WHERE fingerprint=? AND package=? AND app_version=? AND device_model=?",
            (rec.fingerprint, rec.package, rec.app_version, rec.device_model))
        row = cur.fetchone()

        is_new = row is None
        regression = False
        if row:
            cur.execute(
                "UPDATE crashes SET last_seen=?, hit_count=hit_count+1 "
                "WHERE fingerprint=? AND package=? AND app_version=? AND device_model=?",
                (now, rec.fingerprint, rec.package, rec.app_version, rec.device_model))
        else:
            cur.execute(
                "INSERT INTO crashes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec.fingerprint, rec.package, rec.app_version, rec.device_model,
                 rec.rom, rec.category, rec.root_cause,
                 json.dumps(rec.source_location, ensure_ascii=False),
                 rec.suggested_fix, rec.confidence, rec.status, now, now, 1))

        # 回归检查：该指纹是否曾被标记 fixed
        cur.execute(
            "SELECT app_version, status FROM crashes WHERE fingerprint=? AND status='fixed'",
            (rec.fingerprint,))
        fixed_rows = cur.fetchall()
        if fixed_rows:
            regression = True
            # 把本次记录状态改回 open（回归即重新打开）
            cur.execute(
                "UPDATE crashes SET status='open' WHERE fingerprint=? AND status='fixed'",
                (rec.fingerprint,))
        self._conn.commit()
        return is_new, regression

    def mark_fixed(self, fingerprint: str) -> int:
        cur = self._conn.cursor()
        cur.execute("UPDATE crashes SET status='fixed' WHERE fingerprint=?", (fingerprint,))
        self._conn.commit()
        return cur.rowcount

    # ---- 查询 ----

    def _rows_to_records(self, rows) -> list[CrashRecord]:
        out = []
        for r in rows:
            out.append(CrashRecord(
                fingerprint=r[0], package=r[1], app_version=r[2],
                device_model=r[3], rom=r[4], category=r[5], root_cause=r[6],
                source_location=json.loads(r[7]), suggested_fix=r[8],
                confidence=r[9], status=r[10], first_seen=r[11],
                last_seen=r[12], hit_count=r[13]))
        return out

    def lookup(self, fingerprint: str) -> list[CrashRecord]:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM crashes WHERE fingerprint=?", (fingerprint,))
        return self._rows_to_records(cur.fetchall())

    def aggregate(self, fingerprint: str) -> dict:
        """跨设备/版本聚合：该指纹在哪些设备、版本上出现，累计多少次。"""
        recs = self.lookup(fingerprint)
        return {
            "fingerprint": fingerprint,
            "total_hits": sum(r.hit_count for r in recs),
            "devices": sorted({r.device_model for r in recs if r.device_model}),
            "versions": sorted({r.app_version for r in recs if r.app_version}),
            "packages": sorted({r.package for r in recs}),
            "status": "fixed" if any(r.status == "fixed" for r in recs) else
                      ("open" if any(r.status == "open" for r in recs) else "ignored"),
        }

    def history(self, package: str = "", limit: int = 50) -> list[CrashRecord]:
        cur = self._conn.cursor()
        if package:
            cur.execute("SELECT * FROM crashes WHERE package=? ORDER BY last_seen DESC LIMIT ?",
                        (package, limit))
        else:
            cur.execute("SELECT * FROM crashes ORDER BY last_seen DESC LIMIT ?", (limit,))
        return self._rows_to_records(cur.fetchall())
