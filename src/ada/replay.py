"""HTML 报告 —— 按时间顺序列出每一步（截图 + 动作 + 日志）。

单文件 HTML：内嵌 base64 截图，零外链，可离线分发。
"""

from __future__ import annotations

import base64
import html
import time
from pathlib import Path
from typing import Optional

from .analyst import Diagnosis
from .device import DeviceProfile
from .logcapture import LogBundle
from .scenario import ExecutionTrace


def _img_b64(path: str) -> str:
    try:
        data = Path(path).read_bytes()
        return "data:image/png;base64," + base64.b64encode(data).decode()
    except OSError:
        return ""


def render_html(trace: ExecutionTrace, diagnoses: list[Diagnosis],
                bundle: Optional[LogBundle] = None,
                title: str = "调试报告",
                profile: Optional[DeviceProfile] = None) -> str:
    markers = bundle.markers if bundle else []

    env_html = ""
    if profile:
        p = profile
        rows = [
            ("设备", f"{p.brand} {p.model}（{p.serial}）"),
            ("系统", f"Android {p.android_release} (SDK {p.sdk}) · ROM {p.rom} · 语言 {p.locale or '未知'}"),
            ("构建指纹", p.build_fingerprint or "未知"),
            ("增量版本", p.incremental or "未知"),
            ("安全补丁", p.security_patch or "未知"),
            ("内核", p.kernel or "未知"),
            ("基带", p.baseband or "未知"),
            ("CPU", f"{p.cpu_hardware or '未知'} · {p.cpu_cores} 核"),
            ("GPU", p.gpu_renderer or "未知"),
            ("内存", f"{p.ram_total_mb}MB 总 / {p.ram_avail_mb}MB 可用"),
            ("存储", f"{p.storage_free_gb}GB 可用 / {p.storage_total_gb}GB 总"),
            ("屏幕", f"{p.screen}@{p.density}dpi · {p.refresh_rate}Hz"),
            ("电池", f"{p.battery_level}% · {p.battery_temp_c}℃ · {p.battery_voltage_mv}mV · {p.battery_status or '未知'}"),
            ("root", "是" if p.rooted else "否"),
        ]
        trs = "".join(
            f'<tr><td>{html.escape(k)}</td><td>{html.escape(str(v))}</td></tr>'
            for k, v in rows)
        env_html = f'<h3>环境信息</h3>\n<table class="env">{trs}</table>'

    # 按时间顺序逐步铺排
    step_html = ""
    for i, s in enumerate(trace.steps):
        logs: list[str] = []
        if bundle and i < len(markers):
            t0 = markers[i]["ts"]
            t1 = markers[i + 1]["ts"] if i + 1 < len(markers) else float("inf")
            logs = [f"[{l.level}/{l.tag}] {l.message}" for l in bundle.lines
                    if t0 <= l.ts < t1][:30]
        ts = time.strftime("%H:%M:%S", time.localtime(markers[i]["ts"])) \
            if i < len(markers) else ""
        img = _img_b64(s.screenshot) if s.screenshot else ""
        img_tag = f'<img src="{img}">' if img else '<div class="noshot">无截图</div>'
        log_block = f'<pre class="logs">{html.escape(chr(10).join(logs))}</pre>' \
            if logs else '<p class="nolog">该步时间窗内无日志</p>'
        badge = "ok" if s.ok else "fail"
        step_html += f"""<div class="step">
<div class="stephead"><span class="badge {badge}">{'✓' if s.ok else '✗'}</span>
<b>第 {s.index} 步 · {html.escape(s.action)}</b>
<span class="ts">{ts} · {s.duration_ms}ms · {html.escape(s.activity)}</span></div>
<div class="stepbody"><div class="shot">{img_tag}</div>
<div class="detail"><p>{html.escape(s.detail)}</p>{log_block}</div></div>
</div>"""

    diag_html = ""
    for d in diagnoses:
        loc = d.source_location
        loc_s = f"{loc.get('symbol','')} → {loc.get('file','')}:{loc.get('line','')}" if loc else "未定位"
        diag_html += f"""<div class="diag">
<b>[{html.escape(d.category)}]</b> {html.escape(d.root_cause)}
<span class="conf">置信度 {d.confidence:.0%}{' · LLM' if d.llm_used else ''}</span><br>
定位：<code>{html.escape(loc_s)}</code><br>指纹：<code>{d.fingerprint}</code><br>
建议：{html.escape(d.suggested_fix)}</div>"""

    status = "通过" if trace.passed else f"失败 @ 第{trace.failure_step}步"
    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
body{{font-family:-apple-system,'PingFang SC',sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#222}}
header{{border-bottom:2px solid #333;padding-bottom:8px}}
.badge{{padding:2px 8px;border-radius:8px;color:#fff;font-size:13px}}
.fail{{background:#c0392b}} .ok{{background:#27ae60}}
.step{{border:1px solid #ddd;border-radius:8px;margin:14px 0;overflow:hidden}}
.stephead{{background:#f7f7f7;padding:8px 14px;display:flex;gap:10px;align-items:center}}
.ts{{color:#888;font-size:12px;margin-left:auto}}
.stepbody{{display:flex;gap:14px;padding:12px 14px}}
.shot img{{width:220px;border:1px solid #eee}}
.noshot{{width:180px;padding:40px 0;text-align:center;color:#999;background:#fafafa}}
.detail{{flex:1}}
.detail p{{margin:0 0 8px}}
.logs{{background:#111;color:#0c0;font:12px/1.5 monospace;padding:10px;max-height:220px;overflow:auto;white-space:pre-wrap;border-radius:6px}}
.nolog{{color:#999;font-size:13px}}
.diag{{background:#fdf6e3;border-left:4px solid #e67e22;padding:10px 14px;margin:10px 0}}
.conf{{color:#888;font-size:12px;margin-left:8px}}
.env{{border-collapse:collapse}}
.env td{{border:1px solid #ddd;padding:4px 14px;font-size:14px}}
.env td:first-child{{background:#f7f7f7;color:#555;width:70px}}
</style></head><body>
<header><h2>{html.escape(title)}</h2>
用例 <b>{html.escape(trace.case_name)}</b> · 结果 <span class="badge {'ok' if trace.passed else 'fail'}">{status}</span>
· {len(trace.steps)} 步 · {trace.finished_at - trace.started_at:.1f}s</header>
{env_html}

<h3>诊断结论（{len(diagnoses)}）</h3>
{diag_html or '<p>未检出异常。</p>'}

<h3>执行步骤</h3>
{step_html}
</body></html>"""
