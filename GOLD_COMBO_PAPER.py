
#!/usr/bin/env python3
# GOLD_COMBO_PAPER.py
# XAU / PAXG - CoinDCX Futures
# Single file: signals, backtest, paper tracking, Telegram
# NO REAL ORDERS

import os
import sys
import csv
import json
import time
import argparse
from pathlib import Path
from dataclasses import dataclass

import numpy as np
import pandas as pd
import requests

NAME = "GOLD_COMBO_PAPER"
STATE = Path(NAME + "_state.json")
TRADES = Path(NAME + "_trades.csv")
SIGNALS = Path(NAME + "_signals.csv")

PAIRS = {
    "XAU": "B-XAU_USDT",
    "PAXG": "B-PAXG_USDT"
}

SYMBOLS = ("XAU", "PAXG")

# Add VERIFIED news event times in UTC.
# ("YYYY-MM-DD HH:MM", minutes_before, minutes_after)
NEWS_EVENTS = []

START_EQUITY = 1000.0
RISK_PCT = 1.0
MAX_LEVERAGE = 10.0
FEE_PCT = 0.05
SLIPPAGE_PCT = 0.01
COST = (FEE_PCT + SLIPPAGE_PCT) / 100


@dataclass
class Cfg:
    symbol: str = "XAU"
    ema_fast: int = 50
    ema_slow: int = 200
    ema_trigger: int = 20
    rsi_length: int = 14
    atr_length: int = 14
    volume_length: int = 20
    swing_15m: int = 20
    sweep_valid_bars: int = 24
    poc_window: int = 288
    poc_bins: int = 40
    bb_length: int = 20
    bb_lookback: int = 100
    min_score: int = 2
    min_sl_atr: float = 0.5
    max_sl_atr: float = 2.5
    sl_buffer_atr: float = 0.2
    tp1_r: float = 2.0
    tp2_r: float = 3.0
    max_bars: int = 288

    @property
    def volume_multiplier(self):
        return 1.2 if self.symbol == "PAXG" else 1.0

    @property
    def session_required(self):
        return self.symbol == "XAU"


def notify(message):
    token = os.getenv("BOT_TOKEN")
    chat = os.getenv("CHAT_ID")
    if not token or not chat:
        print("TELEGRAM NOT CONFIGURED:", message)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": message},
            timeout=15
        )
        r.raise_for_status()
    except requests.RequestException as exc:
        print("TELEGRAM ERROR:", exc, flush=True)


def append_csv(path, row):
    first = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row))
        if first:
            writer.writeheader()
        writer.writerow(row)


def load_csv(filename):
    df = pd.read_csv(filename)
    df.columns = [x.lower() for x in df.columns]
    col = "time" if "time" in df.columns else df.columns[0]
    raw = df[col]
    numeric = pd.to_numeric(raw, errors="coerce")
    if numeric.notna().all():
        unit = "ms" if numeric.median() > 1e11 else "s"
        dt = pd.to_datetime(numeric, unit=unit, utc=True)
    else:
        dt = pd.to_datetime(raw, utc=True)
    df.index = dt
    df = df[["open", "high", "low", "close", "volume"]]
    df = df.apply(pd.to_numeric, errors="coerce").dropna()
    return df[~df.index.duplicated()].sort_index()


