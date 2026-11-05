import unittest

from ada.minimize import minimize
from ada.verify import sink_case, load_caselib, verify_fix
import tempfile
from pathlib import Path


def mkcase(n):
    return {"name": "c", "steps": [{"launch": "com.x"}] +
            [{"tap": {"text": f"btn{i}"}} for i in range(n)] + [{"wait": 1}]}


class TestMinimize(unittest.TestCase):
    def test_removes_irrelevant_steps(self):
        # 崩溃只与 btn2 有关：含 btn2 才崩溃
        case = mkcase(5)
        def rerun(c):
            has_btn2 = any(s.get("tap", {}).get("text") == "btn2" for s in c["steps"])
            return "fp1" if has_btn2 else None
        res = minimize(case, "fp1", rerun)
        self.assertLess(res.minimized_steps, res.original_steps)
        # 最小序列必须仍含 btn2
        texts = [s.get("tap", {}).get("text") for s in res.steps if "tap" in s]
        self.assertIn("btn2", texts)

    def test_keeps_structural(self):
        case = mkcase(2)
        res = minimize(case, "fp1", lambda c: "fp1")  # 永远崩溃
        self.assertIn("launch", res.steps[0])  # 结构性步骤不被删

    def test_no_crash_keeps_all(self):
        case = mkcase(3)
        res = minimize(case, "fp1", lambda c: None)  # 永不崩溃
        self.assertEqual(res.minimized_steps, res.original_steps)


class TestCaseLib(unittest.TestCase):
    def test_sink_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = sink_case(mkcase(2), "fpX", tmp)
            self.assertTrue(Path(path).exists())
            cases = load_caselib(tmp)
            self.assertEqual(len(cases), 1)
            self.assertEqual(cases[0]["meta"]["fingerprint"], "fpX")
            # 同指纹覆盖而非累积
            sink_case(mkcase(3), "fpX", tmp)
            self.assertEqual(len(load_caselib(tmp)), 1)

    def test_empty_lib(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_caselib(tmp), [])


class TestVerifyFix(unittest.TestCase):
    def test_fixed(self):
        marks = []
        with tempfile.TemporaryDirectory() as tmp:
            res = verify_fix(
                ["fp1"],
                run_case=lambda c: (True, []),  # 通过且无指纹
                case=mkcase(1),
                db_mark_fixed=lambda fp: marks.append(fp) or 1,
                caselib_dir=tmp)
            self.assertTrue(res.fixed)
            self.assertEqual(marks, ["fp1"])
            self.assertTrue(res.case_sunk)

    def test_still_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = verify_fix(["fp1"], lambda c: (False, ["fp1"]), mkcase(1),
                             lambda fp: 1, tmp)
            self.assertFalse(res.fixed)
            self.assertIn("仍重现", res.reason)

    def test_crash_gone_but_case_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = verify_fix(["fp1"], lambda c: (False, []), mkcase(1),
                             lambda fp: 1, tmp)
            self.assertFalse(res.fixed)
            self.assertIn("未通过", res.reason)


if __name__ == "__main__":
    unittest.main()
