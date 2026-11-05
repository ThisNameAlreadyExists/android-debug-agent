"""UI 树解析与语义定位（uiautomator）。

优先 resource-id / text / content-desc 定位，避免像素坐标的分辨率依赖。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

from .channel import AdbChannel, UnsupportedError


@dataclass
class UiNode:
    text: str
    resource_id: str
    content_desc: str
    class_name: str
    clickable: bool
    bounds: tuple[int, int, int, int]  # left, top, right, bottom
    checked: bool = False

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.bounds
        return ((l + r) // 2, (t + b) // 2)


_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def parse_ui_tree(xml_text: str) -> list[UiNode]:
    """解析 uiautomator dump 的 XML 为节点列表。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    nodes: list[UiNode] = []
    for el in root.iter("node"):
        b = el.get("bounds", "")
        m = _BOUNDS_RE.match(b)
        if not m:
            continue
        nodes.append(UiNode(
            text=el.get("text", ""),
            resource_id=el.get("resource-id", ""),
            content_desc=el.get("content-desc", ""),
            class_name=el.get("class", ""),
            clickable=el.get("clickable", "false") == "true",
            checked=el.get("checked", "false") == "true",
            bounds=tuple(int(x) for x in m.groups()),  # type: ignore
        ))
    return nodes


def find_node(nodes: list[UiNode], *, text: Optional[str] = None,
              resource_id: Optional[str] = None,
              content_desc: Optional[str] = None,
              contains: bool = True) -> Optional[UiNode]:
    """按语义条件查找节点。contains=True 时 text/desc 为子串匹配。"""
    for n in nodes:
        if resource_id and n.resource_id != resource_id:
            continue
        if text is not None:
            if contains:
                if text not in n.text:
                    continue
            elif n.text != text:
                continue
        if content_desc is not None:
            if contains:
                if content_desc not in n.content_desc:
                    continue
            elif n.content_desc != content_desc:
                continue
        return n
    return None


class UiInspector:
    """设备侧 UI 操作：dump / 查找 / 语义点击。"""

    def __init__(self, channel: AdbChannel):
        self.ch = channel

    def dump(self, serial: Optional[str] = None, retries: int = 2) -> list[UiNode]:
        """dump UI 树。高负载下 uiautomator 可能返回空根节点，重试。"""
        last_err = ""
        for _ in range(retries + 1):
            res = self.ch.shell("uiautomator dump /data/local/tmp/ada_ui.xml", serial=serial)
            xml_res = self.ch.shell("cat /data/local/tmp/ada_ui.xml", serial=serial)
            if res.ok and xml_res.ok:
                nodes = parse_ui_tree(xml_res.stdout)
                if nodes:
                    return nodes
                last_err = "dump 返回空 UI 树"
            else:
                last_err = res.stderr or xml_res.stderr
            import time as _t
            _t.sleep(1.0)
        raise UnsupportedError(f"uiautomator dump 失败: {last_err}")

    def tap_node(self, node: UiNode, serial: Optional[str] = None) -> None:
        x, y = node.center
        self.ch.shell(f"input tap {x} {y}", serial=serial)

    def tap(self, serial: Optional[str] = None, *, text: Optional[str] = None,
            resource_id: Optional[str] = None,
            content_desc: Optional[str] = None,
            exact: bool = False) -> UiNode:
        """语义定位并点击。返回命中的节点；未命中抛异常。"""
        nodes = self.dump(serial)
        node = find_node(nodes, text=text, resource_id=resource_id,
                         content_desc=content_desc, contains=not exact)
        if node is None:
            raise LookupError(
                f"未找到控件 text={text} id={resource_id} desc={content_desc}")
        self.tap_node(node, serial)
        return node
