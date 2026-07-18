# -*- coding: utf-8 -*-
"""
尾盘隔夜策略共用数据源模块（腾讯 + 新浪 + 同花顺）。

本机实测（2026-07-16）：东财 push2/push2his、通达信 TCP7709 不通，
故全部走腾讯/新浪/同花顺，且请求时禁用系统代理（国内源不需要代理）。
"""
import os
import sys
import time
import random
import json
import re
from datetime import date, datetime
from pathlib import Path

# 禁用系统代理（本机 127.0.0.1:1080 代理会拦截国内数据源）
for _k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"]:
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"

import requests
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
for _d in (DATA_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    s.trust_env = False  # 忽略系统代理
    return s


_S = new_session()


def _get(url, params=None, headers=None, timeout=12, retries=3, session=None):
    s = session or _S
    last = None
    for i in range(retries):
        try:
            r = s.get(url, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5 * (i + 1) + random.random() * 0.3)
    raise last


def code_prefix(code: str) -> str:
    return "sh" if code.startswith(("6", "9", "5")) else ("bj" if code.startswith(("4", "8", "9")) else "sz")


def norm_code(code: str) -> str:
    """归一化为 6 位数字代码"""
    m = re.search(r"(\d{6})", str(code))
    return m.group(1) if m else str(code)


# ────────────────────────── 腾讯实时行情 ──────────────────────────

def _prefixed(code: str) -> str:
    """支持已带 sh/sz/bj 前缀的代码（如指数 sh000001）。"""
    c = str(code).lower()
    return c if c[:2] in ("sh", "sz", "bj") else code_prefix(c) + c


def tencent_quote(codes: list[str], session=None) -> dict[str, dict]:
    """批量实时行情。返回 {code: {...}}，字段含 量比/换手/流通市值/成交额/涨停价。"""
    out = {}
    for i in range(0, len(codes), 60):
        batch = codes[i:i + 60]
        prefixed = [_prefixed(c) for c in batch]
        r = _get("https://qt.gtimg.cn/q=" + ",".join(prefixed), session=session)
        for line in r.content.decode("gbk", "ignore").strip().split(";"):
            if "=" not in line or '"' not in line:
                continue
            key = line.split("=")[0].split("_")[-1].strip()
            v = line.split('"')[1].split("~")
            if len(v) < 53:
                continue
            def f(idx):
                try:
                    return float(v[idx]) if v[idx] else 0.0
                except ValueError:
                    return 0.0
            out[key[2:]] = {
                "name": v[1], "price": f(3), "last_close": f(4), "open": f(5),
                "change_pct": f(32), "high": f(33), "low": f(34),
                "amount_wan": f(37), "turnover_pct": f(38),
                "amplitude_pct": f(43), "mcap_yi": f(44), "float_mcap_yi": f(45),
                "limit_up": f(47), "limit_down": f(48), "vol_ratio": f(49),
            }
        if i + 60 < len(codes):
            time.sleep(0.15 + random.random() * 0.1)
    return out


# ────────────────────────── 腾讯日K线 ──────────────────────────

KLINE_HOSTS = [
    "https://web.ifzq.gtimg.cn",
    "https://proxy.finance.qq.com/ifzqgtimg",
    "https://ifzq.gtimg.cn",
]
_kline_host_i = [0]


class ThrottledError(RuntimeError):
    """腾讯 K 线接口限流（HTTP 501/429/403）。"""


def tencent_day_kline(symbol: str, count: int = 650, qfq: bool = True,
                      session=None) -> pd.DataFrame:
    """日K线。symbol 形如 sh600519/sz002463/sh000300(指数)。
    返回列: date, open, close, high, low, vol(手)。qfq=True 前复权。
    多域名轮换；全部被限流时抛 ThrottledError。"""
    adj = "qfq" if qfq else ""
    throttled = 0
    for k in range(len(KLINE_HOSTS)):
        host = KLINE_HOSTS[(_kline_host_i[0] + k) % len(KLINE_HOSTS)]
        url = f"{host}/appstock/app/fqkline/get?param={symbol},day,,,{count},{adj}"
        try:
            d = _get(url, session=session, retries=2).json()["data"][symbol]
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code in (501, 429, 403):
                throttled += 1
                continue
            raise
        _kline_host_i[0] = (_kline_host_i[0] + k) % len(KLINE_HOSTS)
        rows = d.get("qfqday") or d.get("day") or []
        df = pd.DataFrame([r[:6] for r in rows],
                          columns=["date", "open", "close", "high", "low", "vol"])
        for c in ["open", "close", "high", "low", "vol"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df
    raise ThrottledError(f"kline throttled on all hosts ({throttled})")


# ────────────────────────── 腾讯当日分时 ──────────────────────────

def tencent_minute_today(code: str, session=None) -> pd.DataFrame:
    """当日 1 分钟分时（含集合竞价）。code 可为 6 位数字或带前缀(sh000001=上证指数)。
    返回列: time(HHMM), price, cum_vol(手), cum_amt(元), avg_price(分时均价线)。
    attrs: date(会话日期), prev_close(昨收，可能为 None)。
    非交易时段返回最近一个交易日的完整分时。"""
    sym = _prefixed(code)
    payload = _get(f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sym}",
                   session=session).json()["data"][sym]
    d = payload["data"]
    rows = []
    for line in d["data"]:
        p = line.split()
        if len(p) >= 4:
            rows.append((p[0], float(p[1]), float(p[2]), float(p[3])))
    df = pd.DataFrame(rows, columns=["time", "price", "cum_vol", "cum_amt"])
    df["avg_price"] = (df["cum_amt"] / (df["cum_vol"] * 100.0)).where(df["cum_vol"] > 0)
    df.attrs["date"] = str(d.get("date", ""))
    prev_close = None
    try:
        qt = payload.get("qt", {}).get(sym)
        if qt and len(qt) > 4 and qt[4]:
            prev_close = float(qt[4])
    except (TypeError, ValueError):
        pass
    df.attrs["prev_close"] = prev_close
    return df


# ────────────────────────── 新浪全市场快照 ──────────────────────────

def sina_market_snapshot(session=None, max_pages: int = 80) -> pd.DataFrame:
    """全市场 A 股快照。列含 code,name,trade(现价),changepercent,volume(股),amount(元),
    turnoverratio(换手%),nmc(流通市值 万),mktcap(总市值 万)。"""
    s = session or _S
    frames = []
    for page in range(1, max_pages + 1):
        r = _get("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
                 params={"page": str(page), "num": "100", "sort": "symbol",
                         "asc": "1", "node": "hs_a"},
                 headers={"Referer": "https://finance.sina.com.cn/"}, session=s)
        rows = r.json()
        if not rows:
            break
        frames.append(pd.DataFrame(rows))
        time.sleep(0.25 + random.random() * 0.2)
    df = pd.concat(frames, ignore_index=True)
    for c in ["trade", "changepercent", "volume", "amount", "turnoverratio", "nmc", "mktcap", "open", "high", "low", "settlement"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# ────────────────────────── 新浪行业板块 ──────────────────────────

def sina_industry_boards(session=None) -> pd.DataFrame:
    """新浪行业板块行情。返回列: node, board_name, pct(当日涨跌%), leader(领涨股代码)。"""
    r = _get("https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php",
             headers={"Referer": "https://finance.sina.com.cn/"}, session=session)
    txt = r.content.decode("gbk", "ignore")
    m = re.search(r"=\s*(\{.*\})", txt, re.S)
    data = json.loads(m.group(1))
    rows = []
    for node, v in data.items():
        p = v.split(",")
        # 格式: node,名称,家数,均价,涨跌额,涨跌幅%,成交量,成交额,领涨股symbol,...
        if len(p) >= 9:
            rows.append((node, p[1], float(p[5]), p[8]))
    return pd.DataFrame(rows, columns=["node", "board_name", "pct", "leader"])


def sina_board_members(node: str, session=None) -> list[str]:
    """新浪行业板块成分股代码列表。"""
    s = session or _S
    codes = []
    for page in range(1, 6):
        r = _get("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
                 params={"page": str(page), "num": "100", "sort": "symbol",
                         "asc": "1", "node": node},
                 headers={"Referer": "https://finance.sina.com.cn/"}, session=s)
        rows = r.json()
        if not rows:
            break
        codes.extend(x["code"] for x in rows)
        time.sleep(0.2 + random.random() * 0.15)
    return codes


def industry_map(session=None, max_age_days: int = 7) -> tuple[dict, pd.DataFrame]:
    """构建/读取 股票→行业板块 映射缓存。返回 ({code: node}, boards_df)。"""
    cache = CACHE_DIR / "industry_map.json"
    boards = sina_industry_boards(session=session)
    if cache.exists() and (time.time() - cache.stat().st_mtime) < max_age_days * 86400:
        mapping = json.loads(cache.read_text(encoding="utf-8"))
        return mapping, boards
    mapping = {}
    for node in boards["node"]:
        try:
            for c in sina_board_members(node, session=session):
                mapping[c] = node
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] 板块 {node} 成分获取失败: {e}")
    cache.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    return mapping, boards


# ────────────────────────── 同花顺强势股归因 ──────────────────────────

def ths_hot_reason(day: str | None = None, session=None) -> pd.DataFrame:
    """同花顺当日强势股 + 题材归因。day: 'YYYY-MM-DD'，None=今天。"""
    if day is None:
        day = date.today().strftime("%Y-%m-%d")
    r = _get(f"http://zx.10jqka.com.cn/event/api/getharden/date/{day}/orderby/date/orderway/desc/charset/GBK/",
             session=session)
    rows = r.json().get("data") or []
    df = pd.DataFrame(rows)
    if not df.empty:
        df["code"] = df["code"].astype(str).str.zfill(6)
    return df


# ────────────────────────── 工具 ──────────────────────────

def is_st(name: str) -> bool:
    return "ST" in name.upper() or "退" in name


def a_share_universe(snapshot: pd.DataFrame) -> pd.DataFrame:
    """从新浪快照中筛出主板/创业板/科创板 A 股（剔除 ST/退/北交所）。"""
    df = snapshot.copy()
    df["code"] = df["code"].astype(str).str.zfill(6)
    ok_prefix = df["code"].str.startswith(("60", "00", "30", "68"))
    not_st = ~df["name"].map(is_st)
    return df[ok_prefix & not_st].reset_index(drop=True)