def fetch_coindcx(symbol, days=30):
    pair = PAIRS[symbol]
    end = int(time.time())
    start = end - days * 86400
    url = "https://public.coindcx.com/market_data/candlesticks"
    rows = []

    with requests.Session() as session:
        for begin in range(start, end, 86400):
            finish = min(begin + 86400, end)
            params = {
                "pair": pair,
                "pcode": "f",
                "resolution": "5",
                "from": begin,
                "to": finish
            }
            for attempt in range(3):
                try:
                    r = session.get(url, params=params, timeout=30)
                    r.raise_for_status()
                    data = r.json()
                    if not isinstance(data.get("data"), list):
                        raise ValueError("Invalid candle response")
                    rows.extend(data["data"])
                    break
                except (requests.RequestException, ValueError):
                    if attempt == 2:
                        raise
                    time.sleep(2 * (attempt + 1))

    if not rows:
        raise RuntimeError(f"No candles for {pair}")

    df = pd.DataFrame(rows)
    numeric = pd.to_numeric(df["time"], errors="coerce")
    unit = "ms" if numeric.dropna().median() > 1e11 else "s"
    df["time"] = pd.to_datetime(
        numeric, unit=unit, utc=True, errors="coerce"
    )
    df = df.dropna(subset=["time"]).set_index("time")
    df = df[["open", "high", "low", "close", "volume"]]
    df = df.apply(pd.to_numeric, errors="coerce").dropna()
    df = df[~df.index.duplicated()].sort_index()

    now = pd.Timestamp.now(tz="UTC")
    df = df[df.index + pd.Timedelta(minutes=5) <= now]

    if len(df) < 100:
        raise RuntimeError(f"Not enough candles: {symbol}")

    print(f"{symbol}: fetched {len(df)} completed candles")
    return df


def ema(series, length):
    return series.ewm(span=length, adjust=False).mean()


def rsi(series, length=14):
    delta = series.diff()
    up = delta.clip(lower=0).ewm(
        alpha=1 / length, adjust=False
    ).mean()
    down = (-delta.clip(upper=0)).ewm(
        alpha=1 / length, adjust=False
    ).mean()
    rs = up / down.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def atr(df, length=14):
    previous = df["close"].shift()
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - previous).abs(),
        (df["low"] - previous).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def vwap(df):
    typical = (
        df["high"] + df["low"] + df["close"]
    ) / 3
    day = df.index.floor("D")
    pv = (typical * df["volume"]).groupby(day).cumsum()
    volume = df["volume"].groupby(day).cumsum()
    return pv / volume.replace(0, np.nan)


def poc(df, window=288, bins=40):
    closes = df["close"].to_numpy()
    volumes = df["volume"].to_numpy()
    output = np.full(len(df), np.nan)

    for i in range(window, len(df), 12):
        c = closes[i-window:i]
        v = volumes[i-window:i]
        low, high = c.min(), c.max()
        if high <= low:
            continue
        hist, edges = np.histogram(
            c, bins=bins, range=(low, high), weights=v
        )
        k = hist.argmax()
        output[i:i+12] = (edges[k] + edges[k+1]) / 2

    return pd.Series(output, index=df.index).ffill()


def higher_timeframe(df, rule):
    return df.resample(
        rule, label="right", closed="left"
    ).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    }).dropna()


def align_htf(series, index):
    return series.reindex(
        index.union(series.index)
    ).ffill().reindex(index)


