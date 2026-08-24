"""Build and execute a compact audit notebook without a Jupyter dependency."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
NOTEBOOK = ROOT / "backtest_outputs" / "中际旭创_做T指标回测.ipynb"


def markdown(source: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": source.splitlines(keepends=True)}


def code(source: str) -> dict:
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": source.splitlines(keepends=True),
    }


cells = [
    markdown(
        "# 中际旭创（300308）做T横线指标分钟级回测\n\n"
        "本笔记使用本地逐笔成交重建分钟线。信号在分钟收盘后确认，卖出按下一分钟第一笔成交价，"
        "以避免同一分钟偷看未来。每个交易日只执行第一次确认信号。"
    ),
    code(
        "from pathlib import Path\n"
        "import pandas as pd\n\n"
        "root = Path.cwd()\n"
        "out = root / 'backtest_outputs'\n"
        "summary = pd.read_csv(out / 'summary.csv')\n"
        "trades = pd.read_csv(out / 'trades.csv')\n"
        "qa = pd.read_csv(out / 'data_quality.csv')\n"
        "sweep = pd.read_csv(out / 'threshold_sweep.csv')\n"
        "print(f'样本：{len(qa)}个交易日，{qa.date.min()} 至 {qa.date.max()}')\n"
        "print(f'日线收盘与逐笔末价差异>0.1%的天数：{(qa.close_abs_diff_pct > .001).sum()}')\n"
    ),
    code(
        "cols = ['variant','period','signal_days','same_day_buyback_rate','by_next_day_buyback_rate','fly_rate']\n"
        "view = summary[cols].copy()\n"
        "for c in cols[3:]: view[c] = (view[c] * 100).round(1).astype(str) + '%'\n"
        "print(view.to_string(index=False))\n"
    ),
    markdown(
        "## 交易定义\n\n"
        "接回成功：卖出后至下一交易日最低成交价至少低于卖价0.8%。"
        "抛飞：期间没有0.8%接回机会，且价格又上涨至少2%。0.8%为毛差价，未扣个别券商佣金。"
    ),
    code(
        "fixed = trades[trades.variant.eq('fixed')].copy()\n"
        "minute_no = fixed.signal_time.str[:2].astype(int)*60 + fixed.signal_time.str[3:5].astype(int)\n"
        "fixed['time_bucket'] = pd.cut(minute_no,[569,600,690,779,840,901],labels=['09:30-10:00','10:01-11:30','午间','13:00-14:00','14:01-15:00'])\n"
        "by_time = fixed.groupby('time_bucket', observed=True).agg(signals=('date','size'),same_day=('buyback_same_0_8','mean'),by_next=('buyback_by_next_0_8','mean'),fly=('fly_2pct_without_buyback','mean'))\n"
        "print((by_time.assign(same_day=lambda x:(x.same_day*100).round(1),by_next=lambda x:(x.by_next*100).round(1),fly=lambda x:(x.fly*100).round(1))).to_string())\n"
    ),
    code(
        "recent = sweep[sweep.period.eq('recent_30pct')][['lower_pct','signal_days','same_day_buyback_rate','by_next_day_buyback_rate','fly_rate']].copy()\n"
        "recent['lower_pct'] = (recent.lower_pct*100).round(1)\n"
        "for c in ['same_day_buyback_rate','by_next_day_buyback_rate','fly_rate']: recent[c] = (recent[c]*100).round(1)\n"
        "print(recent.to_string(index=False))\n"
    ),
    markdown(
        "## 结论\n\n"
        "固定1.8%下沿的全样本次日内0.8%接回率为80.0%，最近30%样本降至76.4%，"
        "不能称为高确定性。分时均价动态下沿没有稳定提高结果。信号集中在10:00前时表现更好，"
        "下午尤其14:00后的样本少且抛飞明显更高。大盘/通信板块历史分钟数据不在本地样本中，"
        "因此没有把收盘后数据冒充盘中条件。"
    ),
]


namespace: dict = {"__name__": "__notebook__"}
execution_count = 0
for cell in cells:
    if cell["cell_type"] != "code":
        continue
    execution_count += 1
    stream = io.StringIO()
    try:
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            exec(compile("".join(cell["source"]), str(NOTEBOOK), "exec"), namespace)
        cell["execution_count"] = execution_count
        output = stream.getvalue()
        if output:
            cell["outputs"] = [{"name": "stdout", "output_type": "stream", "text": output.splitlines(keepends=True)}]
    except Exception as exc:
        cell["execution_count"] = execution_count
        cell["outputs"] = [
            {
                "ename": type(exc).__name__,
                "evalue": str(exc),
                "output_type": "error",
                "traceback": [f"{type(exc).__name__}: {exc}"],
            }
        ]
        raise

notebook = {
    "cells": cells,
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": "3.14"},
    },
    "nbformat": 4,
    "nbformat_minor": 5,
}
NOTEBOOK.write_text(json.dumps(notebook, ensure_ascii=False, indent=1), encoding="utf-8")
print(NOTEBOOK)
