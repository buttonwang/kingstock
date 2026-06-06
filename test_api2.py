# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding='utf-8')
import requests

headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://data.eastmoney.com/'}
params = {
    'secid': '90.BK0420',
    'fields1': 'f1,f2,f3,f4,f5,f6',
    'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',
    'klt': '101',
    'fqt': '0',
    'beg': '20250501',
    'end': '20250601',
    'lmt': '5',
}

# Test different numbered push2 subdomains
for i in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 16, 18, 20, 22, 24, 40, 50, 60, 70, 80, 82, 84, 86, 88]:
    url = f'http://{i}.push2.eastmoney.com/api/qt/stock/kline/get'
    try:
        r = requests.get(url, params=params, headers=headers, timeout=8)
        data = r.json()
        klines = data.get('data', {}).get('klines', [])
        if klines:
            print(f'{i}.push2: OK! klines={klines[:2]}')
            break
        else:
            print(f'{i}.push2: status={r.status_code}, rc={data.get("rc")}, klines=empty')
    except Exception as e:
        err_type = type(e).__name__
        print(f'{i}.push2: {err_type}')

# Also try quote.eastmoney.com
print("\n--- Testing quote.eastmoney.com ---")
try:
    r = requests.get('http://quote.eastmoney.com/center/api/kline.json',
                    params={'secid': '90.BK0420', 'klt': '101', 'fqt': '0',
                            'beg': '20250501', 'end': '20250601', 'lmt': '5'},
                    headers=headers, timeout=15)
    print(f'status={r.status_code}, text={r.text[:500]}')
except Exception as e:
    print(f'ERROR: {e}')

# Try push2 with ssl=False workaround
print("\n--- Testing HTTPS with different TLS ---")
import urllib3
urllib3.disable_warnings()
try:
    r = requests.get('https://push2his.eastmoney.com/api/qt/stock/kline/get',
                    params=params, headers=headers, timeout=15, verify=False)
    print(f'HTTPS push2his: status={r.status_code}, text={r.text[:500]}')
except Exception as e:
    print(f'HTTPS push2his: {type(e).__name__}: {e}')
