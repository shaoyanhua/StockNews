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
import time
import traceback
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from predict_updown import (run_prediction, day_ticks, today_ticks, amount_at,
                            tdx_connect, tencent_kline, tencent_kline_vol,
                            tencent_realtime, S, UA, RemoteDataError)
from continuation_analysis import continuation_run
from resistance_levels import top_forecast

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

# 换赛道雷达: 代码, 名称, 赛道分支 (AI硬件产业链观察池)
WATCHLIST = [
    ("300308", "中际旭创", "光模块"), ("300502", "新易盛", "光模块"),
    ("300394", "天孚通信", "光模块"),
    ("002463", "沪电股份", "PCB/CCL"), ("002916", "深南电路", "PCB/CCL"),
    ("600183", "生益科技", "PCB/CCL"), ("300476", "胜宏科技", "PCB/CCL"),
    ("301217", "铜冠铜箔", "PCB/CCL"),
    ("601138", "工业富联", "服务器/ODM"), ("002384", "东山精密", "精密制造"),
    ("688256", "寒武纪", "算力芯片"), ("688041", "海光信息", "算力芯片"),
    ("688008", "澜起科技", "存储接口"), ("603986", "兆易创新", "存储"),
    ("001309", "德明利", "存储"), ("301308", "江波龙", "存储"),
    ("002371", "北方华创", "半导体设备"), ("600584", "长电科技", "封测"),
    ("002156", "通富微电", "封测"), ("600206", "有研新材", "半导体材料"),
    ("688146", "中船特气", "半导体材料"), ("600176", "中国巨石", "玻纤材料"),
    ("000636", "风华高科", "被动元件"),
    ("002428", "云南锗业", "稀有金属"), ("000657", "中钨高新", "稀有金属"),
    ("000725", "京东方A", "面板"),
    ("688143", "长盈通", "特种光纤"), ("603259", "药明康德", "医药/CXO"),
    ("300475", "香农芯创", "存储"), ("600487", "亨通光电", "光纤光缆"),
    ("603083", "剑桥科技", "光模块"), ("688655", "迅捷兴", "PCB/CCL"),
    ("688836", "宇树科技", "人形机器人"),
    ("688825", "长鑫科技", "存储"), ("688629", "华丰科技", "连接器"),
    # ETF: 价格3位小数, 东财个股资金流多半缺失(显示"资金数据缺"), 评分按缺失口径计
    ("588200", "科创芯片ETF嘉实", "ETF"), ("560780", "半导体设备ETF广发", "ETF"),
    ("515050", "通信ETF华夏", "ETF"), ("159530", "机器人ETF易方达", "ETF"),
    # ── 红利/防御 风向标(非交易标的) ──
    # 作用: 避险资金的去处。成长杀跌的同时红利吸金 = 资金在撤离风险资产,
    # 是"有重大利空"的经典特征。这几只不进强势榜(不是可切入方向), 只用来判断市场状态。
    #
    # 成分由 research_defensive_basket.py 实测选出(近250日中创业板跌≥3%的18天):
    # 只保留"逆势胜率≥60% 且 大跌日自身仍上涨"的, 每行业取最优。被实测淘汰的:
    #   保险(平安50%/太保50%/人寿61%但大跌日均为负, 与创业板相关系数为正 +0.13~+0.20
    #        —— 险资持有大量权益, 市场跌它自己也跌, 放进来会稀释信号)
    #   红利ETF 510880(胜率仅50% —— 上证红利指数混入钢铁/地产等周期股, 大跌时跟跌)
    #   中国移动(56%, 明显弱于中国电信67%)
    ("601939", "建设银行", "红利/防御"),   # 胜率83% 大跌日+1.19%
    ("601288", "农业银行", "红利/防御"),   # 胜率78% 大跌日+1.52%
    ("601088", "中国神华", "红利/防御"),   # 煤炭 72% +1.10%
    ("600900", "长江电力", "红利/防御"),   # 电力 72% +0.73%
    ("601919", "中远海控", "红利/防御"),   # 运输 67% +0.54%
    ("601728", "中国电信", "红利/防御"),   # 运营商 67% +0.71%
    ("601857", "中国石油", "红利/防御"),   # 石油 61% +1.01%
]

# 风向标标的: 计入赛道统计与避险信号, 但排除出"当前强势"(它们不是可切入方向)
DEFENSIVE = {"601939", "601288", "601088", "600900", "601919", "601728", "601857"}

# 重点监视对象: 持仓/待买核心票, 跌幅达阈值立即告警
FOCUS = {"600183", "600176", "002916", "603986",
         "688256", "600206", "002384", "301217",
         "300394", "300502",
         "515050", "588200", "560780"}   # 通信ETF华夏 / 科创芯片ETF嘉实 / 半导体设备ETF广发

# 告警阈值(当日涨跌幅, 单位%)
ALERT_WARN = -3.0        # 警戒
ALERT_DANGER = -5.0      # 严重
# 冲高回落: 从日内高点回撤幅度(%)。7/31 中船特气从+9%砸到-14%, 当日跌幅告警在前半段
# 完全不响, 但回撤告警在-5%时就会触发, 故单列一类。
ALERT_DRAWDOWN = -5.0

_lock = threading.Lock()          # 串行执行预测, 避免并发打爆数据源
_cache = {}                        # (code,date,cutoff) -> result, 仅缓存回测
_rescache = {}                     # (date,cutoff) -> 共振结果, 仅缓存回测
_contcache = {}                    # (code,date,cutoff) -> 延续分析, 仅缓存回测
_contgroupcache = {}               # (date,cutoff) -> 四股延续分析, 仅缓存回测
_topcache = {}                     # (code,date,cutoff) -> 压力位/顶部预估, 仅缓存回测

_wl_lock = threading.Lock()        # 守护下面两个换赛道雷达的内存态(与_lock分离, 不受重预测阻塞)
_wl_history = {}                   # code -> [(ts, pct), ...] 近20分钟滚动样本, 用于算涨速变化
_wl_trend_cache = {}               # code -> (ts, trend_dict) 日线趋势判断, 10分钟缓存(日线口径变化慢)
_wl_day = {}                       # code -> 当日累计状态(资金峰值/首个VWAP/破线计数), 跨日自动重置
_WL_HIST_WINDOW = 1200             # 滚动样本保留窗口(秒)
_WL_TREND_TTL = 600                # 趋势快照缓存有效期(秒)


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


# ───────────────────────── 换赛道雷达 ─────────────────────────
# 实时监控池强度: 主升浪判定 / 主力资金流向 / 当前涨幅 / 涨幅变化速度 -> 10分制综合评分
# 数据口径: 腾讯实时行情(单请求, 覆盖全池, 价格/涨幅主数据源, 稳定可达) +
#          东财批量资金流(单请求, 可选增强; 东财对非浏览器客户端有TLS指纹风控, 偶发连接被拒,
#          失败时静默降级为"资金数据缺", 不影响价格/趋势/涨速正常展示) +
#          腾讯日K(10分钟缓存判趋势) + 服务端内存滚动样本(自算涨速, 不依赖额外接口)。
#          全部为轻量数据源, 与预测器的分笔重构完全分离, 不占用 _lock, 30秒轮询不会拖慢/
#          被拖慢主预测功能。

def _em_batch_quote(codes):
    """东财批量资金流(单请求覆盖全池, 尽力而为)。返回 {code: {...}}，任一字段缺失记为None。
    仅用作主力资金增强信号；价格/涨幅一律以 tencent_realtime 为准(见 watchlist_run)。"""
    # 与 predict_updown.prefix 保持一致: 6/9/5(沪市ETF)开头为沪市
    secids = ",".join(f"{1 if c.startswith(('6', '9', '5')) else 0}.{c}" for c in codes)
    r = S.get("https://push2.eastmoney.com/api/qt/ulist.np/get", params={
        "fltt": "2",
        "fields": "f12,f13,f14,f2,f3,f8,f9,f23,f20,f62,f184,f66",
        "secids": secids,
    }, headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}, timeout=8)
    out = {}
    for row in (r.json().get("data") or {}).get("diff") or []:
        code = row.get("f12")
        if not code:
            continue

        def g(k):
            v = row.get(k)
            return None if v in (None, "-") else v

        mcap = g("f20")
        main_net = g("f62")
        super_net = g("f66")
        out[code] = {
            "name": g("f14"), "price": g("f2"), "pct": g("f3"), "turnover": g("f8"),
            "pe": g("f9"), "pb": g("f23"),
            "mcap_yi": round(mcap / 1e8, 1) if mcap is not None else None,
            "main_net_yi": round(main_net / 1e8, 2) if main_net is not None else None,
            "super_net_yi": round(super_net / 1e8, 2) if super_net is not None else None,
            "main_pct": g("f184"),
        }
    return out


_EM_TTL = 30            # 东财资金流缓存(秒): 页面3秒轮询, 但资金流只需30秒级刷新, 且避免触发风控
_EM_STALE_MAX = 300     # 东财连续失败时, 旧数据最多沿用的秒数
_wl_em = {"ts": 0.0, "ok_ts": 0.0, "data": {}}


def _em_cached(codes):
    """带TTL的东财资金流。失败时沿用旧数据(超过 _EM_STALE_MAX 则视为缺失)。"""
    now = time.time()
    with _wl_lock:
        if now - _wl_em["ts"] < _EM_TTL:
            return _wl_em["data"] if now - _wl_em["ok_ts"] < _EM_STALE_MAX else {}
        _wl_em["ts"] = now   # 先占位, 防止并发请求同时打东财
    try:
        data = _em_batch_quote(codes)
        with _wl_lock:
            if data:
                _wl_em["data"], _wl_em["ok_ts"] = data, time.time()
    except Exception:
        pass
    with _wl_lock:
        return _wl_em["data"] if time.time() - _wl_em["ok_ts"] < _EM_STALE_MAX else {}


_TREND_NA = {"above_ma20": None, "above_ma60": None, "ma20_rising": None,
             "ret20_pct": None, "ret5_pct": None, "ext_from_high20_pct": None,
             "avg5_vol": None}


