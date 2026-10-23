"""USD high-impact news + BTC reaction filter. PAPER ONLY; no order endpoints.

This is a conservative experimental filter, NOT a proven profitable signal.
It blocks new entries when calendar/market data cannot be verified.
"""
import os
import time
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

import requests

CALENDAR_URL = os.getenv('NEWS_CALENDAR_URL', 'https://n8n.system.forexfactory.com/api/v1/economic-events')
BEFORE_MIN = 30
AFTER_MIN = 30
REACTION_MIN = 180
CACHE_SEC = 900
KEYWORDS = ('cpi', 'fomc', 'interest rate', 'non-farm', 'nonfarm', 'nfp', 'ppi', 'federal funds', 'fed rate', 'powell')
_cache = {'at': 0, 'events': None, 'error': None}
# Extra-event data MUST come from a verified external feed or curated JSON.
# No live ETF/hack feed is bundled. Fail closed until configured and fresh.
EXTRA_EVENTS_URL = os.getenv('EXTRA_EVENTS_URL', '').strip()
EXTRA_EVENTS_FILE = os.getenv('EXTRA_EVENTS_FILE', 'EXTRA_NEWS_EVENTS.json').strip()
EXTRA_MAX_AGE_MIN = 120



def _calendar(now):
    if time.monotonic() - _cache['at'] < CACHE_SEC:
        return _cache['events'], _cache['error']
    _cache.update(at=time.monotonic(), events=None, error='Calendar unavailable')
    try:
        r = requests.get(CALENDAR_URL, timeout=12)
        r.raise_for_status()
        payload = r.json()
        events = payload if isinstance(payload, list) else payload.get('events') if isinstance(payload, dict) else None
        if not isinstance(events, list) or not events:
            raise ValueError('Empty/unsupported economic calendar')
        parsed, coverage = [], []
        for ev in events:
            if not isinstance(ev, dict):
                raise ValueError('Malformed event')
            title = str(ev.get('title') or ev.get('name') or '').strip()
            usd = str(ev.get('currency', '')).upper().strip() == 'USD'
            impact = str(ev.get('impact', '')).lower().strip()
            important = usd and (impact == 'high' or any(k in title.lower() for k in KEYWORDS))
            raw = ev.get('date')
            if not raw:
                if important:
                    raise ValueError('Important USD event has no date')
                continue
            dt = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
            if dt.tzinfo is None:
                raise ValueError('Calendar timestamp missing timezone')
            dt = dt.astimezone(timezone.utc)
            coverage.append(dt.date())
            if important:
                parsed.append({'time': dt, 'title': title,
                               'actual': ev.get('actual'), 'forecast': ev.get('forecast')})
        if not any(abs((d - now.date()).days) <= 1 for d in coverage):
            raise ValueError('Calendar coverage of today unverified')
        _cache.update(events=sorted(parsed, key=lambda x: x['time']), error=None)
    except (requests.RequestException, ValueError, TypeError, OverflowError, AttributeError) as exc:
        _cache.update(events=None, error=f'Calendar unverified: {exc}')
    return _cache['events'], _cache['error']


