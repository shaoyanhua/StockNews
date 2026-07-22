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
import os
import threading
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from predict_updown import (run_prediction, day_ticks, today_ticks, amount_at,
                            tdx_connect, tencent_kline, tencent_realtime, S, UA,
                            RemoteDataError)
from continuation_analysis import continuation_run

PORT = int(os.environ.get("STOCKNEWS_PORT", "8688"))
STOCKS = [("300308", "中际旭创"), ("002384", "东山精密"), ("688256", "寒武纪"),
          ("600183", "生益科技"), ("002463", "沪电股份"), ("603986", "兆易创新")]
CUTOFFS = ["09:30", "09:35", "09:40", "09:45", "09:50", "09:55",
           "10:00", "10:30", "11:30", "13:30", "14:00", "14:30", "14:45"]
# 共振四票: 代码, 名称, 产业分支
RES_STOCKS = [("300308", "中际旭创", "光模块/通信设备"),
              ("002463", "沪电股份", "PCB/元器件"),
              ("002384", "东山精密", "PCB/元器件"),
              ("688256", "寒武纪", "半导体")]

_lock = threading.Lock()          # 串行执行预测, 避免并发打爆数据源
_cache = {}                        # (code,date,cutoff) -> result, 仅缓存回测
_rescache = {}                     # (date,cutoff) -> 共振结果, 仅缓存回测
_contcache = {}                    # (code,date,cutoff) -> 延续分析, 仅缓存回测
_contgroupcache = {}               # (date,cutoff) -> 四股延续分析, 仅缓存回测


def _index_pcts(date):
    """上证指数/科创50 涨跌幅 (回测=当日收盘, 实时=当前)"""
    out = {}
    try:
        if date:
            for sym, nm in (("sh000001", "上证指数"), ("sz399006", "创业板指"),
                            ("sh000688", "科创50")):
                u = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                     f"?param={sym},day,,,120,")
                d = S.get(u, headers={"User-Agent": UA}, timeout=15).json()["data"][sym]
                bars = d.get("day") or d.get("qfqday")
                closes = {b[0]: float(b[2]) for b in bars}
                ds = sorted(closes)
                if date in closes and ds.index(date) > 0:
                    prev = closes[ds[ds.index(date) - 1]]
                    out[nm] = round((closes[date] / prev - 1) * 100, 2)
        else:
            r = S.get("https://qt.gtimg.cn/q=sh000001,sz399006,sh000688",
                      headers={"User-Agent": UA}, timeout=10)
            for line in r.content.decode("gbk", "ignore").split(";"):
                if '"' not in line:
                    continue
                v = line.split('"')[1].split("~")
                if len(v) > 32:
                    key = line.split("=")[0]
                    nm = ("上证指数" if "000001" in key else
                          ("创业板指" if "399006" in key else "科创50"))
                    out[nm] = float(v[32] or 0)
    except Exception:
        pass
    return out


