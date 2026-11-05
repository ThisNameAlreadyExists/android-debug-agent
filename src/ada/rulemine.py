"""规则库自学习（设计方案 §8.9）。

LLM 诊断经用户确认（ada learn <fingerprint>）后，提炼为规则：
以「异常类名」为匹配键，命中时直接产出诊断，不再调 LLM。
规则存 workdir/rules.json，越用 LLM 调用越少。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional


class RuleMiner:
    def __init__(self, rules_path: str | Path):
        self.path = Path(rules_path)
        self.rules: dict[str, dict] = {}
        if self.path.exists():
            self.rules = json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.rules, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    def learn(self, exception_class: str, category: str,
              root_cause: str, suggested_fix: str) -> None:
        """沉淀一条规则。键 = 异常类短名（如 TimeoutException）。"""
        key = exception_class.split(".")[-1]
        self.rules[key] = {
            "category": category,
            "root_cause": root_cause,
            "suggested_fix": suggested_fix,
            "learned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "hit_count": self.rules.get(key, {}).get("hit_count", 0),
        }
        self._save()

    def match(self, exception_class: str) -> Optional[dict]:
        """命中返回规则并累计命中数。"""
        key = exception_class.split(".")[-1]
        rule = self.rules.get(key)
        if rule:
            rule["hit_count"] = rule.get("hit_count", 0) + 1
            self._save()
        return rule
