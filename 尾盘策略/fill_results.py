# -*- coding: utf-8 -*-
"""
次日回填：读取 records/signals_YYYYMMDD.csv，在【次日 10:05 后】运行，
按《尾盘隔夜策略》§5 退出规则机械计算实际收益并回填。

退出规则近似（§5，全部用 1 分钟分时价）：
  高开>2.5%          → 9:35 价卖出
  高开0.3%~2.5%      → 9:45 前创开盘价新高(≥+0.2%) → 10:00 卖，否则 9:45 卖
  平开(±0.3%)        → 9:45 站上昨收 → 10:00 卖，否则 9:45 卖
  低开<-0.3%         → 9:35 未收复昨收 → 9:35 卖；已收复 → 10:00 卖

用法:
  python fill_results.py             # 回填最近一份未完成的记录
  python fill_results.py 20260716    # 指定信号日期
"""
import sys
import time
import random
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_sources import BASE_DIR, new_session, tencent_minute_today

RECORDS_DIR = BASE_DIR / "records"
COST = 0.0035  # 双边佣金+印花税+滑点


def price_at(m: pd.DataFrame, t: str) -> float | None:
    x = m[m["time"] <= t]
    return float(x["price"].iloc[-1]) if len(x) else None


def decide_exit(m: pd.DataFrame, prev_close: float) -> tuple[str, float, float]:
    """返回 (退出时刻, 退出价, 高开幅度%)。"""
    open_p = float(m["price"].iloc[0])
    gap = (open_p / prev_close - 1) * 100
    p0935, p0945, p1000 = price_at(m, "0935"), price_at(m, "0945"), price_at(m, "1000")
    if gap > 2.5:
        return "0935", p0935, gap
    if gap >= 0.3:
        hi_0945 = m[m["time"] <= "0945"]["price"].max()
        return ("1000", p1000, gap) if hi_0945 >= open_p * 1.002 else ("0945", p0945, gap)
    if gap > -0.3:
        return ("1000", p1000, gap) if (p0945 or 0) >= prev_close else ("0945", p0945, gap)
    # 低开
    return ("0935", p0935, gap) if (p0935 or 0) < prev_close else ("1000", p1000, gap)


def main():
    session = new_session()
    if len(sys.argv) > 1:
        fp = RECORDS_DIR / f"signals_{sys.argv[1]}.csv"
    else:
        cands = sorted(RECORDS_DIR.glob("signals_*.csv"))
        if not cands:
            print("records/ 下没有信号文件")
            return
        fp = cands[-1]
    df = pd.read_csv(fp, dtype={"代码": str, "日期": str})
    for col in ["次日开盘", "9:45", "10:00", "固定规则收益", "沪深300同期", "超额收益", "是否高开", "备注"]:
        if col in df.columns:
            df[col] = df[col].astype("object")
    sig_date = str(df["日期"].iloc[0])
    print(f"回填 {fp.name}（信号日 {sig_date}）")

    # 沪深300 次日分时
    idx_m = tencent_minute_today("sh000300", session=session)
    idx_date = idx_m.attrs["date"]
    if idx_date <= sig_date:
        print(f"⚠️ 当前最新会话 {idx_date} 不晚于信号日 {sig_date}，请在次日 10:05 后运行。")
        return
    idx_prev = idx_m.attrs.get("prev_close")

    for i, row in df.iterrows():
        code = str(row.get("代码") or "").zfill(6)
        if not code.strip("0") or pd.isna(row.get("尾盘价")):
            continue
        try:
            m = tencent_minute_today(code, session=session)
            time.sleep(0.1 + random.random() * 0.1)
        except Exception as e:  # noqa: BLE001
            print(f"  {code} 分时获取失败: {e}")
            continue
        if m.attrs["date"] != idx_date:
            print(f"  {code} 会话日期不符({m.attrs['date']})，跳过")
            continue
        prev_close = m.attrs.get("prev_close") or float(row["尾盘价"])
        buy = float(row["尾盘价"])
        exit_t, exit_p, gap = decide_exit(m, prev_close)
        if exit_p is None:
            continue
        ret = exit_p / buy - 1 - COST
        idx_ret = (price_at(idx_m, exit_t) / idx_prev - 1) if idx_prev else 0.0
        df.loc[i, "次日开盘"] = float(m["price"].iloc[0])
        df.loc[i, "9:45"] = price_at(m, "0945")
        df.loc[i, "10:00"] = price_at(m, "1000")
        df.loc[i, "固定规则收益"] = round(ret * 100, 2)
        df.loc[i, "沪深300同期"] = round(idx_ret * 100, 2)
        df.loc[i, "超额收益"] = round((ret - idx_ret) * 100, 2)
        df.loc[i, "是否高开"] = "是" if gap > 0 else "否"
        note = str(row.get("备注") or "")
        df.loc[i, "备注"] = f"{note} 退出{exit_t} 高开{gap:+.2f}%".strip()
        print(f"  {code} {row['名称']}: 买{buy} → {exit_t}卖{exit_p}  净收益 {ret*100:+.2f}%（含成本0.35%）")

    df.to_csv(fp, index=False, encoding="utf-8-sig")
    print(f"已回填 {fp}")


if __name__ == "__main__":
    main()
