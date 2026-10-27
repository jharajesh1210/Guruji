#!/usr/bin/env python3
"""NIFTY 50 CASH EQUITY intraday, delayed Yahoo observations, no broker orders.
Run: python NIFTY50_EQUITY_INTRADAY.py --mode paper
     python NIFTY50_EQUITY_INTRADAY.py --mode scan
     python NIFTY50_EQUITY_INTRADAY.py --mode report
Options: --no-index-filter   (disable NIFTY index trend filter)
"""
import argparse
import csv
import json
import os
import time as systime
from datetime import datetime, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import requests
import yfinance as yf

IST = ZoneInfo('Asia/Kolkata')
# Snapshot of widely held NIFTY50 constituents; review after NSE index rebalances.
SYMBOLS = '''ADANIENT ADANIPORTS APOLLOHOSP ASIANPAINT AXISBANK BAJAJ-AUTO BAJFINANCE BAJAJFINSV BEL BHARTIARTL CIPLA COALINDIA DRREDDY EICHERMOT ETERNAL GRASIM HCLTECH HDFCBANK HDFCLIFE HINDALCO HINDUNILVR ICICIBANK INDUSINDBK INFY ITC JIOFIN JSWSTEEL KOTAKBANK LT M&M MARUTI NESTLEIND NTPC ONGC POWERGRID RELIANCE SBIN SHRIRAMFIN SUNPHARMA TATACONSUM TATAMOTORS TATASTEEL TCS TECHM TITAN TRENT ULTRACEMCO WIPRO BAJAJHLDNG TMPV TATAMOTORS'''.split()
SYMBOLS = list(dict.fromkeys(SYMBOLS))
# Note: this is a configurable watchlist, NOT a guaranteed current official index list.
STATE = Path('NIFTY50_EQUITY_PAPER_STATE.json')
TRADES = Path('NIFTY50_EQUITY_PAPER_TRADES.csv')
EVENTS = Path('NIFTY50_EQUITY_PAPER_EVENTS.csv')
SIGNALS = Path('NIFTY50_EQUITY_SIGNALS.csv')
LOCK = Path('NIFTY50_EQUITY.lock')
START_EQUITY = 100000.0
RISK_FRACTION = 0.0025
MAX_OPEN = 5
MAX_PER_SYMBOL_DAY = 1
SLIPPAGE = 0.0002
LEVERAGE = 1.0              # 1.0 = pure cash; MIS leverage would be higher (e.g. 5.0) but riskier
MAX_DAILY_LOSS = 0.02       # stop NEW entries once the day's realised loss exceeds 2% of equity
INDEX_TICKER = '^NSEI'
FRESH_MIN_AGE = 300         # seconds since bar start (bar must be complete)
# Max age of a bar that may still create an entry. If Yahoo lags more than this, no entries
# will ever fire; raise it via env var only if you accept staler entry prices.
FRESH_MAX_AGE = int(os.getenv('FRESH_MAX_AGE', '900'))


def alert(msg):
    print(msg)
    token = os.getenv('BOT_TOKEN') or os.getenv('TELEGRAM_BOT_TOKEN')
    chat = os.getenv('CHAT_ID') or os.getenv('TELEGRAM_CHAT_ID')
    if token and chat:
        try:
            r = requests.post(f'https://api.telegram.org/bot{token}/sendMessage', data={'chat_id': chat, 'text': msg}, timeout=12)
            r.raise_for_status()
        except requests.RequestException as e:
            print('Telegram delivery failed:', e)


def write_row(path, row):
    exists = path.exists() and path.stat().st_size > 0
    with path.open('a', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if not exists:
            w.writeheader()
        w.writerow(row)


def save_state(state):
    """Atomic write so a crash mid-write cannot corrupt the state file."""
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE)


def acquire_lock():
    """Prevents two overlapping cron runs from corrupting state. Stale locks (>10 min) are cleared."""
    for _ in range(2):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if systime.time() - LOCK.stat().st_mtime > 600:
                    LOCK.unlink()
                    continue
            except OSError:
                pass
            return False
    return False


