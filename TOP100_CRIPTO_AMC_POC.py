"""TOP100 CoinDCX Futures AMD + approximate POC forward PAPER scanner.
No real orders. GitHub Actions can invoke every 15 minutes.

V2 FIXES (continuous loss ke liye):
 1. Entry chase band: confirmation ke baad sweep level ke RETEST par entry
 2. Risk range 0.30%-1.20% + MAX_COST_R fee/slippage filter
 3. BTC 1H bias filter (sirf bias ki direction mein trade)
 4. Sweep quality: minimum depth + rejection wick
 5. Stop par buffer
 6. Max open trades / same-direction limit (correlated losses rokne ke liye)
 7. Lagataar 3 loss ke baad entries pause
 8. Liquidity filter 2M USDT, slippage 0.04% per side
 9. Trailing: 1.5R -> BE+, 2R -> lock 1R, 3R -> lock 2R, final 4R
10. CSV/Telegram mein R-multiple
"""
import csv
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

NAME = 'TOP100_CRIPTO_AMC_POC'
STATE = Path(NAME + '_state.json')
TRADES = Path(NAME + '_trades.csv')
SIGNALS = Path(NAME + '_signals.csv')
UNIVERSE = Path(NAME + '_universe.csv')

MIN_TURNOVER = 2_000_000
ACCUM_HOURS = 4
MAX_RANGE_PCT = 2.0
FEE_SIDE = 0.0005
SLIP_SIDE = 0.0004
MAX_WORKERS = 5
ENTRY_MAX_DELAY_MIN = 5

TARGET_RR = 4
# (reach R, lock R): price reach_R tak aaye to stop lock_R par shift
TRAIL_RULES = [(1.5, 0.2), (2.0, 1.0), (3.0, 2.0)]

STOP_BUFFER_PCT = 0.05      # sweep extreme ke bahar extra buffer (%)
MIN_RISK_PCT = 0.30
MAX_RISK_PCT = 1.20
MAX_COST_R = 0.25           # fee+slippage risk ka max 25%

MIN_SWEEP_DEPTH_PCT = 0.10  # range edge ke bahar kam se kam itna sweep
MIN_WICK_RATIO = 0.50       # rejection wick >= candle range ka 50%
CONFIRM_BUFFER = 0.0008     # confirmation close sweep extreme se itna aage
RETEST_EXPIRY_BARS = 4      # retest ke liye max 4 x 15m candles

MAX_OPEN_TRADES = 3
MAX_SAME_DIRECTION = 2
PAUSE_AFTER_LOSSES = 3
PAUSE_HOURS = 4

BTC_PAIR = 'B-BTC_USDT'

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
                headers={'User-Agent': 'Top100-AMC-POC-Paper/4.0'}
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
        'net_return_sum_pct': 0.0,
        'r_sum': 0.0,
        'loss_streak': 0,
        'pause_until': None
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

    # Purane format ke pending setups (stage ke bina) hata do.
    for pair in list(data['pending']):
        item = data['pending'][pair]
        if not isinstance(item, dict) or 'stage' not in item:
            data['pending'].pop(pair)

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

    if not new:
        # Header badla ho to purani file alag rakh do (columns mix na hon).
        with path.open('r', encoding='utf-8') as handle:
            header = handle.readline().strip().split(',')
        if header != list(row):
            backup = path.with_name(
                f'{path.stem}_prev_{int(time.time())}{path.suffix}'
            )
            path.rename(backup)
            LOG.info('CSV header changed; old file kept as %s', backup)
            new = True

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
        raise RuntimeError('CoinGecko Top100 unavailable; safe stop')

    stables = {
        'usdt', 'usdc', 'dai', 'fdusd', 'tusd',
        'usde', 'usdd', 'pyusd', 'usds',
        'rlusd', 'eurc'
    }

    result = []

    for coin in data:
        symbol = str(coin.get('symbol', '')).upper()

        if symbol and symbol.lower() not in stables:
            result.append((symbol, coin.get('market_cap_rank')))

    return result


