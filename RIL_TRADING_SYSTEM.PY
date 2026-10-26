#!/usr/bin/env python3
# RIL_TRADING_SYSTEM.PY
# RELIANCE NSE - 5 MINUTE COMBINED STRATEGY
# RSI + BB + POC + VWAP + EMA + ORB
# TRAILING SL: 1:2 -> 1:3 -> FINAL 1:4
# FREE YAHOO DATA - PAPER TRADING ONLY

import os
import json
import argparse
from pathlib import Path
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf


SYMBOL = "RELIANCE.NS"
IST = ZoneInfo("Asia/Kolkata")

CAPITAL = 100000.0
RISK_PERCENT = 0.25
MAX_TRADES_DAY = 2
DAILY_LOSS_PERCENT = 0.5
SLIPPAGE = 0.0002

STATE_FILE = Path("RELIANCE_PAPER_STATE.json")
PAPER_TRADES = Path("RELIANCE_PAPER_TRADES.csv")
PAPER_EVENTS = Path("RELIANCE_PAPER_EVENTS.csv")
ALL_TRADES = Path("RELIANCE_ALL_TRADES.csv")

TRADE_COLUMNS = [
    "strategy", "side", "entry_time", "exit_time",
    "entry", "exit", "initial_sl", "final_sl",
    "qty", "gross", "charges", "net",
    "r_multiple", "reason"
]

EVENT_COLUMNS = [
    "timestamp", "event", "strategy",
    "side", "price", "note"
]


# ---------------------------------------
# TELEGRAM
# ---------------------------------------

def telegram(message):
    print(message)
    token = os.getenv("BOT_TOKEN", "")
    chat_id = os.getenv("CHAT_ID", "")

    if not token or not chat_id:
        print("Telegram secrets not configured")
        return

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": message
            },
            timeout=15
        )
        response.raise_for_status()
    except requests.RequestException as error:
        print("Telegram error:", error)


def append_csv(path, row, columns):
    pd.DataFrame([
        {key: row.get(key, "") for key in columns}
    ]).to_csv(
        path,
        mode="a",
        header=not path.exists() or path.stat().st_size == 0,
        index=False
    )


# ---------------------------------------
# YAHOO FINANCE DATA
# ---------------------------------------

def download_data(days=7):
    days = min(max(int(days), 2), 59)

    df = yf.download(
        SYMBOL,
        period=f"{days}d",
        interval="5m",
        auto_adjust=False,
        progress=False,
        threads=False
    )

    if df.empty:
        raise RuntimeError("Yahoo Finance data unavailable")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df.columns = [
        str(c).strip().lower()
        for c in df.columns
    ]

    df = df[
        ["open", "high", "low", "close", "volume"]
    ].dropna().copy()

    if df.index.tz is None:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_convert(IST)

    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]

    df = df[
        (df.index.time >= time(9, 15)) &
        (df.index.time < time(15, 30))
    ]

    return df


# ---------------------------------------
# APPROXIMATE VOLUME PROFILE
# ---------------------------------------

def volume_profile(day):
    if len(day) < 15 or day['volume'].sum() <= 0:
        return None

    low = float(day['low'].min())
    high = float(day['high'].max())

    if high <= low:
        return None

    edges = np.linspace(low, high, 41)

    typical = (
        day['high'] + day['low'] + day['close']
    ) / 3

    histogram, _ = np.histogram(
        typical,
        bins=edges,
        weights=day['volume']
    )

    if histogram.sum() == 0:
        return None

    poc_index = int(np.argmax(histogram))

    left = poc_index
    right = poc_index

    total = float(histogram[poc_index])
    target = float(histogram.sum()) * 0.70

    while total < target and (
        left > 0 or right < len(histogram) - 1
    ):
        lower_volume = (
            histogram[left - 1]
            if left > 0 else -1
        )

        upper_volume = (
            histogram[right + 1]
            if right < len(histogram) - 1 else -1
        )

        if upper_volume >= lower_volume:
            right += 1
            total += histogram[right]
        else:
            left -= 1
            total += histogram[left]

    return {
        "poc": float(
            (edges[poc_index] + edges[poc_index + 1]) / 2
        ),
        "val": float(edges[left]),
        "vah": float(edges[right + 1]),
        "prev_low": low,
        "prev_high": high
    }


# ---------------------------------------
# TECHNICAL INDICATORS
# ---------------------------------------

