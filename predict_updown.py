# -*- coding: utf-8 -*-
"""
当日涨跌可能性估计器
====================
依据: 个股涨跌幅 与 [板块涨跌幅]、[特大单净流入]、[主力净流入] 的高度相关性。
在指定时点(默认10:30)采集当日盘面特征, 用过去N个交易日训练逻辑回归, 输出收涨概率。

数据源(全部免费, 无东财依赖):
  - 涨跌幅/K线      : 腾讯财经 (不封IP)
  - 行业归属/板块代理: 通达信 tdxhy.cfg + 同行业成交额Top3代表股
  - 资金流(特大/主力): 通达信历史分笔重构 (特大单>=100万, 大单20-100万, 主力=特大+大)

用法:
  python predict_updown.py 688256                     # 实时(盘中)估计今天
  python predict_updown.py 688256 --date 2026-07-17   # 回看某天(用当天10:30数据)
  python predict_updown.py 688256 --cutoff 11:30 --train 40

口径说明: 分笔重构无法还原订单级拆单, 资金流绝对值与东财APP不可直接对照,
但训练与预测同一口径, 模型内部自洽。
"""
import argparse
import csv
import json
import math
import os
import socket
import sys
import time

import requests

try:
    from pytdx.hq import TdxHq_API
except ImportError:
    sys.exit("需要 pytdx: pip install pytdx")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
TDX_SERVERS = [("115.238.56.198", 7709), ("115.238.90.165", 7709),
               ("180.153.18.170", 7709), ("60.191.117.167", 7709),
               ("119.97.185.59", 7709), ("124.70.133.119", 7709),
               ("116.205.183.150", 7709), ("123.60.73.44", 7709),
               ("116.205.163.254", 7709), ("121.36.225.169", 7709)]
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache_predict")
os.makedirs(CACHE, exist_ok=True)

S = requests.Session()
S.trust_env = False  # 绕过系统代理


class RemoteDataError(RuntimeError):
    """远端行情不可用，且没有可用的本地缓存。"""


def _kline_cache_path(code, n, fq):
    suffix = fq or "raw"
    return os.path.join(CACHE, f"tencent_kline_{prefix(code)}{code}_{suffix}_{n}.json")


def _tencent_kline_bars(code, n=90, fq="qfq"):
    """腾讯日K：短重试后回退最近一次成功缓存。"""
    p = prefix(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={p}{code},day,,,{n},{fq}"
    cache_path = _kline_cache_path(code, n, fq)
    last_error = None
    for attempt in range(3):
        try:
            response = S.get(url, headers={"User-Agent": UA}, timeout=(5, 15))
            response.raise_for_status()
            payload = response.json()
            stock_data = payload["data"][f"{p}{code}"]
            bars = stock_data.get("qfqday") or stock_data.get("day")
            if not bars:
                raise ValueError("返回中没有日K数据")
            tmp_path = cache_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(bars, f, ensure_ascii=False)
            os.replace(tmp_path, cache_path)
            return bars
        except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))

    try:
        with open(cache_path, encoding="utf-8") as f:
            bars = json.load(f)
        if bars:
            return bars
    except (OSError, ValueError, TypeError):
        pass
    raise RemoteDataError(
        f"腾讯日K接口暂不可用，且无本地缓存（{code}）：{type(last_error).__name__}: {last_error}"
    ) from last_error


# ───────────────────────── 基础工具 ─────────────────────────

def prefix(code):
    return "sh" if code.startswith(("6", "9")) else ("bj" if code.startswith("8") else "sz")

def tdx_market(code):
    return 1 if prefix(code) == "sh" else 0

def tdx_connect():
    api = TdxHq_API()
    for ip, port in TDX_SERVERS:
        try:
            with socket.create_connection((ip, port), timeout=2):
                pass
            if api.connect(ip, port):
                return api
        except Exception:
            continue
    raise RuntimeError("通达信服务器均不可达")


# ───────────────────────── 数据采集 ─────────────────────────