def _trend_features(cl):
    """从前复权收盘价序列(升序, 末位=评估日)算日线趋势特征。
    纯函数, 无IO无缓存 —— 回测通过传入历史切片复用同一套口径, 避免与线上实现漂移。"""
    if len(cl) < 25:
        return dict(_TREND_NA)
    price = cl[-1]
    ma20 = sum(cl[-20:]) / 20
    ma20_5ago = sum(cl[-25:-5]) / 20
    ma60 = sum(cl[-60:]) / 60 if len(cl) >= 60 else None
    ret20 = price / cl[-21] - 1 if len(cl) >= 21 else None
    ret5 = price / cl[-6] - 1 if len(cl) >= 6 else None
    ext20 = price / max(cl[-20:]) - 1
    return {
        "above_ma20": bool(price > ma20),
        "above_ma60": bool(ma60 is None or ma20 > ma60),
        "ma20_rising": bool(ma20 > ma20_5ago),
        "ret20_pct": round(ret20 * 100, 1) if ret20 is not None else None,
        "ret5_pct": round(ret5 * 100, 1) if ret5 is not None else None,
        "ext_from_high20_pct": round(ext20 * 100, 1),
    }


def _trend_snapshot(code):
    """取"截至上一交易日"的日线原料, 10分钟缓存(日线变化慢, 无需每次拉取)。

    只缓存原料(D-1及之前的前复权收盘序列 + 前5日均量), 不在这里算趋势特征 ——
    特征由 watchlist_run 用「D-1序列 + 当前实时价」现算, 与盘中回测
    backtest_intraday_cutoffs.py(`_trend_features(cl_series + [p_now])`)完全同口径。
    旧实现直接用腾讯日K原样算特征: 盘前不含今日bar(位置停留在昨收), 盘中是否含今日bar
    又取决于数据源刷新, 且缓存10分钟 —— 回测验证过的准确率并不能代表线上实际跑的口径。
    显式按日期剔除今日bar, 无论数据源当时是否已生成今日bar, 结果都一致。"""
    now = time.time()
    cached = _wl_trend_cache.get(code)
    if cached and now - cached[0] < _WL_TREND_TTL:
        return cached[1]
    today = datetime.now().strftime("%Y-%m-%d")
    result = {"closes": [], "avg5_vol": None}
    try:
        bars = tencent_kline(code, n=70, fq="qfq")  # [(date, close)]
        result["closes"] = [c for d, c in bars if d < today]
        try:
            # 前5个完整交易日均量(手) —— 用于盘中算同时段量比
            vd = tencent_kline_vol(code, n=12)
            v5 = [v for d, v in sorted(vd.items()) if d < today][-5:]
            result["avg5_vol"] = (sum(v5) / len(v5)) if len(v5) >= 3 else None
        except Exception:
            pass
    except Exception:
        pass
    with _wl_lock:
        _wl_trend_cache[code] = (now, result)
    return result


def _classify_wave(trend, pct):
    """结合日线特征 + 今日实时涨幅, 判定主升浪。
    两条独立通路(任一满足即算主升浪):
      ① 建制型上升趋势: 站稳MA20/60, MA20已上行, 20日涨幅>12% — 适合慢牛/已确立的趋势
      ② V型暴力反弹: 5日涨幅>20%且今日仍在涨(>3%), 站稳MA20 — 不要求MA20已转向。
         股灾后大反攻期间MA20仍被崩盘那几天的低值拖累下行，若死守①会系统性错杀正在
         反攻的龙头(20日均线滞后于V型底几乎是必然, 不是①的判定失灵)。
    """
    above20, rising = trend.get("above_ma20"), trend.get("ma20_rising")
    if above20 is None:
        return "数据不足", False
    above60 = trend.get("above_ma60")
    ret20, ret5 = trend.get("ret20_pct"), trend.get("ret5_pct")
    established = bool(above20 and above60 and rising and ret20 is not None and ret20 > 12)
    breakout = bool(above20 and ret5 is not None and ret5 > 20 and pct is not None and pct > 3)
    if established or breakout:
        return "主升浪", True
    if above20 and rising:
        return "上升趋势", False
    if above20:
        return "弱势反弹", False
    return "调整/破位", False


def _momentum(code, pct):
    """近5/15分钟涨速变化: 服务端维护滚动样本自算, 不依赖额外接口。"""
    now = time.time()
    with _wl_lock:
        hist = _wl_history.setdefault(code, [])
        if pct is not None:
            hist.append((now, pct))
        cutoff = now - _WL_HIST_WINDOW
        while hist and hist[0][0] < cutoff:
            hist.pop(0)
        snap = list(hist)

    def delta(seconds, max_age=None):
        if pct is None:
            return None
        target = now - seconds
        cand = [(t, p) for t, p in snap if t <= target]
        if not cand:
            return None
        # 短周期要求样本贴近目标时刻, 否则(轮询停过/刚启动)会拿很旧的样本冒充6秒走势
        if max_age is not None and target - cand[-1][0] > max_age:
            return None
        return round(pct - cand[-1][1], 2)

    d6, d20 = delta(6, 6), delta(20, 10)
    d5, d15 = delta(300), delta(900)
    if pct is None:
        speed_label = "行情数据缺"
    elif d5 is None:
        speed_label = "首次采样"
    elif d5 >= 1.0:
        speed_label = "加速上涨"
    elif d5 >= 0.2:
        speed_label = "温和走强"
    elif d5 > -0.2:
        speed_label = "走平"
    elif d5 > -1.0:
        speed_label = "转弱"
    else:
        speed_label = "加速下跌"
    return {"chg_6s": d6, "chg_20s": d20, "chg_5m": d5, "chg_15m": d15,
            "speed_label": speed_label}


def _score_stock(pct, trend, mom, main_pct):
    """透明打分(条件+分值, 与共振评分同一套设计语言), 满分10分。"""
    conds = []

    def add(name, pts, met):
        conds.append({"name": name, "pts": pts, "met": bool(met)})

    # 趋势档位互斥计分: 直接取 _classify_wave 的最终档位, 避免V型反弹股(MA20尚未转向)
    # 同时命中"主升浪"与"弱势反弹"而重复加分。
    label = trend.get("label")
    add("主升浪(建制上升趋势 或 V型暴力反弹)", 3.5, label == "主升浪")
    add("上升趋势(站稳MA20且MA20上行, 未到主升浪强度)", 2.0, label == "上升趋势")
    add("弱势反弹(站稳MA20但MA20走平/下行)", 0.8, label == "弱势反弹")

    mp = main_pct
    add("主力净占比≥5%(大资金明显流入)", 3.0, mp is not None and mp >= 5)
    add("主力净占比1~5%(资金温和流入)", 1.6, mp is not None and 1 <= mp < 5)
    add("主力净占比<-1%(资金明显流出)", -1.5, mp is not None and mp < -1)

    add("今日涨幅≥7%", 2.0, pct is not None and pct >= 7)
    add("今日涨幅3~7%", 1.3, pct is not None and 3 <= pct < 7)
    add("今日涨幅0~3%", 0.6, pct is not None and 0 <= pct < 3)
    add("今日下跌", -1.2, pct is not None and pct < 0)

    d5 = mom.get("chg_5m")
    add("涨速加速(近5分钟再涨≥1%)", 1.5, d5 is not None and d5 >= 1.0)
    add("涨速走强(近5分钟+0.2~1%)", 0.8, d5 is not None and 0.2 <= d5 < 1.0)
    add("涨速转弱(近5分钟-1~-0.2%)", -1.0, d5 is not None and -1.0 < d5 <= -0.2)
    add("涨速加速下跌(近5分钟≤-1%)", -1.8, d5 is not None and d5 <= -1.0)

    raw = sum(c["pts"] for c in conds if c["met"])
    return max(0.0, min(10.0, round(raw, 1))), conds


def _minutes_traded(hm):
    """当日已交易分钟数(9:30-11:30 + 13:00-15:00 共240分钟)。"""
    t = int(hm[:2]) * 60 + int(hm[3:])
    if t <= 9 * 60 + 30:
        return 0
    if t <= 11 * 60 + 30:
        return t - (9 * 60 + 30)
    if t <= 13 * 60:
        return 120
    if t <= 15 * 60:
        return 120 + (t - 13 * 60)
    return 240


def _volume_state(q, avg5_vol, pct, now_hm, dist_high20):
    """量能四象限(仅标注, 不计入评分)。

    为什么不计分: 三轮实证都判定不稳健 ——
      第一轮 低涨幅子样本 量比IC=-0.005, 前后两段符号相反(-0.080/+0.056)
      第二轮 强势票样本   IC=+0.036, 同号但过小
      第三轮 量×方向2x2   四格次日超额全在+0.04%~+0.17%, 胜率全是53~54%,
                         "放量上涨"(+0.10%)甚至不如"缩量上涨"(+0.17%)
    唯一有稳健数据支撑的是减分项「高位放量滞涨」(超额-0.33%, 胜率46%, 前后同号),
    故单独作为 warn 返回。

    量比口径: 同时段量比 = 当前累计量 /(前5日均量/240 × 已交易分钟数)。
    直接拿盘中累计量比全天均量会系统性偏小, 必须按已交易时长折算。
    成交量由 成交额/均价 反推(手), 以绕开腾讯v[6]在科创板是"股"、主板是"手"的单位坑。
    """
    mins = _minutes_traded(now_hm)
    amt_wan, vwap = q.get("amount_wan"), q.get("vwap")
    if not (avg5_vol and mins >= 5 and amt_wan and vwap):
        return {"ratio": None, "label": "量能数据不足", "tier": "", "warn": ""}
    cur_vol = amt_wan * 100 / vwap          # 万元->元/均价/100 = 手
    expect = avg5_vol / 240 * mins
    ratio = cur_vol / expect if expect else None
    if not ratio:
        return {"ratio": None, "label": "量能数据不足", "tier": "", "warn": ""}

    if ratio >= 1.5:
        tier, tname = "high", "放量"
    elif ratio < 0.8:
        tier, tname = "low", "缩量"
    else:
        tier, tname = "mid", "平量"
    direction = "上涨" if (pct or 0) > 0 else ("下跌" if (pct or 0) < 0 else "平盘")
    warn = ""
    if (tier == "high" and dist_high20 is not None and dist_high20 > -3
            and pct is not None and pct < 2):
        # 回测口径是「距高点>-3% 且 量比≥1.5 且 涨幅<2%」(含下跌), 条件不动;
        # 仅按实际涨跌调整措辞 —— 跌着还叫"滞涨"是自相矛盾的。
        warn = ("高位放量滞涨(疑派发)" if pct >= 0
                else "高位放量下跌(疑派发)")
    return {"ratio": round(ratio, 2), "label": f"{tname}{direction}",
            "tier": tier, "warn": warn}


