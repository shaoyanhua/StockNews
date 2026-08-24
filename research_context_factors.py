# -*- coding: utf-8 -*-
"""
环境因子检验: 板块共振 / 大盘指数 / 量能
=========================================
问: 个股强势判定要不要叠加 板块共振、大盘涨跌、放量缩量?

已知(第一轮研究): vol_ratio 在低涨幅子样本 IC=-0.005 且前后两段符号相反, 不稳健。
本轮换个问法重测量能, 并新测板块与大盘。

方法:
  样本 = (个股, 日) , 只保留"当日走强"的(pct>=3%), 因为我们要判断的是
         "强势票的强势能不能延续", 而不是全样本预测。
  因子:
    sector_pct   同赛道其他票当日均涨(**剔除自身**, 避免自我included偏差)
    idx_cyb      创业板指当日涨幅 (科技股的真实beta锚)
    idx_sh       上证当日涨幅
    vol_ratio    当日量 / 前5日均量
    breadth      同赛道当日上涨家数占比(剔除自身)
  目标: 次日收益
  评估: 分档看次日均值+胜率, 以及 IC 与前后两段稳健性。
"""
import json
import statistics
import subprocess
import sys

from research_early_signal import load, spearman
from webapp import WATCHLIST


def _curl(url):
    return subprocess.run(["curl", "-s", "--max-time", "25", "-A", "Mozilla/5.0", url],
                          capture_output=True, text=True, encoding="utf-8").stdout


def index_series(sym, n=300):
    d = json.loads(_curl(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                         f"?param={sym},day,,,{n},"))["data"][sym]
    bars = d.get("day") or d.get("qfqday")
    out, prev = {}, None
    for b in bars:
        c = float(b[2])
        if prev:
            out[b[0]] = (c / prev - 1) * 100
        prev = c
    return out


def build(data):
    branch = {c: b for c, _, b in WATCHLIST}
    # 先按日期组织每只票的当日涨幅/量比
    per = {}
    for code, bars in data.items():
        for i in range(6, len(bars) - 1):
            b, p = bars[i], bars[i - 1]
            if not p["close"]:
                continue
            v5 = statistics.mean([x["vol"] for x in bars[i - 5:i]])
            per.setdefault(b["date"], {})[code] = {
                "pct": (b["close"] / p["close"] - 1) * 100,
                "fwd": (bars[i + 1]["close"] / b["close"] - 1) * 100,
                "vol_ratio": (b["vol"] / v5) if v5 else None,
            }

    cyb = index_series("sz399006")
    sh = index_series("sh000001")

    smp = []
    for date, m in per.items():
        for code, r in m.items():
            if r["pct"] < 3.0:          # 只看"当日已走强"的票
                continue
            peers = [o for c2, o in m.items()
                     if c2 != code and branch.get(c2) == branch.get(code)]
            if peers:
                sec = statistics.mean([o["pct"] for o in peers])
                bre = sum(1 for o in peers if o["pct"] > 0) / len(peers)
            else:
                sec, bre = None, None
            smp.append({
                "date": date, "code": code, "pct": r["pct"], "fwd": r["fwd"],
                "vol_ratio": r["vol_ratio"], "sector_pct": sec, "breadth": bre,
                "idx_cyb": cyb.get(date), "idx_sh": sh.get(date),
            })
    return smp


def buckets(smp, key, edges, label):
    sub = [s for s in smp if s.get(key) is not None]
    if len(sub) < 40:
        print(f"\n  {label}: 样本不足({len(sub)})")
        return
    base = statistics.mean([s["fwd"] for s in sub])
    ic = spearman([s[key] for s in sub], [s["fwd"] for s in sub])
    ss = sorted(sub, key=lambda s: s["date"])
    h = len(ss) // 2
    i1 = spearman([s[key] for s in ss[:h]], [s["fwd"] for s in ss[:h]])
    i2 = spearman([s[key] for s in ss[h:]], [s["fwd"] for s in ss[h:]])
    ok = "✓稳健" if (i1 > 0) == (i2 > 0) and abs(ic) > 0.04 else "✗不稳健"
    print(f"\n  {label}  n={len(sub)} 基准{base:+.2f}%  "
          f"IC={ic:+.3f} (前{i1:+.3f}/后{i2:+.3f}) {ok}")
    for lo, hi in edges:
        seg = [s for s in sub if lo <= s[key] < hi]
        if len(seg) < 15:
            continue
        m = statistics.mean([s["fwd"] for s in seg])
        w = sum(1 for s in seg if s["fwd"] > 0) / len(seg)
        bar = "█" * max(0, min(26, int((m - base + 1.5) * 7)))
        print(f"    [{lo:>6.1f},{hi:>6.1f}) n={len(seg):4d} 次日{m:+6.2f}% "
              f"超额{m-base:+6.2f}% 胜率{w:>4.0%} {bar}")


