#!/usr/bin/env python3
"""
SPY RSI(2) mean-reversion PAPER scanner (no real orders, no broker connection).

Rules (identical to the backtest in sp500_strategies_backtest.py, best variant on out-of-sample data):
  * Signal  : at a day's CLOSE, RSI(2) < 15 and close > SMA200  -> plan to buy at the NEXT session open.
  * Entry   : next session open (recorded as paper entry).
  * Exit    : at a close where RSI(2) > 65, or after 10 sessions (checked on every close, incl. entry day).
  * Cost    : 0.02% per round trip (same as the backtest).

Designed to run once per US trading day after the close (GitHub Actions, 21:30 UTC). It processes every
completed daily bar it has not seen yet, so a delayed or skipped run catches up on the next one.
State: sp500_rsi2_state.json   Trades: sp500_rsi2_trades.csv   Telegram alerts (optional secrets).
Note: the backtest exits at the same day's close; in practice that needs a market-on-close order placed
before the close, so real fills can differ from paper fills.
"""
import argparse
import csv
import json
import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from sp500_strategies_backtest import COST, add_indicators, fetch_data

TICKER = "SPY"
ENTRY_RSI = 15.0
EXIT_RSI = 65.0
MAX_HOLD = 10
TREND_FILTER = True
STATE_FILE = "sp500_rsi2_state.json"
TRADES_FILE = "sp500_rsi2_trades.csv"
HISTORY_START = "2015-01-01"
TRADE_FIELDS = ["entry_date", "exit_date", "entry", "exit", "ret_pct", "hold_days", "reason"]


def send_telegram(text):
    token = os.getenv("TG_TOKEN") or os.getenv("BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID") or os.getenv("CHAT_ID")
    if not token or not chat_id:
        print("[telegram] credentials missing; skipped:", text.replace("\n", " | "))
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat_id, "text": text}, timeout=20)
        r.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[telegram] send failed: {exc}")
        return False


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    return {"position": None, "pending_signal": None, "last_processed": None}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def append_trade(trade):
    new = not os.path.exists(TRADES_FILE)
    with open(TRADES_FILE, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=TRADE_FIELDS)
        if new:
            w.writeheader()
        w.writerow(trade)


def is_complete(ts, now):
    """A daily bar is final if its date is before today (UTC), or it is after 21:15 UTC (US close, EST or EDT)."""
    return ts.date() < now.date() or (now.hour * 60 + now.minute) >= 21 * 60 + 15


def has_signal(row):
    return row["rsi2"] < ENTRY_RSI and (not TREND_FILTER or row["close"] > row["sma200"])


def process(df, state, now):
    """Advance the paper state over all completed bars not yet processed.

    Returns (events, closed_trades). Pure: it does not write files or send messages.
    """
    events, closed = [], []
    bars = df[[is_complete(ts, now) for ts in df.index]]
    if bars.empty:
        return events, closed
    last = state.get("last_processed")
    if last is None:
        # First run: no history replay. Start from the latest completed bar and honour its signal.
        ts, row = bars.index[-1], bars.iloc[-1]
        state["last_processed"] = ts.strftime("%Y-%m-%d")
        if has_signal(row):
            state["pending_signal"] = state["last_processed"]
            events.append(("signal", f"SPY RSI(2) PAPER SIGNAL ({state['last_processed']} close)\n"
                           f"RSI(2)={row['rsi2']:.1f} (< {ENTRY_RSI:.0f}), close {row['close']:.2f} > SMA200 {row['sma200']:.2f}\n"
                           "Plan: paper BUY at the next session open."))
        return events, closed

    for ts, row in bars[bars.index > pd.Timestamp(last)].iterrows():
        d = ts.strftime("%Y-%m-%d")
        pos = state.get("position")
        if pos is None and state.get("pending_signal"):
            pos = {"entry_date": d, "entry": float(row["open"]), "hold_days": 0}
            state["pending_signal"] = None
            events.append(("entry", f"SPY RSI(2) PAPER ENTRY\n{d} open: {pos['entry']:.2f}"))
        if pos is not None:
            pos["hold_days"] += 1
            reason = "RSI_EXIT" if row["rsi2"] > EXIT_RSI else ("MAX_HOLD" if pos["hold_days"] >= MAX_HOLD else None)
            if reason:
                exit_px = float(row["close"])
                ret = exit_px / pos["entry"] - 1 - COST
                trade = {"entry_date": pos["entry_date"], "exit_date": d, "entry": round(pos["entry"], 4),
                         "exit": round(exit_px, 4), "ret_pct": round(100 * ret, 4),
                         "hold_days": pos["hold_days"], "reason": reason}
                closed.append(trade)
                events.append(("exit", f"SPY RSI(2) PAPER EXIT ({reason})\n{pos['entry_date']} -> {d}\n"
                               f"Entry {pos['entry']:.2f} | Exit {exit_px:.2f}\nNet return: {100 * ret:.2f}% in {pos['hold_days']} days"))
                pos = None
        state["position"] = pos
        if pos is None and has_signal(row):
            state["pending_signal"] = d
            events.append(("signal", f"SPY RSI(2) PAPER SIGNAL ({d} close)\n"
                           f"RSI(2)={row['rsi2']:.1f} (< {ENTRY_RSI:.0f}), close {row['close']:.2f} > SMA200 {row['sma200']:.2f}\n"
                           "Plan: paper BUY at the next session open."))
        state["last_processed"] = d
    return events, closed


def main():
    ap = argparse.ArgumentParser(description="SPY RSI(2) paper scanner (no real orders)")
    ap.add_argument("--telegram-test", action="store_true", help="only send a Telegram test message")
    args = ap.parse_args()
    if args.telegram_test:
        ok = send_telegram("SPY RSI(2) paper scanner: Telegram delivery test successful. No real orders are placed.")
        raise SystemExit(0 if ok else "Telegram test failed: check BOT_TOKEN/CHAT_ID (or TG_TOKEN/TG_CHAT_ID) secrets.")

    now = datetime.now(timezone.utc)
    df = add_indicators(fetch_data(TICKER, HISTORY_START))
    state = load_state()
    events, closed = process(df, state, now)
    for trade in closed:
        append_trade(trade)
    save_state(state)
    for _, text in events:
        print(text.replace("\n", " | "))
        send_telegram(text)
    last = df[[is_complete(ts, now) for ts in df.index]].iloc[-1]
    print(f"[{now:%Y-%m-%d %H:%M} UTC] last complete bar {last.name.date()} | close {last['close']:.2f} | "
          f"RSI(2) {last['rsi2']:.1f} | SMA200 {last['sma200']:.2f} | position {state['position']} | "
          f"pending {state['pending_signal']} | new events {len(events)}")


if __name__ == "__main__":
    main()