def _shape_state(code, q, main_pct, now_hm):
    """日内形态判别 -> 【突破冲高成功】/【继续强势】/【危险：回落走弱】

    依据来自两个真实案例的对照(中际旭创2026-08-04 全项成立 vs 寒武纪2026-08-13 全项失败):
      ① 现价在当日振幅的分位 ≥70%      (收在高位=买盘主导)
      ② 站上分时均价线 且 未持续破线    (破线后30分钟收不回=派发确认)
      ③ 分时均价线较当日首次观测抬升
      ④ 主力资金占比未从峰值大幅衰减    (看衰减速度, 不看绝对值)
      ⑤ 距日内高点回撤 <3%

    重要局限(已在返回值中标注):
      - 开盘30分钟内 VWAP≈现价, 各项不稳定 -> 09:45 前返回"观察中"
      - ④依赖服务端当日累计的资金峰值; 若服务是盘中才启动, 峰值不可靠 -> 标注数据不全
    """
    px, hi, lo, vwap = q.get("price"), q.get("high"), q.get("low"), q.get("vwap")
    if not (px and hi and lo and vwap):
        return {"state": "数据不足", "n": None, "conds": [], "partial": True}

    today = datetime.now().strftime("%Y-%m-%d")
    with _wl_lock:
        d = _wl_day.get(code)
        # 缺任一键都视为需要重建(跨日重置, 也防御被部分写入的脏状态)
        if (not d or d.get("date") != today
                or not {"peak_main", "vwap_first", "below_since", "first_seen"} <= d.keys()):
            d = {"date": today, "peak_main": None, "vwap_first": vwap,
                 "below_since": None, "first_seen": now_hm}
            _wl_day[code] = d
        if main_pct is not None:
            d["peak_main"] = main_pct if d["peak_main"] is None else max(d["peak_main"], main_pct)
        if px < vwap:
            if d["below_since"] is None:
                d["below_since"] = now_hm
        else:
            d["below_since"] = None
        snap = dict(d)

    if now_hm < "09:45":
        return {"state": "观察中", "n": None, "conds": [],
                "note": "开盘30分钟内均价线未成形", "partial": True}

    amp = hi - lo
    pos = ((px - lo) / amp * 100) if amp > 0 else 50.0
    dvw = (px / vwap - 1) * 100
    dhi = (px / hi - 1) * 100
    peak = snap["peak_main"]
    decay_ok = (main_pct is not None and peak is not None
                and main_pct > 0 and main_pct >= peak * 0.6)
    # 破线持续时长 —— 按"交易分钟"算而非墙钟分钟: 11:25破线、13:05复查,
    # 墙钟差100分钟但真实交易只过了10分钟, 旧算法会把午休90分钟计入, 误触发≥60分钟的危险否决
    below_min = None
    if snap["below_since"]:
        below_min = max(0, _minutes_traded(now_hm) - _minutes_traded(snap["below_since"]))

    vw_txt = (f"未持续跌破分时均价线({dvw:+.2f}%"
              + (f", 已破线{below_min}分钟)" if below_min else ")"))
    conds = [
        {"name": f"现价处当日振幅{pos:.0f}%分位(≥70%强)", "met": pos >= 70},
        {"name": vw_txt, "met": dvw >= 0 or (below_min is not None and below_min < 30)},
        # 用开盘价作基准而非"服务首次观测到的均价": 开盘时均价≈开盘价, 均价高于开盘价即说明
        # 均价线已抬升。旧实现在服务盘中/盘后才启动时 vwap_first==vwap, 本条恒为False,
        # 导致晚启动时形态判定系统性偏空。开盘价缺失时才退回首次观测值。
        {"name": "分时均价线高于开盘价(均价线已抬升)",
         "met": vwap > (q.get("open") or snap["vwap_first"])},
        {"name": (f"主力占比{main_pct:+.1f}%未从峰值{peak:+.1f}%大幅衰减"
                  if (main_pct is not None and peak is not None) else "主力资金数据缺"),
         "met": decay_ok},
        {"name": f"距日内高点{dhi:+.2f}%(>-3%)", "met": dhi > -3},
    ]
    n = sum(1 for c in conds if c["met"])

    # 有没有发生过"冲高": 日内最高涨幅 ≥4%
    lc = q.get("last_close")
    surge = bool(lc and (hi / lc - 1) * 100 >= 4.0)

    if n >= 4:
        state = "突破冲高成功" if surge else "继续强势"
    elif n <= 2:
        state = "危险：回落走弱"
    else:
        halved = (main_pct is not None and peak is not None and peak > 0
                  and main_pct < peak * 0.5)
        state = "危险：回落走弱" if halved else "多空拉锯"

    # 分时均价线的一票否决权:
    # 它不是普通的1/5票 —— 均价线是当日全部买盘的平均成本, 跌破意味着当日买入者
    # 整体浮亏, 抛压会自我强化。两个对照案例的分水岭正在这里(旭创全天未破 /
    # 寒武纪10:00破后再没收回)。所以按破线时长做封顶降级, 而不是让其余4项把它稀释掉。
    if dvw < 0:
        if below_min is not None and below_min >= 60:
            state = "危险：回落走弱"
        elif below_min is not None and below_min >= 30:
            if state in ("突破冲高成功", "继续强势"):
                state = "多空拉锯"
        elif state == "突破冲高成功":
            state = "继续强势"     # 现价在均价线下方, 不能叫"突破成功"

    partial = (main_pct is None or peak is None or snap["first_seen"] > "10:00")
    return {"state": state, "n": n, "conds": conds, "pos": round(pos, 0),
            "dvw": round(dvw, 2), "dhi": round(dhi, 2), "surge": surge,
            "peak_main": peak, "below_min": below_min, "partial": partial,
            "note": ("资金峰值不完整(服务启动较晚)" if snap["first_seen"] > "10:00" else "")}


def _alert_of(pct, drawdown):
    """告警判定。返回 (level, reason) —— level: ''|'warn'|'danger'。
    两类独立触发, 取更严重的一个:
      当日跌幅   pct <= -3% 警戒, <= -5% 严重
      冲高回落   自日内高点回撤 <= -5% (即便当日仍为正也告警)
    """
    hits = []
    if pct is not None:
        if pct <= ALERT_DANGER:
            hits.append(("danger", f"当日跌{abs(pct):.1f}%"))
        elif pct <= ALERT_WARN:
            hits.append(("warn", f"当日跌{abs(pct):.1f}%"))
    if drawdown is not None and drawdown <= ALERT_DRAWDOWN:
        lvl = "danger" if drawdown <= 2 * ALERT_DRAWDOWN else "warn"
        hits.append((lvl, f"自高点回撤{abs(drawdown):.1f}%"))
    if not hits:
        return "", ""
    if any(h[0] == "danger" for h in hits):
        return "danger", " · ".join(h[1] for h in hits)
    return "warn", " · ".join(h[1] for h in hits)


def _is_strong(score, label, pct, main_pct):
    """强势判定(驱动"醒目列出")。
    刻意不只卡总分线: 东财资金流经常拿不到(TLS指纹风控), 缺失时总分会被系统性压低最多3分,
    单卡分数线会在"资金数据可用/不可用"两种状态下表现不一致, 对换赛道决策是误导。
    所以用"实质条件 + 分数线"双重口径, 缺资金数据时仍能正确识别强势:
      必要条件: 趋势在上升通道(主升浪/上升趋势) 且 今日在涨
      再满足其一: 主升浪 / 分数≥6 / 资金明确流入(≥1%)
    """
    if label not in ("主升浪", "上升趋势"):
        return False
    if pct is None or pct <= 0:
        return False
    return bool(label == "主升浪" or score >= 6.0
                or (main_pct is not None and main_pct >= 1))


