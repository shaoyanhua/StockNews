# -*- coding: utf-8 -*-
"""
换赛道雷达 · 盘中时点回测 (09:35 / 09:45 / 10:00)
==================================================
问题: 早盘这三个时点看雷达做换赛道决策, 判断准不准?

口径(严格只用该时刻可得的信息, 杜绝前视):
  信号 @ cutoff:
    趋势     ← 截至 D-1 收盘的日K (盘中日K还没定型, 只能用昨收之前)
    当前涨幅 ← cutoff 时刻最新价 / D-1 收盘
    涨速     ← cutoff 价 vs cutoff-5分钟 价 (线上5分钟滚动样本的历史等价物)
    主力资金 ← 分笔重构至 cutoff (predict_updown.flow_at, 与线上同口径概念)
    位置     ← 距20日高点(用 D-1 及之前的日K + 当前价)
  结果:
    ret_close = cutoff价 -> 当日收盘   (最贴合"此刻换仓,持到收盘")
    ret_30m   = cutoff价 -> +30分钟    (短线跟随度)

评估:
  IC          评分与 ret_close 的日内横截面秩相关(逐日算再平均)
  Top5 超额   评分前5 vs 当日24只等权
  强势组超额  is_strong 命中的 vs 等权
  方向准确率  评分>=阈值 且 ret_close>0 的比例

用法: python backtest_intraday_cutoffs.py [--days 6]
"""
import argparse
import statistics
import sys
import time

from predict_updown import (tdx_connect, day_ticks, flow_at, amount_at,
                            tencent_kline)
from webapp import (WATCHLIST, _trend_features, _classify_wave,
                    _score_stock, _is_strong)

CUTOFFS = ["09:35", "09:45", "10:00"]


def add_min(hhmm, mins):
    h, m = int(hhmm[:2]), int(hhmm[3:])
    t = h * 60 + m + mins
    return f"{t//60:02d}:{t%60:02d}"


def price_at(ticks, cutoff):
    p = None
    for t in ticks:
        if t["time"] <= cutoff:
            p = t["price"]
        else:
            break
    return p


def load_daily(codes, n=90):
    px = {}
    for c in codes:
        try:
            px[c] = tencent_kline(c, n=n, fq="qfq")     # [(date, close)] 前复权
        except Exception as e:
            print(f"  日K失败 {c}: {e}", file=sys.stderr)
    return px


def daily_highs(code, n=90):
    """取20日高点需要 high, 单独拉一次不复权日K并用复权因子折算过于复杂;
    这里直接用前复权收盘价的20日最大值近似 20日高点 —— 与 webapp._trend_features
    的 ext_from_high20 口径一致(它也是用 close 序列), 保持前后一致比绝对精确更重要。"""
    return None


def run(days):
    codes = [c for c, _, _ in WATCHLIST]
    names = {c: n for c, n, _ in WATCHLIST}
    branches = {c: b for c, _, b in WATCHLIST}

    print("拉取日K...", file=sys.stderr)
    px = load_daily(codes)
    codes = [c for c in codes if c in px and len(px[c]) > 30]

    # 池内共同交易日
    common = None
    for c in codes:
        ds = {d for d, _ in px[c]}
        common = ds if common is None else (common & ds)
    dates = sorted(common)
    target_days = dates[-days:]
    print(f"回测交易日: {target_days[0]} ~ {target_days[-1]} ({len(target_days)}天)",
          file=sys.stderr)

    api = tdx_connect()
    ticks = {}
    try:
        for di, d in enumerate(target_days):
            di_int = int(d.replace("-", ""))
            for ci, c in enumerate(codes):
                try:
                    tk = day_ticks(api, c, di_int)
                except Exception:
                    tk = []
                ticks[(c, d)] = tk
            got = sum(1 for c in codes if ticks.get((c, d)))
            print(f"  [{di+1}/{len(target_days)}] {d} 分笔可用 {got}/{len(codes)} 只",
                  file=sys.stderr)
    finally:
        api.disconnect()

    results = {cut: [] for cut in CUTOFFS}
    for d in target_days:
        ds = [x for x, _ in px[codes[0]]]
        if d not in ds:
            continue
        for cut in CUTOFFS:
            recs = []
            for c in codes:
                tk = ticks.get((c, d)) or []
                if not tk:
                    continue
                cl_series = [v for x, v in px[c] if x < d]      # 严格 D-1 及之前
                if len(cl_series) < 25:
                    continue
                prev_close = cl_series[-1]
                p_now = price_at(tk, cut)
                if not p_now or not prev_close:
                    continue
                p_5ago = price_at(tk, add_min(cut, -5))
                p_close = tk[-1]["price"]
                p_30 = price_at(tk, add_min(cut, 30)) or p_close

                pct = (p_now / prev_close - 1) * 100
                # 涨速: 当前涨幅 - 5分钟前涨幅
                chg5 = (pct - (p_5ago / prev_close - 1) * 100) if p_5ago else None

                # 趋势: 用 D-1 及之前收盘序列 + 当前价 拼出"含今日"的位置
                trend = _trend_features(cl_series + [p_now])
                label, is_wave = _classify_wave(trend, pct)
                trend = dict(trend, label=label, is_main_wave=is_wave)

                # 主力资金净占比(与线上 f184 同概念: 主力净额/成交额)
                _sup, main_yi, _lp = flow_at(tk, cut)
                amt = amount_at(tk, cut)
                main_pct = (main_yi * 1e8 / amt * 100) if amt else None

                if chg5 is None:
                    mom = {"chg_5m": None, "chg_15m": None, "speed_label": "首次采样"}
                else:
                    if chg5 >= 1.0:
                        sl = "加速上涨"
                    elif chg5 >= 0.2:
                        sl = "温和走强"
                    elif chg5 > -0.2:
                        sl = "走平"
                    elif chg5 > -1.0:
                        sl = "转弱"
                    else:
                        sl = "加速下跌"
                    mom = {"chg_5m": round(chg5, 2), "chg_15m": None, "speed_label": sl}

                score, _ = _score_stock(pct, trend, mom, main_pct)
                recs.append({
                    "code": c, "name": names[c], "branch": branches[c], "date": d,
                    "cut": cut, "score": score, "pct": pct, "label": label,
                    "main_pct": main_pct, "chg5": chg5,
                    "is_strong": _is_strong(score, label, pct, main_pct),
                    "ret_close": (p_close / p_now - 1) * 100,
                    "ret_30m": (p_30 / p_now - 1) * 100,
                })
            if recs:
                results[cut].append({"date": d, "recs": recs})
    return results


