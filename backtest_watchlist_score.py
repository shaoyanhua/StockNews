# -*- coding: utf-8 -*-
"""
换赛道雷达评分 · 历史回测
==========================
核心问题: 当天评分高的股票, 第二天是不是真的更强? (评分能否指导换赛道)

方法:
  - 直接复用 webapp.py 线上评分代码 (_trend_features / _classify_wave / _score_stock
    / _is_strong), 不重新实现, 避免口径漂移
  - 信号: 用截至 T 日收盘可得的数据算分 (前复权日K + T日涨幅 + T日资金流)
  - 结果: T+1 日收益 (前复权收盘价, 规避除权跳空)
  - 基准: 同池24只等权平均收益 —— 只有跑赢基准才叫"评分有用",
          反攻行情里所有股票都涨, 绝对收益为正毫无意义

已知局限(必须随结果一起看):
  1) 涨速项(±1.8分)无法还原: 线上靠服务端内存的5分钟滚动样本, 历史无此数据。
     回测跑的是"去掉涨速项"的评分, 对应线上刚启动/首次采样的状态。
  2) 资金流为东财**全天**口径; 线上盘中拿到的是当时累计值。回测因此略偏乐观。
  3) 样本极小且处于单一市场状态(股灾后反攻), 不足以外推到震荡/下跌市。

用法: python backtest_watchlist_score.py [--days 6]
"""
import argparse
import json
import subprocess
import sys
import time
from collections import defaultdict

from predict_updown import tencent_kline, prefix
from webapp import WATCHLIST, _trend_features, _classify_wave, _score_stock, _is_strong


def fetch_klines(code, n=120):
    """前复权日K + 不复权成交额。返回 {date: {close, amount}} 与日期升序列表。"""
    qfq = tencent_kline(code, n=n, fq="qfq")          # [(date, close)] 前复权
    raw = _raw_bars(code, n)                           # 成交额(元)
    return qfq, raw


def _raw_bars(code, n):
    """不复权日K取成交额(腾讯第6列为成交量(手), 用 收盘*量*100 近似成交额)。"""
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={prefix(code)}{code},day,,,{n},")
    out = {}
    try:
        txt = _curl(url)
        d = json.loads(txt)["data"][f"{prefix(code)}{code}"]
        for b in (d.get("day") or []):
            if len(b) > 5:
                out[b[0]] = float(b[2]) * float(b[5]) * 100
    except Exception:
        pass
    return out


def _curl(url, referer=None):
    """东财/腾讯部分端点对 requests 的 TLS 指纹做风控(见 webapp._em_batch_quote 注释),
    curl 可通, 回测这种离线批量场景直接走 curl。"""
    cmd = ["curl", "-s", "--max-time", "20", "-A", "Mozilla/5.0"]
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    cmd.append(url)
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8").stdout


def fetch_fund_flow(code, lmt=30):
    """东财日级主力净流入。返回 {date: main_net_yuan}"""
    mkt = 1 if code.startswith(("6", "9")) else 0
    url = (f"https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
           f"?secid={mkt}.{code}&lmt={lmt}&fields1=f1,f2,f3,f7"
           f"&fields2=f51,f52,f53,f54,f55,f56")
    out = {}
    try:
        d = json.loads(_curl(url, "https://quote.eastmoney.com/"))["data"]
        for line in d["klines"]:
            p = line.split(",")
            out[p[0]] = float(p[1])
        return out
    except Exception:
        return out


CACHE_FILE = "cache_predict/backtest_watchlist_data.json"


