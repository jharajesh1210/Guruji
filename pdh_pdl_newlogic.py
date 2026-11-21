"""
pdh_pdl_newlogic.py - PAPER ONLY (no orders). Telegram alerts ON.

Gemini file (gemini-code-1795269094055.py) with the trading LOGIC UNCHANGED.
Only the plumbing is changed for CoinDCX:
  - market data: CoinDCX futures (B-XXX_USDT) instead of Binance (same 15m candles, same 24h USDT volume filter)
  - Telegram token/chat from GitHub secrets (TG_TOKEN/TG_CHAT_ID or BOT_TOKEN/CHAT_ID), messages tagged with the file name
  - paper log pdh_pdl_newlogic_trades.csv (ENTRY / LOCK / EXIT with R result) so profit/loss can be reported
Run:  python pdh_pdl_newlogic.py          (one scan)   |   python pdh_pdl_newlogic.py --report
"""
import os
import sys
import csv
import json
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta

# ==============================================================================
# CONFIGURATION & PARAMETERS (Aapke Rules + Session-Wise PDH/PDL + 1:2..1:6 Exit)
# ==============================================================================
TIMEFRAME = "15m"                  # 15-minute candles
CANDLE_LIMIT = 300                 # ~3+ days of 15m data (PDH/PDL & 100-median ke liye safe)
MIN_24H_VOLUME_USDT = 100_000_000  # Stage 1: Minimum $100M 24h volume
MIN_ATR_PCT = 0.002                # Stage 1: Minimum 0.2% ATR (dead coins filter)
MAX_ASIA_RANGE_PCT = 0.035         # London mein Asia range max 3.5% tak tight honi chahiye

# Stage 3: Fake Breakout Filter Parameters
BB_LENGTH = 20
BB_STD = 2.0
BB_MEDIAN_LOOKBACK = 100           # Pichhli 100 candles ka median BB width
MIN_BODY_RATIO = 0.60              # Candle body kam se kam 60% honi chahiye
MAX_REJECTION_WICK_RATIO = 0.30    # Breakout direction mein rejection wick max 30%
VOLUME_MULTIPLIER = 1.4            # Breakout volume >= 1.4x 20-candle avg volume
MAX_CANDLE_ATR_MULT = 2.5          # No-chasing guardrail: candle > 2.5x ATR ho to skip

# Exit Ladder (1:2 to 1:6)
RR_TIERS = [2, 3, 4, 5, 6]
FINAL_EXIT_RR = 6

# Active Trades State File (1:2 -> 1:3 -> 1:4 -> 1:5 -> 1:6 trailing lock track karne ke liye)
ACTIVE_TRADES_FILE = "pdh_pdl_newlogic_state.json"

# Telegram Settings (Apna Token aur Chat ID daalein)
TELEGRAM_BOT_TOKEN = os.getenv("TG_TOKEN") or os.getenv("BOT_TOKEN") or ""
TELEGRAM_CHAT_ID = os.getenv("TG_CHAT_ID") or os.getenv("CHAT_ID") or ""
ENABLE_TELEGRAM = os.getenv("NEWLOGIC_TELEGRAM", "YES").strip().upper() == "YES"
SCRIPT_TAG = "[pdh_pdl_newlogic.py] PAPER"
TRADES_LOG_FILE = "pdh_pdl_newlogic_trades.csv"

# High-Impact Macro News Calendar (UTC format: "YYYY-MM-DD HH:MM")
HIGH_IMPACT_NEWS_UTC = [
    # Example: "2026-12-09 19:00",
]
NEWS_BUFFER_MINUTES = 30           # News se 30 min pehle aur 30 min baad trades skip

# CoinDCX Futures Public API (was Binance)
BASE_URL = "https://public.coindcx.com"
CANDLE_URL = BASE_URL + "/market_data/candlesticks"
PRICE_URL = BASE_URL + "/market_data/v3/current_prices/futures/rt"
MAX_TICKER_AGE_SEC = 600           # CoinDCX: ticker older than this = not trading (Binance "status TRADING")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "PDH-PDL-NEWLOGIC/1.0"})


