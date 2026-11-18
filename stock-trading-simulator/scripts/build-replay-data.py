"""Normalize captured TDX data, validate all 240 minutes, and emit a browser bundle.

The input is an archived response, not fabricated/filled market data. No network required.
"""
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
raw = json.loads((ROOT / 'data/raw-2026-11-12.json').read_text(encoding='utf-8'))
DATE = raw['date']
expected = [f'{9+(31+i)//60:02d}:{(31+i)%60:02d}' for i in range(120)] + [f'{13+(1+i)//60:02d}:{(1+i)%60:02d}' for i in range(120)]
checks = []

def normalize(code, rows, quote, is_index=False):
    rows = [r for r in rows if r['datetime'].startswith(DATE)]
    assert [r['datetime'][11:16] for r in rows] == expected, f'{code}: incomplete or duplicated timeline'
    factor = min([1, 10, 100, 1000], key=lambda n: abs(rows[-1]['close']/n-quote['close']))
    assert abs(rows[-1]['close']/factor-quote['close']) < .0001, f'{code}: unverified price scale'
    data = []
    for i, r in enumerate(rows):
        p = {k: round(r[k]/factor, 4) for k in ['open','high','low','close']}
        assert p['low'] <= min(p['open'], p['close']) <= max(p['open'], p['close']) <= p['high'], f'{code}: invalid OHLC'
        assert r['vol'] >= 0 and r['amount'] >= 0
        p.update(time=i+1, label=r['datetime'][11:16], volume=r['vol'], amount=r['amount'])
        if is_index:
            p.update(up=r.get('up_count'), down=r.get('down_count'))
        data.append(p)
    for key in ['high','low']:
        observed = (max if key == 'high' else min)(r[key] for r in data)
        assert abs(observed-quote[key]) < .011, f'{code}: {key} mismatches snapshot'
    volume = sum(r['volume'] for r in data)
    amount = sum(r['amount'] for r in data)
    assert abs(amount/quote['amount']-1) < .0001, f'{code}: amount mismatch'
    if not is_index:
        assert abs(volume/quote['vol']-1) < .00001, f'{code}: share units mismatch'
    checks.append({'code':code,'minuteCount':len(data),'priceDivisor':factor,
                   'volumeDelta':None if is_index else volume-quote['vol'],
                   'amountDelta':round(amount-quote['amount'],2),'closeMatches':True})
    return data, factor

out = {'date':DATE,'datasetId':'tdx-20261112-min1-v1','source':raw['source'],
       'retrievedAt':raw['retrievedAt'],'timezone':'Asia/Shanghai','complete':True,
       'assets':{},'indices':{},'notes':[
           '股票及指数原始 OHLC 除以 10；ETF 无需缩放。分钟成交量为股/份，日线成交量为手（乘100）。',
           '分钟累计量与收盘快照最多相差104股/份；保留原始分钟数值，不补造行情。金额存在浮点汇总尾差。',
           '量比使用前5个完整交易日日均每分钟成交量估算；换手按收盘快照推算的当日流通规模估算。',
           '市场温度是沪深指数接口上涨家数占上涨与下跌合计家数的比例，不含平盘和北交所；非预测指标。',
           '只提供2026-11-12回放，不持续更新、不生成次日行情。实际账户撮合为模拟。'
       ]}
for quote in raw['quotes']:
    code = quote['code']; a = raw['assets'][code]; info = a['info'][0]
    assert info['time'].startswith(DATE), f'{code}: stale source date'
    bars, divisor = normalize(code, a['minutes'], quote)
    daily = [r for r in a['daily'] if r['datetime'][:10] < DATE][-5:]
    assert len(daily) == 5
    assert abs(daily[-1]['close']/divisor-quote['pre_close']) < .0001
    average = sum(r['vol']*100 for r in daily)/5
    out['assets'][code] = {'name':info['name'],'prev':quote['pre_close'],'open':quote['open'],
        'close':quote['close'],'high':quote['high'],'low':quote['low'],
        'totalVolume':quote['vol'],'totalAmount':quote['amount'],'turnover':quote['turnover'],
        'floatUnits':quote['vol']/(quote['turnover']/100),
        'average5DayVolume':average,'baselineDates':[r['datetime'][:10] for r in daily],
        'insideVolume':quote['in_vol'],'outsideVolume':quote['out_vol'],
        'snapshotTime':info['time'],'bars':bars}
for quote in raw['indices'][:3]:
    code = quote['code']
    bars, _ = normalize(code, raw['assets'][code]['minutes'], quote, True)
    out['indices'][code] = {'name':{'999999':'上证指数','399001':'深证成指','399006':'创业板指'}[code],
                            'prev':quote['pre_close'],'open':quote['open'],'bars':bars}
out['validation'] = checks
(ROOT/'data/validation-2026-11-12.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
(ROOT/'dist/replay-data.js').write_text('window.REPLAY_DATA = '+json.dumps(out,ensure_ascii=False,separators=(',',':'))+';\n',encoding='utf-8')
print(json.dumps({'date':DATE,'securities':len(out['assets']),'indices':len(out['indices']),'bars':sum(len(a['bars']) for a in out['assets'].values()),'validation':'passed'},ensure_ascii=False))