def _btc_reaction(event_time, now):
    """Closed 5m candles; return LONG/SHORT only on aligned 15m momentum, volume and VWAP."""
    end = int(now.timestamp())
    start = int((event_time - timedelta(hours=4)).timestamp())
    r = requests.get('https://public.coindcx.com/market_data/candlesticks', params={
        'pair': 'B-BTC_USDT', 'pcode': 'f', 'resolution': '5', 'from': start, 'to': end
    }, timeout=25)
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
        raise ValueError('BTC futures candle data invalid')
    bars = {}
    for x in payload['data']:
        t = float(x['time'])
        if t > 1e11:
            t /= 1000
        if t + 300 > now.timestamp():
            continue
        o, h, l, c, v = (float(x[k]) for k in ('open', 'high', 'low', 'close', 'volume'))
        if c <= 0 or v < 0 or h < l:
            continue
        bars[int(t)] = (o, h, l, c, v)
    ordered = sorted(bars.items())
    pre = [v for t, v in ordered if t + 300 <= event_time.timestamp()]
    post = [(t, v) for t, v in ordered if t >= event_time.timestamp()]
    if len(pre) < 12 or len(post) < 6:
        return None, 'BTC reaction data insufficient'
    # Only use the most recent 3 fully closed, consecutive 5m bars (15m).
    recent = ordered[-3:]
    if len(recent) != 3 or any(recent[i+1][0] - recent[i][0] != 300 for i in (0, 1)):
        return None, 'BTC recent 15m candles incomplete'
    if recent[0][0] < event_time.timestamp():
        return None, 'BTC post-news 15m confirmation pending'
    ref = pre[-1][3]
    latest = recent[-1][1][3]
    # 24-bar VWAP proxy from typical price and candle volume (not trade-level VWAP).
    lookback = ordered[-24:]
    vol = sum(b[4] for _, b in lookback)
    if len(lookback) < 24 or vol <= 0:
        return None, 'BTC VWAP unavailable'
    vwap = sum(((b[1] + b[2] + b[3]) / 3) * b[4] for _, b in lookback) / vol
    baseline = sum(b[4] for b in pre[-12:]) / 12
    recent_avg = sum(b[4] for _, b in recent) / 3
    if baseline <= 0 or recent_avg < baseline * 1.2:
        return None, 'BTC volume confirmation missing (needs >=1.2x baseline)'
    # Price must move at least 0.20% from the pre-news close.
    change = (latest / ref - 1) * 100
    if change >= 0.20 and latest > vwap and recent[-1][1][3] > recent[0][1][0]:
        return 'LONG', f'BTC bullish reaction {change:+.2f}%, volume {recent_avg / baseline:.2f}x, above VWAP'
    if change <= -0.20 and latest < vwap and recent[-1][1][3] < recent[0][1][0]:
        return 'SHORT', f'BTC bearish reaction {change:+.2f}%, volume {recent_avg / baseline:.2f}x, below VWAP'
    return None, f'BTC direction not confirmed ({change:+.2f}%, VWAP {vwap:.2f})'


def _parse_utc(value):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Event timestamp must contain timezone')
    return dt.astimezone(timezone.utc)


def _extra_events(now):
    """Strict, timestamped event feed. Empty is valid only with fresh updated_at.

    Format: {updated_at: ISO-UTC, events: [{type: ETF|POWELL|HACK,
    time: ISO-UTC, title: ..., source: https://..., affected_symbols: [...]}]}.
    The publisher is responsible for independently verifying the event.
    """
    try:
        if EXTRA_EVENTS_URL:
            if not EXTRA_EVENTS_URL.startswith('https://'):
                raise ValueError('EXTRA_EVENTS_URL must use HTTPS')
            response = requests.get(EXTRA_EVENTS_URL, timeout=12)
            response.raise_for_status()
            data = response.json()
        else:
            path = Path(EXTRA_EVENTS_FILE)
            if not path.is_file():
                raise ValueError('Extra-events feed not configured; provide EXTRA_EVENTS_URL or a fresh EXTRA_NEWS_EVENTS.json')
            data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('events'), list):
            raise ValueError('Invalid extra-events schema')
        updated = _parse_utc(data['updated_at'])
        age = (now - updated).total_seconds()
        if age < -300 or age > EXTRA_MAX_AGE_MIN * 60:
            raise ValueError('Extra-events feed stale or future-dated')
        events = []
        for item in data['events']:
            if not isinstance(item, dict):
                raise ValueError('Malformed extra event')
            kind = str(item.get('type', '')).strip().upper()
            if kind not in ('ETF', 'POWELL', 'HACK'):
                raise ValueError('Unknown extra event type')
            event_time = _parse_utc(item['time'])
            title = str(item.get('title', '')).strip()
            source = str(item.get('source', '')).strip()
            if not title or not source.startswith('https://'):
                raise ValueError('Event title or verifiable HTTPS source missing')
            affected = item.get('affected_symbols', [])
            if not isinstance(affected, list) or not all(isinstance(x, str) for x in affected):
                raise ValueError('affected_symbols must be a list')
            if kind == 'HACK' and not affected:
                raise ValueError('HACK needs affected_symbols, or ["ALL"] for exchange-wide risk')
            events.append({'type': kind, 'time': event_time, 'title': title,
                           'source': source, 'affected': [x.upper() for x in affected]})
        return events, None
    except (OSError, requests.RequestException, ValueError, KeyError, TypeError,
            json.JSONDecodeError, OverflowError) as exc:
        return None, f'Extra-events data unverified: {exc}'


