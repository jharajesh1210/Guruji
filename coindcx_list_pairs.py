#!/usr/bin/env python3
"""One-off helper: list EVERY active CoinDCX USDT futures instrument (no price/volume filter) with last price
and estimated 24h turnover, and flag index-like names. Read-only public data; no orders, no API keys."""
import re
import requests

ACTIVE = "https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments?margin_currency_short_name[]=USDT"
PRICES = "https://public.coindcx.com/market_data/v3/current_prices/futures/rt"

pairs = requests.get(ACTIVE, timeout=25).json()
payload = requests.get(PRICES, timeout=25).json()
prices = payload.get("prices", {}) if isinstance(payload, dict) else {}
rows = []
for p in pairs:
    q = prices.get(p) if isinstance(prices.get(p), dict) else {}
    try:
        last = float(q.get("ls", 0)); vol = last * float(q.get("v", 0))
    except (TypeError, ValueError):
        last = vol = 0.0
    rows.append((p, last, vol))
rows.sort(key=lambda r: r[0])
print(f"{len(rows)} active USDT futures instruments")
for p, last, vol in rows:
    print(f"{p:28s} last={last:<14.6g} turnover24h_usdt={vol:,.0f}")
idx = [r for r in rows if re.search(r"SPX|SP500|SPY|US500|NAS|NDX|DJI|DOW|GOLD|INDEX|^B-ES_|^B-NQ_|VIX|DXY", r[0], re.I)]
print("\nINDEX-LIKE / TRADFI CANDIDATES:", [r[0] for r in idx])
