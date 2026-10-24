#!/usr/bin/env python3
"""Forward-only CoinDCX XAU/PAXG paper tracker. No exchange orders."""
import csv
import json
import os
import time
from pathlib import Path

import pandas as pd
import requests
from gold_combo_strategy import Cfg, build_signals, fetch_coindcx

NAME = 'GOLD_COMBO_PAPER'
STATE = Path(NAME + '_state.json')
TRADES = Path(NAME + '_trades.csv')
SIGNALS = Path(NAME + '_signals.csv')
COST = (0.05 + 0.01) / 100  # each side, fee plus modeled slippage
SYMBOLS = ('XAU', 'PAXG')


def notify(message):
    token, chat = os.getenv('BOT_TOKEN'), os.getenv('CHAT_ID')
    if not token or not chat:
        print('Telegram not configured; message:', message, flush=True)
        return
    try:
        response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                                 json={'chat_id': chat, 'text': message}, timeout=15)
        response.raise_for_status()
    except requests.RequestException as exc:
        print('Telegram error:', exc, flush=True)


def append(path, row):
    first = not path.exists() or path.stat().st_size == 0
    with path.open('a', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        if first:
            writer.writeheader()
        writer.writerow(row)


def load():
    if STATE.exists():
        state = json.loads(STATE.read_text(encoding='utf-8'))
        if not isinstance(state, dict):
            raise RuntimeError('Invalid state')
        return state
    return {s: {'last_bar': None, 'position': None, 'closed_count': 0,
                'wins': 0, 'net_pnl_usdt': 0.0, 'net_return_sum_pct': 0.0,
                'equity': 1000.0} for s in SYMBOLS}


def save(state):
    temp = STATE.with_suffix('.tmp')
    temp.write_text(json.dumps(state, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(STATE)


def settle(symbol, record, candle, stamp):
    pos = record['position']
    if not pos:
        return
    if stamp <= pd.Timestamp(pos['entry_bar']):
        return  # no same-bar fill/exit
    side = pos['side']
    sign = 1 if side == 'LONG' else -1
    stop, tp1, tp2 = pos['sl'], pos['tp1'], pos['tp2']
    low, high = float(candle['low']), float(candle['high'])
    stop_hit = low <= stop if sign == 1 else high >= stop
    tp1_hit = high >= tp1 if sign == 1 else low <= tp1
    tp2_hit = high >= tp2 if sign == 1 else low <= tp2
    fills = []
    # Conservative OHLC ordering: active stop has priority.
    if stop_hit:
        fills.append((pos['qty_left'], stop, 'BE/SL' if pos['tp1_hit'] else 'SL'))
    else:
        if not pos['tp1_hit'] and tp1_hit:
            half = pos['qty'] * 0.5
            fills.append((half, tp1, 'TP1'))
            pos['tp1_hit'] = True
            pos['sl'] = pos['entry']  # effective NEXT candle only
        if tp2_hit:
            fills.append((pos['qty_left'], tp2, 'TP2'))
    closed = False
    for qty, price, reason in fills:
        if qty <= 0:
            continue
        net = (price - pos['entry']) * sign * qty - (price + pos['entry']) * qty * COST
        pos['pnl'] += net
        pos['qty_left'] -= qty
        if reason != 'TP1':
            closed = True
            pos['reason'] = reason
    pos['bars'] += 1
    if not closed and pos['bars'] >= 288:
        price = float(candle['close'])
        qty = pos['qty_left']
        pos['pnl'] += (price - pos['entry']) * sign * qty - (price + pos['entry']) * qty * COST
        pos['qty_left'] = 0
        pos['reason'] = 'TIME'
        closed = True
    if closed:
        net = pos['pnl']
        record['closed_count'] += 1
        record['wins'] += int(net > 0)
        record['net_pnl_usdt'] += net
        record['equity'] += net
        pct = net / pos['equity_at_entry'] * 100
        record['net_return_sum_pct'] += pct
        append(TRADES, {'symbol': symbol, 'side': side, 'entry_time': pos['entry_time'],
                        'exit_time': stamp.isoformat(), 'entry': pos['entry'],
                        'initial_sl': pos['initial_sl'], 'tp1': tp1, 'tp2': tp2,
                        'result': pos['reason'], 'net_pnl_usdt': round(net, 6),
                        'return_pct_of_equity': round(pct, 5)})
        notify(f'GOLD PAPER EXIT | {symbol} {side}\n{pos["reason"]}\nNet P/L {net:+.2f} USDT\nClosed {record["closed_count"]} | Wins {record["wins"]}\nNO REAL ORDER')
        record['position'] = None


def enter(symbol, record, signal, stamp):
    cfg = Cfg(symbol=symbol)
    side = 'LONG' if bool(signal['long']) else 'SHORT'
    sign = 1 if side == 'LONG' else -1
    entry = float(signal['close'])  # proxy fill at last completed candle close
    atr = float(signal['atr'])
    anchor = float(signal['sweep_low'] if sign == 1 else signal['sweep_high'])
    raw_stop = anchor - sign * cfg.sl_buffer_atr * atr
    dist = min(max(abs(entry - raw_stop), cfg.min_sl_atr * atr), cfg.max_sl_atr * atr)
    if not (entry > 0 and dist > 0 and record['equity'] > 0):
        return
    sl = entry - sign * dist
    qty = min((record['equity'] * cfg.risk_pct / 100) / dist,
              record['equity'] * cfg.max_leverage / entry)
    if qty <= 0:
        return
    tp1, tp2 = entry + sign * 2 * dist, entry + sign * 3 * dist
    record['position'] = {'side': side, 'entry': entry, 'sl': sl, 'initial_sl': sl,
                          'tp1': tp1, 'tp2': tp2, 'qty': qty, 'qty_left': qty,
                          'entry_bar': stamp.isoformat(), 'entry_time': stamp.isoformat(),
                          'tp1_hit': False, 'pnl': 0.0, 'bars': 0,
                          'equity_at_entry': record['equity']}
    append(SIGNALS, {'symbol': symbol, 'side': side, 'entry_time': stamp.isoformat(),
                     'entry_proxy': entry, 'sl': sl, 'tp1': tp1, 'tp2': tp2,
                     'score': int(signal['score_l'] if sign == 1 else signal['score_s'])})
    notify(f'GOLD PAPER ENTRY | {symbol} {side}\nEntry approx {entry:.3f}\nSL {sl:.3f}\nTP1 {tp1:.3f} | TP2 {tp2:.3f}\nNO REAL ORDER')


def main():
    state = load()
    for symbol in SYMBOLS:
        state.setdefault(symbol, {'last_bar': None, 'position': None, 'closed_count': 0,
                                  'wins': 0, 'net_pnl_usdt': 0.0,
                                  'net_return_sum_pct': 0.0, 'equity': 1000.0})
        record = state[symbol]
        try:
            df = fetch_coindcx(symbol, days=30)
            sig = build_signals(df, Cfg(symbol=symbol))
            if sig.empty:
                raise RuntimeError('No completed candles')
            latest = sig.index[-1]
            if record['last_bar'] is None:
                record['last_bar'] = latest.isoformat()
                print(f'{symbol}: initialized at {latest}; no retroactive trades', flush=True)
                continue
            previous = pd.Timestamp(record['last_bar'])
            new_bars = sig[sig.index > previous]
            if new_bars.empty:
                print(f'{symbol}: no new closed bars', flush=True)
                continue
            for stamp, candle in new_bars.iterrows():
                settle(symbol, record, candle, stamp)
                # Only take a fresh signal on the newest bar, never backfill missed entries.
                if stamp == latest and record['position'] is None and (bool(candle['long']) or bool(candle['short'])):
                    age = (pd.Timestamp.now(tz='UTC') - (stamp + pd.Timedelta(minutes=5))).total_seconds()
                    if 0 <= age <= 360:
                        enter(symbol, record, candle, stamp)
                record['last_bar'] = stamp.isoformat()
            print(f'{symbol}: open={int(record["position"] is not None)} closed={record["closed_count"]} wins={record["wins"]} net={record["net_pnl_usdt"]:.2f} USDT', flush=True)
        finally:
            save(state)
    print('GOLD PAPER COMPLETE — no real orders', flush=True)


if __name__ == '__main__':
    main()