def tencent_kline(code, n=90, fq="qfq"):
    """日K: [(date, close)]. fq="qfq"前复权(算涨跌幅), fq=""不复权(与分笔原始价同口径)"""
    bars = _tencent_kline_bars(code, n, fq)
    return [(b[0], float(b[2])) for b in bars]

def tencent_realtime(codes):
    """实时Level-1行情；五档盘口为腾讯公开快照，可能有秒级延迟。"""
    import time as _time
    out = {}
    for i in range(0, len(codes), 50):
        batch = [prefix(c) + c for c in codes[i:i + 50]]
        r = None
        for attempt in range(3):
            try:
                r = S.get("https://qt.gtimg.cn/q=" + ",".join(batch),
                          headers={"User-Agent": UA}, timeout=15)
                break
            except requests.RequestException:
                if attempt == 2:
                    raise
                _time.sleep(2)
        for line in r.content.decode("gbk", "ignore").strip().split(";"):
            if '"' not in line:
                continue
            key = line.split("=")[0].split("_")[-1]
            v = line.split('"')[1].split("~")
            if len(v) < 40:
                continue
            def num(i):
                try:
                    return float(v[i] or 0)
                except (ValueError, IndexError):
                    return 0.0

            bids = [{"price": num(9 + i * 2), "volume": num(10 + i * 2)}
                    for i in range(5)]
            asks = [{"price": num(19 + i * 2), "volume": num(20 + i * 2)}
                    for i in range(5)]
            bid_vol, ask_vol = sum(x["volume"] for x in bids), sum(x["volume"] for x in asks)
            depth_imbalance = ((bid_vol - ask_vol) / (bid_vol + ask_vol) * 100
                               if bid_vol + ask_vol else None)
            out[key[2:]] = {
                "name": v[1], "price": num(3), "last_close": num(4), "open": num(5),
                "pct": num(32), "high": num(33), "low": num(34),
                "amount_wan": num(37), "turnover": num(38),
                "bids": bids, "asks": asks, "depth_imbalance": depth_imbalance,
            }
    return out

def tencent_kline_vol(code, n=90):
    """日K成交量(手, 不复权): {date: vol} — 用于校验分笔完整性"""
    bars = _tencent_kline_bars(code, n, "")
    return {b[0]: float(b[5]) for b in bars if len(b) > 5}

def em_flow_check(code, date=None):
    """东财官方资金流对照通道(可达时). 返回 {main, super, scope} 或 None(被封/超时)"""
    mkt = 1 if prefix(code) == "sh" else 0
    headers = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}
    try:
        if date:  # 历史日: 官方日级值
            r = S.get("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
                      params={"secid": f"{mkt}.{code}", "lmt": "120",
                              "fields1": "f1,f2,f3,f7",
                              "fields2": "f51,f52,f53,f54,f55,f56"},
                      headers=headers, timeout=4)
            for line in r.json()["data"]["klines"]:
                p = line.split(",")
                if p[0] == date:
                    return {"main": round(float(p[1]) / 1e8, 2),
                            "super": round(float(p[5]) / 1e8, 2), "scope": "官方全天"}
        else:     # 实时: 官方分钟级累计
            r = S.get("https://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
                      params={"secid": f"{mkt}.{code}", "klt": "1", "lmt": "0",
                              "fields1": "f1,f2,f3,f7",
                              "fields2": "f51,f52,f53,f54,f55,f56"},
                      headers=headers, timeout=4)
            last = r.json()["data"]["klines"][-1].split(",")
            return {"main": round(float(last[1]) / 1e8, 2),
                    "super": round(float(last[5]) / 1e8, 2),
                    "scope": f"官方截至{last[0][-5:]}"}
    except Exception:
        return None
    return None

