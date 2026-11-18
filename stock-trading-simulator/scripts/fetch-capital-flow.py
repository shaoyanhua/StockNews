"""Fetch date-matched Eastmoney daily net flows; never infer gross from net."""
import concurrent.futures
import datetime
import json
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
DATE = '2026-11-12'
CODES = ['300394', '600487', '000636', '300308', '600183', '300475', '600206', '515050', '560780', '588200', '159530']
URL = 'https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get'

def fetch(code):
    market = 1 if code[0] in '56' else 0
    query = f'{URL}?secid={market}.{code}&lmt=3&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56'
    result = {'sourceUrl': query, 'status': 'unavailable', 'main': None, 'super': None}
    raw = None
    for attempt in range(2):
        try:
            response = requests.get(query, timeout=12)
            response.raise_for_status()
            raw = response.json()
            for line in (raw.get('data') or {}).get('klines', []):
                cells = line.split(',')
                if cells[0] != DATE:
                    continue
                main, small, medium, large, super_ = map(float, cells[1:6])
                if abs(main-large-super_) > max(100, abs(main)*.0001):
                    raise ValueError('Main / large / super identity mismatch')
                result.update(status='daily-net-only', date=DATE, asOf='15:00',
                              main={'inflow': None, 'outflow': None, 'net': main},
                              super={'inflow': None, 'outflow': None, 'net': super_})
                return code, result, raw
            result['reason'] = '来源未提供该日期资金记录'
            break
        except Exception as error:
            result['reason'] = type(error).__name__
    return code, result, raw

if __name__ == '__main__':
    old_path = ROOT/'data/capital-flow-2026-11-12.json'
    old = json.loads(old_path.read_text(encoding='utf-8')) if old_path.exists() else {}
    data = {'date': DATE, 'source': '东方财富资金流向历史日线', 'unit': 'CNY',
            'retrievedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'granularity': 'daily', 'assets': {}}
    raw = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for code, record, original in pool.map(fetch, CODES):
            cached = old.get('assets', {}).get(code, {})
            if record['status'] == 'unavailable' and cached.get('date') == DATE and cached.get('status') == 'daily-net-only':
                record = {**cached, 'cacheFallback': True, 'lastFetchError': record.get('reason')}
            data['assets'][code] = record
            raw[code] = original
            print(code, record['status'], flush=True)
    (ROOT/'data/capital-flow-2026-11-12.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    raw_path = ROOT/'data/capital-flow-raw-2026-11-12.json'
    previous_raw = json.loads(raw_path.read_text(encoding='utf-8')) if raw_path.exists() else {}
    raw = {code: value if value is not None else previous_raw.get(code) for code, value in raw.items()}
    raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
    (ROOT/'dist/capital-flow-data.js').write_text('window.CAPITAL_FLOW_DATA = '+json.dumps(data, ensure_ascii=False)+';\n', encoding='utf-8')
