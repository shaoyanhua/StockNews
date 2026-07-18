# -*- coding: utf-8 -*-
"""
涨跌预测网页APP
===============
本地HTTP服务, 调用 predict_updown.run_prediction():
  - 实时模式: 抓当前最新数据(腾讯实时+通达信当日分笔), 预测今天
  - 回测模式: 选任一历史交易日复盘
  - 展示: 主力净流入 / 特大单净流入 / 板块涨跌幅 / 买卖观望概率与建议

运行:  python webapp.py   然后浏览器打开  http://127.0.0.1:8688
"""
import json
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from predict_updown import run_prediction

PORT = 8688
STOCKS = [("300308", "中际旭创"), ("002384", "东山精密"), ("688256", "寒武纪"),
          ("600183", "生益科技"), ("002463", "沪电股份"), ("603986", "兆易创新")]
CUTOFFS = ["09:30", "09:35", "09:40", "09:45", "09:50", "09:55",
           "10:00", "10:30", "11:30", "13:30", "14:00", "14:30"]

_lock = threading.Lock()          # 串行执行预测, 避免并发打爆数据源
_cache = {}                        # (code,date,cutoff) -> result, 仅缓存回测

PAGE = """<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>涨跌预测台</title>
<style>
:root { --bg:#F7F6F3; --panel:#FFF; --ink:#26282D; --muted:#8A8175; --line:#E5E0D6;
  --up:#C2402F; --dn:#1E7F5C; --flat:#9A938A; --accent:#4A5878; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.6 "Noto Sans SC","Microsoft YaHei",sans-serif; padding:1.2rem; }
h1 { font-size:1.25rem; margin:0 0 .2rem; }
.sub { color:var(--muted); font-size:.8rem; margin:0 0 1rem; }
.bar { display:flex; gap:.6rem; flex-wrap:wrap; align-items:center;
  background:var(--panel); border:1px solid var(--line); border-radius:6px;
  padding:.7rem 1rem; margin-bottom:1rem; }
.bar label { font-size:.82rem; color:var(--muted); }
select,input[type=date] { font:inherit; padding:.25rem .5rem; border:1px solid var(--line);
  border-radius:4px; background:var(--bg); color:var(--ink); }
button { font:inherit; padding:.35rem 1rem; border:none; border-radius:4px;
  background:var(--accent); color:#fff; cursor:pointer; }
button:hover { opacity:.9; } button:disabled { opacity:.5; cursor:wait; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); gap:1rem; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:8px;
  padding:1rem; }
.card h2 { font-size:1.02rem; margin:0; }
.meta { color:var(--muted); font-size:.75rem; margin:.1rem 0 .6rem; }
.badge { display:inline-block; padding:.15rem .7rem; border-radius:3px; color:#fff;
  font-size:.85rem; font-weight:600; }
.b-buy { background:var(--up); } .b-sell { background:var(--dn); }
.b-hold { background:var(--flat); }
.probs { display:flex; gap:.4rem; margin:.6rem 0; }
.pb { flex:1; text-align:center; border-radius:4px; padding:.35rem 0; font-size:.8rem;
  background:var(--bg); border:1px solid var(--line); }
.pb b { display:block; font-size:1.05rem; }
.pb.buy b { color:var(--up); } .pb.sell b { color:var(--dn); }
.feats { display:grid; grid-template-columns:1fr 1fr; gap:.4rem; margin:.6rem 0; }
.ft { background:var(--bg); border:1px solid var(--line); border-radius:4px;
  padding:.3rem .6rem; font-size:.78rem; color:var(--muted); }
.ft b { display:block; font-size:.95rem; font-variant-numeric:tabular-nums; }
.pos { color:var(--up)!important; } .neg { color:var(--dn)!important; }
.diag { font-size:.72rem; color:var(--muted); border-top:1px dashed var(--line);
  padding-top:.5rem; margin-top:.5rem; }
.actual { font-size:.85rem; margin-top:.4rem; padding:.3rem .6rem; border-radius:4px;
  background:var(--bg); border:1px solid var(--line); }
.warn { font-size:.82rem; margin:.5rem 0; padding:.4rem .7rem; border-radius:4px;
  background:rgba(194,64,47,.08); border:1px solid var(--up); color:var(--up); }
.status { color:var(--muted); font-size:.85rem; }
.err { color:var(--up); font-size:.85rem; }
.note { color:var(--muted); font-size:.72rem; margin-top:1.2rem; }
</style></head><body>
<h1>涨跌预测台 · 六股</h1>
<p class="sub">特征: 主力净流入 / 特大单净流入 / 板块涨跌 / 自身涨跌 ·
多通道: 通达信分笔(主) + 腾讯量能校验 + 东财官方对照 + 全同业板块二通道 ·
高置信(≥60%)才给买卖建议, 否则观望 · 概率仅供研究, 非投资建议</p>
<div class="bar">
  <label>模式</label>
  <select id="mode"><option value="live">实时(今天)</option>
    <option value="backtest" selected>回测(历史日)</option></select>
  <input type="date" id="date" value="2026-07-17">
  <span id="cutwrap"><label>时点</label>
  <select id="cutoff">__CUTOFF_OPTS__</select></span>
  <label>股票</label>
  <select id="code">__STOCK_OPTS__<option value="ALL">全部六只</option></select>
  <button id="run" onclick="run()">运行预测</button>
  <span class="status" id="status"></span>
</div>
<div class="grid" id="grid"></div>
<p class="note">首次跑某只股票需重构约40天分笔(1~3分钟), 之后有本地缓存(约10秒)。
实时模式请在交易时段使用; 回测日期需为交易日。资金流为分笔重构口径, 与东财APP绝对值不可直接对照。</p>
<script>
const $ = id => document.getElementById(id);
$('mode').onchange = () => { const live = $('mode').value === 'live';
  $('date').style.display = live ? 'none' : '';
  $('cutwrap').style.display = live ? 'none' : ''; };
function pct(v) { return (v*100).toFixed(0) + '%'; }
function sgn(v, suf) { return (v>0?'+':'') + v.toFixed(2) + (suf||''); }
function cls(v) { return v>0 ? 'pos' : (v<0 ? 'neg' : ''); }
function card(r) {
  const badgeCls = r.advice.startsWith('买') ? 'b-buy' :
                   (r.advice.startsWith('卖') ? 'b-sell' : 'b-hold');
  const f = r.features, m = r.model, p = r.probs3;
  let actual = '';
  if (r.actual) actual = `<div class="actual">实际收盘 <b class="${cls(r.actual.pct)}">`+
    `${sgn(r.actual.pct,'%')}</b> [${r.actual.cls}] ${r.actual.hit3?'✓命中':'✗未中'}</div>`;
  const warn = r.warning ? `<div class="warn">⚠️ ${r.warning}</div>` : '';
  return `<div class="card">
  <h2>${r.name} <span style="color:var(--muted);font-size:.8rem">${r.code}</span>
      <span class="badge ${badgeCls}" style="float:right">${r.advice}</span></h2>
  <div class="meta">${r.date} · ${r.cutoff}时点 · 行业${r.industry} ·
      板块代理: ${r.proxies.join('/')}</div>
  <div class="probs">
    <div class="pb buy"><b>${pct(p.buy)}</b>买(&gt;+${r.thr}%)</div>
    <div class="pb"><b>${pct(p.hold)}</b>观望(±${r.thr}%)</div>
    <div class="pb sell"><b>${pct(p.sell)}</b>卖(&lt;-${r.thr}%)</div>
  </div>
  <div class="feats">
    <div class="ft">主力净流入<b class="${cls(f.main)}">${sgn(f.main,'亿')}</b></div>
    <div class="ft">特大单净流入<b class="${cls(f.super)}">${sgn(f.super,'亿')}</b></div>
    <div class="ft">板块涨跌(代理)<b class="${cls(f.board)}">${sgn(f.board,'%')}</b></div>
    <div class="ft">自身涨跌<b class="${cls(f.self)}">${sgn(f.self,'%')}</b></div>
  </div>
  ${warn}
  <div>预测全天 <b class="${cls(r.pred_pct)}">${sgn(r.pred_pct,'%')}</b>
    <span style="color:var(--muted);font-size:.78rem">(±1σ ${sgn(r.band[0])}~${sgn(r.band[1])}%)</span>
    · 收涨概率 <b>${pct(r.prob_up)}</b></div>
  ${actual}
  <div class="diag">训练${m.n_train}天 acc=${pct(m.acc_insample)} R²=${m.r2} ·
    r: 板块${m.corrs.board} 主力${m.corrs.main} 特大${m.corrs.super} 自身${m.corrs.self}</div>
  <div class="diag">${quality(r.quality)}</div>
  </div>`;
}
function quality(q) {
  if (!q) return '';
  let parts = [`分笔${q.ticks}笔 ${q.coverage}`];
  if (q.vol_ratio !== undefined)
    parts.push(`量校验(vs腾讯) ${(q.vol_ratio*100).toFixed(1)}% ${q.vol_ok?'✓':'⚠️缺漏'}`);
  if (typeof q.em_check === 'object')
    parts.push(`东财对照[${q.em_check.scope}] 主力${sgn(q.em_check.main,'亿')} `+
      `特大${sgn(q.em_check.super,'亿')} 方向${q.em_direction_agree?'一致✓':'分歧⚠️'}`);
  else parts.push(q.em_check);
  if (q.board_peers_avg !== undefined)
    parts.push(`板块二通道(全同业均值) ${sgn(q.board_peers_avg,'%')} `+
      `${q.board_agree?'一致✓':'与Top3分歧⚠️'}`);
  return '可靠性: ' + parts.join(' · ');
}
async function fetchOne(code) {
  const mode = $('mode').value;
  let q = `/api/predict?code=${code}`;
  if (mode === 'backtest') q += `&cutoff=${$('cutoff').value}&date=${$('date').value}`;
  // 实时模式不传cutoff, 服务端自动取当前时刻
  const resp = await fetch(q);
  const d = await resp.json();
  if (d.error) throw new Error(d.error);
  return d;
}
async function run() {
  const sel = $('code').value;
  const codes = sel === 'ALL' ? __ALL_CODES__ : [sel];
  $('run').disabled = true; $('grid').innerHTML = '';
  for (const c of codes) {
    $('status').textContent = `${c} 计算中(首次1~3分钟)...`;
    try {
      const r = await fetchOne(c);
      $('grid').insertAdjacentHTML('beforeend', card(r));
    } catch (e) {
      $('grid').insertAdjacentHTML('beforeend',
        `<div class="card"><h2>${c}</h2><div class="err">${e.message}</div></div>`);
    }
  }
  $('status').textContent = '完成';
  $('run').disabled = false;
}
</script></body></html>"""