def add_indicators(df):
    df = df.copy()

    close = df['close']
    high = df['high']
    low = df['low']
    volume = df['volume']

    # RSI 14
    delta = close.diff()

    gain = delta.clip(lower=0).ewm(
        alpha=1 / 14,
        adjust=False,
        min_periods=14
    ).mean()

    loss = (-delta.clip(upper=0)).ewm(
        alpha=1 / 14,
        adjust=False,
        min_periods=14
    ).mean()

    rs = gain / loss.replace(0, np.nan)

    df["rsi"] = 100 - 100 / (1 + rs)
    df.loc[(loss == 0) & (gain > 0), "rsi"] = 100

    # Bollinger Bands
    df["bb_mid"] = close.rolling(20).mean()
    std = close.rolling(20).std(ddof=0)
    df["bb_upper"] = df["bb_mid"] + 2 * std
    df["bb_lower"] = df["bb_mid"] - 2 * std

    # EMA
    df["ema9"] = close.ewm(span=9, adjust=False).mean()
    df["ema21"] = close.ewm(span=21, adjust=False).mean()

    # ATR
    previous_close = close.shift(1)
    true_range = pd.concat([
        high - low,
        (high - previous_close).abs(),
        (low - previous_close).abs()
    ], axis=1).max(axis=1)

    df["atr"] = true_range.ewm(
        alpha=1 / 14,
        adjust=False,
        min_periods=14
    ).mean()

    # Previous 20 completed candles volume average
    df["vol_avg"] = volume.shift(1).rolling(20).mean()

    # Daily VWAP
    typical = (high + low + close) / 3
    session = pd.Series(df.index.date, index=df.index)

    df["vwap"] = (
        (typical * volume).groupby(session).cumsum()
        /
        volume.groupby(session).cumsum().replace(0, np.nan)
    )

    for col in [
        "poc", "val", "vah",
        "prev_low", "prev_high",
        "orb_high", "orb_low"
    ]:
        df[col] = np.nan

    groups = list(df.groupby(df.index.date))

    for j, (_, day) in enumerate(groups):
        opening = day[
            (day.index.time >= time(9, 15)) &
            (day.index.time < time(9, 30))
        ]

        if len(opening) >= 2:
            eligible = day[
                day.index.time >= time(9, 30)
            ].index

            df.loc[eligible, "orb_high"] = opening['high'].max()
            df.loc[eligible, "orb_low"] = opening['low'].min()

        if j > 0:
            levels = volume_profile(groups[j - 1][1])
            if levels:
                for key, value in levels.items():
                    df.loc[day.index, key] = value

    return df


# ---------------------------------------
# COMBINED BUY / SELL STRATEGY
# ---------------------------------------

def get_signal(df, i):
    if i < 25:
        return None

    row = df.iloc[i]
    previous = df.iloc[i - 1]
    ts = df.index[i]

    if not (time(9, 35) <= ts.time() < time(14, 20)):
        return None

    # Avoid lunch-time sideways period
    if time(12, 0) <= ts.time() < time(13, 30):
        return None

    required = [
        "rsi", "atr", "vol_avg",
        "vwap", "bb_lower", "bb_upper"
    ]

    if any(not np.isfinite(row[k]) for k in required):
        return None

    if row['vol_avg'] <= 0:
        return None

    high_volume = row['volume'] >= 1.5 * row['vol_avg']
    if not high_volume:
        return None

    # STRATEGY 1 + 2: RSI + BB REJECTION + VOLUME PROFILE
    for side in (1, -1):
        if side == 1:
            extreme = (
                previous['rsi'] < 21
                and previous['low'] <= previous['bb_lower']
            )
            rejection = (
                row['rsi'] > 21
                and row['close'] > row['bb_lower']
                and row['close'] > row['open']
            )
            levels = [row['val'], row['poc'], row['prev_low']]
            stop = min(previous['low'], row['low']) - 0.10 * row['atr']
        else:
            extreme = (
                previous['rsi'] > 85
                and previous['high'] >= previous['bb_upper']
            )
            rejection = (
                row['rsi'] < 85
                and row['close'] < row['bb_upper']
                and row['close'] < row['open']
            )
            levels = [row['vah'], row['poc'], row['prev_high']]
            stop = max(previous['high'], row['high']) + 0.10 * row['atr']

        near_profile = any(
            np.isfinite(level)
            and row['low'] - 0.25 * row['atr']
            <= level
            <= row['high'] + 0.25 * row['atr']
            for level in levels
        )

        if extreme and rejection and near_profile:
            against_trend = (
                (side == 1 and row['ema9'] < row['ema21'] and row['close'] < row['vwap'])
                or
                (side == -1 and row['ema9'] > row['ema21'] and row['close'] > row['vwap'])
            )

            if not against_trend:
                return {
                    "strategy": "RSI_BB_POC",
                    "side": side,
                    "stop": float(stop)
                }

    # STRATEGY 3 + 4: ORB + VWAP + EMA + VOLUME
    if not np.isfinite(row['orb_high']):
        return None

    # ORB BUY
    if (
        previous['close'] <= row['orb_high']
        and row['close'] > row['orb_high']
        and row['close'] > row['vwap']
        and row['ema9'] > row['ema21']
    ):
        return {
            "strategy": "ORB_VWAP",
            "side": 1,
            "stop": float(max(row['close'] - 1.5 * row['atr'], row['orb_low']))
        }

    # ORB SELL
    if (
        previous['close'] >= row['orb_low']
        and row['close'] < row['orb_low']
        and row['close'] < row['vwap']
        and row['ema9'] < row['ema21']
    ):
        return {
            "strategy": "ORB_VWAP",
            "side": -1,
            "stop": float(min(row['close'] + 1.5 * row['atr'], row['orb_high']))
        }

    return None


