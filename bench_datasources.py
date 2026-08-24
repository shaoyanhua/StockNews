# -*- coding: utf-8 -*-
"""
行情数据源压测: 谁又稳又快?
============================
用项目实际使用的 requests(而非curl)测, 因为部分站点(东财)对非浏览器TLS指纹做风控,
curl能通不代表代码里能通 —— 只有 requests 的结果才反映真实可用性。

指标: 成功率 / p50延迟 / p95延迟 / 返回是否含有效数据
用法: python bench_datasources.py [--rounds 6]
"""
import argparse
import json
import statistics
import time

import requests

S = requests.Session()
S.trust_env = False
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

CODE, SYM_SH, SYM_SZ = "600519", "sh600519", "sz300308"


def _get(url, headers=None, timeout=8):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    return S.get(url, headers=h, timeout=timeout)


# 每项: (分类, 名称, 调用函数->(ok, 摘要))
def t_tencent_quote():
    r = _get(f"https://qt.gtimg.cn/q={SYM_SH},{SYM_SZ}")
    v = r.content.decode("gbk", "ignore").split('"')[1].split("~")
    return (len(v) > 40 and float(v[3]) > 0), f"{v[1]} {v[3]}"


def t_tencent_kline():
    u = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={SYM_SZ},day,,,20,qfq"
    d = _get(u).json()["data"][SYM_SZ]
    b = d.get("qfqday") or d.get("day")
    return len(b) >= 10, f"{len(b)}根 末{b[-1][0]}"


def t_tencent_minute():
    u = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={SYM_SZ}"
    d = _get(u).json()["data"][SYM_SZ]["data"]["data"]
    return len(d) > 10, f"{len(d)}个分时点"


def t_sina_quote():
    r = _get(f"https://hq.sinajs.cn/list={SYM_SH},{SYM_SZ}",
             headers={"Referer": "https://finance.sina.com.cn/"})
    txt = r.content.decode("gbk", "ignore")
    p = txt.split('"')[1].split(",")
    return len(p) > 30 and float(p[3]) > 0, f"{p[0]} {p[3]}"


def t_sina_kline():
    u = ("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService."
         f"getKLineData?symbol={SYM_SZ}&scale=240&ma=no&datalen=20")
    d = json.loads(_get(u).text)
    return len(d) >= 10, f"{len(d)}根 末{d[-1]['day']}"


def t_em_quote_flow():
    u = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2"
         "&fields=f12,f14,f2,f3,f62,f184&secids=1.600519,0.300308")
    d = _get(u, {"Referer": "https://quote.eastmoney.com/"}).json()
    rows = (d.get("data") or {}).get("diff") or []
    return len(rows) > 0, f"{len(rows)}只 主力占比{rows[0].get('f184') if rows else '-'}"


def t_em_flow_hist():
    u = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
         "?secid=1.600519&lmt=5&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56")
    d = _get(u, {"Referer": "https://quote.eastmoney.com/"}).json()
    k = d["data"]["klines"]
    return len(k) > 0, f"{len(k)}天 末{k[-1].split(',')[0]}"


def t_ths_hot():
    u = "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock?stock_type=a&type=hour&list_type=normal"
    d = _get(u).json()
    arr = (d.get("data") or {}).get("stock_list") or []
    return len(arr) > 0, f"{len(arr)}只热门"


def t_ths_north():
    u = "https://data.10jqka.com.cn/dataapi/transaction/northbound/v1/hgt"
    d = _get(u, {"Referer": "https://data.10jqka.com.cn/"}).json()
    return bool(d), f"keys={list(d)[:3]}"


def t_baidu_kline():
    u = ("https://finance.pae.baidu.com/vapi/v1/getquotation?srcid=5353"
         f"&pointType=string&group=quotation_kline_ab&query={CODE}"
         "&code=" + CODE + "&market_type=ab&newFormat=1&is_kc=0&ktype=day&finClientType=pc")
    d = _get(u, {"Referer": "https://gushitong.baidu.com/"}).json()
    r = d.get("Result") or {}
    return bool(r), f"Result keys={list(r)[:3]}"