def build_signals(df, cfg):
    d = df.copy()
    index = d.index

    d["ema20"] = ema(d["close"], cfg.ema_trigger)
    d["rsi"] = rsi(d["close"], cfg.rsi_length)
    d["atr"] = atr(d, cfg.atr_length)
    d["vwap"] = vwap(d)
    d["poc"] = poc(d, cfg.poc_window, cfg.poc_bins)

    average_volume = d["volume"].rolling(20).mean()
    d["vol_ok"] = (
        d["volume"] >
        average_volume * cfg.volume_multiplier
    )

    # Bollinger squeeze
    ma = d["close"].rolling(cfg.bb_length).mean()
    std = d["close"].rolling(cfg.bb_length).std()
    bandwidth = 4 * std / ma
    squeeze = bandwidth <= bandwidth.rolling(
        cfg.bb_lookback
    ).quantile(0.2)
    recent_squeeze = squeeze.rolling(12).max().fillna(0).astype(bool)
    bb_up = ma + 2 * std
    bb_down = ma - 2 * std

    # 1-hour trend - completed candles only
    h1 = higher_timeframe(d, "1h")
    fast = ema(h1["close"], cfg.ema_fast)
    slow = ema(h1["close"], cfg.ema_slow)
    up = (fast > slow) & (h1["close"] > slow)
    down = (fast < slow) & (h1["close"] < slow)
    d["trend_up"] = align_htf(up, index).fillna(False)
    d["trend_down"] = align_htf(down, index).fillna(False)

    # 15-minute liquidity sweep
    m15 = higher_timeframe(d, "15min")
    prev_low = m15["low"].rolling(cfg.swing_15m).min().shift(1)
    prev_high = m15["high"].rolling(cfg.swing_15m).max().shift(1)

    sweep_long = (
        (m15["low"] < prev_low) &
        (m15["close"] > prev_low)
    )
    sweep_short = (
        (m15["high"] > prev_high) &
        (m15["close"] < prev_high)
    )

    low15 = align_htf(
        m15["low"].where(sweep_long), index
    )
    high15 = align_htf(
        m15["high"].where(sweep_short), index
    )

    # Limit sweep validity to 24 x 5-minute candles
    low15 = low15.where(
        align_htf(
            pd.Series(
                np.where(sweep_long, 1.0, np.nan),
                index=m15.index
            ).ffill(limit=8),
            index
        ).notna()
    )
    high15 = high15.where(
        align_htf(
            pd.Series(
                np.where(sweep_short, 1.0, np.nan),
                index=m15.index
            ).ffill(limit=8),
            index
        ).notna()
    )

    # Previous-day and Asian-session sweep
    day = index.floor("D")
    daily = d.groupby(day).agg(
        high=("high", "max"),
        low=("low", "min")
    )
    previous_day_low = pd.Series(
        day, index=index
    ).map(daily["low"].shift(1))
    previous_day_high = pd.Series(
        day, index=index
    ).map(daily["high"].shift(1))

    hour = index.hour + index.minute / 60
    asian = hour < 8

    asian_low = d["low"].where(asian).groupby(day).cummin()
    asian_high = d["high"].where(asian).groupby(day).cummax()
    asian_low = asian_low.groupby(day).ffill()
    asian_high = asian_high.groupby(day).ffill()

    low_level = pd.concat([
        previous_day_low,
        asian_low.where(~asian)
    ], axis=1).min(axis=1)

    high_level = pd.concat([
        previous_day_high,
        asian_high.where(~asian)
    ], axis=1).max(axis=1)

    low_event = (
        (d["low"] < low_level) &
        (d["close"] > low_level)
    )
    high_event = (
        (d["high"] > high_level) &
        (d["close"] < high_level)
    )

    low5 = d["low"].where(low_event).ffill(
        limit=cfg.sweep_valid_bars
    )
    high5 = d["high"].where(high_event).ffill(
        limit=cfg.sweep_valid_bars
    )

    d["sweep_low"] = pd.concat(
        [low15, low5], axis=1
    ).min(axis=1)
    d["sweep_high"] = pd.concat(
        [high15, high5], axis=1
    ).max(axis=1)

    # London and New York session UTC
    session = (hour >= 8) & (hour < 17.5)
    d["session"] = session

    # Scheduled news filter
    blocked = pd.Series(False, index=index)
    for event, before, after in NEWS_EVENTS:
        stamp = pd.Timestamp(event, tz="UTC")
        blocked |= (
            (index >= stamp - pd.Timedelta(minutes=before)) &
            (index <= stamp + pd.Timedelta(minutes=after))
        )
    d["news"] = blocked

    session_ok = (
        d["session"] if cfg.session_required
        else pd.Series(True, index=index)
    )

    base = session_ok & ~d["news"]

    long_trigger = (
        (d["close"] > d["vwap"]) &
        (d["close"] > d["ema20"]) &
        (d["rsi"] > 50) &
        d["vol_ok"]
    )

    short_trigger = (
        (d["close"] < d["vwap"]) &
        (d["close"] < d["ema20"]) &
        (d["rsi"] < 50) &
        d["vol_ok"]
    )

    score_long = (
        d["session"].astype(int) +
        ((d["close"] > bb_up.shift()) &
         recent_squeeze).astype(int) +
        (d["close"] > d["poc"]).astype(int)
    )
    score_short = (
        d["session"].astype(int) +
        ((d["close"] < bb_down.shift()) &
         recent_squeeze).astype(int) +
        (d["close"] < d["poc"]).astype(int)
    )

    d["score_l"] = score_long
    d["score_s"] = score_short

    d["long"] = (
        base & d["trend_up"] &
        d["sweep_low"].notna() &
        long_trigger &
        (score_long >= cfg.min_score)
    )
    d["short"] = (
        base & d["trend_down"] &
        d["sweep_high"].notna() &
        short_trigger &
        (score_short >= cfg.min_score)
    )

    return d