def resonance_run(date, cutoff):
    """四票共振: 逐只run_prediction + 盘口扩展信息 + 共振评分"""
    api = tdx_connect()
    stocks, extras = [], []
    try:
        for code, name, branch in RES_STOCKS:
            r = run_prediction(code, date, cutoff)
            if date:
                ticks = day_ticks(api, code, int(date.replace("-", "")))
                kr = tencent_kline(code, n=90, fq="")
                ds = [d for d, _ in kr]
                kc = dict(kr)
                i = ds.index(date)
                prev_close, prev_date = kc[ds[i - 1]], ds[i - 1]
            else:
                ticks = today_ticks(api, code)
                q = tencent_realtime([code])[code]
                prev_close = q["last_close"]
                kr = tencent_kline(code, n=10, fq="")
                ds = [d for d, _ in kr]
                from datetime import datetime as _dt
                today = _dt.now().strftime("%Y-%m-%d")
                prev_date = ds[-2] if ds and ds[-1] == today else ds[-1]
            upto = [t for t in ticks if t["time"] <= cutoff]
            vol = sum(t["vol"] for t in upto)
            vwap = (sum(t["price"] * t["vol"] for t in upto) / vol) if vol else None
            open_p = upto[0]["price"] if upto else None
            px = upto[-1]["price"] if upto else None
            amt = sum(t["price"] * t["vol"] * 100 for t in upto)
            try:
                pticks = day_ticks(api, code, int(prev_date.replace("-", "")))
                amt_prev = amount_at(pticks, cutoff)
            except Exception:
                amt_prev = None
            extras.append({
                "code": code, "name": name, "branch": branch,
                "vwap_above": (px >= vwap) if (px and vwap) else None,
                "gap_up": (open_p > prev_close) if (open_p and prev_close) else None,
                "below_open": (px < open_p) if (px and open_p) else None,
                "vwap": round(vwap, 2) if vwap else None, "px": px,
                "amt": amt, "amt_prev": amt_prev})
            stocks.append(r)
    finally:
        api.disconnect()
    idx = _index_pcts(date)

    conds = []
    def add(name, pts, met):
        conds.append({"name": name, "pts": pts, "met": bool(met)})
    add("四只预测全部上涨", 2, all(s["prob_up"] >= 0.5 for s in stocks))
    branches = {}
    for s, e in zip(stocks, extras):
        branches.setdefault(e["branch"], []).append(s["features"]["board"])
    add("三个产业分支板块全部上涨", 2,
        len(branches) >= 3 and all(sum(v) / len(v) > 0 for v in branches.values()))
    add("至少三只站上分时均价线", 2,
        sum(1 for e in extras if e["vwap_above"]) >= 3)
    add("至少三只主力净流入", 2,
        sum(1 for s in stocks if s["features"]["main"] > 0) >= 3)
    board_avg = sum(s["features"]["board"] for s in stocks) / len(stocks)
    amt_ok = [e for e in extras if e["amt_prev"]]
    heavier = (sum(e["amt"] for e in amt_ok) >=
               1.1 * sum(e["amt_prev"] for e in amt_ok)) if amt_ok else False
    add("AI硬件板块放量上涨(较昨日同时段+10%)", 2, board_avg > 0 and heavier)
    add("大盘/科创50无明显下跌(>-0.5%)", 1,
        bool(idx) and all(v > -0.5 for v in idx.values()))
    add("四只集体高开回落", -3,
        all(e["gap_up"] for e in extras) and all(e["below_open"] for e in extras))
    add("两只以上跌破分时均价线", -3,
        sum(1 for e in extras if e["vwap_above"] is False) >= 2)
    add("板块上涨但资金合计流出", -2,
        board_avg > 0 and sum(s["features"]["main"] for s in stocks) < 0)
    score = sum(c["pts"] for c in conds if c["met"])
    if score >= 8:
        band = "强共振 — 可顺势操作, 但不追直线拉升"
    elif score >= 5:
        band = "偏强 — 小仓位或等回踩"
    elif score >= 2:
        band = "信号冲突 — 以观望、做T为主"
    else:
        band = "预测失效 — 不执行买入"
    branch_up_count = sum(sum(v) / len(v) > 0 for v in branches.values())
    return {"score": score, "band": band, "conds": conds, "indices": idx,
            "branch_up_count": branch_up_count, "branch_count": len(branches),
            "stocks": stocks, "extras": extras,
            "date": stocks[0]["date"], "cutoff": cutoff}


def continuation_group_run(date, cutoff):
    """四股共享同一共振环境，分别计算延续概率。"""
    resonance = resonance_run(date, cutoff)
    items = []
    for stock in resonance["stocks"]:
        result = continuation_run(
            stock["code"], date, cutoff,
            board_pct=stock["features"]["board"],
            indices=resonance["indices"], resonance_score=resonance["score"],
            branch_up_count=resonance["branch_up_count"])
        result["base_prediction"] = {
            "prob_up": stock["prob_up"], "advice": stock["advice"],
            "board_pct": stock["features"]["board"]}
        items.append(result)
    return {"date": resonance["date"], "cutoff": cutoff,
            "resonance_score": resonance["score"],
            "resonance_band": resonance["band"], "items": items}

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
.disc { background:var(--panel); border:1px solid var(--line); border-left:3px solid var(--accent);
  border-radius:6px; padding:.6rem 1rem; margin:0 0 1rem; font-size:.85rem; }