# ---------------------------------------
# APPROXIMATE INTRADAY CHARGES
# ---------------------------------------

def charges(entry, exit_price, qty, side):
    if side == 1:
        buy, sell = entry, exit_price
    else:
        buy, sell = exit_price, entry

    turnover = (buy + sell) * qty

    brokerage = (
        min(buy * qty * 0.0003, 20)
        + min(sell * qty * 0.0003, 20)
    )

    exchange = turnover * 0.0000297
    sebi = turnover * 0.000001
    stt = sell * qty * 0.00025
    stamp = buy * qty * 0.00003
    gst = 0.18 * (brokerage + exchange + sebi)

    return brokerage + exchange + sebi + stt + stamp + gst


# ---------------------------------------
# PAPER ENTRY
# ---------------------------------------

def create_position(signal, price, ts, equity):
    side = signal["side"]
    entry = float(price) * (1 + side * SLIPPAGE)
    stop = float(signal["stop"])
    risk = (entry - stop) * side

    if risk <= 0:
        return None

    if not 0.0015 <= risk / entry <= 0.01:
        return None

    risk_budget = equity * RISK_PERCENT / 100
    qty = min(int(risk_budget / risk), int(equity / entry))

    if qty < 1:
        return None

    return {
        "strategy": signal["strategy"],
        "side": side,
        "entry": entry,
        "entry_time": ts.isoformat(),
        "initial_stop": stop,
        "stop": stop,
        "risk": risk,
        "qty": qty,
        "stage": 0
    }


# ---------------------------------------
# TRAILING SL MANAGEMENT
# ---------------------------------------

def manage_position(pos, candle, ts):
    side = pos["side"]
    entry = pos["entry"]
    risk = pos["risk"]
    stop = pos["stop"]

    stop_hit = (
        candle['low'] <= stop
        if side == 1
        else candle['high'] >= stop
    )

    if stop_hit:
        price = min(candle['open'], stop) if side == 1 else max(candle['open'], stop)
        price *= 1 - side * SLIPPAGE
        reason = "INITIAL_SL" if pos["stage"] == 0 else "TRAILING_SL"
        return float(price), reason

    target4 = entry + side * 4 * risk
    hit4 = candle['high'] >= target4 if side == 1 else candle['low'] <= target4

    if hit4:
        return target4 * (1 - side * SLIPPAGE), "TARGET_1_4"

    if ts.time() >= time(15, 10):
        return float(candle['close']) * (1 - side * SLIPPAGE), "SQUARE_OFF"

    target2 = entry + side * 2 * risk
    target3 = entry + side * 3 * risk

    hit2 = candle['high'] >= target2 if side == 1 else candle['low'] <= target2
    hit3 = candle['high'] >= target3 if side == 1 else candle['low'] <= target3

    if hit3:
        pos["stage"] = 2
        pos["stop"] = target3
    elif hit2 and pos["stage"] < 1:
        pos["stage"] = 1
        pos["stop"] = target2

    return None


# ---------------------------------------
# CLOSE TRADE
# ---------------------------------------

