"""MCP Server 入口（stdio）。可被 WorkBuddy / Claude / Cursor 挂载。

mcp 包未安装时给出明确提示而非崩溃。
工具命名前缀 ada_，全部返回结构化 JSON。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from .analyst import LogAnalyst
from .appctrl import AppController
from .channel import AdbChannel
from .device import DeviceGateway
from .logcapture import LogcatCapture
from .reporter import render_markdown, write_report
from .scenario import ScenarioRunner, load_case


def _confirm_prompt(desc: str) -> bool:
    """MCP 模式下无法交互，RED 级操作一律拒绝（安全默认）。"""
    return False


def build_stack(workdir: str = "./ada_out"):
    ch = AdbChannel(audit_log=Path(workdir) / "audit.jsonl", confirm=_confirm_prompt)
    return {
        "channel": ch,
        "gateway": DeviceGateway(ch),
        "app": AppController(ch),
        "analyst": LogAnalyst(),
        "workdir": workdir,
    }


def main() -> None:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print("未安装 mcp 包。请执行: pip install 'android-debug-agent[mcp]' 或 pip install mcp",
              file=sys.stderr)
        sys.exit(1)

    stack = build_stack(os.environ.get("ADA_WORKDIR", "./ada_out"))
    mcp = FastMCP("ada")
    ch, gw, app, analyst = stack["channel"], stack["gateway"], stack["app"], stack["analyst"]
    workdir = stack["workdir"]

    @mcp.tool()
    def ada_devices() -> list[dict]:
        """列出已连接的安卓设备（含 USB/网络区分与状态）。"""
        return [d.__dict__ for d in gw.list_devices()]

    @mcp.tool()
    def ada_device_profile(serial: str) -> dict:
        """采集设备画像：型号/Android版本/SDK/ABI/ROM/root/能力列表。"""
        return gw.profile(serial).to_dict()

    @mcp.tool()
    def ada_health(serial: str = "") -> dict:
        """健康检查：adb 可用性与设备授权状态。"""
        return ch.health_check(serial or None)

    @mcp.tool()
    def ada_install(apk_path: str, serial: str, grant_permissions: bool = True) -> dict:
        """安装 APK（含 ABI/minSdk 预检与自动权限授予）。"""
        prof = gw.profile(serial)
        return app.install(apk_path, serial, grant_permissions,
                           prof.abi, prof.sdk).to_dict()

    @mcp.tool()
    def ada_launch(package: str, serial: str, activity: str = "") -> dict:
        """启动应用（可指定 Activity），返回运行状态。"""
        return app.launch(package, activity or None, serial).to_dict()

    @mcp.tool()
    def ada_run_scenario(case_path: str, serial: str, package: str = "") -> dict:
        """执行 YAML 测试用例：确定性回放 + 截图 + 日志捕获 + 规则诊断 + 报告。"""
        cap = LogcatCapture(ch, serial=serial, package=package)
        runner = ScenarioRunner(ch, workdir=Path(workdir), capture=cap)
        trace, bundle = runner.run(load_case(case_path), serial=serial, package=package)
        diagnoses = analyst.analyze(bundle, package) if bundle else []
        prof = None
        try:
            prof = gw.profile(serial)
        except Exception:
            pass
        md = render_markdown(trace, diagnoses, prof, bundle)
        report_path = write_report(md, str(Path(workdir) / f"report_{trace.case_name}.md"))
        return {
            "passed": trace.passed,
            "failure_step": trace.failure_step,
            "failure_reason": trace.failure_reason,
            "steps": len(trace.steps),
            "diagnoses": [d.to_dict() for d in diagnoses],
            "report": report_path,
        }

    @mcp.tool()
    def ada_logcat(serial: str, package: str = "", seconds: int = 10,
                   level: str = "W") -> dict:
        """采集 N 秒 logcat，返回结构化行与崩溃/ANR 命中情况。"""
        cap = LogcatCapture(ch, serial=serial, package=package, level=level)
        cap.start()
        import time as _t
        _t.sleep(min(seconds, 120))
        cap.stop()
        b = cap.snapshot()
        return {
            "lines": len(b.lines),
            "crashes": len(b.crash_indices),
            "anrs": len(b.anr_indices),
            "tail": [f"[{l.level}/{l.tag}] {l.message}" for l in b.lines[-100:]],
        }

    @mcp.tool()
    def ada_diagnose_log(log_file: str, package: str = "") -> list[dict]:
        """对已有 logcat 文件做规则层诊断（离线分析）。"""
        from .logcapture import LogLine, LogBundle
        cap = LogcatCapture(ch)
        lines: list[LogLine] = []
        crashes, anrs = [], []
        with open(log_file, encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = cap._parse_line(raw.rstrip("\n"), 0.0)
                if line is None:
                    continue
                from .logcapture import FATAL_RE, ANR_RE
                if FATAL_RE.search(line.message):
                    crashes.append(len(lines))
                if ANR_RE.search(line.message):
                    anrs.append(len(lines))
                lines.append(line)
        bundle = LogBundle(package=package, lines=lines,
                           crash_indices=crashes, anr_indices=anrs)
        return [d.to_dict() for d in analyst.analyze(bundle, package)]

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