def release_lock():
    try:
        LOCK.unlink()
    except OSError:
        pass


def load_seen_signals():
    if not SIGNALS.exists():
        return set()
    try:
        df = pd.read_csv(SIGNALS, usecols=['symbol', 'time', 'strategy'])
        return set(zip(df.symbol, df.time, df.strategy))
    except Exception:
        return set()


def get_data(symbol, period='10d', raw=False, retries=2):
    ticker = symbol if raw else symbol + '.NS'
    for attempt in range(retries + 1):
        try:
            d = yf.download(ticker, period=period, interval='5m', progress=False, auto_adjust=False, threads=False)
            if d is None or d.empty:
                raise ValueError('empty data')
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.get_level_values(0)
            d.columns = [str(x).lower() for x in d.columns]
            d = d[['open', 'high', 'low', 'close', 'volume']].dropna().sort_index()
            if d.index.tz is None:
                d.index = d.index.tz_localize('UTC').tz_convert(IST)
            else:
                d.index = d.index.tz_convert(IST)
            return d[(d.index.time >= time(9, 15)) & (d.index.time < time(15, 30))].copy()
        except Exception as e:
            if attempt == retries:
                print(symbol, 'data error:', str(e)[:140])
                return None
            systime.sleep(1.5 * (attempt + 1))


def indicators(d):
    d = d.copy()
    c = d.close
    change = c.diff()
    up = change.clip(lower=0).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    down = (-change.clip(upper=0)).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    d['rsi'] = 100 - 100/(1+up/down.replace(0, np.nan))
    d.loc[(down == 0) & (up > 0), 'rsi'] = 100
    d['ema9'] = c.ewm(span=9, adjust=False).mean()
    d['ema21'] = c.ewm(span=21, adjust=False).mean()
    macd = c.ewm(span=12, adjust=False).mean()-c.ewm(span=26, adjust=False).mean()
    d['macd_hist'] = macd-macd.ewm(span=9, adjust=False).mean()
    d['bbmid'] = c.rolling(20).mean()
    d['bbstd'] = c.rolling(20).std(ddof=0)
    pc = c.shift(1)
    tr = pd.concat([d.high-d.low, (d.high-pc).abs(), (d.low-pc).abs()], axis=1).max(axis=1)
    d['atr'] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    d['vol20'] = d.volume.shift(1).rolling(20).mean()
    d['orh'] = np.nan; d['orl'] = np.nan
    d['previous_close'] = np.nan; d['or_percent'] = np.nan
    # Weekly VWAP approximated from 5m Yahoo OHLCV; not exchange tick VWAP.
    # tz dropped before to_period to avoid pandas timezone warning.
    week = d.index.tz_localize(None).to_period('W-SUN').astype(str)
    typical = (d.high+d.low+d.close)/3
    d['week_vwap'] = (typical*d.volume).groupby(week).cumsum()/d.volume.groupby(week).cumsum().replace(0, np.nan)
    for _, day in d.groupby(d.index.date):
        opening = day[(day.index.time >= time(9, 15)) & (day.index.time < time(9, 45))]
        if len(opening) == 6:
            ix = day[day.index.time >= time(9, 45)].index
            d.loc[ix, 'orh'] = opening.high.max()
            d.loc[ix, 'orl'] = opening.low.min()
            d.loc[ix, 'or_percent'] = 100*(opening.high.max()-opening.low.min())/opening.iloc[0].open
    days = list(d.groupby(d.index.date))
    for j, (_, day) in enumerate(days):
        if j > 0:
            d.loc[day.index, 'previous_close'] = days[j-1][1].iloc[-1].close
    # Previous completed bars only for weekly high; avoids look-ahead.
    # Shift is done INSIDE each week so Monday's first bar does not inherit last week's high.
    d['week_high'] = d.high.groupby(week).cummax().groupby(week).shift(1)
    d['week_open'] = d.open.groupby(week).transform('first')
    return d