# ==============================================================================
# HELPER FUNCTIONS: TELEGRAM, NEWS FILTER & ACTIVE TRADE STATE
# ==============================================================================
def send_telegram_alert(message: str):
    message = f"{SCRIPT_TAG}\n{message}"
    print(message)
    if not ENABLE_TELEGRAM or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:   # Markdown problem -> send as plain text
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message.replace("*", "").replace("`", "")},
                          timeout=10)
    except Exception as e:
        print(f"[Telegram Error] {e}")


def log_trade(kind: str, trade: dict, status: str, r_result=None):
    """Paper log (not part of the trading logic)."""
    new = not os.path.exists(TRADES_LOG_FILE)
    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    with open(TRADES_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["Time_IST", "Pair", "Kind", "Direction", "Session", "Level", "Entry", "Initial_SL",
                        "Current_SL", "Locked_RR", "Status", "R", "PnL_USDT_10"])
        r_val = "" if r_result is None else round(r_result, 3)
        pnl = ""
        if r_result is not None and trade.get("entry") and trade.get("risk"):
            pnl = round(10.0 * r_result * trade["risk"] / trade["entry"], 4)   # 10 USDT position, before fees
        w.writerow([now_ist.strftime("%Y-%m-%d %H:%M"), trade.get("symbol"), kind, trade.get("direction"),
                    trade.get("session", ""), trade.get("level_name", ""), trade.get("entry"), trade.get("initial_sl"),
                    trade.get("current_sl"), trade.get("locked_rr", 0), status, r_val, pnl])


