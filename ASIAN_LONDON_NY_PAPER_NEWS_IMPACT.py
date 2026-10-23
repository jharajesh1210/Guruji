"""Asian range / London sweep / NY continuation, CoinDCX USDT perpetual PAPER scanner.
No live orders. 5m closed bars only. Four symbols. Persist JSON state + CSV legs.
"""
import csv
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
from NEWS_IMPACT_ENGINE import entry_decision as news_impact_entry_decision


# ============================================================
# HIGH IMPACT USD NEWS FILTER - PAPER COMPARISON VERSION
# Fail closed: no NEW paper entries if calendar unavailable.
# Existing positions and their exits continue normally.
# The URL is an UNOFFICIAL feed; validate before live use.
# ============================================================
NEWS_CALENDAR_URL = os.getenv(
    "NEWS_CALENDAR_URL",
    "https://n8n.system.forexfactory.com/api/v1/economic-events",
)
NEWS_BUFFER_MINUTES = 30
NEWS_CACHE_SECONDS = 3600
NEWS_KEYWORDS = (
    "cpi", "fomc", "interest rate", "non-farm", "nonfarm",
    "nfp", "ppi", "federal funds", "fed rate", "powell",
)
_news_cache = {"at": None, "events": None, "error": None}


def _news_calendar_events():
    now = datetime.now(timezone.utc)
    last = _news_cache["at"]
    if last is not None and (now - last).total_seconds() < NEWS_CACHE_SECONDS:
        return _news_cache["events"], _news_cache["error"]

    _news_cache.update(at=now, events=None, error="Calendar unavailable")
    try:
        response = requests.get(NEWS_CALENDAR_URL, timeout=10)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            events = payload
        elif isinstance(payload, dict):
            events = payload.get("events")
        else:
            events = None
        if not isinstance(events, list) or not events:
            raise ValueError("Empty or unsupported calendar response")
        parsed = []
        coverage_dates = []
        for event in events:
            if not isinstance(event, dict):
                raise ValueError("Invalid calendar event")
            currency = str(event.get("currency", "")).strip().upper()
            impact = str(event.get("impact", "")).strip().lower()
            title = str(event.get("title") or event.get("name") or "").strip()
            important = currency == "USD" and (
                impact == "high" or any(k in title.lower() for k in NEWS_KEYWORDS)
            )
            raw = event.get("date")
            if not raw:
                if important:
                    raise ValueError("High-impact USD event missing date")
                continue
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("News event has no UTC offset")
            dt = dt.astimezone(timezone.utc)
            coverage_dates.append(dt.date())
            if important:
                parsed.append((dt, title))
        # Calendar needs dated events near today, even if no USD news today.
        if not any(abs((d - now.date()).days) <= 1 for d in coverage_dates):
            raise ValueError("Calendar date coverage unverified")
        _news_cache.update(events=parsed, error=None)
    except (requests.RequestException, ValueError, TypeError, OverflowError) as exc:
        _news_cache.update(events=None, error=f"Calendar error: {exc}")
    return _news_cache["events"], _news_cache["error"]


def news_entry_blocked(buffer_minutes=NEWS_BUFFER_MINUTES):
    events, error = _news_calendar_events()
    if error:
        return True, "NEWS DATA UNVERIFIED - " + error
    now = datetime.now(timezone.utc)
    window = timedelta(minutes=buffer_minutes)
    for event_time, title in events:
        if event_time - window <= now <= event_time + window:
            return True, f"HIGH IMPACT USD NEWS: {title} at {event_time:%Y-%m-%d %H:%M UTC}"
    return False, "NEWS FILTER PASSED"


def record_news_skip(scanner, symbol, side, reason):
    import csv as _csv
    path = Path(scanner + "_news_skips.csv")
    row = {
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "scanner": scanner, "symbol": symbol, "side": side,
        "reason": reason,
    }
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as f:
        writer = _csv.DictWriter(f, fieldnames=list(row))
        if new:
            writer.writeheader()
        writer.writerow(row)
    return f"TRADE SKIPPED - HIGH IMPACT NEWS\nScanner: {scanner}\nPair: {symbol}\nSide: {side}\nReason: {reason}\nPAPER ONLY"


