"""Download CoinDCX BTC futures 5-minute candles around four losing trades.
Read-only historical research. No orders, no Telegram, no scanner changes.
"""
import csv
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

PAIR = 'B-BTC_USDT'
START = datetime(2026, 10, 23, 20, 30, tzinfo=timezone.utc)
END = datetime(2026, 10, 23, 23, 30, tzinfo=timezone.utc)
OUT = Path('BTC_23_OCT_5MIN.csv')

response = requests.get('https://public.coindcx.com/market_data/candlesticks', params={
    'pair': PAIR, 'pcode': 'f', 'resolution': '5',
    'from': int(START.timestamp()), 'to': int(END.timestamp())
}, timeout=30)
response.raise_for_status()
body = response.json()
raw = body.get('data', []) if isinstance(body, dict) else []
rows = []
for bar in raw:
    try:
        stamp = float(bar['time'])
        if stamp > 1e11:
            stamp /= 1000
        dt = datetime.fromtimestamp(stamp, timezone.utc)
        if START <= dt < END:
            rows.append({'time_utc': dt.strftime('%Y-%m-%d %H:%M:%S'),
                         'open': float(bar['open']), 'high': float(bar['high']),
                         'low': float(bar['low']), 'close': float(bar['close']),
                         'volume': float(bar['volume'])})
    except (KeyError, ValueError, TypeError, OverflowError):
        continue
rows.sort(key=lambda x: x['time_utc'])
if not rows:
    raise SystemExit('No BTC historical candles returned. Check CoinDCX API response/date; no results were fabricated.')
with OUT.open('w', newline='', encoding='utf-8') as f:
    writer = csv.DictWriter(f, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
print(f'Saved {len(rows)} BTC 5-minute candles to {OUT}')
for when in ['2026-10-23 21:00:00', '2026-10-23 22:20:00', '2026-10-23 22:30:00']:
    matching = [r for r in rows if r['time_utc'] == when]
    if matching:
        r = matching[0]
        print(f"{when} UTC: open={r['open']} high={r['high']} low={r['low']} close={r['close']}")
    else:
        print(f'{when} UTC: candle unavailable')
