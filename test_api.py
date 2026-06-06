# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding='utf-8')
import requests

# Test datacenter-web API for board historical data
headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://data.eastmoney.com/'}

# Try different reportName patterns
report_names = [
    'RPT_BOARD_TRADE_DAY',
    'RPT_BOARD_DAILY',
    'RPT_DAILY_BOARD_INDEX',
    'RPT_BOARD_INDEX_DAILY',
    'RPT_SECTOR_DAILY',
    'RPT_INDUSTRY_BOARD_DAILY',
]

for rn in report_names:
    params = {
        'reportName': rn,
        'columns': 'ALL',
        'pageNumber': '1',
        'pageSize': '5',
        'sortColumns': 'TRADE_DATE',
        'sortTypes': '-1',
    }
    try:
        r = requests.get('http://datacenter-web.eastmoney.com/api/data/v1/get',
                        params=params, headers=headers, timeout=10)
        data = r.json()
        print(f'{rn}: success={data.get("success")}, message={data.get("message","")[:60]}')
        if data.get('success'):
            print(f'  result keys: {list(data.get("result",{}).keys())[:5]}')
            break
    except Exception as e:
        print(f'{rn}: ERROR {e}')

# Also test Tencent API for sector kline
print("\n--- Testing Tencent API ---")
try:
    # Tencent stock API
    r = requests.get('http://web.ifzq.gtimg.cn/appstock/app/fqkline/get',
                    params={
                        '_var': 'kline_dayq',
                        'param': 'b_BK0420,day,,,20,qfq',
                    },
                    headers=headers, timeout=15)
    print(f'Tencent: status={r.status_code}, text={r.text[:500]}')
except Exception as e:
    print(f'Tencent: ERROR {e}')
