"""Quick test: check DB cache status for EBK codes"""
import sys, sqlite3
sys.path.insert(0, '.')

conn = sqlite3.connect('data/db/stock.db')
cursor = conn.cursor()
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cursor.fetchall()
print('Tables:', [t[0] for t in tables])

for t in tables:
    tname = t[0]
    cursor.execute(f'SELECT COUNT(*) FROM "{tname}"')
    cnt = cursor.fetchone()[0]
    print(f'  {tname}: {cnt} rows')

# Try to find daily data table
for t in tables:
    tname = t[0]
    if 'daily' in tname.lower() or 'kline' in tname.lower() or 'stock' in tname.lower():
        cursor.execute(f'SELECT COUNT(DISTINCT code) FROM "{tname}"')
        cnt = cursor.fetchone()[0]
        print(f'  {tname} distinct codes: {cnt}')

conn.close()

from src.ebk_analyzer import parse_ebk_file
ebk_codes = set(parse_ebk_file())
print(f'\nEBK codes: {len(ebk_codes)}')
