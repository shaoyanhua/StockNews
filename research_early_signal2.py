# -*- coding: utf-8 -*-
"""
早期信号 · 第二轮: 锁定 dist_high20(距20日高点)
================================================
第一轮结论: 低涨幅子样本里唯一前后两段同号的稳健特征是 dist_high20(IC +0.054);
           量比(vol_ratio)前后两段符号相反(-0.080 / +0.056), "放量滞涨=吸筹"不成立。

本轮要回答:
  1) dist_high20 的有效性是不是只是"强势股动量"的马甲? (控制 ret5 后是否还在)
  2) 阈值定在哪? 细分档看拐点
  3) 在股灾后反攻这个当前状态下还成立吗? (按时间分段)
  4) 收紧到"几乎没涨"(-1%~+2%)时是否依然有效
"""
import statistics
import sys

from research_early_signal import load, features, spearman
from webapp import WATCHLIST


def samples(data, lo, hi):
    out = []
    for code, bars in data.items():
        for i in range(25, len(bars) - 1):
            f = features(bars, i)
            if not f:
                continue
            if lo <= f["pct"] <= hi:
                out.append(dict(f, fwd=(bars[i + 1]["close"] / bars[i]["close"] - 1) * 100,
                                 date=bars[i]["date"], code=code))
    return out


def buckets(smp, key, edges, label):
    base = statistics.mean([s["fwd"] for s in smp])
    print(f"\n  {label}  (n={len(smp)}, 基准 {base:+.2f}%)")
    for lo, hi in edges:
        seg = [s for s in smp if lo <= s[key] < hi]
        if len(seg) < 15:
            print(f"    [{lo:>6.1f},{hi:>6.1f})  n={len(seg):4d}  样本不足")
            continue
        m = statistics.mean([s["fwd"] for s in seg])
        win = sum(1 for s in seg if s["fwd"] > 0) / len(seg)
        bar = "█" * max(0, min(30, int((m - base + 1) * 10)))
        print(f"    [{lo:>6.1f},{hi:>6.1f})  n={len(seg):4d}  次日{m:+6.2f}%  "
              f"超额{m-base:+6.2f}%  胜率{win:>4.0%}  {bar}")


if __name__ == "__main__":
    data = load(250)
    EDGES = [(-100, -20), (-20, -12), (-12, -8), (-8, -5), (-5, -3), (-3, -1.5), (-1.5, 0.001)]

    print("=" * 78)
    print("Q1 · dist_high20 在不同低涨幅口径下是否稳定")
    print("=" * 78)
    for lo, hi, name in [(-2, 3, "涨幅 -2%~+3%"), (-1, 2, "涨幅 -1%~+2% (几乎没涨)"),
                          (-5, 1, "涨幅 -5%~+1% (在跌/平)")]:
        smp = samples(data, lo, hi)
        ic = spearman([s["dist_high20"] for s in smp], [s["fwd"] for s in smp])
        buckets(smp, "dist_high20", EDGES, f"{name}   IC={ic:+.3f}")

    print("\n" + "=" * 78)
    print("Q2 · 是不是动量的马甲? 控制 ret5 后 dist_high20 是否还有效")
    print("=" * 78)
    smp = samples(data, -2, 3)
    for r5lo, r5hi, nm in [(-100, 0, "ret5<0 (5日在跌)"), (0, 8, "ret5 0~8%"),
                            (8, 100, "ret5>8% (5日已大涨)")]:
        sub = [s for s in smp if r5lo <= s["ret5"] < r5hi]
        if len(sub) < 60:
            continue
        ic = spearman([s["dist_high20"] for s in sub], [s["fwd"] for s in sub])
        near = [s for s in sub if s["dist_high20"] >= -3]
        far = [s for s in sub if s["dist_high20"] < -12]
        b = statistics.mean([s["fwd"] for s in sub])
        print(f"\n  {nm}: n={len(sub)}, IC={ic:+.3f}, 基准{b:+.2f}%")
        if len(near) >= 15:
            m = statistics.mean([s['fwd'] for s in near])
            print(f"    近高点(≥-3%)  n={len(near):4d}  {m:+.2f}%  超额{m-b:+.2f}%")
        if len(far) >= 15:
            m = statistics.mean([s['fwd'] for s in far])
            print(f"    远离(<-12%)   n={len(far):4d}  {m:+.2f}%  超额{m-b:+.2f}%")

    print("\n" + "=" * 78)
    print("Q3 · 分时间段: 这个信号在不同市场状态下稳不稳")
    print("=" * 78)
    smp = sorted(samples(data, -2, 3), key=lambda s: s["date"])
    k = len(smp) // 3
    for idx, seg in enumerate([smp[:k], smp[k:2 * k], smp[2 * k:]]):
        ic = spearman([s["dist_high20"] for s in seg], [s["fwd"] for s in seg])
        b = statistics.mean([s["fwd"] for s in seg])
        near = [s for s in seg if s["dist_high20"] >= -3]
        m = statistics.mean([s["fwd"] for s in near]) if len(near) >= 15 else None
        print(f"  第{idx+1}段 {seg[0]['date']}~{seg[-1]['date']}  n={len(seg):4d}  "
              f"IC={ic:+.3f}  基准{b:+.2f}%  "
              f"近高点组{('%+.2f%% (超额%+.2f%%)' % (m, m-b)) if m is not None else '样本不足'}")

    print("\n" + "=" * 78)
    print("Q4 · 可落地规则: 低涨幅 + 近20日高点 + 站上MA20")
    print("=" * 78)
    smp = samples(data, -2, 3)
    base = statistics.mean([s["fwd"] for s in smp])
    print(f"  低涨幅子样本基准: {base:+.2f}% (n={len(smp)})")
    for dh in (-5, -3, -1.5):
        for need_ma in (False, True):
            sel = [s for s in smp if s["dist_high20"] >= dh
                   and (s["above_ma20"] > 0 if need_ma else True)]
            if len(sel) < 20:
                continue
            m = statistics.mean([s["fwd"] for s in sel])
            win = sum(1 for s in sel if s["fwd"] > 0) / len(sel)
            print(f"  距高点≥{dh:>5.1f}%{' + 站上MA20' if need_ma else '           '}  "
                  f"n={len(sel):4d}  次日{m:+.2f}%  超额{m-base:+.2f}%  胜率{win:.0%}")