def watchlist_run():
    """换赛道雷达: 观察池全量实时强度快照, 按综合评分降序 + 按赛道聚合。"""
    codes = [c for c, _, _ in WATCHLIST]
    try:
        rt = tencent_realtime(codes)  # 价格/涨幅主数据源, 稳定可达
    except Exception:
        rt = {}
    flows = _em_cached(codes)  # 主力资金增强信号, 尽力而为; 东财拒连时仅资金标签显示"数据缺"

    snaps = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_trend_snapshot, c): c for c in codes}
        for fut in futs:
            code = futs[fut]
            try:
                snaps[code] = fut.result()
            except Exception:
                snaps[code] = {"closes": [], "avg5_vol": None}

    now_hm = datetime.now().strftime("%H:%M")
    today = datetime.now().strftime("%Y-%m-%d")
    items = []
    for code, name, branch in WATCHLIST:
        q = rt.get(code) or {}
        f = flows.get(code) or {}
        pct = q.get("pct")
        main_pct = f.get("main_pct")
        mom = _momentum(code, pct)
        sn = snaps.get(code) or {}
        closes = sn.get("closes") or []
        live_px = q.get("price")
        # 与盘中回测同口径: D-1序列 + 当前实时价。
        # 仅当行情确属今天才拼接: 盘前/休市日腾讯返回的"现价"就是昨收, 拼上去会让昨天
        # 在序列里出现两次, MA/5日涨幅/20日高点全部错位一天。
        if live_px and q.get("qdate") == today:
            trend = _trend_features(closes + [live_px])
        else:
            trend = _trend_features(closes)
        trend["avg5_vol"] = sn.get("avg5_vol")
        label, is_main_wave = _classify_wave(trend, pct)
        trend = dict(trend, label=label, is_main_wave=is_main_wave)
        score, conds = _score_stock(pct, trend, mom, main_pct)
        if main_pct is None:
            flow_label = "资金数据缺"
        elif main_pct >= 5:
            flow_label = "大幅流入"
        elif main_pct >= 1:
            flow_label = "温和流入"
        elif main_pct > -1:
            flow_label = "持平"
        else:
            flow_label = "流出"

        # 冲高回落: 现价相对日内最高价的回撤(告警只对重点监视对象生效)
        hi = q.get("high")
        px = q.get("price")
        drawdown = round((px / hi - 1) * 100, 2) if (hi and px) else None
        shape = _shape_state(code, q, main_pct, now_hm)
        vol = _volume_state(q, trend.get("avg5_vol"), pct, now_hm,
                            trend.get("ext_from_high20_pct"))
        if code in FOCUS:
            alert, alert_reason = _alert_of(pct, drawdown)
            # 形态判定为走弱时也升级告警: 它比"当日跌3%"更早(寒武纪10:00破线时
            # 还涨着+3.58%, 当日跌幅告警要到收盘才响)
            if shape["state"].startswith("危险"):
                if not alert:
                    alert, alert_reason = "warn", "形态转弱(破均价线/资金衰减)"
                elif alert == "warn":
                    alert = "danger"
                    alert_reason += " · 形态转弱"
        else:
            alert, alert_reason = "", ""

        items.append({
            "code": code, "name": q.get("name") or name, "branch": branch,
            "price": q.get("price"), "pct": pct, "turnover": q.get("turnover"),
            "mcap_yi": f.get("mcap_yi"),
            "main_net_yi": f.get("main_net_yi"), "main_pct": main_pct,
            "super_net_yi": f.get("super_net_yi"),
            "flow_label": flow_label,
            "main_inflow": bool(main_pct is not None and main_pct > 0),
            "trend_label": trend.get("label", "数据不足"),
            "is_main_wave": bool(trend.get("is_main_wave")),
            "ret20_pct": trend.get("ret20_pct"),
            "ext_from_high20_pct": trend.get("ext_from_high20_pct"),
            "chg_6s": mom.get("chg_6s"), "chg_20s": mom.get("chg_20s"),
            "chg_5m": mom.get("chg_5m"), "chg_15m": mom.get("chg_15m"),
            "speed_label": mom.get("speed_label"),
            "score": score, "conds": conds,
            "is_strong": (code not in DEFENSIVE
                          and _is_strong(score, trend.get("label"), pct, main_pct)),
            "is_defensive": code in DEFENSIVE,
            "is_focus": code in FOCUS,
            "high": q.get("high"), "drawdown": drawdown,
            "vwap": q.get("vwap"),
            "vol_ratio": vol["ratio"], "vol_label": vol["label"],
            "vol_tier": vol["tier"], "vol_warn": vol["warn"],
            "shape": shape["state"], "shape_n": shape.get("n"),
            "shape_conds": shape.get("conds", []),
            "shape_partial": shape.get("partial", False),
            "shape_note": shape.get("note", ""),
            "alert": alert, "alert_reason": alert_reason,
        })
    items.sort(key=lambda x: -x["score"])

    sectors = OrderedDict()
    for it in items:
        sectors.setdefault(it["branch"], []).append(it)
    sector_list = []
    for branch, arr in sectors.items():
        n = len(arr)
        avg_score = round(sum(x["score"] for x in arr) / n, 2)
        pct_vals = [x["pct"] for x in arr if x["pct"] is not None]
        avg_pct = round(sum(pct_vals) / len(pct_vals), 2) if pct_vals else None
        inflow_n = sum(1 for x in arr if x["main_inflow"])
        strong_n = sum(1 for x in arr if x["is_strong"])
        sector_list.append({"branch": branch, "n": n, "avg_score": avg_score,
                             "avg_pct": avg_pct, "inflow_n": inflow_n,
                             "strong_n": strong_n})
    # 换赛道优先看"强势家数占比", 其次看均分: 一条赛道整体在动比个别票冲高更值得切入
    sector_list.sort(key=lambda x: (-(x["strong_n"] / x["n"]), -x["avg_score"]))

    # ── 避险信号 ──
    # 红利/防御 在涨 而 成长在跌 = 资金撤离风险资产, 是"有重大利空"的经典特征。
    # 只看红利自己涨跌没用(它波动小), 关键是与成长的剪刀差 + 是否有真金白银流入。
    dfs = [x for x in items if x["is_defensive"] and x["pct"] is not None]
    grw = [x for x in items if not x["is_defensive"] and x["pct"] is not None]
    risk = None
    if dfs and grw:
        d_pct = sum(x["pct"] for x in dfs) / len(dfs)
        g_pct = sum(x["pct"] for x in grw) / len(grw)
        spread = d_pct - g_pct
        d_in = sum(1 for x in dfs if x["main_inflow"])
        d_flow = [x["main_pct"] for x in dfs if x["main_pct"] is not None]
        d_flow_avg = (sum(d_flow) / len(d_flow)) if d_flow else None
        if d_pct > 0 and g_pct < 0 and spread >= 2.0:
            lvl, txt = "danger", "资金明显撤向防御，警惕重大利空"
        elif d_pct > 0 and g_pct < 0:
            lvl, txt = "warn", "资金转向防御，成长承压"
        elif spread >= 2.0:
            lvl, txt = "warn", "红利显著跑赢成长，风险偏好下降"
        elif g_pct > 0 and d_pct < g_pct:
            lvl, txt = "", "风险偏好正常，资金在成长端"
        else:
            lvl, txt = "", "无明显避险迹象"
        risk = {"level": lvl, "text": txt,
                "def_pct": round(d_pct, 2), "grow_pct": round(g_pct, 2),
                "spread": round(spread, 2),
                "def_inflow_n": d_in, "def_n": len(dfs),
                "def_flow_avg": round(d_flow_avg, 1) if d_flow_avg is not None else None}

    strong = [it for it in items if it["is_strong"]]
    focus = [it for it in items if it["is_focus"]]
    focus.sort(key=lambda x: (x["pct"] if x["pct"] is not None else 0))
    alerts = [it for it in focus if it["alert"]]
    alerts.sort(key=lambda x: (x["alert"] != "danger",
                                x["pct"] if x["pct"] is not None else 0))
    return {"time": datetime.now().strftime("%H:%M:%S"),
            "items": items, "sectors": sector_list,
            "strong": strong, "strong_n": len(strong), "total_n": len(items),
            "focus": focus, "focus_n": len(focus),
            "risk": risk,
            "alerts": alerts, "alert_n": len(alerts),
            "danger_n": sum(1 for a in alerts if a["alert"] == "danger"),
            "flow_ok": bool(flows)}


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
.layout { display:flex; gap:1rem; align-items:flex-start; }
.mainCol { flex:1; min-width:0; }
.wlCol { flex:0 0 calc(80% - .5rem); max-width:calc(80% - .5rem); min-width:0; }
@media (max-width:1100px) { .layout{flex-direction:column;} .wlCol{flex:0 0 auto;
  width:100%; max-width:100%;} .mainCol{width:100%;} }
.wltop { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr));
  gap:0 .8rem; align-items:start; }
.wltop > div:empty { display:none; }
.wlpanel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:.8rem; }
.wlhead { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:.5rem; gap:.5rem; }
.wlhead h2 { font-size:1rem; margin:0; }
.wlhead button { padding:.2rem .6rem; font-size:.75rem; }
.wlsectors { display:flex; flex-direction:column; gap:.28rem; margin-bottom:.7rem;
  border-bottom:1px dashed var(--line); padding-bottom:.6rem; }
.wlsecthead { display:flex; justify-content:space-between; align-items:baseline;
  cursor:pointer; user-select:none; font-size:.72rem; color:var(--muted);
  padding:.1rem .1rem .25rem; }
.wlsecthead:hover { color:var(--accent); }
.wlsecthead b { color:var(--ink); font-size:.78rem; }
.wlsecthead .chev { font-size:.68rem; white-space:nowrap; }
.wlsector { display:flex; align-items:baseline; gap:.4rem; font-size:.74rem; }
.wlsector-name { flex:0 0 68px; color:var(--muted); }
.wlsector-score { font-weight:800; font-size:1.05rem; font-variant-numeric:tabular-nums; width:2.6rem; }
.wlsector-meta { color:var(--muted); }
.wlrisk { border-radius:8px; padding:.5rem .6rem; margin-bottom:.7rem; font-size:.78rem;
  border:1px solid var(--line); background:var(--bg); }