if __name__ == "__main__":
    data = load(250)
    smp = build(data)
    print("=" * 78)
    print(f"样本: 当日涨幅≥3% 的 (个股,日) 共 {len(smp)} 个")
    print("问题: 这些'已经走强'的票, 次日还能不能延续? 环境因子有没有解释力?")
    print("=" * 78)

    buckets(smp, "sector_pct", [(-100, -2), (-2, 0), (0, 2), (2, 4), (4, 7), (7, 100)],
            "① 板块共振 (同赛道其他票当日均涨, 剔除自身)")
    buckets(smp, "breadth", [(0, 0.34), (0.34, 0.67), (0.67, 0.99), (0.99, 1.01)],
            "② 板块广度 (同赛道上涨家数占比)")
    buckets(smp, "idx_cyb", [(-100, -2), (-2, -0.5), (-0.5, 0.5), (0.5, 2), (2, 100)],
            "③ 大盘 · 创业板指当日涨幅")
    buckets(smp, "idx_sh", [(-100, -1), (-1, -0.2), (-0.2, 0.5), (0.5, 100)],
            "④ 大盘 · 上证当日涨幅")
    buckets(smp, "vol_ratio", [(0, 0.8), (0.8, 1.2), (1.2, 1.8), (1.8, 3), (3, 99)],
            "⑤ 量比 (当日量/前5日均量)")

    print("\n" + "=" * 78)
    print("组合检验: 强势票 + 环境条件")
    print("=" * 78)
    base = statistics.mean([s["fwd"] for s in smp])
    print(f"  全部强势票基准: {base:+.2f}%  n={len(smp)}")
    tests = [
        ("板块同涨(sector>0)", lambda s: s["sector_pct"] is not None and s["sector_pct"] > 0),
        ("板块同跌(sector<0)", lambda s: s["sector_pct"] is not None and s["sector_pct"] < 0),
        ("板块强共振(sector>2%)", lambda s: s["sector_pct"] is not None and s["sector_pct"] > 2),
        ("创业板涨(idx>0)", lambda s: s["idx_cyb"] is not None and s["idx_cyb"] > 0),
        ("创业板跌(idx<0)", lambda s: s["idx_cyb"] is not None and s["idx_cyb"] < 0),
        ("创业板大跌(idx<-1%)", lambda s: s["idx_cyb"] is not None and s["idx_cyb"] < -1),
        ("逆势强(个股涨但创业板跌)", lambda s: s["idx_cyb"] is not None and s["idx_cyb"] < 0),
        ("板块跌+大盘跌(双逆)", lambda s: s["sector_pct"] is not None and s["idx_cyb"] is not None
            and s["sector_pct"] < 0 and s["idx_cyb"] < 0),
        ("板块涨+大盘涨(双顺)", lambda s: s["sector_pct"] is not None and s["idx_cyb"] is not None
            and s["sector_pct"] > 0 and s["idx_cyb"] > 0),
    ]
    for name, fn in tests:
        sel = [s for s in smp if fn(s)]
        if len(sel) < 20:
            print(f"  {name:<26} n={len(sel):4d}  样本不足")
            continue
        m = statistics.mean([s["fwd"] for s in sel])
        w = sum(1 for s in sel if s["fwd"] > 0) / len(sel)
        print(f"  {name:<26} n={len(sel):4d}  次日{m:+6.2f}%  "
              f"超额{m-base:+6.2f}%  胜率{w:>4.0%}")
