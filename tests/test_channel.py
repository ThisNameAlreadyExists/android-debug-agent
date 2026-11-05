import unittest

from ada.channel import Risk, classify_shell, check_path_allowed, AdbResult


class TestRiskClassify(unittest.TestCase):
    def test_green(self):
        self.assertEqual(classify_shell("getprop ro.product.model"), Risk.GREEN)
        self.assertEqual(classify_shell("logcat -d"), Risk.GREEN)
        self.assertEqual(classify_shell("dumpsys battery"), Risk.GREEN)

    def test_yellow(self):
        self.assertEqual(classify_shell("input tap 100 200"), Risk.YELLOW)
        self.assertEqual(classify_shell("pm grant a.b c.d"), Risk.YELLOW)
        self.assertEqual(classify_shell("am force-stop x.y"), Risk.YELLOW)

    def test_red(self):
        self.assertEqual(classify_shell("pm uninstall x.y"), Risk.RED)
        self.assertEqual(classify_shell("rm -rf /sdcard/x"), Risk.RED)
        self.assertEqual(classify_shell("reboot"), Risk.RED)

    def test_unknown_defaults_yellow(self):
        self.assertEqual(classify_shell("some_vendor_cmd --x"), Risk.YELLOW)


class TestPathProtection(unittest.TestCase):
    def test_protected_roots_rejected(self):
        for p in ["/", "/system", "/data", "/proc"]:
            with self.assertRaises(PermissionError):
                check_path_allowed(p)

    def test_normal_path_ok(self):
        check_path_allowed("/sdcard/Download/a.txt")
        check_path_allowed("/data/local/tmp/x.png")


class TestRedConfirmGate(unittest.TestCase):
    def test_red_rejected_without_confirm(self):
        from ada.channel import AdbChannel
        ch = AdbChannel(adb_path="/bin/echo", confirm=lambda d: False)
        res = ch.shell("reboot", serial="x")
        self.assertFalse(res.ok)
        self.assertIn("未获确认", res.stderr)

    def test_red_allowed_with_confirm(self):
        from ada.channel import AdbChannel
        ch = AdbChannel(adb_path="/bin/echo", confirm=lambda d: True)
        res = ch.adb(["shell", "reboot"], serial="x", risk=Risk.RED)
        self.assertTrue(res.ok)  # echo 只回显不执行

    def test_shell_escape_hatch_default_closed(self):
        from ada.channel import AdbChannel
        ch = AdbChannel(adb_path="/bin/echo", allow_shell=False)
        with self.assertRaises(PermissionError):
            ch.run_shell("id")


if __name__ == "__main__":
    unittest.main()
