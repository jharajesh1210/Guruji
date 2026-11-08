#!/usr/bin/env python3
"""
BTC Daily + Weekly Combined Intraday Strategy  (v2: signal-frequency fix)
CoinDCX Futures market data | 5-minute candles | PAPER TRADING ONLY

Four preserved setups (same logic as before):
1) WEEKLY_VWAP_RECLAIM (LONG)
2) BB_MIDDLE_WEEKLY_TOUCH (LONG)
3) WEEKLY_HIGH_EXHAUSTION (SHORT)
4) BB_MIDDLE_REJECTION (SHORT)

Exit ladder (unchanged): 2R locks SL at 1R; 3R locks SL at 2R; 4R closes full position.

What changed in v2
------------------
* Thresholds live in two profiles: ORIGINAL (old numbers) and RELAXED (default).
  Pick with env STRATEGY_PROFILE=ORIGINAL|RELAXED.
* Several conditions that were contradictory / near-impossible together were fixed
  (see PROFILES below and the notes next to each setup).
* Opening-range look-ahead and partial-first-day bugs fixed.
* Signals are computed vectorised, so the live scanner and the backtest share the same code.
* New diagnostics: run with --backtest to see exactly which condition blocks signals.

Usage
-----
  python BTC_DAILY_WEEKLY_INTRADAY_PAPER.py                       # live paper scanner
  python BTC_DAILY_WEEKLY_INTRADAY_PAPER.py --backtest --compare  # ORIGINAL vs RELAXED funnel + trades
  python BTC_DAILY_WEEKLY_INTRADAY_PAPER.py --backtest --days 60  # longer history if the API allows

No authenticated CoinDCX endpoints and no exchange order placement are used.
"""

from __future__ import annotations

import argparse
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
SESSION.headers.update({"User-Agent": "BTC-Daily-Weekly-Paper-Scanner/2.0"})
STOP_REQUESTED = False

# --------------------------- STRATEGY PROFILES ---------------------------
# ORIGINAL = the exact numbers from the first version (very rarely fires).
# RELAXED  = same four setups, same structure, thresholds loosened so that
#            the conditions can actually be true at the same time.

PROFILES: dict[str, dict[str, Any]] = {
    "ORIGINAL": dict(
        # shared gate
        or_mode="both",            # opening-range range AND volume must beat the baseline
        or_factor=1.0,             # ... baseline multiplier
        or_baseline_min_days=5,    # prior days needed for the baseline
        level_atr=0.75,            # close within N*ATR of a weekly level
        min_quote_vol=10_000_000.0,  # 24h quote volume (USDT) liquidity floor
        vol_spike=1.5,             # volume >= N * 20-bar average
        # candle shapes
        body_strong=0.60, maru_body=0.75, maru_wick=0.15,
        rsi_long_min=57.0,
        # setup 1
        s1_lookback=1,
        # setup 2
        s2_touch_bars=3, s2_touch_atr=0.25,
        # setup 3
        s3_rise_bars=4, s3_breakout_lookback=2, s3_rsi_exh=70.0, s3_rsi_lookback=1,
        s3_need_neg_hist=True, s3_open_mode="strong",
        # setup 4
        s4_window=12, s4_min=2, s4_max=3, s4_rsi_lo=45.0, s4_rsi_hi=52.0, s4_mid_atr=0.15,
    ),
    "RELAXED": dict(
        or_mode="either",
        or_factor=0.8,
        or_baseline_min_days=3,
        level_atr=2.0,
        min_quote_vol=1_000_000.0,
        vol_spike=1.2,
        body_strong=0.50, maru_body=0.65, maru_wick=0.20,
        rsi_long_min=52.0,
        s1_lookback=6,
        s2_touch_bars=6, s2_touch_atr=0.50,
        s3_rise_bars=3, s3_breakout_lookback=6, s3_rsi_exh=65.0, s3_rsi_lookback=3,
        s3_need_neg_hist=False, s3_open_mode="green",
        s4_window=18, s4_min=2, s4_max=4, s4_rsi_lo=40.0, s4_rsi_hi=55.0, s4_mid_atr=0.30,
    ),
}
P: dict[str, Any] = {}


