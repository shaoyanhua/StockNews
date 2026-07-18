# -*- coding: utf-8 -*-
"""
回测数据下载：全市场筛出候选池（当前流通市值 30–800 亿缓冲带），
逐股下载腾讯日K（前复权 + 不复权各 650 根），存 data/daily/{code}.csv。
断点续传：已有且最新日期为最近交易日的文件跳过。

用法: python download_daily.py
"""
import sys
import time
import random
import threading
import queue
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_sources import (BASE_DIR, DATA_DIR, new_session, sina_market_snapshot,
                          a_share_universe, tencent_day_kline, code_prefix, ThrottledError)

DAILY_DIR = DATA_DIR / "daily"
DAILY_DIR.mkdir(parents=True, exist_ok=True)

CAP_MIN_YI, CAP_MAX_YI = 30, 800   # 候选池流通市值缓冲带（亿）
WORKERS = 2                        # 4 线程曾触发腾讯限流(501)，勿调高
PACE = 0.30                        # 每股间隔基数（秒）


def build_universe() -> pd.DataFrame:
    print("拉取新浪全市场快照 ...", flush=True)
    snap = sina_market_snapshot()
    uni = a_share_universe(snap)
    uni["float_cap_yi"] = uni["nmc"] / 1e4        # 万 → 亿
    uni["float_shares"] = uni["nmc"] * 1e4 / uni["trade"]   # 股
    uni = uni[(uni["float_cap_yi"] >= CAP_MIN_YI) & (uni["float_cap_yi"] <= CAP_MAX_YI)]
    uni = uni[["code", "name", "trade", "float_cap_yi", "float_shares", "turnoverratio"]]
    uni.to_csv(DATA_DIR / "universe.csv", index=False, encoding="utf-8-sig")
    print(f"候选池: {len(uni)} 只（流通市值 {CAP_MIN_YI}–{CAP_MAX_YI} 亿，剔除ST/北交所）", flush=True)
    return uni


def latest_trade_date(session) -> str:
    idx = tencent_day_kline("sh000001", count=5, qfq=False, session=session)
    return str(idx["date"].iloc[-1])


def fetch_one(code: str, session) -> pd.DataFrame:
    sym = code_prefix(code) + code
    qfq = tencent_day_kline(sym, count=650, qfq=True, session=session)
    time.sleep(0.15 + random.random() * 0.15)
    raw = tencent_day_kline(sym, count=650, qfq=False, session=session)
    df = qfq.merge(raw[["date", "open", "close", "high", "low"]],
                   on="date", suffixes=("", "_raw"))
    return df


def worker(q: queue.Queue, latest: str, stats: dict, lock: threading.Lock):
    session = new_session()
    while True:
        try:
            code = q.get_nowait()
        except queue.Empty:
            return
        out = DAILY_DIR / f"{code}.csv"
        try:
            if out.exists():
                try:
                    old = pd.read_csv(out, dtype={"date": str})
                    if len(old) and str(old["date"].iloc[-1]) == latest:
                        with lock:
                            stats["skip"] += 1
                        continue
                except Exception:
                    pass
            df = None
            for attempt in range(4):
                try:
                    df = fetch_one(code, session)
                    break
                except ThrottledError:
                    with lock:
                        stats["throttle"] += 1
                    print(f"  [throttle] {code} 第{attempt+1}次，冷却90s ...", flush=True)
                    time.sleep(90 + random.random() * 20)
            if df is None:
                with lock:
                    stats["fail"] += 1
                    stats["fails"].append((code, "throttled x4"))
                continue
            if len(df) < 130:   # 新股（不足130个交易日）不参与
                with lock:
                    stats["short"] += 1
                continue
            df.to_csv(out, index=False, encoding="utf-8")
            with lock:
                stats["ok"] += 1
        except Exception as e:  # noqa: BLE001
            with lock:
                stats["fail"] += 1
                stats["fails"].append((code, str(e)[:60]))
        finally:
            done = stats["ok"] + stats["skip"] + stats["fail"] + stats["short"]
            if done % 200 == 0:
                print(f"  进度 {done}/{stats['total']}  ok={stats['ok']} skip={stats['skip']} "
                      f"short={stats['short']} fail={stats['fail']} throttle={stats['throttle']}", flush=True)
            time.sleep(PACE + random.random() * 0.25)


def main():
    t0 = time.time()
    uni = build_universe()
    session = new_session()
    latest = latest_trade_date(session)
    print(f"最近交易日: {latest}", flush=True)

    # 指数
    for sym, name in [("sh000300", "hs300"), ("sh000001", "shcomp")]:
        idx = tencent_day_kline(sym, count=650, qfq=False, session=session)
        idx.to_csv(DATA_DIR / f"index_{name}.csv", index=False, encoding="utf-8")
        print(f"指数 {name}: {len(idx)} 根", flush=True)

    q = queue.Queue()
    for c in uni["code"]:
        q.put(str(c).zfill(6))
    stats = {"ok": 0, "skip": 0, "fail": 0, "short": 0, "throttle": 0, "total": len(uni), "fails": []}
    lock = threading.Lock()
    threads = [threading.Thread(target=worker, args=(q, latest, stats, lock), daemon=True)
               for _ in range(WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print(f"\n完成: ok={stats['ok']} skip={stats['skip']} short={stats['short']} "
          f"fail={stats['fail']}  用时 {(time.time()-t0)/60:.1f} 分钟", flush=True)
    if stats["fails"]:
        print("失败样例:", stats["fails"][:10], flush=True)


if __name__ == "__main__":
    main()
