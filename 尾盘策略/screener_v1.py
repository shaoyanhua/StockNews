# -*- coding: utf-8 -*-
"""
尾盘隔夜策略 · 增强版V1 筛选器
================================
按《尾盘隔夜策略_视频原版与增强版.md》§3.1–3.4 的机械条件筛选候选股。

用法:
  python screener_v1.py              # 盘中 14:35–14:50 运行 = 实时筛选
  python screener_v1.py --at 1445    # 指定截止时刻（默认 1445）
  收盘后/凌晨运行 = 自动回放最近一个交易日（复盘/验证用，数据同源）

输出:
  控制台候选表 + records/signals_YYYYMMDD.csv（第七章模板，次日字段留空，
  由 fill_results.py 次日回填）

数据源: 腾讯(行情/日K/分时) + 新浪(全市场快照/行业板块) + 同花顺(题材归因)。
无法机械验证的条件(公告面、龙头跳水等)标记 NA，在输出中提示人工确认。
"""
import argparse
import sys
import time
import random
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_sources import (BASE_DIR, DATA_DIR, new_session, sina_market_snapshot, a_share_universe,
                          tencent_quote, tencent_day_kline, tencent_minute_today,
                          sina_industry_boards, industry_map, ths_hot_reason, code_prefix)

RECORDS_DIR = BASE_DIR / "records"
RECORDS_DIR.mkdir(exist_ok=True)

# ── §3.1 初筛阈值（文档默认值，调整须开新版本）─────────────────────
TH = dict(
    pct=(2.5, 5.5), vol_ratio=(1.2, 2.5), turnover=(3.0, 8.0),
    cap_yi=(100, 500), amount_yi=8.0,
    dist_ma5=5.0, dist_ma20=12.0, pct3=15.0,
    afternoon_above_avg=0.80,        # §3.3.1 午后均价线上方时间占比
    break_min=0.002,                 # §3.4.2 突破幅度 ≥0.2%
    win_1430_1450=(0.5, 1.5),        # §3.4.1 14:30后涨幅区间 %
    clv=0.85,                        # §3.4.5 收盘位置上部15%
    rush10=1.5,                      # §3.4.6 最后10分钟拉升>1.5%排除
    board_top_pct=20,                # §3.2.3 板块涨幅前20%
    beat_board=1.0,                  # §3.2.5 跑赢行业 ≥1pct
)
# 粗筛缓冲带（新浪快照值与14:30值有偏差，放宽后由精筛收口）
COARSE = dict(pct=(1.0, 8.5), turnover=(2.0, 13.0), cap=(90, 520), amount_yi=7.0)


def hhmm(s):  # "0931"→分钟序号
    return int(s[:2]) * 60 + int(s[2:])


# ── 5分钟K线（§3.4 及"简化判定"，2026-07-16 文档新增）────────────

def five_min_bars(m: pd.DataFrame) -> pd.DataFrame:
    """由1分钟分时合成5分钟K线。价格用每分钟最新价合成，高低点为近似值。
    bar 索引: 0-23=上午(0935..1130), 24-47=下午(1305..1500)，集合竞价并入第一根。"""
    def bar_id(t):
        x = hhmm(t)
        if x <= 570:
            return 0
        if x <= 690:
            return (x - 571) // 5
        if x <= 780:
            return 23
        if x <= 900:
            return 24 + (x - 781) // 5
        return 47
    g = m.copy()
    g["bar"] = g["time"].map(bar_id)
    bars = g.groupby("bar").agg(open=("price", "first"), close=("price", "last"),
                                high=("price", "max"), low=("price", "min"))
    cum = g.groupby("bar")["cum_amt"].max()
    bars["amount"] = cum.diff()
    if len(bars):
        bars.iloc[0, bars.columns.get_loc("amount")] = cum.iloc[0]
    return bars


def bar_end_min(b: int) -> int:
    return 570 + 5 * (b + 1) if b <= 23 else 780 + 5 * (b - 23)