def futures_pairs():
    data = request_json(
        'https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments'
    )

    if isinstance(data, dict):
        data = data.get('data', data.get('instruments', []))

    if not isinstance(data, list):
        raise RuntimeError('CoinDCX futures list unavailable; safe stop')

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
        raise RuntimeError('No CoinDCX USDT futures found; safe stop')

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

    columns = ['time', 'open', 'high', 'low', 'close', 'volume']

    if df.empty or any(column not in df.columns for column in columns):
        return pd.DataFrame()

    for column in columns:
        df[column] = pd.to_numeric(df[column], errors='coerce')

    df = df.dropna(subset=columns)

    if df.empty:
        return df

    unit = 'ms' if df['time'].median() > 1e11 else 's'

    df['time'] = pd.to_datetime(df['time'], unit=unit, utc=True)

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

    duration = pd.Timedelta(minutes=int(resolution))

    return df[df.index + duration <= utc_now()].copy()


def make_15m(df5):
    if df5.empty:
        return pd.DataFrame()

    grouped = df5.resample('15min', label='left', closed='left')

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
    typical = ((df['high'] + df['low'] + df['close']) / 3).to_numpy()

    volume = df['volume'].to_numpy()

    if len(typical) == 0:
        return 0.0

    if np.sum(volume) <= 0 or np.ptp(typical) == 0:
        return float(typical[-1])

    histogram, edges = np.histogram(typical, bins=30, weights=volume)

    best = int(np.argmax(histogram))

    return float((edges[best] + edges[best + 1]) / 2)


def accumulation(hourly, sweep_start):
    prior = hourly[hourly.index + pd.Timedelta(hours=1) <= sweep_start]

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

    return {'high': high, 'low': low, 'poc': approximate_poc(segment)}


def market_bias():
    """BTC 1H bias: close vs EMA50 aur EMA20/EMA50 ka order."""
    df = candles(BTC_PAIR, 60, 8)

    if len(df) < 55:
        LOG.warning('BTC 1H data kam hai; bias NEUTRAL')
        return 'NEUTRAL'

    ema20 = df['close'].ewm(span=20, adjust=False).mean().iloc[-1]
    ema50 = df['close'].ewm(span=50, adjust=False).mean().iloc[-1]
    last = float(df['close'].iloc[-1])

    if last > ema50 and ema20 > ema50:
        return 'LONG'

    if last < ema50 and ema20 < ema50:
        return 'SHORT'

    return 'NEUTRAL'


def volume_candidate(item):
    symbol, rank, pair = item

    hourly = candles(pair, 60, 3)

    if len(hourly) < ACCUM_HOURS + 1:
        return None

    last = hourly.iloc[-1]

    # Approximate USDT turnover.
    turnover = float(last['volume'] * last['close'])

    return {
        'symbol': symbol,
        'rank': rank,
        'pair': pair,
        'turnover': turnover,
        'hourly': hourly
    }


def is_paused(state):
    value = state.get('pause_until')
    return bool(value) and utc_now() < pd.Timestamp(value)


