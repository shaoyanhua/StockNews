# -*- coding: utf-8 -*-
"""盘中上涨延续分析。

用指定时点之前的分笔构造量价特征，再从该股票此前交易日中寻找最相似的
盘面，给出经验概率和风险区间。规则评分用于解释当前盘面，经验概率来自
历史相似样本；二者有意分开，避免把人工打分当成统计概率。
"""
from __future__ import annotations

import math
from datetime import datetime
from statistics import median

from predict_updown import (day_ticks, flow_at, prefix, tdx_connect,
                            tencent_kline, tencent_realtime, today_ticks)


FEATURE_NAMES = (
    "gap_pct", "path_pct", "vwap_dist", "retreat_from_high",
    "range_pos", "trend_15m", "one_min_slope", "vwap_slope_10m",
    "flow_strength", "flow_change_15m", "up_down_volume_ratio",
    "volume_ratio", "structure",
)


def _minutes(t):
    h, m = (int(x) for x in t[:5].split(":"))
    return h * 60 + m


def _trading_add(t, delta):
    """A股交易时钟增加若干分钟，自动跨越午休。"""
    n = _minutes(t)
    if n <= 11 * 60 + 30:
        trading_index = n - (9 * 60 + 30)
    else:
        trading_index = 120 + n - 13 * 60
    trading_index = max(0, min(240, trading_index + delta))
    if trading_index <= 120:
        n = 9 * 60 + 30 + trading_index
    else:
        n = 13 * 60 + trading_index - 120
    return f"{n // 60:02d}:{n % 60:02d}"


def _market_ticks(ticks):
    return [t for t in ticks if "09:25" <= t["time"] <= "15:00" and t["vol"] > 0]


def _upto(ticks, cutoff):
    return [t for t in _market_ticks(ticks) if t["time"] <= cutoff]


def _price_at(ticks, at):
    xs = [t for t in _market_ticks(ticks) if t["time"] <= at]
    return xs[-1]["price"] if xs else None


