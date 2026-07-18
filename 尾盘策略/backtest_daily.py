# -*- coding: utf-8 -*-
"""
尾盘隔夜策略 —— 日线降级回测（近似版）。

⚠️ 重要局限（详见 回测报告_降级版.md）：
  - 文档所有条件定义在 14:30 盘中，此处用【收盘】数据近似 → 结果只是粗略参考；
  - 分时类条件（均价线占比、尾盘突破细节、板块过滤）无法用日线验证，未纳入；
  - 换手率/量比/成交额用 当前流通股本 + 不复权收盘 估算，存在股本变动误差；
  - 卖出用【次日开盘价】（隔夜跳空是该策略核心赌注），文档的 9:45/10:00 规则
    需分钟数据，日线测不了；另给出次日收盘卖出作对照。

策略组：
  video   视频原版近似（涨幅3-5%、换手5-10%、市值50-200亿、量比>1、多头排列、强收盘）
  v1      增强版V1近似（3.1 中可日线化的全部条件 + 收盘位置上部15%）
  bare    裸动量对照（仅 涨幅2.5-5.5% + 收盘位置上部15%）
  ALL     全市场基准（候选池所有股票日，无条件）

用法: python backtest_daily.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_sources import DATA_DIR

BT_DIR = DATA_DIR / "backtest"
BT_DIR.mkdir(parents=True, exist_ok=True)

START, END = "2025-07-01", "2026-07-14"   # 信号日窗口（END 需留出次日）
COST = 0.0035                             # 双边佣金+印花税+滑点 ≈ 0.35%


def load_index() -> pd.DataFrame:
    idx = pd.read_csv(DATA_DIR / "index_hs300.csv", dtype={"date": str})
    idx["idx_pct"] = idx["close"].pct_change() * 100
    rng = (idx["high"] - idx["low"]).replace(0, np.nan)
    idx["idx_clv"] = ((idx["close"] - idx["low"]) / rng).fillna(0.5)
    idx["idx_open_next"] = idx["open"].shift(-1)
    idx["idx_ret_open"] = idx["idx_open_next"] / idx["close"] - 1
    shc = pd.read_csv(DATA_DIR / "index_shcomp.csv", dtype={"date": str})
    shc["shc_pct"] = shc["close"].pct_change() * 100
    idx = idx.merge(shc[["date", "shc_pct"]], on="date", how="left")
    return idx.set_index("date")


def prep_stock(fp: Path, float_shares: float) -> pd.DataFrame | None:
    df = pd.read_csv(fp, dtype={"date": str})
    if len(df) < 130:
        return None
    if fp.stem.startswith("68"):
        df["vol"] = df["vol"] / 100.0  # 腾讯日K：科创板成交量单位是股，其他板是手
    c = df["close"]
    df["pct"] = c.pct_change() * 100
    for n in (5, 10, 20, 60):
        df[f"ma{n}"] = c.rolling(n).mean()
    for n in (5, 10, 20):
        up = df[f"ma{n}"] > df[f"ma{n}"].shift(1)
        df[f"ma{n}_up"] = up
        df[f"ma{n}_up3"] = up & up.shift(1, fill_value=False) & up.shift(2, fill_value=False)
    df["vol_ratio5"] = df["vol"] / df["vol"].rolling(5).mean().shift(1)
    df["turnover"] = df["vol"] * 100 / float_shares * 100          # %
    df["amount_yi"] = df["vol"] * 100 * df["close_raw"] / 1e8       # 亿（近似）
    df["cap_yi"] = float_shares * df["close_raw"] / 1e8             # 亿（近似）
    df["pct3"] = (c / c.shift(3) - 1) * 100
    df["dist_ma5"] = (c / df["ma5"] - 1) * 100
    df["dist_ma20"] = (c / df["ma20"] - 1) * 100
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    df["clv"] = ((df["close"] - df["low"]) / rng).fillna(1.0)       # 一字视为强收盘
    df["ohlc4"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    df["vol_up"] = df["vol"] > df["vol"].shift(1)
    df["open_next"] = df["open"].shift(-1)
    df["close_next"] = df["close"].shift(-1)
    df["low_next_raw"] = df["low_raw"].shift(-1)
    df["open_next_raw"] = df["open_raw"].shift(-1)
    df["high_next_raw"] = df["high_raw"].shift(-1)
    df["bar_i"] = np.arange(len(df))
    return df


def main():
    uni = pd.read_csv(DATA_DIR / "universe.csv", dtype={"code": str})
    uni["code"] = uni["code"].str.zfill(6)
    fs_map = dict(zip(uni["code"], uni["float_shares"]))
    name_map = dict(zip(uni["code"], uni["name"]))
    idx = load_index()

    sig_rows = {"video": [], "v1": [], "bare": []}
    all_days = []   # 全市场基准

    files = sorted((DATA_DIR / "daily").glob("*.csv"))
    print(f"股票文件: {len(files)}")
    for i, fp in enumerate(files):
        code = fp.stem
        fs = fs_map.get(code)
        if not fs or fs <= 0:
            continue
        df = prep_stock(fp, fs)
        if df is None:
            continue
        m = (df["date"] >= START) & (df["date"] <= END) & df["open_next"].notna() & (df["bar_i"] >= 130)
        w = df[m]
        if w.empty:
            continue
        j = w.join(idx[["idx_pct", "idx_clv", "idx_ret_open", "shc_pct"]], on="date")

        # 全市场基准（抽样存储收益即可）
        all_days.append(pd.DataFrame({
            "date": j["date"],
            "ret_open": j["open_next"] / j["close"] - 1,
            "ret_close": j["close_next"] / j["close"] - 1,
        }))

        base_ok = j["ma60"].notna() & j["idx_pct"].notna() & j["shc_pct"].notna()

        video = (base_ok
                 & j["pct"].between(3, 5)
                 & (j["vol_ratio5"] > 1)
                 & j["turnover"].between(5, 10)
                 & j["cap_yi"].between(50, 200)
                 & j["vol_up"]
                 & (j["close"] > j["ma5"])
                 & (j["ma5"] > j["ma10"]) & (j["ma10"] > j["ma20"]) & (j["ma20"] > j["ma60"])
                 & j["ma5_up"] & j["ma10_up"] & j["ma20_up"]
                 & (j["pct"] > j["shc_pct"])       # 视频语境"大盘"=上证指数
                 & (j["clv"] >= 0.85)
                 & (j["close"] >= j["ohlc4"]))

        v1 = (base_ok
              & j["pct"].between(2.5, 5.5)
              & j["vol_ratio5"].between(1.2, 2.5)
              & j["turnover"].between(3, 8)
              & j["cap_yi"].between(100, 500)
              & (j["amount_yi"] >= 8)
              & (j["close"] > j["ma5"]) & (j["close"] > j["ma10"])
              & (j["close"] > j["ma20"]) & (j["close"] > j["ma60"])
              & (j["ma5"] > j["ma10"]) & (j["ma10"] > j["ma20"]) & (j["ma20"] > j["ma60"])
              & j["ma5_up3"] & j["ma10_up3"] & j["ma20_up3"]
              & (j["dist_ma5"] <= 5)
              & (j["dist_ma20"] <= 12)
              & (j["pct3"] <= 15)
              & (j["pct"] > j["idx_pct"])          # 对照表：V1须跑赢大盘(沪深300)
              & (j["clv"] >= 0.85))

        bare = base_ok & j["pct"].between(2.5, 5.5) & (j["clv"] >= 0.85)

        for tag, mask in (("video", video), ("v1", v1), ("bare", bare)):
            hit = j[mask]
            if hit.empty:
                continue
            r = pd.DataFrame({
                "date": hit["date"], "code": code,
                "name": name_map.get(code, ""),
                "close": hit["close"], "pct": hit["pct"],
                "turnover": hit["turnover"], "cap_yi": hit["cap_yi"],
                "amount_yi": hit["amount_yi"], "vol_ratio5": hit["vol_ratio5"],
                "clv": hit["clv"],
                "ret_open": hit["open_next"] / hit["close"] - 1,
                "ret_close": hit["close_next"] / hit["close"] - 1,
                "idx_pct": hit["idx_pct"], "idx_clv": hit["idx_clv"],
                "idx_ret_open": hit["idx_ret_open"],
                # 次日一字跌停无法卖出的标记（开=最高=跌停附近）
                "next_locked_down": (hit["open_next_raw"] <= hit["close_raw"] * 0.905)
                                    & (hit["high_next_raw"] <= hit["open_next_raw"] * 1.002),
            })
            sig_rows[tag].append(r)
        if (i + 1) % 500 == 0:
            print(f"  已处理 {i+1}/{len(files)}", flush=True)

    all_df = pd.concat(all_days, ignore_index=True)
    all_df.to_csv(BT_DIR / "baseline_all_days.csv", index=False)

    def summarize(name, s: pd.DataFrame) -> dict:
        g = s["ret_open"]
        net = g - COST
        wins, losses = net[net > 0], net[net <= 0]
        return {
            "策略": name, "信号数": len(s),
            "交易日数": s["date"].nunique() if "date" in s else "",
            "高开概率%": round((g > 0).mean() * 100, 1),
            "净胜率%(开盘卖)": round((net > 0).mean() * 100, 1),
            "平均毛收益%": round(g.mean() * 100, 3),
            "平均净收益%": round(net.mean() * 100, 3),
            "中位净收益%": round(net.median() * 100, 3),
            "平均盈利%": round(wins.mean() * 100, 3) if len(wins) else 0,
            "平均亏损%": round(losses.mean() * 100, 3) if len(losses) else 0,
            "P10%": round(net.quantile(0.1) * 100, 2),
            "P90%": round(net.quantile(0.9) * 100, 2),
            "次日收盘卖净胜率%": round(((s["ret_close"] - COST) > 0).mean() * 100, 1) if "ret_close" in s else "",
            "次日收盘卖平均净%": round((s["ret_close"] - COST).mean() * 100, 3) if "ret_close" in s else "",
        }

    results = []
    for tag in ("video", "v1", "bare"):
        if sig_rows[tag]:
            s = pd.concat(sig_rows[tag], ignore_index=True).sort_values("date")
            s.to_csv(BT_DIR / f"signals_{tag}.csv", index=False, encoding="utf-8-sig")
            results.append(summarize(tag, s))
            # 大盘过滤后的变体
            f = s[(s["idx_pct"] >= -1.0) & (s["idx_clv"] >= 0.3)]
            results.append(summarize(tag + "+大盘过滤", f))
        else:
            print(f"[warn] {tag} 无信号")
    results.append(summarize("ALL全市场基准", all_df.assign(date=all_df["date"])))

    out = pd.DataFrame(results)
    out.to_csv(BT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    print("\n===== 汇总 =====")
    print(out.to_string(index=False))

    # 分月表现（v1 与 video，开盘卖净收益）
    for tag in ("video", "v1"):
        f = BT_DIR / f"signals_{tag}.csv"
        if f.exists():
            s = pd.read_csv(f, dtype={"date": str})
            s["month"] = s["date"].str[:7]
            s["net"] = s["ret_open"] - COST
            bym = s.groupby("month").agg(信号数=("net", "size"),
                                          净胜率=("net", lambda x: round((x > 0).mean() * 100, 1)),
                                          平均净收益万分之=("net", lambda x: round(x.mean() * 1e4, 1)))
            bym.to_csv(BT_DIR / f"bymonth_{tag}.csv", encoding="utf-8-sig")
            print(f"\n===== {tag} 分月 =====")
            print(bym.to_string())


if __name__ == "__main__":
    main()
