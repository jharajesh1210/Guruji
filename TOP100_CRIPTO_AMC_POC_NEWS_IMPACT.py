
"""TOP100 CoinDCX Futures AMD + approximate POC forward PAPER scanner.
No real orders. GitHub Actions can invoke every 15 minutes.
"""
import csv
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
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


NAME = 'TOP100_CRIPTO_AMC_POC_NEWS_IMPACT'
STATE = Path(NAME + '_state.json')
TRADES = Path(NAME + '_trades.csv')
SIGNALS = Path(NAME + '_signals.csv')
UNIVERSE = Path(NAME + '_universe.csv')

MIN_TURNOVER = 500_000
ACCUM_HOURS = 4
MAX_RANGE_PCT = 2.0
RR_VALUES = (4,)  # New positions use 4R with 2R/3R trailing stops
FEE_SIDE = 0.0005
SLIP_SIDE = 0.0002
MAX_WORKERS = 5
ENTRY_MAX_DELAY_MIN = 5

LOG = logging.getLogger(NAME)


def utc_now():
    return pd.Timestamp.now(tz='UTC')


def iso(value):
    return pd.Timestamp(value).isoformat()


def request_json(url, params=None):
    for attempt in range(3):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=25,
                headers={'User-Agent': 'Top100-AMC-POC-Paper/3.0'}
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            LOG.warning(
                'HTTP attempt %d failed: %s | %s',
                attempt + 1, url, exc
            )
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    return None


def telegram(message):
    token = os.environ.get('BOT_TOKEN')
    chat = os.environ.get('CHAT_ID')

    if not token or not chat:
        LOG.warning('Telegram secrets absent: %s', message)
        return

    try:
        response = requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat, 'text': message[:4000]},
            timeout=20
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        LOG.warning('Telegram send failed: %s', exc)


def initial_state():
    return {
        'last_bar': {},
        'pending': {},
        'open_trades': [],
        'closed_count': 0,
        'closed_wins': 0,
        'net_return_sum_pct': 0.0
    }


def load_state():
    if not STATE.exists():
        return initial_state()

    try:
        data = json.loads(STATE.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('State is not a JSON object')
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f'State unreadable; stopping safely: {exc}'
        ) from exc

    for key, default in initial_state().items():
        data.setdefault(key, default)

    return data


def save_state(state):
    temp = STATE.with_suffix('.json.tmp')
    temp.write_text(
        json.dumps(state, indent=2, allow_nan=False),
        encoding='utf-8'
    )
    temp.replace(STATE)


def append_csv(path, row):
    new = not path.exists() or path.stat().st_size == 0

    with path.open('a', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        if new:
            writer.writeheader()
        writer.writerow(row)


def market_cap_top100():
    data = request_json(
        'https://api.coingecko.com/api/v3/coins/markets',
        {
            'vs_currency': 'usd',
            'order': 'market_cap_desc',
            'per_page': 100,
            'page': 1,
            'sparkline': 'false'
        }
    )

    if not isinstance(data, list) or len(data) < 50:
        raise RuntimeError(
            'CoinGecko Top100 unavailable; safe stop'
        )

    stables = {
        'usdt', 'usdc', 'dai', 'fdusd', 'tusd',
        'usde', 'usdd', 'pyusd', 'usds',
        'rlusd', 'eurc'
    }

    result = []

    for coin in data:
        symbol = str(coin.get('symbol', '')).upper()

        if symbol and symbol.lower() not in stables:
            result.append(
                (symbol, coin.get('market_cap_rank'))
            )

    return result


def futures_pairs():
    data = request_json(
        'https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments'
    )

    if isinstance(data, dict):
        data = data.get(
            'data', data.get('instruments', [])
        )

    if not isinstance(data, list):
        raise RuntimeError(
            'CoinDCX futures list unavailable; safe stop'
        )

    mapping = {}

    for item in data:
        if isinstance(item, str):
            pair = item.upper()
        elif isinstance(item, dict):
            pair = str(
                item.get('pair')
                or item.get('instrument')
                or item.get('symbol')
                or item.get('coindcx_name')
                or ''
            ).upper()
        else:
            continue

        if pair.endswith('_USDT'):
            if not pair.startswith('B-'):
                pair = 'B-' + pair

            symbol = pair[2:-5]
            mapping[symbol] = pair

    if not mapping:
        raise RuntimeError(
            'No CoinDCX USDT futures found; safe stop'
        )

    return mapping


def candles(pair, resolution, days):
    end = int(time.time())

    raw = request_json(
        'https://public.coindcx.com/market_data/candlesticks',
        {
            'pair': pair,
            'pcode': 'f',
            'resolution': str(resolution),
            'from': end - int(days * 86400),
            'to': end
        }
    )

    if not isinstance(raw, dict):
        return pd.DataFrame()

    if not isinstance(raw.get('data'), list):
        return pd.DataFrame()

    df = pd.DataFrame(raw['data'])

    columns = [
        'time', 'open', 'high',
        'low', 'close', 'volume'
    ]

    if df.empty or any(
        column not in df.columns for column in columns
    ):
        return pd.DataFrame()

    for column in columns:
        df[column] = pd.to_numeric(
            df[column], errors='coerce'
        )

    df = df.dropna(subset=columns)

    if df.empty:
        return df

    unit = (
        'ms' if df['time'].median() > 1e11 else 's'
    )

    df['time'] = pd.to_datetime(
        df['time'], unit=unit, utc=True
    )

    df = (
        df.drop_duplicates('time')
        .set_index('time')
        .sort_index()
    )

    df = df[
        (df['high'] >= df['low'])
        & (df['close'] > 0)
        & (df['volume'] >= 0)
    ]

    duration = pd.Timedelta(
        minutes=int(resolution)
    )

    return df[
        df.index + duration <= utc_now()
    ].copy()


def make_15m(df5):
    if df5.empty:
        return pd.DataFrame()

    grouped = df5.resample(
        '15min',
        label='left',
        closed='left'
    )

    result = grouped.agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    })

    count = grouped['close'].count()

    # Three complete 5m candles required.
    return result[count == 3].dropna()


