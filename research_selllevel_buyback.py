# -*- coding: utf-8 -*-
"""
做T卖点回测: 挂多高抛出, 才能"卖得掉"且"2天内更低接回"?
=========================================================
两个条件互相矛盾, 必须联合看:
  挂太高 -> 卖不掉(当日最高价够不着)
  挂太低 -> 卖掉了但价格没跌回来, 踏空

对每个候选卖价(相对昨收的涨幅x%), 统计:
  成交率      当日最高价 >= 卖价                       (挂单能成交)
  接回率      成交后, T+1/T+2 的最低价 < 卖价           (能更低买回)
  当日接回率  成交后, 当日收盘价 < 卖价                 (当天就能接回, 不用过夜)
  联合成功率  成交率 × 接回率                          (真正做成一次T的概率)
  期望价差    成交且接回时, (卖价-最低接回价)/卖价的中位数

口径: 前复权日K(除权不影响日内比值与日间收益)。
局限: 用日K的最低价近似"可接回价", 实盘未必挂得到最低点; 且不含手续费。
"""
import argparse
import json
import statistics
import subprocess
import sys

from predict_updown import prefix


def curl_json(url):
    return json.loads(subprocess.run(
        ["curl", "-s", "--max-time", "25", "-A", "Mozilla/5.0", url],
        capture_output=True, text=True, encoding="utf-8").stdout)


def bars(code, n=400):
    sym = f"{prefix(code)}{code}"
    d = curl_json(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                  f"?param={sym},day,,,{n},qfq")["data"][sym]
    b = d.get("qfqday") or d.get("day")
    return [{"date": x[0], "open": float(x[1]), "close": float(x[2]),
             "high": float(x[3]), "low": float(x[4])} for x in b]


def run(code, name, levels, hold_days=2, lookback=250):
    bs = bars(code)[-lookback:]
    print(f"\n{'='*84}")
    print(f"{name} {code}  样本 {len(bs)-hold_days} 天  ({bs[0]['date']} ~ {bs[-1]['date']})")
    print(f"{'='*84}")
    print(f"{'卖价(相对昨收)':<14}{'成交率':>8}{'接回率':>8}{'当日接回':>9}"
          f"{'联合成功率':>10}{'中位价差':>9}{'样本':>6}")
    out = []
    for x in levels:
        filled = back = sameday = 0
        gaps = []
        for i in range(1, len(bs) - hold_days):
            pc = bs[i - 1]["close"]
            sell = pc * (1 + x / 100)
            if bs[i]["high"] < sell:
                continue                      # 挂单没成交
            filled += 1
            lows = [bs[i + k]["low"] for k in range(1, hold_days + 1)]
            lo = min(lows)
            if lo < sell:
                back += 1
                gaps.append((sell - lo) / sell * 100)
            if bs[i]["close"] < sell:
                sameday += 1
        n = len(bs) - hold_days - 1
        if not n:
            continue
        fr = filled / n
        br = (back / filled) if filled else 0
        sr = (sameday / filled) if filled else 0
        joint = fr * br
        med = statistics.median(gaps) if gaps else 0
        out.append((x, fr, br, joint, med, filled))
        print(f"  昨收+{x:>4.1f}%     {fr:>7.0%}{br:>8.0%}{sr:>9.0%}"
              f"{joint:>10.0%}{med:>8.2f}%{filled:>6}")
    if out:
        best = max(out, key=lambda r: r[3])
        print(f"\n  联合成功率最高: 昨收+{best[0]:.1f}%  "
              f"(成交{best[1]:.0%} × 接回{best[2]:.0%} = {best[3]:.0%}, "
              f"中位价差{best[4]:.2f}%)")
        # 兼顾价差的实用解: 联合成功率>=45% 中价差最大的
        prac = [r for r in out if r[3] >= 0.45]
        if prac:
            p = max(prac, key=lambda r: r[4])
            print(f"  兼顾价差(成功率≥45%): 昨收+{p[0]:.1f}%  "
                  f"成功率{p[3]:.0%}, 中位价差{p[4]:.2f}%")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="688256")
    ap.add_argument("--name", default="寒武纪")
    ap.add_argument("--hold", type=int, default=2)
    a = ap.parse_args()
    LV = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0, 7.0, 8.0]
    run(a.code, a.name, LV, hold_days=a.hold)
