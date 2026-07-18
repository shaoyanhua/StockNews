# StockNews
## 1. 项目概述
抓取影响股市或板块走势的最新新闻讯息，分析并给出利好或利空，以及警报，显示在网页上。

## 2. 核心业务描述
网页可以手动刷新按钮，点击会让python后端抓取最新股市财经要闻，从这些网站:
1.财联社:https://www.cls.cn/
2.Finscope:https://finance.baidu.com/
3.同花顺财经:https://www.10jqka.com.cn/
4.东方财富网:https://www.eastmoney.com/
5.巨潮资讯网:https://www.cninfo.com.cn/new/index 。
*资金流向涨跌榜单：https://data.10jqka.com.cn/funds/ggzjl/

新闻类型:
1.全球大盘要闻(Top 5)
2.AI科技要闻(Top 10)，影响AI相关股票的：人工智能etf、算力芯片etf、半导体etf，科创etf，光通信etf。
3.美股纳斯达克etf要闻(Top 5)
4.机器人etf要闻(Top 1)
5.新能源etf要闻(Top 1)

重点关键词：有新催化、有题材、中线逻辑硬、护城河深、垄断、业绩有兑现、各种重大利好.

然后打包成上下文提示词发送给AI(API)，让AI来整理，判断利好还是利空。

最后将最终整理好的板块新闻以及利好利空都显示在网页web上。


预测当天涨跌模型重要数据：
https://claude.ai/code/artifact/c1d6aeb9-ae69-422c-9edf-0be1fe562126
https://claude.ai/code/artifact/20d1d0d0-e625-472b-b06d-a7ec334ccbb4?via=auto_preview



## 3. 注意事项：
- **API Key:**利用.env存储API KEY
- **后端:**Python + FastAPI (提供 RESTful API)
- **数据抓取:** 优先使用 `akshare` 库，若不支持则使用 `requests` 抓取后台 API 接口，尽量避免使用繁重的 Selenium/Playwright。
- **前端:** 纯 HTML + JS (Fetch) + TailwindCSS (CDN引入，保持轻量级，无需 npm 构建)。
- **前端渲染:** 美观易用即可。