def open_paper(state, pair, direction, bar_end, raw_entry, stop, poc,
               turnover, bias):
    if any(t['pair'] == pair for t in state['open_trades']):
        return

    if direction != bias:
        LOG.info('SKIP %s %s: BTC bias %s', pair, direction, bias)
        return

    if is_paused(state):
        LOG.info('SKIP %s %s: entries paused after loss streak', pair, direction)
        return

    if len(state['open_trades']) >= MAX_OPEN_TRADES:
        LOG.info('SKIP %s %s: max open trades', pair, direction)
        return

    same = sum(1 for t in state['open_trades'] if t['direction'] == direction)

    if same >= MAX_SAME_DIRECTION:
        LOG.info('SKIP %s %s: same-direction limit', pair, direction)
        return

    is_long = direction == 'LONG'
    sign = 1 if is_long else -1

    buffer = raw_entry * STOP_BUFFER_PCT / 100
    stop = stop - buffer if is_long else stop + buffer
    entry = raw_entry * (1 + SLIP_SIDE if is_long else 1 - SLIP_SIDE)

    if (is_long and stop >= entry) or (not is_long and stop <= entry):
        return

    risk = abs(entry - stop)

    if entry <= 0:
        return

    risk_pct = risk / entry * 100

    cost_pct = (2 * FEE_SIDE + 2 * SLIP_SIDE) * 100
    cost_r = cost_pct / risk_pct if risk_pct > 0 else 99

    if not MIN_RISK_PCT <= risk_pct <= MAX_RISK_PCT:
        LOG.info('SKIP %s %s: risk %.2f%% out of range', pair, direction, risk_pct)
        return

    if cost_r > MAX_COST_R:
        LOG.info('SKIP %s %s: cost %.2fR too high', pair, direction, cost_r)
        return

    trade = {
        'id': f'{pair}-{iso(bar_end)}-TRAIL-RR4',
        'pair': pair, 'direction': direction, 'rr': TARGET_RR,
        'entry_time': iso(bar_end), 'entry': float(entry),
        'stop': float(stop), 'original_stop': float(stop),
        'target': float(entry + sign * TARGET_RR * risk),
        'poc': float(poc), 'turnover': float(turnover),
        'last_checked_5m': iso(bar_end),
        'locked_rr': 0, 'trailing_stop': float(stop),
        'risk_pct': round(risk_pct, 4),
        'strategy': 'TRAIL_RR4'
    }

    state['open_trades'].append(trade)

    append_csv(SIGNALS, {
        'trade_id': trade['id'], 'pair': pair, 'direction': direction,
        'entry_time': iso(bar_end), 'entry': entry, 'stop': stop,
        'target': trade['target'], 'rr': TARGET_RR,
        'risk_pct': round(risk_pct, 4), 'cost_r': round(cost_r, 3),
        'approx_poc': poc, 'turnover_usdt': turnover
    })

    trail_text = ' | '.join(f'{r}R->lock {l}R' for r, l in TRAIL_RULES)

    telegram(
        f'PAPER ENTRY | {NAME}\n{pair} {direction}\n'
        f'BTC bias: {bias}\n'
        f'Entry approx: {entry:.8g}\nSL: {stop:.8g} ({risk_pct:.2f}%)\n'
        f'TP {TARGET_RR}R: {trade["target"]:.8g} | Cost: {cost_r:.2f}R\n'
        f'Trail: {trail_text}\n'
        'No real order. Candle-close paper fill.'
    )

    LOG.info('PAPER ENTRY %s %s at %s', pair, direction, iso(bar_end))


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
            active_stop = (
                float(trade.get('trailing_stop', trade['stop']))
                if trailing else float(trade['stop'])
            )
            target = float(trade['target'])

            if sign == 1:
                hit_sl = bar['low'] <= active_stop
                hit_tp = bar['high'] >= target
            else:
                hit_sl = bar['high'] >= active_stop
                hit_tp = bar['low'] <= target

            # Conservative: candle ke START wala stop pehle. Naya milestone
            # agli candle se active hota hai.
            if hit_sl or hit_tp:
                if hit_sl:
                    moved = float(trade.get('locked_rr', 0)) > 0
                    result = 'TRAIL_SL' if trailing and moved else 'SL'
                else:
                    result = 'TP_RR4' if trailing else 'TP'

                outcome = (result, t, active_stop if hit_sl else target)
                break

            if trailing and risk > 0:
                best = float(bar['high']) if sign == 1 else float(bar['low'])
                reached = sign * (best - entry) / risk
                previous = float(trade.get('locked_rr', 0))

                for reach_rr, lock_rr in sorted(TRAIL_RULES, reverse=True):
                    if reached >= reach_rr and reach_rr > previous:
                        trade['locked_rr'] = reach_rr
                        trade['trailing_stop'] = float(
                            entry + sign * lock_rr * risk
                        )
                        LOG.info(
                            'TRAIL %s %s reached %sR; new SL %.8g (next candle)',
                            pair, trade['direction'], reach_rr,
                            trade['trailing_stop']
                        )
                        telegram(
                            f'PAPER TRAILING SL | {NAME}\n{pair} {trade["direction"]}\n'
                            f'{reach_rr}R reached; SL now {trade["trailing_stop"]:.8g} '
                            f'(locks {lock_rr}R)\n'
                            'Effective from next 5m candle. No real order.'
                        )
                        break

        if outcome is None:
            if not newer.empty:
                trade['last_checked_5m'] = iso(
                    newer.index[-1] + pd.Timedelta(minutes=5)
                )
            remaining.append(trade)
            continue

        result, exit_time, raw_exit = outcome

        exit_price = raw_exit * (1 - SLIP_SIDE if sign == 1 else 1 + SLIP_SIDE)
        gross = sign * (exit_price / entry - 1) * 100
        net = gross - 2 * FEE_SIDE * 100

        risk_pct = risk / entry * 100 if entry > 0 else 0
        r_multiple = net / risk_pct if risk_pct > 0 else 0.0

        state['closed_count'] += 1
        state['closed_wins'] += int(net > 0)
        state['net_return_sum_pct'] += net
        state['r_sum'] = round(state.get('r_sum', 0.0) + r_multiple, 4)

        if net > 0:
            state['loss_streak'] = 0
        else:
            state['loss_streak'] = state.get('loss_streak', 0) + 1

            if state['loss_streak'] >= PAUSE_AFTER_LOSSES:
                state['pause_until'] = iso(
                    utc_now() + pd.Timedelta(hours=PAUSE_HOURS)
                )
                state['loss_streak'] = 0
                telegram(
                    f'PAPER PAUSE | {NAME}\n'
                    f'{PAUSE_AFTER_LOSSES} losses in a row; '
                    f'new entries paused {PAUSE_HOURS}h.'
                )

        append_csv(TRADES, {
            'trade_id': trade['id'], 'pair': pair,
            'direction': trade['direction'], 'rr': trade['rr'],
            'entry_time': trade['entry_time'], 'exit_time': iso(exit_time),
            'entry': entry, 'stop': trade['stop'], 'target': trade['target'],
            'exit': exit_price, 'result': result,
            'net_return_pct_per_leg': round(net, 4),
            'r_multiple': round(r_multiple, 3),
            'risk_pct': round(risk_pct, 4)
        })

        rate = 100 * state['closed_wins'] / state['closed_count']

        telegram(
            f'PAPER EXIT | {NAME}\n{trade["id"]}\n{result}\n'
            f'Net: {net:+.2f}% | {r_multiple:+.2f}R\n'
            f'Closed: {state["closed_count"]} | Win rate: {rate:.1f}%\n'
            f'Total R: {state["r_sum"]:+.2f}\nNo real order.'
        )

        LOG.info('PAPER EXIT %s %s %.2f%% %.2fR', trade['id'], result, net, r_multiple)

    state['open_trades'] = remaining