def is_high_impact_news_window(now_utc: datetime) -> tuple[bool, str]:
    """Stage 1: Macro Event Guard (FOMC/CPI/NFP ke 30 min pehle/baad skip)"""
    for event_str in HIGH_IMPACT_NEWS_UTC:
        event_dt = datetime.strptime(event_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        diff_minutes = abs((now_utc - event_dt).total_seconds()) / 60.0
        if diff_minutes <= NEWS_BUFFER_MINUTES:
            return True, f"High-impact news window ({event_str} UTC)"
    return False, "Clear"


def load_active_trades() -> list[dict]:
    if not os.path.exists(ACTIVE_TRADES_FILE):
        return []
    try:
        with open(ACTIVE_TRADES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_active_trades(trades: list[dict]):
    with open(ACTIVE_TRADES_FILE, "w", encoding="utf-8") as f:
        json.dump(trades, f, indent=2)


# ==============================================================================
# STAGE 1: UNIVERSAL COIN SELECTION (VOLUME & LIQUIDITY PRE-FILTER)
# ==============================================================================
def get_liquid_usdt_futures_pairs() -> dict[str, dict]:
    """24h USDT volume >= $100M wale active USDT-M perpetual pairs ka map return karta hai. (CoinDCX)"""
    payload = HTTP.get(PRICE_URL, timeout=25).json()
    prices = payload.get("prices", {}) if isinstance(payload, dict) else {}
    now_ms = time.time() * 1000
    selected = []
    for symbol, q in prices.items():
        if not (symbol.startswith("B-") and symbol.endswith("_USDT")):   # USDT perpetual
            continue
        try:
            ltp = float(q.get("ls", 0))
            quote_vol = float(q.get("v", 0))                                # 24h volume in USDT
            ts = float(q.get("ctRT") or q.get("btST") or 0)
        except (TypeError, ValueError):
            continue
        if ltp <= 0 or ts <= 0 or (now_ms - ts) > MAX_TICKER_AGE_SEC * 1000:   # not trading
            continue
        if quote_vol >= MIN_24H_VOLUME_USDT:
            selected.append({"symbol": symbol, "volume_24h": quote_vol})

    selected.sort(key=lambda x: x["volume_24h"], reverse=True)
    return {item["symbol"]: item for item in selected}


def fetch_klines(symbol: str, interval: str = TIMEFRAME, limit: int = CANDLE_LIMIT) -> pd.DataFrame:
    """OHLCV candles fetch karta hai aur sirf closed candles rakhta hai. (CoinDCX 15m)"""
    now_s = int(time.time())
    payload = HTTP.get(CANDLE_URL, params={"pair": symbol, "from": now_s - 900 * (limit + 2), "to": now_s,
                                           "resolution": "15", "pcode": "f"}, timeout=25).json()
    raw = payload.get("data", payload.get("candles", [])) if isinstance(payload, dict) else payload
    d = pd.DataFrame(raw or [])
    if d.empty or not {"time", "open", "high", "low", "close", "volume"}.issubset(d.columns):
        return pd.DataFrame(columns=["open_time", "open", "high", "low", "close", "volume", "close_time",
                                     "open_dt", "close_dt"])
    d = d.drop_duplicates("time").sort_values("time").tail(limit)
    df = pd.DataFrame({"open_time": d["time"].astype("int64"), "open": d["open"], "high": d["high"],
                       "low": d["low"], "close": d["close"], "volume": d["volume"]}).reset_index(drop=True)
    df["close_time"] = df["open_time"] + 15 * 60 * 1000 - 1
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["open_dt"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_dt"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    df = df[df["close_time"] < now_ms].reset_index(drop=True)
    return df


# ==============================================================================
# INDICATORS CALCULATION
# ==============================================================================
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    # 1. Bollinger Bands & Normalized Band Width
    df["bb_mid"] = df["close"].rolling(BB_LENGTH).mean()
    bb_std = df["close"].rolling(BB_LENGTH).std(ddof=0)
    df["bb_upper"] = df["bb_mid"] + (BB_STD * bb_std)
    df["bb_lower"] = df["bb_mid"] - (BB_STD * bb_std)
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]
    df["bb_width_median_100"] = df["bb_width"].rolling(BB_MEDIAN_LOOKBACK).median()

    # 2. EMA 20 & EMA 50
    df["ema_20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema_50"] = df["close"].ewm(span=50, adjust=False).mean()

    # 3. ATR (14)
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift(1)).abs()
    low_close = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr_14"] = tr.rolling(14).mean()

    # 4. Volume 20-Candle Moving Average
    df["vol_avg_20"] = df["volume"].rolling(20).mean()

    # 5. Daily Session VWAP (UTC day reset)
    df["utc_date"] = df["open_dt"].dt.date
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    df["tp_vol"] = typical_price * df["volume"]
    df["cum_tp_vol"] = df.groupby("utc_date")["tp_vol"].cumsum()
    df["cum_vol"] = df.groupby("utc_date")["volume"].cumsum()
    df["vwap"] = df["cum_tp_vol"] / df["cum_vol"]

    return df


# ==============================================================================
# STAGE 2: SESSION DETECTION & EFFECTIVE PDH/PDL + SESSION LEVELS
# ==============================================================================
def get_session_candidate_levels(df: pd.DataFrame, now_utc: datetime) -> list[dict]:
    """
    Teeno sessions ke liye effective levels return karta hai (priority order mein):
    - ASIA (00:00 - 07:00 UTC | 05:30 - 12:30 IST):
        Primary = PDH / PDL (jab aaj ki range already > 3.5% na phail chuki ho)
    - LONDON (07:00 - 13:00 UTC | 12:30 - 18:30 IST):
        1) PDH / PDL (high-conviction daily level)
        2) Asia High / Low (jab Asia range tight <= 3.5% ho)
    - NY (13:00 - 22:00 UTC | 18:30 - 03:30 IST):
        1) London High / Low (session breakout/continuation level)
        2) PDH / PDL (daily high/low level)
    """
    hour = now_utc.hour
    today = now_utc.date()
    yesterday = today - timedelta(days=1)

    today_df = df[df["utc_date"] == today]
    yest_df = df[df["utc_date"] == yesterday]

    pdh = float(yest_df["high"].max()) if not yest_df.empty else np.nan
    pdl = float(yest_df["low"].min()) if not yest_df.empty else np.nan
    has_pdh_pdl = not (np.isnan(pdh) or np.isnan(pdl))

    asia_df = today_df[(today_df["open_dt"].dt.hour >= 0) & (today_df["open_dt"].dt.hour < 7)]
    asia_high = float(asia_df["high"].max()) if not asia_df.empty else np.nan
    asia_low = float(asia_df["low"].min()) if not asia_df.empty else np.nan
    has_asia = not (np.isnan(asia_high) or np.isnan(asia_low))
    asia_range_pct = ((asia_high - asia_low) / asia_low) if (has_asia and asia_low > 0) else np.inf

    london_df = today_df[(today_df["open_dt"].dt.hour >= 7) & (today_df["open_dt"].dt.hour < 13)]
    london_high = float(london_df["high"].max()) if not london_df.empty else np.nan
    london_low = float(london_df["low"].min()) if not london_df.empty else np.nan
    has_london = not (np.isnan(london_high) or np.isnan(london_low))

    candidates = []

    if 0 <= hour < 7:
        # ASIA: Agar aaj ka intraday swing already 3.5% se zyada ho chuka hai to late breakout skip
        if not today_df.empty:
            day_high = float(today_df["high"].max())
            day_low = float(today_df["low"].min())
            if day_low > 0 and ((day_high - day_low) / day_low) > MAX_ASIA_RANGE_PCT:
                return []
        if has_pdh_pdl:
            candidates.append({
                "session": "ASIA",
                "ref_high": pdh,
                "ref_low": pdl,
                "level_name": "PDH / PDL"
            })

    elif 7 <= hour < 13:
        # LONDON: Pehle PDH/PDL check, phir tight Asia High/Low
        if has_pdh_pdl:
            candidates.append({
                "session": "LONDON",
                "ref_high": pdh,
                "ref_low": pdl,
                "level_name": "PDH / PDL (Daily)"
            })
        if has_asia and asia_range_pct <= MAX_ASIA_RANGE_PCT:
            candidates.append({
                "session": "LONDON",
                "ref_high": asia_high,
                "ref_low": asia_low,
                "level_name": "Asia High / Low"
            })

    elif 13 <= hour < 22:
        # NEW YORK: Pehle London High/Low, phir PDH/PDL
        if has_london:
            candidates.append({
                "session": "NEW YORK",
                "ref_high": london_high,
                "ref_low": london_low,
                "level_name": "London High / Low"
            })
        if has_pdh_pdl:
            candidates.append({
                "session": "NEW YORK",
                "ref_high": pdh,
                "ref_low": pdl,
                "level_name": "PDH / PDL (Daily)"
            })

    return candidates


# ==============================================================================
# EXIT RULE ENGINE: 1:2 -> 1:3 -> 1:4 -> 1:5 -> 1:6 PROFIT LOCK & FULL EXIT
# ==============================================================================
def compute_rr_price(entry: float, risk: float, direction: str, rr: float) -> float:
    if direction == "LONG":
        return entry + (rr * risk)
    return entry - (rr * risk)


def update_and_check_exit_rules(trade: dict, candle: pd.Series) -> tuple[dict | None, str | None]:
    """
    Aapka exact trailing exit rule:
    - Price >= 1:2 -> Profit lock at 1:2
    - Price >= 1:3 -> Profit lock at 1:3
    - Price >= 1:4 -> Profit lock at 1:4
    - Price >= 1:5 -> Profit lock at 1:5
    - Price >= 1:6 -> Full Exit at 1:6
    """
    direction = trade["direction"]
    entry = trade["entry"]
    risk = trade["risk"]
    current_sl = trade["current_sl"]
    locked_rr = trade.get("locked_rr", 0)

    tp_2 = compute_rr_price(entry, risk, direction, 2)
    tp_3 = compute_rr_price(entry, risk, direction, 3)
    tp_4 = compute_rr_price(entry, risk, direction, 4)
    tp_5 = compute_rr_price(entry, risk, direction, 5)
    tp_6 = compute_rr_price(entry, risk, direction, 6)

    candle_high = float(candle["high"])
    candle_low = float(candle["low"])

    # 1. Pehle dekhein ki kya 1:6 final exit target hit hua
    if direction == "LONG":
        if candle_high >= tp_6:
            return None, (
                f"✅ *FULL EXIT AT 1:6 TARGET* | `{trade['symbol']}` (LONG)\n"
                f"• *Entry:* `{entry}` | *Exit Price:* `{round(tp_6, 6)}` (+6R)"
            )
    else:  # SHORT
        if candle_low <= tp_6:
            return None, (
                f"✅ *FULL EXIT AT 1:6 TARGET* | `{trade['symbol']}` (SHORT)\n"
                f"• *Entry:* `{entry}` | *Exit Price:* `{round(tp_6, 6)}` (+6R)"
            )

    # 2. Tiered Profit Lock progression (1:2 -> 1:3 -> 1:4 -> 1:5)
    new_locked_rr = locked_rr
    new_sl = current_sl

    if direction == "LONG":
        if candle_high >= tp_5 and locked_rr < 5:
            new_locked_rr = 5
            new_sl = max(current_sl, tp_5)
        elif candle_high >= tp_4 and locked_rr < 4:
            new_locked_rr = 4
            new_sl = max(current_sl, tp_4)
        elif candle_high >= tp_3 and locked_rr < 3:
            new_locked_rr = 3
            new_sl = max(current_sl, tp_3)
        elif candle_high >= tp_2 and locked_rr < 2:
            new_locked_rr = 2
            new_sl = max(current_sl, tp_2)
    else:  # SHORT
        if candle_low <= tp_5 and locked_rr < 5:
            new_locked_rr = 5
            new_sl = min(current_sl, tp_5)
        elif candle_low <= tp_4 and locked_rr < 4:
            new_locked_rr = 4
            new_sl = min(current_sl, tp_4)
        elif candle_low <= tp_3 and locked_rr < 3:
            new_locked_rr = 3
            new_sl = min(current_sl, tp_3)
        elif candle_low <= tp_2 and locked_rr < 2:
            new_locked_rr = 2
            new_sl = min(current_sl, tp_2)

    lock_alert = None
    if new_locked_rr > locked_rr:
        trade["locked_rr"] = new_locked_rr
        trade["current_sl"] = round(new_sl, 6)
        lock_alert = (
            f"🔒 *PROFIT LOCKED AT 1:{new_locked_rr}* | `{trade['symbol']}` ({direction})\n"
            f"• *Entry:* `{entry}` | *New Protected SL:* `{trade['current_sl']}` (+{new_locked_rr}R)\n"
            f"• *Next Target:* `1:{min(new_locked_rr + 1, FINAL_EXIT_RR)}`"
        )

    # 3. Check karein ki kya current/locked SL hit hua
    if direction == "LONG":
        if candle_low <= trade["current_sl"]:
            status_label = f"PROFIT LOCK EXIT (1:{trade['locked_rr']})" if trade["locked_rr"] > 0 else "INITIAL SL EXIT"
            return None, (
                f"🛑 *{status_label}* | `{trade['symbol']}` (LONG)\n"
                f"• *Entry:* `{entry}` | *Exit SL:* `{trade['current_sl']}`"
            )
    else:  # SHORT
        if candle_high >= trade["current_sl"]:
            status_label = f"PROFIT LOCK EXIT (1:{trade['locked_rr']})" if trade["locked_rr"] > 0 else "INITIAL SL EXIT"
            return None, (
                f"🛑 *{status_label}* | `{trade['symbol']}` (SHORT)\n"
                f"• *Entry:* `{entry}` | *Exit SL:* `{trade['current_sl']}`"
            )

    return trade, lock_alert


def manage_open_trades():
    """Sabhi active trades ke naye closed candles par 1:2..1:6 lock aur exit rule check karta hai."""
    trades = load_active_trades()
    if not trades:
        return

    updated_trades = []
    for trade in trades:
        symbol = trade["symbol"]
        try:
            df = fetch_klines(symbol, limit=15)
            if df.empty:
                updated_trades.append(trade)
                continue

            last_processed_open = trade.get("last_processed_open_time", 0)
            new_candles = df[df["open_time"] > last_processed_open]

            still_open = True
            for _, row in new_candles.iterrows():
                trade["last_processed_open_time"] = int(row["open_time"])
                prev_lock = trade.get("locked_rr", 0)
                updated_trade, alert_msg = update_and_check_exit_rules(trade, row)
                if alert_msg:
                    send_telegram_alert(alert_msg)
                if updated_trade is None:
                    # paper log: 1:6 target = +6R, else exit at current SL (locked R, or -1R at the initial SL)
                    if "1:6" in (alert_msg or ""):
                        log_trade("EXIT", trade, "FULL_EXIT_1:6", 6.0)
                    else:
                        lr = trade.get("locked_rr", 0)
                        if lr > 0:
                            log_trade("EXIT", trade, f"PROFIT_LOCK_EXIT_1:{lr}", float(lr))
                        else:
                            sgn = 1 if trade["direction"] == "LONG" else -1
                            log_trade("EXIT", trade, "INITIAL_SL_EXIT",
                                      sgn * (trade["current_sl"] - trade["entry"]) / trade["risk"])
                    still_open = False
                    break
                if trade.get("locked_rr", 0) > prev_lock:
                    log_trade("LOCK", trade, f"PROFIT_LOCKED_1:{trade['locked_rr']}")
                trade = updated_trade

            if still_open:
                updated_trades.append(trade)
        except Exception as e:
            print(f"[Exit Monitor Error {symbol}] {e}")
            updated_trades.append(trade)
        time.sleep(0.1)

    save_active_trades(updated_trades)


# ==============================================================================
# STAGE 3: COMBINED FAKE BREAKOUT FILTER & SIGNAL ENGINE
# ==============================================================================
def evaluate_symbol(symbol: str, vol_24h: float, now_utc: datetime) -> dict | None:
    df = fetch_klines(symbol)
    if len(df) < 150:
        return None

    df = add_indicators(df)

    curr = df.iloc[-1]   # Latest closed breakout candle (t)
    prev = df.iloc[-2]   # Pre-breakout candle (t-1)

    # Stage 1 Check: Minimum ATR volatility
    if pd.isna(curr["atr_14"]) or (curr["atr_14"] / curr["close"]) < MIN_ATR_PCT:
        return None

    # Stage 2 Check: Session-wise candidate levels (PDH/PDL + Session High/Low)
    candidates = get_session_candidate_levels(df, now_utc)
    if not candidates:
        return None

    # RULE 1: BB SQUEEZE RULE (t-1 candle BB width < pichhli 100 candles ka median)
    if pd.isna(prev["bb_width"]) or pd.isna(prev["bb_width_median_100"]):
        return None
    if not (prev["bb_width"] < prev["bb_width_median_100"]):
        return None

    # RULE 2: STRICT CANDLE CLOSE CONFIRMATION (Priority order mein candidate levels check)
    matched_level = None
    direction = None
    for lvl in candidates:
        ref_high = lvl["ref_high"]
        ref_low = lvl["ref_low"]
        if curr["close"] > ref_high and prev["close"] <= ref_high:
            direction = "LONG"
            matched_level = lvl
            break
        elif curr["close"] < ref_low and prev["close"] >= ref_low:
            direction = "SHORT"
            matched_level = lvl
            break

    if direction is None or matched_level is None:
        return None

    # RULE 3: STRONG BODY-TO-WICK RATIO & REJECTION FILTER
    candle_range = float(curr["high"]) - float(curr["low"])
    if candle_range <= 0:
        return None

    body_size = abs(float(curr["close"]) - float(curr["open"]))
    body_ratio = body_size / candle_range

    upper_wick = float(curr["high"]) - max(float(curr["open"]), float(curr["close"]))
    lower_wick = min(float(curr["open"]), float(curr["close"])) - float(curr["low"])

    if body_ratio < MIN_BODY_RATIO:
        return None
    if direction == "LONG" and (upper_wick / candle_range) > MAX_REJECTION_WICK_RATIO:
        return None
    if direction == "SHORT" and (lower_wick / candle_range) > MAX_REJECTION_WICK_RATIO:
        return None

    # RULE 4: VOLUME EXPANSION CHECK (>= 1.4x 20-candle average volume)
    if pd.isna(curr["vol_avg_20"]) or curr["vol_avg_20"] <= 0:
        return None
    vol_ratio = float(curr["volume"]) / float(curr["vol_avg_20"])
    if vol_ratio < VOLUME_MULTIPLIER:
        return None

    # RULE 5: TREND & VWAP ALIGNMENT
    if direction == "LONG":
        if not (curr["close"] > curr["vwap"] and curr["close"] > curr["ema_20"] and curr["close"] > curr["ema_50"]):
            return None
    else:
        if not (curr["close"] < curr["vwap"] and curr["close"] < curr["ema_20"] and curr["close"] < curr["ema_50"]):
            return None

    # GUARDRAIL: NO CHASING RULE
    if candle_range > (MAX_CANDLE_ATR_MULT * float(curr["atr_14"])):
        return None

    # FUTURES RISK & TIERED R:R LEVELS (1:2, 1:3, 1:4, 1:5, 1:6 Exit)
    entry_price = float(curr["close"])
    atr_buffer = 0.25 * float(curr["atr_14"])

    if direction == "LONG":
        initial_sl = float(curr["low"]) - atr_buffer
        risk = entry_price - initial_sl
        if risk <= 0:
            return None
        tp_levels = {f"1:{r}": round(compute_rr_price(entry_price, risk, direction, r), 6) for r in RR_TIERS}
        broken_price = matched_level["ref_high"]
    else:
        initial_sl = float(curr["high"]) + atr_buffer
        risk = initial_sl - entry_price
        if risk <= 0:
            return None
        tp_levels = {f"1:{r}": round(compute_rr_price(entry_price, risk, direction, r), 6) for r in RR_TIERS}
        broken_price = matched_level["ref_low"]

    return {
        "symbol": symbol,
        "session": matched_level["session"],
        "level_name": matched_level["level_name"],
        "ref_level": round(broken_price, 6),
        "direction": direction,
        "entry": round(entry_price, 6),
        "initial_sl": round(initial_sl, 6),
        "risk": round(risk, 6),
        "targets": tp_levels,
        "vol_24h_m": round(vol_24h / 1_000_000, 1),
        "vol_spike": round(vol_ratio, 2),
        "body_pct": round(body_ratio * 100, 1),
        "bb_width_prev": round(float(prev["bb_width"]), 5),
        "bb_median_100": round(float(prev["bb_width_median_100"]), 5),
        "candle_open_time": int(curr["open_time"]),
        "candle_time_utc": curr["open_dt"].strftime("%Y-%m-%d %H:%M"),
    }


# ==============================================================================
# MAIN SCANNER RUNNER
# ==============================================================================
def run_scanner():
    now_utc = datetime.now(timezone.utc)
    ist_time = now_utc + timedelta(hours=5, minutes=30)
    print(f"\n=== Scan Started | UTC: {now_utc:%Y-%m-%d %H:%M} | IST: {ist_time:%Y-%m-%d %H:%M} ===")

    # Pehle existing active trades par 1:2 -> 1:3 -> 1:4 -> 1:5 -> 1:6 exit logic check karein
    manage_open_trades()

    # Stage 1: Macro Event Check (naya breakout entry skip karne ke liye)
    is_news, news_reason = is_high_impact_news_window(now_utc)
    if is_news:
        print(f"[NEW ENTRIES SKIPPED] {news_reason}")
        return

    # Stage 1: Liquid USDT Futures Coins Selection
    coins_map = get_liquid_usdt_futures_pairs()
    print(f"Stage 1 Passed: {len(coins_map)} Futures coins with 24h Volume >= $100M")

    active_trades = load_active_trades()
    open_symbols = {t["symbol"] for t in active_trades}

    signals_found = 0
    for symbol, coin in coins_map.items():
        if symbol in open_symbols:
            continue
        try:
            signal = evaluate_symbol(symbol, coin["volume_24h"], now_utc)
            if signal:
                signals_found += 1
                active_trades.append({
                    "symbol": signal["symbol"],
                    "direction": signal["direction"],
                    "entry": signal["entry"],
                    "initial_sl": signal["initial_sl"],
                    "current_sl": signal["initial_sl"],
                    "risk": signal["risk"],
                    "locked_rr": 0,
                    "last_processed_open_time": signal["candle_open_time"],
                    "session": signal["session"],
                    "level_name": signal["level_name"],
                    "ref_level": signal["ref_level"],
                    "entry_candle_utc": signal["candle_time_utc"],
                })
                open_symbols.add(symbol)
                log_trade("ENTRY", active_trades[-1], f"{signal['direction']}_BREAKOUT")

                msg = (
                    f"🚨 *FUTURES BREAKOUT SIGNAL* ({signal['session']} Session)\n"
                    f"• *Pair:* `{signal['symbol']}` | *Side:* *{signal['direction']}*\n"
                    f"• *Breakout Level ({signal['level_name']}):* `{signal['ref_level']}`\n"
                    f"• *Entry:* `{signal['entry']}` | *Initial SL:* `{signal['initial_sl']}`\n"
                    f"• *Profit Lock Ladder:* 1:2=`{signal['targets']['1:2']}` | 1:3=`{signal['targets']['1:3']}` | "
                    f"1:4=`{signal['targets']['1:4']}` | 1:5=`{signal['targets']['1:5']}`\n"
                    f"• *Full Exit (1:6):* `{signal['targets']['1:6']}`\n"
                    f"• *BB Squeeze (t-1):* `{signal['bb_width_prev']}` < Median `{signal['bb_median_100']}`\n"
                    f"• *Confirmation:* Body `{signal['body_pct']}%` | Vol `{signal['vol_spike']}x` | "
                    f"24h Vol `${signal['vol_24h_m']}M`\n"
                    f"• *Candle (UTC):* `{signal['candle_time_utc']}`"
                )
                send_telegram_alert(msg)
        except Exception as e:
            print(f"[Error {symbol}] {e}")
        time.sleep(0.15)

    save_active_trades(active_trades)
    if signals_found == 0:
        print("No new valid breakout passed all 3 stages in this scan.")


def report():
    if not os.path.exists(TRADES_LOG_FILE):
        print("No trades yet.")
        return
    d = pd.read_csv(TRADES_LOG_FILE)
    x = d[d["Kind"] == "EXIT"]
    print(f"Entries: {(d['Kind'] == 'ENTRY').sum()} | Exits: {len(x)} | Open: {len(load_active_trades())}")
    if len(x):
        print(f"Wins: {(x['R'] > 0).sum()} | Win %: {100 * (x['R'] > 0).mean():.1f} | Net R: {x['R'].sum():.2f} | "
              f"PnL @10 USDT/trade (no fees): {x['PnL_USDT_10'].sum():.3f}")
        print(x.groupby("Status")["R"].agg(["count", "sum"]).to_string())


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    else:
        run_scanner()