def index_regime(idx):
    """+1 = NIFTY index uptrend, -1 = downtrend, 0 = neutral (5m close vs EMA21 and EMA9/21 cross)."""
    if idx is None or len(idx) < 30:
        return None
    c = idx.close
    e9 = c.ewm(span=9, adjust=False).mean()
    e21 = c.ewm(span=21, adjust=False).mean()
    reg = pd.Series(0, index=idx.index)
    reg[(c > e21) & (e9 > e21)] = 1
    reg[(c < e21) & (e9 < e21)] = -1
    return reg


def regime_at(reg, ts):
    if reg is None:
        return 0
    try:
        v = reg.asof(ts)
        return int(v) if pd.notna(v) else 0
    except Exception:
        return 0


def signal(d, i, regime=0):
    if i < 35:
        return None
    x = d.iloc[i]; p = d.iloc[i-1]; ts = d.index[i]
    if not (time(9, 45) <= ts.time() < time(14, 20)):
        return None
    cols = ['rsi', 'ema9', 'ema21', 'macd_hist', 'bbmid', 'atr', 'vol20', 'week_vwap', 'orh', 'orl', 'previous_close']
    if not all(np.isfinite(x[k]) for k in cols) or x.vol20 <= 0 or x.atr <= 0:
        return None
    if x.volume < 1.5*x.vol20 or x.or_percent < 0.3:
        return None
    bullish = x.macd_hist > 0 and x.rsi > 57 and x.ema9 > x.ema21
    bearish = x.macd_hist < 0 and x.ema9 < x.ema21
    body = abs(x.close-x.open)
    rng = max(x.high-x.low, 0.00001)
    maru_buy = x.close > x.open and body/rng >= 0.70 and (x.high-x.close)/rng <= 0.20
    maru_sell = x.close < x.open and body/rng >= 0.65
    setup = None; side = 0
    # LONG 1: reclaim weekly VWAP, from below previous close and BB middle.
    if bullish and x.close > x.week_vwap and p.close <= p.week_vwap and p.close < x.previous_close and p.close < p.bbmid and x.close > x.orh:
        setup = 'WEEKLY_VWAP_RECLAIM'; side = 1
    # LONG 2: BB middle break, weekly opening/VWAP proximity.
    elif bullish and maru_buy and p.close <= p.bbmid and x.close > x.bbmid and x.close > x.orh and min(abs(x.low-x.week_vwap), abs(x.low-x.week_open)) <= 0.5*x.atr:
        setup = 'BB_MARUBOZU_LONG'; side = 1
    # SHORT 1: previous weekly high sweep + RSI overbought and MACD turns negative.
    elif bearish and maru_sell and p.rsi > 70 and p.high >= p.week_high and x.close < p.low and p.macd_hist >= 0:
        setup = 'WEEKLY_HIGH_EXHAUSTION'; side = -1
    # SHORT 2: 2 of last 3 bars reject BB middle; RSI near 50.
    elif bearish and maru_sell and 40 <= x.rsi <= 55 and x.close < x.bbmid and x.close < x.orl:
        recent = d.iloc[i-2:i+1]
        touches = ((recent.high >= recent.bbmid*0.998) & (recent.close < recent.bbmid)).sum()
        if touches >= 2:
            setup = 'BB_MIDDLE_REJECTION'; side = -1
    if not setup:
        return None
    # Market regime filter: do not buy against a falling NIFTY, do not short against a rising one.
    if (side == 1 and regime < 0) or (side == -1 and regime > 0):
        return None
    # ATR/structure SL, risk must be positive and reasonably sized.
    if side == 1:
        stop = min(x.low, x.close-1.2*x.atr, x.week_vwap-0.1*x.atr)
    else:
        stop = max(x.high, x.close+1.2*x.atr, x.week_vwap+0.1*x.atr)
    risk = side*(x.close-stop)
    if risk <= 0 or not (0.001 <= risk/x.close <= 0.025):
        return None
    return {'strategy': setup, 'side': side, 'entry': float(x.close), 'stop': float(stop), 'risk': float(risk), 'bar': ts.isoformat()}


