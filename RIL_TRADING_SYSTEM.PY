
from __future__ import annotations

import argparse
import csv
import json
import os
import time as time_module
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf

# ============================================================
# RELIANCE NSE 5-MINUTE PAPER TRADING + BACKTEST
# NO REAL ORDERS ARE PLACED
# ============================================================

SYMBOL = "RELIANCE.NS"
IST = ZoneInfo("Asia/Kolkata")
CAPITAL = 100000.0
RISK_PCT = 0.01
DAILY_LOSS_LIMIT = 0.025
MAX_TRADES_DAY = 3
MAX_STOP_PCT = 0.01
MIN_STOP_PCT = 0.0015
SLIPPAGE = 0.0002
VOLUME_MULT = 1.0
ATR_MULT = 1.5

STATE_FILE = "RELIANCE_PAPER_STATE.json"
PAPER_FILE = "RELIANCE_PAPER_TRADES.csv"
EVENT_FILE = "RELIANCE_PAPER_EVENTS.csv"

TRADE_FIELDS = [
    "strategy", "date", "side", "entry_time", "exit_time",
    "entry", "exit", "initial_stop", "final_stop", "qty",
    "gross", "costs", "net", "r_multiple", "exit_reason"
]

EVENT_FIELDS = [
    "timestamp", "event", "side", "price", "qty",
    "stop", "details"
]


def now_ist():
    return datetime.now(IST)


def money(x):
    return round(float(x), 2)


def tick(x):
    return round(round(float(x) / 0.05) * 0.05, 2)


def append_csv(filename, fields, record):
    path = Path(filename)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if new:
            writer.writeheader()
        writer.writerow({
            key: record.get(key, "") for key in fields
        })


def save_json(filename, data):
    temp = filename + ".tmp"
    Path(temp).write_text(
        json.dumps(data, indent=2, default=str),
        encoding="utf-8"
    )
    os.replace(temp, filename)


def telegram(message):
    token = os.getenv("BOT_TOKEN", "")
    chat = os.getenv("CHAT_ID", "")

    if not token or not chat:
        print("TELEGRAM NOT CONFIGURED:", message)
        return

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": message},
            timeout=15
        )
        response.raise_for_status()
    except Exception as exc:
        print("Telegram error:", exc)


def event(kind, side="", price=0, qty=0, stop=0, details=""):
    record = {
        "timestamp": now_ist().isoformat(),
        "event": kind,
        "side": side,
        "price": price,
        "qty": qty,
        "stop": stop,
        "details": details
    }
    append_csv(EVENT_FILE, EVENT_FIELDS, record)
    print("EVENT:", record)


def costs(entry, exit_price, qty, side):
    buy = entry if side == "BUY" else exit_price
    sell = exit_price if side == "BUY" else entry

    turnover = (buy + sell) * qty

    brokerage = (
        min(buy * qty * 0.0003, 20)
        + min(sell * qty * 0.0003, 20)
    )

    stt = sell * qty * 0.00025
    exchange = turnover * 0.0000297
    sebi = turnover * 0.000001
    stamp = buy * qty * 0.00003
    gst = (brokerage + exchange + sebi) * 0.18

    return money(
        brokerage + stt + exchange + sebi + stamp + gst
    )


def download_data(days=7):
    period = f"{min(int(days), 59)}d"

    df = yf.download(
        SYMBOL,
        period=period,
        interval="5m",
        auto_adjust=False,
        progress=False,
        threads=False
    )

    if df.empty:
        raise RuntimeError("Yahoo Finance returned no data")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df.columns = [str(c).lower() for c in df.columns]

    df = df[["open", "high", "low", "close", "volume"]]
    df = df.dropna().copy()

    if df.index.tz is None:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_convert(IST)

    df = df[
        (df.index.time >= time(9, 15))
        & (df.index.time < time(15, 30))
    ]

    df = df[~df.index.duplicated(keep="last")]
    return df.sort_index()


