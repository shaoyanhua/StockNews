"""Minute-level backtest for the TongDaXin intraday T-trading indicator.

The script uses only information available at each minute.  A signal is formed
at a minute close and the simulated sale occurs at the next minute's first
trade, avoiding same-bar look-ahead.  It writes audit-ready CSV/JSON outputs.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache_predict"
DAILY_PATH = CACHE / "tencent_kline_sz300308_raw_400.json"
OUT_DIR = ROOT / "backtest_outputs"


@dataclass(frozen=True)
class Config:
    lower_pct: float = 0.018
    upper_pct: float = 0.028
    vwap_lower_pct: float = 0.006
    vwap_upper_pct: float = 0.012
    filter_bars: int = 15
    buyback_pct: float = 0.008
    fly_rise_pct: float = 0.020


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        return (math.nan, math.nan)
    p = successes / total
    den = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / den
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / den
    return (centre - margin, centre + margin)


def load_daily() -> pd.DataFrame:
    rows = json.loads(DAILY_PATH.read_text(encoding="utf-8"))
    # Tencent appends a seventh corporate-action object on a few ex-dividend rows.
    daily = pd.DataFrame([row[:6] for row in rows], columns=["date", "open", "close", "high", "low", "volume"])
    daily["date"] = pd.to_datetime(daily["date"])
    for col in ["open", "close", "high", "low", "volume"]:
        daily[col] = pd.to_numeric(daily[col], errors="coerce")
    daily = daily.sort_values("date").reset_index(drop=True)
    daily["prev_close"] = daily["close"].shift(1)
    return daily


def load_minutes(path: Path) -> pd.DataFrame:
    ticks = pd.read_csv(path, header=None, names=["time", "price", "volume", "side"])
    ticks["time"] = ticks["time"].astype(str).str.slice(0, 5)
    ticks["price"] = pd.to_numeric(ticks["price"], errors="coerce")
    ticks["volume"] = pd.to_numeric(ticks["volume"], errors="coerce").fillna(0)
    ticks = ticks.dropna(subset=["price"])
    ticks = ticks[
        ((ticks["time"] >= "09:30") & (ticks["time"] <= "11:30"))
        | ((ticks["time"] >= "13:00") & (ticks["time"] <= "15:00"))
    ].copy()
    if ticks.empty:
        return pd.DataFrame()
    grouped = ticks.groupby("time", sort=True, observed=True)
    minute = grouped.agg(
        open=("price", "first"),
        high=("price", "max"),
        low=("price", "min"),
        close=("price", "last"),
        volume=("volume", "sum"),
        trades=("price", "size"),
    ).reset_index()
    minute["turnover_proxy"] = grouped.apply(
        lambda x: float((x["price"] * x["volume"]).sum()), include_groups=False
    ).to_numpy()
    minute["cum_volume"] = minute["volume"].cumsum()
    minute["cum_turnover"] = minute["turnover_proxy"].cumsum()
    minute["vwap"] = np.where(
        minute["cum_volume"] > 0,
        minute["cum_turnover"] / minute["cum_volume"],
        minute["close"].expanding().mean(),
    )
    minute["ma3"] = minute["close"].rolling(3, min_periods=3).mean()
    return minute


def apply_filter(raw: pd.Series, bars: int) -> pd.Series:
    """Approximate TongDaXin FILTER: suppress signals for the following N bars."""
    keep = np.zeros(len(raw), dtype=bool)
    blocked_through = -1
    for idx, value in enumerate(raw.fillna(False).astype(bool).to_numpy()):
        if value and idx > blocked_through:
            keep[idx] = True
            blocked_through = idx + bars
    return pd.Series(keep, index=raw.index)


def signal_frame(minute: pd.DataFrame, prev_close: float, cfg: Config, variant: str) -> pd.DataFrame:
    m = minute.copy()
    if variant == "fixed":
        m["lower"] = prev_close * (1 + cfg.lower_pct)
        m["upper"] = prev_close * (1 + cfg.upper_pct)
    elif variant == "vwap_dynamic":
        m["lower"] = np.maximum(prev_close * (1 + cfg.lower_pct), m["vwap"] * (1 + cfg.vwap_lower_pct))
        m["upper"] = np.maximum(prev_close * (1 + cfg.upper_pct), m["vwap"] * (1 + cfg.vwap_upper_pct))
    else:
        raise ValueError(f"Unknown variant: {variant}")
    raw = (
        (m["close"] >= m["lower"])
        & (m["close"] < m["close"].shift(1))
        & (m["close"] < m["ma3"])
        & (m["volume"] < m["volume"].shift(1) * 1.2)
    )
    m["raw_signal"] = raw.fillna(False)
    m["signal"] = apply_filter(m["raw_signal"], cfg.filter_bars)
    return m


def backtest_variant(
    dates: list[pd.Timestamp],
    minute_by_date: dict[pd.Timestamp, pd.DataFrame],
    prev_close_by_date: dict[pd.Timestamp, float],
    cfg: Config,
    variant: str,
) -> pd.DataFrame:
    records: list[dict] = []
    for day_idx, date in enumerate(dates):
        minute = minute_by_date[date]
        prev_close = prev_close_by_date[date]
        frame = signal_frame(minute, prev_close, cfg, variant)
        candidates = frame.index[frame["signal"]].tolist()
        if not candidates:
            continue
        signal_idx = candidates[0]  # one T sale per day: first confirmed signal
        if signal_idx + 1 >= len(frame):
            continue
        signal = frame.loc[signal_idx]
        execution = frame.loc[signal_idx + 1]
        sell_price = float(execution["open"])
        later_same = frame.iloc[signal_idx + 1 :]
        next_date = dates[day_idx + 1] if day_idx + 1 < len(dates) else None
        next_minutes = minute_by_date[next_date] if next_date is not None else pd.DataFrame()
        same_low = float(later_same["low"].min()) if not later_same.empty else math.nan
        next_low = float(next_minutes["low"].min()) if not next_minutes.empty else math.nan
        same_high = float(later_same["high"].max()) if not later_same.empty else math.nan
        next_high = float(next_minutes["high"].max()) if not next_minutes.empty else math.nan
        window_low = float(np.nanmin([same_low, next_low]))
        window_high = float(np.nanmax([same_high, next_high]))
        buyback_same = same_low <= sell_price * (1 - cfg.buyback_pct)
        buyback_by_next = window_low <= sell_price * (1 - cfg.buyback_pct)
        fly = (not buyback_by_next) and (window_high >= sell_price * (1 + cfg.fly_rise_pct))
        records.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "variant": variant,
                "prev_close": prev_close,
                "lower_at_signal": float(signal["lower"]),
                "upper_at_signal": float(signal["upper"]),
                "signal_time": signal["time"],
                "signal_close": float(signal["close"]),
                "sell_time": execution["time"],
                "sell_price": sell_price,
                "same_day_low_after": same_low,
                "next_day_low": next_low,
                "window_low": window_low,
                "window_high": window_high,
                "max_retracement_pct": (sell_price - window_low) / sell_price,
                "max_adverse_pct": (window_high - sell_price) / sell_price,
                "buyback_same_0_8": bool(buyback_same),
                "buyback_by_next_0_8": bool(buyback_by_next),
                "fly_2pct_without_buyback": bool(fly),
                "next_date": next_date.strftime("%Y-%m-%d") if next_date is not None else None,
            }
        )
    return pd.DataFrame(records)


def summarize(trades: pd.DataFrame, eligible_days: int, label: str) -> dict:
    n = len(trades)
    same = int(trades["buyback_same_0_8"].sum()) if n else 0
    next_ = int(trades["buyback_by_next_0_8"].sum()) if n else 0
    fly = int(trades["fly_2pct_without_buyback"].sum()) if n else 0
    ci_low, ci_high = wilson_interval(next_, n)
    return {
        "period": label,
        "eligible_days": eligible_days,
        "signal_days": n,
        "signal_day_rate": n / eligible_days if eligible_days else math.nan,
        "same_day_buyback_count": same,
        "same_day_buyback_rate": same / n if n else math.nan,
        "by_next_day_buyback_count": next_,
        "by_next_day_buyback_rate": next_ / n if n else math.nan,
        "by_next_day_rate_ci95_low": ci_low,
        "by_next_day_rate_ci95_high": ci_high,
        "fly_count": fly,
        "fly_rate": fly / n if n else math.nan,
        "median_max_retracement_pct": float(trades["max_retracement_pct"].median()) if n else math.nan,
        "mean_max_retracement_pct": float(trades["max_retracement_pct"].mean()) if n else math.nan,
        "median_max_adverse_pct": float(trades["max_adverse_pct"].median()) if n else math.nan,
    }


def threshold_sweep(
    dates: list[pd.Timestamp],
    minute_by_date: dict[pd.Timestamp, pd.DataFrame],
    prev_close_by_date: dict[pd.Timestamp, float],
) -> pd.DataFrame:
    split = int(len(dates) * 0.7)
    train_dates, test_dates = dates[:split], dates[split:]
    rows = []
    for pct in np.arange(0.010, 0.0301, 0.002):
        cfg = Config(lower_pct=float(round(pct, 3)))
        for subset, subset_dates in [("earlier_70pct", train_dates), ("recent_30pct", test_dates)]:
            trades = backtest_variant(subset_dates, minute_by_date, prev_close_by_date, cfg, "fixed")
            summary = summarize(trades, len(subset_dates), subset)
            rows.append({"lower_pct": cfg.lower_pct, **summary})
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    daily = load_daily()
    daily_map = daily.set_index("date")
    minute_by_date: dict[pd.Timestamp, pd.DataFrame] = {}
    qa_rows = []
    pattern = re.compile(r"ticks_300308_(\d{8})\.csv$")
    for path in sorted(CACHE.glob("ticks_300308_*.csv")):
        match = pattern.match(path.name)
        if not match or path.stat().st_size == 0:
            continue
        date = pd.to_datetime(match.group(1), format="%Y%m%d")
        if date not in daily_map.index or pd.isna(daily_map.loc[date, "prev_close"]):
            continue
        minute = load_minutes(path)
        if len(minute) < 120:
            continue
        minute_by_date[date] = minute
        daily_close = float(daily_map.loc[date, "close"])
        tick_close = float(minute.iloc[-1]["close"])
        qa_rows.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "minutes": len(minute),
                "first_minute": minute.iloc[0]["time"],
                "last_minute": minute.iloc[-1]["time"],
                "daily_close": daily_close,
                "tick_close": tick_close,
                "close_abs_diff_pct": abs(tick_close - daily_close) / daily_close,
            }
        )
    dates = sorted(minute_by_date)
    prev_close_by_date = {date: float(daily_map.loc[date, "prev_close"]) for date in dates}
    cfg = Config()
    all_trades = []
    summaries = []
    split = int(len(dates) * 0.7)
    periods = [("full", dates), ("earlier_70pct", dates[:split]), ("recent_30pct", dates[split:])]
    for variant in ["fixed", "vwap_dynamic"]:
        trades = backtest_variant(dates, minute_by_date, prev_close_by_date, cfg, variant)
        all_trades.append(trades)
        for label, period_dates in periods:
            period_set = {d.strftime("%Y-%m-%d") for d in period_dates}
            subset = trades[trades["date"].isin(period_set)].copy()
            summaries.append({"variant": variant, **summarize(subset, len(period_dates), label)})

    trades_all = pd.concat(all_trades, ignore_index=True)
    summary_df = pd.DataFrame(summaries)
    sweep_df = threshold_sweep(dates, minute_by_date, prev_close_by_date)
    qa_df = pd.DataFrame(qa_rows)
    day_stats = []
    for date in dates:
        m = minute_by_date[date]
        prev = prev_close_by_date[date]
        day_stats.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "prev_close": prev,
                "day_high": float(m["high"].max()),
                "touched_lower_1_8": bool(m["high"].max() >= prev * 1.018),
                "touched_upper_2_8": bool(m["high"].max() >= prev * 1.028),
            }
        )
    day_stats_df = pd.DataFrame(day_stats)

    qa_summary = {
        "data_start": dates[0].strftime("%Y-%m-%d"),
        "data_end": dates[-1].strftime("%Y-%m-%d"),
        "eligible_days": len(dates),
        "median_minutes_per_day": float(qa_df["minutes"].median()),
        "days_close_mismatch_over_0_1pct": int((qa_df["close_abs_diff_pct"] > 0.001).sum()),
        "max_close_abs_diff_pct": float(qa_df["close_abs_diff_pct"].max()),
        "lower_touch_days": int(day_stats_df["touched_lower_1_8"].sum()),
        "upper_touch_days": int(day_stats_df["touched_upper_2_8"].sum()),
    }
    result = {
        "strategy": {
            "formula": "C>=lower AND C<REF(C,1) AND C<MA(C,3) AND VOL<REF(VOL,1)*1.2; FILTER(...,15)",
            "fixed_lower_pct": cfg.lower_pct,
            "fixed_upper_pct": cfg.upper_pct,
            "execution": "signal at minute close; sell at next minute open; first signal per day",
            "buyback_success": "same-day or next-session low <= sell price * (1-0.8%)",
            "fly": "no 0.8% buyback through next session AND later high >= sell price * (1+2%)",
        },
        "qa": qa_summary,
        "summary": summary_df.replace({np.nan: None}).to_dict(orient="records"),
    }
    trades_all.to_csv(OUT_DIR / "trades.csv", index=False, encoding="utf-8-sig")
    summary_df.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    sweep_df.to_csv(OUT_DIR / "threshold_sweep.csv", index=False, encoding="utf-8-sig")
    qa_df.to_csv(OUT_DIR / "data_quality.csv", index=False, encoding="utf-8-sig")
    day_stats_df.to_csv(OUT_DIR / "day_touches.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
