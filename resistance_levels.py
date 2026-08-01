# -*- coding: utf-8 -*-
"""动态压力位与顶部预估（做T/低吸高抛辅助）。

三层结构:
  1. 结构压力位: 日K筹码密集区(带时间衰减) + 摆动前高 + 均线 + 整数关口
     + 涨停价 + 日内高点/成交密集/分时均价, 聚合去重后按强度排序。
  2. 统计顶部: 在过去N个交易日中找与当前盘面(分笔特征)最相似的日子,
     统计它们在同一时点之后的真实剩余上冲(MFE)分布, 取分位数得到
     "当天顶部"; 同理用相似日之后3个交易时段的最高价得到"几日内顶部"。
  3. 环境调节: 大盘指数与板块代理的即时强弱决定采用哪一档分位 ——
     环境强用高分位(顶部看高一线), 环境弱用低分位(见好就收)。
  统计顶部再吸附到最近的结构压力位, 给出可执行的高抛区/低吸区。

口径与局限:
  - 日K用不复权价, 与分笔同口径; 窗口内若有除权, 远处筹码位会有偏移。
  - 回测模式的指数涨跌为当日收盘值(非盘中时点), 实时模式为当前值。
  - 分位数来自历史相似盘面的经验分布, 不是保证; 仅供研究, 非投资建议。

用法:
  python resistance_levels.py 300308                      # 实时
  python resistance_levels.py 300308 --date 2025-07-18 --cutoff 10:00
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from statistics import median

from predict_updown import (S, UA, day_ticks, flow_at, pick_board_proxies,
                            prefix, tdx_connect, tdx_industry_peers,
                            tencent_realtime, today_ticks, _tencent_kline_bars)
from continuation_analysis import (_market_ticks, _nearest, _quantile,
                                   snapshot_features)

INDEX_SYMBOLS = (("sh000001", "上证指数"), ("sz399006", "创业板指"),
                 ("sh000688", "科创50"))
# 环境档位 -> (高抛区下分位, 高抛区上分位)
QUANT_BANDS = {1: (0.65, 0.90), 0: (0.50, 0.80), -1: (0.35, 0.65)}
# 多日顶部相似度所用特征(均为时点可得, 无未来泄漏)
MULTIDAY_KEYS = ("self_pct", "gap_pct", "volume_ratio", "trend_15m",
                 "flow_strength", "pct5_prev", "pos60_prev", "dist_high20_prev")


# ───────────────────────── 基础工具 ─────────────────────────

def parse_bars(raw):
    """腾讯日K原始数组 -> [{date,open,close,high,low,vol}], 剔除坏行"""
    out = []
    for b in raw:
        try:
            row = {"date": b[0], "open": float(b[1]), "close": float(b[2]),
                   "high": float(b[3]), "low": float(b[4]), "vol": float(b[5])}
        except (TypeError, ValueError, IndexError):
            continue
        if row["high"] <= 0 or row["low"] <= 0:
            continue
        out.append(row)
    return out


def limit_up_price(code, prev_close):
    """涨停价: 创业板/科创板20%, 北交所30%, 其余10% (未处理ST)"""
    if code.startswith(("30", "68")):
        ratio = 0.20
    elif code.startswith(("8", "4")):
        ratio = 0.30
    else:
        ratio = 0.10
    return round(prev_close * (1 + ratio), 2)


def post_cutoff_extremes(ticks, cutoff, px):
    """cutoff之后的最高/最低价及出现时间(历史训练与回测复盘用)"""
    after = [t for t in _market_ticks(ticks) if t["time"] > cutoff]
    if not after or not px:
        return None
    hi = max(after, key=lambda t: t["price"])
    lo = min(after, key=lambda t: t["price"])
    return {"mfe": (hi["price"] / px - 1) * 100, "mae": (lo["price"] / px - 1) * 100,
            "high_px": hi["price"], "high_time": hi["time"],
            "low_px": lo["price"], "low_time": lo["time"]}


# ───────────────────────── 结构压力位 ─────────────────────────

def chip_profile(bars, bins=80, decay=0.97):
    """筹码分布近似: 每日成交量摊到当日高低区间, 按距今天数衰减"""
    if not bars:
        return []
    lo = min(b["low"] for b in bars)
    hi = max(b["high"] for b in bars)
    if hi <= lo:
        return []
    step = (hi - lo) / bins
    w = [0.0] * bins
    n = len(bars)
    for i, b in enumerate(bars):
        weight = b["vol"] * (decay ** (n - 1 - i))
        b0 = max(0, min(bins - 1, int((b["low"] - lo) / step)))
        b1 = max(0, min(bins - 1, int((b["high"] - lo) / step)))
        share = weight / (b1 - b0 + 1)
        for k in range(b0, b1 + 1):
            w[k] += share
    sm = [(w[max(0, k - 1)] + w[k] + w[min(bins - 1, k + 1)]) / 3
          for k in range(bins)]
    return [(lo + (k + 0.5) * step, sm[k]) for k in range(bins)]


def profile_peaks(profile):
    """筹码/成交分布的局部峰 -> [(price, weight)], 按权重降序"""
    peaks = []
    for k in range(1, len(profile) - 1):
        if profile[k][1] >= profile[k - 1][1] and profile[k][1] > profile[k + 1][1]:
            peaks.append(profile[k])
    return sorted(peaks, key=lambda p: -p[1])


def swing_points(bars, wing=2):
    """摆动高/低点(两侧wing天都更低/更高), 带触碰次数 -> (highs, lows)"""
    highs, lows = [], []
    for i in range(wing, len(bars) - wing):
        h, l = bars[i]["high"], bars[i]["low"]
        if all(h > bars[j]["high"] for j in range(i - wing, i + wing + 1) if j != i):
            touches = sum(1 for b in bars if abs(b["high"] / h - 1) <= 0.004)
            highs.append({"price": h, "date": bars[i]["date"], "touches": touches})
        if all(l < bars[j]["low"] for j in range(i - wing, i + wing + 1) if j != i):
            touches = sum(1 for b in bars if abs(b["low"] / l - 1) <= 0.004)
            lows.append({"price": l, "date": bars[i]["date"], "touches": touches})
    return highs, lows


def round_number_levels(px, hi_cap, lo_cap):
    """整数关口: 价位越高步长越大"""
    step = 10.0 if px >= 100 else (5.0 if px >= 20 else 1.0)
    out, v = [], (int(lo_cap / step)) * step
    while v <= hi_cap:
        if lo_cap <= v <= hi_cap and abs(v / px - 1) > 1e-4:
            out.append(round(v, 2))
        v += step
    return out


def _cluster_levels(cands, tol=0.006):
    """相邻(0.6%内)的候选位按强度加权合并"""
    cands = sorted(cands, key=lambda x: x["price"])
    merged = []
    for lv in cands:
        if merged and abs(lv["price"] / merged[-1]["price"] - 1) <= tol:
            m = merged[-1]
            total = m["strength"] + lv["strength"]
            m["price"] = round((m["price"] * m["strength"] +
                                lv["price"] * lv["strength"]) / total, 2)
            m["strength"] = round(total, 1)
            m["tags"] = sorted(set(m["tags"]) | set(lv["tags"]))
        else:
            merged.append({"price": round(lv["price"], 2),
                           "strength": round(lv["strength"], 1),
                           "tags": sorted(set(lv["tags"]))})
    return merged


def structural_levels(hist_bars, px, prev_close, code, intraday=None):
    """汇总目标日之前的日K与当日盘中信息 -> (压力位列表, 支撑位列表)

    intraday: {"high","low","vwap","open","peaks":[(price,weight)..]} 可缺省
    """
    limit_up = limit_up_price(code, prev_close)
    win = hist_bars[-120:]
    cands = []

    def add(price, strength, tag):
        if price and price > 0:
            cands.append({"price": price, "strength": strength, "tags": [tag]})

    # 筹码密集区(近120日, 时间衰减)
    profile = chip_profile(win)
    if profile:
        wmax = max(p[1] for p in profile) or 1.0
        for price, weight in profile_peaks(profile)[:6]:
            add(price, 1 + 3 * weight / wmax, "筹码密集")
    # 摆动前高/前低
    highs, lows = swing_points(win)
    for s in sorted(highs, key=lambda x: -x["touches"])[:6]:
        add(s["price"], 2 + 0.5 * s["touches"], f"前高{s['date'][5:]}")
    for s in sorted(lows, key=lambda x: -x["touches"])[:6]:
        add(s["price"], 2 + 0.5 * s["touches"], f"前低{s['date'][5:]}")
    # 关键日K价位
    if win:
        add(max(b["high"] for b in win[-20:]), 2, "20日高")
        add(min(b["low"] for b in win[-20:]), 2, "20日低")
        add(max(b["high"] for b in win[-60:]), 2, "60日高")
        add(win[-1]["high"], 2, "昨日高点")
        add(win[-1]["low"], 2, "昨日低点")
    add(prev_close, 1.5, "昨收")
    # 均线(不复权收盘)
    closes = [b["close"] for b in hist_bars]
    for n in (5, 10, 20, 30, 60):
        if len(closes) >= n:
            add(sum(closes[-n:]) / n, 1.5, f"MA{n}")
    # 整数关口与涨停价
    for v in round_number_levels(px, limit_up, px * 0.88):
        add(v, 1, "整数关口")
    add(limit_up, 3, "涨停价")
    # 当日盘中
    if intraday:
        add(intraday.get("high"), 2, "日内高点")
        add(intraday.get("low"), 2, "日内低点")
        add(intraday.get("vwap"), 1.5, "分时均价")
        add(intraday.get("open"), 1, "今开")
        for price, _w in (intraday.get("peaks") or [])[:2]:
            add(price, 1.5, "日内密集")

    merged = _cluster_levels(cands)
    res = [dict(m, dist_pct=round((m["price"] / px - 1) * 100, 2))
           for m in merged if px < m["price"] <= max(limit_up, px * 1.15)]
    sup = [dict(m, dist_pct=round((m["price"] / px - 1) * 100, 2))
           for m in merged if px * 0.85 <= m["price"] < px]
    res.sort(key=lambda x: x["price"])
    sup.sort(key=lambda x: -x["price"])
    return res[:6], sup[:5], limit_up


def snap_to_level(price, levels, tol=0.006):
    """统计顶部吸附到最近结构位(0.6%内), 便于挂单"""
    best = None
    for lv in levels:
        d = abs(lv["price"] / price - 1)
        if d <= tol and (best is None or d < best[0]):
            best = (d, lv)
    return best[1] if best else None


# ───────────────────────── 环境与统计顶部 ─────────────────────────

def index_snapshot(date=None):
    """大盘涨跌: 实时=当前值, 回测=当日收盘值"""
    out = {}
    try:
        if date:
            for sym, nm in INDEX_SYMBOLS:
                u = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                     f"?param={sym},day,,,400,")
                d = S.get(u, headers={"User-Agent": UA}, timeout=15).json()["data"][sym]
                bars = d.get("day") or d.get("qfqday")
                closes = {b[0]: float(b[2]) for b in bars}
                ds = sorted(closes)
                if date in closes and ds.index(date) > 0:
                    prev = closes[ds[ds.index(date) - 1]]
                    out[nm] = round((closes[date] / prev - 1) * 100, 2)
        else:
            r = S.get("https://qt.gtimg.cn/q=" + ",".join(s for s, _ in INDEX_SYMBOLS),
                      headers={"User-Agent": UA}, timeout=10)
            for line in r.content.decode("gbk", "ignore").split(";"):
                if '"' not in line:
                    continue
                v = line.split('"')[1].split("~")
                if len(v) > 32:
                    key = line.split("=")[0]
                    for sym, nm in INDEX_SYMBOLS:
                        if sym[2:] in key:
                            out[nm] = float(v[32] or 0)
    except Exception:
        pass
    return out


def env_stance(board_pct, indices):
    """板块+大盘 -> 环境档位(-1弱/0中/+1强)与说明"""
    step, notes = 0, []
    if board_pct is not None:
        if board_pct >= 0.5:
            step += 1
            notes.append(f"板块偏强{board_pct:+.1f}%")
        elif board_pct <= -0.5:
            step -= 1
            notes.append(f"板块偏弱{board_pct:+.1f}%")
        else:
            notes.append(f"板块中性{board_pct:+.1f}%")
    idx_min = min(indices.values()) if indices else None
    if idx_min is not None:
        if idx_min <= -1.0:
            step -= 1
            notes.append("大盘明显走弱")
        elif idx_min >= 0.3:
            step += 1
            notes.append("大盘配合")
        else:
            notes.append("大盘中性")
    step = max(-1, min(1, step))
    stance = {1: "偏强(顶部看高一线)", 0: "中性", -1: "偏弱(见好就收)"}[step]
    return step, stance, "、".join(notes) or "环境数据缺失, 按中性处理"


def _nearest_by(rows, target, keys, limit=30):
    """按指定特征列做z标准化欧氏距离检索(通用版, 不动continuation口径)"""
    pool = [r for r in rows if all(k in r["features"] for k in keys)]
    if not pool:
        return []
    scales = {}
    for k in keys:
        vals = [r["features"][k] for r in pool]
        mean = sum(vals) / len(vals)
        scales[k] = max(0.2, (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5)
    for r in pool:
        r["mdist"] = (sum(((r["features"][k] - target[k]) / scales[k]) ** 2
                          for k in keys) / len(keys)) ** 0.5
    return sorted(pool, key=lambda r: r["mdist"])[:min(limit, len(pool))]


def _walk_forward_top(rows, limit=40):
    """走步验证: 只用当日之前的样本预测P50/P80剩余上冲, 检查覆盖率与误差"""
    tests = []
    start = max(20, len(rows) - limit)
    for i in range(start, len(rows)):
        hist = rows[:i]
        if len(hist) < 20:
            continue
        nb = _nearest(hist, rows[i]["features"], limit=30)
        mfes = [r["outcome"]["mfe"] for r in nb]
        q50, q80 = _quantile(mfes, 0.5), _quantile(mfes, 0.8)
        actual = rows[i]["outcome"]["mfe"]
        if q50 is not None and q80 is not None and actual is not None:
            tests.append((q50, q80, actual))
    if not tests:
        return None
    coverage = sum(a <= q80 for _, q80, a in tests) / len(tests)
    errs = sorted(abs(a - q50) for q50, _, a in tests)
    return {"n": len(tests), "coverage80": round(coverage, 2),
            "median_err50": round(errs[len(errs) // 2], 2)}


def _time_stats(times):
    """相似日顶部出现时间的分布"""
    ts = sorted(t for t in times if t)
    if not ts:
        return None
    return {"median": ts[len(ts) // 2][:5],
            "before_1130": round(sum(t <= "11:30" for t in ts) / len(ts), 2),
            "before_1400": round(sum(t <= "14:00" for t in ts) / len(ts), 2)}


# ───────────────────────── 暴涨日守卫 ─────────────────────────
# 卖飞主因是暴涨日(剩余上冲远超常态分位)。用可在时点观测的联动信号识别:
# 联动龙头(默认成交额最大的板块代理, 如300308→新易盛)同步大涨、放量抢筹、
# 强势新高。每个条件都用训练窗历史日校准"该条件下的暴涨频率", 不拍脑袋。

SURGE_CONDITIONS = (
    ("leader", "联动龙头涨幅≥+3%",
     lambda f: f.get("leader_pct", -99) >= 3.0),
    ("volume", "量比≥2且主力强度≥+3%",
     lambda f: f.get("volume_ratio", 0) >= 2.0 and f.get("flow_strength", -99) >= 3.0),
    ("breakout", "自身涨≥3%且贴着日内新高",
     lambda f: f.get("range_pos", 0) >= 95 and f.get("self_pct", -99) >= 3.0),
)


def surge_guard(rows, target, surge_thr, board_pct=None):
    """暴涨日守卫: 历史校准各联动条件下的暴涨率, 判断今天是否具备暴涨特征.

    暴涨日定义: cutoff后剩余上冲 >= surge_thr (训练窗MFE的P85, 下限2.5%).
    返回 conditions(含历史暴涨率)/active/cond_quantiles(同条件日MFE分位)."""
    pool = [r for r in rows if r["outcome"].get("mfe") is not None]
    if not pool:
        return None
    base_rate = sum(r["outcome"]["mfe"] >= surge_thr for r in pool) / len(pool)

    def rows_meeting(keys):
        preds = [p for k, _, p in SURGE_CONDITIONS if k in keys]
        return [r for r in pool if all(p(r["features"]) for p in preds)]

    conds, met_keys = [], []
    for key, name, pred in SURGE_CONDITIONS:
        subset = rows_meeting([key])
        rate = (sum(r["outcome"]["mfe"] >= surge_thr for r in subset) / len(subset)
                if len(subset) >= 8 else None)
        met = bool(pred(target))
        if met:
            met_keys.append(key)
        conds.append({"key": key, "name": name, "met": met, "n": len(subset),
                      "hist_rate": round(rate, 2) if rate is not None else None})
    board_met = board_pct is not None and board_pct >= 2.0
    conds.append({"key": "board", "name": "板块代理均涨≥+2%", "met": board_met,
                  "n": 0, "hist_rate": None})

    active = (len(met_keys) >= 2 or
              target.get("leader_pct", -99) >= 5.0 or
              ("leader" in met_keys and board_met))

    # 同条件日经验分布: 优先满足全部已触发条件的历史日, 不足8个退回
    # 历史暴涨率最高的单一已触发条件
    cond_rows = rows_meeting(met_keys) if met_keys else []
    met_calibrated = [c for c in conds if c["met"] and c["hist_rate"] is not None]
    if len(cond_rows) < 8 and met_calibrated:
        strongest = max(met_calibrated, key=lambda c: c["hist_rate"])
        cond_rows = rows_meeting([strongest["key"]])
    cq = None
    if len(cond_rows) >= 8:
        mfes = [r["outcome"]["mfe"] for r in cond_rows]
        cq = {"n": len(cond_rows), "q50": round(_quantile(mfes, 0.5), 2),
              "q80": round(_quantile(mfes, 0.8), 2),
              "q90": round(_quantile(mfes, 0.9), 2)}
    return {"thr_pct": round(surge_thr, 2), "base_rate": round(base_rate, 2),
            "conditions": conds, "active": bool(active),
            "surge_prob": max((c["hist_rate"] for c in met_calibrated),
                              default=None),
            "cond_quantiles": cq}


CRASH_CONDITIONS = (
    ("leader", "联动龙头跌幅≤-3%",
     lambda f: f.get("leader_pct", 99) <= -3.0),
    ("volume", "量比≥2且主力强度≤-3%",
     lambda f: f.get("volume_ratio", 0) >= 2.0 and f.get("flow_strength", 99) <= -3.0),
    ("breakdown", "自身跌≥3%且贴着日内新低",
     lambda f: f.get("range_pos", 100) <= 5 and f.get("self_pct", 99) <= -3.0),
)


def crash_guard(rows, target, crash_thr, board_pct=None):
    """暴跌日守卫(surge_guard的下行镜像): 识别不接飞刀的日子.

    暴跌日定义: cutoff后剩余下杀 <= crash_thr (训练窗MAE的P15, 上限-2.5%)."""
    pool = [r for r in rows if r["outcome"].get("mae") is not None]
    if not pool:
        return None
    base_rate = sum(r["outcome"]["mae"] <= crash_thr for r in pool) / len(pool)

    def rows_meeting(keys):
        preds = [p for k, _, p in CRASH_CONDITIONS if k in keys]
        return [r for r in pool if all(p(r["features"]) for p in preds)]

    conds, met_keys = [], []
    for key, name, pred in CRASH_CONDITIONS:
        subset = rows_meeting([key])
        rate = (sum(r["outcome"]["mae"] <= crash_thr for r in subset) / len(subset)
                if len(subset) >= 8 else None)
        met = bool(pred(target))
        if met:
            met_keys.append(key)
        conds.append({"key": key, "name": name, "met": met, "n": len(subset),
                      "hist_rate": round(rate, 2) if rate is not None else None})
    board_met = board_pct is not None and board_pct <= -2.0
    conds.append({"key": "board", "name": "板块代理均跌≤-2%", "met": board_met,
                  "n": 0, "hist_rate": None})

    active = (len(met_keys) >= 2 or
              target.get("leader_pct", 99) <= -5.0 or
              ("leader" in met_keys and board_met))

    cond_rows = rows_meeting(met_keys) if met_keys else []
    met_calibrated = [c for c in conds if c["met"] and c["hist_rate"] is not None]
    if len(cond_rows) < 8 and met_calibrated:
        strongest = max(met_calibrated, key=lambda c: c["hist_rate"])
        cond_rows = rows_meeting([strongest["key"]])
    cq = None
    if len(cond_rows) >= 8:
        maes = [r["outcome"]["mae"] for r in cond_rows]
        cq = {"n": len(cond_rows), "q50": round(_quantile(maes, 0.5), 2),
              "q20": round(_quantile(maes, 0.2), 2),
              "q10": round(_quantile(maes, 0.1), 2)}
    return {"thr_pct": round(crash_thr, 2), "base_rate": round(base_rate, 2),
            "conditions": conds, "active": bool(active),
            "crash_prob": max((c["hist_rate"] for c in met_calibrated),
                              default=None),
            "cond_quantiles": cq}


# ───────────────────────── 主流程 ─────────────────────────

def top_forecast(code, date=None, cutoff="10:00", train=240, leader=None,
                 progress=lambda msg: None):
    """动态压力位+当日/3日顶部预估, 返回可JSON序列化的dict.

    leader: 联动龙头代码(缺省自动取成交额最大的板块代理股)"""
    progress(f"[1/4] 拉取 {code} 日K...")
    raw_bars = parse_bars(_tencent_kline_bars(code, n=max(320, train + 70), fq=""))
    if len(raw_bars) < 80:
        raise ValueError("日K数据不足")
    dates = [b["date"] for b in raw_bars]
    api = tdx_connect()
    try:
        q = tencent_realtime([code]).get(code, {})
        if date:
            if date not in dates:
                raise ValueError(f"{date}不是可用交易日")
            ti = dates.index(date)
            if ti == 0:
                raise ValueError("缺少前一交易日数据")
            target_ticks = day_ticks(api, code, int(date.replace("-", "")))
            prev_close = raw_bars[ti - 1]["close"]
            display_date = date
            hist_bars = raw_bars[:ti]
        else:
            if not q or not q.get("last_close"):
                raise ValueError("实时行情或昨收获取失败")
            target_ticks = today_ticks(api, code)
            prev_close = q["last_close"]
            display_date = datetime.now().strftime("%Y-%m-%d")
            hist_bars = [b for b in raw_bars if b["date"] < display_date]
            ti = len(hist_bars)
        if not target_ticks:
            raise ValueError("未取得目标日分笔数据")

        target = snapshot_features(target_ticks, cutoff, prev_close)
        px = target["price"]

        # ── 板块代理与联动龙头 ──
        proxies, board_names = [], []
        try:
            _, peers = tdx_industry_peers(api, code)
            if peers:
                proxies, peer_quotes = pick_board_proxies(code, peers,
                                                          return_quotes=True)
                board_names = [peer_quotes[p]["name"] for p in proxies
                               if p in peer_quotes]
        except Exception:
            pass
        leader_code = leader or (proxies[0] if proxies else None)
        leader_name, leader_prev = None, {}
        if leader_code:
            try:
                lbars = parse_bars(_tencent_kline_bars(leader_code,
                                                       n=max(320, train + 70),
                                                       fq=""))
                leader_prev = {lbars[i]["date"]: lbars[i - 1]["close"]
                               for i in range(1, len(lbars))}
                lq = tencent_realtime([leader_code]).get(leader_code)
                leader_name = lq["name"] if lq else leader_code
                if not date and lq:
                    target["leader_pct"] = lq["pct"]
            except Exception:
                leader_code = None

        # ── 历史样本: 分笔特征 + 日K上下文 + 之后的真实极值 ──
        progress(f"[2/4] 构建 {train} 天相似盘面样本(首次较慢, 之后走缓存)...")
        prelim = []
        for i in range(max(6, ti - train), ti):
            d = dates[i]
            ticks = day_ticks(api, code, int(d.replace("-", "")))
            if not ticks:
                continue
            try:
                f = snapshot_features(ticks, cutoff, raw_bars[i - 1]["close"])
            except ValueError:
                continue
            if abs(f["gap_pct"]) > 22:   # 超涨跌停幅的缺口按除权处理
                continue
            w60 = raw_bars[max(0, i - 60):i]
            w20 = raw_bars[max(0, i - 20):i]
            if len(w20) < 10:
                continue
            lo60 = min(b["low"] for b in w60)
            hi60 = max(b["high"] for b in w60)
            f["pos60_prev"] = ((f["price"] - lo60) / (hi60 - lo60) * 100
                               if hi60 > lo60 else 50.0)
            f["dist_high20_prev"] = (f["price"] / max(b["high"] for b in w20) - 1) * 100
            f["pct5_prev"] = ((raw_bars[i - 1]["close"] / raw_bars[i - 6]["close"] - 1) * 100
                              if i >= 6 else 0.0)
            if leader_code and d in leader_prev:
                try:
                    lt = day_ticks(api, leader_code, int(d.replace("-", "")))
                    _, _, lpx = flow_at(lt, cutoff) if lt else (0, 0, None)
                    if lpx:
                        f["leader_pct"] = (lpx / leader_prev[d] - 1) * 100
                except Exception:
                    pass
            ex = post_cutoff_extremes(ticks, cutoff, f["price"])
            if not ex:
                continue
            fut = raw_bars[i + 1:i + 3]
            top3 = None
            # 当日剩余+之后两个交易日=3个交易时段; 后两日须严格早于目标日, 防前视
            if len(fut) == 2 and i + 2 < ti:
                top3 = (max([ex["high_px"]] + [b["high"] for b in fut])
                        / f["price"] - 1) * 100
            prelim.append({"date": d, "features": f,
                           "outcome": dict(ex, top3=top3)})
        if len(prelim) < 25:
            raise ValueError(f"历史有效样本不足({len(prelim)})")
        amounts = [r["features"]["amount"] for r in prelim]
        for i, r in enumerate(prelim):
            base = median(amounts[max(0, i - 20):i] or amounts[:max(1, i + 1)])
            r["features"]["volume_ratio"] = (r["features"]["amount"] / base
                                             if base else 1.0)

        # 目标日同款上下文特征
        w60 = hist_bars[-60:]
        w20 = hist_bars[-20:]
        lo60, hi60 = min(b["low"] for b in w60), max(b["high"] for b in w60)
        target["pos60_prev"] = ((px - lo60) / (hi60 - lo60) * 100
                                if hi60 > lo60 else 50.0)
        target["dist_high20_prev"] = (px / max(b["high"] for b in w20) - 1) * 100
        target["pct5_prev"] = ((hist_bars[-1]["close"] / hist_bars[-6]["close"] - 1) * 100
                               if len(hist_bars) >= 6 else 0.0)
        target_base = median(amounts[-20:])
        target["volume_ratio"] = target["amount"] / target_base if target_base else 1.0

        # 回测模式: 目标日龙头涨幅同样取cutoff时点值, 与训练口径一致
        if leader_code and date and date in leader_prev:
            try:
                lt = day_ticks(api, leader_code, int(date.replace("-", "")))
                _, _, lpx = flow_at(lt, cutoff) if lt else (0, 0, None)
                if lpx:
                    target["leader_pct"] = (lpx / leader_prev[date] - 1) * 100
            except Exception:
                pass

        # ── 环境: 板块代理 + 大盘指数 ──
        progress("[3/4] 采集板块/大盘环境...")
        board_pct = None
        try:
            if proxies:
                if date:
                    bps = []
                    for p in proxies:
                        pbars = parse_bars(_tencent_kline_bars(p, 400, ""))
                        pdates = [b["date"] for b in pbars]
                        if date in pdates and pdates.index(date) > 0:
                            ppc = pbars[pdates.index(date) - 1]["close"]
                            pt = day_ticks(api, p, int(date.replace("-", "")))
                            if pt and ppc:
                                _, _, ppx = flow_at(pt, cutoff)
                                if ppx:
                                    bps.append((ppx / ppc - 1) * 100)
                    board_pct = sum(bps) / len(bps) if bps else None
                else:
                    rq = tencent_realtime(proxies)
                    bps = [rq[p]["pct"] for p in proxies if p in rq]
                    board_pct = sum(bps) / len(bps) if bps else None
        except Exception:
            pass
    finally:
        api.disconnect()

    indices = index_snapshot(date)
    step, stance, env_note = env_stance(board_pct, indices)
    qlo, qhi = QUANT_BANDS[step]
    qop = (qlo + qhi) / 2

    # ── 暴涨/暴跌日守卫: 联动条件历史校准 ──
    all_mfes = [r["outcome"]["mfe"] for r in prelim]
    all_maes = [r["outcome"]["mae"] for r in prelim]
    surge = surge_guard(prelim, target, max(2.5, _quantile(all_mfes, 0.85)),
                        board_pct)
    crash = crash_guard(prelim, target, min(-2.5, _quantile(all_maes, 0.15)),
                        board_pct)
    leader_info = ({"code": leader_code, "name": leader_name or leader_code,
                    "pct_now": (round(target["leader_pct"], 2)
                                if "leader_pct" in target else None)}
                   if leader_code else None)
    if surge:
        surge["leader"] = leader_info
    if crash:
        crash["leader"] = leader_info

    # ── 结构压力位 ──
    intraday_ticks = [t for t in _market_ticks(target_ticks) if t["time"] <= cutoff]
    tick_profile = chip_profile(
        [{"low": t["price"], "high": t["price"], "vol": t["vol"]}
         for t in intraday_ticks], bins=40, decay=1.0)
    intraday_info = {"high": target["high"], "low": target["low"],
                     "vwap": target["vwap"], "open": target["open"],
                     "peaks": profile_peaks(tick_profile)}
    resistance, support, limit_up = structural_levels(
        hist_bars, px, prev_close, code, intraday_info)

    def price_at(pct):
        return round(min(px * (1 + pct / 100), limit_up), 2)

    # ── 当日顶部: 相似盘面剩余上冲分位 ──
    progress("[4/4] 相似盘面检索与分位估计...")
    intraday_est = None
    if cutoff < "14:50":
        analogs = _nearest(prelim, target, limit=30)
        mfes = [r["outcome"]["mfe"] for r in analogs]
        maes = [r["outcome"]["mae"] for r in analogs]
        sell_lo, sell_hi = _quantile(mfes, qlo), _quantile(mfes, qhi)
        op = _quantile(mfes, qop)
        dip_deep, dip_mid = _quantile(maes, 0.2), _quantile(maes, 0.5)
        snap = snap_to_level(price_at(op), resistance)
        intraday_est = {
            "q50": {"pct": round(_quantile(mfes, 0.5), 2),
                    "price": price_at(_quantile(mfes, 0.5))},
            "q80": {"pct": round(_quantile(mfes, 0.8), 2),
                    "price": price_at(_quantile(mfes, 0.8))},
            "op": {"pct": round(op, 2), "price": price_at(op),
                   "quantile": qop},
            "sell_zone": [price_at(sell_lo), price_at(sell_hi)],
            "dip_zone": [round(px * (1 + dip_deep / 100), 2),
                         round(px * (1 + dip_mid / 100), 2)],
            "capped_by_limit": px * (1 + _quantile(mfes, qhi) / 100) > limit_up,
            "snap": snap,
            "high_time": _time_stats([r["outcome"]["high_time"] for r in analogs]),
            "n_analogs": len(analogs),
            "validation": _walk_forward_top(prelim),
            "actual": None,
        }
        # 暴涨特征触发且有足够同条件样本 → 高抛区上移到同条件分位
        # (与常规区间取高者, 保证守卫只上移不下移)
        intraday_est["surge_shift"] = False
        if surge and surge["active"] and surge["cond_quantiles"]:
            scq = surge["cond_quantiles"]
            base_zone = intraday_est["sell_zone"]
            intraday_est["base_sell_zone"] = base_zone
            intraday_est["sell_zone"] = [
                max(base_zone[0], price_at(scq["q50"])),
                max(base_zone[1], price_at(scq["q80"]))]
            op_price = intraday_est["sell_zone"][1]
            intraday_est["op"] = {"pct": round((op_price / px - 1) * 100, 2),
                                  "price": op_price, "quantile": 0.8}
            intraday_est["snap"] = snap_to_level(op_price, resistance)
            intraday_est["surge_shift"] = True
        # 暴跌特征触发 → 低吸区下移到同条件分位(只下移不上移, 不接飞刀)
        intraday_est["crash_shift"] = False
        if crash and crash["active"] and crash["cond_quantiles"]:
            ccq = crash["cond_quantiles"]
            base_dip = intraday_est["dip_zone"]
            intraday_est["base_dip_zone"] = base_dip
            intraday_est["dip_zone"] = [
                min(base_dip[0], round(px * (1 + ccq["q20"] / 100), 2)),
                min(base_dip[1], round(px * (1 + ccq["q50"] / 100), 2))]
            intraday_est["crash_shift"] = True
        advice = ["暴涨日特征: 只减不清(单次T仓≤1/3), 高抛区已上移; 跌破分时均价线再清"
                  if intraday_est["surge_shift"] else
                  "触及高抛区分批出T仓, 回落至低吸区或收盘接回"]
        if intraday_est["crash_shift"]:
            advice.append("暴跌日特征: 不接飞刀, 低吸区已下移; 收复分时均价线再接")
        elif crash and crash["active"]:
            advice.append("暴跌日特征触发: 暂缓低吸, 等企稳信号")
        # 低吸联动否决(单条件即提示): 回测显示触及低吸区时龙头/板块负联动
        # 是接飞刀的主要亏损来源, 守卫未满2条件也应暂缓
        intraday_est["dip_veto"] = bool(crash and any(
            c["met"] for c in crash["conditions"] if c["key"] in ("leader", "board")))
        if intraday_est["dip_veto"] and not (crash and crash["active"]):
            advice.append("联动否决在线(龙头/板块走弱): 触及低吸区暂不接, 待联动转正")
        intraday_est["t_advice"] = " | ".join(advice)
        if date:
            ex = post_cutoff_extremes(target_ticks, cutoff, px)
            if ex:
                intraday_est["actual"] = {
                    "high": ex["high_px"], "pct": round(ex["mfe"], 2),
                    "time": ex["high_time"][:5],
                    # 实际最高触及高抛区下沿 = 在区间挂单可成交
                    "hit_zone": ex["high_px"] >= intraday_est["sell_zone"][0],
                }

    # ── 3日顶部: 日K上下文相似 ──
    pool = [r for r in prelim if r["outcome"]["top3"] is not None]
    manalogs = _nearest_by(pool, target, MULTIDAY_KEYS, limit=30)
    multiday_est = None
    if manalogs:
        tops = [r["outcome"]["top3"] for r in manalogs]
        m_op = _quantile(tops, qop)
        m50, m80 = _quantile(tops, 0.5), _quantile(tops, 0.8)
        msnap = snap_to_level(round(px * (1 + m_op / 100), 2), resistance, tol=0.01)
        multiday_est = {
            "horizon": "当日剩余+后2个交易日",
            "q50": {"pct": round(m50, 2), "price": round(px * (1 + m50 / 100), 2)},
            "q80": {"pct": round(m80, 2), "price": round(px * (1 + m80 / 100), 2)},
            "op": {"pct": round(m_op, 2), "price": round(px * (1 + m_op / 100), 2),
                   "quantile": qop},
            "snap": msnap, "n_analogs": len(manalogs), "actual": None,
        }
        if date:
            ex = post_cutoff_extremes(target_ticks, cutoff, px)
            fut = raw_bars[ti + 1:ti + 3]
            if ex and len(fut) == 2:
                actual_top = max([ex["high_px"]] + [b["high"] for b in fut])
                multiday_est["actual"] = {
                    "high": actual_top,
                    "pct": round((actual_top / px - 1) * 100, 2)}

    warnings = []
    if surge and surge["active"] and not surge["cond_quantiles"]:
        warnings.append("暴涨日特征触发但历史同条件样本不足, 高抛区未上移 — 建议只减不清")
    if crash and crash["active"] and not crash["cond_quantiles"]:
        warnings.append("暴跌日特征触发但历史同条件样本不足, 低吸区未下移 — 建议暂缓低吸")
    if px >= limit_up * 0.995:
        warnings.append("已贴近涨停价, 当日上方空间受板制约, 分位估计参考多日口径")
    v = intraday_est and intraday_est.get("validation")
    if v and (v["coverage80"] < 0.6 or v["coverage80"] > 0.97):
        warnings.append(f"走步验证P80覆盖率{v['coverage80']:.0%}偏离80%, 分位可信度打折")
    if target["volume_ratio"] >= 2.5:
        warnings.append("同时点量比≥2.5, 盘面异动, 历史相似样本代表性下降")

    return {
        "code": code, "name": q.get("name", code) if q else code,
        "date": display_date, "cutoff": cutoff,
        "price": px, "prev_close": prev_close, "self_pct": round(target["self_pct"], 2),
        "vwap": round(target["vwap"], 2), "limit_up": limit_up,
        "env": {"indices": indices, "board_pct": (round(board_pct, 2)
                                                  if board_pct is not None else None),
                "board_proxies": board_names, "step": step,
                "stance": stance, "note": env_note,
                "band": [qlo, qhi]},
        "resistance": resistance, "support": support,
        "intraday": intraday_est, "multiday": multiday_est,
        "surge": surge, "crash": crash,
        "samples": len(prelim), "warnings": warnings,
        "method": ("结构压力位(筹码密集/前高/均线/整数关/涨停/日内) × "
                   "历史相似盘面剩余上冲分位 × 大盘板块环境档位调节; "
                   "分位为经验分布, 仅供研究"),
    }


def _fmt_level(lv):
    return (f"{lv['price']:>8.2f}  {lv['dist_pct']:+5.1f}%  强度{lv['strength']:<4}"
            f" {'/'.join(lv['tags'])}")


def main():
    ap = argparse.ArgumentParser(description="动态压力位与顶部预估")
    ap.add_argument("code", help="6位股票代码, 如 300308")
    ap.add_argument("--date", help="回看某交易日 YYYY-MM-DD (缺省=今天实时)")
    ap.add_argument("--cutoff", default="10:00", help="观察时点 HH:MM")
    ap.add_argument("--train", type=int, default=240, help="历史样本天数")
    ap.add_argument("--leader", help="联动龙头代码(缺省=成交额最大的板块代理)")
    args = ap.parse_args()
    try:
        r = top_forecast(args.code, args.date, args.cutoff, args.train,
                         args.leader, progress=print)
    except ValueError as e:
        sys.exit(str(e))
    print("=" * 60)
    print(f"  {r['name']}({r['code']}) {r['date']} {r['cutoff']}时点  "
          f"现价 {r['price']:.2f} ({r['self_pct']:+.2f}%)  涨停 {r['limit_up']:.2f}")
    e = r["env"]
    idx = " ".join(f"{k}{v:+.2f}%" for k, v in e["indices"].items())
    print(f"  环境: {e['stance']} | {e['note']} | {idx}")
    if e["board_proxies"]:
        pct_txt = f" {e['board_pct']:+.2f}%" if e["board_pct"] is not None else ""
        print(f"  板块代理: {'/'.join(e['board_proxies'])}{pct_txt}")
    print("-" * 60)
    print("  上方压力位:")
    for lv in r["resistance"]:
        print("   " + _fmt_level(lv))
    print("  下方支撑位:")
    for lv in r["support"]:
        print("   " + _fmt_level(lv))
    print("-" * 60)
    it = r["intraday"]
    if it:
        print(f"  当日顶部(相似盘面{it['n_analogs']}个): "
              f"P50 {it['q50']['price']:.2f} / P80 {it['q80']['price']:.2f}"
              f"{' [触涨停]' if it['capped_by_limit'] else ''}")
        print(f"  操作顶部(P{int(it['op']['quantile']*100)}): {it['op']['price']:.2f}"
              + (f"  ≈压力位 {it['snap']['price']:.2f}"
                 f"({'/'.join(it['snap']['tags'])})" if it["snap"] else ""))
        print(f"  高抛区 {it['sell_zone'][0]:.2f} ~ {it['sell_zone'][1]:.2f} | "
              f"低吸区 {it['dip_zone'][0]:.2f} ~ {it['dip_zone'][1]:.2f}")
        if it["high_time"]:
            t = it["high_time"]
            print(f"  顶部时间: 相似日中位 {t['median']}, "
                  f"{t['before_1130']:.0%}在11:30前, {t['before_1400']:.0%}在14:00前")
        if it["validation"]:
            v = it["validation"]
            print(f"  走步验证{v['n']}天: P80覆盖率{v['coverage80']:.0%}, "
                  f"P50中位误差{v['median_err50']}%")
        if it["actual"]:
            a = it["actual"]
            print(f"  回测实际: 之后最高 {a['high']:.2f}({a['pct']:+.2f}%) "
                  f"于{a['time']} {'✓落入高抛区' if a['hit_zone'] else '✗未及高抛区'}")
    else:
        print("  当日顶部: 时点太晚, 剩余时间不足, 仅参考多日与压力位")
    sg = r.get("surge")
    if sg and sg.get("leader") and sg["leader"]["pct_now"] is not None:
        lead = sg["leader"]
        lc = next((c for c in sg["conditions"] if c["key"] == "leader"), None)
        rate_txt = (f" (历史其≥+3%时本股暴涨率{lc['hist_rate']:.0%}, "
                    f"全样本基础暴涨率{sg['base_rate']:.0%}, 暴涨=上冲≥{sg['thr_pct']}%)"
                    if lc and lc["hist_rate"] is not None else "")
        print(f"  联动龙头 {lead['name']}({lead['code']}): {lead['pct_now']:+.2f}%{rate_txt}")
    if sg and sg["active"]:
        met = [c["name"] for c in sg["conditions"] if c["met"]]
        print(f"  ⚡ 暴涨日特征触发: {'; '.join(met)}")
        if sg["cond_quantiles"]:
            c = sg["cond_quantiles"]
            print(f"     同条件{c['n']}日上冲分布: P50 {c['q50']:+.2f}% / "
                  f"P80 {c['q80']:+.2f}% / P90 {c['q90']:+.2f}% — 高抛区已上移, 只减不清")
    cg = r.get("crash")
    if cg and cg["active"]:
        met = [c["name"] for c in cg["conditions"] if c["met"]]
        print(f"  🔻 暴跌日特征触发: {'; '.join(met)}")
        if cg["cond_quantiles"]:
            c = cg["cond_quantiles"]
            print(f"     同条件{c['n']}日下杀分布: P50 {c['q50']:+.2f}% / "
                  f"P20 {c['q20']:+.2f}% / P10 {c['q10']:+.2f}% — 低吸区已下移, 不接飞刀")
    md = r["multiday"]
    if md:
        print(f"  3日内顶部({md['horizon']}): "
              f"P50 {md['q50']['price']:.2f} / P80 {md['q80']['price']:.2f}"
              + (f"  ≈压力位 {md['snap']['price']:.2f}"
                 f"({'/'.join(md['snap']['tags'])})" if md["snap"] else ""))
        if md["actual"]:
            print(f"  回测实际3日最高: {md['actual']['high']:.2f}"
                  f"({md['actual']['pct']:+.2f}%)")
    for w in r["warnings"]:
        print(f"  ⚠️ {w}")
    print("=" * 60)
    print(f"  样本{r['samples']}天 | {r['method']}")


if __name__ == "__main__":
    main()
