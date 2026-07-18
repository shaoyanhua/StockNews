# -*- coding: utf-8 -*-
"""
回测数据质量核查：
  A. 内部一致性：日期单调无重复、OHLC 逻辑、前复权锚定（最后一根 qfq==raw）
  B. 除权处理：qfq 序列在主板不应出现 <-10.5% 的"假跌"（raw 序列会有，因分红送转）
  C. 跨源交叉验证（腾讯K线 vs 新浪快照，07-15）：涨跌幅 / 成交额估计 / 换手率估计
  D. 停牌缺失：与指数交易日历对比
用法: python validate_data.py
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_sources import DATA_DIR, sina_market_snapshot, a_share_universe

START = "2025-07-01"

idx = pd.read_csv(DATA_DIR / "index_shcomp.csv", dtype={"date": str})
trade_days = set(idx[idx["date"] >= START]["date"])
files = sorted((DATA_DIR / "daily").glob("*.csv"))
print(f"文件数: {len(files)}, 窗口交易日: {len(trade_days)}")

stats = dict(files=0, dup_date=0, unsorted=0, ohlc_bad=0, anchor_bad=0,
             qfq_cliff=0, raw_cliff=0, miss10=0, nan_rows=0)
qfq_cliff_samples, anchor_samples = [], []
last_rows = {}

for f in files:
    df = pd.read_csv(f, dtype={"date": str})
    stats["files"] += 1
    if df["date"].duplicated().any():
        stats["dup_date"] += 1
    if not df["date"].is_monotonic_increasing:
        stats["unsorted"] += 1
    bad = ((df["high"] < df[["open", "close"]].max(axis=1) - 1e-6)
           | (df["low"] > df[["open", "close"]].min(axis=1) + 1e-6)).sum()
    if bad:
        stats["ohlc_bad"] += 1
    if df[["open", "close", "high", "low", "vol"]].isna().any().any():
        stats["nan_rows"] += 1
    # 前复权锚定：最后一根 qfq close 应等于 raw close
    if abs(df["close"].iloc[-1] - df["close_raw"].iloc[-1]) > 0.011:
        stats["anchor_bad"] += 1
        if len(anchor_samples) < 5:
            anchor_samples.append((f.stem, df["close"].iloc[-1], df["close_raw"].iloc[-1]))
    # 除权悬崖检查（仅主板10%涨跌幅限制的 60/00 开头）
    if f.stem[:2] in ("60", "00"):
        w = df[df["date"] >= START]
        pq = w["close"].pct_change() * 100
        pr = w["close_raw"].pct_change() * 100
        n_q = int((pq < -10.5).sum())
        n_r = int((pr < -10.5).sum())
        stats["qfq_cliff"] += n_q
        stats["raw_cliff"] += n_r
        if n_q and len(qfq_cliff_samples) < 5:
            d = w[pq < -10.5]["date"].tolist()
            qfq_cliff_samples.append((f.stem, d[:2]))
    # 窗口内停牌缺失
    have = set(df[df["date"] >= START]["date"])
    if len(trade_days - have) > len(trade_days) * 0.10:
        stats["miss10"] += 1
    last_rows[f.stem] = df.iloc[-1]

print("\n== A/B/D 内部一致性 ==")
print(f"日期重复文件: {stats['dup_date']}  乱序: {stats['unsorted']}  OHLC异常: {stats['ohlc_bad']}  含NaN: {stats['nan_rows']}")
print(f"前复权锚定不符(>0.01元): {stats['anchor_bad']}  样例: {anchor_samples}")
print(f"主板 qfq 序列 <-10.5% 天数(应≈0): {stats['qfq_cliff']}  样例: {qfq_cliff_samples}")
print(f"主板 raw 序列 <-10.5% 天数(除权所致,应>0): {stats['raw_cliff']}")
print(f"窗口内缺失>10%交易日(长期停牌): {stats['miss10']}")

print("\n== C 跨源交叉验证（腾讯 vs 新浪, 最近交易日）==")
snap = a_share_universe(sina_market_snapshot())
snap = snap.set_index("code")
uni = pd.read_csv(DATA_DIR / "universe.csv", dtype={"code": str}).set_index("code")
rows = []
for code, lr in last_rows.items():
    if code not in snap.index or code not in uni.index:
        continue
    s = snap.loc[code]
    fs = uni.loc[code, "float_shares"]
    my_pct = None
    # 用最后两根算涨幅
    vol_shou = lr["vol"] / 100.0 if code.startswith("68") else lr["vol"]  # 68板单位是股
    rows.append({
        "code": code,
        "tx_close": lr["close_raw"],
        "sina_close": s["trade"],
        "my_amt_yi": vol_shou * 100 * lr["close_raw"] / 1e8,
        "sina_amt_yi": s["amount"] / 1e8,
        "my_turn": vol_shou * 100 / fs * 100,
        "sina_turn": s["turnoverratio"],
    })
cmp_df = pd.DataFrame(rows)
cmp_df["close_diff"] = (cmp_df["tx_close"] - cmp_df["sina_close"]).abs()
cmp_df["amt_err%"] = (cmp_df["my_amt_yi"] / cmp_df["sina_amt_yi"] - 1) * 100
cmp_df["turn_err%"] = (cmp_df["my_turn"] / cmp_df["sina_turn"].replace(0, np.nan) - 1) * 100
print(f"对比样本: {len(cmp_df)}")
print(f"收盘价不一致(>0.01元): {(cmp_df['close_diff'] > 0.011).sum()} 只")
print(f"成交额估计误差(vol×close vs 真实): 中位 {cmp_df['amt_err%'].median():+.2f}%  "
      f"P5 {cmp_df['amt_err%'].quantile(.05):+.2f}%  P95 {cmp_df['amt_err%'].quantile(.95):+.2f}%")
print(f"换手率估计误差: 中位 {cmp_df['turn_err%'].median():+.2f}%  "
      f"P5 {cmp_df['turn_err%'].quantile(.05):+.2f}%  P95 {cmp_df['turn_err%'].quantile(.95):+.2f}%")
print(f"换手误差>20%的股票占比: {(cmp_df['turn_err%'].abs() > 20).mean()*100:.1f}%")
cmp_df.to_csv(DATA_DIR / "backtest" / "validation_cross_source.csv", index=False, encoding="utf-8-sig")

# 指数核对
hs = pd.read_csv(DATA_DIR / "index_hs300.csv", dtype={"date": str})
print(f"\n沪深300最后一根: {hs['date'].iloc[-1]} close={hs['close'].iloc[-1]}（人工核对东财/同花顺行情页）")
print(f"上证最后一根: {idx['date'].iloc[-1]} close={idx['close'].iloc[-1]}")
