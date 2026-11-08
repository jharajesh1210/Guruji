#!/usr/bin/env python3
"""
BTC Daily + Weekly Combined Intraday Strategy
CoinDCX Futures market data | 5-minute candles | PAPER TRADING ONLY

Four preserved setups:
1) WEEKLY_VWAP_RECLAIM (LONG)
2) BB_MIDDLE_WEEKLY_TOUCH (LONG)
3) WEEKLY_HIGH_EXHAUSTION (SHORT)
4) BB_MIDDLE_REJECTION (SHORT)

No authenticated CoinDCX endpoints and no exchange order placement are used.
The script polls CoinDCX public futures candles and maintains a local paper ledger.
Exit ladder: 2R locks SL at 1R; 3R locks SL at 2R; 4R closes the full position.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
import requests

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    IST = timezone(timedelta(hours=5, minutes=30))

# --------------------------- CONFIGURATION ---------------------------

PAIR = os.getenv("COINDCX_PAIR", "B-BTC_USDT")
CAPITAL_PER_TRADE = float(os.getenv("CAPITAL_PER_TRADE_USDT", "100"))
TIMEFRAME_MINUTES = 5
OPENING_RANGE_MINUTES = 30
OPENING_RANGE_BARS = OPENING_RANGE_MINUTES // TIMEFRAME_MINUTES
VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", "1.5"))
MIN_24H_QUOTE_VOLUME = float(os.getenv("MIN_24H_QUOTE_VOLUME_USDT", "10000000"))
HISTORY_DAYS = int(os.getenv("HISTORY_DAYS", "30"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))
PAPER_MODE = True  # Intentionally hard-locked. This file cannot place live orders.

BASE_URL = "https://public.coindcx.com/market_data/candlesticks"
ROOT = Path(os.getenv("BTC_PAPER_DIR", ".")).resolve()
STATE_FILE = ROOT / "BTC_DAILY_WEEKLY_PAPER_state.json"
TRADES_FILE = ROOT / "BTC_DAILY_WEEKLY_PAPER_trades.csv"
SIGNALS_FILE = ROOT / "BTC_DAILY_WEEKLY_PAPER_signals.csv"
EVENTS_FILE = ROOT / "BTC_DAILY_WEEKLY_PAPER_events.csv"
ENV_FILE = ROOT / ".env"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("BTC_DAILY_WEEKLY_PAPER")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "BTC-Daily-Weekly-Paper-Scanner/1.0"})
STOP_REQUESTED = False


# --------------------------- LOCAL CONFIG ---------------------------

def load_dotenv_file() -> None:
    """Load KEY=VALUE pairs from a local .env without requiring python-dotenv."""
    if not ENV_FILE.exists():
        return
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def telegram(message: str) -> bool:
    """Send only actionable BUY/SELL entry/exit alerts. Never sends HOLD alerts."""
    token = os.getenv("TG_TOKEN") or os.getenv("BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID") or os.getenv("CHAT_ID")
    if not token or not chat_id:
        log.warning("Telegram not configured; alert was printed locally only.")
        return False
    try:
        response = SESSION.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(str(payload))
        return True
    except Exception as exc:
        log.error("Telegram delivery failed: %s", exc)
        return False


# --------------------------- COINDCX MARKET DATA ---------------------------

def fetch_futures_candles() -> pd.DataFrame:
    """Fetch public CoinDCX futures 5-minute candles; API timestamps are milliseconds."""
    now = int(time.time())
    params = {
        "pair": PAIR,
        "from": now - HISTORY_DAYS * 24 * 60 * 60,
        "to": now,
        "resolution": str(TIMEFRAME_MINUTES),
        "pcode": "f",
    }
    response = SESSION.get(BASE_URL, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()

    if isinstance(payload, dict):
        if payload.get("s") not in (None, "ok"):
            raise RuntimeError(f"CoinDCX candle API status: {payload.get('s')}")
        raw = payload.get("data", payload.get("candlesticks", []))
    elif isinstance(payload, list):
        raw = payload
    else:
        raw = []

    if not raw:
        raise RuntimeError("CoinDCX returned an empty candle response.")

    frame = pd.DataFrame(raw)
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns):
        raise RuntimeError(f"Unexpected CoinDCX candle fields: {list(frame.columns)}")

    time_col = "time" if "time" in frame.columns else (
        "timestamp" if "timestamp" in frame.columns else None
    )
    if time_col is None:
        raise RuntimeError("CoinDCX candle response has no time field.")

    for col in ["open", "high", "low", "close", "volume"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    raw_time = pd.to_numeric(frame[time_col], errors="coerce")
    # CoinDCX futures candlestick endpoint documents `time` in milliseconds.
    unit = "ms" if raw_time.dropna().median() > 10**11 else "s"
    frame["timestamp"] = pd.to_datetime(raw_time, unit=unit, utc=True, errors="coerce")

    frame = (
        frame.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
        .sort_values("timestamp")
        .drop_duplicates("timestamp")
        .reset_index(drop=True)
    )

    # Exclude the currently forming 5-minute candle. Timestamp is candle open time.
    current_open = pd.Timestamp.now(tz="UTC").floor("5min")
    frame = frame.loc[frame["timestamp"] < current_open].reset_index(drop=True)

    if len(frame) < 1500:
        log.warning("Only %s closed candles received; some weekly/session filters may be unavailable.", len(frame))
    if len(frame) < 300:
        raise RuntimeError(f"Insufficient closed candles ({len(frame)}); need at least 300.")
    return frame


# --------------------------- INDICATORS + LEVELS ---------------------------

def add_indicators(source: pd.DataFrame) -> pd.DataFrame:
    x = source.copy()
    x["local_time"] = x["timestamp"].dt.tz_convert(IST)
    x["session_date"] = x["local_time"].dt.strftime("%Y-%m-%d")
    x["week_key"] = x["local_time"].apply(
        lambda value: (value.date() - timedelta(days=value.weekday())).isoformat()
    )

    close = x["close"]
    high = x["high"]
    low = x["low"]
    volume = x["volume"]
    typical = (high + low + close) / 3.0

    # EMA 9/21
    x["ema9"] = close.ewm(span=9, adjust=False).mean()
    x["ema21"] = close.ewm(span=21, adjust=False).mean()

    # Bollinger Bands 20, 2
    x["bb_mid"] = close.rolling(20).mean()
    std = close.rolling(20).std(ddof=0)
    x["bb_upper"] = x["bb_mid"] + 2.0 * std
    x["bb_lower"] = x["bb_mid"] - 2.0 * std

    # RSI 14 (Wilder smoothing)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    x["rsi"] = 100 - (100 / (1 + rs))
    x.loc[(avg_loss == 0) & (avg_gain > 0), "rsi"] = 100
    x.loc[(avg_gain == 0) & (avg_loss > 0), "rsi"] = 0

    # MACD 12,26,9
    fast = close.ewm(span=12, adjust=False).mean()
    slow = close.ewm(span=26, adjust=False).mean()
    x["macd"] = fast - slow
    x["macd_signal"] = x["macd"].ewm(span=9, adjust=False).mean()
    x["macd_hist"] = x["macd"] - x["macd_signal"]

    # ATR 14
    previous_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    x["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

    # Daily VWAP, reset at 00:00 IST.
    pv = typical * volume
    x["daily_vwap"] = pv.groupby(x["session_date"]).cumsum() / (
        volume.groupby(x["session_date"]).cumsum().replace(0, np.nan)
    )

    # Weekly VWAP, reset at Monday 00:00 IST.
    x["weekly_vwap"] = pv.groupby(x["week_key"]).cumsum() / (
        volume.groupby(x["week_key"]).cumsum().replace(0, np.nan)
    )

    x["volume_avg20"] = volume.rolling(20).mean()
    x["volume_spike"] = volume >= (x["volume_avg20"] * VOLUME_SPIKE_MULTIPLIER)

    # Daily previous close and previous completed week's high/low.
    daily = x.groupby("session_date", sort=True).agg(
        daily_close=("close", "last"),
        or_high=("high", lambda s: s.iloc[:OPENING_RANGE_BARS].max() if len(s) >= OPENING_RANGE_BARS else np.nan),
        or_low=("low", lambda s: s.iloc[:OPENING_RANGE_BARS].min() if len(s) >= OPENING_RANGE_BARS else np.nan),
        or_volume=("volume", lambda s: s.iloc[:OPENING_RANGE_BARS].sum() if len(s) >= OPENING_RANGE_BARS else np.nan),
    )
    first_candle = x.groupby("session_date", sort=True).head(1).set_index("session_date")
    first_range = (first_candle["high"] - first_candle["low"]).replace(0, np.nan)
    daily["opening_green_strong"] = (
        (first_candle["close"] > first_candle["open"])
        & (((first_candle["close"] - first_candle["open"]) / first_range) >= 0.60)
    )
    daily["or_range"] = daily["or_high"] - daily["or_low"]
    daily["previous_daily_close"] = daily["daily_close"].shift(1)
    daily["avg_prev5_or_range"] = daily["or_range"].shift(1).rolling(5, min_periods=5).mean()
    daily["avg_prev5_or_volume"] = daily["or_volume"].shift(1).rolling(5, min_periods=5).mean()
    daily["or_ready"] = daily["or_high"].notna() & daily["or_low"].notna()
    x = x.join(daily.drop(columns=["daily_close"]), on="session_date")

    weekly = x.groupby("week_key", sort=True).agg(
        weekly_open=("open", "first"),
        weekly_high=("high", "max"),
        weekly_low=("low", "min"),
    )
    weekly["previous_week_high"] = weekly["weekly_high"].shift(1)
    weekly["previous_week_low"] = weekly["weekly_low"].shift(1)
    x = x.join(weekly, on="week_key")

    # Quote-volume approximation in USDT (base volume × candle close).
    x["quote_volume"] = x["volume"] * x["close"]
    x["quote_volume_24h"] = x["quote_volume"].rolling(288, min_periods=200).sum()

    # Opening range validity: first 30m range and volume must beat the prior 5 comparable days.
    x["or_filter"] = (
        x["or_ready"]
        & (x["or_range"] > x["avg_prev5_or_range"])
        & (x["or_volume"] > x["avg_prev5_or_volume"])
    )

    # Strong candle definitions.
    candle_range = (x["high"] - x["low"]).replace(0, np.nan)
    body = (x["close"] - x["open"]).abs()
    x["bull_strong"] = (x["close"] > x["open"]) & (body / candle_range >= 0.60)
    x["bear_strong"] = (x["close"] < x["open"]) & (body / candle_range >= 0.60)
    x["bull_marubozu"] = (
        (x["close"] > x["open"])
        & (body / candle_range >= 0.75)
        & ((x["high"] - x["close"]) / candle_range <= 0.15)
        & ((x["open"] - x["low"]) / candle_range <= 0.15)
    )

    # Weekly-level proximity: at least one weekly reference level within 0.75 ATR.
    level_cols = ["weekly_open", "weekly_vwap", "previous_week_high", "previous_week_low"]
    distances = pd.concat([(x["close"] - x[c]).abs() for c in level_cols], axis=1)
    x["near_weekly_level"] = distances.min(axis=1) <= (0.75 * x["atr"])

    # Rolling touch count around BB middle; a touch is a candle range intersecting the midline.
    tolerance = x["atr"] * 0.10
    x["bb_touch"] = (
        (x["low"] <= (x["bb_mid"] + tolerance))
        & (x["high"] >= (x["bb_mid"] - tolerance))
    )
    # Count only touches that failed to close above the middle band.
    x["bb_rejection_touch"] = (x["bb_touch"] & (x["close"] < x["bb_mid"])).astype(int)
    x["bb_touches_12"] = x["bb_rejection_touch"].rolling(12, min_periods=12).sum()

    # 24h liquidity check is quote value, not raw BTC units.
    x["liquid_24h"] = x["quote_volume_24h"] >= MIN_24H_QUOTE_VOLUME

    return x


# --------------------------- FOUR ORIGINAL SETUPS ---------------------------

def detect_signal(x: pd.DataFrame) -> Optional[dict[str, Any]]:
    if len(x) < 300:
        return None
    b = x.iloc[-1]
    p = x.iloc[-2]

    needed = [
        "rsi", "macd", "macd_signal", "macd_hist", "ema9", "ema21",
        "bb_mid", "atr", "weekly_vwap", "weekly_open", "previous_daily_close",
        "previous_week_high", "avg_prev5_or_range", "avg_prev5_or_volume",
    ]
    if any(pd.isna(b.get(k)) for k in needed):
        return None

    # Shared day/session gate: first 30m completed; range and its volume exceed prior
    # five comparable sessions; weekly levels are nearby; BTC 24h quote liquidity is adequate.
    shared_gate = bool(b["or_filter"] and b["near_weekly_level"] and b["liquid_24h"])
    if not shared_gate:
        return None

    confirmations_long = (
        b["macd"] > b["macd_signal"]
        and b["macd_hist"] > 0
        and b["rsi"] > 57
        and b["ema9"] > b["ema21"]
        and bool(b["volume_spike"])
    )

    # 1) LONG: Weekly VWAP reclaim.
    # Previous candle must be below previous close, weekly VWAP and BB middle;
    # current candle must reclaim weekly VWAP with a strong bullish close.
    setup1 = (
        float(p["close"]) < float(b["previous_daily_close"])
        and float(p["close"]) < float(p["weekly_vwap"])
        and float(p["close"]) < float(p["bb_mid"])
        and float(b["close"]) > float(b["weekly_vwap"])
        and bool(b["bull_strong"])
        and confirmations_long
    )
    if setup1:
        return {"side": "LONG", "setup": "WEEKLY_VWAP_RECLAIM", "bar": b}

    # 2) LONG: BB middle break + weekly open / weekly VWAP touch.
    last3 = x.iloc[-3:]
    touch_levels = [float(b["weekly_open"]), float(b["weekly_vwap"])]
    weekly_touch = any(
        ((last3["low"] <= level + 0.25 * float(b["atr"])) &
         (last3["high"] >= level - 0.25 * float(b["atr"]))).any()
        for level in touch_levels
    )
    setup2 = (
        float(b["close"]) > float(b["bb_mid"])
        and bool(b["bull_marubozu"])
        and weekly_touch
        and confirmations_long
    )
    if setup2:
        return {"side": "LONG", "setup": "BB_MIDDLE_WEEKLY_TOUCH", "bar": b}

    # 3) SHORT: Weekly high exhaustion.
    # Prior completed week's high is the breakout reference; price must reject it.
    last4 = x.iloc[-4:]
    rising_highs = all(
        float(last4.iloc[i]["high"]) > float(last4.iloc[i - 1]["high"])
        for i in range(1, len(last4))
    )
    prior_breakout = (
        float(last4.iloc[-2]["high"]) > float(b["previous_week_high"])
        or float(last4.iloc[-3]["high"]) > float(b["previous_week_high"])
    )
    rsi_exhaustion = float(p["rsi"]) > 70 and float(b["rsi"]) < float(p["rsi"])
    macd_bear_turn = (
        float(b["macd_hist"]) < float(p["macd_hist"])
        and float(b["macd"]) < float(p["macd"])
    )
    setup3 = (
        rising_highs
        and prior_breakout
        and float(b["high"]) > float(b["previous_week_high"])
        and float(b["close"]) < float(b["previous_week_high"])
        and bool(b["bear_strong"])
        and rsi_exhaustion
        and macd_bear_turn
        and float(b["macd_hist"]) < 0
        and bool(b["opening_green_strong"])
        and bool(b["volume_spike"])
    )
    if setup3:
        return {"side": "SHORT", "setup": "WEEKLY_HIGH_EXHAUSTION", "bar": b}

    # 4) SHORT: BB middle rejection after 2–3 failed tests.
    setup4 = (
        2 <= float(b["bb_touches_12"]) <= 3
        and float(b["high"]) >= float(b["bb_mid"]) - 0.15 * float(b["atr"])
        and float(b["close"]) < float(b["bb_mid"])
        and bool(b["bear_strong"])
        and float(b["macd"]) < float(b["macd_signal"])
        and float(b["macd_hist"]) < 0
        and 45 <= float(b["rsi"]) <= 52
        and float(b["rsi"]) < float(p["rsi"])
        and bool(b["volume_spike"])
    )
    if setup4:
        return {"side": "SHORT", "setup": "BB_MIDDLE_REJECTION", "bar": b}

    return None


# --------------------------- PAPER LEDGER ---------------------------

TRADE_FIELDS = [
    "event_time", "event", "side", "setup", "entry_price", "event_price",
    "quantity", "remaining_quantity", "notional_usdt", "stop_price",
    "target_1r", "target_2r", "target_3r", "target_4r", "realized_pnl_usdt", "cumulative_trade_pnl_usdt",
    "reason", "candle_time",
]


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            log.exception("State file unreadable; starting a fresh paper state.")
    return {
        "mode": "PAPER_ONLY",
        "last_processed_candle": None,
        "position": None,
        "cumulative_pnl_usdt": 0.0,
        "completed_trades": 0,
        "capital_per_trade_usdt": CAPITAL_PER_TRADE,
    }


def save_state(state: dict[str, Any]) -> None:
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    temp.replace(STATE_FILE)


def append_csv(path: Path, row: dict[str, Any], fields: list[str]) -> None:
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def paper_pnl(side: str, entry: float, exit_price: float, quantity: float) -> float:
    if side == "LONG":
        return (exit_price - entry) * quantity
    return (entry - exit_price) * quantity


def make_stop(side: str, entry: float, bar: pd.Series) -> float:
    atr = float(bar["atr"])
    if side == "LONG":
        candidates = [entry - 1.2 * atr, float(bar["low"]), float(bar["weekly_vwap"])]
        valid = [value for value in candidates if value < entry]
        stop = min(valid) if valid else entry - 1.2 * atr
    else:
        candidates = [entry + 1.2 * atr, float(bar["high"]), float(bar["weekly_vwap"])]
        valid = [value for value in candidates if value > entry]
        stop = max(valid) if valid else entry + 1.2 * atr
    return float(stop)


def record_event(state: dict[str, Any], pos: dict[str, Any], event: str,
                 price: float, quantity: float, reason: str, candle_time: str) -> float:
    pnl = paper_pnl(pos["side"], float(pos["entry_price"]), float(price), float(quantity))
    pos["realized_pnl_usdt"] = float(pos.get("realized_pnl_usdt", 0.0)) + pnl
    state["cumulative_pnl_usdt"] = float(state.get("cumulative_pnl_usdt", 0.0)) + pnl
    row = {
        "event_time": datetime.now(IST).isoformat(),
        "event": event,
        "side": pos["side"],
        "setup": pos["setup"],
        "entry_price": round(float(pos["entry_price"]), 4),
        "event_price": round(float(price), 4),
        "quantity": round(float(quantity), 10),
        "remaining_quantity": round(float(pos.get("remaining_quantity", 0.0)), 10),
        "notional_usdt": round(float(pos["notional_usdt"]), 4),
        "stop_price": round(float(pos["stop_price"]), 4),
        "target_1r": round(float(pos["target_1r"]), 4),
        "target_2r": round(float(pos["target_2r"]), 4),
        "target_3r": round(float(pos.get("target_3r", pos["entry_price"] + (3 * pos["risk_per_btc"] if pos["side"] == "LONG" else -3 * pos["risk_per_btc"]))), 4),
        "target_4r": round(float(pos.get("target_4r", pos["entry_price"] + (4 * pos["risk_per_btc"] if pos["side"] == "LONG" else -4 * pos["risk_per_btc"]))), 4),
        "realized_pnl_usdt": round(pnl, 8),
        "cumulative_trade_pnl_usdt": round(float(pos["realized_pnl_usdt"]), 8),
        "reason": reason,
        "candle_time": candle_time,
    }
    append_csv(TRADES_FILE, row, TRADE_FIELDS)
    return pnl


def open_paper_position(state: dict[str, Any], signal_data: dict[str, Any], candle_time: str) -> None:
    side = signal_data["side"]
    setup = signal_data["setup"]
    bar = signal_data["bar"]
    entry = float(bar["close"])
    stop = make_stop(side, entry, bar)
    risk = (entry - stop) if side == "LONG" else (stop - entry)
    if not np.isfinite(risk) or risk <= 0:
        log.warning("Rejected %s: invalid stop/risk.", setup)
        return

    # 100 USDT is the full position notional, not the risk amount or leverage.
    quantity = CAPITAL_PER_TRADE / entry
    if quantity <= 0:
        return
    target1 = entry + risk if side == "LONG" else entry - risk
    target2 = entry + 2 * risk if side == "LONG" else entry - 2 * risk
    target3 = entry + 3 * risk if side == "LONG" else entry - 3 * risk
    target4 = entry + 4 * risk if side == "LONG" else entry - 4 * risk

    position = {
        "side": side,
        "setup": setup,
        "entry_time": candle_time,
        "entry_price": entry,
        "initial_stop": stop,
        "stop_price": stop,
        "risk_per_btc": risk,
        "target_1r": target1,
        "target_2r": target2,
        "target_3r": target3,
        "target_4r": target4,
        "highest_r_reached": 0,
        "initial_quantity": quantity,
        "remaining_quantity": quantity,
        "notional_usdt": CAPITAL_PER_TRADE,
        "realized_pnl_usdt": 0.0,
    }
    state["position"] = position

    direction = "BUY" if side == "LONG" else "SELL"
    message = (
        f"BTC FUTURES PAPER {direction} ENTRY\n"
        f"Setup: {setup}\n"
        f"Pair: {PAIR} | TF: 5m\n"
        f"Entry: {entry:.2f}\n"
        f"Stop Loss: {stop:.2f}\n"
        f"Target 1R: {target1:.2f}\n"
        f"Target 2R: {target2:.2f}\n"
        f"Target 3R: {target3:.2f}\n"
        f"Target 4R: {target4:.2f} (FINAL BOOKING)\n"
        f"Management: 2R→SL 1R | 3R→SL 2R | 4R→FULL EXIT\n"
        f"Notional: {CAPITAL_PER_TRADE:.2f} USDT\n"
        f"Quantity: {quantity:.8f} BTC\n"
        f"Mode: PAPER ONLY"
    )
    log.info(message.replace("\n", " | "))
    telegram(message)


def close_remaining(state: dict[str, Any], pos: dict[str, Any], price: float,
                    reason: str, candle_time: str) -> None:
    qty = float(pos["remaining_quantity"])
    if qty <= 0:
        state["position"] = None
        return
    pnl = record_event(state, pos, "FINAL_EXIT", price, qty, reason, candle_time)
    pos["remaining_quantity"] = 0.0
    state["completed_trades"] = int(state.get("completed_trades", 0)) + 1

    # Closing a long is a SELL; closing a short is a BUY.
    exit_action = "SELL" if pos["side"] == "LONG" else "BUY"
    message = (
        f"BTC FUTURES PAPER {exit_action} EXIT ({pos['side']})\n"
        f"Setup: {pos['setup']}\n"
        f"Exit: {price:.2f}\n"
        f"Reason: {reason}\n"
        f"Trade P&L: {float(pos['realized_pnl_usdt']):+.4f} USDT\n"
        f"Total P&L: {float(state['cumulative_pnl_usdt']):+.4f} USDT"
    )
    log.info(message.replace("\n", " | "))
    telegram(message)
    state["position"] = None


def manage_open_position(state: dict[str, Any], bar: pd.Series, candle_time: str) -> None:
    """
    Required R ladder:
      2R reached -> move SL to 1R (locks 1R).
      3R reached -> move SL to 2R (locks 2R).
      4R reached -> close the full remaining position.
    No 1R partial booking and no EMA/VWAP discretionary trailing stop.
    The stop active at candle start is checked first if OHLC sequencing is ambiguous.
    """
    pos = state.get("position")
    if not pos:
        return

    side = pos["side"]
    entry = float(pos["entry_price"])
    stop = float(pos["stop_price"])
    risk = float(pos["risk_per_btc"])
    # Migrate an already-open position from the older 1R/2R paper-state format.
    pos.setdefault("target_3r", entry + 3 * risk if side == "LONG" else entry - 3 * risk)
    pos.setdefault("target_4r", entry + 4 * risk if side == "LONG" else entry - 4 * risk)
    pos.setdefault("highest_r_reached", 0)
    high, low = float(bar["high"]), float(bar["low"])

    def reached(r_multiple: int) -> bool:
        level = entry + r_multiple * risk if side == "LONG" else entry - r_multiple * risk
        return high >= level if side == "LONG" else low <= level

    stop_hit = (low <= stop) if side == "LONG" else (high >= stop)
    if stop_hit:
        close_remaining(state, pos, stop, "STOP_OR_LOCKED_PROFIT_STOP", candle_time)
        return

    if reached(4):
        close_remaining(state, pos, float(pos["target_4r"]), "4R_FINAL_PROFIT_BOOKING", candle_time)
        return

    highest = int(pos.get("highest_r_reached", 0))
    exit_action = "SELL" if side == "LONG" else "BUY"

    if reached(2) and highest < 2:
        pos["stop_price"] = entry + risk if side == "LONG" else entry - risk
        pos["highest_r_reached"] = 2
        msg = (
            f"BTC FUTURES PAPER {exit_action} SL LOCK UPDATE\n"
            f"Setup: {pos['setup']}\n"
            f"2R reached: {float(pos['target_2r']):.2f}\n"
            f"New SL locked at 1R: {float(pos['stop_price']):.2f}\n"
            f"Remaining position: 100% | No partial booking"
        )
        log.info(msg.replace("\n", " | "))
        telegram(msg)

    if reached(3) and int(pos.get("highest_r_reached", 0)) < 3:
        pos["stop_price"] = entry + 2 * risk if side == "LONG" else entry - 2 * risk
        pos["highest_r_reached"] = 3
        msg = (
            f"BTC FUTURES PAPER {exit_action} SL LOCK UPDATE\n"
            f"Setup: {pos['setup']}\n"
            f"3R reached: {float(pos['target_3r']):.2f}\n"
            f"New SL locked at 2R: {float(pos['stop_price']):.2f}\n"
            f"Remaining position: 100% | Final target: 4R"
        )
        log.info(msg.replace("\n", " | "))
        telegram(msg)


# --------------------------- SCAN LOOP ---------------------------

def process_latest_candle(state: dict[str, Any], candles: pd.DataFrame) -> None:
    data = add_indicators(candles)
    latest = data.iloc[-1]
    candle_time = latest["timestamp"].isoformat()

    if state.get("last_processed_candle") == candle_time:
        return

    # Save the indicator/signal history for audit, including no-signal candles.
    signal_data = detect_signal(data)
    signal_label = signal_data["setup"] if signal_data else ""
    snapshot = latest.copy()
    snapshot["signal"] = signal_label
    append_csv(
        SIGNALS_FILE,
        {k: (v.isoformat() if isinstance(v, (pd.Timestamp, datetime)) else v)
         for k, v in snapshot.to_dict().items()},
        list(snapshot.to_dict().keys()),
    )

    # Manage existing paper position first; do not reverse on the same candle.
    if state.get("position"):
        manage_open_position(state, latest, candle_time)

    if state.get("position") is None and signal_data is not None:
        open_paper_position(state, signal_data, candle_time)

    state["last_processed_candle"] = candle_time
    state["capital_per_trade_usdt"] = CAPITAL_PER_TRADE
    state["mode"] = "PAPER_ONLY"
    save_state(state)

    log.info(
        "Candle=%s close=%.2f RSI=%s OR_filter=%s weekly_near=%s signal=%s open_position=%s",
        candle_time,
        float(latest["close"]),
        f"{float(latest['rsi']):.2f}" if pd.notna(latest["rsi"]) else "n/a",
        bool(latest["or_filter"]),
        bool(latest["near_weekly_level"]),
        signal_label or "NONE",
        bool(state.get("position")),
    )


def request_stop(_signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    log.info("Stop requested. Finishing current cycle...")


def main() -> None:
    if not PAPER_MODE:
        raise RuntimeError("Safety lock: this strategy is paper-only.")
    if CAPITAL_PER_TRADE <= 0:
        raise ValueError("CAPITAL_PER_TRADE_USDT must be positive.")

    load_dotenv_file()
    ROOT.mkdir(parents=True, exist_ok=True)
    state = load_state()

    log.info(
        "Starting CoinDCX BTC Futures PAPER scanner | pair=%s | 5m | capital=%.2f USDT | OR=00:00-00:30 IST",
        PAIR, CAPITAL_PER_TRADE,
    )
    log.info("No API key is required for public candle data. No live-order code exists in this script.")

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    while not STOP_REQUESTED:
        cycle_started = time.time()
        try:
            market_data = fetch_futures_candles()
            process_latest_candle(state, market_data)
        except requests.RequestException as exc:
            log.error("CoinDCX network/API error: %s", exc)
        except Exception:
            log.exception("Scanner cycle failed; next cycle will retry.")

        # GitHub Actions scheduled runs are one-shot; local laptop mode keeps polling.
        if os.getenv("SCAN_ONCE", "0").strip().lower() in ("1", "true", "yes"):
            log.info("One-shot scan finished. Exiting normally for GitHub Actions.")
            break

        elapsed = time.time() - cycle_started
        sleep_for = max(5, POLL_SECONDS - int(elapsed))
        if not STOP_REQUESTED:
            time.sleep(sleep_for)

    save_state(state)
    log.info("Scanner stopped. Paper state saved.")


if __name__ == "__main__":
    main()