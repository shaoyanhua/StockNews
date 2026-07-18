# -*- coding: utf-8 -*-
"""
核查视频"胜率90%+/100%获利"宣传：同一批信号，用不同"胜"的口径分别统计。
口径从严到宽：开盘卖净赚 > 开盘高开 > 次日收盘高于买价 > 次日盘中曾高于买价(一买就涨) 。
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_sources import DATA_DIR

COST = 0.0035
START, END = "2025-07-01", "2026-07-14"


def enrich(sig: pd.DataFrame) -> pd.DataFrame:
    """给信号补 次日最高/最低（qfq口径）。"""
    outs = []
    for code, g in sig.groupby("code"):
        fp = DATA_DIR / "daily" / f"{str(code).zfill(6)}.csv"
        if not fp.exists():
            continue
        d = pd.read_csv(fp, dtype={"date": str})
        d["high_next"] = d["high"].shift(-1)
        d["low_next"] = d["low"].shift(-1)
        m = d.set_index("date")[["high_next", "low_next"]]
        g = g.join(m, on="date")
        outs.append(g)
    return pd.concat(outs, ignore_index=True)


def stats(name, s):
    buy = s["close"]
    r = {
        "策略": name, "N": len(s),
        "①开盘卖净赚(扣0.35%)": (s["ret_open"] > COST).mean(),
        "②开盘高开(毛)": (s["ret_open"] > 0).mean(),
        "③次日收盘>买价": (s["ret_close"] > 0).mean(),
        "④盘中曾>买价(一买就涨)": (s["high_next"] > buy).mean(),
        "⑤盘中曾覆盖成本": (s["high_next"] > buy * (1 + COST)).mean(),
        "盘中最高平均涨幅%": ((s["high_next"] / buy - 1) * 100).mean(),
        "盘中最低平均跌幅%": ((s["low_next"] / buy - 1) * 100).mean(),
    }
    for k in list(r):
        if k not in ("策略", "N", "盘中最高平均涨幅%", "盘中最低平均跌幅%"):
            r[k] = round(r[k] * 100, 1)
    r["盘中最高平均涨幅%"] = round(r["盘中最高平均涨幅%"], 2)
    r["盘中最低平均跌幅%"] = round(r["盘中最低平均跌幅%"], 2)
    return r


rows = []
for tag, label in (("video", "视频原版516笔"), ("v1", "增强版V1 92笔")):
    s = pd.read_csv(DATA_DIR / "backtest" / f"signals_{tag}.csv", dtype={"code": str, "date": str})
    s = enrich(s)
    rows.append(stats(label, s))
    # 最好月份（挑时机也到不了90%的证据）
    s["month"] = s["date"].str[:7]
    best = s.groupby("month")["ret_open"].mean().idxmax()
    rows.append(stats(f"  └{label[:5]}最好月({best})", s[s["month"] == best]))

# 全市场基准（抽样400只）
rng = np.random.default_rng(7)
files = sorted((DATA_DIR / "daily").glob("*.csv"))
sample = rng.choice(len(files), size=400, replace=False)
outs = []
for i in sample:
    d = pd.read_csv(files[i], dtype={"date": str})
    d["ret_open"] = d["open"].shift(-1) / d["close"] - 1
    d["ret_close"] = d["close"].shift(-1) / d["close"] - 1
    d["high_next"] = d["high"].shift(-1)
    d["low_next"] = d["low"].shift(-1)
    w = d[(d["date"] >= START) & (d["date"] <= END)].dropna(subset=["ret_open", "high_next"])
    outs.append(w[["date", "close", "ret_open", "ret_close", "high_next", "low_next"]])
base = pd.concat(outs, ignore_index=True)
rows.append(stats("全市场基准(400只抽样)", base))

out = pd.DataFrame(rows)
sys.stdout.reconfigure(encoding="utf-8")
print(out.to_string(index=False))