def detect_sweep(coin, start, bar, bias):
    """Accumulation range ka quality sweep: depth + rejection wick."""
    region = accumulation(coin['hourly'], start)

    if region is None:
        return None

    hi = region['high']
    lo = region['low']

    high = float(bar['high'])
    low = float(bar['low'])
    open_ = float(bar['open'])
    close = float(bar['close'])
    rng = high - low

    if rng <= 0 or close <= 0:
        return None

    direction = None

    if low < lo and lo < close <= hi:
        depth_pct = (lo - low) / close * 100
        wick = (min(open_, close) - low) / rng
        if depth_pct >= MIN_SWEEP_DEPTH_PCT and wick >= MIN_WICK_RATIO:
            direction = 'LONG'

    elif high > hi and lo <= close < hi:
        depth_pct = (high - hi) / close * 100
        wick = (high - max(open_, close)) / rng
        if depth_pct >= MIN_SWEEP_DEPTH_PCT and wick >= MIN_WICK_RATIO:
            direction = 'SHORT'

    if direction is None or direction != bias:
        return None

    return {
        'stage': 'SWEEP',
        'sweep_start': iso(start),
        'direction': direction,
        'sweep_high': high,
        'sweep_low': low,
        'extreme': low if direction == 'LONG' else high,
        'poc': region['poc']
    }


def advance_pending(state, coin, pair, pending, start, end, bar, latest, bias):
    """SWEEP -> (confirm) -> RETEST -> entry. None = setup khatam."""
    is_long = pending['direction'] == 'LONG'

    invalidated = (
        float(bar['low']) < pending['extreme']
        if is_long else float(bar['high']) > pending['extreme']
    )

    if invalidated:
        return None

    if pending['stage'] == 'SWEEP':
        if pd.Timestamp(pending['sweep_start']) != start - pd.Timedelta(minutes=15):
            return None

        if is_long:
            confirmed = float(bar['close']) > pending['sweep_high'] * (1 + CONFIRM_BUFFER)
        else:
            confirmed = float(bar['close']) < pending['sweep_low'] * (1 - CONFIRM_BUFFER)

        if not confirmed:
            return None

        pending['stage'] = 'RETEST'
        pending['confirm_start'] = iso(start)
        pending['bars_waited'] = 0
        LOG.info('%s %s confirmed at %s; waiting retest', pair, pending['direction'], iso(start))
        return pending

    # RETEST stage
    pending['bars_waited'] = int(pending.get('bars_waited', 0)) + 1

    if pending['bars_waited'] > RETEST_EXPIRY_BARS:
        return None

    level = pending['sweep_high'] if is_long else pending['sweep_low']

    if is_long:
        touched = float(bar['low']) <= level
        held = float(bar['close']) > level and float(bar['close']) > float(bar['open'])
    else:
        touched = float(bar['high']) >= level
        held = float(bar['close']) < level and float(bar['close']) < float(bar['open'])

    if not (touched and held):
        return pending

    age_minutes = (utc_now() - end).total_seconds() / 60

    if start == latest and 0 <= age_minutes <= ENTRY_MAX_DELAY_MIN:
        open_paper(
            state, pair, pending['direction'], end, float(bar['close']),
            pending['extreme'], pending['poc'], coin['turnover'], bias
        )
    else:
        LOG.info('Old retest signal skipped safely: %s %s', pair, iso(start))

    return None