def build(days, refresh=False):
    """拉取并缓存全部数据。务必只调用一次 —— A/B 两组必须跑在同一份数据上,
    否则东财限流导致的资金流缺失会让对比失去意义(且重复请求会加速触发封禁)。"""
    codes = [c for c, _, _ in WATCHLIST]
    names = {c: n for c, n, _ in WATCHLIST}
    branches = {c: b for c, _, b in WATCHLIST}

    cached = {}
    if not refresh:
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                cached = json.load(f)
        except (OSError, ValueError):
            cached = {}

    # 缓存过期判定必须看日期, 不能只看条数: 新交易日到来后条数依然满足,
    # 会永远命中旧缓存且毫无征兆(静默用陈旧数据出结论)。
    newest = None
    try:
        from research_early_signal import latest_trading_date
        newest = latest_trading_date()
    except Exception:
        pass
    if newest and cached:
        stale = [c for c, v in cached.items()
                 if (v.get("px") or [[None]])[-1][0] < newest]
        if stale:
            print(f"  缓存已过期(最新交易日 {newest}), 重新拉取 {len(stale)} 只",
                  file=sys.stderr)
            for c in stale:
                cached.pop(c, None)

    px, amt, flow = {}, {}, {}
    fails = 0          # 熔断: 东财连续失败即停止请求, 继续硬敲只会加重封禁
    tripped = False
    for i, code in enumerate(codes):
        c = cached.get(code) or {}
        # 资金流命中缓存就不再请求东财(限流很紧, 且日级数据当天内不变)
        have_flow = bool(c.get("flow"))
        qfq = [tuple(x) for x in c["px"]] if c.get("px") else None
        if qfq is None:
            qfq, raw = fetch_klines(code)
        else:
            raw = c.get("amt") or {}

        if have_flow:
            fl = c["flow"]
        elif tripped:
            fl = {}
        else:
            fl = fetch_fund_flow(code)
            if fl:
                fails = 0
            else:
                fails += 1
                if fails >= 3:
                    tripped = True
                    print("  ⚠️ 东财连续3次无响应, 已熔断, 本轮不再请求资金流",
                          file=sys.stderr)
            time.sleep(1.6)   # 东财限流铁律: 串行 + ≥1秒间隔, 批量场景调大

        px[code], amt[code], flow[code] = qfq, raw, fl
        print(f"  [{i+1}/{len(codes)}] {names[code]} K线{len(qfq)}根 "
              f"资金流{len(fl)}天{' (缓存)' if have_flow else ''}", file=sys.stderr)

    try:
        import os
        os.makedirs("cache_predict", exist_ok=True)
        merged = {}
        for code in codes:
            old = (cached.get(code) or {})
            merged[code] = {"px": px[code], "amt": amt[code],
                             # 只在这次真拿到时才覆盖, 避免用空结果污染已有缓存
                             "flow": flow[code] or old.get("flow") or {}}
            flow[code] = merged[code]["flow"]
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False)
    except OSError:
        pass

    # 以池内共同交易日为准
    common = None
    for code in codes:
        ds = {d for d, _ in px[code]}
        common = ds if common is None else (common & ds)
    dates = sorted(common)

    # 需要 T 和 T+1 都有数据 -> 信号日取倒数第 days+1 到倒数第 2
    signal_days = dates[-(days + 1):-1]
    return codes, names, branches, px, amt, flow, dates, signal_days


def score_day(code, date, dates, px, amt, flow, use_flow):
    """还原 T 日收盘时刻的评分。"""
    closes = dict(px[code])
    ds = [d for d, _ in px[code]]
    i = ds.index(date)
    cl = [closes[d] for d in ds[:i + 1]]
    if i < 1:
        return None
    pct = (cl[-1] / cl[-2] - 1) * 100

    trend = _trend_features(cl)
    label, is_wave = _classify_wave(trend, pct)
    trend = dict(trend, label=label, is_main_wave=is_wave)

    main_pct = None
    if use_flow:
        net, a = flow[code].get(date), amt[code].get(date)
        if net is not None and a:
            main_pct = net / a * 100     # 主力净流入占成交额% (同 f184 口径)

    # 涨速项历史不可还原 -> 传 None, 该组条件全部不触发(等价于线上"首次采样")
    mom = {"chg_5m": None, "chg_15m": None, "speed_label": "首次采样"}
    score, _ = _score_stock(pct, trend, mom, main_pct)
    return {"score": score, "pct": pct, "label": label,
            "is_wave": is_wave, "main_pct": main_pct,
            "is_strong": _is_strong(score, label, pct, main_pct)}