def tdx_industry_peers(api, code):
    """下载tdxhy.cfg -> (本股T码, 同行业股票列表[(market,code)])"""
    fn = os.path.join(CACHE, "tdxhy.cfg")
    if os.path.exists(fn):
        raw = open(fn, "rb").read()
    else:
        raw, offset = b"", 0
        while True:
            d = api.get_report_file("tdxhy.cfg", offset)
            chunk = d.get("chunkdata") if d else None
            if not chunk:
                break
            raw += chunk
            offset += len(chunk)
        open(fn, "wb").write(raw)
    tcode, peers = None, []
    rows = []
    for ln in raw.decode("gbk", "ignore").splitlines():
        p = ln.split("|")
        if len(p) >= 3 and p[2]:
            rows.append((p[0], p[1], p[2]))
            if p[1] == code:
                tcode = p[2][:5]  # 二级行业前缀 如 T1203
    if not tcode:
        return None, []
    for mkt, c, t in rows:
        if t.startswith(tcode) and c != code and c[0] in "036":
            peers.append(c)
    return tcode, peers

def pick_board_proxies(code, peers, k=3, return_quotes=False):
    """同行业成交额Top-k作为板块代理股; return_quotes=True 时同时返回全体同业实时行情"""
    q = tencent_realtime(peers[:120])
    ranked = sorted(q.items(), key=lambda kv: -kv[1]["amount_wan"])
    proxies = [c for c, _ in ranked[:k]]
    return (proxies, q) if return_quotes else proxies