.disc b { color:var(--accent); margin-right:.4rem; }
.disc .step { margin-right:1.1rem; white-space:nowrap; }
.disc .hint { color:var(--muted); font-size:.76rem; }
.status { color:var(--muted); font-size:.85rem; }
.err { color:var(--up); font-size:.85rem; }
.note { color:var(--muted); font-size:.72rem; margin-top:1.2rem; }
.respanel { background:var(--panel); border:1px solid var(--line); border-radius:8px;
  padding:1rem; margin:0 0 1.4rem; }
.reshead { display:flex; align-items:baseline; gap:.7rem; flex-wrap:wrap; margin:0 0 .6rem; }
.resscore { font-size:1.6rem; font-weight:700; font-variant-numeric:tabular-nums; }
.resband { font-size:.9rem; padding:.2rem .7rem; border-radius:4px; color:#fff; }
.rb-strong { background:var(--up); } .rb-mid { background:#B8860B; }
.rb-mix { background:var(--flat); } .rb-weak { background:var(--dn); }
.condlist { display:grid; grid-template-columns:repeat(auto-fill,minmax(250px,1fr));
  gap:.35rem; margin:0 0 .8rem; }
.cond { display:flex; justify-content:space-between; gap:.5rem; font-size:.78rem;
  padding:.3rem .6rem; border-radius:4px; background:var(--bg); border:1px solid var(--line); }
.cond .pts { font-variant-numeric:tabular-nums; font-weight:600; }
.residx { font-size:.78rem; color:var(--muted); margin:0 0 .8rem; }
.resgrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:.6rem; }
.resmini { background:var(--bg); border:1px solid var(--line); border-radius:6px;
  padding:.5rem .7rem; font-size:.78rem; }
.resmini h3 { margin:0 0 .3rem; font-size:.86rem; display:flex;
  justify-content:space-between; align-items:center; }
.resmini .b { display:inline-block; padding:.05rem .5rem; border-radius:3px; color:#fff;
  font-size:.7rem; font-weight:600; }
.resmini .row { display:flex; justify-content:space-between; color:var(--muted);
  font-variant-numeric:tabular-nums; margin-top:.15rem; }
.resmini .flag { font-size:.7rem; margin-top:.3rem; color:var(--muted); }
.contpanel { margin:0 0 1.4rem; }
.conttitle { display:flex; align-items:baseline; gap:.7rem; margin:0 0 .6rem; }
.conttitle h2 { font-size:1.05rem; margin:0; }
.contgrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr)); gap:1rem; }
.contcard { background:var(--panel); border:1px solid var(--line); border-radius:8px;
  padding:1rem; border-top:4px solid var(--flat); }