def trade_costs(entry, exit_price, qty, side):
    """Approximate Zerodha-style intraday equity charges (brokerage, STT, exchange, SEBI, GST, stamp)."""
    buy = (entry if side == 1 else exit_price)*qty
    sell = (exit_price if side == 1 else entry)*qty
    brokerage = min(20.0, 0.0003*buy) + min(20.0, 0.0003*sell)
    stt = 0.00025*sell
    txn = 0.0000297*(buy+sell)
    sebi = 0.000001*(buy+sell)
    gst = 0.18*(brokerage+txn+sebi)
    stamp = 0.00003*buy
    return brokerage+stt+txn+sebi+gst+stamp


def manage(pos, x, ts):
    side = pos['side']; entry = pos['entry']; risk = pos['risk']; stop = pos['stop']
    hit = x.low <= stop if side == 1 else x.high >= stop
    if hit:
        price = min(float(x.open), stop) if side == 1 else max(float(x.open), stop)
        return (price*(1-side*SLIPPAGE), 'STOP')
    target = entry+side*4*risk
    if (x.high >= target if side == 1 else x.low <= target):
        return (target*(1-side*SLIPPAGE), 'TARGET_4R')
    if ts.time() >= time(15, 10):
        return (float(x.close)*(1-side*SLIPPAGE), 'EOD')
    # Stop modifications become effective only on the NEXT candle.
    if (x.high >= entry+side*3*risk if side == 1 else x.low <= entry+side*3*risk):
        pos['stop'] = entry+side*3*risk; pos['stage'] = 3
    elif pos['stage'] < 2 and (x.high >= entry+side*2*risk if side == 1 else x.low <= entry+side*2*risk):
        pos['stop'] = entry+side*2*risk; pos['stage'] = 2
    return None


def report():
    if not TRADES.exists() or TRADES.stat().st_size == 0:
        print('No closed paper trades yet.'); return
    t = pd.read_csv(TRADES)
    t['net_inr'] = pd.to_numeric(t['net_inr'], errors='coerce').fillna(0.0)
    n = len(t)
    wins = t[t.net_inr > 0]; losses = t[t.net_inr <= 0]
    gross_win = wins.net_inr.sum(); gross_loss = -losses.net_inr.sum()
    pf = gross_win/gross_loss if gross_loss > 0 else float('inf')
    eq = START_EQUITY+t.net_inr.cumsum()
    peak = eq.cummax().clip(lower=START_EQUITY)
    dd = (eq-peak)
    print(f'Trades: {n} | Win rate: {100*len(wins)/n:.1f}% | Net: INR {t.net_inr.sum():.2f}')
    print(f'Avg win: {wins.net_inr.mean() if len(wins) else 0:.2f} | Avg loss: {losses.net_inr.mean() if len(losses) else 0:.2f} | Expectancy/trade: {t.net_inr.mean():.2f}')
    print(f'Profit factor: {pf:.2f} | Max drawdown: INR {dd.min():.2f} ({100*(dd/peak).min():.2f}%)')
    for col in ('strategy', 'side', 'reason'):
        if col in t:
            g = t.groupby(col).net_inr.agg(trades='count', net='sum', avg='mean', win_pct=lambda s: 100*(s > 0).mean()).round(2)
            print(f'\nBy {col}:\n{g.to_string()}')
    print('\nNote: small samples (<100 trades) say very little. Paper results use delayed Yahoo data and estimated fills.')


