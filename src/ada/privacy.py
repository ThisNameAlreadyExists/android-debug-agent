"""隐私脱敏分级管道（设计方案 §8.8）。

L0 全本地：仅本地 Ollama，禁止任何云端外发
L1 脱敏后云端：scrub 清洗后可发云端模型（当前仅有本地模型，行为=L0+强制脱敏）
L2 全量：显式授权，不脱敏

每批外发内容记审计（privacy_audit.jsonl）：级别、去向、原始长度、脱敏后长度、内容哈希。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from enum import IntEnum
from pathlib import Path
from typing import Optional

from .llm import scrub


class PrivacyLevel(IntEnum):
    L0_LOCAL_ONLY = 0   # 仅本地模型
    L1_SCRUBBED = 1     # 脱敏后可发云端（默认）
    L2_FULL = 2         # 全量外发（显式授权）


class PrivacyGate:
    def __init__(self, level: Optional[PrivacyLevel] = None,
                 audit_path: Optional[Path] = None):
        if level is None:
            env = os.environ.get("ADA_PRIVACY", "L1").upper()
            level = {"L0": PrivacyLevel.L0_LOCAL_ONLY,
                     "L1": PrivacyLevel.L1_SCRUBBED,
                     "L2": PrivacyLevel.L2_FULL}.get(env, PrivacyLevel.L1_SCRUBBED)
        self.level = level
        self.audit_path = Path(audit_path) if audit_path else None
        if self.audit_path:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)

    def outbound(self, text: str, destination: str) -> str:
        """外发闸门：按级别脱敏并记审计。destination 如 ollama:local / cloud:xxx。"""
        is_local = "local" in destination or "ollama" in destination
        if self.level == PrivacyLevel.L0_LOCAL_ONLY and not is_local:
            raise PermissionError(f"L0 级禁止非本地外发: {destination}")
        out = text if self.level == PrivacyLevel.L2_FULL else scrub(text)
        self._audit(destination, text, out)
        return out

    def _audit(self, destination: str, raw: str, sent: str) -> None:
        if not self.audit_path:
            return
        entry = {
            "ts": time.time(),
            "level": self.level.name,
            "destination": destination,
            "raw_len": len(raw),
            "sent_len": len(sent),
            "sent_sha1": hashlib.sha1(sent.encode()).hexdigest()[:16],
            "scrubbed": raw != sent,
        }
        with open(self.audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