PREFIX = 'ASIAN_LONDON_NY_PAPER_NEWS_IMPACT'
STATE = Path(PREFIX + '_state.json')
TRADES = Path(PREFIX + '_trades.csv')
SIGNALS = Path(PREFIX + '_signals.csv')
SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT')
API = 'https://public.coindcx.com/market_data/candlesticks'
MARGIN = 100.0
LEVERAGE = 30.0
FEE_RATE = 0.0005  # illustrative taker fee each side, adjust to account
MIN_RISK_PCT = 0.001
MAX_RISK_PCT = 0.025
MIN_RR = 1.2
MAX_ASIA_RANGE_PCT = 0.07
LOG = logging.getLogger('amd_paper')


def utc(ts):
    return datetime.fromtimestamp(ts / 1000, timezone.utc)


def iso(ts):
    return utc(ts).isoformat(timespec='minutes')


def append_csv(path, row):
    exists = path.exists() and path.stat().st_size > 0
    with path.open('a', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if not exists:
            w.writeheader()
        w.writerow(row)


def load_state():
    if STATE.exists():
        with STATE.open(encoding='utf-8') as f:
            s = json.load(f)
        s.setdefault('positions', [])
        s.setdefault('entered_days', {})
        s.setdefault('last_closed_bar', {})
        return s
    return {'positions': [], 'entered_days': {}, 'last_closed_bar': {}}


def save_state(state):
    tmp = STATE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(state, indent=2), encoding='utf-8')
    tmp.replace(STATE)


def notify(message):
    token, chat = os.getenv('BOT_TOKEN'), os.getenv('CHAT_ID')
    if not token or not chat:
        LOG.info('Telegram not configured: %s', message)
        return
    try:
        r = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                          json={'chat_id': chat, 'text': message}, timeout=12)
        r.raise_for_status()
    except requests.RequestException as exc:
        LOG.warning('Telegram failed: %s', exc)