PAGE = (PAGE
        .replace("__CUTOFF_OPTS__", "".join(
            f'<option{" selected" if c == "10:30" else ""}>{c}</option>' for c in CUTOFFS))
        .replace("__STOCK_OPTS__", "".join(
            f'<option value="{c}">{n}({c})</option>' for c, n in STOCKS))
        .replace("__ALL_CODES__", json.dumps([c for c, _ in STOCKS])))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[web] {args[0] if args else ''}")

    def _send(self, body, ctype="application/json; charset=utf-8", status=200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send(PAGE, "text/html; charset=utf-8")
            return
        if u.path != "/api/predict":
            self._send('{"error":"not found"}', status=404)
            return
        q = parse_qs(u.query)
        code = (q.get("code") or [""])[0]
        date = (q.get("date") or [None])[0] or None
        cutoff = (q.get("cutoff") or [None])[0]
        if not cutoff:
            if date:
                cutoff = "10:30"
            else:  # 实时: 用当前时刻(收盘后按15:00算), 训练窗同口径
                from datetime import datetime
                now = datetime.now().strftime("%H:%M")
                cutoff = min(now, "15:00")
        if not (code.isdigit() and len(code) == 6):
            self._send(json.dumps({"error": "无效代码"}, ensure_ascii=False))
            return
        key = (code, date, cutoff)
        if date and key in _cache:
            self._send(json.dumps(_cache[key], ensure_ascii=False))
            return
        try:
            with _lock:
                if date and key in _cache:
                    result = _cache[key]
                else:
                    result = run_prediction(code, date, cutoff,
                                            progress=lambda m: print(f"  {m}"))
                    if date:
                        _cache[key] = result
            self._send(json.dumps(result, ensure_ascii=False))
        except ValueError as e:
            self._send(json.dumps({"error": str(e)}, ensure_ascii=False))
        except Exception as e:
            traceback.print_exc()
            self._send(json.dumps({"error": f"内部错误: {type(e).__name__}: {e}"},
                                  ensure_ascii=False))


if __name__ == "__main__":
    print(f"涨跌预测台已启动: http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