def process_coin(state, coin, cached_5m, bias):
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

        LOG.info('Initialized %s at %s (no historical entries)', pair, iso(latest))
        return

    last = pd.Timestamp(state['last_bar'][pair])

    unprocessed = bars[bars.index > last]

    if unprocessed.empty:
        return

    # Process missed 15m candles in order.
    for start, bar in unprocessed.iterrows():
        expected = last + pd.Timedelta(minutes=15)

        if start != expected:
            state['pending'].pop(pair, None)
            LOG.warning('Data gap for %s: %s -> %s; pending reset', pair, iso(last), iso(start))

        end = start + pd.Timedelta(minutes=15)
        pending = state['pending'].get(pair)

        if pending:
            pending = advance_pending(
                state, coin, pair, pending, start, end, bar, latest, bias
            )

            if pending is None:
                state['pending'].pop(pair, None)
            else:
                state['pending'][pair] = pending

        has_open = any(t['pair'] == pair for t in state['open_trades'])

        if pair not in state['pending'] and not has_open:
            setup = detect_sweep(coin, start, bar, bias)

            if setup:
                state['pending'][pair] = setup
                LOG.info('%s %s sweep at %s; waiting confirmation', pair, setup['direction'], iso(start))

        last = start
        state['last_bar'][pair] = iso(start)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s'
    )

    state = load_state()

    LOG.info('Starting %s | PAPER ONLY | %s', NAME, datetime.now(timezone.utc).isoformat())

    cached_5m = {}

    try:
        # Monitor open trades first.
        settle_trades(state, cached_5m)
        save_state(state)

        bias = market_bias()
        LOG.info('BTC 1H bias: %s | paused: %s', bias, is_paused(state))

        market = market_cap_top100()
        futures = futures_pairs()

        candidates = [
            (symbol, rank, futures[symbol])
            for symbol, rank in market
            if symbol in futures
        ]

        LOG.info('Top100 matched to CoinDCX futures: %d', len(candidates))

        eligible = []

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            tasks = [executor.submit(volume_candidate, item) for item in candidates]

            for task in as_completed(tasks):
                try:
                    item = task.result()

                    if item and item['turnover'] > MIN_TURNOVER:
                        eligible.append(item)

                except Exception as exc:
                    LOG.warning('Coin scan failed: %s', exc)

        eligible.sort(
            key=lambda x: x['rank'] if x['rank'] is not None else 9999
        )

        pd.DataFrame(
            [
                {
                    'symbol': x['symbol'],
                    'rank': x['rank'],
                    'pair': x['pair'],
                    'previous_1h_turnover_usdt_approx': round(x['turnover'], 2)
                }
                for x in eligible
            ],
            columns=[
                'symbol', 'rank', 'pair',
                'previous_1h_turnover_usdt_approx'
            ]
        ).to_csv(UNIVERSE, index=False)

        LOG.info('Passed turnover filter: %d', len(eligible))

        for coin in eligible:
            try:
                process_coin(state, coin, cached_5m, bias)
                save_state(state)

            except Exception as exc:
                LOG.exception('Failed processing %s: %s', coin['pair'], exc)

        LOG.info(
            'COMPLETE eligible=%d open=%d closed=%d total_R=%.2f',
            len(eligible),
            len(state['open_trades']),
            state['closed_count'],
            state.get('r_sum', 0.0)
        )

    finally:
        save_state(state)


if __name__ == '__main__':
    main()
