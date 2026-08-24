# -*- coding: utf-8 -*-
"""
"买不进"风险测算 (一字板 / 高开秒板)
=====================================
问题: 雷达9:35判强, 但哪些票经常直接一字板/高开秒板, 导致根本买不进?

口径说明(为什么可以用前复权数据判涨停):
  前复权对同一天的 O/H/L/C 施加同一因子, 故 **日内比值**(high==low) 与
  **日间收益**(close_t/close_{t-1}) 都不受影响。用不复权反而会被除权跳空污染
  (见项目已知的"tdx K线不复权陷阱")。所以全程用 qfq 序列。

分类(按严重程度):
  一字板     high==low 且 收益达涨停  -> 全天无成交价差, 100%买不进
  开盘即涨停 开盘价即达涨停           -> 除非集合竞价挂单, 否则极难买
  盘中封板   收盘达涨停但开盘没到     -> 早盘有机会买
  大幅高开   开盘涨幅≥5%(未涨停)      -> 买得进但成本高

涨停幅度: 创业板(300/301)/科创板(688) 20%, 其余主板 10%
"""
import statistics
import sys

from research_early_signal import load
from webapp import WATCHLIST


def limit_pct(code):
    return 20.0 if code.startswith(("300", "301", "688")) else 10.0


def analyze(bars, code):
    lim = limit_pct(code)
    thr = lim - 0.3          # 容忍取整误差 (10%->9.7, 20%->19.7)
    n = 0
    yiziban = 0              # 一字板: 全天无价差且涨停
    open_limit = 0           # 开盘即涨停
    seal = 0                 # 收盘涨停(含一字)
    intraday_seal = 0        # 盘中才封板(开盘没涨停)
    big_gap = 0              # 高开≥5%但未涨停
    gaps = []
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        pc = p["close"]
        if not pc:
            continue
        n += 1
        ret = (b["close"] / pc - 1) * 100
        gap = (b["open"] / pc - 1) * 100
        gaps.append(gap)
        is_seal = ret >= thr
        is_open_lim = gap >= thr
        no_range = abs(b["high"] - b["low"]) < 1e-6
        if is_seal:
            seal += 1
            if no_range and is_open_lim:
                yiziban += 1
            elif is_open_lim:
                open_limit += 1
            else:
                intraday_seal += 1
        elif gap >= 5.0:
            big_gap += 1
    if not n:
        return None
    return {
        "n": n, "lim": lim,
        "yiziban": yiziban, "open_limit": open_limit,
        "intraday_seal": intraday_seal, "seal": seal, "big_gap": big_gap,
        "unbuyable": yiziban + open_limit,          # 实际买不进的天数
        "gap_p90": statistics.quantiles(gaps, n=10)[8] if len(gaps) > 10 else None,
        "gap_max": max(gaps) if gaps else None,
    }


if __name__ == "__main__":
    data = load(250)
    names = {c: n for c, n, _ in WATCHLIST}
    branches = {c: b for c, _, b in WATCHLIST}

    rows = []
    for code, name, branch in WATCHLIST:
        bars = data.get(code)
        if not bars:
            continue
        r = analyze(bars, code)
        if r:
            rows.append((code, name, branch, r))

    rows.sort(key=lambda x: -(x[3]["unbuyable"] / x[3]["n"]))

    print("=" * 100)
    print("买不进风险排行 (近250个交易日)")
    print("=" * 100)
    print(f"{'股票':<9}{'赛道':<10}{'涨停幅':>6}{'一字板':>7}{'开盘即停':>9}"
          f"{'买不进率':>9}{'盘中封板':>9}{'涨停总数':>9}{'高开P90':>9}{'最大高开':>9}")
    for code, name, branch, r in rows:
        ub = r["unbuyable"] / r["n"] * 100
        print(f"{name:<9}{branch:<10}{r['lim']:>5.0f}%{r['yiziban']:>6}次"
              f"{r['open_limit']:>8}次{ub:>8.1f}%{r['intraday_seal']:>8}次"
              f"{r['seal']:>8}次"
              f"{(('%+.1f%%' % r['gap_p90']) if r['gap_p90'] is not None else '—'):>9}"
              f"{(('%+.1f%%' % r['gap_max']) if r['gap_max'] is not None else '—'):>9}")

    print("\n" + "=" * 100)
    print("分级")
    print("=" * 100)
    high = [x for x in rows if x[3]["unbuyable"] / x[3]["n"] >= 0.02]
    mid = [x for x in rows if 0.008 <= x[3]["unbuyable"] / x[3]["n"] < 0.02]
    low = [x for x in rows if x[3]["unbuyable"] / x[3]["n"] < 0.008]
    for tag, grp in (("高风险(买不进率≥2%)", high), ("中风险(0.8%~2%)", mid),
                     ("低风险(<0.8%)", low)):
        if grp:
            print(f"  {tag}: " + "、".join(f"{x[1]}" for x in grp))

    print("\n" + "=" * 100)
    print("涨停后还能不能追? (封板日 vs 次日收益)")
    print("=" * 100)
    print(f"{'股票':<9}{'一字次日':>10}{'开停次日':>10}{'盘中封板次日':>13}")
    for code, name, branch, r in rows[:12]:
        bars = data[code]
        lim = limit_pct(code)
        thr = lim - 0.3
        buckets = {"yizi": [], "openlim": [], "intra": []}
        for i in range(1, len(bars) - 1):
            b, p = bars[i], bars[i - 1]
            if not p["close"]:
                continue
            ret = (b["close"] / p["close"] - 1) * 100
            gap = (b["open"] / p["close"] - 1) * 100
            if ret < thr:
                continue
            fwd = (bars[i + 1]["close"] / b["close"] - 1) * 100
            no_range = abs(b["high"] - b["low"]) < 1e-6
            if no_range and gap >= thr:
                buckets["yizi"].append(fwd)
            elif gap >= thr:
                buckets["openlim"].append(fwd)
            else:
                buckets["intra"].append(fwd)

        def fmt(v):
            if len(v) < 2:
                return f"n={len(v)}"
            return f"{statistics.mean(v):+.1f}%(n={len(v)})"
        print(f"{name:<9}{fmt(buckets['yizi']):>10}{fmt(buckets['openlim']):>10}"
              f"{fmt(buckets['intra']):>13}")

    # 全池汇总: 三类封板的次日期望
    print("\n  全池汇总:")
    agg = {"yizi": [], "openlim": [], "intra": []}
    for code, name, branch, r in rows:
        bars = data[code]
        thr = limit_pct(code) - 0.3
        for i in range(1, len(bars) - 1):
            b, p = bars[i], bars[i - 1]
            if not p["close"]:
                continue
            ret = (b["close"] / p["close"] - 1) * 100
            gap = (b["open"] / p["close"] - 1) * 100
            if ret < thr:
                continue
            fwd = (bars[i + 1]["close"] / b["close"] - 1) * 100
            no_range = abs(b["high"] - b["low"]) < 1e-6
            if no_range and gap >= thr:
                agg["yizi"].append(fwd)
            elif gap >= thr:
                agg["openlim"].append(fwd)
            else:
                agg["intra"].append(fwd)
    for k, lbl in (("yizi", "一字板"), ("openlim", "开盘即涨停"), ("intra", "盘中封板")):
        v = agg[k]
        if len(v) >= 3:
            win = sum(1 for x in v if x > 0) / len(v)
            print(f"    {lbl:<12} n={len(v):3d}  次日均值 {statistics.mean(v):+.2f}%  "
                  f"上涨概率 {win:.0%}")
