"""应用操作模块（App Controller）。

全部操作幂等命名；安装前做 ABI / minSdk 兼容预检（aapt 可用时）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from .channel import AdbChannel, Risk, UnsupportedError


@dataclass
class AppState:
    package: str
    installed_version: str = ""
    pid: int = 0
    current_activity: str = ""
    granted_permissions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _aapt_badging(apk_path: str) -> dict:
    """解析 APK 信息。无 aapt 时降级为空 dict（能力探测降级）。"""
    aapt = shutil.which("aapt") or shutil.which("aapt2")
    if not aapt:
        return {}
    try:
        p = subprocess.run([aapt, "dump", "badging", apk_path],
                           capture_output=True, text=True, timeout=30)
    except Exception:
        return {}
    if p.returncode != 0:
        return {}
    info: dict = {}
    m = re.search(r"package: name='([^']+)'.*versionName='([^']*)'", p.stdout)
    if m:
        info["package"], info["version"] = m.group(1), m.group(2)
    m = re.search(r"sdkVersion:'(\d+)'", p.stdout)
    if m:
        info["min_sdk"] = int(m.group(1))
    m = re.search(r"native-code:((?:\s*'[^']+')+)", p.stdout)
    if m:
        info["abis"] = re.findall(r"'([^']+)'", m.group(1))
    info["permissions"] = re.findall(r"uses-permission: name='([^']+)'", p.stdout)
    return info


class AppController:
    def __init__(self, channel: AdbChannel):
        self.ch = channel

    def install(self, apk_path: str, serial: Optional[str] = None,
                grant_permissions: bool = True,
                device_abi: str = "", device_sdk: int = 0) -> AppState:
        """安装 APK。预检 ABI/minSdk；grant_permissions 时自动授予全部声明的运行时权限。"""
        if not Path(apk_path).exists():
            raise FileNotFoundError(apk_path)

        meta = _aapt_badging(apk_path)
        if meta:
            if device_sdk and meta.get("min_sdk") and meta["min_sdk"] > device_sdk:
                raise UnsupportedError(
                    f"APK minSdk={meta['min_sdk']} > 设备 SDK={device_sdk}")
            abis = meta.get("abis") or []
            if device_abi and abis and device_abi not in abis \
                    and not (device_abi == "arm64-v8a" and "armeabi-v7a" in abis):
                raise UnsupportedError(
                    f"ABI 不兼容：APK 支持 {abis}，设备为 {device_abi}")

        args = ["install", "-r"]
        if grant_permissions:
            args.append("-g")
        args.append(apk_path)
        res = self.ch.adb(args, serial=serial, risk=Risk.YELLOW, timeout=120)
        res_ok = res.ok and "Success" in res.stdout
        if not res_ok:
            raise RuntimeError(f"安装失败: {res.stdout} {res.stderr}")
        pkg = meta.get("package", "")
        return self.app_status(pkg, serial) if pkg else AppState(package="")

    def launch(self, package: str, activity: Optional[str] = None,
               serial: Optional[str] = None) -> AppState:
        """启动应用。指定 activity 用 am start，否则 monkey 拉起 launcher。"""
        if activity:
            comp = activity if "/" in activity else f"{package}/{activity}"
            res = self.ch.shell(f"am start -n {comp}", serial=serial)
        else:
            res = self.ch.shell(
                f"monkey -p {package} -c android.intent.category.LAUNCHER 1",
                serial=serial)
        if not res.ok or ("Error" in res.stdout and "error" in res.stdout.lower()):
            raise RuntimeError(f"启动失败: {res.stdout} {res.stderr}")
        return self.app_status(package, serial)

    def force_stop(self, package: str, serial: Optional[str] = None) -> None:
        self.ch.shell(f"am force-stop {package}", serial=serial)

    def clear_data(self, package: str, serial: Optional[str] = None) -> None:
        """清数据重置干净态。属 YELLOW（应用级可逆，不触及系统）。"""
        res = self.ch.shell(f"pm clear {package}", serial=serial)
        if not res.ok or "Success" not in res.stdout:
            raise RuntimeError(f"清数据失败: {res.stdout}")

    def grant(self, package: str, permission: str, serial: Optional[str] = None) -> None:
        self.ch.shell(f"pm grant {package} {permission}", serial=serial)

    def revoke(self, package: str, permission: str, serial: Optional[str] = None) -> None:
        self.ch.shell(f"pm revoke {package} {permission}", serial=serial)

    def uninstall(self, package: str, serial: Optional[str] = None) -> None:
        """卸载 —— RED 级，走通道确认门。"""
        res = self.ch.shell(f"pm uninstall {package}", serial=serial)
        if "未获确认" in res.stderr:
            raise PermissionError(res.stderr)

    def app_status(self, package: str, serial: Optional[str] = None) -> AppState:
        if not package:
            return AppState(package="")
        ver = self.ch.shell(
            f"dumpsys package {package} | grep versionName | head -1",
            serial=serial).stdout
        m = re.search(r"versionName=(\S+)", ver)
        pid_out = self.ch.shell(f"pidof {package}", serial=serial).stdout.strip()
        pid = int(pid_out.split()[0]) if pid_out.split() and pid_out.split()[0].isdigit() else 0
        act = self.ch.shell(
            "dumpsys activity activities | grep -m1 ResumedActivity",
            serial=serial).stdout
        am = re.search(r"\S+/(\S+)", act)
        perms = self.ch.shell(
            f"dumpsys package {package} | grep 'granted=true' | awk '{{print $1}}'",
            serial=serial).stdout.split()
        return AppState(
            package=package,
            installed_version=m.group(1) if m else "",
            pid=pid,
            current_activity=am.group(1) if am else "",
            granted_permissions=[p.strip() for p in perms if p.strip()],
        )
