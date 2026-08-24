# Source notes and validation record

## Scope

- Question: explain the continuous-limit-up behavior of 蓝盾光电 (300862) and 百花医药 (600721), then identify recent comparable stocks by theme, catalyst, and speculative structure.
- Audience: A-share short-term market participant.
- Snapshot cutoff: 2026-08-14 close, Asia/Shanghai.
- This is a market-structure study, not a buy list or return forecast.

## Controlling sources

1. Eastmoney public daily-kline API, reviewed on 2026-08-14 after close:
   - 蓝盾光电: https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=0.300862&klt=101&fqt=1&lmt=60&end=20260814&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61
   - 百花医药: https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600721&klt=101&fqt=1&lmt=80&end=20260814&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61
2. Eastmoney 2026-08-14 limit-up pool API, reviewed after the 16:30 snapshot:
   - https://push2ex.eastmoney.com/getTopicZTPool?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0&pagesize=300&sort=fbt:asc&date=20260814
3. 蓝盾光电重组预案摘要:
   - https://pdf.dfcfw.com/pdf/H2_AN202608071827760130_1.pdf?1786135670000.pdf
4. 蓝盾光电严重异常波动暨风险提示公告:
   - https://pdf.dfcfw.com/pdf/H2_AN202608131827943091_1.pdf?1786643762000.pdf
5. 百花医药严重异常波动暨风险提示公告:
   - https://pdf.dfcfw.com/pdf/H2_AN202608121827910164_1.pdf?1786561622000.pdf
6. 百花医药8月12日风险提示公告:
   - https://pdf.dfcfw.com/pdf/H2_AN202608111827862600_1.pdf?1786472708000.pdf
7. 2026-08-14 concept/limit-up snapshot (secondary market classification):
   - https://stocks.matraceai.com/concepts/?date=2026-08-14
8. Recent restructuring-comparable overview (secondary source):
   - https://finance.sina.com.cn/jjxw/2026-07-25/doc-iniizerf4358180.shtml
   - https://finance.sina.com.cn/jjxw/2026-07-11/doc-inihmrez5038697.shtml

## Verified calculations

- 蓝盾光电 cumulative rise from the pre-suspension close on 2026-07-24 (19.01) to 2026-08-14 (47.29): 47.29 / 19.01 - 1 = 148.76%.
- Its first four post-resumption closes were all limit-up closes; the fifth day (2026-08-14) still closed at the 20% limit but had a 14.79% intraday range, 51.69% turnover, CNY 3.495bn turnover value, and five board breaks in the limit-up-pool snapshot.
- 百花医药 cumulative rise from 2026-08-03 close (7.20) to the seventh consecutive limit-up close on 2026-08-12 (14.03): 14.03 / 7.20 - 1 = 94.86%.
- 百花医药 then rose 3.35% on 2026-08-13 and fell 4.76% on 2026-08-14, so the continuous-limit-up phase had already ended.
- Similarity scores in the report are a transparent qualitative heuristic (theme overlap, catalyst type, float/price elasticity, and current board position). They are not probability estimates.

## Validation assessment

- Overall: Share with caveats.
- High-confidence facts: prices, dates, board counts, turnover, disclosed transaction structure, company risk warnings, and company statements about business scope.
- Medium-confidence interpretation: which market narrative attracted speculative capital and whether a stock is a leader, follow-on, or shadow stock.
- Unverified/forward-looking: final transaction value, audited target financials, valuation, performance commitments, approvals, and the persistence of sector momentum.
- No causal claim is made that a single headline alone caused the price moves; the report separates disclosed facts from market inference.

## Report design notes

- Chart map: 当前可比股；question: 哪些标的在题材、催化与筹码结构上最接近；family/type: Comparison & Ranking / ranked bar；fields: `stock`, `similarity_score`；takeaway: 博济医药、澳洋健康与华西股份处于当前可比样本前列；palette: single-root preferred；delivery: canonical artifact chart backed by `active_analogs`.
- The score is explicitly heuristic and is not a forecast probability. Exact catalyst, boundary and risk text remains in the adjacent table because heterogeneous event details should not be compressed into the chart.
- Required executive structure mapping: title; Executive Summary; two-stock findings; current analogues; recent mechanism cases; next steps; further questions; caveats.