def add_indicators(df):
    df = df.copy()

    df["ema9"] = df.close.ewm(
        span=9, adjust=False
    ).mean()

    df["ema21"] = df.close.ewm(
        span=21, adjust=False
    ).mean()

    prev_close = df.close.shift(1)

    tr = pd.concat([
        df.high - df.low,
        (df.high - prev_close).abs(),
        (df.low - prev_close).abs()
    ], axis=1).max(axis=1)

    df["atr"] = tr.ewm(
        alpha=1 / 14, adjust=False
    ).mean()

    df["vol_ma"] = df.volume.rolling(
        20, min_periods=10
    ).mean()

    dates = pd.Series(
        df.index.date, index=df.index
    )

    typical = (df.high + df.low + df.close) / 3
    cumulative_pv = (typical * df.volume).groupby(
        dates
    ).cumsum()

    cumulative_vol = df.volume.groupby(
        dates
    ).cumsum()

    df["vwap"] = cumulative_pv / cumulative_vol.replace(
        0, np.nan
    )

    df["orb_high"] = np.nan
    df["orb_low"] = np.nan

    for day, part in df.groupby(df.index.date):
        opening = part[
            (part.index.time >= time(9, 15))
            & (part.index.time < time(9, 30))
        ]

        if len(opening) < 3:
            continue

        df.loc[part.index, "orb_high"] = opening.high.max()
        df.loc[part.index, "orb_low"] = opening.low.min()

    return df


def signal(df, i, strategy="orb"):
    if i < 22:
        return None

    row = df.iloc[i]
    prev = df.iloc[i - 1]
    ts = df.index[i]
    tm = ts.time()

    if tm < time(9, 30) or tm >= time(14, 30):
        return None

    if time(12, 0) <= tm < time(13, 30):
        return None

    if not np.isfinite(row.atr):
        return None

    if not np.isfinite(row.vol_ma):
        return None

    if row.volume < row.vol_ma * VOLUME_MULT:
        return None

    if strategy == "orb":
        if not np.isfinite(row.orb_high):
            return None

        long_ok = (
            prev.close <= row.orb_high
            and row.close > row.orb_high
            and row.close > row.vwap
            and row.ema9 > row.ema21
        )

        short_ok = (
            prev.close >= row.orb_low
            and row.close < row.orb_low
            and row.close < row.vwap
            and row.ema9 < row.ema21
        )

    else:
        long_ok = (
            prev.ema9 <= prev.ema21
            and row.ema9 > row.ema21
            and row.close > row.vwap
        )

        short_ok = (
            prev.ema9 >= prev.ema21
            and row.ema9 < row.ema21
            and row.close < row.vwap
        )

    if not long_ok and not short_ok:
        return None

    side = "BUY" if long_ok else "SELL"
    return {
        "side": side,
        "atr": float(row.atr),
        "orb_high": float(row.orb_high)
            if np.isfinite(row.orb_high) else None,
        "orb_low": float(row.orb_low)
            if np.isfinite(row.orb_low) else None,
        "strategy": strategy.upper()
    }


def new_position(sig, price, timestamp, equity):
    side = sig["side"]
    entry = price * (
        1 + SLIPPAGE if side == "BUY" else 1 - SLIPPAGE
    )
    entry = tick(entry)

    atr_distance = sig["atr"] * ATR_MULT

    if sig["strategy"] == "ORB":
        if side == "BUY":
            orb_stop = sig["orb_low"]
            distance = min(
                entry - orb_stop, atr_distance
            )
        else:
            orb_stop = sig["orb_high"]
            distance = min(
                orb_stop - entry, atr_distance
            )
    else:
        distance = atr_distance

    if distance <= 0:
        return None

    stop_pct = distance / entry

    if not MIN_STOP_PCT <= stop_pct <= MAX_STOP_PCT:
        return None

    stop = tick(
        entry - distance if side == "BUY"
        else entry + distance
    )

    risk_per_share = abs(entry - stop)

    if risk_per_share <= 0:
        return None

    risk_qty = int(
        equity * RISK_PCT / risk_per_share
    )

    cash_qty = int(equity / entry)
    qty = min(risk_qty, cash_qty)

    if qty < 1:
        return None

    return {
        "strategy": sig["strategy"],
        "side": side,
        "entry_time": timestamp.isoformat(),
        "entry": entry,
        "initial_stop": stop,
        "stop": stop,
        "qty": qty,
        "risk": risk_per_share,
        "stage": 0,
        "last_bar": "",
    }