def five_min_checks(m: pd.DataFrame, cutoff: str) -> dict:
    """§3.4 简化判定：5条至少满足4条且第1条必须满足；尾盘不足3根完整5分K不提前确认。
    另检 §3.4.11 单根急拉>1.5%且随后无法走强。"""
    cut = hhmm(cutoff)
    bars = five_min_bars(m)
    done = bars[[bar_end_min(b) <= cut for b in bars.index]]
    tail = done[[bar_end_min(b) > 870 for b in done.index]]      # 14:30 后
    if len(done) < 8 or len(tail) < 3:
        return {"trend5": None, "no_spike_fail": None}
    closes = done["close"]
    ma5 = closes.rolling(5).mean()
    c1 = bool(closes.iloc[-1] > ma5.iloc[-1])                    # 必须满足
    c2 = bool(ma5.iloc[-1] > ma5.iloc[-2])
    c3 = int((closes.diff().iloc[-3:] > 0).sum()) >= 2           # 3根中至少2根收盘抬高
    h, l = done["high"].iloc[-3:].values, done["low"].iloc[-3:].values
    c4 = not (h[0] > h[1] > h[2] and l[0] > l[1] > l[2])         # 高低点同时连续下移
    win = done.iloc[-6:]
    ch = win["close"].diff()
    up_a, dn_a = win["amount"][ch > 0], win["amount"][ch < 0]
    c5 = True if len(dn_a) == 0 else (bool(up_a.mean() > dn_a.mean()) if len(up_a) else False)
    trend5 = c1 and (c1 + c2 + c3 + c4 + c5) >= 4
    # §3.4.11：尾盘单根5分K涨幅>1.5%，且下一根收盘更低 → 排除
    gain = (tail["close"] / tail["open"] - 1) * 100
    spike_fail = any(gain.iloc[i] > 1.5 and tail["close"].iloc[i + 1] < tail["close"].iloc[i]
                     for i in range(len(tail) - 1))
    return {"trend5": bool(trend5), "no_spike_fail": not spike_fail}


