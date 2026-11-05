import unittest
from unittest.mock import MagicMock, patch

from ada.device import DeviceGateway
from ada.appctrl import AppController
from ada.channel import UnsupportedError


def mk_shell(responses: dict):
    """按命令关键字返回假输出。"""
    def shell(cmd, serial=None, timeout=None):
        r = MagicMock()
        r.ok = True
        r.stderr = ""
        for k, v in responses.items():
            if k in cmd:
                r.stdout = v
                return r
        r.stdout = ""
        return r
    return shell


class TestDeviceGateway(unittest.TestCase):
    def test_profile_and_cache(self):
        ch = MagicMock()
        ch.health_check.return_value = {"ok": True}
        ch.shell.side_effect = mk_shell({
            "ro.product.model": "Pixel 8",
            "ro.product.brand": "google",
            "ro.build.version.release": "15",
            "ro.build.version.sdk": "35",
            "ro.product.cpu.abi": "arm64-v8a",
            "wm size": "Physical size: 1080x2400",
            "wm density": "Physical density: 420",
            "dumpsys battery": "level: 88",
            "id": "uid=2000(shell)",
        })
        gw = DeviceGateway(ch)
        p = gw.profile("SERIAL1")
        self.assertEqual(p.model, "Pixel 8")
        self.assertEqual(p.sdk, 35)
        self.assertEqual(p.screen, "1080x2400")
        self.assertFalse(p.rooted)
        self.assertIn("uiautomator", p.capabilities)
        # 会话缓存：第二次不再调 shell
        calls = ch.shell.call_count
        p2 = gw.profile("SERIAL1")
        self.assertEqual(ch.shell.call_count, calls)
        self.assertIs(p, p2)

    def test_unauthorized_raises(self):
        ch = MagicMock()
        ch.health_check.return_value = {"ok": False, "reason": "设备未授权"}
        with self.assertRaises(UnsupportedError):
            DeviceGateway(ch).profile("X")


class TestAppController(unittest.TestCase):
    def _fake_apk(self) -> str:
        import tempfile, os
        fd, path = tempfile.mkstemp(suffix=".apk")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        return path

    def test_abi_incompatible_rejected(self):
        ch = MagicMock()
        app = AppController(ch)
        import ada.appctrl as m
        with patch.object(m, "_aapt_badging",
                          return_value={"package": "a.b", "abis": ["x86_64"], "min_sdk": 24}):
            with self.assertRaises(UnsupportedError):
                app.install(self._fake_apk(), "S", device_abi="arm64-v8a", device_sdk=35)

    def test_minsdk_too_high_rejected(self):
        ch = MagicMock()
        app = AppController(ch)
        import ada.appctrl as m
        with patch.object(m, "_aapt_badging",
                          return_value={"package": "a.b", "min_sdk": 34}):
            with self.assertRaises(UnsupportedError):
                app.install(self._fake_apk(), "S", device_sdk=30)


if __name__ == "__main__":
    unittest.main()