def day_ticks(api, code, date_int):
    """某日全天分笔(带缓存)"""
    fn = os.path.join(CACHE, f"ticks_{code}_{date_int}.csv")
    if os.path.exists(fn):
        out = [{"time": r[0], "price": float(r[1]), "vol": int(r[2]),
                "buyorsell": int(r[3])} for r in csv.reader(open(fn, encoding="utf-8"))]
        out.sort(key=lambda t: t["time"])  # 分页块顺序乱, 必须按时间排序
        return out
    out, start = [], 0
    while True:
        chunk = api.get_history_transaction_data(tdx_market(code), code, start, 2000, date_int)
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 2000:
            break
        start += len(chunk)
    out.sort(key=lambda t: t["time"])  # 分页块顺序乱, 必须按时间排序
    with open(fn, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for t in out:
            w.writerow([t["time"], t["price"], t["vol"], t["buyorsell"]])
    return out

def _today_ticks_from(api, code):
    out, start = [], 0
    while True:
        chunk = api.get_transaction_data(tdx_market(code), code, start, 2000)
        if not chunk:
            break
        out = chunk + out
        if len(chunk) < 2000:
            break
        start += len(chunk)
    out.sort(key=lambda t: t["time"])
    return out

def today_ticks(api, code):
    """今日实时分笔(不缓存); 部分服务器不推当日分笔, 取不到时自动换备用服务器"""
    out = _today_ticks_from(api, code)
    if out:
        return out
    for ip, port in TDX_SERVERS:
        try:
            with socket.create_connection((ip, port), timeout=2):
                pass
            alt = TdxHq_API()
            if not alt.connect(ip, port):
                continue
            out = _today_ticks_from(alt, code)
            alt.disconnect()
            if out:
                return out
        except Exception:
            continue
    return []

def amount_at(ticks, cutoff):
    """截止cutoff的累计成交额(元)"""
    return sum(t["price"] * t["vol"] * 100 for t in ticks if t["time"] <= cutoff)

def flow_at(ticks, cutoff):
    """截止cutoff的 (特大单净, 主力净, 最后价) 单位亿"""
    sup = lg = 0.0
    last_price = None
    for t in ticks:
        if t["time"] > cutoff:
            continue
        last_price = t["price"]
        b = t["buyorsell"]
        sign = 1 if b == 0 else (-1 if b == 1 else 0)
        if not sign:
            continue
        amt = t["price"] * t["vol"] * 100
        if amt >= 1_000_000:
            sup += sign * amt
        elif amt >= 200_000:
            lg += sign * amt
    return sup / 1e8, (sup + lg) / 1e8, last_price


# ───────────────────────── 逻辑回归(纯python) ─────────────────────────

def logistic_fit(X, y, iters=800, lr=0.3, l2=0.01, nonneg=True):
    """nonneg=True: 系数投影到>=0 (板块/资金流/自身与涨跌的关系原理上非负,
    防止共线性把资金流系数翻负学出"流出看多"的病态权重)"""
    n, m = len(X), len(X[0])
    mean = [sum(r[j] for r in X) / n for j in range(m)]
    std = [max(1e-9, math.sqrt(sum((r[j] - mean[j]) ** 2 for r in X) / n)) for j in range(m)]
    Z = [[(r[j] - mean[j]) / std[j] for j in range(m)] for r in X]
    w = [0.0] * m
    b = 0.0
    for _ in range(iters):
        gw, gb = [0.0] * m, 0.0
        for zi, yi in zip(Z, y):
            p = 1 / (1 + math.exp(-max(-30, min(30, sum(wj * zj for wj, zj in zip(w, zi)) + b))))
            e = p - yi
            for j in range(m):
                gw[j] += e * zi[j]
            gb += e
        for j in range(m):
            w[j] -= lr * (gw[j] / n + l2 * w[j])
            if nonneg and w[j] < 0:
                w[j] = 0.0
        b -= lr * gb / n
    return {"w": w, "b": b, "mean": mean, "std": std}

def _zclamp(x, model, lim=2.5):
    """标准化+截断到训练分布±lim个标差, 防止极端日外推失效"""
    return [max(-lim, min(lim, (x[j] - model["mean"][j]) / model["std"][j]))
            for j in range(len(x))]

def logistic_prob(model, x):
    z = _zclamp(x, model)
    s = sum(wj * zj for wj, zj in zip(model["w"], z)) + model["b"]
    return 1 / (1 + math.exp(-max(-30, min(30, s))))

def softmax_fit(X, labels, k=3, iters=800, lr=0.3, l2=0.01):
    """多分类softmax回归: labels in 0..k-1"""
    n, m = len(X), len(X[0])
    mean = [sum(r[j] for r in X) / n for j in range(m)]
    std = [max(1e-9, math.sqrt(sum((r[j] - mean[j]) ** 2 for r in X) / n)) for j in range(m)]
    Z = [[(r[j] - mean[j]) / std[j] for j in range(m)] for r in X]
    W = [[0.0] * m for _ in range(k)]
    B = [0.0] * k
    for _ in range(iters):
        gW = [[0.0] * m for _ in range(k)]
        gB = [0.0] * k
        for zi, yi in zip(Z, labels):
            ss = [sum(W[c][j] * zi[j] for j in range(m)) + B[c] for c in range(k)]
            mx = max(ss)
            ex = [math.exp(s - mx) for s in ss]
            tot = sum(ex)
            for c in range(k):
                e = ex[c] / tot - (1 if yi == c else 0)
                for j in range(m):
                    gW[c][j] += e * zi[j]
                gB[c] += e
        for c in range(k):
            for j in range(m):
                W[c][j] -= lr * (gW[c][j] / n + l2 * W[c][j])
            B[c] -= lr * gB[c] / n
    return {"W": W, "B": B, "mean": mean, "std": std, "k": k}

def softmax_prob(model, x):
    m = len(x)
    z = _zclamp(x, model)
    ss = [sum(model["W"][c][j] * z[j] for j in range(m)) + model["B"][c]
          for c in range(model["k"])]
    mx = max(ss)
    ex = [math.exp(s - mx) for s in ss]
    tot = sum(ex)
    return [e / tot for e in ex]

def ridge_fit(X, y, l2=0.5, nonneg=True, iters=2000, lr=0.1):
    """岭回归(投影梯度, nonneg=True时系数>=0) -> 预测连续涨跌幅"""
    n, m = len(X), len(X[0])
    mean = [sum(r[j] for r in X) / n for j in range(m)]
    std = [max(1e-9, math.sqrt(sum((r[j] - mean[j]) ** 2 for r in X) / n)) for j in range(m)]
    Z = [[(r[j] - mean[j]) / std[j] for j in range(m)] for r in X]
    my = sum(y) / n
    yc = [v - my for v in y]
    w = [0.0] * m
    for _ in range(iters):
        resid = [sum(w[j] * Z[i][j] for j in range(m)) - yc[i] for i in range(n)]
        for j in range(m):
            g = sum(resid[i] * Z[i][j] for i in range(n)) / n + l2 * w[j] / n
            w[j] -= lr * g
            if nonneg and w[j] < 0:
                w[j] = 0.0
    pred = [sum(w[j] * Z[i][j] for j in range(m)) + my for i in range(n)]
    ss_res = sum((p - yv) ** 2 for p, yv in zip(pred, y))
    ss_tot = sum((yv - my) ** 2 for yv in y) or 1e-9
    return {"w": w, "b": my, "mean": mean, "std": std,
            "resid_std": math.sqrt(ss_res / max(1, n - m - 1)),
            "r2": 1 - ss_res / ss_tot}

def ridge_pred(model, x):
    z = _zclamp(x, model)
    return sum(wj * zj for wj, zj in zip(model["w"], z)) + model["b"]

def _phi(x):
    """标准正态CDF"""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def probs3_ensemble(model3, modelr, x, thr):
    """三分类概率 = softmax(截断) 与 回归正态近似 的均值集成.
    正态近似: 全天涨跌 ~ N(ridge预测, 残差σ), 极端日更稳健."""
    sm = softmax_prob(model3, x)
    mu, sd = ridge_pred(modelr, x), max(0.5, modelr["resid_std"])
    ps = _phi((-thr - mu) / sd)
    pb = 1 - _phi((thr - mu) / sd)
    ph = max(0.0, 1 - ps - pb)
    na = [ps, ph, pb]
    mix = [(a + b) / 2 for a, b in zip(sm, na)]
    tot = sum(mix)
    return [v / tot for v in mix]

def pearson(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return 0.0 if sx * sy == 0 else sum((a - mx) * (b - my)
                                        for a, b in zip(xs, ys)) / (sx * sy)


# ───────────────────────── 主流程 ─────────────────────────

def run_prediction(code, date=None, cutoff="10:30", train=60, thr=None,
                   progress=lambda msg: None):
    """完整预测流程, 返回结构化dict(供CLI与网页共用). 失败抛ValueError."""
    progress(f"[1/5] 拉取 {code} 日K与行业归属...")
    kdepth = 350  # 足够覆盖 回看日期+训练窗
    kline = tencent_kline(code, n=kdepth)
    closes_raw = dict(tencent_kline(code, n=kdepth, fq=""))  # 不复权, 与分笔同口径
    closes = dict(kline)
    dates = [d for d, _ in kline]
    api = tdx_connect()
    tcode, peers = tdx_industry_peers(api, code)
    if not peers:
        api.disconnect()
        raise ValueError("未找到行业归属")
    proxies, peer_quotes = pick_board_proxies(code, peers, return_quotes=True)
    pq = tencent_realtime(proxies + [code])
    pnames = [pq[c]["name"] for c in proxies if c in pq]
    progress(f"      行业 {tcode}, 板块代理股: {'/'.join(pnames)}")

    # 训练窗口: 目标日之前的 train 个交易日
    if date:
        if date not in dates:
            api.disconnect()
            raise ValueError(f"{date} 不是交易日或超出K线范围")
        di = dates.index(date)
    else:
        di = len(dates)  # 今天(不在历史K线里)
    train_dates = dates[max(1, di - train):di]

    proxy_kline = {p: dict(tencent_kline(p, n=kdepth, fq="")) for p in proxies}  # 不复权
    proxy_dates = {p: sorted(proxy_kline[p]) for p in proxies}

    def prev_close(series_dates, series, d):
        i = series_dates.index(d) if d in series_dates else -1
        return series[series_dates[i - 1]] if i > 0 else None

    progress(f"[2/5] 重构训练集 {len(train_dates)} 天 x (1股+{len(proxies)}代理) 分笔...")
    X, Y, rows = [], [], []
    for d in train_dates:
        di_int = int(d.replace("-", ""))
        ticks = day_ticks(api, code, di_int)
        if not ticks:
            continue
        s_c, m_c, px = flow_at(ticks, cutoff)
        pc = prev_close(dates, closes, d)              # 前复权: 算目标涨跌幅
        pc_raw = prev_close(dates, closes_raw, d)      # 不复权: 与分笔价对比
        if not (pc and pc_raw and px):
            continue
        self_c = (px / pc_raw - 1) * 100
        bps = []
        for p in proxies:
            pt = day_ticks(api, p, di_int)
            ppc = prev_close(proxy_dates[p], proxy_kline[p], d)
            if pt and ppc:
                _, _, ppx = flow_at(pt, cutoff)
                if ppx:
                    bps.append((ppx / ppc - 1) * 100)
        if not bps:
            continue
        board_c = sum(bps) / len(bps)
        day_pct = (closes[d] / pc - 1) * 100
        X.append([board_c, m_c, s_c, self_c])
        Y.append(1 if day_pct > 0 else 0)
        rows.append((d, board_c, m_c, s_c, self_c, day_pct))

    if len(X) < 15:
        api.disconnect()
        raise ValueError(f"有效训练样本不足({len(X)})")

    pcts = [r[5] for r in rows]
    fnames = ["board", "main", "super", "self"]
    corrs = {nm: round(pearson([r[1 + j] for r in rows], pcts), 2)
             for j, nm in enumerate(fnames)}
    progress(f"[3/5] 特征相关性: {corrs}")

    progress("[4/5] 训练模型...")
    model = logistic_fit(X, Y)
    hit = sum(1 for xv, yv in zip(X, Y) if (logistic_prob(model, xv) >= 0.5) == (yv == 1))
    coefs = {nm: round(w, 2) for nm, w in zip(fnames, model["w"])}

    # 三分类: 0=显著下跌 1=震荡 2=显著上涨, 阈值按个股波动自适应
    abs_pcts = sorted(abs(r[5]) for r in rows)
    if thr is None:
        thr = round(abs_pcts[len(abs_pcts) // 2] / 2, 2)
    labels = [0 if r[5] < -thr else (2 if r[5] > thr else 1) for r in rows]
    model3 = softmax_fit(X, labels)
    modelr = ridge_fit(X, [r[5] for r in rows])

    # 目标日特征
    progress(f"[5/5] 目标日特征 (截止 {cutoff})...")
    if date:
        di_int = int(date.replace("-", ""))
        ticks = day_ticks(api, code, di_int)
        pc = prev_close(dates, closes, date)
        pc_raw = prev_close(dates, closes_raw, date)
        bps = []
        for p in proxies:
            pt = day_ticks(api, p, di_int)
            ppc = prev_close(proxy_dates[p], proxy_kline[p], date)
            if pt and ppc:
                _, _, ppx = flow_at(pt, cutoff)
                if ppx:
                    bps.append((ppx / ppc - 1) * 100)
    else:  # 今天实时
        ticks = today_ticks(api, code)
        pc = pc_raw = pq[code]["last_close"]
        rq = tencent_realtime(proxies)
        bps = [rq[p]["pct"] for p in proxies if p in rq]
    api.disconnect()
    s_c, m_c, px = flow_at(ticks, cutoff)
    if not ticks:
        raise ValueError("未取到当日分笔(非交易日/未开盘/通达信服务器无实时数据?)")
    if not (pc and pc_raw):
        raise ValueError("昨收价获取失败(腾讯行情异常?)")
    if not px:
        raise ValueError(f"截止{cutoff}无成交(未开盘?)")
    if not bps:
        raise ValueError("板块代理股行情获取失败")
    self_c = (px / pc_raw - 1) * 100
    board_c = sum(bps) / len(bps)

    # ── 数据可靠性: 多通道交叉校验 ──
    quality = {"flow_channel": "通达信分笔重构(主通道)",
               "ticks": len(ticks),
               "coverage": f"{ticks[0]['time']}~{ticks[-1]['time']}"}
    # 校验1: 分笔总量 vs 腾讯日K成交量(独立通道) — 检测分笔缺漏
    try:
        tick_vol = sum(t["vol"] for t in ticks)
        if date:
            kv = tencent_kline_vol(code, kdepth).get(date)
        else:
            kv = None  # 实时盘中用成交额校验
        if kv:
            quality["vol_ratio"] = round(tick_vol / kv, 3)
            quality["vol_ok"] = 0.95 <= quality["vol_ratio"] <= 1.05
    except Exception:
        pass
    # 校验2: 东财官方资金流(对照通道, 口径不同仅看方向/量级)
    em = em_flow_check(code, date)
    quality["em_check"] = em if em else "东财不可达(备用对照源, 不影响主通道)"
    if em:
        quality["em_direction_agree"] = (em["main"] > 0) == (m_c > 0)
    # 校验3: 板块第二通道 — 全体同业平均(实时) vs Top3代理
    quality["board_channel"] = f"同业成交额Top3代理(n={len(bps)}, 与训练口径一致)"
    if not date and peer_quotes:
        peer_pcts = [v["pct"] for v in peer_quotes.values() if v.get("pct") is not None]
        if len(peer_pcts) >= 10:
            avg2 = sum(peer_pcts) / len(peer_pcts)
            quality["board_peers_avg"] = round(avg2, 2)
            quality["board_agree"] = abs(avg2 - board_c) <= 1.5
    x = [board_c, m_c, s_c, self_c]
    prob = logistic_prob(model, x)
    p3 = probs3_ensemble(model3, modelr, x, thr)  # softmax+正态近似集成, 抗极端日
    pred_pct = ridge_pred(modelr, x)

    # 建议规则(回测校准): 最大概率>=60%才行动; 二分类极端时兜底
    tag = ["卖出/回避", "观望", "买入"]
    cls_pred = p3.index(max(p3))
    if max(p3) >= 0.6:
        advice = tag[cls_pred]
    elif prob <= 0.2:
        advice = "卖出/回避(二分类强信号)"
    elif prob >= 0.8:
        advice = "买入(二分类强信号)"
    else:
        advice = "观望(置信不足)"

    # 资金-价格背离守卫: 巨量资金与价格不配合是非线性信号(出货/买不动),
    # 线性模型学不到且概率会失真 —— 用训练窗分位数触发(z分数对巨量股失灵),
    # 显式警示并禁止买入类建议
    train_ms = sorted(r[2] for r in rows)
    k10 = max(1, len(train_ms) // 10)
    p10, p90 = train_ms[k10 - 1], train_ms[-k10]
    amt = amount_at(ticks, cutoff)
    strength = m_c * 1e8 / amt * 100 if amt else 0  # 主力净流入占成交额%
    warning = None
    if (m_c <= p10 or strength <= -8) and self_c > -1.5:
        warning = (f"主力流出{m_c:+.1f}亿(占成交额{strength:+.0f}%, 训练窗最差10%分位"
                   f"{p10:+.1f}亿)但价格未大跌 — 典型出货形态, "
                   "上方概率可能失真, 建议回避")
    elif (m_c >= p90 or strength >= 8) and self_c < 1.5:
        warning = (f"主力流入{m_c:+.1f}亿(占成交额{strength:+.0f}%, 训练窗最强10%分位"
                   f"{p90:+.1f}亿)但价格滞涨 — 买不动形态, "
                   "上方概率可能失真, 谨慎追买")
    if warning and advice.startswith("买"):
        advice = "观望(资金背离警示)"

    result = {
        "code": code, "name": pq.get(code, {}).get("name", code),
        "industry": tcode, "proxies": pnames,
        "mode": "backtest" if date else "live",
        "date": date or "今天(实时)", "cutoff": cutoff,
        "features": {"board": round(board_c, 2), "main": round(m_c, 2),
                     "super": round(s_c, 2), "self": round(self_c, 2)},
        "prob_up": round(prob, 3),
        "probs3": {"sell": round(p3[0], 3), "hold": round(p3[1], 3),
                   "buy": round(p3[2], 3)},
        "advice": advice, "thr": thr,
        "pred_pct": round(pred_pct, 2),
        "band": [round(pred_pct - modelr["resid_std"], 2),
                 round(pred_pct + modelr["resid_std"], 2)],
        "model": {"n_train": len(X), "acc_insample": round(hit / len(X), 2),
                  "up_base": round(sum(Y) / len(Y), 2), "r2": round(modelr["r2"], 2),
                  "resid_std": round(modelr["resid_std"], 2),
                  "corrs": corrs, "coefs": coefs},
        "quality": quality,
        "warning": warning,
        "actual": None,
    }
    if date:
        actual = (closes[date] / pc - 1) * 100
        cls_actual = 0 if actual < -thr else (2 if actual > thr else 1)
        result["actual"] = {"pct": round(actual, 2),
                            "cls": ["显著跌", "震荡", "显著涨"][cls_actual],
                            "hit3": cls_pred == cls_actual}
    return result


def main():
    ap = argparse.ArgumentParser(description="当日涨跌可能性估计")
    ap.add_argument("code", help="6位股票代码, 如 688256")
    ap.add_argument("--date", help="回看某交易日 YYYY-MM-DD (缺省=今天实时)")
    ap.add_argument("--cutoff", default="10:30", help="观察时点 HH:MM, 默认10:30")
    ap.add_argument("--train", type=int, default=60, help="训练窗口天数, 默认60")
    ap.add_argument("--thr", type=float, default=None,
                    help="显著涨跌阈值%%, 缺省=训练窗日均|涨跌|中位数的一半")
    args = ap.parse_args()
    try:
        r = run_prediction(args.code, args.date, args.cutoff, args.train, args.thr,
                           progress=print)
    except ValueError as e:
        sys.exit(str(e))
    f, m = r["features"], r["model"]
    print(f"      板块代理涨跌 {f['board']:+.2f}% | 主力净 {f['main']:+.2f}亿 | "
          f"特大单净 {f['super']:+.2f}亿 | 自身 {f['self']:+.2f}%")
    print("=" * 56)
    print(f"  {r['name']}({r['code']}) {r['date']} {r['cutoff']}时点")
    print(f"  预测全天涨跌幅: {r['pred_pct']:+.2f}%  "
          f"(±1σ区间 {r['band'][0]:+.2f}% ~ {r['band'][1]:+.2f}%)")
    print(f"  当日收涨概率(二分类): {r['prob_up']:.0%}")
    p3 = r["probs3"]
    print(f"  决策概率(三分类, 阈值±{r['thr']}%):")
    print(f"    买   (预期收盘涨幅 > +{r['thr']}%) : {p3['buy']:.0%}")
    print(f"    不动 (预期收盘在 ±{r['thr']}% 内)  : {p3['hold']:.0%}")
    print(f"    卖   (预期收盘跌幅 < -{r['thr']}%) : {p3['sell']:.0%}")
    print(f"  建议: {r['advice']}")
    if r["actual"]:
        a = r["actual"]
        print(f"  实际收盘: {a['pct']:+.2f}% [{a['cls']}] → 三分类"
              f"{'✓ 命中' if a['hit3'] else '✗ 未中'}")
    print("=" * 56)
    print(f"  模型: 训练{m['n_train']}天 样本内acc={m['acc_insample']:.0%} "
          f"R²={m['r2']:.2f} | 相关性 {m['corrs']} | 系数 {m['coefs']}")
    print("  注: 资金流为分笔重构口径; 板块为同业成交额Top3代理; 概率仅供研究, 非投资建议")


if __name__ == "__main__":
    main()