def minute_metrics(mdf: pd.DataFrame, cutoff: str, prev_close: float) -> dict:
    """从分时数据(截止 cutoff)计算所有盘中指标。"""
    m = mdf[mdf["time"] <= cutoff].copy()
    if len(m) < 60:
        return {}
    cur = m.iloc[-1]
    day_high, day_low = m["price"].max(), m["price"].min()
    res = dict(
        price=cur["price"],
        pct=(cur["price"] / prev_close - 1) * 100,
        amount_yi=cur["cum_amt"] / 1e8,
        clv=1.0 if day_high == day_low else (cur["price"] - day_low) / (day_high - day_low),
    )
    # §3.3.1 午后(≥1300)在均价线上方占比
    pm = m[m["time"] >= "1300"]
    res["above_avg_ratio"] = float((pm["price"] >= pm["avg_price"]).mean()) if len(pm) else np.nan
    # §3.3.2 均价线向上（比30分钟前高）
    if len(m) > 30:
        res["avg_rising"] = bool(cur["avg_price"] > m["avg_price"].iloc[-31])
    # §3.3.3 14:00-14:30 成交额 vs 13:30-14:00
    a1 = m[(m["time"] > "1330") & (m["time"] <= "1400")]["cum_amt"]
    a2 = m[(m["time"] > "1400") & (m["time"] <= "1430")]["cum_amt"]
    if len(a1) and len(a2):
        amt_1330 = a1.iloc[-1] - m[m["time"] <= "1330"]["cum_amt"].iloc[-1]
        amt_1400 = a2.iloc[-1] - a1.iloc[-1]
        res["vol_shift_up"] = bool(amt_1400 > amt_1330)
        # §3.3.4 放量滞涨：后半时段放量1.5倍以上但价格下跌
        p_1400 = m[m["time"] <= "1400"]["price"].iloc[-1]
        p_1430 = m[m["time"] <= "1430"]["price"].iloc[-1]
        res["vol_no_stall"] = not (amt_1400 > 1.5 * amt_1330 and p_1430 < p_1400)
    # §3.3.5 14:30后跌破均价线需5分钟内收回
    after = m[m["time"] > "1430"]
    if len(after):
        below = (after["price"] < after["avg_price"]).astype(int)
        run = below.groupby((below != below.shift()).cumsum()).cumsum()
        res["reclaim_5min"] = bool(run.max() <= 5)
        # §3.4.1 14:30→cutoff 涨幅
        p1430 = m[m["time"] <= "1430"]["price"].iloc[-1]
        res["win_pct"] = (cur["price"] / p1430 - 1) * 100
        # §3.4.2/3/4 突破当日(14:30前)高点确认
        pre_high = m[m["time"] <= "1430"]["price"].max()
        brk = after[after["price"] >= pre_high * (1 + TH["break_min"])]
        res["break_high"] = bool(len(brk))
        if len(brk):
            t0 = brk.index[0]
            pos = m.index.get_loc(t0)
            amt = m["cum_amt"].diff().fillna(0)
            last5 = amt.iloc[max(0, pos - 4):pos + 1].sum()
            prev5 = amt.iloc[max(0, pos - 9):max(0, pos - 4)].sum()
            res["break_vol_up"] = bool(last5 > prev5)
            hold = m["price"].iloc[pos:pos + 6]
            res["break_hold"] = bool(hold.min() >= pre_high) if len(hold) >= 3 else None
        # §3.4.6 最近10分钟直线拉升排除
        if len(m) > 10:
            res["no_rush"] = bool((cur["price"] / m["price"].iloc[-11] - 1) * 100 <= TH["rush10"])
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", default="1445", help="截止时刻 HHMM，默认1445")
    ap.add_argument("--loose", action="store_true", help="宽松模式：容忍≤1项FAIL（观察名单）")
    args = ap.parse_args()
    cutoff = args.at
    t0 = time.time()
    session = new_session()

    # ── 会话日期与大盘状态 ──────────────────────────────
    idx_min = tencent_minute_today("sh000001", session=session)   # 上证指数
    sess_date = idx_min.attrs["date"]
    now = datetime.now()
    live = (now.strftime("%Y%m%d") == sess_date and "0930" <= now.strftime("%H%M") <= "1500")
    if live:
        cutoff = min(cutoff, now.strftime("%H%M"))
    print(f"会话日期: {sess_date}  截止: {cutoff}  模式: {'实时' if live else '回放(复盘)'}")

    im = idx_min[idx_min["time"] <= cutoff]
    idx_prev = idx_min.attrs.get("prev_close") or im["price"].iloc[0]
    idx_pct = (im["price"].iloc[-1] / idx_prev - 1) * 100
    ih, il = im["price"].max(), im["price"].min()
    idx_pos = 0.5 if ih == il else (im["price"].iloc[-1] - il) / (ih - il)
    # §3.4.2 大盘5分钟K线不能处于明显下降趋势（收盘<5分MA5 且 MA5向下）
    idx5_ok = True
    ib = five_min_bars(idx_min)
    ib = ib[[bar_end_min(b) <= hhmm(cutoff) for b in ib.index]]
    if len(ib) >= 6:
        ima5 = ib["close"].rolling(5).mean()
        idx5_ok = not (ib["close"].iloc[-1] < ima5.iloc[-1] and ima5.iloc[-1] < ima5.iloc[-2])
    mkt_ok = idx_pct >= -1.0 and idx_pos >= 0.2 and idx5_ok
    print(f"大盘: 上证 {idx_pct:+.2f}%  日内位置 {idx_pos:.0%}  5分K趋势{'正常' if idx5_ok else '明显下降'}  "
          f"§3.2/§3.4大盘条件: {'通过' if mkt_ok else '不通过(纪律上不开仓)'}")

    # ── 粗筛（新浪快照）─────────────────────────────────
    print("拉取全市场快照粗筛 ...", flush=True)
    snap = a_share_universe(sina_market_snapshot(session=session))
    snap["cap_yi"] = snap["nmc"] / 1e4
    snap["amt_yi"] = snap["amount"] / 1e8
    c = snap[
        snap["changepercent"].between(*COARSE["pct"])
        & snap["turnoverratio"].between(*COARSE["turnover"])
        & snap["cap_yi"].between(*COARSE["cap"])
        & (snap["amt_yi"] >= COARSE["amount_yi"])
    ].copy()
    print(f"粗筛通过: {len(c)} 只")
    if c.empty:
        print("无信号（粗筛即无候选，正常结果，按§7记录'无信号'）")
        return

    # ── 精筛1：腾讯实时行情（量比等）────────────────────
    qmap = tencent_quote(list(c["code"]), session=session)
    float_shares = dict(zip(c["code"], c["nmc"] * 1e4 / c["trade"]))

    # ── 板块与题材 ──────────────────────────────────────
    board_of, boards = {}, None
    try:
        board_of, boards = industry_map(session=session)
        boards["rank_pct"] = boards["pct"].rank(pct=True, ascending=False) * 100
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 行业板块数据不可用，相关条件记 NA: {e}")
    ths_tags = {}
    try:
        d = f"{sess_date[:4]}-{sess_date[4:6]}-{sess_date[6:]}"
        hot = ths_hot_reason(d, session=session)
        if not hot.empty:
            ths_tags = dict(zip(hot["code"], hot.get("reason", "")))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 同花顺题材数据不可用: {e}")

    # 同板块内今日涨幅≥3%的家数（§3.2.4 近似：同板块≥3只同步走强）
    snap_b = snap.assign(board=snap["code"].map(board_of))
    strong_cnt = snap_b[snap_b["changepercent"] >= 3].groupby("board")["code"].count().to_dict()
    board_pct_map = dict(zip(boards["node"], boards["pct"])) if boards is not None else {}
    board_rank_map = dict(zip(boards["node"], boards["rank_pct"])) if boards is not None else {}
    board_name_map = dict(zip(boards["node"], boards["board_name"])) if boards is not None else {}

    # ── 精筛2：逐股 日K + 分时 检查 ─────────────────────
    # 上一交易日（用于判断本地日线缓存是否新鲜）
    prev_td = None
    idx_fp = DATA_DIR / "index_shcomp.csv"
    if idx_fp.exists():
        idx_days = pd.read_csv(idx_fp, dtype={"date": str})["date"]
        sd = f"{sess_date[:4]}-{sess_date[4:6]}-{sess_date[6:]}"
        older = idx_days[idx_days < sd]
        prev_td = older.iloc[-1] if len(older) else None

    def load_kline(code):
        fp = DATA_DIR / "daily" / f"{code}.csv"
        if fp.exists():
            kc = pd.read_csv(fp, dtype={"date": str})
            if len(kc) and (prev_td is None or str(kc["date"].iloc[-1]) >= prev_td):
                return kc[["date", "open", "close", "high", "low", "vol"]], True
        k = tencent_day_kline(code_prefix(code) + code, count=140, qfq=True, session=session)
        return k, False

    rows = []
    n_err = 0
    for _, r in c.iterrows():
        code, name = r["code"], r["name"]
        q = qmap.get(code, {})
        try:
            k, from_cache = load_kline(code)
            if not from_cache:
                time.sleep(0.15 + random.random() * 0.1)
            mdf = tencent_minute_today(code, session=session)
            if mdf.attrs["date"] != sess_date:
                continue
            time.sleep(0.06 + random.random() * 0.06)
        except Exception as e:  # noqa: BLE001
            n_err += 1
            if n_err <= 3:
                print(f"  [warn] {code} 数据获取失败: {str(e)[:80]}")
            continue
        # 历史截到会话日前一天
        hist = k[k["date"] < f"{sess_date[:4]}-{sess_date[4:6]}-{sess_date[6:]}"]
        if len(hist) < 130:
            continue
        prev_close = q.get("last_close") or mdf.attrs.get("prev_close") or hist["close"].iloc[-1]
        mm = minute_metrics(mdf, cutoff, prev_close)
        if not mm:
            continue
        fm = five_min_checks(mdf, cutoff)

        # 含今日临时价的均线
        cl = pd.concat([hist["close"], pd.Series([mm["price"]])], ignore_index=True)
        ma = {n: cl.rolling(n).mean() for n in (5, 10, 20, 60)}
        ma_up3 = all(ma[n].iloc[-1] > ma[n].iloc[-2] > ma[n].iloc[-3] > ma[n].iloc[-4] for n in (5, 10, 20))
        pct3 = (mm["price"] / hist["close"].iloc[-3] - 1) * 100
        vr = q.get("vol_ratio") or np.nan
        turn = mm["amount_yi"] * 1e8 / mm["price"] / float_shares.get(code, np.inf) * 100
        cap = float_shares.get(code, 0) * mm["price"] / 1e8
        bd = board_of.get(code)

        checks = {
            "涨幅2.5-5.5": TH["pct"][0] <= mm["pct"] <= TH["pct"][1],
            "量比1.2-2.5": (TH["vol_ratio"][0] <= vr <= TH["vol_ratio"][1]) if vr and vr > 0 else None,
            "换手3-8": TH["turnover"][0] <= turn <= TH["turnover"][1],
            "市值100-500亿": TH["cap_yi"][0] <= cap <= TH["cap_yi"][1],
            "成交额≥8亿": mm["amount_yi"] >= TH["amount_yi"],
            "价>四均线": all(mm["price"] > ma[n].iloc[-1] for n in (5, 10, 20, 60)),
            "均线多头": ma[5].iloc[-1] > ma[10].iloc[-1] > ma[20].iloc[-1] > ma[60].iloc[-1],
            "均线三日向上": ma_up3,
            "距MA5≤5%": (mm["price"] / ma[5].iloc[-1] - 1) * 100 <= TH["dist_ma5"],
            "距MA20≤12%": (mm["price"] / ma[20].iloc[-1] - 1) * 100 <= TH["dist_ma20"],
            "3日累计≤15%": pct3 <= TH["pct3"],
            "午后80%均价上": (mm.get("above_avg_ratio") or 0) >= TH["afternoon_above_avg"],
            "均价线向上": mm.get("avg_rising"),
            "尾盘量能递增": mm.get("vol_shift_up"),
            "无放量滞涨": mm.get("vol_no_stall"),
            "破位5分钟收回": mm.get("reclaim_5min"),
            "14:30后0.5-1.5%": (TH["win_1430_1450"][0] <= mm.get("win_pct", -9) <= TH["win_1430_1450"][1]) if "win_pct" in mm else None,
            "突破当日新高": mm.get("break_high"),
            "突破放量": mm.get("break_vol_up"),
            "突破站稳5分": mm.get("break_hold"),
            "位置上部15%": mm["clv"] >= TH["clv"],
            "无尾盘偷袭": mm.get("no_rush"),
            "5分K上升趋势": fm.get("trend5"),
            "无单根急拉失续": fm.get("no_spike_fail"),
            "板块前20%": (board_rank_map.get(bd, 999) <= TH["board_top_pct"]) if bd and board_rank_map else None,
            "同板块3只走强": (strong_cnt.get(bd, 0) >= 3) if bd else None,
            "跑赢板块1pct": (mm["pct"] - board_pct_map.get(bd, 0) >= TH["beat_board"]) if bd and board_pct_map else None,
        }
        # numpy.bool_ 不能用 `is False` 判断，统一转原生 bool/None
        checks = {k_: (None if v is None or (isinstance(v, float) and pd.isna(v)) else bool(v))
                  for k_, v in checks.items()}
        n_fail = sum(1 for v in checks.values() if v is False)
        n_na = sum(1 for v in checks.values() if v is None)
        if True:   # 全量收集，输出时再分层
            rows.append({
                "代码": code, "名称": name, "现价": round(mm["price"], 2),
                "涨幅%": round(mm["pct"], 2), "量比": vr, "换手%": round(turn, 2),
                "流通市值亿": round(cap, 1), "成交额亿": round(mm["amount_yi"], 1),
                "FAIL数": n_fail, "NA数": n_na,
                "5分K": {True: "是", False: "否", None: "NA"}[checks.get("5分K上升趋势")],
                "未过项": ";".join(k_ for k_, v in checks.items() if v is False),
                "NA项": ";".join(k_ for k_, v in checks.items() if v is None),
                "板块": board_name_map.get(bd, ""), "题材": str(ths_tags.get(code, ""))[:40],
            })

    out = pd.DataFrame(rows).sort_values(["FAIL数", "涨幅%"], ascending=[True, False]) if rows else pd.DataFrame()
    passed = out[out["FAIL数"] == 0] if len(out) else out

    print(f"\n评估: {len(out)} 只（粗筛 {len(c)} 只，其余为历史不足/数据缺失被跳过，获取失败 {n_err}）")
    if len(out):
        print(f"FAIL 数分布: {out['FAIL数'].value_counts().sort_index().to_dict()}")
    print(f"\n===== 候选（全部机械条件通过，NA 项需人工确认公告/龙头面）=====")
    print(passed.to_string(index=False) if len(passed) else "无信号（正常结果，不降低条件）")
    near = out[out["FAIL数"].between(1, 3 if args.loose else 2)] if len(out) else out
    if len(near):
        print(f"\n----- 观察名单（差1-{3 if args.loose else 2}项，仅参考，不属于信号）-----")
        cols_show = [x for x in near.columns if x != "NA项"]
        print(near.head(10)[cols_show].to_string(index=False))
    if not mkt_ok:
        print("\n⚠️ §3.2 大盘条件未通过：按纪律今日不开仓（候选仅记录）。")

    # ── 落表（§7 模板）──────────────────────────────────
    rec_fp = RECORDS_DIR / f"signals_{sess_date}.csv"
    cols = ["日期", "策略版本", "代码", "名称", "5分钟趋势通过", "尾盘价", "次日开盘", "9:45", "10:00",
            "固定规则收益", "沪深300同期", "超额收益", "是否高开", "盘后事件", "备注"]
    recs = []
    if len(passed):
        for _, p in passed.iterrows():
            recs.append({"日期": sess_date, "策略版本": "V1", "代码": p["代码"], "名称": p["名称"],
                         "5分钟趋势通过": p["5分K"], "尾盘价": p["现价"],
                         "备注": ("大盘过滤未过,不开仓; " if not mkt_ok else "")
                                + (f"NA:{p['NA项']}" if p["NA项"] else "")})
    else:
        recs.append({"日期": sess_date, "策略版本": "V1", "代码": "", "名称": "无信号",
                     "备注": f"截止{cutoff}无满足全部条件的股票"})
    pd.DataFrame(recs, columns=cols).to_csv(rec_fp, index=False, encoding="utf-8-sig")
    print(f"\n已写入 {rec_fp}  （次日运行 fill_results.py 回填）  用时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
