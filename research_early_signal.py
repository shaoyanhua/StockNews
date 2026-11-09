# -*- coding: utf-8 -*-
"""
早期强势信号研究
================
问题: 能否在"当前涨幅还不高"时就判定强势? (现行评分把涨幅≥7%给2分, 等于追高)

做法: 拉长历史(250日), 只在"当日涨幅不高"的子样本里, 检验哪些特征能预测次日收益。
      候选特征全部不依赖大涨, 都是"启动前"可观测的量:
        vol_ratio   放量程度(量比): 当日量 / 前5日均量  —— 放量滞涨 = 吸筹?
        dist_high20 距20日高点     —— 逼近突破?
        above_ma20  是否站上MA20
        ret5        5日涨幅        —— 已在反弹通道?
        ma20_slope  MA20斜率
        amt_ratio   成交额放大倍数
        gap         当日开盘跳空

评估: 每个特征与次日收益的秩相关(IC), 并做前后两段样本分割检验稳健性。
      基准 = 子样本内等权次日平均收益, 只有超额才算有效。

用法: python research_early_signal.py [--lowpct 3.0] [--days 250]
"""
import argparse
import json
import statistics
import subprocess
import sys
import time

from predict_updown import prefix
from webapp import WATCHLIST

CACHE = "cache_predict/research_early_signal.json"


def _curl(url):
    return subprocess.run(["curl", "-s", "--max-time", "25", "-A", "Mozilla/5.0", url],
                          capture_output=True, text=True, encoding="utf-8").stdout


def fetch(code, n):
    """腾讯日K: 前复权算收益, 不复权取量。返回按日期升序的 bar 列表。"""
    sym = f"{prefix(code)}{code}"
    qfq = json.loads(_curl(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                           f"?param={sym},day,,,{n},qfq"))["data"][sym]
    raw = json.loads(_curl(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                           f"?param={sym},day,,,{n},"))["data"][sym]
    qb = qfq.get("qfqday") or qfq.get("day")
    rb = {b[0]: b for b in (raw.get("day") or [])}
    out = []
    for b in qb:
        r = rb.get(b[0])
        if not r or len(r) < 6:
            continue
        out.append({"date": b[0], "open": float(b[1]), "close": float(b[2]),
                    "high": float(b[3]), "low": float(b[4]),
                    "vol": float(r[5]), "raw_close": float(r[2])})
    return out


