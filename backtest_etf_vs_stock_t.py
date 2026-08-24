"""ETF vs stock intraday T-trading comparison backtest.

Compares sh515880 (通信ETF) and sz300308 (中际旭创) on the same 低吸高抛 T
strategy over the same trading days, using 1-minute bars from TDX.

Strategy template (dip-buy / rally-sell, requires an existing base position):
  - Track the running intraday high (reset after each completed round).
  - BUY signal when a 1-min close <= running_high * (1 - x); execute at next
    bar's open (no same-bar look-ahead).
  - SELL when a close >= buy_price * (1 + y); execute at next bar open.
  - Force-sell at the 14:55 bar open if the target was never reached, so every
    round closes intraday and the base position is unchanged (pure T P&L).
  - Multiple rounds per day allowed; after a round the reference high resets.

Costs (round trip, applied to net results):
  - ETF  515880: commission 0.005% per side, no stamp duty.
  - Stock 300308: commission 0.025% per side + 0.05% stamp duty on the sell.
A separate "with 1-tick slippage per side" scenario is reported because the
ETF trades near 0.76 yuan where one 0.001 tick is ~13 bp.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache_predict"
OUT_DIR = ROOT / "backtest_outputs"

TDX_SERVERS = [("115.238.56.198", 7709), ("115.238.90.165", 7709),
               ("180.153.18.170", 7709), ("60.191.117.167", 7709),
               ("119.97.185.59", 7709), ("124.70.133.119", 7709),
               ("116.205.183.150", 7709), ("123.60.73.44", 7709)]

SYMBOLS = {
    "sh515880": {"market": 1, "code": "515880", "name": "通信ETF",
                 "commission": 0.00005, "stamp_sell": 0.0, "tick": 0.001},
    "sz300308": {"market": 0, "code": "300308", "name": "中际旭创",
                 "commission": 0.00025, "stamp_sell": 0.0005, "tick": 0.01},
}

GRID_X = [0.002, 0.003, 0.005, 0.008, 0.012, 0.018]  # dip from running high
GRID_Y = [0.002, 0.003, 0.005, 0.008, 0.012, 0.018]  # rebound target


def tdx_connect():
    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    for ip, port in TDX_SERVERS:
        try:
            with socket.create_connection((ip, port), timeout=2):
                pass
            if api.connect(ip, port):
                print(f"connected {ip}:{port}")
                return api
        except Exception:
            continue
    raise RuntimeError("通达信服务器均不可达")


def fetch_min1(api, market: int, code: str, max_pages: int = 40) -> pd.DataFrame:
    """Page back through 1-min bars (category 8, 800/page) until exhausted."""
    frames = []
    for page in range(max_pages):
        for attempt in range(3):
            try:
                bars = api.get_security_bars(8, market, code, page * 800, 800)
                break
            except Exception:
                time.sleep(0.5)
                bars = None
        if not bars:
            break
        frames.append(pd.DataFrame(bars))
        if len(bars) < 800:
            break
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df[["datetime", "open", "high", "low", "close", "vol", "amount"]].copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)
    df["date"] = df["datetime"].dt.strftime("%Y-%m-%d")
    df["time"] = df["datetime"].dt.strftime("%H:%M")
    return df


def load_or_fetch(sym: str) -> pd.DataFrame:
    cache_path = CACHE / f"min1_{sym}.csv"
    if cache_path.exists():
        df = pd.read_csv(cache_path, parse_dates=["datetime"])
        df["date"] = df["date"].astype(str)
        return df
    api = tdx_connect()
    meta = SYMBOLS[sym]
    df = fetch_min1(api, meta["market"], meta["code"])
    api.disconnect()
    if df.empty:
        raise RuntimeError(f"no 1-min data for {sym}")
    df.to_csv(cache_path, index=False, encoding="utf-8-sig")
    return df


def day_frames(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    out = {}
    for date, g in df.groupby("date", sort=True):
        g = g[((g["time"] > "09:30") & (g["time"] <= "11:30"))
              | ((g["time"] > "13:00") & (g["time"] <= "15:00"))]
        g = g.sort_values("datetime").reset_index(drop=True)
        if len(g) >= 200:  # complete-ish session
            out[date] = g
    return out


def run_day(g: pd.DataFrame, x: float, y: float) -> list[dict]:
    """One day, one (x, y) cell. Returns completed rounds."""
    rounds = []
    n = len(g)
    force_idx = n - 6  # ~14:55, leave bars to execute the forced sell
    state = "flat"
    ref_high = float(g.loc[0, "high"])
    buy_price = np.nan
    buy_time = None
    i = 0
    while i < n - 1:
        row = g.loc[i]
        if state == "flat":
            ref_high = max(ref_high, float(row["high"]))
            if i < force_idx and float(row["close"]) <= ref_high * (1 - x):
                state = "long"
                buy_price = float(g.loc[i + 1, "open"])
                buy_time = g.loc[i + 1, "time"]
                i += 1
                continue
        else:
            hit_target = float(row["close"]) >= buy_price * (1 + y)
            if hit_target or i >= force_idx:
                sell_price = float(g.loc[i + 1, "open"])
                rounds.append({
                    "buy_time": buy_time, "sell_time": g.loc[i + 1, "time"],
                    "buy": buy_price, "sell": sell_price,
                    "gross_ret": sell_price / buy_price - 1.0,
                    "forced": not hit_target,
                })
                state = "flat"
                ref_high = float(g.loc[i + 1, "high"])
                i += 1
                continue
        i += 1
    return rounds


def net_return(gross: float, buy: float, sell: float, meta: dict,
               slip_ticks: int = 0) -> float:
    tick = meta["tick"]
    buy_eff = buy + slip_ticks * tick
    sell_eff = sell - slip_ticks * tick
    cost = meta["commission"] * 2 + meta["stamp_sell"]
    return sell_eff / buy_eff - 1.0 - cost


def backtest_symbol(sym: str, days: dict[str, pd.DataFrame],
                    dates: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    meta = SYMBOLS[sym]
    grid_rows, all_rounds = [], []
    for x in GRID_X:
        for y in GRID_Y:
            day_net0, day_net1 = [], []
            n_rounds = n_forced = 0
            for date in dates:
                rounds = run_day(days[date], x, y)
                net0 = sum(net_return(r["gross_ret"], r["buy"], r["sell"], meta, 0)
                           for r in rounds)
                net1 = sum(net_return(r["gross_ret"], r["buy"], r["sell"], meta, 1)
                           for r in rounds)
                day_net0.append(net0)
                day_net1.append(net1)
                n_rounds += len(rounds)
                n_forced += sum(r["forced"] for r in rounds)
                for r in rounds:
                    all_rounds.append({"symbol": sym, "date": date, "x": x, "y": y, **r})
            arr0, arr1 = np.array(day_net0), np.array(day_net1)
            wins = int((arr0 > 0).sum())
            active = int((arr0 != 0).sum())
            grid_rows.append({
                "symbol": sym, "x": x, "y": y,
                "days": len(dates), "rounds": n_rounds,
                "rounds_per_day": n_rounds / len(dates),
                "forced_ratio": n_forced / n_rounds if n_rounds else np.nan,
                "total_net_pct": float(arr0.sum()) * 100,
                "avg_day_net_bp": float(arr0.mean()) * 1e4,
                "avg_round_net_bp": float(arr0.sum() / n_rounds) * 1e4 if n_rounds else np.nan,
                "win_day_rate": wins / active if active else np.nan,
                "total_net_slip1_pct": float(arr1.sum()) * 100,
                "avg_round_net_slip1_bp": float(arr1.sum() / n_rounds) * 1e4 if n_rounds else np.nan,
                "day_net_std_bp": float(arr0.std()) * 1e4,
            })
    return pd.DataFrame(grid_rows), pd.DataFrame(all_rounds)


def volatility_stats(sym: str, days: dict[str, pd.DataFrame],
                     dates: list[str]) -> dict:
    meta = SYMBOLS[sym]
    ranges, closes, spreads, drifts = [], [], [], []
    prev_close = None
    for date in dates:
        g = days[date]
        hi, lo, close = float(g["high"].max()), float(g["low"].min()), float(g["close"].iloc[-1])
        day_open = float(g["open"].iloc[0])
        base = prev_close if prev_close else day_open
        # ETF share splits create a ~-50% overnight gap; fall back to day open.
        if abs(day_open / base - 1) > 0.15:
            base = day_open
        ranges.append((hi - lo) / base)
        closes.append(close)
        spreads.append(meta["tick"] / close)
        drifts.append(close / day_open - 1)
        prev_close = close
    r = np.array(ranges)
    d = np.array(drifts)
    return {
        "symbol": sym, "name": meta["name"], "days": len(dates),
        "median_price": float(np.median(closes)),
        "one_tick_bp": float(np.median(spreads)) * 1e4,
        "median_day_range_pct": float(np.median(r)) * 100,
        "mean_day_range_pct": float(np.mean(r)) * 100,
        "p90_day_range_pct": float(np.percentile(r, 90)) * 100,
        "days_range_over_1pct": int((r > 0.01).sum()),
        "days_range_over_2pct": int((r > 0.02).sum()),
        "roundtrip_fee_bp": (meta["commission"] * 2 + meta["stamp_sell"]) * 1e4,
        "intraday_drift_total_pct": float(d.sum()) * 100,
    }


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    data = {sym: day_frames(load_or_fetch(sym)) for sym in SYMBOLS}
    common = sorted(set.intersection(*(set(d) for d in data.values())))
    print(f"common trading days: {len(common)} ({common[0]} .. {common[-1]})")

    vol_stats = pd.DataFrame([volatility_stats(s, data[s], common) for s in SYMBOLS])
    grids, rounds = [], []
    for sym in SYMBOLS:
        gdf, rdf = backtest_symbol(sym, data[sym], common)
        grids.append(gdf)
        rounds.append(rdf)
    grid_df = pd.concat(grids, ignore_index=True)
    rounds_df = pd.concat(rounds, ignore_index=True)

    grid_df.to_csv(OUT_DIR / "etf_vs_stock_grid.csv", index=False, encoding="utf-8-sig")
    rounds_df.to_csv(OUT_DIR / "etf_vs_stock_rounds.csv", index=False, encoding="utf-8-sig")
    vol_stats.to_csv(OUT_DIR / "etf_vs_stock_volatility.csv", index=False, encoding="utf-8-sig")

    pd.set_option("display.width", 220)
    print("\n=== volatility / cost profile ===")
    print(vol_stats.to_string(index=False))
    for sym in SYMBOLS:
        sub = grid_df[grid_df["symbol"] == sym].sort_values("total_net_pct", ascending=False)
        print(f"\n=== {sym} top cells by total net (no slippage) ===")
        print(sub.head(8).to_string(index=False))

    # Walk-forward stability: pick best cell on first 70% of days, apply to last 30%.
    split = int(len(common) * 0.7)
    train, test = common[:split], common[split:]
    print(f"\n=== walk-forward (train {train[0]}..{train[-1]}, test {test[0]}..{test[-1]}) ===")
    wf_rows = []
    for sym in SYMBOLS:
        meta = SYMBOLS[sym]
        best, best_val = None, -np.inf
        for x in GRID_X:
            for y in GRID_Y:
                tot = sum(
                    sum(net_return(r["gross_ret"], r["buy"], r["sell"], meta, 0)
                        for r in run_day(data[sym][dt], x, y))
                    for dt in train)
                if tot > best_val:
                    best_val, best = tot, (x, y)
        x, y = best
        test_day_nets = [
            sum(net_return(r["gross_ret"], r["buy"], r["sell"], meta, 0)
                for r in run_day(data[sym][dt], x, y))
            for dt in test]
        test_day_nets_s1 = [
            sum(net_return(r["gross_ret"], r["buy"], r["sell"], meta, 1)
                for r in run_day(data[sym][dt], x, y))
            for dt in test]
        wf_rows.append({
            "symbol": sym, "best_x": x, "best_y": y,
            "train_total_net_pct": best_val * 100,
            "test_total_net_pct": float(np.sum(test_day_nets)) * 100,
            "test_total_net_slip1_pct": float(np.sum(test_day_nets_s1)) * 100,
            "test_days": len(test),
            "test_win_day_rate": float(np.mean([v > 0 for v in test_day_nets if v != 0])),
        })
    wf_df = pd.DataFrame(wf_rows)
    wf_df.to_csv(OUT_DIR / "etf_vs_stock_walkforward.csv", index=False, encoding="utf-8-sig")
    print(wf_df.to_string(index=False))


if __name__ == "__main__":
    main()