def exit_position(pos, exit_price, timestamp, reason):
    side = pos["side"]
    direction = 1 if side == "BUY" else -1

    entry = pos["entry"]
    qty = pos["qty"]

    gross = (
        exit_price - entry
    ) * direction * qty

    charges = costs(entry, exit_price, qty, side)
    net = gross - charges

    record = {
        "strategy": pos["strategy"],
        "date": timestamp.date().isoformat(),
        "side": side,
        "entry_time": pos["entry_time"],
        "exit_time": timestamp.isoformat(),
        "entry": entry,
        "exit": tick(exit_price),
        "initial_stop": pos["initial_stop"],
        "final_stop": pos["stop"],
        "qty": qty,
        "gross": money(gross),
        "costs": charges,
        "net": money(net),
        "r_multiple": round(
            net / (pos["risk"] * qty), 4
        ),
        "exit_reason": reason
    }

    return record


def manage_position(pos, bar, timestamp):
    side = pos["side"]
    direction = 1 if side == "BUY" else -1

    entry = pos["entry"]
    risk = pos["risk"]
    stop = pos["stop"]

    high = float(bar.high)
    low = float(bar.low)
    opening = float(bar.open)
    closing = float(bar.close)

    target2 = entry + direction * risk * 2
    target3 = entry + direction * risk * 3
    target4 = entry + direction * risk * 4

    # Existing stop is checked BEFORE raising the stop.
    # This avoids using a newly raised stop on the same candle.

    if side == "BUY":
        stop_hit = low <= stop
        final_hit = high >= target4
    else:
        stop_hit = high >= stop
        final_hit = low <= target4

    if stop_hit:
        if side == "BUY":
            fill = min(opening, stop)
            fill *= 1 - SLIPPAGE
        else:
            fill = max(opening, stop)
            fill *= 1 + SLIPPAGE

        reason = (
            "stop_loss" if pos["stage"] == 0
            else f"trail_sl_1:{pos['stage'] + 1}"
        )

        return tick(fill), reason

    if final_hit:
        fill = target4 * (
            1 - SLIPPAGE if side == "BUY"
            else 1 + SLIPPAGE
        )
        return tick(fill), "target_1:4"

    # EOD exit: 15:15 candle OPEN is not known until 15:15.
    # This function processes a completed candle, so use close.
    if timestamp.time() >= time(15, 10):
        fill = closing * (
            1 - SLIPPAGE if side == "BUY"
            else 1 + SLIPPAGE
        )
        return tick(fill), "square_off"

    # Newly trailed stop becomes effective on NEXT candle.
    if side == "BUY":
        hit3 = high >= target3
        hit2 = high >= target2
    else:
        hit3 = low <= target3
        hit2 = low <= target2

    if hit3:
        pos["stop"] = tick(target3)
        pos["stage"] = 2
    elif hit2 and pos["stage"] < 1:
        pos["stop"] = tick(target2)
        pos["stage"] = 1

    return None


def default_state(day):
    return {
        "date": day,
        "equity": CAPITAL,
        "day_start_equity": CAPITAL,
        "day_pnl": 0.0,
        "trades_today": 0,
        "position": None,
        "pending": None,
        "last_processed": "",
        "last_signal_bar": "",
        "last_alert": ""
    }