def approximate_poc(df):
    typical = (
        (df['high'] + df['low'] + df['close']) / 3
    ).to_numpy()

    volume = df['volume'].to_numpy()

    if len(typical) == 0:
        return 0.0

    if (
        np.sum(volume) <= 0
        or np.ptp(typical) == 0
    ):
        return float(typical[-1])

    histogram, edges = np.histogram(
        typical,
        bins=30,
        weights=volume
    )

    best = int(np.argmax(histogram))

    return float(
        (edges[best] + edges[best + 1]) / 2
    )


def accumulation(hourly, sweep_start):
    prior = hourly[
        hourly.index + pd.Timedelta(hours=1)
        <= sweep_start
    ]

    if len(prior) < ACCUM_HOURS:
        return None

    segment = prior.tail(ACCUM_HOURS)

    high = float(segment['high'].max())
    low = float(segment['low'].min())
    average = float(segment['close'].mean())

    if average <= 0:
        return None

    range_pct = (high - low) / average * 100

    if range_pct >= MAX_RANGE_PCT:
        return None

    return {
        'high': high,
        'low': low,
        'poc': approximate_poc(segment)
    }


def volume_candidate(item):
    symbol, rank, pair = item

    hourly = candles(pair, 60, 3)

    if len(hourly) < ACCUM_HOURS + 1:
        return None

    last = hourly.iloc[-1]

    # Approximate USDT turnover.
    turnover = float(
        last['volume'] * last['close']
    )

    return {
        'symbol': symbol,
        'rank': rank,
        'pair': pair,
        'turnover': turnover,
        'hourly': hourly
    }


def open_paper(state, pair, direction, bar_end, raw_entry, stop, poc, turnover):
    # One position per signal; trailing lock at 2R and 3R; final exit at 4R.
    if any(t['pair'] == pair for t in state['open_trades']):
        return
    entry = raw_entry * (1 + SLIP_SIDE if direction == 'LONG' else 1 - SLIP_SIDE)
    if (direction == 'LONG' and stop >= entry) or (direction == 'SHORT' and stop <= entry):
        return
    risk = abs(entry - stop)
    if entry <= 0 or not 0.0005 <= risk / entry <= 0.10:
        return
    blocked, reason = news_impact_entry_decision(pair, direction)
    if blocked:
        telegram(record_news_skip(NAME, pair, direction, reason))
        LOG.info('NEWS BLOCKED %s %s: %s', pair, direction, reason)
        return
    sign = 1 if direction == 'LONG' else -1
    trade = {
        'id': f'{pair}-{iso(bar_end)}-TRAIL-RR4',
        'pair': pair, 'direction': direction, 'rr': 4,
        'entry_time': iso(bar_end), 'entry': float(entry),
        'stop': float(stop), 'original_stop': float(stop),
        'target': float(entry + sign * 4 * risk),
        'poc': float(poc), 'turnover': float(turnover),
        'last_checked_5m': iso(bar_end),
        'locked_rr': 0, 'trailing_stop': float(stop),
        'strategy': 'TRAIL_RR4'
    }
    state['open_trades'].append(trade)
    append_csv(SIGNALS, {
        'trade_id': trade['id'], 'pair': pair, 'direction': direction,
        'entry_time': iso(bar_end), 'entry': entry, 'stop': stop,
        'target': trade['target'], 'rr': 4,
        'approx_poc': poc, 'turnover_usdt': turnover
    })
    telegram(
        f'PAPER ENTRY | {NAME}\n{pair} {direction}\n'
        f'Entry approx: {entry:.8g}\nOriginal SL: {stop:.8g}\n'
        'At 1:2 lock SL at 1:2; at 1:3 lock SL at 1:3; final TP 1:4.\n'
        'No real order. Candle-close paper fill.'
    )
    LOG.info('PAPER ENTRY %s %s at %s TRAIL-RR4', pair, direction, iso(bar_end))