def _minute_bars(ticks, cutoff, width=5):
    bars = {}
    for t in _upto(ticks, cutoff):
        n = _minutes(t["time"])
        # 09:25集合竞价单独放入第一个桶，连续竞价从09:30开始。
        bucket = n if n < 570 else (n // width) * width
        b = bars.setdefault(bucket, {"high": t["price"], "low": t["price"],
                                     "close": t["price"], "amount": 0.0})
        b["high"] = max(b["high"], t["price"])
        b["low"] = min(b["low"], t["price"])
        b["close"] = t["price"]
        b["amount"] += t["price"] * t["vol"] * 100
    return [bars[k] for k in sorted(bars)]


def _five_minute_bars(ticks, cutoff):
    return _minute_bars(ticks, cutoff, 5)


def snapshot_features(ticks, cutoff, prev_close, volume_ratio=1.0):
    """仅使用cutoff之前的数据，生成无未来泄漏的盘中特征。"""
    xs = _upto(ticks, cutoff)
    if not xs or not prev_close:
        raise ValueError(f"截至{cutoff}没有有效分笔或昨收缺失")
    open_px = xs[0]["price"]
    px = xs[-1]["price"]
    high = max(t["price"] for t in xs)
    low = min(t["price"] for t in xs)
    amount = sum(t["price"] * t["vol"] * 100 for t in xs)
    volume = sum(t["vol"] for t in xs)
    vwap = sum(t["price"] * t["vol"] for t in xs) / volume if volume else px
    _, main, _ = flow_at(xs, cutoff)
    flow_strength = main * 1e8 / amount * 100 if amount else 0.0

    t10 = _trading_add(cutoff, -10)
    earlier10 = [t for t in xs if t["time"] <= t10]
    vol10 = sum(t["vol"] for t in earlier10)
    vwap10 = (sum(t["price"] * t["vol"] for t in earlier10) / vol10
              if vol10 else vwap)
    t15 = _trading_add(cutoff, -15)
    earlier15 = [t for t in xs if t["time"] <= t15]
    amount15 = sum(t["price"] * t["vol"] * 100 for t in earlier15)
    _, main15, _ = flow_at(earlier15, t15)
    strength15 = main15 * 1e8 / amount15 * 100 if amount15 else 0.0

    before_15 = _price_at(xs, _trading_add(cutoff, -15))
    if before_15 is None:
        before_15 = open_px
    bars = _five_minute_bars(xs, cutoff)
    bars1 = _minute_bars(xs, cutoff, 1)
    structure = 0
    if len(bars) >= 3:
        last = bars[-3:]
        if last[0]["high"] < last[1]["high"] < last[2]["high"] and \
                last[0]["low"] < last[1]["low"] < last[2]["low"]:
            structure = 1
        elif last[0]["high"] > last[1]["high"] > last[2]["high"] and \
                last[0]["low"] > last[1]["low"] > last[2]["low"]:
            structure = -1

    recent_bars = bars[-7:]
    up_amount = down_amount = 0.0
    for prev, cur in zip(recent_bars, recent_bars[1:]):
        if cur["close"] >= prev["close"]:
            up_amount += cur["amount"]
        else:
            down_amount += cur["amount"]
    up_down_volume_ratio = (up_amount / down_amount if down_amount else
                            (3.0 if up_amount else 1.0))
    last_two_below_vwap = (len(bars) >= 2 and
                           all(b["close"] < vwap for b in bars[-2:]))
    one_min_slope = 0.0
    if len(bars1) >= 6 and bars1[-6]["close"]:
        one_min_slope = (bars1[-1]["close"] / bars1[-6]["close"] - 1) * 100

    range_pos = 50.0 if high == low else (px - low) / (high - low) * 100
    recent = [t["price"] for t in xs if t["time"] >= _trading_add(cutoff, -15)]
    last_15_low = min(recent) if recent else low
    return {
        "open": open_px, "price": px, "high": high, "low": low,
        "vwap": vwap, "amount": amount, "main": main,
        "gap_pct": (open_px / prev_close - 1) * 100,
        "path_pct": (px / open_px - 1) * 100,
        "self_pct": (px / prev_close - 1) * 100,
        "vwap_dist": (px / vwap - 1) * 100,
        "retreat_from_high": (px / high - 1) * 100,
        "rebound_from_low": (px / low - 1) * 100,
        "range_pos": range_pos,
        "trend_15m": (px / before_15 - 1) * 100,
        "one_min_slope": one_min_slope,
        "vwap_slope_10m": (vwap / vwap10 - 1) * 100,
        "flow_strength": flow_strength,
        "flow_change_15m": flow_strength - strength15,
        "main_change_15m": main - main15,
        "up_down_volume_ratio": min(3.0, up_down_volume_ratio),
        "volume_ratio": volume_ratio,
        "structure": structure,
        "last_two_below_vwap": last_two_below_vwap,
        "bars_1m": len(bars1), "bars_5m": len(bars),
        "last_15_low": last_15_low,
    }


def future_outcome(ticks, cutoff, px, up_target=1.5, down_stop=1.0):
    """计算cutoff之后的真实路径；用于历史训练及回测展示。"""
    after = [t for t in _market_ticks(ticks) if t["time"] > cutoff]
    if not after:
        return None
    t30 = _trading_add(cutoff, 30)
    p30 = _price_at(ticks, t30)
    pam = _price_at(ticks, "11:30") if cutoff < "11:30" else None
    pclose = _price_at(ticks, "15:00")
    hit, hit_time = "neither", None
    for t in after:
        r = (t["price"] / px - 1) * 100
        if r >= up_target:
            hit, hit_time = "up", t["time"]
            break
        if r <= -down_stop:
            hit, hit_time = "down", t["time"]
            break
    prices = [t["price"] for t in after]
    return {
        "ret_30m": (p30 / px - 1) * 100 if p30 else None,
        "ret_am": (pam / px - 1) * 100 if pam else None,
        "ret_close": (pclose / px - 1) * 100 if pclose else None,
        "mfe": (max(prices) / px - 1) * 100,
        "mae": (min(prices) / px - 1) * 100,
        "first_hit": hit, "first_hit_time": hit_time,
    }


def _quantile(values, q):
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    p = (len(xs) - 1) * q
    lo, hi = math.floor(p), math.ceil(p)
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (p - lo)


def _prob(rows, key):
    vals = [r["outcome"][key] for r in rows if r["outcome"].get(key) is not None]
    return round((sum(v > 0 for v in vals) + 1) / (len(vals) + 2), 3) if vals else None


def _nearest(rows, target, limit=25):
    if not rows:
        return []
    means, scales = {}, {}
    for k in FEATURE_NAMES:
        vals = [r["features"][k] for r in rows]
        means[k] = sum(vals) / len(vals)
        scales[k] = max(0.2, math.sqrt(sum((v - means[k]) ** 2 for v in vals) / len(vals)))
    for r in rows:
        r["distance"] = math.sqrt(sum(
            ((r["features"][k] - target[k]) / scales[k]) ** 2 for k in FEATURE_NAMES
        ) / len(FEATURE_NAMES))
    return sorted(rows, key=lambda r: r["distance"])[:min(limit, len(rows))]


def _walk_forward_validation(rows, limit=40):
    """仅用每个验证日之前的数据，检查收盘方向概率，避免样本内准确率。"""
    tests = []
    start = max(15, len(rows) - limit)
    for i in range(start, len(rows)):
        history = rows[:i]
        if len(history) < 15:
            continue
        neighbors = _nearest(history, rows[i]["features"], limit=25)
        p = _prob(neighbors, "ret_close")
        actual = rows[i]["outcome"].get("ret_close")
        if p is not None and actual is not None:
            tests.append((p, 1 if actual > 0 else 0))
    if not tests:
        return None
    accuracy = sum((p >= 0.5) == bool(y) for p, y in tests) / len(tests)
    brier = sum((p - y) ** 2 for p, y in tests) / len(tests)
    calibration_gap = abs(sum(p for p, _ in tests) / len(tests) -
                          sum(y for _, y in tests) / len(tests))
    return {"n": len(tests), "accuracy": round(accuracy, 3),
            "brier": round(brier, 3), "calibration_gap": round(calibration_gap, 3)}


def _rule_score(f, board_pct=None, indices=None, resonance_score=None,
                branch_up_count=None, depth_imbalance=None):
    score, reasons = 50, []

    def apply(points, text):
        nonlocal score
        score += points
        reasons.append({"points": points, "text": text})

    if f["vwap_dist"] >= 0.2:
        apply(12, "股价站在分时均价线上")
    elif f["vwap_dist"] <= -0.2:
        apply(-12, "股价位于分时均价线下")
    if f["vwap_slope_10m"] > 0.05:
        apply(8, "分时均价线持续向上")
    elif f["vwap_slope_10m"] < -0.05:
        apply(-8, "分时均价线向下")
    if f["structure"] > 0:
        apply(15, "最近三根5分钟线高点、低点抬高")
    elif f["structure"] < 0:
        apply(-15, "最近三根5分钟线高点、低点降低")
    if f["trend_15m"] >= 0.35:
        apply(8, "最近15分钟保持上行")
    elif f["trend_15m"] <= -0.35:
        apply(-8, "最近15分钟明显走弱")
    if f["flow_change_15m"] >= 0.5:
        apply(10, "最近15分钟主力净流入强度扩大")
    elif f["flow_change_15m"] <= -0.5:
        apply(-10, "最近15分钟主力资金趋势转弱")
    if f["up_down_volume_ratio"] >= 1.2:
        apply(15, "最近5分钟上涨量大于下跌量")
    elif f["up_down_volume_ratio"] <= 0.8:
        apply(-10, "最近5分钟下跌量占优")
    if f["volume_ratio"] >= 1.1 and f["trend_15m"] > 0:
        apply(5, "较近期同一时点放量上行")
    if f["range_pos"] >= 90 and f["retreat_from_high"] > -0.3:
        apply(10, "突破并站稳早盘高位")
    if f["gap_pct"] >= 5 and f["path_pct"] < 0:
        apply(-15, "高开超过5%后涨幅收窄")
    if f["volume_ratio"] >= 1.2 and f["retreat_from_high"] <= -1:
        apply(-15, "放量冲高后不能维持新高")
    if f["last_two_below_vwap"]:
        apply(-20, "连续两根5分钟线收在均价线下")
    if board_pct is not None:
        if board_pct >= 0.5:
            apply(8, "行业代理板块同步走强")
        elif board_pct <= -0.5:
            apply(-8, "行业代理板块走弱")
        if f["self_pct"] > 1 and board_pct <= 0:
            apply(-15, "个股上涨但板块未跟随，发生背离")
    if indices:
        vals = list(indices.values())
        if vals and min(vals) > -0.5:
            apply(10, "创业板、科创50等市场环境稳定")
        elif vals and min(vals) <= -1:
            apply(-8, "市场指数明显走弱")
    if branch_up_count is not None:
        if branch_up_count >= 3:
            apply(15, "光模块、PCB、AI芯片三个分支共振")
        elif branch_up_count <= 1:
            apply(-15, "AI硬件分支缺乏共振")
    if resonance_score is not None:
        if resonance_score >= 8:
            apply(8, "四股处于强共振")
        elif resonance_score < 2:
            apply(-8, "四股共振失效")
    if depth_imbalance is not None:
        if depth_imbalance >= 20:
            apply(5, "五档买盘厚度暂时占优")
        elif depth_imbalance <= -20:
            apply(-5, "五档卖盘厚度暂时占优")
    return max(0, min(100, score)), sorted(reasons, key=lambda x: abs(x["points"]), reverse=True)


def _decision(score):
    if score >= 75:
        return "强延续", "保留底仓，不急卖；T仓最多分批卖出0%—20%"
    if score >= 60:
        return "偏强延续", "保留底仓，可小幅卖出T仓，等待回踩确认"
    if score >= 45:
        return "多空分歧", "不追涨；仅用小仓位做T，等待方向确认"
    if score >= 30:
        return "冲高回落风险较高", "分批卖出30%—50%的T仓，底仓单独判断"
    return "延续失败", "停止追涨并降低机动仓；反抽不过均价线不接回"


def continuation_run(code, date=None, cutoff="10:00", train=240,
                     board_pct=None, indices=None, resonance_score=None,
                     branch_up_count=None):
    """运行单股延续分析，返回可直接JSON序列化的结果。"""
    raw = tencent_kline(code, n=max(320, train + 50), fq="")
    dates = [d for d, _ in raw]
    closes = dict(raw)
    api = tdx_connect()
    try:
        if date:
            if date not in dates:
                raise ValueError(f"{date}不是可用交易日")
            target_i = dates.index(date)
            if target_i == 0:
                raise ValueError("缺少前一交易日收盘价")
            target_ticks = day_ticks(api, code, int(date.replace("-", "")))
            prev_close = closes[dates[target_i - 1]]
            train_dates = dates[max(1, target_i - train):target_i]
            display_date = date
        else:
            q = tencent_realtime([code]).get(code)
            if not q or not q.get("last_close"):
                raise ValueError("实时行情或昨收获取失败")
            target_ticks = today_ticks(api, code)
            prev_close = q["last_close"]
            display_date = datetime.now().strftime("%Y-%m-%d")
            # 腾讯日K盘中可能已包含当天未完成K线，训练集必须严格排除今天。
            train_dates = [d for d in dates if d < display_date][-train:]
        if not target_ticks:
            raise ValueError("未取得目标日分笔数据")

        prelim = []
        for d in train_dates:
            i = dates.index(d)
            if i == 0:
                continue
            ticks = day_ticks(api, code, int(d.replace("-", "")))
            if not ticks:
                continue
            try:
                f = snapshot_features(ticks, cutoff, closes[dates[i - 1]])
            except ValueError:
                continue
            # 超过正常涨跌停范围的开盘缺口通常是除权，不纳入相似盘面。
            if abs(f["gap_pct"]) > 22:
                continue
            prelim.append({"date": d, "ticks": ticks, "features": f})

        if len(prelim) < 15:
            raise ValueError(f"历史有效样本不足({len(prelim)})")
        amounts = [r["features"]["amount"] for r in prelim]
        for i, r in enumerate(prelim):
            base = median(amounts[max(0, i - 20):i] or amounts[:max(1, i + 1)])
            r["features"]["volume_ratio"] = r["features"]["amount"] / base if base else 1
            r["outcome"] = future_outcome(r["ticks"], cutoff, r["features"]["price"])
        rows = [r for r in prelim if r["outcome"]]
        validation = _walk_forward_validation(rows)
        target_base = median(amounts[-20:])
        target = snapshot_features(target_ticks, cutoff, prev_close)
        target["volume_ratio"] = target["amount"] / target_base if target_base else 1
        analogs = _nearest(rows, target)
        live_quote = q if not date else None
        depth_imbalance = live_quote.get("depth_imbalance") if live_quote else None
        target["turnover"] = live_quote.get("turnover") if live_quote else None
        target["depth_imbalance"] = depth_imbalance
        score, reasons = _rule_score(target, board_pct, indices, resonance_score,
                                     branch_up_count, depth_imbalance)
        state, advice = _decision(score)

        decisive = [r for r in analogs if r["outcome"]["first_hit"] != "neither"]
        first_up = ((sum(r["outcome"]["first_hit"] == "up" for r in decisive) + 1) /
                    (len(decisive) + 2)) if decisive else None
        maes = [r["outcome"]["mae"] for r in analogs]
        mfes = [r["outcome"]["mfe"] for r in analogs]
        actual = future_outcome(target_ticks, cutoff, target["price"]) if date else None
        if not validation or validation["n"] < 20:
            probability_quality = "样本不足"
        elif validation["accuracy"] < 0.52 or validation["brier"] > 0.25:
            probability_quality = "低，仅作历史参考"
        elif validation["accuracy"] < 0.58 or validation["brier"] > 0.22:
            probability_quality = "中"
        else:
            probability_quality = "较高"
        invalidations = [
            f"连续两根5分钟线收在均价线 {target['vwap']:.2f} 下方",
            f"跌破最近15分钟低点 {target['last_15_low']:.2f}，且板块同步转弱",
            "主力净流入快速缩小或转为净流出",
        ]
        return {
            "code": code,
            "name": (live_quote or tencent_realtime([code]).get(code, {})).get("name", code),
            "date": display_date, "cutoff": cutoff,
            "score": score, "state": state, "advice": advice,
            "market_indices": indices or {}, "branch_up_count": branch_up_count,
            "features": {k: round(v, 3) if isinstance(v, float) else v
                         for k, v in target.items() if k != "amount"},
            "reasons": reasons[:8],
            "probabilities": {
                "next_30m": _prob(analogs, "ret_30m"),
                "to_am_close": _prob(analogs, "ret_am"),
                "to_close": _prob(analogs, "ret_close"),
                "up15_before_down10": round(first_up, 3) if first_up is not None else None,
            },
            "risk": {
                "median_pullback": round(_quantile(maes, 0.5), 2),
                "bad_case_pullback": round(_quantile(maes, 0.2), 2),
                "median_upside": round(_quantile(mfes, 0.5), 2),
            },
            "samples": {"train": len(rows), "similar": len(analogs),
                        "decisive_first_touch": len(decisive)},
            "validation": validation,
            "probability_quality": probability_quality,
            "order_book": ({"bids": live_quote.get("bids", []),
                            "asks": live_quote.get("asks", []),
                            "imbalance": depth_imbalance}
                           if live_quote else None),
            "analogs": [{"date": r["date"], "distance": round(r["distance"], 2),
                         "ret_close": round(r["outcome"]["ret_close"], 2),
                         "mfe": round(r["outcome"]["mfe"], 2),
                         "mae": round(r["outcome"]["mae"], 2)} for r in analogs[:5]],
            "invalidations": invalidations,
            "actual": actual,
            "method": "规则评分＋历史相似盘面经验概率（拉普拉斯平滑）",
        }
    finally:
        api.disconnect()