.contcard.strong { border-top-color:var(--up); }
.contcard.mid { border-top-color:#B8860B; }
.contcard.weak { border-top-color:var(--dn); }
.conthead { display:flex; align-items:center; justify-content:space-between; gap:.6rem; }
.conthead h3 { margin:0; font-size:1rem; }
.scorebox { display:flex; align-items:baseline; gap:.2rem; font-variant-numeric:tabular-nums; }
.scorebox b { font-size:1.55rem; }
.contstate { font-size:.78rem; color:var(--muted); margin:.1rem 0 .6rem; }
.probgrid { display:grid; grid-template-columns:repeat(4,1fr); gap:.35rem; }
.probitem { background:var(--bg); border:1px solid var(--line); border-radius:4px;
  padding:.35rem .25rem; text-align:center; font-size:.68rem; color:var(--muted); }
.probitem b { display:block; color:var(--ink); font-size:.95rem; }
.contmetrics { display:grid; grid-template-columns:repeat(3,1fr); gap:.35rem; margin:.5rem 0; }
.contmetric { background:var(--bg); border:1px solid var(--line); border-radius:4px;
  padding:.35rem .5rem; font-size:.7rem; color:var(--muted); }
.contmetric b { display:block; color:var(--ink); font-size:.86rem; }
.advice { background:rgba(74,88,120,.07); border-left:3px solid var(--accent);
  padding:.45rem .65rem; font-size:.8rem; margin:.5rem 0; }
.reasonlist,.invalidlist { margin:.4rem 0 0; padding-left:1.1rem; font-size:.74rem; color:var(--muted); }
.reasonlist .plus { color:var(--up); } .reasonlist .minus { color:var(--dn); }
.contfoot { border-top:1px dashed var(--line); margin-top:.55rem; padding-top:.45rem;
  font-size:.68rem; color:var(--muted); }
.book { width:100%; border-collapse:collapse; font-size:.7rem; margin-top:.3rem; }
.book th,.book td { border-bottom:1px solid var(--line); padding:.18rem .35rem; text-align:right; }
.book th:first-child,.book td:first-child { text-align:left; }
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
  <button id="runRes" onclick="runResonance()">共振预测</button>
  <button id="runCont" onclick="runContinuation()">上涨延续系统</button>
  <span class="status" id="status"></span>
</div>
<div class="disc"><b>我的纪律</b>
<span class="step">① 9:45前 决定是否卖 — 依据主力/特大单/板块趋势, 避险避跌</span>
<span class="step">② 10:00 决策是否买 — 不追高买, 可做T</span>
<span class="step">③ 14:45前 决定买/卖 — 原则上保持100股过夜</span>
<div class="hint">配合市况开关: 涨市执行买入端 · 跌市执行卖出端 · 震荡观望 ·
遇红色背离警示一律回避</div></div>
<div class="grid" id="grid"></div>
<div id="resPanel"></div>
<div id="contPanel"></div>
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
  return apiJson(q);
}

async function apiJson(url, timeoutMs=240000, retry=true) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const resp = await fetch(url, {signal: controller.signal});
    const raw = await resp.text();
    let data;
    try { data = JSON.parse(raw); }
    catch (_) { throw new Error(`预测服务返回了无效数据（HTTP ${resp.status}）`); }
    if (!resp.ok || data.error) throw new Error(data.error || `预测服务错误（HTTP ${resp.status}）`);
    return data;
  } catch (e) {
    if (e.name === 'AbortError')
      throw new Error('远端行情响应超时，请稍后重试；本次预测已取消');
    if (e instanceof TypeError) {
      // 本机连接偶发重置时先确认服务健康，再重试原请求一次。
      if (retry) {
        await new Promise(resolve => setTimeout(resolve, 800));
        try {
          const health = await fetch('/api/health', {cache: 'no-store'});
          if (health.ok) return apiJson(url, timeoutMs, false);
        } catch (_) { /* 下面给出明确的服务状态提示 */ }
      }
      throw new Error('预测服务连接已中断；请运行 start_webapp.ps1，并查看 webapp.current.stderr.log');
    }
    throw e;
  } finally { clearTimeout(timer); }
}
async function run() {
  const sel = $('code').value;
  const codes = sel === 'ALL' ? __ALL_CODES__ : [sel];
  $('run').disabled = true; $('runRes').disabled = true; $('runCont').disabled = true;
  $('grid').innerHTML = '';
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
  $('run').disabled = false; $('runRes').disabled = false; $('runCont').disabled = false;
}

