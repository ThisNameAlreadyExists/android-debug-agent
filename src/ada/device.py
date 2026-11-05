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
    # 版本细节（缺陷单环境信息常用）
    device: str = ""            # ro.product.device
    build_fingerprint: str = "" # ro.build.fingerprint
    incremental: str = ""       # ro.build.version.incremental
    security_patch: str = ""    # ro.build.version.security_patch
    kernel: str = ""            # uname -r
    baseband: str = ""          # gsm.version.baseband
    locale: str = ""            # persist.sys.locale
    # 温度与硬件状态
    battery_temp_c: float = 0.0    # 电池温度（℃）
    battery_voltage_mv: int = 0
    battery_status: str = ""       # charging/discharging/full...
    ram_total_mb: int = 0
    ram_avail_mb: int = 0
    storage_total_gb: float = 0.0
    storage_free_gb: float = 0.0
    cpu_hardware: str = ""         # /proc/cpuinfo Hardware
    cpu_cores: int = 0
    refresh_rate: float = 0.0      # 屏幕刷新率
    gpu_renderer: str = ""         # GLES renderer

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
        temp = re.search(r"temperature:\s*(\d+)", bat)
        volt = re.search(r"voltage:\s*(\d+)", bat)
        bstatus = re.search(r"status:\s*(\d+)", bat)
        _BSTATUS = {"1": "未知", "2": "充电中", "3": "放电中", "4": "未充电", "5": "已充满"}

        meminfo = self.ch.shell("cat /proc/meminfo", serial=serial).stdout
        mt = re.search(r"MemTotal:\s*(\d+)\s*kB", meminfo)
        ma = re.search(r"MemAvailable:\s*(\d+)\s*kB", meminfo)

        df = self.ch.shell("df /data | tail -1", serial=serial).stdout.split()
        # df 输出: Filesystem 1K-blocks Used Available ... —— 字段位置因 ROM 略有差异，宽松取
        stotal = sfree = 0.0
        nums = [x for x in df if x.isdigit()]
        if len(nums) >= 3:
            stotal = int(nums[0]) / 1048576
            sfree = int(nums[2]) / 1048576

        cpuinfo = self.ch.shell("cat /proc/cpuinfo", serial=serial).stdout
        cpu_hw = re.search(r"Hardware\s*:\s*(.+)", cpuinfo)
        cpu_cores = cpuinfo.count("processor")

        disp = self.ch.shell("dumpsys display | grep -m1 DisplayDeviceInfo",
                             serial=serial).stdout
        rr = (re.search(r"fps=([\d.]+)", disp)
              or re.search(r"refreshRate=([\d.]+)", disp))

        gles = self.ch.shell(
            "dumpsys SurfaceFlinger | grep -m1 GLES", serial=serial).stdout
        gpu = re.sub(r"^.*GLES:\s*", "", gles).strip()

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
            device=self._getprop(serial, "ro.product.device"),
            build_fingerprint=self._getprop(serial, "ro.build.fingerprint"),
            incremental=self._getprop(serial, "ro.build.version.incremental"),
            security_patch=self._getprop(serial, "ro.build.version.security_patch"),
            kernel=self.ch.shell("uname -r", serial=serial).stdout.strip(),
            baseband=self._getprop(serial, "gsm.version.baseband"),
            locale=self._getprop(serial, "persist.sys.locale"),
            battery_temp_c=int(temp.group(1)) / 10 if temp else 0.0,
            battery_voltage_mv=(lambda v: v // 1000 if v > 100000 else v)(
                int(volt.group(1)) if volt else 0),  # 部分固件以 µV 上报
            battery_status=_BSTATUS.get(bstatus.group(1) if bstatus else "", "未知"),
            ram_total_mb=int(mt.group(1)) // 1024 if mt else 0,
            ram_avail_mb=int(ma.group(1)) // 1024 if ma else 0,
            storage_total_gb=round(stotal, 1),
            storage_free_gb=round(sfree, 1),
            cpu_hardware=cpu_hw.group(1).strip() if cpu_hw else "",
            cpu_cores=cpu_cores,
            refresh_rate=float(rr.group(1)) if rr else 0.0,
            gpu_renderer=gpu,
        )
        self._cache[serial] = prof
        return prof