.wlrisk.warn { border:2px solid #B8860B; background:rgba(184,134,11,.10); }
.wlrisk.danger { border:2px solid var(--up); background:rgba(194,64,47,.12);
  animation:wlpulse 1.8s ease-out infinite; }
.wlrisk .hd { font-weight:800; display:flex; justify-content:space-between;
  align-items:baseline; gap:.5rem; }
.wlrisk.danger .hd { color:var(--up); }
.wlrisk.warn .hd { color:#8a6508; }
.wlrisk .nums { color:var(--muted); font-size:.72rem; margin-top:.25rem;
  font-variant-numeric:tabular-nums; }
.wlrisk .nums b { color:var(--ink); }
@keyframes wlpulse { 0%,100%{box-shadow:0 0 0 0 rgba(194,64,47,.55);} 50%{box-shadow:0 0 0 7px rgba(194,64,47,0);} }
.wlalertbox { border:2px solid var(--up); background:rgba(194,64,47,.10); border-radius:8px;
  padding:.55rem .6rem; margin-bottom:.7rem; animation:wlpulse 1.8s ease-out infinite; }
.wlalertbox.warnonly { border-color:#B8860B; background:rgba(184,134,11,.10); animation:none; }
.wlalerttitle { font-size:.9rem; font-weight:800; color:var(--up); margin-bottom:.4rem;
  display:flex; justify-content:space-between; align-items:baseline; }
.wlalertbox.warnonly .wlalerttitle { color:#8a6508; }
.wlalerttitle small { font-weight:400; font-size:.68rem; color:var(--muted); }
.wlalertrow { display:flex; align-items:center; gap:.5rem; padding:.35rem .5rem;
  background:var(--panel); border-radius:5px; margin-bottom:.3rem;
  border-left:4px solid var(--up); }
.wlalertrow.warn { border-left-color:#B8860B; }
.wlalertrow:last-child { margin-bottom:0; }
.wlalertname { font-weight:700; font-size:.86rem; flex:1; min-width:0; }
.wlalertpct { font-variant-numeric:tabular-nums; font-weight:800; font-size:1.1rem; }
.wlalertwhy { font-size:.68rem; color:var(--muted); }
.wlfocusbox { border:1px solid var(--line); border-radius:8px; padding:.5rem .6rem;
  margin-bottom:.7rem; background:var(--bg); }
.wlfocustitle { font-size:.8rem; font-weight:700; margin-bottom:.35rem; color:var(--accent);
  display:flex; justify-content:space-between; align-items:baseline; }
.wlfocustitle small { font-weight:400; font-size:.68rem; color:var(--muted); }
.wlfocusrow { display:flex; align-items:center; gap:.4rem; font-size:.76rem;
  padding:.16rem 0; border-bottom:1px dotted var(--line); }
.wlfocusrow:last-child { border-bottom:none; }
.wlfocusrow .nm { flex:1; min-width:0; }
.wlfocusrow .pc { font-variant-numeric:tabular-nums; font-weight:700; width:4.2rem; text-align:right; }
.wlfocusrow .dd { font-variant-numeric:tabular-nums; width:4.6rem; text-align:right; color:var(--muted); font-size:.7rem; }
.voltag { display:inline-block; font-size:.66rem; padding:.05rem .35rem; border-radius:3px;
  border:1px dashed var(--line); color:var(--muted); margin-left:.25rem; }
.voltag.v-high { border-color:var(--up); color:var(--up); }
.voltag.v-low { border-color:var(--accent); color:var(--accent); }
.voltag.v-warn { border-style:solid; background:rgba(184,134,11,.14);
  border-color:#B8860B; color:#8a6508; font-weight:700; }
.shape { display:inline-block; font-size:.68rem; padding:.06rem .4rem; border-radius:3px;
  font-weight:700; color:#fff; margin-left:.25rem; }
.shape.s-break { background:var(--up); }
.shape.s-strong { background:#1E7F5C; }
.shape.s-mix { background:#B8860B; }
.shape.s-weak { background:#7A2E22; }
.shape.s-wait { background:var(--flat); font-weight:400; }
.shape.partial { opacity:.72; }
.wlrow.focus { background:#fff; }
.wlrow.alertwarn { border-color:#B8860B; box-shadow:inset 3px 0 0 #B8860B; }
.wlrow.alertdanger { border-color:var(--up); box-shadow:inset 3px 0 0 var(--up); }
.focustag { display:inline-block; font-size:.62rem; padding:0 .3rem; border-radius:2px;
  background:var(--accent); color:#fff; vertical-align:middle; margin-left:.25rem; }
.wlstrongbox { border:2px solid var(--up); border-radius:8px; padding:.55rem .6rem .6rem;
  margin-bottom:.7rem; background:rgba(194,64,47,.05); }
.wlstrongbox.empty { border-color:var(--line); background:var(--bg); }
.wlstrongtitle { font-size:.85rem; font-weight:800; color:var(--up); margin-bottom:.45rem;
  display:flex; justify-content:space-between; align-items:baseline; }
.wlstrongbox.empty .wlstrongtitle { color:var(--muted); font-weight:600; }
.wlstrongtitle small { font-weight:400; font-size:.7rem; color:var(--muted); }
.wlstrongrow { display:flex; align-items:center; gap:.5rem; padding:.4rem .5rem;
  background:var(--panel); border:1px solid var(--line); border-radius:6px; margin-bottom:.35rem; }
.wlstrongrow:last-child { margin-bottom:0; }
.wlstrongscore { flex:0 0 3.1rem; font-size:2rem; font-weight:800; line-height:1;
  font-variant-numeric:tabular-nums; text-align:center; color:var(--up); }
.wlstrongmain { flex:1; min-width:0; }
.wlstrongname { font-size:.95rem; font-weight:700; }
.wlstrongname .wlbranch { font-size:.72rem; }
.wlstrongmeta { font-size:.72rem; color:var(--muted); margin-top:.1rem; }
.wlstrongpct { flex:0 0 auto; text-align:right; font-variant-numeric:tabular-nums; }
.wlstrongpct b { display:block; font-size:1.05rem; }
.wlstrongpct small { font-size:.68rem; color:var(--muted); }
.wlrow.strong { border-color:var(--up); box-shadow:inset 3px 0 0 var(--up); }
.wllist { display:flex; flex-direction:column; gap:.5rem; }
.wllisthead { font-size:.72rem; color:var(--muted); margin:.2rem 0 .1rem; }
.wlrow { background:var(--bg); border:1px solid var(--line); border-radius:6px;
  padding:.35rem .6rem; }
.wlline, .wlhdrrow { display:grid; align-items:center; column-gap:.6rem;
  grid-template-columns:2.8rem 12rem 6rem 4.6rem 4.6rem 4.6rem 5.6rem 8rem 5.6rem; }
.wlhdrrow { position:sticky; top:0; z-index:2; background:var(--panel); font-size:.7rem;
  color:var(--muted); padding:.3rem .6rem; border-bottom:1px solid var(--line); }
.wlhdrrow span, .wlcell { text-align:right; font-variant-numeric:tabular-nums; }
.wlhdrrow span:first-child, .wlhdrrow span:nth-child(2),
.wlhdrrow span:nth-child(8), .wlline > .wlnamecell { text-align:left; }
.wlcell { font-size:.84rem; font-weight:600; }
.wlcell small { display:block; font-size:.66rem; font-weight:400; color:var(--muted); }
.wlcell.wlpct { font-size:1.45rem; font-weight:800; }
.wlfocusrow .pc { font-size:.95rem; }
.layout.hideMain .mainCol { display:none; }
.layout.hideMain .wlCol { flex:1 1 100%; max-width:100%; }
.wlhead button.ghost, .mainhead button.ghost { background:var(--panel); color:var(--accent);
  border:1px solid var(--accent); }
.mainhead { display:flex; justify-content:flex-end; margin-bottom:.5rem; }
.mainhead button { padding:.2rem .7rem; font-size:.78rem; }
.mainCol .disc .step { display:block; white-space:normal; margin-right:0; }
.wlline .wltags { margin:0; }
.wlwhy { display:flex; flex-wrap:wrap; align-items:center; gap:.25rem; margin-top:.25rem; }
.wlwhy .shape { margin-left:0; }
.wlwhy .wltag.up { color:var(--up); border-color:var(--up); }
.wlwhy .wltag.dn { color:var(--dn); border-color:var(--dn); }
.wlscore { font-size:1.25rem; font-weight:800; line-height:1.1;
  font-variant-numeric:tabular-nums; text-align:center; border-radius:6px; padding:.25rem 0; color:#fff; }
.wl-strong { background:var(--up); } .wl-mid { background:#B8860B; } .wl-weak { background:var(--dn); }
.wlbody { flex:1; min-width:0; }
.wlname { font-size:1.02rem; font-weight:700; }
.wlbranch { color:var(--muted); font-weight:400; font-size:.72rem; margin-left:.3rem; }
.wltags { display:flex; flex-wrap:wrap; gap:.3rem; margin:.3rem 0; }
.wltag { font-size:.68rem; padding:.1rem .4rem; border-radius:3px; background:var(--panel);
  border:1px solid var(--line); color:var(--muted); }
.wltag.wave { color:#fff; background:var(--up); border-color:var(--up); font-weight:700; }
.wltag.flow { color:var(--up); border-color:var(--up); }
.wltag.flowout { color:var(--dn); border-color:var(--dn); }
.wlnums { display:flex; justify-content:space-between; font-size:.78rem; font-variant-numeric:tabular-nums; }
.wlspeed { color:var(--muted); }
.wldetails { margin-top:.3rem; } .wldetails summary { font-size:.7rem; color:var(--muted); cursor:pointer; }
.wldetails .condlist { margin-top:.3rem; }
</style></head><body>
<h1>涨跌预测台 · 六股</h1>
<p class="sub">特征: 主力净流入 / 特大单净流入 / 板块涨跌 / 自身涨跌 ·
多通道: 通达信分笔(主) + 腾讯量能校验 + 东财官方对照 + 全同业板块二通道 ·
高置信(≥60%)才给买卖建议, 否则观望 · 概率仅供研究, 非投资建议</p>
<div class="layout">
<div class="wlCol">
  <div class="wlpanel">
    <div class="wlhead">
      <h2>换赛道雷达</h2>
      <span>
        <button class="ghost" id="wlToggleMain" onclick="toggleMainCol()">隐藏预测台</button>
        <button onclick="refreshWatchlist()">刷新</button>
        <span class="status" id="wlStatus">加载中…</span>
      </span>
    </div>
    <div class="wltop">
      <div id="wlRisk"></div>
      <div id="wlAlerts"></div>
      <div id="wlFocus"></div>
      <div id="wlStrong"></div>
    </div>
    <div class="wlsectors" id="wlSectors"></div>
    <div class="wllisthead" id="wlListHead"></div>
    <div class="wllist" id="wlList"></div>
  </div>
</div>
<div class="mainCol">
<div class="mainhead"><button class="ghost" onclick="toggleMainCol()">隐藏此栏 ▶</button></div>
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
  <button id="runTop" onclick="runTop()">压力位/做T</button>
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
<div id="topPanel"></div>
<p class="note">首次跑某只股票需重构约40天分笔(1~3分钟), 之后有本地缓存(约10秒)。
实时模式请在交易时段使用; 回测日期需为交易日。资金流为分笔重构口径, 与东财APP绝对值不可直接对照。</p>
</div>
</div>
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
  $('run').disabled = true; $('runRes').disabled = true; $('runCont').disabled = true; $('runTop').disabled = true;
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
  $('run').disabled = false; $('runRes').disabled = false; $('runCont').disabled = false; $('runTop').disabled = false;
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
  $('run').disabled = true; $('runRes').disabled = true; $('runCont').disabled = true; $('runTop').disabled = true;
  $('status').textContent = '共振预测计算中(需拉4只股票, 首次1~3分钟)...';
  $('resPanel').innerHTML = '';
  try {
    const d = await apiJson(q);
    $('resPanel').innerHTML = resonanceCard(d);
  } catch (e) {
    $('resPanel').innerHTML = `<div class="respanel"><div class="err">${e.message}</div></div>`;
  }
  $('status').textContent = '完成';
  $('run').disabled = false; $('runRes').disabled = false; $('runCont').disabled = false; $('runTop').disabled = false;
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
  $('run').disabled = true; $('runRes').disabled = true; $('runCont').disabled = true; $('runTop').disabled = true;
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
  $('run').disabled = false; $('runRes').disabled = false; $('runCont').disabled = false; $('runTop').disabled = false;
}

function lvlRows(levels) {
  return (levels||[]).map(l => `<tr><td>${l.price.toFixed(2)}</td>`+
    `<td class="${cls(l.dist_pct)}">${sgn(l.dist_pct,'%')}</td>`+
    `<td>${l.strength}</td><td>${l.tags.join('/')}</td></tr>`).join('');
}
function lvlTable(title, levels) {
  return `<div><div class="meta">${title}</div><table class="book">`+
    `<tr><th>价位</th><th>距离</th><th>强度</th><th>来源</th></tr>${lvlRows(levels)}</table></div>`;
}
function estItem(label, e) {
  if (!e) return '';
  return `<div class="probitem"><b>${e.price.toFixed(2)}</b>${label} ${sgn(e.pct,'%')}</div>`;
}
function topCard(d) {
  const envCls = d.env.step > 0 ? 'strong' : (d.env.step < 0 ? 'weak' : 'mid');
  const it = d.intraday, md = d.multiday;
  const idx = Object.entries(d.env.indices||{}).map(([k,v])=>`${k} ${sgn(v,'%')}`)
    .join(' · ') || '指数数据不可用';
  const boardTxt = d.env.board_pct === null ? '板块数据缺失' :
    `板块代理(${(d.env.board_proxies||[]).join('/')}) <b class="${cls(d.env.board_pct)}">${sgn(d.env.board_pct,'%')}</b>`;
  const warns = (d.warnings||[]).map(w=>`<div class="warn">⚠️ ${w}</div>`).join('');
  let surge = '';
  if (d.surge) {
    const s = d.surge;
    const lead = s.leader && s.leader.pct_now !== null && s.leader.pct_now !== undefined ?
      `联动龙头 ${s.leader.name} <b class="${cls(s.leader.pct_now)}">${sgn(s.leader.pct_now,'%')}</b> · ` : '';
    const condHtml = s.conditions.map(c =>
      `<li class="${c.met?'plus':''}">${c.met?'⚡':'·'} ${c.name}`+
      `${c.hist_rate!==null?`（历史该条件下暴涨率 ${pct(c.hist_rate)}，n=${c.n}）`:''}</li>`).join('');
    if (s.active) {
      const cq = s.cond_quantiles;
      surge = `<div class="warn">⚡ 暴涨日特征触发 — ${lead}`+
        (cq ? `同条件${cq.n}日上冲 P50 ${sgn(cq.q50,'%')} / P80 ${sgn(cq.q80,'%')} / P90 ${sgn(cq.q90,'%')}，`+
              `高抛区已上移，<b>只减不清</b>`
            : `同条件历史样本不足，高抛区未上移，建议只减不清`) + `</div>`;
    } else {
      surge = `<div class="meta">暴涨守卫未触发（${lead}暴涨=上冲≥${s.thr_pct}%，基础概率 ${pct(s.base_rate)}）</div>`;
    }
    surge += `<details><summary>暴涨守卫条件明细</summary><ul class="reasonlist">${condHtml}</ul></details>`;
  }
  if (d.crash) {
    const s = d.crash;
    const condHtml = s.conditions.map(c =>
      `<li class="${c.met?'minus':''}">${c.met?'🔻':'·'} ${c.name}`+
      `${c.hist_rate!==null?`（历史该条件下暴跌率 ${pct(c.hist_rate)}，n=${c.n}）`:''}</li>`).join('');
    if (s.active) {
      const cq = s.cond_quantiles;
      surge += `<div class="warn">🔻 暴跌日特征触发 — `+
        (cq ? `同条件${cq.n}日下杀 P50 ${sgn(cq.q50,'%')} / P20 ${sgn(cq.q20,'%')} / P10 ${sgn(cq.q10,'%')}，`+
              `低吸区已下移，<b>不接飞刀</b>，收复分时均价线再接`
            : `同条件历史样本不足，建议暂缓低吸，等企稳信号`) + `</div>`;
    }
    surge += `<details><summary>暴跌守卫条件明细（暴跌=下杀≤${s.thr_pct}%，基础概率 ${pct(s.base_rate)}）</summary>`+
      `<ul class="reasonlist">${condHtml}</ul></details>`;
  }
  let est = '';
  if (it) est += estItem('当日顶P50', it.q50) + estItem('当日顶P80', it.q80);
  if (md) est += estItem('3日顶P50', md.q50) + estItem('3日顶P80', md.q80);
  let zones;
  if (it) {
    const snapTxt = it.snap ? `，≈压力位 <b>${it.snap.price.toFixed(2)}</b>（${it.snap.tags.join('/')}）` : '';
    const bandTxt = it.surge_shift && it.base_sell_zone ?
      `暴涨守卫上移，常规区 ${it.base_sell_zone[0].toFixed(2)}~${it.base_sell_zone[1].toFixed(2)}` :
      `环境${d.env.stance}取P${Math.round(d.env.band[0]*100)}~P${Math.round(d.env.band[1]*100)}`;
    zones = `<div class="advice"><b>高抛区：</b>${it.sell_zone[0].toFixed(2)} ~ ${it.sell_zone[1].toFixed(2)}`+
      `（${bandTxt}${it.capped_by_limit?'，触涨停截断':''}）${snapTxt}<br>`+
      `<b>低吸区：</b>${it.dip_veto?'<b style="color:var(--up)">⚠️联动否决在线，暂不接</b> ':''}`+
      `${it.dip_zone[0].toFixed(2)} ~ ${it.dip_zone[1].toFixed(2)}`+
      `（${it.crash_shift&&it.base_dip_zone?`暴跌守卫下移，常规区 ${it.base_dip_zone[0].toFixed(2)}~${it.base_dip_zone[1].toFixed(2)}`:'相似日回撤P20~P50'}；破分时均价 ${d.vwap.toFixed(2)} 不接）`+
      `${it.t_advice?`<br><b>操作：</b>${it.t_advice}`:''}</div>`;
  } else {
    zones = `<div class="advice">时点已晚，当日剩余时间不足，仅参考3日顶部与压力位</div>`;
  }
  let timing = '';
  if (it && it.high_time) timing = `顶部时间：相似日中位 ${it.high_time.median}，`+
    `${pct(it.high_time.before_1130)}出现在11:30前，${pct(it.high_time.before_1400)}在14:00前`;
  let valid = '';
  if (it && it.validation) valid = ` · 走步验证${it.validation.n}天：P80覆盖率`+
    `${pct(it.validation.coverage80)}，P50中位误差${it.validation.median_err50}%`;
  if (md && md.validation) valid += ` · 3日验证${md.validation.n}天：P80覆盖率`+
    `${pct(md.validation.coverage80)}，P50中位误差${md.validation.median_err50}%`;
  let actual = '';
  if (it && it.actual) actual += `<div class="actual">回测实际：之后最高 <b>${it.actual.high.toFixed(2)}</b>`+
    `（${sgn(it.actual.pct,'%')}）于 ${it.actual.time} ${it.actual.hit_zone?'✓触及高抛区':'✗未及高抛区'}</div>`;
  if (md && md.actual) actual += `<div class="actual">回测实际3日最高：<b>${md.actual.high.toFixed(2)}</b>`+
    `（${sgn(md.actual.pct,'%')}）</div>`;
  const mdSnap = md && md.snap ? ` · 3日顶≈压力位 ${md.snap.price.toFixed(2)}（${md.snap.tags.join('/')}）` : '';
  return `<div class="contcard ${envCls}">
    <div class="conthead"><h3>${d.name} <span class="meta">${d.code}</span></h3>
      <div class="scorebox"><b>${d.price.toFixed(2)}</b><span class="${cls(d.self_pct)}">${sgn(d.self_pct,'%')}</span></div></div>
    <div class="contstate">${d.date} ${d.cutoff}时点 · 涨停 ${d.limit_up.toFixed(2)} · 环境${d.env.stance}`+
    `${d.trend?` · MA20${d.trend.side==='above'?'上方':'下方'} <b class="${cls(d.trend.dist_pct)}">${sgn(d.trend.dist_pct,'%')}</b>`:''}</div>
    <div class="residx">${boardTxt} · ${idx}</div>
    ${warns}${surge}
    <div class="probgrid">${est}</div>
    ${zones}
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:.6rem;margin-top:.5rem">
      ${lvlTable('上方压力位', d.resistance)}${lvlTable('下方支撑位', d.support)}
    </div>
    ${actual}
    <div class="contfoot">${timing}${mdSnap}<br>历史样本${d.samples}天(相似盘面${it?it.n_analogs:'—'}个)${valid}<br>${d.method}</div>
  </div>`;
}
async function runTop() {
  const selected = $('code').value;
  const codes = selected === 'ALL' ? __ALL_CODES__ : [selected];
  $('run').disabled = true; $('runRes').disabled = true; $('runCont').disabled = true; $('runTop').disabled = true;
  $('topPanel').innerHTML = `<div class="contpanel"><div class="conttitle">`+
    `<h2>动态压力位 · 顶部预估</h2><span class="status">计算中…</span></div>`+
    `<div class="contgrid" id="topGrid"></div></div>`;
  for (const code of codes) {
    $('status').textContent = `${code} 压力位计算中（首次需重构240天分笔，可能5~10分钟）...`;
    try {
      const params = new URLSearchParams({code});
      if ($('mode').value === 'backtest') {
        params.set('date', $('date').value); params.set('cutoff', $('cutoff').value);
      }
      const d = await apiJson('/api/top?' + params.toString(), 600000);
      $('topGrid').insertAdjacentHTML('beforeend', topCard(d));
    } catch (e) {
      $('topGrid').insertAdjacentHTML('beforeend',
        `<div class="contcard weak"><h3>${code}</h3><div class="err">${e.message}</div></div>`);
    }
  }
  const st = $('topPanel').querySelector('.conttitle .status');
  if (st) st.textContent = '完成';
  $('status').textContent = '完成';
  $('run').disabled = false; $('runRes').disabled = false; $('runCont').disabled = false; $('runTop').disabled = false;
}

// ───────────────────────── 换赛道雷达 ─────────────────────────
function scoreCls(s) { return s >= 7 ? 'wl-strong' : (s >= 4 ? 'wl-mid' : 'wl-weak'); }
const WL_SPEED_ICON = {'加速上涨':'⏫','温和走强':'↗','走平':'→','转弱':'↘','加速下跌':'⏬',
  '首次采样':'…','行情数据缺':'?'};
function wlSectorRow(s) {
  const pctTxt = s.avg_pct === null || s.avg_pct === undefined ? '—' : sgn(s.avg_pct, '%');
  // 红利/防御是风向标不是可切入方向, 不显示"强势N/N"(它永远是0, 显示出来是误导)
  const strongTxt = s.branch === '红利/防御' ? '<b>风向标</b>'
    : (s.strong_n > 0 ? `<b style="color:var(--up)">强势${s.strong_n}/${s.n}</b>`
                      : `强势0/${s.n}`);
  return `<div class="wlsector">
    <span class="wlsector-name">${s.branch}</span>
    <span class="wlsector-score" style="color:${s.avg_score>=7?'var(--up)':(s.avg_score>=4?'#B8860B':'var(--dn)')}">${s.avg_score.toFixed(1)}</span>
    <span class="wlsector-meta">${strongTxt} · 均涨${pctTxt} · 资金流入${s.inflow_n}/${s.n}</span>
  </div>`;
}
// 赛道列表折叠: 默认只显示最强的一个, 避免14条赛道把下面的强势榜/个股列表挤出视野。
// 展开状态存 localStorage, 刷新和30秒轮询都不会重置。
let _wlSecOpen = false;
try { _wlSecOpen = localStorage.getItem('wlSecOpen') === '1'; } catch (_) {}
let _wlLastData = null;
function wlSectorsBox(d) {
  const list = d.sectors || [];
  if (!list.length) return '';
  const top = list[0];
  // 折叠态: 整个列表隐藏, 只留一行摘要(最强赛道名+分数+均涨), 不占版面
  if (!_wlSecOpen) {
    const pct = (top.avg_pct === null || top.avg_pct === undefined)
      ? '' : ` ${sgn(top.avg_pct, '%')}`;
    return `<div class="wlsecthead" onclick="toggleSectors()">
      <span>最强赛道 <b>${top.branch}</b>
        <span style="color:${top.avg_score>=7?'var(--up)':(top.avg_score>=4?'#B8860B':'var(--dn)')};font-weight:700">
        ${top.avg_score.toFixed(1)}</span>${pct}</span>
      <span class="chev">赛道 ${list.length} 条 ▼</span></div>`;
  }
  return `<div class="wlsecthead" onclick="toggleSectors()">
      <span><b>赛道强弱</b></span>
      <span class="chev">收起 ▲</span></div>` + list.map(wlSectorRow).join('');
}
function toggleSectors() {
  _wlSecOpen = !_wlSecOpen;
  try { localStorage.setItem('wlSecOpen', _wlSecOpen ? '1' : '0'); } catch (_) {}
  if (_wlLastData) $('wlSectors').innerHTML = wlSectorsBox(_wlLastData);
}
function wlStrongRow(it) {
  const pctTxt = it.pct===null||it.pct===undefined ? '—' : sgn(it.pct,'%');
  const chg5 = (it.chg_5m===null||it.chg_5m===undefined) ? '' : ` ${sgn(it.chg_5m,'%')}`;
  const flowTxt = it.main_pct!=null
    ? `资金${sgn(it.main_pct,'%')}` : it.flow_label;
  return `<div class="wlstrongrow">
    <div class="wlstrongscore">${it.score.toFixed(1)}</div>
    <div class="wlstrongmain">
      <div class="wlstrongname">${it.name}<span class="wlbranch">${it.branch}</span></div>
      <div class="wlstrongmeta">${it.is_main_wave?'⚡主升浪':it.trend_label} · ${flowTxt} ·
        ${WL_SPEED_ICON[it.speed_label]||'·'}${it.speed_label}${chg5}</div>
    </div>
    <div class="wlstrongpct"><b class="${cls(it.pct)}">${pctTxt}</b>
      <small>${it.price!=null?it.price.toFixed(2):'—'}</small></div>
  </div>`;
}
function wlRiskBox(d) {
  const r = d.risk;
  if (!r) return '';
  const icon = r.level === 'danger' ? '🔴' : (r.level === 'warn' ? '🟡' : '🟢');
  const flow = (r.def_flow_avg === null || r.def_flow_avg === undefined)
    ? '资金数据缺' : `红利主力均值 <b>${sgn(r.def_flow_avg,'%')}</b>`;
  return `<div class="wlrisk ${r.level}">
    <div class="hd"><span>${icon} ${r.text}</span>
      <span style="font-weight:400;font-size:.68rem;color:var(--muted)">避险信号</span></div>
    <div class="nums">红利 <b class="${cls(r.def_pct)}">${sgn(r.def_pct,'%')}</b> ·
      成长 <b class="${cls(r.grow_pct)}">${sgn(r.grow_pct,'%')}</b> ·
      剪刀差 <b class="${cls(r.spread)}">${sgn(r.spread,'%')}</b> ·
      流入 <b>${r.def_inflow_n}/${r.def_n}</b> · ${flow}</div>
  </div>`;
}
function wlAlertBox(d) {
  if (!d.alerts || !d.alerts.length)
    return `<div class="wlfocusbox"><div class="wlfocustitle">
      <span>✅ 重点监视 · 无告警</span>
      <small>阈值 跌3%警戒 / 跌5%严重 / 自高点回撤5%</small></div></div>`;
  const onlyWarn = d.danger_n === 0;
  const rows = d.alerts.map(it => `<div class="wlalertrow ${it.alert}">
      <span class="wlalertname">${it.alert==='danger'?'🔴':'🟡'} ${it.name}
        <span class="wlalertwhy">${it.alert_reason}</span></span>
      <span class="wlalertpct ${cls(it.pct)}">${sgn(it.pct,'%')}</span>
    </div>`).join('');
  return `<div class="wlalertbox${onlyWarn?' warnonly':''}">
    <div class="wlalerttitle">
      <span>⚠️ 告警 ${d.alert_n} 只${d.danger_n?`（严重 ${d.danger_n}）`:''}</span>
      <small>重点监视 ${d.focus_n} 只</small></div>
    ${rows}</div>`;
}
function wlFocusBox(d) {
  if (!d.focus || !d.focus.length) return '';
  const rows = d.focus.map(it => {
    const dd = (it.drawdown===null||it.drawdown===undefined) ? '' : `${it.drawdown.toFixed(1)}%`;
    const mark = it.alert === 'danger' ? '🔴' : (it.alert === 'warn' ? '🟡' : '·');
    return `<div class="wlfocusrow">
      <span class="nm">${mark} ${it.name}${shapeTag(it)}${it.vol_warn?volTag(it):''}</span>
      <span class="dd" title="自日内高点回撤">${dd}</span>
      <span class="pc ${cls(it.pct)}">${it.pct===null?'—':sgn(it.pct,'%')}</span>
    </div>`;
  }).join('');
  return `<div class="wlfocusbox"><div class="wlfocustitle">
    <span>🎯 重点监视 ${d.focus_n} 只</span><small>右列=自高点回撤 · 按涨幅升序</small>
    </div>${rows}</div>`;
}
function wlStrongBox(d) {
  const note = d.flow_ok ? '' : ' · 东财资金流暂不可达，已按趋势+涨速口径判定';
  if (!d.strong || !d.strong.length) {
    return `<div class="wlstrongbox empty"><div class="wlstrongtitle">
      <span>当前无强势标的</span><small>${d.total_n}只在监视${note}</small></div>
      <div class="wlstrongmeta" style="font-size:.74rem;color:var(--muted)">
      判定：趋势处上升通道(主升浪/上升趋势) 且 今日上涨 且 (主升浪 / 评分≥6 / 资金流入≥1%)</div></div>`;
  }
  return `<div class="wlstrongbox"><div class="wlstrongtitle">
    <span>🔥 当前强势 ${d.strong.length}/${d.total_n}</span>
    <small>可切入方向${note}</small></div>
    ${d.strong.map(wlStrongRow).join('')}</div>`;
}
const SHAPE_CLS = {'突破冲高成功':'s-break','继续强势':'s-strong','多空拉锯':'s-mix',
  '危险：回落走弱':'s-weak','观察中':'s-wait','数据不足':'s-wait'};
function shapeTag(it) {
  if (!it.shape) return '';
  const c = SHAPE_CLS[it.shape] || 's-wait';
  const n = (it.shape_n === null || it.shape_n === undefined) ? '' : ` ${it.shape_n}/5`;
  const t = it.shape_note ? ` title="${it.shape_note}"` : '';
  return `<span class="shape ${c}${it.shape_partial?' partial':''}"${t}>${it.shape}${n}</span>`;
}
// 量能标签: 仅标注不计分(三轮实证均判定对次日收益无稳健预测力),
// 唯一例外是"高位放量滞涨"(超额-0.33%,胜率46%,前后两段同号)单独高亮为警示。
function volTag(it) {
  if (!it.vol_label) return '';
  if (it.vol_warn)
    return `<span class="voltag v-warn" title="量比${it.vol_ratio}">⚠${it.vol_warn}</span>`;
  if (it.vol_ratio === null || it.vol_ratio === undefined)
    return `<span class="voltag" title="盘中不足5分钟或缺日均量">${it.vol_label}</span>`;
  const c = it.vol_tier === 'high' ? 'v-high' : (it.vol_tier === 'low' ? 'v-low' : '');
  return `<span class="voltag ${c}" title="同时段量比(已按已交易时长折算), 不计入评分">`
    + `${it.vol_label} ${it.vol_ratio.toFixed(2)}</span>`;
}
// 短周期走势单元格: 涨幅变化(百分点), 无足够样本时显示"—"
function wlTrendCell(v) {
  if (v === null || v === undefined) return '<div class="wlcell">—</div>';
  return `<div class="wlcell ${cls(v)}">${sgn(v,'%')}</div>`;
}
// 评分依据: 命中的条件常显, 完整条件(含未命中) + 形态判定五项放进折叠区。
// 3秒重绘会重建DOM, 展开状态按code记住, 否则点开后立刻被收起。
const _wlOpen = new Set();
function wlToggle(code, open) { if (open) _wlOpen.add(code); else _wlOpen.delete(code); }
function wlRow(it) {
  const waveTag = it.is_main_wave ? '<span class="wltag wave">⚡主升浪</span>' :
    `<span class="wltag">${it.trend_label}</span>`;
  const flowTag = it.main_inflow ?
    `<span class="wltag flow">✓资金流入${it.main_pct!=null?' '+it.main_pct.toFixed(1)+'%':''}</span>` :
    `<span class="wltag flowout">${it.flow_label}</span>`;
  const pctTxt = it.pct===null||it.pct===undefined ? '—' : sgn(it.pct,'%');
  const px = it.price!=null ? it.price.toFixed(/^(5|159)/.test(it.code) ? 3 : 2) : '—';
  const speed = `${WL_SPEED_ICON[it.speed_label]||'·'}${it.speed_label}`;
  const sup = it.super_net_yi;
  const supCell = (sup===null||sup===undefined) ? '<div class="wlcell">—</div>'
    : `<div class="wlcell ${cls(sup)}">${sgn(sup,'亿')}</div>`;
  const metChips = (it.conds||[]).filter(c => c.met).map(c =>
    `<span class="wltag ${c.pts>0?'up':'dn'}">${c.name} ${c.pts>0?'+':''}${c.pts}</span>`).join('');
  const shapeHtml = (it.shape_conds||[]).map(c =>
    `<div class="cond"><span>${c.met?'✓':'·'} ${c.name}</span></div>`).join('');
  const rowCls = (it.is_strong?' strong':'') + (it.is_focus?' focus':'')
    + (it.alert==='danger'?' alertdanger':(it.alert==='warn'?' alertwarn':''));
  return `<div class="wlrow${rowCls}">
    <div class="wlline">
      <div class="wlscore ${scoreCls(it.score)}">${it.score.toFixed(1)}</div>
      <div class="wlnamecell"><div class="wlname">${it.name}${it.is_focus?'<span class="focustag">重点</span>':''}<span class="wlbranch">${it.branch}</span></div>
        <div class="wlbranch" style="margin:0">${it.code}</div></div>
      <div class="wlcell wlpct ${cls(it.pct)}">${pctTxt}</div>
      <div class="wlcell">${px}</div>
      ${wlTrendCell(it.chg_6s)}
      ${wlTrendCell(it.chg_20s)}
      <div class="wlcell ${cls(it.chg_5m)}">${(it.chg_5m===null||it.chg_5m===undefined)?'—':sgn(it.chg_5m,'%')}<small>${speed}</small></div>
      <div class="wlcell" style="text-align:left">${volTag(it) || '—'}</div>
      ${supCell}
    </div>
    <div class="wlwhy">${shapeTag(it)}${waveTag}${flowTag}${metChips}</div>
    <details class="wldetails" ${_wlOpen.has(it.code)?'open':''}
        ontoggle="wlToggle('${it.code}', this.open)"><summary>全部评分条件 / 形态判定</summary>
      <div class="condlist">${(it.conds||[]).map(condRow).join('')}</div>
      ${shapeHtml?`<div class="condlist">${shapeHtml}</div>`:''}</details>
  </div>`;
}
const WL_HDR = `<div class="wlhdrrow"><span>评分</span><span>股票</span><span>涨跌幅</span>
  <span>现价</span><span title="近6秒涨幅变化">6秒</span><span title="近20秒涨幅变化">20秒</span>
  <span title="近5分钟涨幅变化 + 涨速判断">5分钟</span>
  <span style="text-align:left" title="同时段量比, 对比前5日均量, 不计入评分">量能(量比)</span>
  <span title="东财特大单当日净流入累计">特大资金</span></div>`;
// 告警去重: 同一只票在同一告警级别下只提醒一次, 升级(warn->danger)会再提醒一次,
// 避免30秒轮询把同一个告警反复弹出来。
const _wlNotified = {};
function notifyAlerts(d) {
  const fresh = [];
  for (const it of (d.alerts || [])) {
    if (_wlNotified[it.code] !== it.alert) { fresh.push(it); _wlNotified[it.code] = it.alert; }
  }
  for (const code of Object.keys(_wlNotified)) {
    if (!(d.alerts || []).some(a => a.code === code)) delete _wlNotified[code];  // 已恢复
  }
  if (!fresh.length) return;
  const body = fresh.map(x => `${x.name} ${sgn(x.pct,'%')} (${x.alert_reason})`).join(String.fromCharCode(10));
  try {
    if (window.Notification && Notification.permission === 'granted')
      new Notification(`⚠️ 重点监视告警 ${fresh.length} 只`, {body, tag: 'wl-alert'});
  } catch (_) {}
  try {  // 声音提示: WebAudio 蜂鸣, 不依赖外部音频文件(CSP/离线均可用)
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (Ctx) {
      const ctx = new Ctx(), o = ctx.createOscillator(), g = ctx.createGain();
      o.type = 'sine';
      o.frequency.value = fresh.some(x => x.alert === 'danger') ? 880 : 620;
      g.gain.setValueAtTime(0.09, ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.45);
      o.connect(g); g.connect(ctx.destination);
      o.start(); o.stop(ctx.currentTime + 0.45);
    }
  } catch (_) {}
}
// 隐藏右侧预测台后, 雷达占满整屏; 状态存 localStorage, 刷新页面后保持
function applyMainCol(hidden) {
  document.querySelector('.layout').classList.toggle('hideMain', hidden);
  $('wlToggleMain').textContent = hidden ? '显示预测台' : '隐藏预测台';
}
function toggleMainCol() {
  const hidden = !document.querySelector('.layout').classList.contains('hideMain');
  applyMainCol(hidden);
  try { localStorage.setItem('wlHideMain', hidden ? '1' : '0'); } catch (_) {}
}
try { applyMainCol(localStorage.getItem('wlHideMain') === '1'); } catch (_) {}
let _wlTimer = null, _wlBusy = false;
async function refreshWatchlist() {
  if (_wlBusy) return;   // 3秒轮询下, 上一次未返回就跳过, 避免请求堆积
  _wlBusy = true;
  try {
    const d = await apiJson('/api/watchlist', _wlLastData ? 8000 : 45000, false);
    $('wlStatus').textContent = `更新于 ${d.time}`;
    _wlLastData = d;
    $('wlRisk').innerHTML = wlRiskBox(d);
    $('wlAlerts').innerHTML = wlAlertBox(d);
    $('wlFocus').innerHTML = wlFocusBox(d);
    // 标签页标题带告警数, 不切窗口也能看见
    document.title = d.alert_n ? `🔴${d.alert_n} 涨跌预测台` : '涨跌预测台';
    notifyAlerts(d);
    $('wlStrong').innerHTML = wlStrongBox(d);
    $('wlSectors').innerHTML = wlSectorsBox(d);
    $('wlListHead').textContent = `全部 ${d.total_n} 只 · 按评分降序 · 价格/走势每3秒刷新, 资金流每30秒更新`;
    $('wlList').innerHTML = WL_HDR + d.items.map(wlRow).join('');
  } catch (e) {
    $('wlStatus').textContent = `刷新失败: ${e.message}`;
  } finally { _wlBusy = false; }
}
function wlStart() {
  refreshWatchlist();
  if (_wlTimer) clearInterval(_wlTimer);
  _wlTimer = setInterval(refreshWatchlist, 3000);
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { if (_wlTimer) clearInterval(_wlTimer); }
  else wlStart();
});
// 浏览器通知与声音都需要用户手势后才允许, 首次点击页面时申请一次
document.addEventListener('click', function _once() {
  try { if (window.Notification && Notification.permission === 'default')
    Notification.requestPermission(); } catch (_) {}
  document.removeEventListener('click', _once);
}, {once: true});
wlStart();
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
                          "/api/continuation_group", "/api/top", "/api/watchlist"):
            self._send('{"error":"not found"}', status=404)
            return
        if u.path == "/api/watchlist":
            try:
                self._send(json.dumps(watchlist_run(), ensure_ascii=False))
            except Exception as e:
                traceback.print_exc()
                self._send_error(e)
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
        if u.path == "/api/top":
            try:
                train = max(30, min(400, int((q.get("train") or ["240"])[0])))
            except ValueError:
                train = 240
            key = (code, date, cutoff, train)
            if date and key in _topcache:
                self._send(json.dumps(_topcache[key], ensure_ascii=False))
                return
            try:
                with _lock:
                    if date and key in _topcache:
                        result = _topcache[key]
                    else:
                        result = top_forecast(code, date, cutoff, train,
                                              progress=lambda m: print(f"  {m}"))
                        if date:
                            _topcache[key] = result
                self._send(json.dumps(result, ensure_ascii=False))
            except ValueError as e:
                self._send(json.dumps({"error": str(e)}, ensure_ascii=False))
            except Exception as e:
                traceback.print_exc()
                self._send_error(e)
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
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n涨跌预测台已停止", flush=True)
