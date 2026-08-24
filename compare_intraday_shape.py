# -*- coding: utf-8 -*-
"""
日内形态对比: 突破继续强势 vs 冲高回落走弱
==========================================
用两个真实案例对照, 提炼可操作的判别依据:
  A) 中际旭创 2026-08-04  当日+13.24% -> 次日 -7.27%   (回落走弱的样本)
  B) 寒武纪   2026-08-13  今日走势                      (待判定)

拆解维度(全部为盘中可实时观测):
  分时价格路径     每30分钟收盘价相对昨收
  VWAP位置/斜率    站上还是跌破分时均价线, 均价线在抬升还是走平
  主力资金累积     分笔重构(特大单≥100万, 大单20-100万)逐时段累计
  量能分布         上午/下午成交额占比, 高位放量还是缩量
  尾盘表现         14:30后相对日内高点的位置
  收盘位置         收盘价在当日振幅区间的百分位(收在高位还是低位)
"""
import sys
from datetime import datetime

from predict_updown import (tdx_connect, day_ticks, flow_at, amount_at,
                            tencent_kline, prefix)

SLOTS = ["09:35", "09:45", "10:00", "10:30", "11:00", "11:30",
         "13:30", "14:00", "14:30", "14:50", "15:00"]


def price_at(ticks, t):
    p = None
    for x in ticks:
        if x["time"] <= t:
            p = x["price"]
        else:
            break
    return p


def vwap_at(ticks, t):
    v = a = 0.0
    for x in ticks:
        if x["time"] > t:
            break
        v += x["vol"]
        a += x["price"] * x["vol"]
    return (a / v) if v else None


def analyze(api, code, name, date, prev_close):
    tk = day_ticks(api, code, int(date.replace("-", "")))
    if not tk:
        print(f"  {name} {date}: 无分笔数据")
        return None
    hi = max(x["price"] for x in tk)
    lo = min(x["price"] for x in tk)
    close = tk[-1]["price"]
    open_p = tk[0]["price"]
    total_amt = amount_at(tk, "15:00")

    print(f"\n{'='*82}")
    print(f"{name} {code}  {date}   昨收 {prev_close:.2f}")
    print(f"{'='*82}")
    print(f"开{open_p:.2f}({(open_p/prev_close-1)*100:+.2f}%) "
          f"高{hi:.2f}({(hi/prev_close-1)*100:+.2f}%) "
          f"低{lo:.2f}({(lo/prev_close-1)*100:+.2f}%) "
          f"收{close:.2f}({(close/prev_close-1)*100:+.2f}%)")
    pos = (close - lo) / (hi - lo) * 100 if hi > lo else 50
    print(f"收盘位于当日振幅 {pos:.0f}% 分位  ({'收在高位' if pos>=70 else ('收在低位' if pos<=30 else '中位')})")
    print(f"自日内高点回撤 {(close/hi-1)*100:+.2f}%   全天成交额 {total_amt/1e8:.1f}亿")

    print(f"\n{'时点':<8}{'价格':>9}{'涨幅':>8}{'VWAP':>9}{'距VWAP':>8}"
          f"{'主力净(亿)':>11}{'主力占比':>9}{'距日内高':>9}")
    rows = []
    prev_main = 0.0
    for t in SLOTS:
        p = price_at(tk, t)
        if p is None:
            continue
        vw = vwap_at(tk, t)
        _sup, main, _ = flow_at(tk, t)
        amt = amount_at(tk, t)
        mp = (main * 1e8 / amt * 100) if amt else 0
        hi_sofar = max(x["price"] for x in tk if x["time"] <= t)
        rows.append({"t": t, "p": p, "pct": (p / prev_close - 1) * 100,
                     "vwap": vw, "dvw": (p / vw - 1) * 100 if vw else 0,
                     "main": main, "mp": mp,
                     "dhi": (p / hi_sofar - 1) * 100,
                     "dmain": main - prev_main})
        prev_main = main
        r = rows[-1]
        flag = ""
        if r["dvw"] < 0:
            flag = " ⚠破均价"
        print(f"{t:<8}{p:>9.2f}{r['pct']:>7.2f}%{vw:>9.2f}{r['dvw']:>7.2f}%"
              f"{main:>11.2f}{mp:>8.1f}%{r['dhi']:>8.2f}%{flag}")

    # 上下午量能分布
    am = amount_at(tk, "11:30")
    pm = total_amt - am
    print(f"\n量能分布: 上午 {am/1e8:.1f}亿 ({am/total_amt*100:.0f}%) | "
          f"下午 {pm/1e8:.1f}亿 ({pm/total_amt*100:.0f}%)")
    return {"rows": rows, "hi": hi, "close": close, "pos": pos,
            "pct": (close / prev_close - 1) * 100}


def verdict(name, r):
    """按可量化规则给判别结论。"""
    rows = r["rows"]
    last = rows[-1]
    # 1) 收盘位置
    c1 = r["pos"] >= 70
    # 2) 全天是否始终站上VWAP
    below = [x["t"] for x in rows if x["dvw"] < 0]
    c2 = len(below) == 0
    # 3) VWAP是否持续抬升(用首尾比较)
    c3 = rows[-1]["vwap"] > rows[0]["vwap"]
    # 4) 主力资金占比收盘是否为正且未大幅衰减
    peak_mp = max(x["mp"] for x in rows)
    c4 = last["mp"] > 0 and last["mp"] >= peak_mp * 0.6
    # 5) 尾盘(14:30后)是否守住高点附近
    tail = [x for x in rows if x["t"] >= "14:30"]
    c5 = bool(tail) and min(x["dhi"] for x in tail) > -3
    print(f"\n【{name} 判别】")
    for ok, txt in ((c1, f"收盘在振幅{r['pos']:.0f}%分位(≥70%为强)"),
                    (c2, f"全天站稳分时均价线" + (f" [破线时点: {','.join(below)}]" if below else "")),
                    (c3, "分时均价线全天抬升"),
                    (c4, f"主力占比收盘{last['mp']:+.1f}%(峰值{peak_mp:+.1f}%)未大幅衰减"),
                    (c5, "尾盘守住日内高点3%以内")):
        print(f"  {'✅' if ok else '❌'} {txt}")
    n = sum([c1, c2, c3, c4, c5])
    tag = "突破继续强势" if n >= 4 else ("多空拉锯" if n == 3 else "冲高回落走弱")
    print(f"  → {n}/5 项成立 ⇒ 【{tag}】")
    return n


if __name__ == "__main__":
    api = tdx_connect()
    try:
        cases = [("300308", "中际旭创", "2026-08-04"),
                 ("688256", "寒武纪", "2026-08-13")]
        res = {}
        for code, name, date in cases:
            k = tencent_kline(code, n=120, fq="")   # 不复权,与分笔原始价同口径
            ds = [d for d, _ in k]
            cl = dict(k)
            if date not in ds:
                print(f"{name}: {date} 非交易日或数据缺失", file=sys.stderr)
                continue
            prev = cl[ds[ds.index(date) - 1]]
            res[name] = analyze(api, code, name, date, prev)
        print("\n" + "=" * 82)
        print("判别对照")
        print("=" * 82)
        for name in res:
            if res[name]:
                verdict(name, res[name])
    finally:
        api.disconnect()