def load_state(day):
    if Path(STATE_FILE).exists():
        try:
            state = json.loads(
                Path(STATE_FILE).read_text(
                    encoding="utf-8"
                )
            )
        except Exception:
            state = default_state(day)
    else:
        state = default_state(day)

    if state.get("date") != day:
        old_equity = float(
            state.get("equity", CAPITAL)
        )
        old_position = state.get("position")

        state = default_state(day)
        state["equity"] = old_equity
        state["day_start_equity"] = old_equity

        # Do not silently discard an overnight position.
        if old_position:
            state["position"] = old_position

    return state


def record_exit(state, pos, price, ts, reason, paper=True):
    record = exit_position(
        pos, price, ts, reason
    )

    state["equity"] = money(
        state["equity"] + record["net"]
    )
    state["day_pnl"] = money(
        state["day_pnl"] + record["net"]
    )
    state["position"] = None

    if paper:
        append_csv(
            PAPER_FILE, TRADE_FIELDS, record
        )
        event(
            "EXIT", pos["side"], price,
            pos["qty"], pos["stop"], reason
        )
        telegram(
            "RELIANCE PAPER EXIT\n"
            f"Side: {pos['side']}\n"
            f"Exit: {price}\n"
            f"Reason: {reason}\n"
            f"Net P/L: Rs {record['net']}\n"
            f"Equity: Rs {state['equity']}"
        )

    return record


def run_paper():
    now = now_ist()

    if now.weekday() >= 5:
        print("Weekend. No paper trading.")
        return

    if not time(9, 30) <= now.time() <= time(15, 45):
        print("Outside paper trading hours.")
        return

    df = add_indicators(download_data(7))

    # Only completed candles are eligible.
    # Candle timestamp represents its opening time.
    cutoff = now - timedelta(minutes=5, seconds=30)
    df = df[df.index <= cutoff]

    if df.empty:
        print("No completed candle available.")
        return

    day = now.date().isoformat()
    state = load_state(day)

    today_df = df[
        df.index.date == now.date()
    ]

    if today_df.empty:
        print("No current trading day data.")
        return

    last_processed = state.get(
        "last_processed", ""
    )

    new_bars = today_df[
        today_df.index.map(
            lambda x: x.isoformat() > last_processed
        )
    ]

    if new_bars.empty:
        print("No new completed 5m candle.")
        return

    for ts, bar in new_bars.iterrows():
        # Never open a position on an old historical signal
        # after the GitHub workflow has been delayed.
        fresh = (
            now - ts.to_pydatetime()
        ).total_seconds() <= 12 * 60

        pos = state.get("position")

        if pos:
            result = manage_position(
                pos, bar, ts
            )

            if result:
                price, reason = result
                record_exit(
                    state, pos, price, ts,
                    reason, paper=True
                )

        # Enter pending signal at next candle OPEN,
        # but only when the bar is recent.
        pending = state.get("pending")

        if (
            pending
            and state["position"] is None
            and fresh
            and ts.isoformat() >
                pending["signal_time"]
            and ts.time() < time(14, 30)
        ):
            position = new_position(
                pending["signal"],
                float(bar.open),
                ts,
                float(state["equity"])
            )

            if position:
                state["position"] = position
                state["trades_today"] += 1

                event(
                    "ENTRY",
                    position["side"],
                    position["entry"],
                    position["qty"],
                    position["stop"],
                    "PAPER ONLY"
                )

                telegram(
                    "RELIANCE PAPER ENTRY\n"
                    f"Side: {position['side']}\n"
                    f"Entry: {position['entry']}\n"
                    f"Stop: {position['stop']}\n"
                    f"Qty: {position['qty']}\n"
                    "NO REAL ORDER"
                )

            state["pending"] = None

        # New signal at completed candle CLOSE.
        if (
            state["position"] is None
            and state["pending"] is None
            and fresh
            and state["trades_today"] < MAX_TRADES_DAY
            and state["day_pnl"] >
                -state["day_start_equity"]
                * DAILY_LOSS_LIMIT
            and ts.time() < time(14, 25)
        ):
            idx = df.index.get_loc(ts)
            sig = signal(df, idx, "orb")

            if sig:
                state["pending"] = {
                    "signal": sig,
                    "signal_time": ts.isoformat()
                }

                event(
                    "SIGNAL", sig["side"],
                    float(bar.close),
                    details="ORB VWAP VOLUME"
                )

                telegram(
                    "RELIANCE PAPER SIGNAL\n"
                    f"Side: {sig['side']}\n"
                    f"Candle: {ts}\n"
                    "Entry: NEXT 5m CANDLE\n"
                    "PAPER ONLY"
                )

        state["last_processed"] = ts.isoformat()

    # Cancel pending signal outside entry window.
    if now.time() >= time(14, 30):
        state["pending"] = None

    save_json(STATE_FILE, state)

    print("PAPER STATE:")
    print(json.dumps(state, indent=2, default=str))


