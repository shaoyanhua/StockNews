# -*- coding: utf-8 -*-
"""
F1 策略回测：在 V1 入场基础上加「市场状态门 + 持有期延长 + 跳空规则」。

设计动机（来自 backtest_daily.py 的结果）：
  1. V1 入场有选股力（高开概率 59% vs 基准 38%）→ 入场保留；
  2. 隔夜毛收益 +0.57% 被 0.35% 成本吃掉六成 → 延长持有摊薄成本；
  3. 盈利集中在情绪活跃月 → 市场状态门（HS300>MA20 且 宽度>50%）；
  4. 跳空规则：高开≥+2.5% 开盘兑现；低开≤-2% 开盘止损；其余持有至第3日收盘。

⚠️ 诚实声明：本策略设计基于同一段历史的观察，属于样本内改进，
   存在过拟合风险；报告中给出前后半段分割与逐变体结果供自查。

用法: python backtest_f1.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_sources import DATA_DIR
from backtest_daily import prep_stock, load_index, START, END, COST

BT_DIR = DATA_DIR / "backtest"
SPLIT = "2026-01-01"          # 前后半段分割点
GAP_TP, GAP_SL = 0.025, -0.02  # 跳空兑现 / 跳空止损


def build_breadth(files, fs_map) -> pd.DataFrame:
    """全市场宽度：每日 close>MA20 的比例（当日收盘可得，无未来函数）。"""
    fp = BT_DIR / "breadth.csv"
    if fp.exists():
        return pd.read_csv(fp, dtype={"date": str}).set_index("date")
    cnt = {}
    for i, f in enumerate(files):
        if f.stem not in fs_map:
            continue
        df = pd.read_csv(f, usecols=["date", "close"], dtype={"date": str})
        ma20 = df["close"].rolling(20).mean()
        above = (df["close"] > ma20).values
        for d, a in zip(df["date"].values[20:], above[20:]):
            t = cnt.setdefault(d, [0, 0])
            t[0] += 1
            t[1] += int(a)
        if (i + 1) % 500 == 0:
            print(f"  宽度 {i+1}/{len(files)}", flush=True)
    out = pd.DataFrame([(d, v[0], v[1]) for d, v in sorted(cnt.items())],
                       columns=["date", "n", "n_above"])
    out["breadth"] = out["n_above"] / out["n"]
    out.to_csv(fp, index=False)
    return out.set_index("date")


def main():
    uni = pd.read_csv(DATA_DIR / "universe.csv", dtype={"code": str})
    uni["code"] = uni["code"].str.zfill(6)
    fs_map = dict(zip(uni["code"], uni["float_shares"]))
    name_map = dict(zip(uni["code"], uni["name"]))
    idx = load_index()
    idx_ma20 = pd.read_csv(DATA_DIR / "index_hs300.csv", dtype={"date": str}).set_index("date")["close"]
    regime_idx = (idx_ma20 > idx_ma20.rolling(20).mean())

    files = sorted((DATA_DIR / "daily").glob("*.csv"))
    breadth = build_breadth(files, fs_map)
    regime = pd.DataFrame({
        "idx_above_ma20": regime_idx,
        "breadth": breadth["breadth"],
    })
    regime["gate"] = regime["idx_above_ma20"] & (regime["breadth"] >= 0.50)
    print(f"市场状态门开启天数: {int(regime.loc[START:END,'gate'].sum())} / "
          f"{len(regime.loc[START:END])}")

    rows = []
    for i, f in enumerate(files):
        code = f.stem
        fs = fs_map.get(code)
        if not fs or fs <= 0:
            continue
        df = prep_stock(f, fs)
        if df is None:
            continue
        # 多日退出所需列
        for k in (1, 2, 3, 5):
            df[f"close_{k}"] = df["close"].shift(-k)
        m = (df["date"] >= START) & (df["date"] <= END) & df["open_next"].notna() & (df["bar_i"] >= 130)
        w = df[m]
        if w.empty:
            continue
        j = w.join(idx[["idx_pct"]], on="date").join(regime[["gate", "breadth"]], on="date")
        base_ok = j["ma60"].notna() & j["idx_pct"].notna()

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
              & (j["dist_ma5"] <= 5) & (j["dist_ma20"] <= 12) & (j["pct3"] <= 15)
              & (j["pct"] > j["idx_pct"])          # 与主回测一致：跑赢大盘
              & (j["clv"] >= 0.85))

        # E2 宽入：放宽带宽换更多样本（换手/市值/量比上限放开，涨幅2-6）
        e2 = (base_ok
              & j["pct"].between(2.0, 6.0)
              & (j["vol_ratio5"] >= 1.1)
              & j["turnover"].between(2, 10)
              & j["cap_yi"].between(50, 800)
              & (j["amount_yi"] >= 5)
              & (j["close"] > j["ma5"]) & (j["close"] > j["ma20"]) & (j["close"] > j["ma60"])
              & (j["ma5"] > j["ma10"]) & (j["ma10"] > j["ma20"]) & (j["ma20"] > j["ma60"])
              & j["ma5_up3"] & j["ma10_up3"] & j["ma20_up"]
              & (j["dist_ma5"] <= 6) & (j["dist_ma20"] <= 15) & (j["pct3"] <= 18)
              & (j["clv"] >= 0.80))

        for tag, mask in (("v1", v1), ("e2", e2)):
            hit = j[mask]
            if hit.empty:
                continue
            rows.append(pd.DataFrame({
                "date": hit["date"], "code": code, "name": name_map.get(code, ""),
                "entry": tag, "gate": hit["gate"].fillna(False),
                "close": hit["close"],
                "gap": hit["open_next"] / hit["close"] - 1,
                "ret_open1": hit["open_next"] / hit["close"] - 1,
                "ret_close1": hit["close_1"] / hit["close"] - 1,
                "ret_close2": hit["close_2"] / hit["close"] - 1,
                "ret_close3": hit["close_3"] / hit["close"] - 1,
                "ret_close5": hit["close_5"] / hit["close"] - 1,
            }))
        if (i + 1) % 500 == 0:
            print(f"  信号 {i+1}/{len(files)}", flush=True)

    sig = pd.concat(rows, ignore_index=True)
    # 条件退出：高开兑现 / 低开止损 / 否则持有3日
    sig["ret_cond"] = np.where(sig["gap"] >= GAP_TP, sig["gap"],
                        np.where(sig["gap"] <= GAP_SL, sig["gap"], sig["ret_close3"]))
    sig.to_csv(BT_DIR / "signals_f1.csv", index=False, encoding="utf-8-sig")

    def summ(name, s, col):
        r = s[col].dropna()
        if len(r) < 3:
            return None
        net = r - COST
        t = net.mean() / (net.std(ddof=1) / np.sqrt(len(net))) if net.std() > 0 else 0
        wins, losses = net[net > 0], net[net <= 0]
        return {"策略": name, "N": len(net),
                "净胜率%": round((net > 0).mean() * 100, 1),
                "平均净%": round(net.mean() * 100, 3),
                "中位净%": round(net.median() * 100, 3),
                "平均盈%": round(wins.mean() * 100, 2) if len(wins) else 0,
                "平均亏%": round(losses.mean() * 100, 2) if len(losses) else 0,
                "P10%": round(net.quantile(.1) * 100, 2),
                "P90%": round(net.quantile(.9) * 100, 2),
                "t值": round(t, 2)}

    res = []
    for entry in ("v1", "e2"):
        s0 = sig[sig["entry"] == entry]
        sg = s0[s0["gate"]]
        for label, s in ((f"{entry}·无门", s0), (f"{entry}·门", sg)):
            for col, ex in (("ret_open1", "开1"), ("ret_close2", "收2"),
                            ("ret_close3", "收3"), ("ret_close5", "收5"),
                            ("ret_cond", "条件")):
                r = summ(f"{label}·{ex}", s, col)
                if r:
                    res.append(r)
    out = pd.DataFrame(res)
    out.to_csv(BT_DIR / "summary_f1.csv", index=False, encoding="utf-8-sig")
    print("\n===== 变体矩阵（净值，成本0.35%）=====")
    print(out.to_string(index=False))

    # 头号方案的前后半段 + 分月
    print("\n===== 前后半段分割（过拟合自查）=====")
    for entry in ("v1", "e2"):
        s = sig[(sig["entry"] == entry) & sig["gate"]]
        for label, part in (("H1(25-07~12)", s[s["date"] < SPLIT]),
                            ("H2(26-01~07)", s[s["date"] >= SPLIT])):
            for col in ("ret_open1", "ret_cond"):
                r = summ(f"{entry}·门·{col}·{label}", part, col)
                if r:
                    print(pd.DataFrame([r]).to_string(index=False, header=(col == "ret_open1" and label.startswith("H1"))))

    s = sig[(sig["entry"] == "e2") & sig["gate"]].copy()
    s["month"] = s["date"].str[:7]
    s["net"] = s["ret_cond"] - COST
    bym = s.groupby("month").agg(N=("net", "size"),
                                  净胜率=("net", lambda x: round((x > 0).mean() * 100, 1)),
                                  平均净万分之=("net", lambda x: round(x.mean() * 1e4, 1)))
    print("\n===== e2·门·条件退出 分月 =====")
    print(bym.to_string())


if __name__ == "__main__":
    main()