def run(mode, use_index_filter=True):
    now = datetime.now(IST)
    today = now.date().isoformat()
    state = None
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print('State file unreadable; NOT overwriting it. Fix or restore it manually:', e); return
    first_run = state is None
    if state is None:
        state = {'equity': START_EQUITY, 'positions': {}, 'last': {}, 'traded': {}, 'day': ''}
    state.setdefault('positions', {}); state.setdefault('last', {}); state.setdefault('traded', {}); state.setdefault('equity', START_EQUITY)
    # traded: {"YYYY-MM-DD|SYMBOL": count}; prune old days and legacy-format entries.
    state['traded'] = {k: v for k, v in state['traded'].items() if isinstance(v, int) and k.startswith(today)}
    dp = state.setdefault('day_pnl', {'day': today, 'pnl': 0.0})
    if dp.get('day') != today:
        state['day_pnl'] = dp = {'day': today, 'pnl': 0.0}
    if mode == 'paper' and (now.weekday() > 4 or not time(9, 45) <= now.time() < time(15, 45)):
        print('Outside NSE paper window'); return
    cutoff = now-timedelta(minutes=5, seconds=30)   # only fully completed 5m bars are used (both modes)
    if mode == 'paper' and first_run:
        # Initial observation only; do not enter based on earlier candles.
        for symbol in SYMBOLS:
            d = get_data(symbol, '3d')
            if d is not None and not d.empty:
                past = d[d.index <= cutoff]
                if not past.empty:
                    state['last'][symbol] = past.index[-1].isoformat()
        save_state(state); print('Initialized state; wait for next completed candles'); return
    reg = None
    if use_index_filter:
        idx = get_data(INDEX_TICKER, '10d', raw=True)
        if idx is not None:
            reg = index_regime(idx[idx.index <= cutoff])
        if reg is None:
            print('Index filter unavailable this run; continuing without it')
    seen = load_seen_signals()
    scanned = 0; found = 0
    for symbol in SYMBOLS:
        d = get_data(symbol, '10d')
        if d is None or len(d) < 45:
            continue
        d = indicators(d); scanned += 1
        d = d[d.index <= cutoff]
        if d.empty:
            continue
        if mode == 'scan':
            indices = [len(d)-1]
        else:
            last = state['last'].get(symbol, '')
            indices = [i for i, t in enumerate(d.index) if t.isoformat() > last]
        for i in indices:
            ts = d.index[i]; x = d.iloc[i]; day = ts.date().isoformat()
            if symbol in state['positions']:
                pos = state['positions'][symbol]
                # Avoid falsely carrying overnight positions as if live.
                if day != pos['day']:
                    prev = d.iloc[i-1] if i else x
                    px = float(prev.close)*(1-pos['side']*SLIPPAGE)
                    result = (px, 'MISSED_EOD_ESTIMATE')
                else:
                    old_stop = pos['stop']
                    result = manage(pos, x, ts)
                    if result is None and pos['stop'] != old_stop:
                        write_row(EVENTS, {'symbol': symbol, 'time': ts.isoformat(), 'event': f'STOP_MOVE_{pos["stage"]}R', 'strategy': pos['strategy'], 'price': '', 'stop': round(pos['stop'], 2)})
                if result:
                    pnl = (result[0]-pos['entry'])*pos['side']*pos['qty']
                    # Approximate round-trip costs, not actual Zerodha ledger.
                    net = round(pnl-trade_costs(pos['entry'], result[0], pos['qty'], pos['side']), 2)
                    state['equity'] = round(state['equity']+net, 2)
                    if day == state['day_pnl']['day']:
                        state['day_pnl']['pnl'] = round(state['day_pnl']['pnl']+net, 2)
                    write_row(TRADES, {'symbol': symbol, 'strategy': pos['strategy'], 'side': 'BUY' if pos['side'] == 1 else 'SELL', 'entry_time': pos['time'], 'exit_time': ts.isoformat(), 'entry': round(pos['entry'], 2), 'exit': round(result[0], 2), 'qty': pos['qty'], 'net_inr': net, 'reason': result[1]})
                    write_row(EVENTS, {'symbol': symbol, 'time': ts.isoformat(), 'event': 'EXIT_'+result[1], 'strategy': pos['strategy'], 'price': round(result[0], 2), 'stop': ''})
                    alert(f'NIFTY50 EQUITY PAPER EXIT {symbol}\n{result[1]} | Net approx INR {net}\nDelayed Yahoo estimate, not broker fill')
                    del state['positions'][symbol]
            # Only recent completed candles create new paper entries.
            age = (now-ts.to_pydatetime()).total_seconds()
            fresh = (ts.date() == now.date() and FRESH_MIN_AGE <= age <= FRESH_MAX_AGE)
            if mode == 'scan' or (fresh and ts.time() < time(14, 20)):
                s = signal(d, i, regime_at(reg, ts))
                if s:
                    key = (symbol, s['bar'], s['strategy'])
                    if key not in seen:
                        seen.add(key)
                        write_row(SIGNALS, {'symbol': symbol, 'time': s['bar'], 'strategy': s['strategy'], 'side': 'BUY' if s['side'] == 1 else 'SELL', 'price': round(s['entry'], 2), 'stop': round(s['stop'], 2), 'target4': round(s['entry']+s['side']*4*s['risk'], 2)})
                    found += 1
                    if mode == 'scan':
                        print(f'SIGNAL {symbol} {s["strategy"]} {"BUY" if s["side"] == 1 else "SELL"} @ {s["entry"]:.2f} SL {s["stop"]:.2f}')
                    tkey = f'{day}|{symbol}'
                    loss_limit_hit = state['day_pnl']['pnl'] <= -MAX_DAILY_LOSS*state['equity']
                    if (mode == 'paper' and symbol not in state['positions'] and state['traded'].get(tkey, 0) < MAX_PER_SYMBOL_DAY
                            and len(state['positions']) < MAX_OPEN and not loss_limit_hit):
                        entry = s['entry']*(1+s['side']*SLIPPAGE)
                        risk = s['side']*(entry-s['stop'])
                        deployed = sum(p_['entry']*p_['qty'] for p_ in state['positions'].values())
                        available = max(state['equity']*LEVERAGE-deployed, 0.0)
                        qty = min(int(state['equity']*RISK_FRACTION/risk), int(available/entry)) if risk > 0 else 0
                        if qty > 0:
                            state['positions'][symbol] = {'strategy': s['strategy'], 'side': s['side'], 'entry': entry, 'stop': s['stop'], 'risk': risk, 'qty': qty, 'stage': 0, 'day': day, 'time': ts.isoformat()}
                            state['traded'][tkey] = state['traded'].get(tkey, 0)+1
                            write_row(EVENTS, {'symbol': symbol, 'time': ts.isoformat(), 'event': 'ENTRY', 'strategy': s['strategy'], 'price': round(entry, 2), 'stop': round(s['stop'], 2)})
                            alert(f'NIFTY50 CASH PAPER {symbol}\n{s["strategy"]} {"BUY" if s["side"] == 1 else "SELL"} qty {qty}\nIndicative {entry:.2f} SL {s["stop"]:.2f}\n2R lock -> 3R lock -> 4R exit\nYahoo delayed, no real order')
                    elif mode == 'paper' and loss_limit_hit:
                        print(f'Daily loss limit reached (INR {state["day_pnl"]["pnl"]}); skipping new entry in {symbol}')
            if mode == 'paper':
                state['last'][symbol] = ts.isoformat()
        if mode == 'paper':
            save_state(state)
    print(f'Cash equity symbols scanned: {scanned}/{len(SYMBOLS)} | matching signals: {found} | open paper positions: {len(state["positions"])} | indicative equity: INR {state["equity"]:.2f} | today P&L: INR {state["day_pnl"]["pnl"]:.2f}')
    if mode == 'paper':
        save_state(state)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--mode', choices=['paper', 'scan', 'report'], default='paper')
    p.add_argument('--no-index-filter', action='store_true')
    args = p.parse_args()
    if args.mode == 'report':
        report()
    else:
        if args.mode == 'paper' and not acquire_lock():
            print('Another paper run is in progress; skipping this one')
        else:
            try:
                run(args.mode, use_index_filter=not args.no_index_filter)
            finally:
                if args.mode == 'paper':
                    release_lock()
