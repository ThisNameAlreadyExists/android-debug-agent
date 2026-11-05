"""ADB 通道适配层 —— 系统中唯一允许接触设备的地方。

职责收口（设计方案 §2）：
- 命令风险分级（GREEN 只读 / YELLOW 可逆 / RED 破坏性）
- 破坏性操作确认门（confirm 回调）
- 裸 shell 逃生口默认关闭（ADA_ALLOW_SHELL=1 才开放）
- 受保护路径拒绝（/ /system /data 等根路径上的 rm/pull 写入）
- 统一超时 + 指数退避重试
- 审计日志（JSONL，记录全部 YELLOW/RED 操作）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Callable, Optional


class Risk(Enum):
    GREEN = "green"    # 只读
    YELLOW = "yellow"  # 状态变更但可逆
    RED = "red"        # 破坏性


class UnsupportedError(RuntimeError):
    """能力探测降级：当前环境不支持该操作时抛出（DeepADB 模式）。"""


@dataclass
class AdbResult:
    ok: bool
    stdout: str
    stderr: str
    code: int
    duration_ms: int
    attempts: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


# ---- 风险分级规则（DroidMind 模式，可外部 YAML 扩展） ----

_GREEN_BIN = {"getprop", "logcat", "screencap", "dumpsys", "ls", "cat",
              "pm list", "settings get", "wm size", "wm density", "id",
              "uiautomator dump", "date", "df", "ps", "pidof"}
_YELLOW_BIN = {"input", "am start", "am force-stop", "pm grant", "pm revoke",
               "pm clear", "pm install", "install", "push", "pull",
               "monkey", "am broadcast", "am startservice", "setprop",
               "settings put", "svc", "am send-trim-memory"}
_RED_BIN = {"pm uninstall", "uninstall", "rm", "reboot", "setenforce",
            "pm disable", "dd", "mkfs", "mount"}

PROTECTED_ROOTS = ("/", "/system", "/vendor", "/data", "/proc", "/sys")


def classify_shell(cmd: str) -> Risk:
    """对单条 shell 命令做风险分级。取最高风险命中项。"""
    cmd = cmd.strip()
    for red in _RED_BIN:
        if cmd == red or cmd.startswith(red + " "):
            return Risk.RED
    for y in _YELLOW_BIN:
        if cmd == y or cmd.startswith(y + " "):
            return Risk.YELLOW
    for g in _GREEN_BIN:
        if cmd == g or cmd.startswith(g + " "):
            return Risk.GREEN
    # 未识别命令按 YELLOW 处理（fail-safe：宁可多记审计）
    return Risk.YELLOW


def check_path_allowed(path: str) -> None:
    """文件操作路径保护：拒绝在受保护根目录上做写/删。"""
    p = path.strip()
    if p in PROTECTED_ROOTS:
        raise PermissionError(f"拒绝操作受保护路径: {path}")


@dataclass
class AuditEntry:
    ts: float
    serial: Optional[str]
    risk: str
    cmd: list[str]
    ok: bool
    duration_ms: int


class AdbChannel:
    """唯一 ADB 通道。所有模块必须经过它。"""

    def __init__(
        self,
        adb_path: Optional[str] = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        audit_log: Optional[Path] = None,
        confirm: Optional[Callable[[str], bool]] = None,
        allow_shell: Optional[bool] = None,
    ):
        self.adb_path = adb_path or self._detect_adb()
        self.timeout = timeout
        self.max_retries = max_retries
        self.audit_log = Path(audit_log) if audit_log else None
        self.confirm = confirm  # RED 级确认回调，返回 True 才执行
        self.allow_shell = (
            allow_shell
            if allow_shell is not None
            else os.environ.get("ADA_ALLOW_SHELL", "0") in ("1", "true")
        )
        if self.audit_log:
            self.audit_log.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _detect_adb() -> str:
        if os.environ.get("ADB_PATH"):
            return os.environ["ADB_PATH"]
        for root in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT")):
            if root:
                cand = Path(root) / "platform-tools" / "adb"
                if cand.exists():
                    return str(cand)
        found = shutil.which("adb")
        if found:
            return found
        raise FileNotFoundError("找不到 adb，请安装 platform-tools 或设置 ADB_PATH/ANDROID_HOME")

    # ---- 基础执行 ----

    def _exec(self, args: list[str], timeout: Optional[float] = None) -> AdbResult:
        """带重试的底层执行。瞬时失败（device offline 等）指数退避。"""
        attempts = 0
        last: Optional[AdbResult] = None
        while attempts < self.max_retries:
            attempts += 1
            t0 = time.monotonic()
            try:
                p = subprocess.run(
                    args, capture_output=True, text=True,
                    timeout=timeout or self.timeout,
                )
                dur = int((time.monotonic() - t0) * 1000)
                transient = "device offline" in p.stderr or "device not found" in p.stderr
                res = AdbResult(p.returncode == 0 and not transient,
                                p.stdout, p.stderr, p.returncode, dur, attempts)
                if res.ok or not transient:
                    return res
                last = res
            except subprocess.TimeoutExpired:
                dur = int((time.monotonic() - t0) * 1000)
                last = AdbResult(False, "", "timeout", -1, dur, attempts)
                return last  # 超时不重试，避免挂死
            time.sleep(0.5 * (2 ** (attempts - 1)))
        return last  # type: ignore

    def _audit(self, serial: Optional[str], risk: Risk, cmd: list[str], res: AdbResult) -> None:
        if risk == Risk.GREEN or not self.audit_log:
            return
        entry = AuditEntry(time.time(), serial, risk.value, cmd, res.ok, res.duration_ms)
        with open(self.audit_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")

    def adb(self, args: list[str], serial: Optional[str] = None,
            risk: Risk = Risk.GREEN, timeout: Optional[float] = None) -> AdbResult:
        """执行 adb 命令（非 shell 部分）。risk 由调用方声明。"""
        if risk == Risk.RED:
            desc = "adb " + " ".join(args)
            if not (self.confirm and self.confirm(desc)):
                return AdbResult(False, "", "RED 级操作未获确认，已拒绝", -2, 0)
        full = [self.adb_path]
        if serial:
            full += ["-s", serial]
        full += args
        res = self._exec(full, timeout)
        self._audit(serial, risk, full, res)
        return res

    def shell(self, cmd: str, serial: Optional[str] = None,
              timeout: Optional[float] = None) -> AdbResult:
        """执行设备 shell 命令。风险自动分级；RED 需确认。"""
        risk = classify_shell(cmd)
        if risk == Risk.RED:
            if not (self.confirm and self.confirm(f"adb shell {cmd}")):
                return AdbResult(False, "", "RED 级操作未获确认，已拒绝", -2, 0)
        return self.adb(["shell", cmd], serial=serial, risk=risk, timeout=timeout)

    def run_shell(self, cmd: str, serial: Optional[str] = None) -> AdbResult:
        """裸 shell 逃生口 —— 默认关闭（adb-mcp 安全模型）。"""
        if not self.allow_shell:
            raise PermissionError(
                "裸 shell 逃生口默认关闭。确需使用请设环境变量 ADA_ALLOW_SHELL=1")
        return self.shell(cmd, serial=serial)

    # ---- 语义化文件操作（带路径保护） ----

    def push(self, local: str, remote: str, serial: Optional[str] = None) -> AdbResult:
        check_path_allowed(remote)
        return self.adb(["push", local, remote], serial=serial, risk=Risk.YELLOW)

    def pull(self, remote: str, local: str, serial: Optional[str] = None) -> AdbResult:
        return self.adb(["pull", remote, local], serial=serial, risk=Risk.YELLOW)

    def delete_file(self, remote: str, serial: Optional[str] = None) -> AdbResult:
        check_path_allowed(remote)
        if not re.match(r"^/(sdcard|data/local/tmp|data/data)/", remote):
            raise PermissionError(f"删除仅允许 /sdcard、/data/local/tmp、/data/data 之下: {remote}")
        return self.shell(f"rm -f {remote}", serial=serial)

    def health_check(self, serial: Optional[str] = None) -> dict:
        """健康检查：adb 可用性 + 设备授权状态。"""
        ver = self._exec([self.adb_path, "version"])
        if not ver.ok:
            return {"ok": False, "reason": "adb 不可用", "detail": ver.stderr}
        devs = self._exec([self.adb_path, "devices"])
        if serial:
            state = "absent"
            for line in devs.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == serial:
                    state = parts[1]
            if state == "absent":
                return {"ok": False, "reason": f"设备 {serial} 未连接"}
            if state == "unauthorized":
                return {"ok": False, "reason": "设备未授权，请在手机上确认 USB 调试 RSA 指纹"}
            if state != "device":
                return {"ok": False, "reason": f"设备状态异常: {state}"}
        return {"ok": True, "adb_version": ver.stdout.splitlines()[0] if ver.stdout else ""}
