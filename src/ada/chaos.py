"""主动诱发式稳定性测试（设计方案 §8.7）。

在用例任意步骤插入 chaos 动作，模拟恶劣环境，专杀「上线才暴露」的 bug。
全部通过通道层执行，继承风险分级（YELLOW 记审计）。
"""

from __future__ import annotations

import time
from typing import Optional

from .channel import AdbChannel, UnsupportedError


def chaos_low_memory(ch: AdbChannel, serial: Optional[str], package: str) -> str:
    """模拟系统低内存：向目标进程发 trim-memory 信号。"""
    res = ch.shell(f"am send-trim-memory {package} RUNNING_CRITICAL", serial=serial)
    if not res.ok:
        raise UnsupportedError(f"低内存诱发失败: {res.stderr}")
    return f"低内存诱发 RUNNING_CRITICAL → {package}"


def chaos_background_reclaim(ch: AdbChannel, serial: Optional[str], wait: float = 5.0) -> str:
    """切后台等待回收再回前台：杀「状态丢失」类 bug。"""
    ch.shell("input keyevent KEYCODE_HOME", serial=serial)
    time.sleep(wait)
    ch.shell("input keyevent KEYCODE_BACK", serial=serial)  # 部分设备需 recents，back 兜底
    return f"后台驻留 {wait}s 后回前台"


def chaos_rotate(ch: AdbChannel, serial: Optional[str], times: int = 3) -> str:
    """旋转风暴：Activity 重建压力测试。"""
    ch.shell("settings put system accelerometer_rotation 0", serial=serial)
    for _ in range(times):
        ch.shell("settings put system user_rotation 1", serial=serial)
        time.sleep(0.8)
        ch.shell("settings put system user_rotation 0", serial=serial)
        time.sleep(0.8)
    ch.shell("settings put system accelerometer_rotation 1", serial=serial)
    return f"旋转风暴 x{times}"


def chaos_permission_revoke(ch: AdbChannel, serial: Optional[str],
                            package: str, permission: str) -> str:
    """运行中途回收权限：专杀「授权后又拒绝」路径崩溃。"""
    ch.shell(f"pm revoke {package} {permission}", serial=serial)
    return f"运行中回收权限 {permission}"


def chaos_network_off(ch: AdbChannel, serial: Optional[str], off_seconds: float = 5.0) -> str:
    """断网数秒后恢复：弱网/断网路径。"""
    ch.shell("svc wifi disable", serial=serial)
    ch.shell("svc data disable", serial=serial)
    time.sleep(off_seconds)
    ch.shell("svc wifi enable", serial=serial)
    ch.shell("svc data enable", serial=serial)
    return f"断网 {off_seconds}s 后恢复"


_CHAOS = {
    "low_memory": lambda ch, s, a: chaos_low_memory(ch, s, a["package"]),
    "background": lambda ch, s, a: chaos_background_reclaim(ch, s, a.get("wait", 5)),
    "rotate": lambda ch, s, a: chaos_rotate(ch, s, a.get("times", 3)),
    "permission_revoke": lambda ch, s, a: chaos_permission_revoke(
        ch, s, a["package"], a["permission"]),
    "network_off": lambda ch, s, a: chaos_network_off(ch, s, a.get("seconds", 5)),
}


def run_chaos(ch: AdbChannel, serial: Optional[str], spec: dict) -> str:
    """执行一个 chaos 动作。spec 形如 {type: low_memory, package: ...}。"""
    t = spec.get("type")
    fn = _CHAOS.get(t)
    if not fn:
        raise ValueError(f"未知 chaos 类型: {t}（支持 {list(_CHAOS)}）")
    return fn(ch, serial, spec)