function resStyle(score) {
  if (score >= 8) return ['rb-strong', '强共振'];
  if (score >= 5) return ['rb-mid', '偏强'];
  if (score >= 2) return ['rb-mix', '信号冲突'];
  return ['rb-weak', '预测失效'];
}
function condRow(c) {
  const icon = c.met ? (c.pts > 0 ? '✓' : '⚠') : '·';
  const vc = c.met ? (c.pts > 0 ? 'pos' : 'neg') : '';
  return `<div class="cond"><span>${icon} ${c.name}</span>`+
    `<span class="pts ${vc}">${c.pts > 0 ? '+' : ''}${c.pts}</span></div>`;
}
function resMini(s, e) {
  const badgeCls = s.advice.startsWith('买') ? 'b-buy' :
                   (s.advice.startsWith('卖') ? 'b-sell' : 'b-hold');
  const f = s.features, p = s.probs3;
  const vwapFlag = e.vwap_above === true ? '✓站上均价线' :
                   (e.vwap_above === false ? '✗跌破均价线' : '均价线数据缺');
  const gapFlag = e.gap_up ? (e.below_open ? ' · 高开已回落' : ' · 高开维持') :
                  (e.gap_up === false ? ' · 低开' : '');
  return `<div class="resmini">
    <h3>${s.name}<span class="b ${badgeCls}">${s.advice}</span></h3>
    <div class="row"><span>${e.branch}</span><span>${s.code}</span></div>
    <div class="row"><span>买${pct(p.buy)}/望${pct(p.hold)}/卖${pct(p.sell)}</span></div>
    <div class="row"><span>板块<b class="${cls(f.board)}">${sgn(f.board,'%')}</b></span>
      <span>主力<b class="${cls(f.main)}">${sgn(f.main,'亿')}</b></span></div>
    <div class="flag">${vwapFlag}${gapFlag}</div>
  </div>`;
}
function resonanceCard(d) {
  const [bandCls, bandLabel] = resStyle(d.score);
  const condHtml = d.conds.map(condRow).join('');
  const idxHtml = Object.entries(d.indices || {}).map(([k, v]) =>
    `${k} ${sgn(v, '%')}`).join(' · ') || '指数数据不可用(不计入大盘一项评分)';
  const miniHtml = d.stocks.map((s, i) => resMini(s, d.extras[i])).join('');
  return `<div class="respanel">
    <div class="reshead">
      <span class="resscore">${d.score}</span>
      <span class="resband ${bandCls}">${bandLabel}</span>
      <span class="status">${d.date} · ${d.cutoff}时点</span>
    </div>
    <div class="condlist">${condHtml}</div>
    <div class="residx">大盘参考: ${idxHtml}</div>
    <div class="resgrid">${miniHtml}</div>
  </div>`;
}
async function runResonance() {
  const mode = $('mode').value;
  const params = new URLSearchParams();
  if (mode === 'backtest') {
    params.set('cutoff', $('cutoff').value);
    params.set('date', $('date').value);
  }
  const q = '/api/resonance' + (params.toString() ? '?' + params.toString() : '');
  $('run').disabled = true; $('runRes').disabled = true; $('runCont').disabled = true;
  $('status').textContent = '共振预测计算中(需拉4只股票, 首次1~3分钟)...';
  $('resPanel').innerHTML = '';
  try {
    const d = await apiJson(q);
    $('resPanel').innerHTML = resonanceCard(d);
  } catch (e) {
    $('resPanel').innerHTML = `<div class="respanel"><div class="err">${e.message}</div></div>`;
  }
  $('status').textContent = '完成';
  $('run').disabled = false; $('runRes').disabled = false; $('runCont').disabled = false;
}

