import unittest
from unittest.mock import MagicMock

from ada.explore import Explorer
from ada.uiautomator import UiNode


def node(text, rid, clickable=True):
    return UiNode(text=text, resource_id=rid, content_desc="",
                  class_name="android.widget.Button", clickable=clickable,
                  bounds=(0, 0, 100, 100))


class TestExplorer(unittest.TestCase):
    def _explorer(self, decisions):
        ch = MagicMock()
        ok = MagicMock(); ok.ok = True; ok.stdout = ""; ok.stderr = ""
        ch.shell.return_value = ok
        llm = MagicMock()
        llm.chat.side_effect = decisions
        ex = Explorer(ch, llm, serial="S", max_steps=10)
        ex.ui.dump = MagicMock(return_value=[node("登录", "com.x:id/btn")])
        ex.ui.tap_node = MagicMock()
        return ex

    def test_done_terminates(self):
        ex = self._explorer([
            '{"action":"tap","index":0,"reason":"点登录"}',
            '{"action":"done","reason":"完成"}',
        ])
        r = ex.explore("测试登录")
        self.assertEqual(r.stopped_reason, "LLM 判断目标达成")
        self.assertEqual(r.steps_taken, 1)

    def test_loop_detection(self):
        ex = self._explorer(['{"action":"tap","index":0,"reason":"x"}'] * 10)
        r = ex.explore("测试")
        self.assertIn("循环", r.stopped_reason)

    def test_forbidden_widget_blocked(self):
        ch = MagicMock()
        ok = MagicMock(); ok.ok = True; ok.stdout = ""
        ch.shell.return_value = ok
        llm = MagicMock()
        llm.chat.side_effect = [
            '{"action":"tap","index":0,"reason":"x"}',
            '{"action":"done"}',
        ]
        ex = Explorer(ch, llm, serial="S")
        ex.ui.dump = MagicMock(return_value=[node("卸载应用", "com.x:id/del")])
        ex.ui.tap_node = MagicMock()
        r = ex.explore("测试")
        ex.ui.tap_node.assert_not_called()
        self.assertEqual(r.steps_taken, 0)

    def test_step_limit(self):
        # 每次 dump 返回不同界面避免循环检测
        ch = MagicMock()
        ok = MagicMock(); ok.ok = True; ok.stdout = ""
        ch.shell.return_value = ok
        llm = MagicMock()
        llm.chat.return_value = '{"action":"back","reason":"x"}'
        ex = Explorer(ch, llm, serial="S", max_steps=3)
        counter = {"i": 0}
        def varied_dump(serial=None):
            counter["i"] += 1
            return [node(f"btn{counter['i']}", f"id{counter['i']}")]
        ex.ui.dump = MagicMock(side_effect=varied_dump)
        r = ex.explore("测试")
        self.assertEqual(r.steps_taken, 3)
        self.assertIn("上限", r.stopped_reason)


if __name__ == "__main__":
    unittest.main()
