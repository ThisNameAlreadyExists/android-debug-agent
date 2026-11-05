"""时间轴回放 HTML 报告（设计方案 §8.3）。

单文件 HTML：内嵌 base64 截图 + 时间轴滑块，拖动即还原每一步的
截图 / 动作 / Activity / 对应日志段。零外链，可离线分发。
"""

from __future__ import annotations

import base64
import html
import json
import time
from pathlib import Path
from typing import Optional

from .analyst import Diagnosis
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
                title: str = "调试回放报告") -> str:
    # 每步帧：截图 + 元信息 + 该步时间窗内的日志摘录
    frames = []
    markers = bundle.markers if bundle else []
    for i, s in enumerate(trace.steps):
        logs: list[str] = []
        if bundle and i < len(markers):
            t0 = markers[i]["ts"]
            t1 = markers[i + 1]["ts"] if i + 1 < len(markers) else float("inf")
            logs = [f"[{l.level}/{l.tag}] {l.message}" for l in bundle.lines
                    if t0 <= l.ts < t1][:30]
        frames.append({
            "i": i, "action": s.action, "detail": s.detail, "ok": s.ok,
            "activity": s.activity, "ms": s.duration_ms,
            "img": _img_b64(s.screenshot) if s.screenshot else "",
            "logs": logs,
        })

    diag_html = ""
    for d in diagnoses:
        loc = d.source_location
        loc_s = f"{loc.get('symbol','')} → {loc.get('file','')}:{loc.get('line','')}" if loc else "未定位"
        diag_html += f"""<div class="diag">
<b>[{html.escape(d.category)}]</b> {html.escape(d.root_cause)}
<span class="conf">置信度 {d.confidence:.0%}{' · LLM' if d.llm_used else ''}</span><br>
定位：<code>{html.escape(loc_s)}</code><br>指纹：<code>{d.fingerprint}</code><br>
建议：{html.escape(d.suggested_fix)}</div>"""

    payload = json.dumps(frames, ensure_ascii=False)
    status = "通过" if trace.passed else f"失败 @ 第{trace.failure_step}步"
    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
body{{font-family:-apple-system,'PingFang SC',sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#222}}
header{{border-bottom:2px solid #333;padding-bottom:8px}}
.badge{{padding:2px 10px;border-radius:10px;color:#fff;font-size:13px}}
.fail{{background:#c0392b}} .ok{{background:#27ae60}}
#stage{{display:flex;gap:16px;margin-top:16px}}
#shot{{width:300px;min-height:400px;background:#f4f4f4;border:1px solid #ddd}}
#shot img{{width:100%}}
#meta{{flex:1}}
#meta div{{margin:4px 0}}
input[type=range]{{width:100%;margin:12px 0}}
#logs{{background:#111;color:#0c0;font:12px/1.5 monospace;padding:10px;height:220px;overflow:auto;white-space:pre-wrap}}
.diag{{background:#fdf6e3;border-left:4px solid #e67e22;padding:10px 14px;margin:10px 0}}
.conf{{color:#888;font-size:12px;margin-left:8px}}
button{{margin:0 4px;padding:4px 12px}}
</style></head><body>
<header><h2>{html.escape(title)}</h2>
用例 <b>{html.escape(trace.case_name)}</b> · 结果 <span class="badge {'ok' if trace.passed else 'fail'}">{status}</span>
· {len(trace.steps)} 步 · {trace.finished_at - trace.started_at:.1f}s</header>

<h3>诊断结论（{len(diagnoses)}）</h3>
{diag_html or '<p>未检出异常。</p>'}

<h3>时间轴回放</h3>
<input type="range" id="slider" min="0" max="{max(len(frames)-1,0)}" value="0">
<div><button onclick="play()">▶ 播放</button><button onclick="pause()">⏸ 暂停</button></div>
<div id="stage">
<div id="shot"></div>
<div id="meta"><div id="info"></div><div id="logs"></div></div>
</div>
<script>
const F = {payload};
let cur = 0, timer = null;
function show(i) {{
  cur = i; document.getElementById('slider').value = i;
  const f = F[i]; if (!f) return;
  document.getElementById('shot').innerHTML = f.img ? `<img src="${{f.img}}">` : '<p style="padding:20px;color:#999">无截图</p>';
  document.getElementById('info').innerHTML =
    `<b>第 ${{f.i}} 步</b> ${{f.ok?'✅':'❌'}} 动作：<code>${{f.action}}</code><br>` +
    `详情：${{f.detail}}<br>Activity：${{f.activity}} · ${{f.ms}}ms`;
  document.getElementById('logs').textContent = f.logs.join('\\n') || '（该步时间窗内无日志）';
}}
function play() {{ pause(); timer = setInterval(() => {{ if (cur < F.length-1) show(cur+1); else pause(); }}, 1000); }}
function pause() {{ if (timer) clearInterval(timer); timer = null; }}
document.getElementById('slider').oninput = e => show(+e.target.value);
show(0);
</script></body></html>"""
