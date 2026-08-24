# -*- coding: utf-8 -*-
"""
红利篮子选股: 哪些"防御股"在大跌日真的防御?
=============================================
不按行业分类想当然, 用数据选: 在创业板大跌的日子里, 谁真的逆势上涨?

指标(样本=近250日中 创业板指跌幅≥3% 的交易日):
  逆势胜率   大跌日该股上涨的比例
  平均超额   大跌日该股涨幅 - 创业板涨幅
  相关系数   该股日收益与创业板日收益的相关性(越低越独立)
  全期涨幅   避免选到"一直在跌所以跌不动"的伪防御
"""
import json
import statistics
import subprocess
import sys
import time

CANDIDATES = [
    ("601398", "工商银行", "银行"), ("601939", "建设银行", "银行"),
    ("601288", "农业银行", "银行"),
    ("601318", "中国平安", "保险"), ("601628", "中国人寿", "保险"),
    ("601601", "中国太保", "保险"),
    ("600900", "长江电力", "电力"), ("600886", "国投电力", "电力"),
    ("601088", "中国神华", "煤炭"), ("601225", "陕西煤业", "煤炭"),
    ("601006", "大秦铁路", "运输"), ("600377", "宁沪高速", "运输"),
    ("601919", "中远海控", "运输"), ("600018", "上港集团", "运输"),
    ("600941", "中国移动", "运营商"), ("601728", "中国电信", "运营商"),
    ("601857", "中国石油", "石油"), ("600028", "中国石化", "石油"),
    ("510880", "红利ETF", "ETF"),
]


def pfx(c):
    if c.startswith(("6", "9", "5")):
        return "sh"
    if c.startswith(("8", "4")):
        return "bj"
    return "sz"


def curl(u):
    return subprocess.run(["curl", "-s", "--max-time", "25", "-A", "Mozilla/5.0", u],
                          capture_output=True, text=True, encoding="utf-8").stdout


def closes(sym, n=300):
    d = json.loads(curl(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                        f"?param={sym},day,,,{n},qfq"))["data"][sym]
    b = d.get("qfqday") or d.get("day")
    return {x[0]: float(x[2]) for x in b}


def rets(c):
    s = closes(c)
    ds = sorted(s)
    return {ds[i]: (s[ds[i]] / s[ds[i - 1]] - 1) * 100 for i in range(1, len(ds))}


def corr(xs, ys):
    n = len(xs)
    if n < 5:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    dx = sum((a - mx) ** 2 for a in xs) ** .5
    dy = sum((b - my) ** 2 for b in ys) ** .5
    return num / (dx * dy) if dx and dy else 0.0


if __name__ == "__main__":
    print("拉取创业板指...", file=sys.stderr)
    cyb = rets("sz399006")
    crash = sorted([d for d, v in cyb.items() if v <= -3.0])
    print(f"近{len(cyb)}个交易日中, 创业板跌≥3%的有 {len(crash)} 天", file=sys.stderr)
    print(f"最近几天: {crash[-5:]}", file=sys.stderr)

    print(f"\n{'股票':<10}{'行业':<7}{'逆势胜率':>9}{'平均超额':>10}"
          f"{'大跌日均涨':>11}{'相关系数':>9}{'全期涨幅':>10}")
    print("-" * 74)
    rows = []
    for code, name, ind in CANDIDATES:
        try:
            r = rets(pfx(code) + code)
        except Exception as e:
            print(f"{name:<10}{ind:<7}  取数失败 {type(e).__name__}")
            continue
        common = [d for d in crash if d in r]
        if len(common) < 5:
            print(f"{name:<10}{ind:<7}  样本不足({len(common)})")
            continue
        up = sum(1 for d in common if r[d] > 0) / len(common)
        own = statistics.mean([r[d] for d in common])
        ex = statistics.mean([r[d] - cyb[d] for d in common])
        both = [d for d in r if d in cyb]
        cc = corr([r[d] for d in both], [cyb[d] for d in both])
        allds = sorted(r)
        tot = sum(r[d] for d in allds)
        rows.append((name, ind, up, ex, own, cc, tot, len(common)))
        time.sleep(0.2)

    rows.sort(key=lambda x: -x[2])
    for name, ind, up, ex, own, cc, tot, n in rows:
        flag = "✅" if (up >= 0.6 and own > 0) else ("⚠️" if up >= 0.45 else "❌")
        print(f"{name:<10}{ind:<7}{up:>8.0%}{ex:>+10.2f}%{own:>+10.2f}%"
              f"{cc:>+9.2f}{tot:>+9.1f}%  {flag}")

    print("\n" + "=" * 74)
    print("建议入选(大跌日胜率≥60% 且 大跌日自身仍为正)")
    print("=" * 74)
    keep = [r for r in rows if r[2] >= 0.6 and r[4] > 0]
    for name, ind, up, ex, own, cc, tot, n in keep:
        print(f"  ✅ {name:<10}{ind:<7} 胜率{up:.0%} 大跌日{own:+.2f}% 相关{cc:+.2f}")
    drop = [r for r in rows if r not in keep]
    print("\n不建议入选:")
    for name, ind, up, ex, own, cc, tot, n in drop:
        why = "大跌日自己也跌" if own <= 0 else "逆势胜率低"
        print(f"  ❌ {name:<10}{ind:<7} 胜率{up:.0%} 大跌日{own:+.2f}%  ({why})")
