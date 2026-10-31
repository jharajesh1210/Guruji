"""TOP100 CoinDCX Futures AMD + approximate POC forward PAPER scanner (V4.2).
No real orders. GitHub Actions can invoke every 15 minutes.

V4.2 (from the 21 closed trades, 24-28 Oct: +7.0R, 38% win, 10 SL = -11.6R):
 G. signals_in_bar >= 5 => skip the WHOLE bar ('signal_cluster'). 5 such trades,
    0 winners (-3.6R). Many coins sweeping together = a market-wide move, not a
    local liquidity grab, so the 'sweep and reclaim' idea does not apply.
 H. MIN_RISK_PCT 0.40 -> 0.60. Fees+slippage (~0.14% round trip) cost 0.25R+ on a
    tight stop; the two trades under 0.60% risk (SOL, PAXG) both hit SL.
 I. PAXG excluded (gold token: BTC filter meaningless, tiny range, SL at 0.0R MFE).
 J. result_counts / recent_r are rebuilt from trades.csv if the state lost them
    (state showed SL:1 and one recent R although 21 trades were closed).
 Already in V4.1 (kept): range_pct > 1.5, sweep_depth > 0.18, POC ahead > 0.25R,
    1R lock at +0.25R. Those filters did not exist when most of the 21 trades ran.
 Caveat: 21 trades is a tiny sample. Longs are weak (14 trades -3.8R) and shorts
    strong (7 trades +10.8R); that may be regime, not edge, so no long ban yet.
 NOTE: NAME unchanged => existing state/CSV files continue.
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

NAME = 'TOP100_CRIPTO_AMC_POC_V4'  # unchanged => state/CSVs continue
STATE = Path(NAME + '_state.json')
TRADES = Path(NAME + '_trades.csv')
SIGNALS = Path(NAME + '_signals.csv')
SKIPPED = Path(NAME + '_skipped.csv')
UNIVERSE = Path(NAME + '_universe.csv')

# ---------------- Universe / accumulation ----------------
MIN_TURNOVER = 500_000          # avg hourly USDT turnover (last TURNOVER_HOURS)
TURNOVER_HOURS = 6
ACCUM_HOURS = 4
MAX_RANGE_PCT = 2.0             # absolute cap on accumulation range
RANGE_ATR_MULT = 1.5            # range must be <= 1.5 x hourly ATR
ATR_PERIOD = 14
EXCLUDE_SYMBOLS = ('PAXG',)     # V4.2: gold token, does not behave like the crypto book

# ---------------- Entry / risk ----------------
RR_VALUES = (4,)
FEE_SIDE = 0.0005
SLIP_SIDE = 0.0002
ENTRY_MAX_DELAY_MIN = 14        # GitHub cron is often late
MAX_ENTRY_DEVIATION_PCT = 0.30  # latest 5m close vs confirm-bar close (abs sanity gate)
MAX_CHASE_R = 0.35              # adverse move since confirm close, in R of the trade
MAX_TRADE_RANGE_PCT = 1.5       # range 1.5-2.0% => 4 of 4 SL
MAX_SWEEP_DEPTH_PCT = 0.18      # depth > 0.18% => 4 of 4 SL
USE_POC_AHEAD_FILTER = True     # skip if POC is further than POC_MAX_AHEAD_R ahead of entry
POC_MAX_AHEAD_R = 0.25          # poc_room_r > 0.25 => 7 of 8 SL
USE_CONFIRM_QUALITY = False     # log only until validated
MIN_CLOSE_STRENGTH = 0.60       # confirm bar must close in top 60% (LONG) / bottom 40% (SHORT)
MIN_CONFIRM_VOL_RATIO = 1.0     # confirm bar volume / mean of previous 8 bars
CONFIRM_BUFFER = 0.0008
SL_ATR_BUFFER = 0.15            # extra stop distance beyond wick, in hourly ATR
MIN_RISK_PCT = 0.60             # V4.2: was 0.40; below this fees eat the trade
MAX_RISK_PCT = 2.00

# ---------------- POC filter ----------------
# OFF by default (V4.1): poc_room_r is still logged per trade.
USE_POC_FILTER = False
POC_MIN_R = 0.5                 # POC must be >= 0.5R away on the profit side

# ---------------- Correlation / BTC filter ----------------
MAX_ENTRIES_PER_BAR = 2         # 1 per bar tested worse on V4 data
MAX_SIGNALS_IN_BAR = 5          # V4.2: >= this many signals in one bar => market-wide move, skip all
MAX_OPEN_TRADES = 4
MAX_SAME_DIR_OPEN = 4           # = off; lower it if same-direction clusters keep losing
USE_BTC_FILTER = True
BTC_PAIR = 'B-BTC_USDT'
BTC_MAX_1H_PCT = 1.5            # skip all entries if |BTC 1h| larger
BTC_MAX_4H_PCT = 3.0
BTC_ALIGN_PCT = 0.5             # skip LONG if BTC 1h < -0.5%, SHORT if > +0.5%

# ---------------- Time filter (log first, filter later) ----------------
SKIP_HOURS_UTC = ()             # 21-23 UTC: 8 trades -1.0R so far; wait for more data

# ---------------- Trailing ----------------
# reached R -> locked stop in R. Lock activates from the NEXT 5m candle.
TRAIL_STEPS = {1: 0.25, 2: 2.0, 3: 3.0}
FINAL_RR = 4

MAX_WORKERS = 5

LOG = logging.getLogger(NAME)


def utc_now():
    return pd.Timestamp.now(tz='UTC')


def iso(value):
    return pd.Timestamp(value).isoformat()


def request_json(url, params=None):
    for attempt in range(3):
        try:
            response = requests.get(
                url, params=params, timeout=25,
                headers={'User-Agent': 'Top100-AMC-POC-Paper/5.0'}
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            LOG.warning('HTTP attempt %d failed: %s | %s', attempt + 1, url, exc)
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    return None


def telegram(message):
    token = os.environ.get('BOT_TOKEN')
    chat = os.environ.get('CHAT_ID')

    if not token or not chat:
        LOG.warning('TELEGRAM DELIVERY: NOT CONFIGURED')
        return

    try:
        response = requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat, 'text': message[:4000]},
            timeout=20
        )
        response.raise_for_status()
        if not response.json().get('ok'):
            raise ValueError('Telegram API returned ok=false')
        LOG.info('TELEGRAM DELIVERY: API ACCEPTED')
    except (requests.RequestException, ValueError) as exc:
        LOG.error('TELEGRAM DELIVERY: FAILED: %s', exc)


def initial_state():
    return {
        'last_bar': {},
        'pending': {},
        'open_trades': [],
        'closed_count': 0,
        'closed_wins': 0,
        'net_return_sum_pct': 0.0,
        'net_r_sum': 0.0,
        'result_counts': {},
        'recent_r': []
    }


def load_state():
    if not STATE.exists():
        return initial_state()

    try:
        data = json.loads(STATE.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('State is not a JSON object')
    except (OSError, ValueError) as exc:
        raise RuntimeError(f'State unreadable; stopping safely: {exc}') from exc

    for key, default in initial_state().items():
        data.setdefault(key, default)

    return data


def backfill_stats(state):
    """V4.2: result_counts / recent_r were added after many trades had closed, so
    the state under-reported them. Rebuild once from trades.csv when it matches."""
    counts = state.get('result_counts') or {}
    if sum(counts.values()) >= state['closed_count'] or not TRADES.exists():
        return

    try:
        df = pd.read_csv(TRADES)
    except Exception as exc:
        LOG.warning('Backfill skipped: %s', exc)
        return

    if len(df) != state['closed_count'] or 'result' not in df or 'net_r' not in df:
        LOG.warning('Backfill skipped: trades.csv rows (%d) != closed_count (%d)',
                    len(df), state['closed_count'])
        return

    state['result_counts'] = {k: int(v) for k, v in df['result'].value_counts().items()}
    state['recent_r'] = [round(float(x), 3) for x in df['net_r'].tail(10)]
    LOG.info('Backfilled result_counts and recent_r from trades.csv')


def save_state(state):
    temp = STATE.with_suffix('.json.tmp')
    temp.write_text(json.dumps(state, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(STATE)


def append_csv(path, row):
    # Keep the header the file already has. Rows with different keys must not
    # crash the run (DictWriter raises on unknown keys): extras are ignored and
    # missing values are left blank.
    exists = path.exists() and path.stat().st_size > 0
    fieldnames = list(row)

    if exists:
        with path.open('r', newline='', encoding='utf-8') as handle:
            header = next(csv.reader(handle), None)
        if header:
            fieldnames = header

    with path.open('a', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(
            handle, fieldnames=fieldnames, extrasaction='ignore', restval=''
        )
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def market_cap_top100():
    data = request_json(
        'https://api.coingecko.com/api/v3/coins/markets',
        {
            'vs_currency': 'usd', 'order': 'market_cap_desc',
            'per_page': 100, 'page': 1, 'sparkline': 'false'
        }
    )

    if not isinstance(data, list) or len(data) < 50:
        raise RuntimeError('CoinGecko Top100 unavailable; safe stop')

    stables = {
        'usdt', 'usdc', 'dai', 'fdusd', 'tusd', 'usde', 'usdd',
        'pyusd', 'usds', 'rlusd', 'eurc'
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
                item.get('pair') or item.get('instrument')
                or item.get('symbol') or item.get('coindcx_name') or ''
            ).upper()
        else:
            continue

        if pair.endswith('_USDT'):
            if not pair.startswith('B-'):
                pair = 'B-' + pair
            mapping[pair[2:-5]] = pair

    if not mapping:
        raise RuntimeError('No CoinDCX USDT futures found; safe stop')
    return mapping


def candles(pair, resolution, days):
    end = int(time.time())

    raw = request_json(
        'https://public.coindcx.com/market_data/candlesticks',
        {
            'pair': pair, 'pcode': 'f', 'resolution': str(resolution),
            'from': end - int(days * 86400), 'to': end
        }
    )

    if not isinstance(raw, dict) or not isinstance(raw.get('data'), list):
        return pd.DataFrame()

    df = pd.DataFrame(raw['data'])
    columns = ['time', 'open', 'high', 'low', 'close', 'volume']

    if df.empty or any(c not in df.columns for c in columns):
        return pd.DataFrame()

    for column in columns:
        df[column] = pd.to_numeric(df[column], errors='coerce')

    df = df.dropna(subset=columns)
    if df.empty:
        return df

    unit = 'ms' if df['time'].median() > 1e11 else 's'
    df['time'] = pd.to_datetime(df['time'], unit=unit, utc=True)
    df = df.drop_duplicates('time').set_index('time').sort_index()

    df = df[(df['high'] >= df['low']) & (df['close'] > 0) & (df['volume'] >= 0)]

    duration = pd.Timedelta(minutes=int(resolution))
    return df[df.index + duration <= utc_now()].copy()


def make_15m(df5):
    if df5.empty:
        return pd.DataFrame()

    grouped = df5.resample('15min', label='left', closed='left')
    result = grouped.agg({
        'open': 'first', 'high': 'max', 'low': 'min',
        'close': 'last', 'volume': 'sum'
    })
    count = grouped['close'].count()
    # Three complete 5m candles required.
    return result[count == 3].dropna()


def compute_atr(df, period=ATR_PERIOD):
    if len(df) < period + 1:
        return None
    prev_close = df['close'].shift(1)
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - prev_close).abs(),
        (df['low'] - prev_close).abs()
    ], axis=1).max(axis=1)
    atr = tr.tail(period).mean()
    return float(atr) if pd.notna(atr) and atr > 0 else None


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

    if len(prior) < max(ACCUM_HOURS, ATR_PERIOD + 1):
        return None

    atr = compute_atr(prior)
    if atr is None:
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
    if (high - low) > RANGE_ATR_MULT * atr:
        return None

    return {
        'high': high, 'low': low,
        'poc': approximate_poc(segment),
        'atr': atr, 'range_pct': range_pct
    }


def volume_candidate(item):
    symbol, rank, pair = item

    hourly = candles(pair, 60, 3)

    if len(hourly) < ATR_PERIOD + ACCUM_HOURS + 1:
        return None

    recent = hourly.tail(TURNOVER_HOURS)
    turnover = float((recent['volume'] * recent['close']).mean())

    return {
        'symbol': symbol, 'rank': rank, 'pair': pair,
        'turnover': turnover, 'hourly': hourly
    }


def btc_context():
    """Returns dict with btc_1h / btc_4h percent moves, or None if unavailable."""
    df = candles(BTC_PAIR, 60, 3)
    if len(df) < 6:
        return None
    close = df['close']
    return {
        'btc_1h': float((close.iloc[-1] / close.iloc[-2] - 1) * 100),
        'btc_4h': float((close.iloc[-1] / close.iloc[-5] - 1) * 100)
    }


def log_skip(pair, direction, bar_end, reason, extra=None):
    row = {
        'time': iso(bar_end), 'pair': pair,
        'direction': direction, 'reason': reason,
        'range_pct': '', 'sweep_depth_pct': ''  # fixed columns for every skip row
    }
    if extra:
        row.update(extra)
    append_csv(SKIPPED, row)
    LOG.info('SKIP %s %s %s: %s', pair, direction, iso(bar_end), reason)


def try_open_paper(state, sig, btc, bar_signals):
    """Applies all entry filters. Returns True if a trade was opened."""
    pair = sig['pair']
    direction = sig['direction']
    bar_end = sig['bar_end']
    entry_raw = sig['raw_entry']
    stop = sig['stop']
    poc = sig['poc']

    def skip(reason):
        log_skip(pair, direction, bar_end, reason, {
            'range_pct': round(sig['range_pct'], 3),
            'sweep_depth_pct': round(sig['sweep_depth_pct'], 3)
        })
        return False

    if any(t['pair'] == pair for t in state['open_trades']):
        return skip('already_open')
    if len(state['open_trades']) >= MAX_OPEN_TRADES:
        return skip('max_open_trades')
    if sum(1 for t in state['open_trades'] if t['direction'] == direction) >= MAX_SAME_DIR_OPEN:
        return skip('max_same_direction')
    if bar_end.hour in SKIP_HOURS_UTC:
        return skip('time_filter')

    if USE_BTC_FILTER:
        if btc is None:
            return skip('btc_unavailable')
        if abs(btc['btc_1h']) > BTC_MAX_1H_PCT or abs(btc['btc_4h']) > BTC_MAX_4H_PCT:
            return skip('btc_shock')
        if direction == 'LONG' and btc['btc_1h'] < -BTC_ALIGN_PCT:
            return skip('btc_against_long')
        if direction == 'SHORT' and btc['btc_1h'] > BTC_ALIGN_PCT:
            return skip('btc_against_short')

    if sig['range_pct'] > MAX_TRADE_RANGE_PCT:
        return skip('range_too_wide')
    if sig['sweep_depth_pct'] > MAX_SWEEP_DEPTH_PCT:
        return skip('sweep_too_deep')

    if USE_CONFIRM_QUALITY:
        strength = sig.get('close_strength')
        vol_ratio = sig.get('vol_ratio')
        if strength is not None and strength < MIN_CLOSE_STRENGTH:
            return skip(f'weak_confirm_close_{strength:.2f}')
        if vol_ratio is not None and vol_ratio < MIN_CONFIRM_VOL_RATIO:
            return skip(f'low_confirm_volume_{vol_ratio:.2f}')

    sign = 1 if direction == 'LONG' else -1
    entry = entry_raw * (1 + SLIP_SIDE if sign == 1 else 1 - SLIP_SIDE)

    if (sign == 1 and stop >= entry) or (sign == -1 and stop <= entry):
        return skip('stop_wrong_side')

    risk = abs(entry - stop)
    risk_pct = risk / entry * 100 if entry > 0 else 0

    if risk_pct < MIN_RISK_PCT:
        return skip(f'risk_too_small_{risk_pct:.2f}')
    if risk_pct > MAX_RISK_PCT:
        return skip(f'risk_too_large_{risk_pct:.2f}')

    confirm_close = sig.get('confirm_close')
    chase_r = sign * (entry_raw - confirm_close) / risk if confirm_close and risk > 0 else 0.0
    if chase_r > MAX_CHASE_R:
        return skip(f'chased_entry_{chase_r:.2f}R')

    if USE_POC_AHEAD_FILTER and sign * (poc - entry) / risk > POC_MAX_AHEAD_R:
        return skip(f'poc_too_far_ahead_{sign * (poc - entry) / risk:.2f}R')

    if USE_POC_FILTER:
        room_r = sign * (poc - entry) / risk
        if room_r < POC_MIN_R:
            return skip(f'poc_no_room_{room_r:.2f}R')

    trade = {
        'id': f'{pair}-{iso(bar_end)}-TRAIL-RR{FINAL_RR}',
        'pair': pair, 'direction': direction, 'rr': FINAL_RR,
        'entry_time': iso(bar_end), 'entry': float(entry),
        'stop': float(stop), 'original_stop': float(stop),
        'target': float(entry + sign * FINAL_RR * risk),
        'poc': float(poc), 'turnover': float(sig['turnover']),
        # Only 5m candles AFTER the fill candle may hit SL/TP (cron can be late).
        'last_checked_5m': iso(sig.get('fill_next_5m', bar_end)),
        'locked_rr': 0, 'trailing_stop': float(stop),
        'strategy': 'TRAIL_RR4',
        'mfe_r': 0.0, 'mae_r': 0.0,
        'meta': {
            'btc_1h': round(btc['btc_1h'], 3) if btc else None,
            'btc_4h': round(btc['btc_4h'], 3) if btc else None,
            'range_pct': round(sig['range_pct'], 3),
            'sweep_depth_pct': round(sig['sweep_depth_pct'], 3),
            'risk_pct': round(risk_pct, 3),
            'hour_utc': bar_end.hour,
            'signals_in_bar': bar_signals,
            'poc_room_r': round(sign * (poc - entry) / risk, 2),
            'close_strength': round(sig['close_strength'], 3) if sig.get('close_strength') is not None else None,
            'vol_ratio': round(sig['vol_ratio'], 3) if sig.get('vol_ratio') is not None else None,
            'chase_r': round(chase_r, 3)
        }
    }
    state['open_trades'].append(trade)

    row = {
        'trade_id': trade['id'], 'pair': pair, 'direction': direction,
        'entry_time': iso(bar_end), 'entry': entry, 'stop': stop,
        'target': trade['target'], 'rr': FINAL_RR,
        'approx_poc': poc, 'turnover_usdt': sig['turnover']
    }
    row.update(trade['meta'])
    append_csv(SIGNALS, row)

    telegram(
        f'PAPER ENTRY | {NAME}\n{pair} {direction}\n'
        f'Entry approx: {entry:.8g}\nOriginal SL: {stop:.8g} ({risk_pct:.2f}%)\n'
        f'TP 1:{FINAL_RR}: {trade["target"]:.8g}\n'
        'Locks: 1R->SL +0.25R, 2R->SL 2R, 3R->SL 3R.\n'
        'No real order.'
    )
    LOG.info('PAPER ENTRY %s %s at %s', pair, direction, iso(bar_end))
    return True


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

            # MFE / MAE in R
            if risk > 0:
                fav = sign * ((float(bar['high']) if sign == 1 else float(bar['low'])) - entry) / risk
                adv = sign * ((float(bar['low']) if sign == 1 else float(bar['high'])) - entry) / risk
                trade['mfe_r'] = max(float(trade.get('mfe_r', 0.0)), fav)
                trade['mae_r'] = min(float(trade.get('mae_r', 0.0)), adv)

            if sign == 1:
                hit_sl = bar['low'] <= active_stop
                hit_tp = bar['high'] >= target
            else:
                hit_sl = bar['high'] >= active_stop
                hit_tp = bar['low'] <= target

            # Conservative: stop active at START of the candle has priority.
            if hit_sl or hit_tp:
                if hit_sl:
                    locked = trade.get('locked_rr', 0)
                    result = ('TRAIL_SL' if locked >= 2 else 'BE_SL' if locked == 1 else 'SL') if trailing else 'SL'
                else:
                    result = 'TP_RR4' if trailing else 'TP'
                outcome = (result, t, active_stop if hit_sl else target)
                break

            if trailing and risk > 0:
                best = float(bar['high']) if sign == 1 else float(bar['low'])
                reached = sign * (best - entry) / risk
                current_lock = int(trade.get('locked_rr', 0))
                new_lock = current_lock
                for level in sorted(TRAIL_STEPS):
                    if reached >= level and level > new_lock:
                        new_lock = level

                if new_lock > current_lock:
                    trade['locked_rr'] = new_lock
                    trade['trailing_stop'] = float(entry + sign * TRAIL_STEPS[new_lock] * risk)
                    LOG.info('TRAIL LOCK %s reached 1:%d; new SL %.8g', pair, new_lock, trade['trailing_stop'])
                    telegram(
                        f'PAPER TRAILING SL | {NAME}\n{pair} {trade["direction"]}\n'
                        f'1:{new_lock} reached; SL moved to {trade["trailing_stop"]:.8g}\n'
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
        risk_pct = risk / entry * 100 if entry > 0 else 0
        net_r = net / risk_pct if risk_pct > 0 else 0.0

        state['closed_count'] += 1
        state['closed_wins'] += int(net > 0)
        state['net_return_sum_pct'] += net
        state['net_r_sum'] = state.get('net_r_sum', 0.0) + net_r
        counts = state.setdefault('result_counts', {})
        counts[result] = counts.get(result, 0) + 1
        recent = state.setdefault('recent_r', [])
        recent.append(round(net_r, 3))
        del recent[:-10]

        row = {
            'trade_id': trade['id'], 'pair': pair,
            'direction': trade['direction'], 'rr': trade['rr'],
            'entry_time': trade['entry_time'], 'exit_time': iso(exit_time),
            'entry': entry, 'stop': trade['stop'], 'target': trade['target'],
            'exit': exit_price, 'result': result,
            'net_return_pct_per_leg': round(net, 4),
            'net_r': round(net_r, 3),
            'mfe_r': round(float(trade.get('mfe_r', 0.0)), 3),
            'mae_r': round(float(trade.get('mae_r', 0.0)), 3),
            'locked_rr': trade.get('locked_rr', 0)
        }
        row.update(trade.get('meta', {}))
        append_csv(TRADES, row)

        rate = 100 * state['closed_wins'] / state['closed_count']
        telegram(
            f'PAPER EXIT | {NAME}\n{trade["id"]}\n{result}\n'
            f'Net return: {net:+.2f}% ({net_r:+.2f}R)\n'
            f'MFE {trade.get("mfe_r", 0):.2f}R / MAE {trade.get("mae_r", 0):.2f}R\n'
            f'Closed: {state["closed_count"]} | Win rate: {rate:.1f}%\n'
            f'Total: {state["net_r_sum"]:+.2f}R | Avg: {state["net_r_sum"] / state["closed_count"]:+.2f}R/trade\n'
            f'Last {len(recent)}: {sum(recent):+.2f}R\n'
            f'Results: {", ".join(f"{k} {v}" for k, v in sorted(counts.items()))}\nNo real order.'
        )
        LOG.info('PAPER EXIT %s %s %.2f%% %.2fR', trade['id'], result, net, net_r)

    state['open_trades'] = remaining


def process_coin(state, coin, cached_5m, signals_out):
    """Detect sweeps / confirmations. Confirmed signals are collected in
    signals_out and opened later, after ranking and correlation limits."""
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

    for start, bar in unprocessed.iterrows():
        expected = last + pd.Timedelta(minutes=15)

        if start != expected:
            state['pending'].pop(pair, None)
            LOG.warning('Data gap for %s: %s -> %s; pending reset', pair, iso(last), iso(start))

        pending = state['pending'].pop(pair, None)

        if pending and pd.Timestamp(pending['sweep_start']) == start - pd.Timedelta(minutes=15):
            direction = pending['direction']

            if direction == 'LONG':
                confirmed = bar['close'] > pending['sweep_high'] * (1 + CONFIRM_BUFFER)
            else:
                confirmed = bar['close'] < pending['sweep_low'] * (1 - CONFIRM_BUFFER)

            end = start + pd.Timedelta(minutes=15)
            age_minutes = (utc_now() - end).total_seconds() / 60

            if confirmed and start == latest and 0 <= age_minutes <= ENTRY_MAX_DELAY_MIN:
                # Fill from the latest CLOSED 5m candle (more realistic with late cron)
                df5 = cached_5m[pair]
                fill = float(df5['close'].iloc[-1]) if not df5.empty else float(bar['close'])
                deviation = abs(fill / float(bar['close']) - 1) * 100

                if deviation > MAX_ENTRY_DEVIATION_PCT:
                    log_skip(pair, direction, end, f'entry_ran_away_{deviation:.2f}pct')
                else:
                    rng = float(bar['high']) - float(bar['low'])
                    pos = (float(bar['close']) - float(bar['low'])) / rng if rng > 0 else 0.5
                    strength = pos if direction == 'LONG' else 1 - pos
                    prev_vol = bars[bars.index < start].tail(8)['volume'].mean()
                    vol_ratio = (
                        float(bar['volume']) / float(prev_vol)
                        if pd.notna(prev_vol) and prev_vol > 0 else None
                    )
                    signals_out.append({
                        'confirm_close': float(bar['close']),
                        'close_strength': float(strength), 'vol_ratio': vol_ratio,
                        'pair': pair, 'rank': coin['rank'], 'direction': direction,
                        'bar_end': end, 'raw_entry': fill, 'stop': pending['stop'],
                        'fill_next_5m': (
                            df5.index[-1] + pd.Timedelta(minutes=5)
                            if not df5.empty else end
                        ),
                        'poc': pending['poc'], 'turnover': coin['turnover'],
                        'range_pct': pending['range_pct'],
                        'sweep_depth_pct': pending['sweep_depth_pct']
                    })
            elif confirmed:
                LOG.info('Old confirmed signal skipped safely: %s %s', pair, iso(start))

        region = accumulation(coin['hourly'], start)

        if region is not None:
            hi = region['high']
            lo = region['low']
            direction = None

            if bar['low'] < lo and lo < bar['close'] <= hi:
                direction = 'LONG'
            elif bar['high'] > hi and lo <= bar['close'] < hi:
                direction = 'SHORT'

            if direction:
                buffer = SL_ATR_BUFFER * region['atr']
                if direction == 'LONG':
                    stop = float(bar['low']) - buffer
                    depth = (lo - float(bar['low'])) / lo * 100
                else:
                    stop = float(bar['high']) + buffer
                    depth = (float(bar['high']) - hi) / hi * 100

                state['pending'][pair] = {
                    'sweep_start': iso(start),
                    'direction': direction,
                    'sweep_high': float(bar['high']),
                    'sweep_low': float(bar['low']),
                    'stop': float(stop),
                    'poc': region['poc'],
                    'range_pct': float(region['range_pct']),
                    'sweep_depth_pct': float(depth)
                }
                LOG.info('%s %s sweep at %s; waiting next candle', pair, direction, iso(start))

        last = start
        state['last_bar'][pair] = iso(start)


def open_ranked_signals(state, signals, btc):
    """Group by bar, rank by market-cap rank, cap entries per bar."""
    by_bar = {}
    for sig in signals:
        by_bar.setdefault(sig['bar_end'], []).append(sig)

    for bar_end, group in sorted(by_bar.items()):
        group.sort(key=lambda s: s['rank'] if s['rank'] is not None else 9999)
        total = len(group)
        opened = 0

        # V4.2: many coins sweeping in the same bar = market-wide move, not a local
        # liquidity grab. 5 such trades in the data, 0 winners (-3.6R).
        if total >= MAX_SIGNALS_IN_BAR:
            for sig in group:
                log_skip(sig['pair'], sig['direction'], bar_end,
                         f'signal_cluster_{total}', {
                             'range_pct': round(sig['range_pct'], 3),
                             'sweep_depth_pct': round(sig['sweep_depth_pct'], 3)
                         })
            LOG.info('Bar %s: %d signals, cluster skip', iso(bar_end), total)
            continue

        for sig in group:
            if opened >= MAX_ENTRIES_PER_BAR:
                log_skip(sig['pair'], sig['direction'], bar_end, 'max_entries_per_bar')
                continue
            if try_open_paper(state, sig, btc, total):
                opened += 1

        LOG.info('Bar %s: %d signals, %d opened', iso(bar_end), total, opened)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s'
    )

    state = load_state()
    backfill_stats(state)

    LOG.info('Starting %s | PAPER ONLY | %s', NAME, datetime.now(timezone.utc).isoformat())

    cached_5m = {}

    try:
        settle_trades(state, cached_5m)
        save_state(state)

        market = market_cap_top100()
        futures = futures_pairs()

        candidates = [
            (symbol, rank, futures[symbol])
            for symbol, rank in market
            if symbol in futures and symbol not in EXCLUDE_SYMBOLS
        ]
        LOG.info('Top100 matched to CoinDCX futures: %d', len(candidates))

        btc = btc_context()
        if btc:
            LOG.info('BTC 1h %.2f%% | 4h %.2f%%', btc['btc_1h'], btc['btc_4h'])
        else:
            LOG.warning('BTC context unavailable')

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

        eligible.sort(key=lambda x: x['rank'] if x['rank'] is not None else 9999)

        pd.DataFrame(
            [
                {
                    'symbol': x['symbol'], 'rank': x['rank'], 'pair': x['pair'],
                    'avg_hourly_turnover_usdt_approx': round(x['turnover'], 2)
                }
                for x in eligible
            ],
            columns=['symbol', 'rank', 'pair', 'avg_hourly_turnover_usdt_approx']
        ).to_csv(UNIVERSE, index=False)

        LOG.info('Passed >%d avg hourly turnover: %d', MIN_TURNOVER, len(eligible))

        signals = []
        for coin in eligible:
            try:
                process_coin(state, coin, cached_5m, signals)
                save_state(state)
            except Exception as exc:
                LOG.exception('Failed processing %s: %s', coin['pair'], exc)

        open_ranked_signals(state, signals, btc)

        LOG.info(
            'COMPLETE eligible=%d open=%d closed=%d net_R=%.2f',
            len(eligible), len(state['open_trades']),
            state['closed_count'], state.get('net_r_sum', 0.0)
        )

    finally:
        save_state(state)


if __name__ == '__main__':
    main()