def spearman(xs, ys):
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    if len(xs) < 3:
        return 0.0
    rx, ry = rank(xs), rank(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** .5
    dy = sum((b - my) ** 2 for b in ry) ** .5
    return num / (dx * dy) if dx and dy else 0.0


def report(cut, days_data, target="ret_close"):
    print(f"\n{'='*80}")
    print(f"时点 {cut}  ·  目标: {'此刻→收盘' if target=='ret_close' else '此刻→+30分钟'}")
    print(f"{'='*80}")
    print(f"{'日期':<12}{'n':>4}{'基准':>9}{'Top5':>9}{'Bot5':>9}"
          f"{'强势':>6}{'强势收益':>10}{'超额':>9}{'IC':>8}")
    ics, top_ex, str_ex, allrecs = [], [], [], []
    for day in days_data:
        rs = sorted(day["recs"], key=lambda r: -r["score"])
        n = len(rs)
        base = statistics.mean([r[target] for r in rs])
        top5 = statistics.mean([r[target] for r in rs[:5]])
        bot5 = statistics.mean([r[target] for r in rs[-5:]])
        st = [r for r in rs if r["is_strong"]]
        sr = statistics.mean([r[target] for r in st]) if st else None
        ic = spearman([r["score"] for r in rs], [r[target] for r in rs])
        ics.append(ic)
        top_ex.append(top5 - base)
        if sr is not None:
            str_ex.append(sr - base)
        allrecs.extend(rs)
        print(f"{day['date']:<12}{n:>4}{base:>8.2f}%{top5:>8.2f}%{bot5:>8.2f}%"
              f"{len(st):>4}只{('%.2f%%' % sr) if sr is not None else '     —':>10}"
              f"{('%+.2f%%' % (sr-base)) if sr is not None else '     —':>9}{ic:>+8.2f}")
    print("-" * 80)
    print(f"IC 日均 {statistics.mean(ics):+.3f} (为正 {sum(1 for x in ics if x>0)}/{len(ics)} 天) · "
          f"Top5超额 日均 {statistics.mean(top_ex):+.2f}% (赢 {sum(1 for x in top_ex if x>0)}/{len(top_ex)})")
    if str_ex:
        print(f"强势组超额 日均 {statistics.mean(str_ex):+.2f}% "
              f"(赢 {sum(1 for x in str_ex if x>0)}/{len(str_ex)} 天, 共{len(str_ex)}天有强势标的)")
    else:
        print("强势组: 6天内无任何标的触发强势")

    # 方向准确率
    st = [r for r in allrecs if r["is_strong"]]
    if st:
        acc = sum(1 for r in st if r[target] > 0) / len(st)
        print(f"强势标的方向准确率(判强势且{target}>0): {acc:.0%}  (n={len(st)})")
    for thr in (4.0, 5.0):
        sel = [r for r in allrecs if r["score"] >= thr]
        if len(sel) >= 5:
            acc = sum(1 for r in sel if r[target] > 0) / len(sel)
            m = statistics.mean([r[target] for r in sel])
            print(f"评分≥{thr}: n={len(sel):3d}  方向准确率 {acc:.0%}  平均 {m:+.2f}%")
    allbase = statistics.mean([r[target] for r in allrecs])
    print(f"全样本基准: {allbase:+.2f}%  (n={len(allrecs)})")
    return {"ic": statistics.mean(ics), "top_ex": statistics.mean(top_ex),
            "str_ex": statistics.mean(str_ex) if str_ex else None,
            "allrecs": allrecs}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=6)
    a = ap.parse_args()

    t0 = time.time()
    res = run(a.days)
    print(f"\n分笔重构耗时 {time.time()-t0:.0f}s", file=sys.stderr)

    summary = {}
    for cut in CUTOFFS:
        if res[cut]:
            summary[cut] = report(cut, res[cut], "ret_close")

    print(f"\n{'='*80}")
    print("三个时点横向对比 (目标: 此刻→收盘)")
    print(f"{'='*80}")
    print(f"{'时点':<10}{'IC日均':>10}{'Top5超额':>12}{'强势组超额':>13}")
    for cut in CUTOFFS:
        s = summary.get(cut)
        if not s:
            continue
        se = f"{s['str_ex']:+.2f}%" if s["str_ex"] is not None else "无强势"
        print(f"{cut:<10}{s['ic']:>+10.3f}{s['top_ex']:>11.2f}%{se:>13}")

    print(f"\n{'='*80}")
    print("附: 30分钟跟随度 (判断是否只是短暂脉冲)")
    print(f"{'='*80}")
    for cut in CUTOFFS:
        if res[cut]:
            report(cut, res[cut], "ret_30m")
