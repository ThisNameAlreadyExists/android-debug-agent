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
from .crashdb import CrashDB, CrashRecord
from .device import DeviceGateway
from .explore import Explorer
from .llm import LLMError, OllamaClient
from .logcapture import LogcatCapture, LogBundle, LogLine, FATAL_RE, ANR_RE
from .replay import render_html
from .reporter import render_markdown, write_report
from .scenario import ScenarioRunner, load_case


def _make_llm(enabled: bool):
    if not enabled:
        return None
    try:
        llm = OllamaClient()
        llm.chat("ping", system="只回复 pong")  # 可用性探测
        return llm
    except LLMError as e:
        print(f"⚠️ LLM 不可用，降级为纯规则模式：{e}", file=sys.stderr)
        return None


def _record_crashes(db: CrashDB, diagnoses, package: str,
                    profile) -> list[str]:
    """诊断结果入指纹库。返回提示信息列表（新指纹/回归告警）。"""
    notes = []
    for d in diagnoses:
        rec = CrashRecord(
            fingerprint=d.fingerprint, package=package,
            app_version=profile.installed_version if hasattr(profile, "installed_version") else "",
            device_model=profile.model if profile else "",
            rom=profile.rom if profile else "",
            category=d.category, root_cause=d.root_cause,
            source_location=d.source_location, suggested_fix=d.suggested_fix,
            confidence=d.confidence)
        is_new, regression = db.record(rec)
        if regression:
            notes.append(f"🚨 回归告警：指纹 {d.fingerprint} 曾标记修复，现重现")
        elif is_new:
            notes.append(f"🆕 新指纹入库：{d.fingerprint}")
        else:
            notes.append(f"🔁 已知指纹命中：{d.fingerprint}")
    return notes


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
    p.add_argument("--llm", action="store_true", help="规则未命中时启用本地 LLM 诊断")

    p = sub.add_parser("explore", help="LLM 自主探索模式")
    p.add_argument("goal", help="探索目标，如：测试登录流程找 bug")
    p.add_argument("-s", "--serial")
    p.add_argument("-p", "--package", default="")
    p.add_argument("--max-steps", type=int, default=30)

    p = sub.add_parser("crashes", help="崩溃指纹库：历史/统计/标记修复")
    p.add_argument("action", choices=["history", "fixed", "show"])
    p.add_argument("value", nargs="?", default="", help="fixed/show 时传指纹；history 时传包名（可空）")

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
        llm = _make_llm(args.llm)
        cap = LogcatCapture(ch, serial=args.serial, package=args.package)
        runner = ScenarioRunner(ch, workdir=Path(args.workdir), capture=cap)
        trace, bundle = runner.run(load_case(args.case), serial=args.serial,
                                   package=args.package)
        analyst = LogAnalyst(llm=llm)
        case_ctx = load_case(args.case).get("name", "")
        diagnoses = analyst.analyze(bundle, args.package, context=case_ctx) if bundle else []
        prof = None
        if args.serial:
            try:
                prof = DeviceGateway(ch).profile(args.serial)
            except Exception:
                pass
        md = render_markdown(trace, diagnoses, prof, bundle)
        rp = write_report(md, str(Path(args.workdir) / f"report_{trace.case_name}.md"))
        hp = write_report(render_html(trace, diagnoses, bundle),
                          str(Path(args.workdir) / f"replay_{trace.case_name}.html"))

        db = CrashDB(Path(args.workdir) / "crashdb.sqlite")
        notes = _record_crashes(db, diagnoses, args.package, prof)
        db.close()

        print(f"结果: {'通过' if trace.passed else '失败'}  诊断 {len(diagnoses)} 项")
        for d in diagnoses:
            src = "LLM" if d.llm_used else "规则"
            print(f"  [{d.category}|{src}] {d.root_cause} (指纹 {d.fingerprint}, 置信度 {d.confidence:.0%})")
        for n in notes:
            print(" ", n)
        print(f"报告: {rp}")
        print(f"回放: {hp}")
        sys.exit(0 if trace.passed else 2)

    if args.cmd == "explore":
        llm = _make_llm(True)
        if not llm:
            print("探索模式依赖本地 LLM（Ollama），当前不可用", file=sys.stderr)
            sys.exit(1)
        cap = LogcatCapture(ch, serial=args.serial, package=args.package)
        ex = Explorer(ch, llm, serial=args.serial, package=args.package,
                      max_steps=args.max_steps)
        result = ex.explore(args.goal, capture=cap)
        bundle = cap.snapshot()
        analyst = LogAnalyst(llm=llm)
        diagnoses = analyst.analyze(bundle, args.package, context=args.goal)
        db = CrashDB(Path(args.workdir) / "crashdb.sqlite")
        notes = _record_crashes(db, diagnoses, args.package, None)
        db.close()
        print(f"探索结束：{result.stopped_reason}，执行 {result.steps_taken} 步，"
              f"{'检测到崩溃' if result.crashed else '未崩溃'}")
        for a in result.actions:
            print(f"  → {a}")
        for d in diagnoses:
            print(f"  [{d.category}] {d.root_cause} (指纹 {d.fingerprint})")
        for n in notes:
            print(" ", n)
        sys.exit(2 if result.crashed else 0)

    if args.cmd == "crashes":
        db = CrashDB(Path(args.workdir) / "crashdb.sqlite")
        if args.action == "history":
            for r in db.history(package=args.value):
                print(f"{r.fingerprint} [{r.status}] x{r.hit_count} "
                      f"{r.package}@{r.app_version} on {r.device_model} — {r.root_cause}")
        elif args.action == "fixed":
            n = db.mark_fixed(args.value)
            print(f"已标记修复 {n} 条记录")
        elif args.action == "show":
            agg = db.aggregate(args.value)
            print(json.dumps(agg, ensure_ascii=False, indent=2))
        db.close()
        return

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
