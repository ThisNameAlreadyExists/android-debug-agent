"""设备接入模块（Device Gateway）。

- 设备发现与枚举（区分 USB / Wi-Fi / 模拟器）
- 能力画像采集（DeviceProfile）
- 会话级缓存：静态属性每会话只查一次（android-mcp-server 经验）
- 多设备 serial 路由由调用方传 serial 实现
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Optional

from .channel import AdbChannel, UnsupportedError


@dataclass
class DeviceInfo:
    serial: str
    state: str
    is_usb: bool
    model: str = ""
    product: str = ""


@dataclass
class DeviceProfile:
    serial: str
    model: str
    brand: str
    android_release: str
    sdk: int
    abi: str
    rom: str            # miui / emui / harmonyos / coloros / aosp / other
    rooted: bool
    screen: str         # e.g. "1080x2400"
    density: int
    battery_level: int
    capabilities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


_PROP_MAP = {
    "model": "ro.product.model",
    "brand": "ro.product.brand",
    "android_release": "ro.build.version.release",
    "sdk": "ro.build.version.sdk",
    "abi": "ro.product.cpu.abi",
    "miui": "ro.miui.ui.version.name",
    "emui": "ro.build.version.emui",
    "harmony": "hw_sc.build.platform.version",
}


class DeviceGateway:
    def __init__(self, channel: AdbChannel):
        self.ch = channel
        self._cache: dict[str, DeviceProfile] = {}  # 会话级缓存

    def list_devices(self) -> list[DeviceInfo]:
        res = self.ch.adb(["devices", "-l"])
        if not res.ok:
            raise RuntimeError(f"adb devices 失败: {res.stderr}")
        out: list[DeviceInfo] = []
        for line in res.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2:
                continue
            serial, state = parts[0], parts[1]
            model = ""
            m = re.search(r"model:(\S+)", line)
            if m:
                model = m.group(1)
            is_usb = not ("." in serial or serial.startswith("emulator"))
            out.append(DeviceInfo(serial, state, is_usb, model))
        return out

    def _getprop(self, serial: str, name: str) -> str:
        res = self.ch.shell(f"getprop {name}", serial=serial)
        return res.stdout.strip() if res.ok else ""

    def _detect_rom(self, serial: str) -> str:
        if self._getprop(serial, _PROP_MAP["miui"]):
            return "miui"
        if self._getprop(serial, _PROP_MAP["emui"]):
            return "emui"
        if self._getprop(serial, _PROP_MAP["harmony"]):
            return "harmonyos"
        return "aosp/other"

    def profile(self, serial: str, refresh: bool = False) -> DeviceProfile:
        """采集设备画像。默认走会话缓存。"""
        if not refresh and serial in self._cache:
            return self._cache[serial]

        health = self.ch.health_check(serial)
        if not health["ok"]:
            raise UnsupportedError(health["reason"])

        sdk_s = self._getprop(serial, _PROP_MAP["sdk"])
        sdk = int(sdk_s) if sdk_s.isdigit() else 0
        rooted = "uid=0" in (self.ch.shell("id", serial=serial).stdout or "")

        size = self.ch.shell("wm size", serial=serial).stdout
        m = re.search(r"(\d+x\d+)", size)
        density_out = self.ch.shell("wm density", serial=serial).stdout
        d = re.search(r"(\d+)", density_out)

        bat = self.ch.shell("dumpsys battery", serial=serial).stdout
        bl = re.search(r"level:\s*(\d+)", bat)

        caps = ["uiautomator", "logcat", "screencap"]
        if sdk >= 21:
            caps.append("run-as")
        if rooted:
            caps += ["root", "tombstone-read"]
        else:
            caps.append("bugreport-fallback")  # /data/anr 不可读时的兜底

        prof = DeviceProfile(
            serial=serial,
            model=self._getprop(serial, _PROP_MAP["model"]),
            brand=self._getprop(serial, _PROP_MAP["brand"]),
            android_release=self._getprop(serial, _PROP_MAP["android_release"]),
            sdk=sdk,
            abi=self._getprop(serial, _PROP_MAP["abi"]),
            rom=self._detect_rom(serial),
            rooted=rooted,
            screen=m.group(1) if m else "unknown",
            density=int(d.group(1)) if d else 0,
            battery_level=int(bl.group(1)) if bl else -1,
            capabilities=caps,
        )
        self._cache[serial] = prof
        return prof