def candles(symbol):
    """CoinDCX USDT perpetual 5m candles, sorted ascending; closed bars only."""
    pair = f'B-{symbol[:-4]}_USDT'
    end_s = int(time.time())
    start_s = end_s - 4 * 24 * 60 * 60
    response = requests.get(API, params={
        'pair': pair, 'from': start_s, 'to': end_s,
        'resolution': '5', 'pcode': 'f'
    }, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get('s') != 'ok':
        raise ValueError(f'CoinDCX bad response for {pair}: {str(payload)[:250]}')
    raw = payload.get('data')
    if not isinstance(raw, list) or not raw:
        raise ValueError(f'CoinDCX returned no candles for {pair}')
    now_ms = int(time.time() * 1000)
    unique = {}
    for x in raw:
        t = int(x['time'])
        if t + 5 * 60 * 1000 <= now_ms - 1500:
            unique[t] = {'t': t, 'o': float(x['open']),
                         'h': float(x['high']), 'l': float(x['low']),
                         'c': float(x['close']), 'v': float(x['volume'])}
    result = [unique[t] for t in sorted(unique)]
    if not result:
        raise ValueError(f'CoinDCX returned no completed candles for {pair}')
    LOG.info('%s CoinDCX futures candles=%s latest=%s', symbol, len(result), iso(result[-1]['t']))
    return result


def approximate_poc(bars, bins=24):
    low, high = min(x['l'] for x in bars), max(x['h'] for x in bars)
    if high <= low:
        return low
    volume = [0.0] * bins
    for b in bars:
        # Uniform volume allocation over candle high-low is only an approximation.
        start = max(0, min(bins - 1, int((b['l'] - low) / (high - low) * bins)))
        end = max(0, min(bins - 1, int((b['h'] - low) / (high - low) * bins)))
        per_bin = b['v'] / (end - start + 1)
        for j in range(start, end + 1):
            volume[j] += per_bin
    return low + (volume.index(max(volume)) + 0.5) * (high - low) / bins


def find_setup(bars, last_ts):
    """Find one current-day completed retest. No future bars in decisions."""
    if not bars:
        return None
    day = utc(bars[-1]['t']).date()
    today = [b for b in bars if utc(b['t']).date() == day]
    asia = [b for b in today if 0 <= utc(b['t']).hour < 8]
    # Strictly wait for all 96 Asian candles, not a partial range.
    if len(asia) != 96:
        return None
    ah, al = max(b['h'] for b in asia), min(b['l'] for b in asia)
    if al <= 0 or (ah - al) / al > MAX_ASIA_RANGE_PCT:
        return None
    poc = approximate_poc(asia)
    london = [b for b in today if 8 <= utc(b['t']).hour < 10]
    if not london:
        return None
    # Scan latest qualified sweep first. A 1-3 bar wick reversal is preferred;
    # require close back inside Asian range within 3 candles.
    for i, b in enumerate(london):
        if b['l'] < al:
            direction, extreme, boundary = 'BUY', b['l'], al
            returned = next((j for j in range(i, min(i + 3, len(london)))
                             if london[j]['c'] > boundary), None)
        elif b['h'] > ah:
            direction, extreme, boundary = 'SELL', b['h'], ah
            returned = next((j for j in range(i, min(i + 3, len(london)))
                             if london[j]['c'] < boundary), None)
        else:
            continue
        if returned is None:
            continue
        rt = london[returned]['t']
        # A CHOCH proxy: close breaks the 3 pre-sweep candle swing in reversal direction.
        prior = [x for x in today if x['t'] < b['t']][-3:]
        if len(prior) < 3:
            continue
        swing = max(x['h'] for x in prior) if direction == 'BUY' else min(x['l'] for x in prior)
        post = [x for x in today if x['t'] > rt and 8 <= utc(x['t']).hour < 18]
        choch = next((x for x in post if (x['c'] > swing if direction == 'BUY'
                                         else x['c'] < swing)), None)
        if choch is None:
            continue
        # FVG created after CHOCH: three completed candles, with displacement.
        for k in range(2, len(today)):
            c1, c2, c3 = today[k-2:k+1]
            if c3['t'] < choch['t'] or c1['t'] < rt:
                continue
            if not 8 <= utc(c3['t']).hour < 18:
                continue
            if direction == 'BUY':
                lo, hi = c1['h'], c3['l']
                displacement = c2['c'] > c2['o'] and (c2['c'] - c2['o']) > 0.35 * (c2['h'] - c2['l'])
            else:
                lo, hi = c3['h'], c1['l']
                displacement = c2['c'] < c2['o'] and (c2['o'] - c2['c']) > 0.35 * (c2['h'] - c2['l'])
            if hi <= lo or not displacement:
                continue
            # Retest must be a NEW closed candle, after FVG creation.
            for retest in today[k+1:]:
                if retest['t'] <= last_ts or not 8 <= utc(retest['t']).hour < 18:
                    continue
                if retest['l'] > hi or retest['h'] < lo:
                    continue
                entry = retest['c']
                if direction == 'BUY' and (entry <= lo or entry <= extreme):
                    continue
                if direction == 'SELL' and (entry >= hi or entry >= extreme):
                    continue
                stop = min(extreme, retest['l']) if direction == 'BUY' else max(extreme, retest['h'])
                risk = entry - stop if direction == 'BUY' else stop - entry
                if entry <= 0 or not MIN_RISK_PCT <= risk / entry <= MAX_RISK_PCT:
                    continue
                # POC boosts score; not a hard gate.
                poc_confirms = entry > poc if direction == 'BUY' else entry < poc
                return {'day': str(day), 'direction': direction, 'entry': entry,
                        'stop': stop, 'risk': risk, 'poc': poc,
                        'poc_confirms': poc_confirms, 'asian_high': ah,
                        'asian_low': al, 'bar_t': retest['t']}
    return None


def settle(state, symbol, bars):
    remaining = []
    for pos in state['positions']:
        if pos['symbol'] != symbol:
            remaining.append(pos)
            continue
        direction = pos['direction']
        for bar in bars:
            if bar['t'] <= pos['last_checked_t']:
                continue
            # Conservative intrabar: if stop and target both touched, stop wins.
            stop_hit = bar['l'] <= pos['stop'] if direction == 'BUY' else bar['h'] >= pos['stop']
            target_hit = bar['h'] >= pos['target'] if direction == 'BUY' else bar['l'] <= pos['target']
            if stop_hit or target_hit:
                exit_price = pos['stop'] if stop_hit else pos['target']
                gross_pct = (exit_price / pos['entry'] - 1) * 100
                if direction == 'SELL':
                    gross_pct = -gross_pct
                net_pct = gross_pct - (FEE_RATE * (1 + exit_price / pos['entry']) * 100)
                append_csv(TRADES, {
                    'symbol': symbol, 'direction': direction, 'entry_time_utc': pos['entry_time_utc'],
                    'exit_time_utc': iso(bar['t']), 'leg': pos['leg'],
                    'entry_price': pos['entry'], 'exit_price': exit_price,
                    'stop_loss': pos['initial_stop'], 'target': pos['target'],
                    'exit_reason': 'STOP' if stop_hit else 'TARGET',
                    'net_return_pct_per_leg': round(net_pct, 6),
                    'paper_pnl_usdt': round(net_pct * MARGIN * LEVERAGE / 100, 2)
                })
                notify(f'PAPER EXIT {symbol} {direction} {pos["leg"]}: '
                       f'{"STOP" if stop_hit else "TARGET"} | net {net_pct:.3f}%')
                break
            # Trailing after 2R / 3R for higher legs (only effective next candle).
            if pos['leg'] in ('3R', '4R'):
                progress = (bar['h'] - pos['entry']) if direction == 'BUY' else (pos['entry'] - bar['l'])
                threshold = 2 if pos['leg'] == '3R' else 3
                if progress >= threshold * pos['risk']:
                    proposed = pos['entry'] + (threshold - 1) * pos['risk'] * (1 if direction == 'BUY' else -1)
                    pos['stop'] = max(pos['stop'], proposed) if direction == 'BUY' else min(pos['stop'], proposed)
            pos['last_checked_t'] = bar['t']
        else:
            remaining.append(pos)
    state['positions'] = remaining


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    state = load_state()
    failures = []
    for symbol in SYMBOLS:
        try:
            bars = candles(symbol)
            if not bars:
                continue
            # Settle first; old entries remain safe across missed workflow runs.
            settle(state, symbol, bars)
            day = str(utc(bars[-1]['t']).date())
            if state['entered_days'].get(symbol) == day:
                state['last_closed_bar'][symbol] = bars[-1]['t']
                save_state(state)
                continue
            last = int(state['last_closed_bar'].get(symbol, 0))
            if last == 0:
                # First-ever run: do not retroactively enter on old signals.
                state['last_closed_bar'][symbol] = bars[-1]['t']
                save_state(state)
                LOG.info('%s initialized at %s', symbol, iso(bars[-1]['t']))
                continue
            setup = find_setup(bars, last)
            state['last_closed_bar'][symbol] = bars[-1]['t']
            if setup and not any(p['symbol'] == symbol for p in state['positions']):
                blocked, reason = news_impact_entry_decision(symbol, setup['direction'])
                if blocked:
                    notify(record_news_skip(PREFIX, symbol, setup['direction'], reason))
                    # Mark this day as processed: never enter a blocked
                    # setup later, after the news window has passed.
                    state['entered_days'][symbol] = day
                    save_state(state)
                    continue
                for rr in (2, 3, 4):
                    target = setup['entry'] + (rr * setup['risk'] * (1 if setup['direction'] == 'BUY' else -1))
                    state['positions'].append({
                        'symbol': symbol, 'direction': setup['direction'], 'leg': f'{rr}R',
                        'entry': setup['entry'], 'stop': setup['stop'],
                        'initial_stop': setup['stop'], 'target': target,
                        'risk': setup['risk'], 'entry_time_utc': iso(setup['bar_t']),
                        'last_checked_t': setup['bar_t']
                    })
                state['entered_days'][symbol] = day
                append_csv(SIGNALS, {'symbol': symbol, 'time_utc': iso(setup['bar_t']),
                                     'direction': setup['direction'], 'entry': setup['entry'],
                                     'stop': setup['stop'], 'asian_high': setup['asian_high'],
                                     'asian_low': setup['asian_low'], 'approx_poc': round(setup['poc'], 8),
                                     'poc_confirms': setup['poc_confirms']})
                notify(f'PAPER ENTRY {symbol} {setup["direction"]} @ {setup["entry"]} '
                       f'SL {setup["stop"]} | 2R/3R/4R | POC {setup["poc_confirms"]}')
                LOG.info('%s PAPER ENTRY %s', symbol, setup['direction'])
            else:
                LOG.info('%s no fresh qualified setup; open legs=%s', symbol,
                         sum(p['symbol'] == symbol for p in state['positions']))
            save_state(state)
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            LOG.error('%s: %s', symbol, exc)
            failures.append(symbol)
    save_state(state)
    LOG.info('COMPLETE open_legs=%s failures=%s', len(state['positions']), failures)
    if failures:
        raise RuntimeError('CoinDCX data/scan failed for: ' + ', '.join(failures))


if __name__ == '__main__':
    main()