def default_record():
    return {
        "last_bar": None,
        "position": None,
        "closed_count": 0,
        "wins": 0,
        "net_pnl_usdt": 0.0,
        "net_return_sum_pct": 0.0,
        "equity": START_EQUITY
    }


def load_state():
    if STATE.exists():
        data = json.loads(STATE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("Invalid state JSON")
    else:
        data = {}

    for symbol in SYMBOLS:
        data.setdefault(symbol, default_record())
    return data


def save_state(data):
    temp = STATE.with_suffix(".tmp")
    temp.write_text(
        json.dumps(data, indent=2, allow_nan=False),
        encoding="utf-8"
    )
    temp.replace(STATE)


def close_part(position, qty, price):
    sign = 1 if position["side"] == "LONG" else -1
    net = (
        (price - position["entry"]) * sign * qty
        - (position["entry"] + price) * qty * COST
    )
    position["pnl"] += net
    position["qty_left"] -= qty


def settle(symbol, record, candle, stamp):
    position = record["position"]
    if position is None:
        return

    if stamp <= pd.Timestamp(position["entry_bar"]):
        return

    sign = 1 if position["side"] == "LONG" else -1
    low = float(candle["low"])
    high = float(candle["high"])

    stop = float(position["sl"])
    tp1 = float(position["tp1"])
    tp2 = float(position["tp2"])

    stop_hit = (
        low <= stop if sign == 1 else high >= stop
    )
    tp1_hit = (
        high >= tp1 if sign == 1 else low <= tp1
    )
    tp2_hit = (
        high >= tp2 if sign == 1 else low <= tp2
    )

    closed = False

    # Conservative: stop wins when same candle is ambiguous.
    if stop_hit:
        close_part(
            position, position["qty_left"], stop
        )
        position["reason"] = (
            "BE/SL" if position["tp1_hit"] else "SL"
        )
        closed = True
    else:
        if not position["tp1_hit"] and tp1_hit:
            half = position["qty"] * 0.5
            half = min(half, position["qty_left"])
            close_part(position, half, tp1)
            position["tp1_hit"] = True
            position["sl"] = position["entry"]

        # FIX: TP2 closes only the REMAINING quantity.
        if tp2_hit and position["qty_left"] > 0:
            close_part(
                position, position["qty_left"], tp2
            )
            position["reason"] = "TP2"
            closed = True

    position["bars"] += 1

    if not closed and position["bars"] >= 288:
        close_part(
            position,
            position["qty_left"],
            float(candle["close"])
        )
        position["reason"] = "TIME"
        closed = True

    if not closed:
        return

    net = position["pnl"]
    record["closed_count"] += 1
    record["wins"] += int(net > 0)
    record["net_pnl_usdt"] += net
    record["equity"] += net

    pct = net / position["equity_at_entry"] * 100
    record["net_return_sum_pct"] += pct

    append_csv(TRADES, {
        "symbol": symbol,
        "side": position["side"],
        "entry_time": position["entry_time"],
        "exit_time": stamp.isoformat(),
        "entry": position["entry"],
        "initial_sl": position["initial_sl"],
        "tp1": tp1,
        "tp2": tp2,
        "result": position["reason"],
        "net_pnl_usdt": round(net, 6),
        "return_pct_of_equity": round(pct, 5)
    })

    notify(
        f"GOLD PAPER EXIT | {symbol} {position['side']}\n"
        f"Reason: {position['reason']}\n"
        f"Net P/L: {net:+.2f} USDT\n"
        f"Closed: {record['closed_count']} | "
        f"Wins: {record['wins']}\n"
        "NO REAL ORDER"
    )
    record["position"] = None


def enter(symbol, record, candle, stamp):
    cfg = Cfg(symbol=symbol)
    side = "LONG" if bool(candle["long"]) else "SHORT"
    sign = 1 if side == "LONG" else -1

    entry = float(candle["close"])
    current_atr = float(candle["atr"])
    anchor = float(
        candle["sweep_low"] if sign == 1
        else candle["sweep_high"]
    )

    raw_sl = anchor - sign * cfg.sl_buffer_atr * current_atr
    distance = abs(entry - raw_sl)
    distance = min(
        max(distance, cfg.min_sl_atr * current_atr),
        cfg.max_sl_atr * current_atr
    )

    if not (
        np.isfinite(distance) and distance > 0 and
        record["equity"] > 0
    ):
        return

    sl = entry - sign * distance
    quantity = min(
        (record["equity"] * RISK_PCT / 100) / distance,
        record["equity"] * MAX_LEVERAGE / entry
    )

    if quantity <= 0:
        return

    tp1 = entry + sign * cfg.tp1_r * distance
    tp2 = entry + sign * cfg.tp2_r * distance

    record["position"] = {
        "side": side,
        "entry": entry,
        "sl": sl,
        "initial_sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "qty": quantity,
        "qty_left": quantity,
        "entry_bar": stamp.isoformat(),
        "entry_time": stamp.isoformat(),
        "tp1_hit": False,
        "pnl": 0.0,
        "bars": 0,
        "equity_at_entry": record["equity"]
    }

    append_csv(SIGNALS, {
        "symbol": symbol,
        "side": side,
        "entry_time": stamp.isoformat(),
        "entry_proxy": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "score": int(
            candle["score_l"] if sign == 1
            else candle["score_s"]
        )
    })

    notify(
        f"GOLD PAPER ENTRY | {symbol} {side}\n"
        f"Entry approx: {entry:.3f}\n"
        f"SL: {sl:.3f}\n"
        f"TP1: {tp1:.3f} | TP2: {tp2:.3f}\n"
        "NO REAL ORDER"
    )


def paper_main():
    state = load_state()

    for symbol in SYMBOLS:
        record = state[symbol]

        try:
            df = fetch_coindcx(symbol, days=30)
            signals = build_signals(df, Cfg(symbol=symbol))
            latest = signals.index[-1]

            if record["last_bar"] is None:
                record["last_bar"] = latest.isoformat()
                print(f"{symbol}: initialized; no past entries")
                continue

            previous = pd.Timestamp(record["last_bar"])
            new_bars = signals[signals.index > previous]

            for stamp, candle in new_bars.iterrows():
                settle(symbol, record, candle, stamp)

                # Only latest completed bar can create a new trade.
                if (
                    stamp == latest and
                    record["position"] is None and
                    (bool(candle["long"]) or bool(candle["short"]))
                ):
                    age = (
                        pd.Timestamp.now(tz="UTC")
                        - (stamp + pd.Timedelta(minutes=5))
                    ).total_seconds()

                    if 0 <= age <= 360:
                        enter(symbol, record, candle, stamp)

                record["last_bar"] = stamp.isoformat()

            print(
                f"{symbol}: open={int(record['position'] is not None)} "
                f"closed={record['closed_count']} "
                f"wins={record['wins']} "
                f"net={record['net_pnl_usdt']:.2f} USDT"
            )

        except Exception as exc:
            print(f"ERROR {symbol}: {exc}", flush=True)
            raise
        finally:
            save_state(state)

    print("GOLD PAPER COMPLETE - NO REAL ORDERS")


def historical_backtest(symbol, days=90, filename=None):
    cfg = Cfg(symbol=symbol)
    df = load_csv(filename) if filename else fetch_coindcx(
        symbol, days=days
    )
    sig = build_signals(df, cfg)

    equity = START_EQUITY
    position = None
    results = []
    curve = [equity]

    for i in range(1, len(sig)):
        stamp = sig.index[i]
        candle = sig.iloc[i]
        previous = sig.iloc[i - 1]

        if position is not None:
            temporary = {
                "position": position,
                "closed_count": 0,
                "wins": 0,
                "net_pnl_usdt": 0.0,
                "net_return_sum_pct": 0.0,
                "equity": equity
            }

            # Backtest uses the same exit rules, but no Telegram
            # and no paper state/CSV changes.
            sign = 1 if position["side"] == "LONG" else -1
            low, high = float(candle["low"]), float(candle["high"])
            stop = position["sl"]

            stop_hit = (
                low <= stop if sign == 1 else high >= stop
            )
            tp1_hit = (
                high >= position["tp1"] if sign == 1
                else low <= position["tp1"]
            )
            tp2_hit = (
                high >= position["tp2"] if sign == 1
                else low <= position["tp2"]
            )

            done = False
            if stop_hit:
                close_part(position, position["qty_left"], stop)
                position["reason"] = "SL"
                done = True
            else:
                if not position["tp1_hit"] and tp1_hit:
                    close_part(
                        position,
                        position["qty"] * 0.5,
                        position["tp1"]
                    )
                    position["tp1_hit"] = True
                    position["sl"] = position["entry"]

                if tp2_hit and position["qty_left"] > 0:
                    close_part(
                        position,
                        position["qty_left"],
                        position["tp2"]
                    )
                    position["reason"] = "TP2"
                    done = True

            position["bars"] += 1

            if not done and position["bars"] >= cfg.max_bars:
                close_part(
                    position,
                    position["qty_left"],
                    float(candle["close"])
                )
                position["reason"] = "TIME"
                done = True

            if done:
                equity += position["pnl"]
                results.append({
                    "symbol": symbol,
                    "entry_time": position["entry_time"],
                    "exit_time": stamp.isoformat(),
                    "side": position["side"],
                    "entry": position["entry"],
                    "pnl": position["pnl"],
                    "reason": position["reason"]
                })
                position = None

        # Next candle open entry after previous candle signal.
        if position is None and (
            bool(previous["long"]) or bool(previous["short"])
        ):
            sign = 1 if previous["long"] else -1
            side = "LONG" if sign == 1 else "SHORT"
            entry = float(candle["open"])
            a = float(previous["atr"])
            anchor = float(
                previous["sweep_low"] if sign == 1
                else previous["sweep_high"]
            )

            raw_sl = anchor - sign * cfg.sl_buffer_atr * a
            dist = min(
                max(abs(entry - raw_sl), cfg.min_sl_atr * a),
                cfg.max_sl_atr * a
            )

            if np.isfinite(dist) and dist > 0 and equity > 0:
                qty = min(
                    equity * RISK_PCT / 100 / dist,
                    equity * MAX_LEVERAGE / entry
                )
                if qty > 0:
                    position = {
                        "side": side,
                        "entry": entry,
                        "sl": entry - sign * dist,
                        "initial_sl": entry - sign * dist,
                        "tp1": entry + sign * 2 * dist,
                        "tp2": entry + sign * 3 * dist,
                        "qty": qty,
                        "qty_left": qty,
                        "entry_bar": stamp.isoformat(),
                        "entry_time": stamp.isoformat(),
                        "tp1_hit": False,
                        "pnl": 0.0,
                        "bars": 0,
                        "equity_at_entry": equity
                    }

        curve.append(equity)

    total = len(results)
    wins = sum(x["pnl"] > 0 for x in results)
    net = sum(x["pnl"] for x in results)

    print(f"\n===== {symbol} HISTORICAL BACKTEST =====")
    print("Trades:", total)
    print("Wins:", wins)
    print("Losses:", total - wins)
    print("Win Rate:", round(wins / total * 100, 2) if total else 0)
    print("Net PnL USDT:", round(net, 2))
    print("Final Equity:", round(equity, 2))

    peak = pd.Series(curve).cummax()
    drawdown = (
        pd.Series(curve) / peak - 1
    ).min() * 100
    print("Max Drawdown %:", round(drawdown, 2))

    output = f"GOLD_{symbol}_{days}DAY_BACKTEST.csv"
    pd.DataFrame(
        results,
        columns=[
            "symbol", "entry_time", "exit_time",
            "side", "entry", "pnl", "reason"
        ]
    ).to_csv(output, index=False)

    print("Saved:", output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        nargs="?",
        default="paper",
        choices=["paper", "backtest"]
    )
    parser.add_argument(
        "--symbol",
        default="XAU",
        choices=["XAU", "PAXG"]
    )
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--csv")
    args = parser.parse_args()

    if args.mode == "backtest":
        historical_backtest(
            args.symbol, args.days, args.csv
        )
    else:
        paper_main()


if __name__ == "__main__":
    main()