def close_trade(pos, exit_price, ts, reason):
    side = pos["side"]
    qty = pos["qty"]
    gross = (exit_price - pos["entry"]) * side * qty
    fee = charges(pos["entry"], exit_price, qty, side)
    net = gross - fee

    return {
        "strategy": pos["strategy"],
        "side": "BUY" if side == 1 else "SELL",
        "entry_time": pos["entry_time"],
        "exit_time": ts.isoformat(),
        "entry": round(pos["entry"], 2),
        "exit": round(exit_price, 2),
        "initial_sl": round(pos["initial_stop"], 2),
        "final_sl": round(pos["stop"], 2),
        "qty": qty,
        "gross": round(gross, 2),
        "charges": round(fee, 2),
        "net": round(net, 2),
        "r_multiple": round(net / (pos["risk"] * qty), 3),
        "reason": reason
    }


def new_state():
    return {
        "equity": CAPITAL,
        "day": "",
        "day_start": CAPITAL,
        "day_pnl": 0.0,
        "trades_today": 0,
        "position": None,
        "pending": None,
        "last_bar": ""
    }


# ---------------------------------------
# BACKTEST & PAPER MODES
# ---------------------------------------

def backtest(df):
    state = new_state()
    records = []

    for i in range(len(df)):
        ts = df.index[i]
        candle = df.iloc[i]
        day = ts.date().isoformat()

        if state["day"] != day:
            if state["position"]:
                previous = df.iloc[i - 1]
                previous_ts = df.index[i - 1]
                pos = state["position"]
                price = float(previous['close']) * (1 - pos["side"] * SLIPPAGE)
                record = close_trade(pos, price, previous_ts, "EOD")
                records.append(record)
                state["equity"] += record["net"]
                state["position"] = None

            state["day"] = day
            state["day_start"] = state["equity"]
            state["day_pnl"] = 0.0
            state["trades_today"] = 0
            state["pending"] = None

        if state["position"]:
            result = manage_position(state["position"], candle, ts)
            if result:
                record = close_trade(state["position"], result[0], ts, result[1])
                records.append(record)
                state["equity"] += record["net"]
                state["day_pnl"] += record["net"]
                state["position"] = None

        pending = state["pending"]
        if pending:
            previous_ts = datetime.fromisoformat(pending["time"])
            valid = (
                ts.date() == previous_ts.date()
                and (ts - previous_ts).total_seconds() == 300
                and ts.time() < time(14, 25)
            )

            if valid and state["position"] is None:
                pos = create_position(pending["signal"], candle['open'], ts, state["equity"])
                if pos:
                    state["position"] = pos
                    state["trades_today"] += 1
                    result = manage_position(pos, candle, ts)
                    if result:
                        record = close_trade(pos, result[0], ts, result[1])
                        records.append(record)
                        state["equity"] += record["net"]
                        state["day_pnl"] += record["net"]
                        state["position"] = None

            state["pending"] = None

        if (
            state["position"] is None
            and state["trades_today"] < MAX_TRADES_DAY
            and state["day_pnl"] > -state["day_start"] * DAILY_LOSS_PERCENT / 100
        ):
            signal = get_signal(df, i)
            if signal:
                state["pending"] = {
                    "time": ts.isoformat(),
                    "signal": signal
                }

    if state["position"]:
        pos = state["position"]
        ts = df.index[-1]
        price = float(df.iloc[-1]['close']) * (1 - pos["side"] * SLIPPAGE)
        records.append(close_trade(pos, price, ts, "END_OF_DATA"))

    result = pd.DataFrame(records, columns=TRADE_COLUMNS)
    result.to_csv(ALL_TRADES, index=False)

    for strategy in ["ORB_VWAP", "RSI_BB_POC"]:
        subset = result[result['strategy'] == strategy]
        subset.to_csv(f"RELIANCE_{strategy}_TRADES.csv", index=False)
        print(strategy, "Trades:", len(subset), "Net INR:", round(subset['net'].sum(), 2))

    print("----------------------------------")
    print("TOTAL TRADES:", len(result))
    print("NET PROFIT INR:", round(result['net'].sum(), 2))
    print("----------------------------------")


