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
from .minimize import minimize
from .multidev import run_multi
from .perf import PerfBaseline, collect_perf
from .privacy import PrivacyGate
from .replay import render_html
from .reporter import render_markdown, write_report
from .rulemine import RuleMiner
from .scenario import ScenarioRunner, load_case
from .verify import load_caselib, sink_case, verify_fix


def _make_llm(enabled: bool, workdir: str = "./ada_out"):
    if not enabled:
        return None
    try:
        gate = PrivacyGate(audit_path=Path(workdir) / "privacy_audit.jsonl")
        llm = OllamaClient(gate=gate)
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

    p = sub.add_parser("verify", help="修复验证：装新APK重跑原场景，原指纹消失且全过=修复确认")
    p.add_argument("case")
    p.add_argument("-s", "--serial")
    p.add_argument("-p", "--package", default="")
    p.add_argument("--apk", default="", help="修复后的新 APK（可选，给了就先装）")
    p.add_argument("--fingerprint", required=True, help="要验证消失的崩溃指纹")

    p = sub.add_parser("regress", help="跑回归用例库全部用例")
    p.add_argument("-s", "--serial")
    p.add_argument("-p", "--package", default="")
    p.add_argument("--llm", action="store_true")

    p = sub.add_parser("minimize", help="复现路径压缩：把用例精简到最小可复现序列")
    p.add_argument("case")
    p.add_argument("-s", "--serial")
    p.add_argument("-p", "--package", default="")
    p.add_argument("--fingerprint", required=True)
    p.add_argument("--max-reruns", type=int, default=12)

    p = sub.add_parser("mrun", help="多设备对照：同一用例并行跑多台设备并 diff")
    p.add_argument("case")
    p.add_argument("-s", "--serials", required=True, help="逗号分隔的多个设备号")
    p.add_argument("-p", "--package", default="")

    p = sub.add_parser("learn", help="把确认过的诊断沉淀为规则（规则自学习）")
    p.add_argument("--exception", required=True, help="异常类名，如 java.util.concurrent.TimeoutException")
    p.add_argument("--category", required=True)
    p.add_argument("--cause", required=True)
    p.add_argument("--fix", required=True)

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
        llm = _make_llm(args.llm, args.workdir)
        cap = LogcatCapture(ch, serial=args.serial, package=args.package)
        runner = ScenarioRunner(ch, workdir=Path(args.workdir), capture=cap)
        trace, bundle = runner.run(load_case(args.case), serial=args.serial,
                                   package=args.package)
        analyst = LogAnalyst(llm=llm,
                             rules_path=str(Path(args.workdir) / "rules.json"))
        case_ctx = load_case(args.case).get("name", "")
        diagnoses = analyst.analyze(bundle, args.package, context=case_ctx) if bundle else []
        # 同一指纹在一次运行内只报一次
        seen = set()
        diagnoses = [d for d in diagnoses if not (d.fingerprint in seen or seen.add(d.fingerprint))]
        prof = None
        if args.serial:
            try:
                prof = DeviceGateway(ch).profile(args.serial)
            except Exception:
                pass
        md = render_markdown(trace, diagnoses, prof, bundle)
        rp = write_report(md, str(Path(args.workdir) / f"report_{trace.case_name}.md"))
        hp = write_report(render_html(trace, diagnoses, bundle, profile=prof),
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
        # 性能基线回归（顺带采集，有包名才做）
        if args.package and args.serial and trace.passed:
            pb = PerfBaseline(Path(args.workdir) / "perf.sqlite")
            sample = collect_perf(ch, args.serial, args.package)
            model = prof.model if prof else ""
            for reg in pb.check(args.package, model, sample):
                print(f"  ⚠️ {reg.text}")
            pb.record(args.package, model, sample)
            pb.close()
        print(f"报告: {rp}")
        print(f"回放: {hp}")
        sys.exit(0 if trace.passed else 2)

    if args.cmd == "explore":
        llm = _make_llm(True, args.workdir)
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

    def _run_and_diagnose(case, llm=None):
        """跑一次用例并诊断，返回 (trace, bundle, diagnoses)。"""
        cap = LogcatCapture(ch, serial=args.serial, package=args.package)
        runner = ScenarioRunner(ch, workdir=Path(args.workdir), capture=cap)
        trace, bundle = runner.run(case, serial=args.serial, package=args.package)
        analyst = LogAnalyst(llm=llm)
        diagnoses = analyst.analyze(bundle, args.package,
                                    context=case.get("name", "")) if bundle else []
        return trace, bundle, diagnoses

    if args.cmd == "verify":
        case = load_case(args.case)
        if args.apk:
            prof = DeviceGateway(ch).profile(args.serial)
            AppController(ch).install(args.apk, args.serial,
                                      device_abi=prof.abi, device_sdk=prof.sdk)
            print(f"已安装修复版 APK: {args.apk}")
        trace, bundle, diagnoses = _run_and_diagnose(case)
        fps_now = [d.fingerprint for d in diagnoses]
        db = CrashDB(Path(args.workdir) / "crashdb.sqlite")
        res = verify_fix(
            fingerprints_before=[args.fingerprint],
            run_case=lambda c: (trace.passed, fps_now),  # 已跑完，直接回放结果
            case=case,
            db_mark_fixed=db.mark_fixed,
            caselib_dir=Path(args.workdir) / "regression_cases")
        db.close()
        print(f"修复验证：{'✅ ' + res.reason if res.fixed else '❌ ' + res.reason}")
        if res.case_sunk:
            print(f"用例已沉淀: {res.case_sunk}")
        sys.exit(0 if res.fixed else 2)

    if args.cmd == "regress":
        cases = load_caselib(Path(args.workdir) / "regression_cases")
        if not cases:
            print("回归用例库为空（verify 修复确认后自动沉淀）")
            return
        llm = _make_llm(args.llm, args.workdir)
        db = CrashDB(Path(args.workdir) / "crashdb.sqlite")
        failed = 0
        for case in cases:
            trace, bundle, diagnoses = _run_and_diagnose(case, llm)
            notes = _record_crashes(db, diagnoses, args.package, None)
            fp = case.get("meta", {}).get("fingerprint", "")
            reproduced = fp and any(d.fingerprint == fp for d in diagnoses)
            ok = trace.passed and not reproduced
            failed_mark = "" if ok else "  <-- 回归失败"
            if not ok:
                failed += 1
            print(f"{'✅' if ok else '❌'} {case.get('name','?')} "
                  f"({len(case.get('steps', []))} 步){failed_mark}")
            for n in notes:
                print("   ", n)
        db.close()
        print(f"\n回归完成：{len(cases) - failed}/{len(cases)} 通过")
        sys.exit(0 if failed == 0 else 2)

    if args.cmd == "minimize":
        case = load_case(args.case)
        def _rerun(trial):
            _, bundle, diagnoses = _run_and_diagnose(trial)
            # "any"：同一崩溃在 Java/native 层表面可能不同，按"目标应用任意崩溃"判定
            if args.fingerprint == "any":
                return diagnoses[0].fingerprint if diagnoses else None
            for d in diagnoses:
                if d.fingerprint == args.fingerprint:
                    return d.fingerprint
            return None
        res = minimize(case, args.fingerprint, _rerun,
                       max_reruns=args.max_reruns)
        print(f"压缩完成：{res.original_steps} 步 → {res.minimized_steps} 步 "
              f"（重跑 {res.reruns} 次，{'收敛' if res.converged else '达重跑上限'}）")
        out = Path(args.workdir) / f"minimized_{args.fingerprint}.yaml"
        mini_case = dict(case)
        mini_case["steps"] = res.steps
        mini_case["name"] = f"min_{args.fingerprint}"
        import yaml as _yaml
        with open(out, "w", encoding="utf-8") as f:
            _yaml.safe_dump(mini_case, f, allow_unicode=True, sort_keys=False)
        print(f"最小复现用例: {out}")
        return

    if args.cmd == "mrun":
        case = load_case(args.case)
        case.setdefault("package", args.package)
        serials = [s.strip() for s in args.serials.split(",") if s.strip()]
        if len(serials) < 2:
            print("多设备对照需要至少 2 台设备（-s serial1,serial2）", file=sys.stderr)
            sys.exit(1)
        print(f"并行执行 {len(serials)} 台设备: {serials}")
        devs, report = run_multi(case, serials, args.package, Path(args.workdir))
        for d in devs:
            mark = "✅" if d.passed else "❌"
            err = f" 错误:{d.error}" if d.error else ""
            print(f"  {mark} {d.serial}  指纹:{d.fingerprints or '无'}{err}")
        print("\n对照结论：")
        for n in report.notes:
            print(f"  · {n}")
        if report.device_specific_fingerprints:
            print("  设备特异崩溃：", report.device_specific_fingerprints)
        sys.exit(0 if not report.failed_serials else 2)

    if args.cmd == "learn":
        miner = RuleMiner(Path(args.workdir) / "rules.json")
        miner.learn(args.exception, args.category, args.cause, args.fix)
        print(f"规则已沉淀：{args.exception.split('.')[-1]} → [{args.category}] "
              f"（后续同类崩溃将直接命中，不再调 LLM）")
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