def set_profile(name: str, apply_env: bool = True) -> None:
    name = name.upper()
    if name not in PROFILES:
        raise ValueError(f"Unknown profile {name}; use one of {list(PROFILES)}")
    P.clear()
    P.update(PROFILES[name])
    P["name"] = name
    if apply_env:
        if os.getenv("VOLUME_SPIKE_MULTIPLIER"):
            P["vol_spike"] = float(os.environ["VOLUME_SPIKE_MULTIPLIER"])
        if os.getenv("MIN_24H_QUOTE_VOLUME_USDT"):
            P["min_quote_vol"] = float(os.environ["MIN_24H_QUOTE_VOLUME_USDT"])


set_profile(os.getenv("STRATEGY_PROFILE", "RELAXED"))


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

def fetch_futures_candles(days: Optional[int] = None) -> pd.DataFrame:
    """Fetch public CoinDCX futures 5-minute candles; API timestamps are milliseconds."""
    days = int(days or HISTORY_DAYS)
    now = int(time.time())
    params = {
        "pair": PAIR,
        "from": now - days * 24 * 60 * 60,
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

    if len(frame) < days * 288 * 0.8:
        log.warning("Only %s closed candles received for %s days; the API may be capping history.",
                    len(frame), days)
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
    x["bar_in_day"] = x.groupby("session_date").cumcount()

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
    x["volume_spike"] = volume >= (x["volume_avg20"] * P["vol_spike"])

    # ---- Daily opening-range block -------------------------------------
    daily = x.groupby("session_date", sort=True).agg(
        daily_close=("close", "last"),
        or_high=("high", lambda s: s.iloc[:OPENING_RANGE_BARS].max() if len(s) >= OPENING_RANGE_BARS else np.nan),
        or_low=("low", lambda s: s.iloc[:OPENING_RANGE_BARS].min() if len(s) >= OPENING_RANGE_BARS else np.nan),
        or_volume=("volume", lambda s: s.iloc[:OPENING_RANGE_BARS].sum() if len(s) >= OPENING_RANGE_BARS else np.nan),
    )
    first_candle = x.groupby("session_date", sort=True).head(1).set_index("session_date")

    # FIX: the first day of the downloaded history usually starts mid-day. Its "opening
    # range" is not the 00:00-00:30 IST range and used to poison the 5-day baseline.
    day_start_ok = (first_candle["local_time"].dt.strftime("%H:%M") == "00:00").reindex(daily.index).fillna(False)
    for col in ("or_high", "or_low", "or_volume"):
        daily[col] = daily[col].where(day_start_ok)

    first_range = (first_candle["high"] - first_candle["low"]).replace(0, np.nan)
    first_body = first_candle["close"] - first_candle["open"]
    daily["opening_green"] = ((first_body > 0) & day_start_ok).astype(bool)
    daily["opening_green_strong"] = (
        (first_body > 0) & ((first_body / first_range) >= 0.60) & day_start_ok
    ).astype(bool)

    daily["or_range"] = daily["or_high"] - daily["or_low"]
    daily["previous_daily_close"] = daily["daily_close"].shift(1)
    mind = int(P["or_baseline_min_days"])
    daily["avg_prev5_or_range"] = daily["or_range"].shift(1).rolling(5, min_periods=mind).mean()
    daily["avg_prev5_or_volume"] = daily["or_volume"].shift(1).rolling(5, min_periods=mind).mean()
    daily["or_ready"] = (daily["or_high"].notna() & daily["or_low"].notna()).astype(bool)
    x = x.join(daily.drop(columns=["daily_close"]), on="session_date")

    weekly = x.groupby("week_key", sort=True).agg(
        weekly_open=("open", "first"),
        weekly_high=("high", "max"),
        weekly_low=("low", "min"),
    )
    weekly["previous_week_high"] = weekly["weekly_high"].shift(1)
    weekly["previous_week_low"] = weekly["weekly_low"].shift(1)
    x = x.join(weekly, on="week_key")

    # Quote-volume approximation in USDT (base volume x candle close).
    x["quote_volume"] = x["volume"] * x["close"]
    x["quote_volume_24h"] = x["quote_volume"].rolling(288, min_periods=200).sum()

    # Opening-range filter: today's 00:00-00:30 IST range/volume vs the prior 5 days.
    # FIX: only usable once the opening range has actually finished (no look-ahead).
    factor = float(P["or_factor"])
    c_range = x["or_range"] > factor * x["avg_prev5_or_range"]
    c_vol = x["or_volume"] > factor * x["avg_prev5_or_volume"]
    mix = (c_range & c_vol) if P["or_mode"] == "both" else (c_range | c_vol)
    x["or_filter"] = x["or_ready"].fillna(False).astype(bool) & (x["bar_in_day"] >= OPENING_RANGE_BARS) & mix

    # Candle shapes.
    candle_range = (x["high"] - x["low"]).replace(0, np.nan)
    body = (x["close"] - x["open"]).abs()
    bs = float(P["body_strong"])
    x["bull_strong"] = (x["close"] > x["open"]) & (body / candle_range >= bs)
    x["bear_strong"] = (x["close"] < x["open"]) & (body / candle_range >= bs)
    wick = float(P["maru_wick"])
    x["bull_marubozu"] = (
        (x["close"] > x["open"])
        & (body / candle_range >= float(P["maru_body"]))
        & ((x["high"] - x["close"]) / candle_range <= wick)
        & ((x["open"] - x["low"]) / candle_range <= wick)
    )

    # Weekly-level proximity.
    level_cols = ["weekly_open", "weekly_vwap", "previous_week_high", "previous_week_low"]
    distances = pd.concat([(x["close"] - x[c]).abs() for c in level_cols], axis=1)
    x["near_weekly_level"] = distances.min(axis=1) <= (float(P["level_atr"]) * x["atr"])

    # Failed tests of the BB middle (touch, but close below it) over a rolling window.
    tolerance = x["atr"] * 0.10
    x["bb_touch"] = (
        (x["low"] <= (x["bb_mid"] + tolerance))
        & (x["high"] >= (x["bb_mid"] - tolerance))
    )
    x["bb_rejection_touch"] = (x["bb_touch"] & (x["close"] < x["bb_mid"])).astype(int)
    win = int(P["s4_window"])
    x["bb_touches"] = x["bb_rejection_touch"].rolling(win, min_periods=win).sum()

    # 24h liquidity check is quote value, not raw BTC units.
    x["liquid_24h"] = x["quote_volume_24h"] >= float(P["min_quote_vol"])

    return x


# --------------------------- FOUR SETUPS (vectorised) ---------------------------

def build_conditions(x: pd.DataFrame):
    """
    Returns (gate_parts, gate, setups).
    gate_parts: list[(label, bool Series)] making up the shared gate.
    setups: ordered dict name -> (side, [(label, bool Series), ...]).
    A bar is a signal when gate AND every step of a setup are True. First setup in
    order wins (same priority as before: 1, 2, 3, 4).
    """
    c, h, l = x["close"], x["high"], x["low"]
    atr = x["atr"]
    vs = x["volume_spike"].fillna(False).astype(bool)

    valid = x[[
        "rsi", "macd", "macd_signal", "macd_hist", "ema9", "ema21", "bb_mid", "atr",
        "weekly_vwap", "weekly_open", "previous_daily_close",
        "avg_prev5_or_range", "avg_prev5_or_volume",
    ]].notna().all(axis=1)

    gate_parts = [
        ("indicators ready", valid),
        ("opening-range filter (day active)", x["or_filter"]),
        ("price near a weekly level", x["near_weekly_level"]),
        ("24h quote volume liquid", x["liquid_24h"]),
    ]
    gate = pd.Series(True, index=x.index)
    for _, s in gate_parts:
        gate &= s

    macd_up = (x["macd"] > x["macd_signal"]) & (x["macd_hist"] > 0)
    rsi_long = x["rsi"] > float(P["rsi_long_min"])
    ema_up = x["ema9"] > x["ema21"]
    long_conf = [
        ("MACD > signal and hist > 0", macd_up),
        ("RSI above long minimum", rsi_long),
        ("EMA9 > EMA21", ema_up),
        ("volume spike", vs),
    ]

    setups: dict[str, tuple[str, list]] = {}

    # 1) LONG: Weekly VWAP reclaim.
    # Price had been below prev-day close, weekly VWAP and BB middle (within the last
    # N bars; originally only the single previous bar) and now crosses back above weekly VWAP.
    below_all = (c < x["previous_daily_close"]) & (c < x["weekly_vwap"]) & (c < x["bb_mid"])
    was_below = below_all.shift(1).fillna(False).astype(float).rolling(int(P["s1_lookback"]), min_periods=1).max() > 0
    crossed = (c.shift(1) <= x["weekly_vwap"].shift(1)) & (c > x["weekly_vwap"])
    setups["WEEKLY_VWAP_RECLAIM"] = ("LONG", [
        ("was below prev-close/VWAP/BB-mid recently", was_below),
        ("crossed above weekly VWAP", crossed),
        ("strong bullish candle", x["bull_strong"]),
    ] + long_conf)

    # 2) LONG: BB middle break + weekly open / weekly VWAP touch.
    tol = float(P["s2_touch_atr"]) * atr

    def touched(level: pd.Series) -> pd.Series:
        t = pd.Series(False, index=x.index)
        for k in range(int(P["s2_touch_bars"])):
            t = t | ((l.shift(k) <= level + tol) & (h.shift(k) >= level - tol))
        return t

    weekly_touch = touched(x["weekly_open"]) | touched(x["weekly_vwap"])
    setups["BB_MIDDLE_WEEKLY_TOUCH"] = ("LONG", [
        ("close > BB middle", c > x["bb_mid"]),
        ("bullish marubozu", x["bull_marubozu"]),
        ("weekly open / VWAP touched recently", weekly_touch),
    ] + long_conf)

    # 3) SHORT: Weekly high exhaustion.
    pwh = x["previous_week_high"]
    rising = pd.Series(True, index=x.index)
    for k in range(int(P["s3_rise_bars"]) - 1):
        rising &= h.shift(k) > h.shift(k + 1)
    prior_breakout = pd.Series(False, index=x.index)
    for k in range(1, int(P["s3_breakout_lookback"]) + 1):
        prior_breakout |= h.shift(k) > pwh
    rsi_prev_max = x["rsi"].shift(1).rolling(int(P["s3_rsi_lookback"]), min_periods=1).max()
    rsi_exh = (rsi_prev_max > float(P["s3_rsi_exh"])) & (x["rsi"] < x["rsi"].shift(1))
    macd_turn = (x["macd_hist"] < x["macd_hist"].shift(1)) & (x["macd"] < x["macd"].shift(1))
    mode = P["s3_open_mode"]
    if mode == "strong":
        open_ok = x["opening_green_strong"].fillna(False).astype(bool)
    elif mode == "green":
        open_ok = x["opening_green"].fillna(False).astype(bool)
    else:
        open_ok = pd.Series(True, index=x.index)
    s3_steps = [
        ("rising highs", rising),
        ("recent bar broke previous-week high", prior_breakout),
        ("high > previous-week high", h > pwh),
        ("closed back under previous-week high", c < pwh),
        ("strong bearish candle", x["bear_strong"]),
        ("RSI exhaustion turning down", rsi_exh),
        ("MACD turning down", macd_turn),
    ]
    # NOTE: RSI > 70 on the prior bar and MACD hist < 0 on this bar almost never happen together.
    if P["s3_need_neg_hist"]:
        s3_steps.append(("MACD hist < 0", x["macd_hist"] < 0))
    s3_steps += [("opening candle green", open_ok), ("volume spike", vs)]
    setups["WEEKLY_HIGH_EXHAUSTION"] = ("SHORT", s3_steps)

    # 4) SHORT: BB middle rejection after repeated failed tests.
    touches = x["bb_touches"]
    setups["BB_MIDDLE_REJECTION"] = ("SHORT", [
        ("2+ failed BB-middle tests", (touches >= int(P["s4_min"])) & (touches <= int(P["s4_max"]))),
        ("high reached BB middle", h >= x["bb_mid"] - float(P["s4_mid_atr"]) * atr),
        ("close < BB middle", c < x["bb_mid"]),
        ("strong bearish candle", x["bear_strong"]),
        ("MACD < signal and hist < 0", (x["macd"] < x["macd_signal"]) & (x["macd_hist"] < 0)),
        ("RSI inside band", (x["rsi"] >= float(P["s4_rsi_lo"])) & (x["rsi"] <= float(P["s4_rsi_hi"]))),
        ("RSI falling", x["rsi"] < x["rsi"].shift(1)),
        ("volume spike", vs),
    ])
    return gate_parts, gate, setups


def add_signals(x: pd.DataFrame) -> pd.DataFrame:
    _, gate, setups = build_conditions(x)
    x = x.copy()
    x["shared_gate"] = gate
    label = pd.Series("", index=x.index, dtype=object)
    side = pd.Series("", index=x.index, dtype=object)
    for name, (sd, steps) in reversed(list(setups.items())):  # earlier setups overwrite later ones
        m = gate.copy()
        for _, s in steps:
            m &= s.fillna(False).astype(bool)
        label[m] = name
        side[m] = sd
    x["signal"] = label
    x["signal_side"] = side
    return x


def detect_signal(x: pd.DataFrame) -> Optional[dict[str, Any]]:
    if len(x) < 300:
        return None
    if "signal" not in x.columns:
        x = add_signals(x)
    b = x.iloc[-1]
    if not b["signal"]:
        return None
    return {"side": b["signal_side"], "setup": b["signal"], "bar": b}


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
    # The signals CSV gained new columns in v2; if the header differs, keep the old file aside
    # instead of writing misaligned rows into it.
    if path.exists():
        with path.open("r", encoding="utf-8", newline="") as handle:
            first = handle.readline().strip()
        if first != ",".join(fields):
            path.rename(path.with_name(f"{path.stem}_old_{int(time.time())}{path.suffix}"))
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
    sign = 1 if pos["side"] == "LONG" else -1
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
        "target_3r": round(float(pos.get("target_3r", pos["entry_price"] + sign * 3 * pos["risk_per_btc"])), 4),
        "target_4r": round(float(pos.get("target_4r", pos["entry_price"] + sign * 4 * pos["risk_per_btc"])), 4),
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
    sgn = 1 if side == "LONG" else -1
    target1 = entry + sgn * risk
    target2 = entry + sgn * 2 * risk
    target3 = entry + sgn * 3 * risk
    target4 = entry + sgn * 4 * risk

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
        f"Pair: {PAIR} | TF: 5m | Profile: {P['name']}\n"
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
    record_event(state, pos, "FINAL_EXIT", price, qty, reason, candle_time)
    pos["remaining_quantity"] = 0.0
    state["completed_trades"] = int(state.get("completed_trades", 0)) + 1

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
    R ladder (unchanged):
      2R reached -> SL to 1R | 3R reached -> SL to 2R | 4R reached -> close full position.
    The stop active at candle start is checked first if OHLC sequencing is ambiguous.
    """
    pos = state.get("position")
    if not pos:
        return

    side = pos["side"]
    entry = float(pos["entry_price"])
    stop = float(pos["stop_price"])
    risk = float(pos["risk_per_btc"])
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


# --------------------------- BACKTEST + DIAGNOSTICS ---------------------------

def _level(side: str, entry: float, risk: float, r: float) -> float:
    return entry + r * risk if side == "LONG" else entry - r * risk


def simulate(x: pd.DataFrame) -> list[dict[str, Any]]:
    """Replays the exact same entry/stop/R-ladder rules bar by bar on signal-annotated data."""
    trades: list[dict[str, Any]] = []
    pos: Optional[dict[str, Any]] = None
    hi = x["high"].to_numpy()
    lo = x["low"].to_numpy()
    sig = x["signal"].to_numpy()
    sides = x["signal_side"].to_numpy()
    ts = x["timestamp"].tolist()

    for i in range(len(x)):
        if pos is not None:
            side, entry, risk = pos["side"], pos["entry"], pos["risk"]
            high, low = hi[i], lo[i]
            stop_hit = (low <= pos["stop"]) if side == "LONG" else (high >= pos["stop"])

            def reached(r: int) -> bool:
                lvl = _level(side, entry, risk, r)
                return high >= lvl if side == "LONG" else low <= lvl

            exit_price, reason = None, ""
            if stop_hit:
                exit_price, reason = pos["stop"], "STOP_OR_LOCKED_PROFIT_STOP"
            elif reached(4):
                exit_price, reason = _level(side, entry, risk, 4), "4R_FINAL_PROFIT_BOOKING"
            else:
                if reached(2) and pos["hr"] < 2:
                    pos["stop"], pos["hr"] = _level(side, entry, risk, 1), 2
                if reached(3) and pos["hr"] < 3:
                    pos["stop"], pos["hr"] = _level(side, entry, risk, 2), 3

            if exit_price is not None:
                r_mult = ((exit_price - entry) if side == "LONG" else (entry - exit_price)) / risk
                trades.append({
                    "entry_time": pos["time"], "exit_time": ts[i], "side": side, "setup": pos["setup"],
                    "entry": round(entry, 2), "initial_stop": round(pos["initial_stop"], 2),
                    "exit": round(exit_price, 2), "reason": reason,
                    "bars_held": i - pos["i"], "r_multiple": round(r_mult, 3),
                    "pnl_usdt": round(r_mult * risk * (CAPITAL_PER_TRADE / entry), 4),
                })
                pos = None

        if pos is None and sig[i]:
            row = x.iloc[i]
            side = sides[i]
            entry = float(row["close"])
            stop = make_stop(side, entry, row)
            risk = (entry - stop) if side == "LONG" else (stop - entry)
            if np.isfinite(risk) and risk > 0:
                pos = {"side": side, "setup": sig[i], "entry": entry, "stop": stop,
                       "initial_stop": stop, "risk": risk, "hr": 0, "i": i, "time": ts[i]}

    if pos is not None:
        trades.append({
            "entry_time": pos["time"], "exit_time": None, "side": pos["side"], "setup": pos["setup"],
            "entry": round(pos["entry"], 2), "initial_stop": round(pos["initial_stop"], 2),
            "exit": None, "reason": "STILL_OPEN", "bars_held": len(x) - 1 - pos["i"],
            "r_multiple": None, "pnl_usdt": None,
        })
    return trades


def _cum_report(title: str, base: pd.Series, steps: list) -> None:
    print(title)
    cum = base.copy()
    print(f"    {'(start)':<46}{int(cum.sum()):>8}")
    for label, s in steps:
        cum &= s.fillna(False).astype(bool)
        print(f"    + {label:<44}{int(cum.sum()):>8}")


def print_funnel(x: pd.DataFrame) -> None:
    parts, gate, setups = build_conditions(x)
    n_days = x["session_date"].nunique()
    print(f"\n=== FUNNEL | profile={P['name']} | bars={len(x)} | days={n_days} ===")
    q = x["quote_volume_24h"].dropna()
    if len(q):
        print(f"  24h quote volume (USDT): min={q.min():,.0f}  median={q.median():,.0f}  max={q.max():,.0f}"
              f"   (floor = {P['min_quote_vol']:,.0f})")
    active_days = x.loc[x["or_filter"], "session_date"].nunique()
    print(f"  days passing opening-range filter: {active_days} of {n_days}")
    _cum_report("  Shared gate (every setup needs this):", pd.Series(True, index=x.index), parts)
    for name, (side, steps) in setups.items():
        _cum_report(f"  {name} ({side}), counted only on gate-passing bars:", gate, steps)
    counts = (x["signal"] != "").sum() if "signal" in x.columns else 0
    print(f"  => raw signal bars: {counts}")


def summarize(trades: list[dict[str, Any]], title: str) -> None:
    closed = [t for t in trades if t["r_multiple"] is not None]
    print(f"\n=== TRADES | {title} ===")
    if not closed:
        print("  no closed trades")
        return
    r = np.array([t["r_multiple"] for t in closed], dtype=float)
    pnl = sum(t["pnl_usdt"] for t in closed)
    wins = int((r > 0).sum())
    gross_win, gross_loss = r[r > 0].sum(), -r[r < 0].sum()
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    print(f"  closed={len(closed)}  wins={wins}  win_rate={wins/len(closed):.0%}  "
          f"total_R={r.sum():+.2f}  avg_R={r.mean():+.2f}  profit_factor={pf:.2f}  P&L={pnl:+.2f} USDT")
    for name in sorted({t['setup'] for t in closed}):
        rr = np.array([t["r_multiple"] for t in closed if t["setup"] == name], dtype=float)
        print(f"    {name:<26} trades={len(rr):<3} total_R={rr.sum():+.2f}  avg_R={rr.mean():+.2f}")
    open_n = len(trades) - len(closed)
    if open_n:
        print(f"  (+{open_n} trade still open at the end of the data)")


def run_backtest(args: argparse.Namespace) -> None:
    candles = fetch_futures_candles(args.days)
    print(f"Loaded {len(candles)} closed 5m candles: {candles['timestamp'].iloc[0]} -> {candles['timestamp'].iloc[-1]}")
    names = ["ORIGINAL", "RELAXED"] if args.compare else [(args.profile or os.getenv("STRATEGY_PROFILE", "RELAXED")).upper()]
    for name in names:
        set_profile(name, apply_env=not args.compare)
        x = add_signals(add_indicators(candles))
        print_funnel(x)
        trades = simulate(x)
        summarize(trades, name)
        out = ROOT / f"BTC_BACKTEST_{name}_trades.csv"
        pd.DataFrame(trades).to_csv(out, index=False)
        print(f"  trade list saved: {out}")


# --------------------------- SCAN LOOP ---------------------------

def process_latest_candle(state: dict[str, Any], candles: pd.DataFrame) -> None:
    data = add_signals(add_indicators(candles))
    latest = data.iloc[-1]
    candle_time = latest["timestamp"].isoformat()

    if state.get("last_processed_candle") == candle_time:
        return

    signal_data = detect_signal(data)
    signal_label = signal_data["setup"] if signal_data else ""
    snapshot = latest.copy()
    append_csv(
        SIGNALS_FILE,
        {k: (v.isoformat() if isinstance(v, (pd.Timestamp, datetime)) else v)
         for k, v in snapshot.to_dict().items()},
        list(snapshot.to_dict().keys()),
    )

    # Manage existing paper position first.
    if state.get("position"):
        manage_open_position(state, latest, candle_time)

    if state.get("position") is None and signal_data is not None:
        open_paper_position(state, signal_data, candle_time)

    state["last_processed_candle"] = candle_time
    state["capital_per_trade_usdt"] = CAPITAL_PER_TRADE
    state["mode"] = "PAPER_ONLY"
    save_state(state)

    log.info(
        "Candle=%s close=%.2f RSI=%s OR_filter=%s weekly_near=%s liquid=%s gate=%s signal=%s open_position=%s",
        candle_time,
        float(latest["close"]),
        f"{float(latest['rsi']):.2f}" if pd.notna(latest["rsi"]) else "n/a",
        bool(latest["or_filter"]),
        bool(latest["near_weekly_level"]),
        bool(latest["liquid_24h"]),
        bool(latest["shared_gate"]),
        signal_label or "NONE",
        bool(state.get("position")),
    )


def request_stop(_signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    log.info("Stop requested. Finishing current cycle...")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="BTC daily+weekly paper strategy")
    ap.add_argument("--backtest", action="store_true", help="replay history, print funnel + trades, then exit")
    ap.add_argument("--compare", action="store_true", help="with --backtest: ORIGINAL vs RELAXED side by side")
    ap.add_argument("--profile", default=None, help="ORIGINAL or RELAXED (default RELAXED)")
    ap.add_argument("--days", type=int, default=None, help="history days to fetch (default HISTORY_DAYS)")
    return ap.parse_args()


def main() -> None:
    if not PAPER_MODE:
        raise RuntimeError("Safety lock: this strategy is paper-only.")
    if CAPITAL_PER_TRADE <= 0:
        raise ValueError("CAPITAL_PER_TRADE_USDT must be positive.")

    args = parse_args()
    load_dotenv_file()
    ROOT.mkdir(parents=True, exist_ok=True)
    set_profile(args.profile or os.getenv("STRATEGY_PROFILE", "RELAXED"))

    if args.backtest:
        run_backtest(args)
        return

    state = load_state()
    log.info(
        "Starting CoinDCX BTC Futures PAPER scanner | pair=%s | 5m | capital=%.2f USDT | OR=00:00-00:30 IST | profile=%s",
        PAIR, CAPITAL_PER_TRADE, P["name"],
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