def t_xueqiu_quote():
    # 雪球需要先拿cookie
    S.get("https://xueqiu.com/", headers={"User-Agent": UA}, timeout=8)
    u = f"https://stock.xueqiu.com/v5/stock/realtime/quotec.json?symbol=SH{CODE}"
    d = _get(u).json()
    arr = d.get("data") or []
    return len(arr) > 0, f"{arr[0].get('current') if arr else '-'}"


def t_tdx_tcp():
    from predict_updown import tdx_connect
    t0 = time.time()
    api = tdx_connect()
    try:
        bars = api.get_security_bars(9, 1, "600519", 0, 10)
        return bool(bars), f"{len(bars)}根K线"
    finally:
        api.disconnect()


TESTS = [
    ("行情", "腾讯 qt.gtimg (实时)", t_tencent_quote),
    ("K线", "腾讯 ifzq (日K)", t_tencent_kline),
    ("分时", "腾讯 minute (分时线)", t_tencent_minute),
    ("行情", "新浪 hq.sinajs (实时)", t_sina_quote),
    ("K线", "新浪 quotes.sina (日K)", t_sina_kline),
    ("资金流", "东财 push2 (实时+主力)", t_em_quote_flow),
    ("资金流", "东财 push2his (日级)", t_em_flow_hist),
    ("信号", "同花顺 热榜", t_ths_hot),
    ("资金流", "同花顺 北向", t_ths_north),
    ("K线", "百度股市通", t_baidu_kline),
    ("行情", "雪球 quotec", t_xueqiu_quote),
    ("全能", "通达信 TCP (pytdx)", t_tdx_tcp),
]


def bench(rounds):
    print(f"每个源测 {rounds} 次, requests 直连(与项目代码同口径)\n")
    print(f"{'分类':<7}{'数据源':<26}{'成功率':>7}{'p50':>8}{'p95':>8}  摘要/错误")
    print("-" * 92)
    results = []
    for cat, name, fn in TESTS:
        lat, ok_n, note, err = [], 0, "", ""
        for i in range(rounds):
            t0 = time.time()
            try:
                ok, summary = fn()
                dt = (time.time() - t0) * 1000
                if ok:
                    ok_n += 1
                    lat.append(dt)
                    note = summary
                else:
                    err = err or f"返回空: {summary}"
            except Exception as e:
                err = err or f"{type(e).__name__}: {str(e)[:45]}"
            time.sleep(0.35)
        rate = ok_n / rounds
        p50 = statistics.median(lat) if lat else None
        p95 = (sorted(lat)[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None)
        results.append((cat, name, rate, p50, p95, note or err))
        rs = f"{rate:.0%}"
        p50s = f"{p50:.0f}ms" if p50 else "—"
        p95s = f"{p95:.0f}ms" if p95 else "—"
        flag = "✅" if rate == 1 else ("⚠️" if rate >= 0.5 else "❌")
        print(f"{cat:<7}{name:<26}{rs:>7}{p50s:>8}{p95s:>8}  {flag} {(note or err)[:44]}")
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=6)
    a = ap.parse_args()
    res = bench(a.rounds)

    print("\n" + "=" * 92)
    print("推荐(成功率100%且p50最低者优先)")
    print("=" * 92)
    by_cat = {}
    for cat, name, rate, p50, p95, note in res:
        by_cat.setdefault(cat, []).append((rate, p50 or 9e9, name))
    for cat, arr in by_cat.items():
        arr.sort(key=lambda x: (-x[0], x[1]))
        best = arr[0]
        alt = arr[1] if len(arr) > 1 else None
        line = f"  {cat:<7} 首选 {best[2]} ({best[0]:.0%}, {best[1]:.0f}ms)"
        if alt and alt[0] > 0:
            line += f"  | 备用 {alt[2]} ({alt[0]:.0%}, {alt[1]:.0f}ms)"
        print(line)