def run(data, use_flow):
    codes, names, branches, px, amt, flow, dates, signal_days = data
    rows, daily = [], []
    for date in signal_days:
        i = dates.index(date)
        nxt = dates[i + 1]
        recs = []
        for code in codes:
            s = score_day(code, date, dates, px, amt, flow, use_flow)
            if not s:
                continue
            closes = dict(px[code])
            fwd = (closes[nxt] / closes[date] - 1) * 100
            recs.append(dict(code=code, name=names[code], branch=branches[code],
                             date=date, nxt=nxt, fwd=fwd, **s))
        if not recs:
            continue
        base = sum(r["fwd"] for r in recs) / len(recs)
        recs.sort(key=lambda r: -r["score"])
        strong = [r for r in recs if r["is_strong"]]
        top5 = recs[:5]
        bot5 = recs[-5:]
        daily.append({
            "date": date, "nxt": nxt, "n": len(recs), "base": base,
            "strong_n": len(strong),
            "strong_ret": (sum(r["fwd"] for r in strong) / len(strong)) if strong else None,
            "top5": sum(r["fwd"] for r in top5) / len(top5),
            "bot5": sum(r["fwd"] for r in bot5) / len(bot5),
            "recs": recs,
        })
        rows.extend(recs)
    return rows, daily


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
    rx, ry = rank(xs), rank(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** .5
    dy = sum((b - my) ** 2 for b in ry) ** .5
    return num / (dx * dy) if dx and dy else 0.0


def report(rows, daily, tag):
    print(f"\n{'='*78}\n{tag}\n{'='*78}")
    print(f"{'信号日':<12}{'次日':<12}{'基准':>8}{'Top5':>9}{'Bot5':>9}"
          f"{'强势':>7}{'强势收益':>10}{'超额':>9}")
    tot = {"base": [], "top5": [], "strong": []}
    for d in daily:
        sr = d["strong_ret"]
        ex = (sr - d["base"]) if sr is not None else None
        print(f"{d['date']:<12}{d['nxt']:<12}{d['base']:>7.2f}%{d['top5']:>8.2f}%"
              f"{d['bot5']:>8.2f}%{d['strong_n']:>5}只"
              f"{('%.2f%%' % sr) if sr is not None else '     —':>10}"
              f"{('%+.2f%%' % ex) if ex is not None else '     —':>9}")
        tot["base"].append(d["base"])
        tot["top5"].append(d["top5"])
        if sr is not None:
            tot["strong"].append(sr - d["base"])

    n = len(daily)
    print("-" * 78)
    print(f"{'日均':<24}{sum(tot['base'])/n:>7.2f}%{sum(tot['top5'])/n:>8.2f}%")
    if tot["strong"]:
        wins = sum(1 for x in tot["strong"] if x > 0)
        print(f"强势组超额: 日均 {sum(tot['strong'])/len(tot['strong']):+.2f}% · "
              f"跑赢基准 {wins}/{len(tot['strong'])} 天")
    ex5 = [d["top5"] - d["base"] for d in daily]
    w5 = sum(1 for x in ex5 if x > 0)
    print(f"Top5 超额:  日均 {sum(ex5)/len(ex5):+.2f}% · 跑赢基准 {w5}/{len(ex5)} 天")

    ic = [spearman([r["score"] for r in d["recs"]], [r["fwd"] for r in d["recs"]])
          for d in daily]
    icw = sum(1 for x in ic if x > 0)
    print(f"评分-次日收益 秩相关(IC): 日均 {sum(ic)/len(ic):+.3f} · 为正 {icw}/{len(ic)} 天")
    print("  逐日IC: " + " ".join(f"{x:+.2f}" for x in ic))
    return {"ic": sum(ic) / len(ic), "top5_ex": sum(ex5) / len(ex5),
            "strong_ex": (sum(tot["strong"]) / len(tot["strong"])) if tot["strong"] else None}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=6)
    ap.add_argument("--refresh", action="store_true", help="忽略本地缓存重新拉取")
    a = ap.parse_args()

    print(f"拉取 {len(WATCHLIST)} 只标的数据...", file=sys.stderr)
    data = build(a.days, refresh=a.refresh)      # 只拉一次, A/B 共用
    flow = data[5]
    n_flow = sum(1 for c in data[0] if flow.get(c))
    print(f"资金流覆盖: {n_flow}/{len(data[0])} 只\n", file=sys.stderr)

    r2, d2 = run(data, use_flow=False)
    s2 = report(r2, d2, f"B · 仅趋势+涨幅 (东财不可达时的线上降级状态) · 近{a.days}个交易日")

    if n_flow == 0:
        print("\n⚠️ 资金流全部缺失(东财限流), A组跳过。稍后重跑可补齐。", file=sys.stderr)
    else:
        r1, d1 = run(data, use_flow=True)
        s1 = report(r1, d1, f"A · 含资金流 (完整评分, 缺涨速项) · 近{a.days}个交易日 "
                            f"[资金流覆盖 {n_flow}/{len(data[0])}]")
        print(f"\n{'='*78}\n资金流维度的边际贡献 (同一份数据, 仅开关资金项)\n{'='*78}")
        print(f"IC:        含资金 {s1['ic']:+.3f}  vs  无资金 {s2['ic']:+.3f}"
              f"   (差 {s1['ic']-s2['ic']:+.3f})")
        print(f"Top5超额:  含资金 {s1['top5_ex']:+.2f}%  vs  无资金 {s2['top5_ex']:+.2f}%")
        if s1["strong_ex"] is not None and s2["strong_ex"] is not None:
            print(f"强势组超额:含资金 {s1['strong_ex']:+.2f}%  vs  无资金 {s2['strong_ex']:+.2f}%")
        if n_flow < len(data[0]):
            print(f"\n注: 仅 {n_flow}/{len(data[0])} 只有资金流数据, 其余按缺失处理, "
                  f"该对比偏保守。")