def backtest_strategy(df, strategy):
    trades = []
    state = default_state("")

    pending = None
    previous_day = None

    for i, (ts, bar) in enumerate(df.iterrows()):
        day = ts.date().isoformat()

        if day != previous_day:
            state["day_pnl"] = 0.0
            state["trades_today"] = 0
            state["day_start_equity"] = state["equity"]
            pending = None
            previous_day = day

        pos = state["position"]

        if pos:
            result = manage_position(
                pos, bar, ts
            )

            if result:
                price, reason = result
                trades.append(
                    record_exit(
                        state, pos, price,
                        ts, reason, paper=False
                    )
                )

        if (
            pending
            and state["position"] is None
            and ts.date().isoformat() ==
                pending["day"]
            and ts.time() < time(14, 30)
        ):
            position = new_position(
                pending["signal"],
                float(bar.open),
                ts,
                state["equity"]
            )

            if position:
                state["position"] = position
                state["trades_today"] += 1

            pending = None

        if (
            state["position"] is None
            and pending is None
            and state["trades_today"] < MAX_TRADES_DAY
            and state["day_pnl"] >
                -state["day_start_equity"]
                * DAILY_LOSS_LIMIT
            and ts.time() < time(14, 25)
        ):
            sig = signal(df, i, strategy)

            if sig:
                pending = {
                    "signal": sig,
                    "day": day
                }

    return pd.DataFrame(
        trades, columns=TRADE_FIELDS
    )


def run_backtest(days):
    df = add_indicators(
        download_data(days)
    )

    results = []

    for strategy in ["orb", "ema_vwap"]:
        trades = backtest_strategy(
            df, strategy
        )

        filename = (
            "RELIANCE_ORB_TRADES.csv"
            if strategy == "orb"
            else "RELIANCE_EMA_VWAP_TRADES.csv"
        )

        trades.to_csv(filename, index=False)
        results.append(trades)

        net = (
            trades.net.sum()
            if len(trades) else 0
        )

        wins = (
            (trades.net > 0).sum()
            if len(trades) else 0
        )

        print(
            strategy.upper(),
            "TRADES:", len(trades),
            "WINS:", wins,
            "NET:", money(net)
        )

    combined = pd.concat(
        results, ignore_index=True
    )

    combined.to_csv(
        "RELIANCE_ALL_TRADES.csv",
        index=False
    )

    print(
        "COMBINED NET:",
        money(combined.net.sum())
        if len(combined) else 0
    )


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--mode",
        choices=["paper", "backtest"],
        default="paper"
    )

    parser.add_argument(
        "--days",
        type=int,
        default=59
    )

    args = parser.parse_args()

    if args.mode == "paper":
        run_paper()
    else:
        run_backtest(args.days)


if __name__ == "__main__":
    main()