def _symbol_affected(symbol, affected):
    symbol = str(symbol).upper().replace('B-', '').replace('_', '')
    return ('ALL' in affected or any(
        symbol == s.replace('B-', '').replace('_', '') or
        symbol.startswith(s.replace('B-', '').replace('_', '').replace('USDT', '') + 'USDT')
        for s in affected))


def entry_decision(symbol, side, now=None):
    """Return (blocked, reason). No trade is ever placed by this module."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        return True, 'Naive UTC timestamp: blocked'
    now = now.astimezone(timezone.utc)
    extra_events, extra_error = _extra_events(now)
    if extra_error:
        return True, extra_error
    # Unscheduled hacks are not directional signals: avoid exposed assets for 24h.
    for ev in extra_events:
        if ev['type'] == 'HACK' and timedelta(0) <= now - ev['time'] <= timedelta(hours=24):
            if _symbol_affected(symbol, ev['affected']):
                return True, f'HACK RISK 24h: {ev["title"]} | {ev["source"]}'
    events, error = _calendar(now)
    if error:
        return True, error
    # Scheduled ETF / Powell events: same 30m no-entry window and BTC reaction gate.
    for item in extra_events:
        if item['type'] in ('ETF', 'POWELL'):
            events.append({'time': item['time'], 'title': item['title'],
                           'actual': None, 'forecast': None})
    events.sort(key=lambda item: item['time'])
    for ev in events:
        dt = ev['time']
        if dt - timedelta(minutes=BEFORE_MIN) <= now <= dt + timedelta(minutes=AFTER_MIN):
            return True, f'USD NEWS NO-TRADE +/-30m: {ev["title"]} at {dt:%Y-%m-%d %H:%M UTC}'
    recent = [ev for ev in events if timedelta(minutes=AFTER_MIN) < now - ev['time'] <= timedelta(minutes=REACTION_MIN)]
    if not recent:
        return False, 'No active high-impact USD news reaction window; normal strategy entry'
    ev = recent[-1]
    try:
        direction, detail = _btc_reaction(ev['time'], now)
    except (requests.RequestException, ValueError, KeyError, TypeError, OverflowError) as exc:
        return True, f'BTC news reaction unverified: {exc}'
    # Calendar actual/forecast is recorded for analysis, not assumed to predict direction.
    actual = ev.get('actual')
    forecast = ev.get('forecast')
    surprise = f' | actual={actual} forecast={forecast}' if actual is not None and forecast is not None else ' | actual/forecast unavailable'
    if direction is None:
        return True, f'{ev["title"]}: {detail}{surprise}'
    requested = 'LONG' if str(side).upper() in ('LONG', 'BUY') else 'SHORT' if str(side).upper() in ('SHORT', 'SELL') else None
    if requested != direction:
        return True, f'{ev["title"]}: signal {requested} opposite BTC {direction}; {detail}{surprise}'
    return False, f'{ev["title"]}: BTC {direction} confirmed; {detail}{surprise}'