def settle_trades(state, cached_5m):
    remaining = []
    for trade in state['open_trades']:
        pair = trade['pair']
        if pair not in cached_5m:
            cached_5m[pair] = candles(pair, 5, 3)
        df = cached_5m[pair]
        if df.empty:
            remaining.append(trade)
            continue
        since = pd.Timestamp(trade['last_checked_5m'])
        newer = df[df.index >= since]
        outcome = None
        trailing = trade.get('strategy') == 'TRAIL_RR4'
        entry = float(trade['entry'])
        original_stop = float(trade.get('original_stop', trade['stop']))
        risk = abs(entry - original_stop)
        sign = 1 if trade['direction'] == 'LONG' else -1
        for t, bar in newer.iterrows():
            active_stop = float(trade.get('trailing_stop', trade['stop'])) if trailing else float(trade['stop'])
            target = float(trade['target'])
            if sign == 1:
                hit_sl = bar['low'] <= active_stop
                hit_tp = bar['high'] >= target
            else:
                hit_sl = bar['high'] >= active_stop
                hit_tp = bar['low'] <= target
            # Conservative ordering: the stop active at the START of this
            # candle has priority. New milestones activate on the NEXT candle.
            if hit_sl or hit_tp:
                result = ('TRAIL_SL' if trailing and trade.get('locked_rr', 0) else 'SL') if hit_sl else 'TP_RR4' if trailing else 'TP'
                outcome = (result, t, active_stop if hit_sl else target)
                break
            if trailing and risk > 0:
                best = float(bar['high']) if sign == 1 else float(bar['low'])
                reached = sign * (best - entry) / risk
                current_lock = int(trade.get('locked_rr', 0))
                new_lock = 3 if reached >= 3 else 2 if reached >= 2 else current_lock
                if new_lock > current_lock:
                    trade['locked_rr'] = new_lock
                    trade['trailing_stop'] = float(entry + sign * new_lock * risk)
                    LOG.info('TRAIL LOCK %s %s reached 1:%d; new SL %.8g (next candle)', pair, trade['direction'], new_lock, trade['trailing_stop'])
                    telegram(
                        f'PAPER TRAILING SL | {NAME}\n{pair} {trade["direction"]}\n'
                        f'1:{new_lock} reached; SL locked at {trade["trailing_stop"]:.8g}\n'
                        'Effective from next 5m candle. No real order.'
                    )
        if outcome is None:
            if not newer.empty:
                trade['last_checked_5m'] = iso(newer.index[-1] + pd.Timedelta(minutes=5))
            remaining.append(trade)
            continue
        result, exit_time, raw_exit = outcome
        exit_price = raw_exit * (1 - SLIP_SIDE if sign == 1 else 1 + SLIP_SIDE)
        gross = sign * (exit_price / entry - 1) * 100
        net = gross - 2 * FEE_SIDE * 100
        state['closed_count'] += 1
        state['closed_wins'] += int(net > 0)
        state['net_return_sum_pct'] += net
        append_csv(TRADES, {
            'trade_id': trade['id'], 'pair': pair,
            'direction': trade['direction'], 'rr': trade['rr'],
            'entry_time': trade['entry_time'], 'exit_time': iso(exit_time),
            'entry': entry, 'stop': trade['stop'], 'target': trade['target'],
            'exit': exit_price, 'result': result,
            'net_return_pct_per_leg': round(net, 4)
        })
        rate = 100 * state['closed_wins'] / state['closed_count']
        telegram(
            f'PAPER EXIT | {NAME}\n{trade["id"]}\n{result}\n'
            f'Net per-position return: {net:+.2f}%\n'
            f'Closed positions/legs: {state["closed_count"]}\n'
            f'Win rate: {rate:.1f}%\nNo real order.'
        )
        LOG.info('PAPER EXIT %s %s %.2f%%', trade['id'], result, net)
    state['open_trades'] = remaining