function probText(v) { return v === null || v === undefined ? '样本不足' : pct(v); }
function contClass(score) { return score >= 75 ? 'strong' : (score >= 45 ? 'mid' : 'weak'); }
function orderBookHtml(book) {
  if (!book) return '<div class="meta">历史回测没有五档盘口快照</div>';
  let rows = '';
  for (let i=0; i<5; i++) {
    const b=book.bids[i]||{}, a=book.asks[i]||{};
    rows += `<tr><td>第${i+1}档</td><td class="pos">${b.price||'—'}</td>`+
      `<td>${b.volume||'—'}</td><td class="neg">${a.price||'—'}</td><td>${a.volume||'—'}</td></tr>`;
  }
  return `<table class="book"><tr><th>档位</th><th>买价</th><th>买量(手)</th>`+
    `<th>卖价</th><th>卖量(手)</th></tr>${rows}</table>`;
}
function continuationCard(d) {
  const f = d.features, p = d.probabilities, r = d.risk;
  const reasons = d.reasons.map(x => `<li class="${x.points>0?'plus':'minus'}">`+
    `${x.points>0?'+':''}${x.points} ${x.text}</li>`).join('');
  const invalids = d.invalidations.map(x => `<li>${x}</li>`).join('');
  let actual = '';
  if (d.actual) actual = `<div class="actual">回测真实结果：30分钟 `+
    `${d.actual.ret_30m===null?'—':sgn(d.actual.ret_30m,'%')} · 收盘 `+
    `${d.actual.ret_close===null?'—':sgn(d.actual.ret_close,'%')} · `+
    `最大上冲 ${sgn(d.actual.mfe,'%')} · 最大回撤 ${sgn(d.actual.mae,'%')}</div>`;
  return `<div class="contcard ${contClass(d.score)}">
    <div class="conthead"><h3>${d.name} <span class="meta">${d.code}</span></h3>
      <div class="scorebox"><b>${d.score}</b><span>/100</span></div></div>
    <div class="contstate">${d.state} · ${d.date} ${d.cutoff}时点 · `+
      `概率可靠度：${d.probability_quality}</div>
    <div class="probgrid">
      <div class="probitem"><b>${probText(p.next_30m)}</b>未来30分钟</div>
      <div class="probitem"><b>${probText(p.to_am_close)}</b>至上午收盘</div>
      <div class="probitem"><b>${probText(p.to_close)}</b>至全天收盘</div>
      <div class="probitem"><b>${probText(p.up15_before_down10)}</b>先涨1.5%</div>
    </div>
    <div class="contmetrics">
      <div class="contmetric">最新价<b>${f.price.toFixed(2)}</b></div>
      <div class="contmetric">开盘缺口<b class="${cls(f.gap_pct)}">${sgn(f.gap_pct,'%')}</b></div>
      <div class="contmetric">分时均价<b>${f.vwap.toFixed(2)}</b></div>
      <div class="contmetric">当前/昨收<b class="${cls(f.self_pct)}">${sgn(f.self_pct,'%')}</b></div>
      <div class="contmetric">开盘至当前<b class="${cls(f.path_pct)}">${sgn(f.path_pct,'%')}</b></div>
      <div class="contmetric">距均价线<b class="${cls(f.vwap_dist)}">${sgn(f.vwap_dist,'%')}</b></div>
      <div class="contmetric">距日内高点<b>${sgn(f.retreat_from_high,'%')}</b></div>
      <div class="contmetric">近15分钟<b class="${cls(f.trend_15m)}">${sgn(f.trend_15m,'%')}</b></div>
      <div class="contmetric">同刻量比<b>${f.volume_ratio.toFixed(2)}</b></div>
      <div class="contmetric">均价线10分钟斜率<b class="${cls(f.vwap_slope_10m)}">${sgn(f.vwap_slope_10m,'%')}</b></div>
      <div class="contmetric">主力强度15分钟变化<b class="${cls(f.flow_change_15m)}">${sgn(f.flow_change_15m,'%')}</b></div>
      <div class="contmetric">主力净额15分钟变化<b class="${cls(f.main_change_15m)}">${sgn(f.main_change_15m,'亿')}</b></div>
      <div class="contmetric">上涨量/下跌量<b>${f.up_down_volume_ratio.toFixed(2)}</b></div>
      <div class="contmetric">1分/5分K线<b>${f.bars_1m}/${f.bars_5m}根</b></div>
      <div class="contmetric">1分钟短趋势<b class="${cls(f.one_min_slope)}">${sgn(f.one_min_slope,'%')}</b></div>
      <div class="contmetric">5分钟结构<b>${f.structure>0?'高低点抬高':(f.structure<0?'高低点降低':'震荡')}</b></div>
      <div class="contmetric">换手率<b>${f.turnover===null?'历史无快照':f.turnover.toFixed(2)+'%'}</b></div>
      <div class="contmetric">五档买卖失衡<b>${f.depth_imbalance===null?'历史无快照':sgn(f.depth_imbalance,'%')}</b></div>
      <div class="contmetric">AI硬件板块代理<b class="${cls(d.base_prediction.board_pct)}">${sgn(d.base_prediction.board_pct,'%')}</b></div>
      <div class="contmetric">三分支共振<b>${d.branch_up_count===null?'单股模式':d.branch_up_count+'/3'}</b></div>
    </div>
    <div class="advice"><b>操作倾向：</b>${d.advice}</div>
    <details><summary>评分依据</summary><ul class="reasonlist">${reasons}</ul></details>
    <details><summary>判断失效条件</summary><ul class="invalidlist">${invalids}</ul></details>
    <details><summary>市场环境</summary><div class="meta">${Object.entries(d.market_indices||{}).map(([k,v])=>`${k} ${sgn(v,'%')}`).join(' · ')||'指数数据不可用'}</div></details>
    <details><summary>五档盘口（仅实时快照，辅助判断）</summary>${orderBookHtml(d.order_book)}</details>
    ${actual}
    <div class="contfoot">相似样本${d.samples.similar}/${d.samples.train}天 · `+
      `典型最大上冲 ${sgn(r.median_upside,'%')} · 典型回撤 ${sgn(r.median_pullback,'%')} · `+
      `偏差情景回撤 ${sgn(r.bad_case_pullback,'%')}<br>${d.method}`+
      `${d.validation ? ` · 走步验证${d.validation.n}天 准确率${pct(d.validation.accuracy)} `+
        `Brier=${d.validation.brier} 校准差${pct(d.validation.calibration_gap)}` : ''}</div>
  </div>`;
}
async function fetchContinuation(code) {
  const params = new URLSearchParams({code});
  if ($('mode').value === 'backtest') {
    params.set('date', $('date').value); params.set('cutoff', $('cutoff').value);
  }
  return apiJson('/api/continuation?' + params.toString());
}
async function runContinuation() {
  const selected = $('code').value;
  const codes = selected === 'ALL' ? __RES_CODES__ : [selected];
  $('run').disabled = true; $('runRes').disabled = true; $('runCont').disabled = true;
  $('contPanel').innerHTML = `<div class="contpanel"><div class="conttitle">`+
    `<h2>上涨延续系统</h2><span class="status">计算中…</span></div><div class="contgrid" id="contGrid"></div></div>`;
  if (selected === 'ALL') {
    const params = new URLSearchParams();
    if ($('mode').value === 'backtest') {
      params.set('date', $('date').value); params.set('cutoff', $('cutoff').value);
    }
    try {
      const group = await apiJson('/api/continuation_group?' + params.toString());
      const st = $('contPanel').querySelector('.conttitle .status');
      if (st) st.textContent = `四股共振 ${group.resonance_score}分 · ${group.resonance_band}`;
      for (const d of group.items)
        $('contGrid').insertAdjacentHTML('beforeend', continuationCard(d));
    } catch (e) {
      $('contGrid').insertAdjacentHTML('beforeend',
        `<div class="contcard weak"><div class="err">${e.message}</div></div>`);
    }
  } else for (const code of codes) {
    $('status').textContent = `${code} 延续分析中（首次需构建历史相似样本）...`;
    try {
      const d = await fetchContinuation(code);
      $('contGrid').insertAdjacentHTML('beforeend', continuationCard(d));
    } catch (e) {
      $('contGrid').insertAdjacentHTML('beforeend',
        `<div class="contcard weak"><h3>${code}</h3><div class="err">${e.message}</div></div>`);
    }
  }
  const st = $('contPanel').querySelector('.conttitle .status');
  if (st && selected !== 'ALL') st.textContent = '完成';
  $('status').textContent = '完成';
  $('run').disabled = false; $('runRes').disabled = false; $('runCont').disabled = false;
}
</script></body></html>"""

PAGE = (PAGE
        .replace("__CUTOFF_OPTS__", "".join(
            f'<option{" selected" if c == "10:30" else ""}>{c}</option>' for c in CUTOFFS))
        .replace("__STOCK_OPTS__", "".join(
            f'<option value="{c}">{n}({c})</option>' for c, n in STOCKS))
        .replace("__ALL_CODES__", json.dumps([c for c, _ in STOCKS]))
        .replace("__RES_CODES__", json.dumps([c for c, _, _ in RES_STOCKS])))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[web] {args[0] if args else ''}")

    def _send(self, body, ctype="application/json; charset=utf-8", status=200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # 浏览器关闭/刷新页面不应影响服务进程。
            pass

    def _send_error(self, exc):
        if isinstance(exc, RemoteDataError):
            message, status = f"远端行情暂不可用：{exc}", 502
        else:
            message, status = f"内部错误: {type(exc).__name__}: {exc}", 500
        self._send(json.dumps({"error": message}, ensure_ascii=False), status=status)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/health":
            self._send(json.dumps({
                "ok": True, "pid": os.getpid(),
                "time": datetime.now().isoformat(timespec="seconds")
            }, ensure_ascii=False))
            return
        if u.path == "/":
            self._send(PAGE, "text/html; charset=utf-8")
            return
        if u.path not in ("/api/predict", "/api/resonance", "/api/continuation",
                          "/api/continuation_group"):
            self._send('{"error":"not found"}', status=404)
            return
        q = parse_qs(u.query)
        date = (q.get("date") or [None])[0] or None
        cutoff = (q.get("cutoff") or [None])[0]
        if not cutoff:
            if date:
                cutoff = "10:30"
            else:  # 实时: 用当前时刻(收盘后按15:00算), 训练窗同口径
                now = datetime.now().strftime("%H:%M")
                if "11:30" < now < "13:00":
                    cutoff = "11:30"       # 午休期间使用上午收盘快照
                elif now > "15:00":
                    cutoff = "15:00"       # 收盘后使用全天快照
                else:
                    cutoff = now
        if u.path == "/api/resonance":
            reskey = (date, cutoff)
            if date and reskey in _rescache:
                self._send(json.dumps(_rescache[reskey], ensure_ascii=False))
                return
            try:
                with _lock:
                    if date and reskey in _rescache:
                        result = _rescache[reskey]
                    else:
                        result = resonance_run(date, cutoff)
                        if date:
                            _rescache[reskey] = result
                self._send(json.dumps(result, ensure_ascii=False))
            except ValueError as e:
                self._send(json.dumps({"error": str(e)}, ensure_ascii=False))
            except Exception as e:
                traceback.print_exc()
                self._send_error(e)
            return
        if u.path == "/api/continuation_group":
            key = (date, cutoff)
            if date and key in _contgroupcache:
                self._send(json.dumps(_contgroupcache[key], ensure_ascii=False))
                return
            try:
                with _lock:
                    if date and key in _contgroupcache:
                        result = _contgroupcache[key]
                    else:
                        result = continuation_group_run(date, cutoff)
                        if date:
                            _contgroupcache[key] = result
                self._send(json.dumps(result, ensure_ascii=False))
            except ValueError as e:
                self._send(json.dumps({"error": str(e)}, ensure_ascii=False))
            except Exception as e:
                traceback.print_exc()
                self._send_error(e)
            return
        code = (q.get("code") or [""])[0]
        if not (code.isdigit() and len(code) == 6):
            self._send(json.dumps({"error": "无效代码"}, ensure_ascii=False))
            return
        if u.path == "/api/continuation":
            key = (code, date, cutoff)
            if date and key in _contcache:
                self._send(json.dumps(_contcache[key], ensure_ascii=False))
                return
            try:
                with _lock:
                    if date and key in _contcache:
                        result = _contcache[key]
                    else:
                        # 复用原预测器的板块代理口径，为延续评分补充行业环境。
                        base = run_prediction(code, date, cutoff,
                                              progress=lambda m: print(f"  {m}"))
                        result = continuation_run(
                            code, date, cutoff,
                            board_pct=base["features"]["board"],
                            indices=_index_pcts(date))
                        result["base_prediction"] = {
                            "prob_up": base["prob_up"], "advice": base["advice"],
                            "board_pct": base["features"]["board"]}
                        if date:
                            _contcache[key] = result
                self._send(json.dumps(result, ensure_ascii=False))
            except ValueError as e:
                self._send(json.dumps({"error": str(e)}, ensure_ascii=False))
            except Exception as e:
                traceback.print_exc()
                self._send_error(e)
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
            self._send_error(e)


if __name__ == "__main__":
    print(f"涨跌预测台已启动: http://127.0.0.1:{PORT} (PID {os.getpid()})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
