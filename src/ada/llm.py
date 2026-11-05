"""LLM 层（设计方案 §3.5 第三级 + §8.8 脱敏前置）。

- Ollama 客户端：urllib 零依赖，默认 http://localhost:11434
- 脱敏 scrub()：手机号/身份证/token/邮箱/IP 正则清洗后才出进程边界
- 任何 LLM 故障向上抛 LLMError，调用方降级（规则层仍可用）
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Optional


class LLMError(RuntimeError):
    pass


_SCRUB_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"1[3-9]\d{9}"), "[手机号]"),
    (re.compile(r"\b\d{17}[\dXx]\b"), "[身份证]"),
    (re.compile(r"(?i)(token|auth|session|key)[=:]\s*[\w\-.]{8,}"), r"\1=[密钥]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[邮箱]"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "[IP]"),
]


def scrub(text: str) -> str:
    """外发前脱敏。规则内件，不含 AI。"""
    for pat, repl in _SCRUB_PATTERNS:
        text = pat.sub(repl, text)
    return text


class OllamaClient:
    """本地 Ollama。默认 qwen2.5:14b（非 thinking 模型，JSON 输出稳定；
    thinking 模型需大 max_tokens 否则 content 为空，不建议用于决策循环）。"""

    def __init__(self, base: Optional[str] = None, model: Optional[str] = None,
                 timeout: float = 120.0, gate=None):
        self.base = (base or os.environ.get("ADA_OLLAMA_BASE")
                     or "http://localhost:11434").rstrip("/")
        self.model = model or os.environ.get("ADA_OLLAMA_MODEL") or "qwen2.5:14b"
        self.timeout = timeout
        self.gate = gate  # PrivacyGate，可选

    def chat(self, prompt: str, system: str = "") -> str:
        """单轮对话。返回 content 文本。失败抛 LLMError。外发前过隐私闸门。"""
        if self.gate is not None:
            prompt = self.gate.outbound(prompt, f"ollama:local:{self.model}")
            system = self.gate.outbound(system, f"ollama:local:{self.model}") if system else system
        messages = []
        messages.append({"role": "system",
                         "content": system or "你是安卓崩溃诊断专家。只输出被要求的格式，不要寒暄。"})
        messages.append({"role": "user", "content": prompt})
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "stream": False,
        }).encode()
        req = urllib.request.Request(
            f"{self.base}/api/chat", data=body,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            raise LLMError(f"Ollama 调用失败（{self.base}）：{e}") from e
        content = (data.get("message") or {}).get("content", "")
        if not content:
            raise LLMError("Ollama 返回空内容")
        return content