def paper(df):
    now = datetime.now(IST)

    if now.weekday() >= 5:
        print("Weekend - NSE closed")
        return

    if not time(9, 30) <= now.time() < time(15, 45):
        print("Outside paper trading window")
        return

    cutoff = now - timedelta(minutes=5, seconds=30)
    df = df[df.index <= cutoff]
    today = df[df.index.date == now.date()]

    if today.empty:
        print("No completed Yahoo candle yet")
        return

    if not STATE_FILE.exists():
        state = new_state()
        state["last_bar"] = today.index[-1].isoformat()
        STATE_FILE.write_text(json.dumps(state, indent=2))
        print("Paper initialized. Waiting for next completed candle.")
        return

    state = json.loads(STATE_FILE.read_text())
    last_bar = state.get("last_bar", "")

    new_indices = [
        i for i, ts in enumerate(df.index)
        if ts.isoformat() > last_bar
    ]

    if not new_indices:
        print("No new Yahoo candle")
        return

    for i in new_indices:
        ts = df.index[i]
        candle = df.iloc[i]
        day = ts.date().isoformat()
        age = (now - ts.to_pydatetime()).total_seconds()
        fresh = (ts.date() == now.date() and 300 <= age <= 720)

        if state.get("day") != day:
            if state.get("position") and i > 0:
                pos = state["position"]
                prev = df.iloc[i - 1]
                prev_ts = df.index[i - 1]
                price = float(prev['close']) * (1 - pos["side"] * SLIPPAGE)
                record = close_trade(pos, price, prev_ts, "DELAYED_EOD")
                append_csv(PAPER_TRADES, record, TRADE_COLUMNS)
                state["equity"] += record["net"]
                state["position"] = None
                telegram(f"RELIANCE PAPER DELAYED EOD\nNet P/L INR: {record['net']}")

            state["day"] = day
            state["day_start"] = state["equity"]
            state["day_pnl"] = 0.0
            state["trades_today"] = 0

        if state.get("position"):
            pos = state["position"]
            result = manage_position(pos, candle, ts)
            if result:
                record = close_trade(pos, result[0], ts, result[1])
                append_csv(PAPER_TRADES, record, TRADE_COLUMNS)
                state["equity"] += record["net"]
                state["day_pnl"] += record["net"]
                state["position"] = None
                append_csv(PAPER_EVENTS, {
                    "timestamp": ts.isoformat(),
                    "event": "EXIT",
                    "strategy": record["strategy"],
                    "side": record["side"],
                    "price": record["exit"],
                    "note": record["reason"]
                }, EVENT_COLUMNS)
                telegram(f"RELIANCE PAPER EXIT\nStrategy: {record['strategy']}\nExit: {record['exit']}\nNet P/L: {record['net']}")

        if (
            fresh
            and state.get("position") is None
            and state["trades_today"] < MAX_TRADES_DAY
            and state["day_pnl"] > -state["day_start"] * DAILY_LOSS_PERCENT / 100
            and ts.time() < time(14, 20)
        ):
            signal = get_signal(df, i)
            if signal:
                pos = create_position(signal, float(candle['close']), ts, state["equity"])
                if pos:
                    state["position"] = pos
                    state["trades_today"] += 1
                    entry = pos["entry"]
                    side = pos["side"]
                    risk = pos["risk"]
                    target2 = entry + side * 2 * risk
                    target3 = entry + side * 3 * risk
                    target4 = entry + side * 4 * risk

                    append_csv(PAPER_EVENTS, {
                        "timestamp": ts.isoformat(),
                        "event": "ENTRY",
                        "strategy": pos["strategy"],
                        "side": "BUY" if side == 1 else "SELL",
                        "price": round(entry, 2),
                        "note": "Delayed indicative entry"
                    }, EVENT_COLUMNS)

                    telegram(
                        f"RELIANCE 5M PAPER SIGNAL\n"
                        f"Strategy: {pos['strategy']}\n"
                        f"Side: {'BUY' if side == 1 else 'SELL'}\n"
                        f"Entry: {entry:.2f}\n"
                        f"SL: {pos['stop']:.2f}\n"
                        f"1:2 = {target2:.2f} | 1:3 = {target3:.2f} | 1:4 = {target4:.2f}\n"
                        f"Qty: {pos['qty']}"
                    )

        state["last_bar"] = ts.isoformat()

    STATE_FILE.write_text(json.dumps(state, indent=2))
    print("----------------------------------")
    print("RELIANCE PAPER SCANNER COMPLETE")
    print("Paper Equity INR:", round(state["equity"], 2))
    print("Open Trade:", bool(state.get("position")))
    print("----------------------------------")


# ---------------------------------------
# MAIN
# ---------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["paper", "backtest"], default="paper")
    parser.add_argument("--days", type=int, default=59)
    args = parser.parse_args()

    print("==================================")
    print("RELIANCE COMBINED 5M SCANNER")
    print("RSI + BB + POC + VWAP + EMA + ORB")
    print("MODE:", args.mode)
    print("==================================")

    days = 7 if args.mode == "paper" else args.days
    df = download_data(days)
    df = add_indicators(df)

    if args.mode == "backtest":
        backtest(df)
    else:
        paper(df)


if __name__ == "__main__":
    main()