def latest_trading_date():
    """探针: 拉一只票的最近日K, 取最后一个交易日。
    用来判断缓存是否已过期 —— 只按"缓存条数"判断是date-blind的, 新交易日到来后
    条数依然满足, 会导致永远命中旧缓存且毫无征兆(本函数即为修此问题而加)。"""
    try:
        b = json.loads(_curl("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                             "?param=sh000001,day,,,3,"))["data"]["sh000001"]
        bars = b.get("day") or b.get("qfqday")
        return bars[-1][0]
    except Exception:
        return None


def load(days, refresh=False):
    data = {}
    if not refresh:
        try:
            data = json.load(open(CACHE, encoding="utf-8"))
        except (OSError, ValueError):
            data = {}

    newest = latest_trading_date()
    if newest:
        stale = [c for c, v in data.items() if v and v[-1]["date"] < newest]
        if stale:
            print(f"  缓存已过期(最新交易日 {newest}), 将重新拉取 {len(stale)} 只",
                  file=sys.stderr)
            for c in stale:
                data.pop(c, None)
    elif data:
        # 探针失败(典型是腾讯fqkline被WAF封, 返回501跳转页)时无法判断缓存新旧。
        # 旧实现这里静默沿用缓存, 曾导致用11天前的数据出结论而毫无察觉 —— 必须显式告警。
        last = max((v[-1]["date"] for v in data.values() if v), default="?")
        print(f"  ⚠️ 最新交易日探针失败(腾讯日K可能被WAF拦截), 无法确认缓存新鲜度;"
              f" 将沿用缓存, 其最新日期为 {last} —— 结论可能基于旧数据!", file=sys.stderr)

    out = {}
    for i, (code, name, branch) in enumerate(WATCHLIST):
        cached = data.get(code)
        if cached and len(cached) >= days - 10 and (
                not newest or cached[-1]["date"] >= newest):
            out[code] = cached
            continue
        try:
            out[code] = fetch(code, days)
            time.sleep(0.3)
        except Exception as e:
            print(f"  skip {name}: {e}", file=sys.stderr)
            continue
        print(f"  [{i+1}/{len(WATCHLIST)}] {name} {len(out[code])}根", file=sys.stderr)
    try:
        import os
        os.makedirs("cache_predict", exist_ok=True)
        json.dump(out, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    except OSError:
        pass
    return out


def features(bars, i):
    """在第 i 根(信号日)收盘时可得的特征。i 需 >= 20 且 < len-1。"""
    b = bars[i]
    cl = [x["close"] for x in bars[:i + 1]]
    vols = [x["vol"] for x in bars[:i + 1]]
    if len(cl) < 25 or vols[i] <= 0:
        return None
    prev5_vol = statistics.mean(vols[i - 5:i]) if i >= 5 else None
    if not prev5_vol:
        return None
    ma20 = statistics.mean(cl[-20:])
    ma20_5ago = statistics.mean(cl[-25:-5])
    high20 = max(x["high"] for x in bars[i - 19:i + 1])
    pct = (cl[-1] / cl[-2] - 1) * 100
    return {
        "pct": pct,
        "vol_ratio": vols[i] / prev5_vol,
        "dist_high20": (b["close"] / high20 - 1) * 100,
        "above_ma20": 1.0 if b["close"] > ma20 else 0.0,
        "ret5": (cl[-1] / cl[-6] - 1) * 100,
        "ma20_slope": (ma20 / ma20_5ago - 1) * 100,
        "gap": (b["open"] / cl[-2] - 1) * 100,
        "intraday": (b["close"] / b["open"] - 1) * 100,   # 开盘->收盘, 日内主动性
    }


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


def build_samples(data, lowpct):
    """收集 (特征, 次日收益) 样本; 只保留"当日涨幅不高"的。"""
    allsmp, lowsmp = [], []
    for code, bars in data.items():
        for i in range(25, len(bars) - 1):
            f = features(bars, i)
            if not f:
                continue
            fwd = (bars[i + 1]["close"] / bars[i]["close"] - 1) * 100
            rec = dict(f, fwd=fwd, date=bars[i]["date"], code=code)
            allsmp.append(rec)
            if -2.0 <= f["pct"] <= lowpct:
                lowsmp.append(rec)
    return allsmp, lowsmp


def ic_table(smp, title, keys):
    print(f"\n{title}  (n={len(smp)}, 次日均值 {statistics.mean([s['fwd'] for s in smp]):+.2f}%)")
    print(f"  {'特征':<14}{'IC':>8}{'前半段':>9}{'后半段':>9}{'稳健':>7}")
    smp_sorted = sorted(smp, key=lambda s: s["date"])
    mid = len(smp_sorted) // 2
    h1, h2 = smp_sorted[:mid], smp_sorted[mid:]
    rows = []
    for k in keys:
        ic = spearman([s[k] for s in smp], [s["fwd"] for s in smp])
        i1 = spearman([s[k] for s in h1], [s["fwd"] for s in h1])
        i2 = spearman([s[k] for s in h2], [s["fwd"] for s in h2])
        ok = "✓" if (i1 > 0) == (i2 > 0) and abs(ic) > 0.03 else ""
        rows.append((abs(ic), k, ic, i1, i2, ok))
        print(f"  {k:<14}{ic:>+8.3f}{i1:>+9.3f}{i2:>+9.3f}{ok:>7}")
    return rows


def quintiles(smp, key, title):
    """按特征分5档看次日收益, 检验单调性(IC看不出非线性)。"""
    s = sorted(smp, key=lambda x: x[key])
    n = len(s) // 5
    if n < 5:
        return
    base = statistics.mean([x["fwd"] for x in smp])
    print(f"\n  {title} 分5档 (基准 {base:+.2f}%)")
    for q in range(5):
        seg = s[q * n:(q + 1) * n] if q < 4 else s[4 * n:]
        m = statistics.mean([x["fwd"] for x in seg])
        rng = f"{seg[0][key]:.2f}~{seg[-1][key]:.2f}"
        bar = "█" * max(0, min(28, int((m - base + 2) * 6)))
        print(f"    Q{q+1} [{rng:>16}] n={len(seg):4d}  {m:+6.2f}%  超额{m-base:+6.2f}%  {bar}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--lowpct", type=float, default=3.0, help="低涨幅上限(百分点)")
    ap.add_argument("--days", type=int, default=250)
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()

    print(f"拉取 {len(WATCHLIST)} 只 × {a.days}日...", file=sys.stderr)
    data = load(a.days, a.refresh)
    allsmp, lowsmp = build_samples(data, a.lowpct)

    KEYS = ["vol_ratio", "dist_high20", "above_ma20", "ret5",
            "ma20_slope", "gap", "intraday", "pct"]

    print("=" * 74)
    print(f"全样本 vs 低涨幅样本 (当日涨幅 -2% ~ +{a.lowpct}%)")
    print("=" * 74)
    ic_table(allsmp, "【全样本】", KEYS)
    rows = ic_table(lowsmp, f"【低涨幅子样本】← 关键: 涨幅还不高时哪些特征有效", KEYS)

    print("\n" + "=" * 74)
    print("低涨幅子样本 · 有效特征的分档表现")
    print("=" * 74)
    for _, k, ic, i1, i2, ok in sorted(rows, reverse=True)[:4]:
        quintiles(lowsmp, k, f"{k} (IC {ic:+.3f}{' 稳健' if ok else ''})")

    # 组合信号: 放量 + 未破位 + 未过度偏离高点
    print("\n" + "=" * 74)
    print("组合验证: 低涨幅 + 放量 + 站上MA20")
    print("=" * 74)
    base = statistics.mean([s["fwd"] for s in lowsmp])
    for vr in (1.2, 1.5, 2.0):
        sel = [s for s in lowsmp if s["vol_ratio"] >= vr and s["above_ma20"] > 0]
        if len(sel) < 20:
            continue
        m = statistics.mean([s["fwd"] for s in sel])
        win = sum(1 for s in sel if s["fwd"] > 0) / len(sel)
        print(f"  量比≥{vr}: n={len(sel):4d}  次日 {m:+.2f}%  超额 {m-base:+.2f}%  胜率 {win:.0%}")
    print(f"  (低涨幅子样本基准: {base:+.2f}%, n={len(lowsmp)})")