def process_coin(state, coin, cached_5m):
    pair = coin['pair']

    if pair not in cached_5m:
        cached_5m[pair] = candles(pair, 5, 3)

    bars = make_15m(cached_5m[pair])

    if len(bars) < 3:
        return

    latest = bars.index[-1]

    if pair not in state['last_bar']:
        state['last_bar'][pair] = iso(latest)
        state['pending'].pop(pair, None)

        LOG.info(
            'Initialized %s at %s (no historical entries)',
            pair, iso(latest)
        )
        return

    last = pd.Timestamp(
        state['last_bar'][pair]
    )

    unprocessed = bars[
        bars.index > last
    ]

    if unprocessed.empty:
        return

    # Process missed 15m candles in order.
    for start, bar in unprocessed.iterrows():
        expected = last + pd.Timedelta(
            minutes=15
        )

        if start != expected:
            state['pending'].pop(pair, None)

            LOG.warning(
                'Data gap for %s: %s -> %s; pending reset',
                pair, iso(last), iso(start)
            )

        pending = state['pending'].pop(
            pair, None
        )

        if (
            pending
            and pd.Timestamp(
                pending['sweep_start']
            ) == start - pd.Timedelta(minutes=15)
        ):
            direction = pending['direction']
            buffer = 0.0008

            if direction == 'LONG':
                confirmed = (
                    bar['close']
                    > pending['sweep_high']
                    * (1 + buffer)
                )
            else:
                confirmed = (
                    bar['close']
                    < pending['sweep_low']
                    * (1 - buffer)
                )

            end = start + pd.Timedelta(
                minutes=15
            )

            age_minutes = (
                utc_now() - end
            ).total_seconds() / 60

            if (
                confirmed
                and start == latest
                and 0 <= age_minutes
                <= ENTRY_MAX_DELAY_MIN
            ):
                open_paper(
                    state,
                    pair,
                    direction,
                    end,
                    float(bar['close']),
                    pending['stop'],
                    pending['poc'],
                    coin['turnover']
                )

            elif confirmed:
                LOG.info(
                    'Old confirmed signal skipped safely: %s %s',
                    pair, iso(start)
                )

        region = accumulation(
            coin['hourly'], start
        )

        if region is not None:
            hi = region['high']
            lo = region['low']

            direction = None

            if (
                bar['low'] < lo
                and lo < bar['close'] <= hi
            ):
                direction = 'LONG'

            elif (
                bar['high'] > hi
                and lo <= bar['close'] < hi
            ):
                direction = 'SHORT'

            if direction:
                state['pending'][pair] = {
                    'sweep_start': iso(start),
                    'direction': direction,
                    'sweep_high': float(
                        bar['high']
                    ),
                    'sweep_low': float(
                        bar['low']
                    ),
                    'stop': float(
                        bar['low']
                        if direction == 'LONG'
                        else bar['high']
                    ),
                    'poc': region['poc']
                }

                LOG.info(
                    '%s %s sweep at %s; waiting next candle',
                    pair, direction, iso(start)
                )

        last = start
        state['last_bar'][pair] = iso(start)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s'
    )

    state = load_state()

    LOG.info(
        'Starting %s | PAPER ONLY | %s',
        NAME,
        datetime.now(timezone.utc).isoformat()
    )

    cached_5m = {}

    try:
        # Monitor open trades first.
        settle_trades(state, cached_5m)
        save_state(state)

        market = market_cap_top100()
        futures = futures_pairs()

        candidates = [
            (symbol, rank, futures[symbol])
            for symbol, rank in market
            if symbol in futures
        ]

        LOG.info(
            'Top100 matched to CoinDCX futures: %d',
            len(candidates)
        )

        eligible = []

        with ThreadPoolExecutor(
            max_workers=MAX_WORKERS
        ) as executor:

            tasks = [
                executor.submit(
                    volume_candidate, item
                )
                for item in candidates
            ]

            for task in as_completed(tasks):
                try:
                    item = task.result()

                    if (
                        item
                        and item['turnover']
                        > MIN_TURNOVER
                    ):
                        eligible.append(item)

                except Exception as exc:
                    LOG.warning(
                        'Coin scan failed: %s', exc
                    )

        eligible.sort(
            key=lambda x: (
                x['rank']
                if x['rank'] is not None
                else 9999
            )
        )

        pd.DataFrame(
            [
                {
                    'symbol': x['symbol'],
                    'rank': x['rank'],
                    'pair': x['pair'],
                    'previous_1h_turnover_usdt_approx':
                        round(x['turnover'], 2)
                }
                for x in eligible
            ],
            columns=[
                'symbol',
                'rank',
                'pair',
                'previous_1h_turnover_usdt_approx'
            ]
        ).to_csv(
            UNIVERSE, index=False
        )

        LOG.info(
            'Passed >500k approximate USDT turnover: %d',
            len(eligible)
        )

        for coin in eligible:
            try:
                process_coin(
                    state,
                    coin,
                    cached_5m
                )
                save_state(state)

            except Exception as exc:
                LOG.exception(
                    'Failed processing %s: %s',
                    coin['pair'], exc
                )

        LOG.info(
            'COMPLETE eligible=%d open_legs=%d closed_legs=%d',
            len(eligible),
            len(state['open_trades']),
            state['closed_count']
        )

    finally:
        save_state(state)


if __name__ == '__main__':
    main()
