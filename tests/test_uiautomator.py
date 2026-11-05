import unittest

from ada.uiautomator import parse_ui_tree, find_node

SAMPLE = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
    <node index="0" text="登录" resource-id="com.example.app:id/btn_login"
          class="android.widget.Button" content-desc="登录按钮" clickable="true"
          bounds="[100,1000][980,1150]"/>
    <node index="1" text="" resource-id="com.example.app:id/et_username"
          class="android.widget.EditText" content-desc="用户名输入框" clickable="true"
          bounds="[100,700][980,850]"/>
  </node>
</hierarchy>"""


class TestUiTree(unittest.TestCase):
    def setUp(self):
        self.nodes = parse_ui_tree(SAMPLE)

    def test_parse(self):
        self.assertEqual(len(self.nodes), 3)
        btn = self.nodes[1]
        self.assertEqual(btn.text, "登录")
        self.assertTrue(btn.clickable)
        self.assertEqual(btn.center, (540, 1075))

    def test_find_by_text(self):
        n = find_node(self.nodes, text="登录")
        self.assertIsNotNone(n)
        self.assertEqual(n.resource_id, "com.example.app:id/btn_login")

    def test_find_by_id(self):
        n = find_node(self.nodes, resource_id="com.example.app:id/et_username")
        self.assertEqual(n.content_desc, "用户名输入框")

    def test_find_by_desc_contains(self):
        n = find_node(self.nodes, content_desc="用户名")
        self.assertIsNotNone(n)

    def test_not_found(self):
        self.assertIsNone(find_node(self.nodes, text="不存在"))

    def test_bad_xml(self):
        self.assertEqual(parse_ui_tree("not xml at all"), [])


if __name__ == "__main__":
    unittest.main()
