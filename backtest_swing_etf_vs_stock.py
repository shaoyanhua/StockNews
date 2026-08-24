"""Multi-day swing (低吸高抛) comparison: sh515880 vs sz300308.

Strategy template (daily bars, qfq-adjusted, signal at close, execute at next
day's open — no look-ahead, T+1 compatible):
  - ENTRY: close <= rolling N-day high * (1 - z)   (pullback buy)
  - EXIT:  close >= entry * (1 + w)  → sell next open (rally sell)
           or force-sell at open after holding H=20 trading days
  - Single position, full size, no pyramiding.

Costs per side: ETF commission 0.005%; stock 0.025% + 0.05% stamp on sell.
Slippage scenario adds one tick per side (ETF ~8 bp at ~0.7-1.5 yuan,
stock ~0.1 bp at ~1000 yuan).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache_predict"
OUT_DIR = ROOT / "backtest_outputs"
UA = "Mozilla/5.0"

SYMBOLS = {
    "sh515880": {"name": "通信ETF", "commission": 0.00005, "stamp_sell": 0.0,
                 "slip_bp_side": 8.0},
    "sz300308": {"name": "中际旭创", "commission": 0.00025, "stamp_sell": 0.0005,
                 "slip_bp_side": 0.1},
}

GRID_N = [5, 10, 20]
GRID_Z = [0.03, 0.05, 0.08, 0.12]
GRID_W = [0.03, 0.05, 0.08, 0.12]
MAX_HOLD = 20


def fetch_daily(sym: str, n: int = 500) -> pd.DataFrame:
    cache_path = CACHE / f"tencent_kline_{sym}_qfq_{n}.json"
    bars = None
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{n},qfq"
    s = requests.Session()
    s.trust_env = False
    for attempt in range(3):
        try:
            payload = s.get(url, headers={"User-Agent": UA}, timeout=(5, 15)).json()
            data = payload["data"][sym]
            bars = data.get("qfqday") or data.get("day")
            if bars:
                cache_path.write_text(json.dumps(bars, ensure_ascii=False), encoding="utf-8")
                break
        except (requests.RequestException, KeyError, TypeError, ValueError):
            pass
    if bars is None:
        bars = json.loads(cache_path.read_text(encoding="utf-8"))
    df = pd.DataFrame([b[:6] for b in bars],
                      columns=["date", "open", "close", "high", "low", "volume"])
    for c in ["open", "close", "high", "low", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)


def run_swing(df: pd.DataFrame, n_high: int, z: float, w: float,
              meta: dict, slip: bool) -> list[dict]:
    """Returns completed trades. Executes at next-day open after a close signal."""
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    c = df["close"].to_numpy()
    dates = df["date"].to_numpy()
    roll_high = pd.Series(h).rolling(n_high, min_periods=n_high).max().to_numpy()
    cost = meta["commission"] * 2 + meta["stamp_sell"]
    slip_bp = meta["slip_bp_side"] / 1e4 if slip else 0.0
    trades = []
    i = n_high
    nbars = len(df)
    while i < nbars - 1:
        if c[i] <= roll_high[i] * (1 - z):
            buy = o[i + 1] * (1 + slip_bp)
            buy_date = dates[i + 1]
            j = i + 1
            exit_j = None
            forced = False
            while j < nbars - 1:
                held = j - (i + 1)
                if c[j] >= buy * (1 + w) or held >= MAX_HOLD:
                    exit_j = j + 1
                    forced = not (c[j] >= buy * (1 + w))
                    break
                j += 1
            if exit_j is None:  # still open at sample end: mark at last close
                exit_j = nbars - 1
                sell = c[exit_j] * (1 - slip_bp)
                forced = True
            else:
                sell = o[exit_j] * (1 - slip_bp)
            net = sell / buy - 1 - cost
            trades.append({
                "buy_date": buy_date, "sell_date": dates[exit_j],
                "hold_days": exit_j - (i + 1), "net": net, "forced": forced,
            })
            i = exit_j  # no overlapping positions
        i += 1
    return trades


def summarize(trades: list[dict], n_days: int) -> dict:
    if not trades:
        return {"trades": 0, "win_rate": np.nan, "avg_net_pct": np.nan,
                "total_compound_pct": 0.0, "avg_hold_days": np.nan,
                "exposure": 0.0, "forced_ratio": np.nan, "worst_trade_pct": np.nan}
    nets = np.array([t["net"] for t in trades])
    equity = np.cumprod(1 + nets)
    return {
        "trades": len(trades),
        "win_rate": float((nets > 0).mean()),
        "avg_net_pct": float(nets.mean()) * 100,
        "total_compound_pct": float(equity[-1] - 1) * 100,
        "avg_hold_days": float(np.mean([t["hold_days"] for t in trades])),
        "exposure": sum(t["hold_days"] for t in trades) / n_days,
        "forced_ratio": float(np.mean([t["forced"] for t in trades])),
        "worst_trade_pct": float(nets.min()) * 100,
    }


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    raw = {s: fetch_daily(s) for s in SYMBOLS}
    common = sorted(set(raw["sh515880"]["date"]) & set(raw["sz300308"]["date"]))
    data = {s: d[d["date"].isin(common)].reset_index(drop=True) for s, d in raw.items()}
    n_days = len(common)
    print(f"common days: {n_days} ({common[0]} .. {common[-1]})")
    for s, d in data.items():
        bh = d["close"].iloc[-1] / d["open"].iloc[0] - 1
        print(f"{s} buy&hold: {bh*100:.1f}%")

    rows = []
    for sym, meta in SYMBOLS.items():
        df = data[sym]
        for n_high in GRID_N:
            for z in GRID_Z:
                for w in GRID_W:
                    s0 = summarize(run_swing(df, n_high, z, w, meta, slip=False), n_days)
                    s1 = summarize(run_swing(df, n_high, z, w, meta, slip=True), n_days)
                    rows.append({"symbol": sym, "N": n_high, "z": z, "w": w, **s0,
                                 "total_slip_pct": s1["total_compound_pct"],
                                 "avg_net_slip_pct": s1["avg_net_pct"]})
    grid = pd.DataFrame(rows)
    grid.to_csv(OUT_DIR / "swing_etf_vs_stock_grid.csv", index=False, encoding="utf-8-sig")

    pd.set_option("display.width", 220)
    for sym in SYMBOLS:
        sub = grid[grid["symbol"] == sym].sort_values("total_compound_pct", ascending=False)
        print(f"\n=== {sym} top by compound net (no slip) ===")
        print(sub.head(8).to_string(index=False))

    # walk-forward 70/30
    split_date = common[int(n_days * 0.7)]
    print(f"\n=== walk-forward: train <= {split_date}, test after ===")
    wf = []
    for sym, meta in SYMBOLS.items():
        df = data[sym]
        tr_df = df[df["date"] <= split_date].reset_index(drop=True)
        te_df = df[df["date"] > split_date].reset_index(drop=True)
        best, best_val = None, -np.inf
        for n_high in GRID_N:
            for z in GRID_Z:
                for w in GRID_W:
                    s = summarize(run_swing(tr_df, n_high, z, w, meta, False), len(tr_df))
                    if s["total_compound_pct"] > best_val:
                        best_val, best = s["total_compound_pct"], (n_high, z, w)
        n_high, z, w = best
        te0 = summarize(run_swing(te_df, n_high, z, w, meta, False), len(te_df))
        te1 = summarize(run_swing(te_df, n_high, z, w, meta, True), len(te_df))
        bh_te = te_df["close"].iloc[-1] / te_df["open"].iloc[0] - 1
        wf.append({"symbol": sym, "N": n_high, "z": z, "w": w,
                   "train_total_pct": best_val,
                   "test_total_pct": te0["total_compound_pct"],
                   "test_total_slip_pct": te1["total_compound_pct"],
                   "test_trades": te0["trades"], "test_win_rate": te0["win_rate"],
                   "test_buyhold_pct": bh_te * 100})
        # grid-wide on test
        vals0, vals1 = [], []
        for nh in GRID_N:
            for zz in GRID_Z:
                for ww in GRID_W:
                    vals0.append(summarize(run_swing(te_df, nh, zz, ww, meta, False),
                                           len(te_df))["total_compound_pct"])
                    vals1.append(summarize(run_swing(te_df, nh, zz, ww, meta, True),
                                           len(te_df))["total_compound_pct"])
        v0, v1 = np.array(vals0), np.array(vals1)
        print(f"{sym} test grid: noslip pos {int((v0>0).sum())}/{len(v0)} "
              f"median {np.median(v0):.1f}% best {v0.max():.1f}% | "
              f"slip pos {int((v1>0).sum())}/{len(v1)} median {np.median(v1):.1f}%")
    wf_df = pd.DataFrame(wf)
    wf_df.to_csv(OUT_DIR / "swing_etf_vs_stock_walkforward.csv", index=False, encoding="utf-8-sig")
    print(wf_df.to_string(index=False))


if __name__ == "__main__":
    main()
