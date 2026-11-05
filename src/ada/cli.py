"""CLI 入口：ada <command> ...

命令：devices / profile / install / run / diagnose / shell(需 ADA_ALLOW_SHELL=1)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .analyst import LogAnalyst
from .appctrl import AppController
from .channel import AdbChannel
from .device import DeviceGateway
from .logcapture import LogcatCapture, LogBundle, LogLine, FATAL_RE, ANR_RE
from .reporter import render_markdown, write_report
from .scenario import ScenarioRunner, load_case


def _confirm(desc: str) -> bool:
    ans = input(f"⚠️ 即将执行破坏性操作：{desc}\n确认执行？[y/N] ")
    return ans.strip().lower() == "y"


def main() -> None:
    ap = argparse.ArgumentParser(prog="ada", description="Android Debug Agent")
    ap.add_argument("--workdir", default="./ada_out")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices", help="列出设备")

    p = sub.add_parser("profile", help="设备画像")
    p.add_argument("serial")

    p = sub.add_parser("install", help="安装 APK")
    p.add_argument("apk")
    p.add_argument("-s", "--serial")

    p = sub.add_parser("run", help="执行 YAML 用例并诊断出报告")
    p.add_argument("case")
    p.add_argument("-s", "--serial")
    p.add_argument("-p", "--package", default="")

    p = sub.add_parser("diagnose", help="离线诊断 logcat 文件")
    p.add_argument("logfile")
    p.add_argument("-p", "--package", default="")

    p = sub.add_parser("shell", help="裸 shell（需 ADA_ALLOW_SHELL=1）")
    p.add_argument("command")
    p.add_argument("-s", "--serial")

    args = ap.parse_args()
    ch = AdbChannel(audit_log=Path(args.workdir) / "audit.jsonl", confirm=_confirm)

    if args.cmd == "devices":
        for d in DeviceGateway(ch).list_devices():
            kind = "USB" if d.is_usb else "NET/EMU"
            print(f"{d.serial}\t{d.state}\t{kind}\t{d.model}")
        return

    if args.cmd == "profile":
        print(json.dumps(DeviceGateway(ch).profile(args.serial).to_dict(),
                         ensure_ascii=False, indent=2))
        return

    if args.cmd == "install":
        gw = DeviceGateway(ch)
        prof = gw.profile(args.serial) if args.serial else None
        st = AppController(ch).install(
            args.apk, args.serial,
            device_abi=prof.abi if prof else "",
            device_sdk=prof.sdk if prof else 0)
        print(json.dumps(st.to_dict(), ensure_ascii=False, indent=2))
        return

    if args.cmd == "run":
        cap = LogcatCapture(ch, serial=args.serial, package=args.package)
        runner = ScenarioRunner(ch, workdir=Path(args.workdir), capture=cap)
        trace, bundle = runner.run(load_case(args.case), serial=args.serial,
                                   package=args.package)
        diagnoses = LogAnalyst().analyze(bundle, args.package) if bundle else []
        prof = None
        if args.serial:
            try:
                prof = DeviceGateway(ch).profile(args.serial)
            except Exception:
                pass
        md = render_markdown(trace, diagnoses, prof, bundle)
        rp = write_report(md, str(Path(args.workdir) / f"report_{trace.case_name}.md"))
        print(f"结果: {'通过' if trace.passed else '失败'}  诊断 {len(diagnoses)} 项")
        for d in diagnoses:
            print(f"  [{d.category}] {d.root_cause} (指纹 {d.fingerprint}, 置信度 {d.confidence:.0%})")
        print(f"报告: {rp}")
        sys.exit(0 if trace.passed else 2)

    if args.cmd == "diagnose":
        cap = LogcatCapture(ch)
        lines: list[LogLine] = []
        crashes, anrs = [], []
        with open(args.logfile, encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = cap._parse_line(raw.rstrip("\n"), 0.0)
                if line is None:
                    continue
                if FATAL_RE.search(line.message):
                    crashes.append(len(lines))
                if ANR_RE.search(line.message):
                    anrs.append(len(lines))
                lines.append(line)
        bundle = LogBundle(package=args.package, lines=lines,
                           crash_indices=crashes, anr_indices=anrs)
        for d in LogAnalyst().analyze(bundle, args.package):
            print(json.dumps(d.to_dict(), ensure_ascii=False, indent=2))
        return

    if args.cmd == "shell":
        res = ch.run_shell(args.command, serial=args.serial)
        print(res.stdout or res.stderr)
        sys.exit(0 if res.ok else 1)


if __name__ == "__main__":
    main()
