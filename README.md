# StockNews
主要功能：抓取影响股市或板块走势的最新新闻讯息，分析并给出利好或利空，以及警报，显示在网页上。

网页可以手动刷新按钮，点击会让python后端抓取最新股市财经要闻，从这些网站:
1.财联社:https://www.cls.cn/
2.财联社电报:https://www.cls.cn/telegraph
3.同花顺财经:https://www.10jqka.com.cn/
4.东方财富网:https://www.eastmoney.com/
5.巨潮资讯网:https://www.cninfo.com.cn/new/index 。

新闻类型:
1.最新5条要闻(影响股市大盘)，
2.最新10条AI科技要闻，影响AI股票的、影响人工智能etf的、影响算力芯片etf的、影响半导体etf的，影响科创etf的，影响通信ETF的
3.最新5条美股纳斯达克etf要闻
4.最新1条机器人etf要闻
5.最新2条新能源etf要闻



然后打包成上下文提示词发送给AI(API)，让AI来整理，判断利好还是利空。

最后将最终整理好的板块新闻以及利好利空都显示在网页web上。