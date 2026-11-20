"""
expert_crypto_strategy.py - Multi-Confluence Crypto Futures Strategy & Backtest/Scanner

Combines:
1. Key Levels: Previous Day High (PDH) & Previous Day Low (PDL)
2. Trend Filter: EMA 20 & EMA 50 + Intraday VWAP
3. Volume Filter: Current candle volume > 1.3x 20-period SMA volume
4. Momentum: RSI (14) + MACD (12, 26, 9)
5. Anti-FOMO Entry Guard: Max distance from breakout level (default 0.5% - 1.0%)
6. Risk & Exit Engine:
   - Hard Stop Loss Cap (max loss protection, never lets a lagging indicator drag loss)
   - Tiered Trailing Profit Lock (1:2, 1:3, 1:4 R:R)
   - Target Exit at 1:5 R:R or RSI exhaustion + MACD reversal
"""

import os
import time
import requests
import pandas as pd
import numpy as np

# ==============================================================================
# CONFIGURATION
# ==============================================================================
CANDLE_URL = "https://public.coindcx.com/market_data/candlesticks"
TICKER_URL = "https://api.coindcx.com/exchange/ticker"

# Filter & Indicator Parameters
EMA_FAST = 20
EMA_SLOW = 50
RSI_PERIOD = 14
VOL_MA_PERIOD = 20
VOL_MULTIPLIER = 1.3           # Volume must be at least 1.3x average

# Entry & Risk Parameters
MAX_BREAKOUT_DIST_PCT = 0.75   # Anti-FOMO: Skip if entry is > 0.75% past PDH/PDL
MIN_RISK_PCT = 0.40            # Minimum stop distance (%)
MAX_RISK_PCT = 2.00            # Hard Stop Loss cap (%)
TARGET_RR = 5.0                # Final target R:R
LOCK_LEVELS = [2.0, 3.0, 4.0]  # Trailing profit lock milestones

# Telegram Alert Config (Optional via Env Vars)
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ==============================================================================
# DATA FETCHING (CoinDCX Futures)
# ==============================================================================
def fetch_candles(pair: str, resolution: str = "15", lookback_hours: int = 120) -> pd.DataFrame:
    """Fetch historical futures candles from CoinDCX."""
    now = int(time.time())
    start = now - (lookback_hours * 3600)
    params = {
        "pair": pair,
        "from": start,
        "to": now,
        "resolution": resolution,
        "pcode": "f",
    }
    resp = requests.get(CANDLE_URL, params=params, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    raw = payload.get("data", payload.get("candles", [])) if isinstance(payload, dict) else payload
    if not raw:
        return pd.DataFrame()

    df = pd.DataFrame(raw)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["Timestamp"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    df = df.sort_values("Timestamp").drop_duplicates("Timestamp").reset_index(drop=True)
    return df


# ==============================================================================
# TECHNICAL INDICATORS & KEY LEVELS
# ==============================================================================
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute EMA, VWAP, RSI, MACD, Volume MA, and Previous Day High/Low (PDH/PDL)."""
    df = df.copy()
    df["Date"] = df["Timestamp"].dt.date

    # 1. EMAs
    df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    # 2. Intraday VWAP (resets daily UTC)
    typical_px = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = typical_px * df["volume"]
    df["cum_pv"] = pv.groupby(df["Date"]).cumsum()
    df["cum_vol"] = df["volume"].groupby(df["Date"]).cumsum()
    df["vwap"] = df["cum_pv"] / df["cum_vol"].replace(0, np.nan)

    # 3. Volume Moving Average
    df["vol_ma"] = df["volume"].rolling(VOL_MA_PERIOD).mean()

    # 4. RSI (14)
    delta = df["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100.0 - (100.0 / (1.0 + rs))

    # 5. MACD (12, 26, 9)
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    # 6. Previous Day High / Low (PDH / PDL)
    daily = df.groupby("Date").agg(day_high=("high", "max"), day_low=("low", "min")).shift(1)
    df = df.merge(daily.rename(columns={"day_high": "pdh", "day_low": "pdl"}), on="Date", how="left")

    return df


# ==============================================================================
# ENTRY SIGNAL CHECK (Multi-Confluence + Anti-FOMO Guard)
# ==============================================================================
def check_entry_signal(row: pd.Series, prev_row: pd.Series) -> dict | None:
    """
    Evaluate a closed candle for BUY or SELL setup.
    Returns trade setup dict or None.
    """
    if pd.isna(row["pdh"]) or pd.isna(row["pdl"]) or pd.isna(row["ema_slow"]) or pd.isna(row["rsi"]):
        return None

    close = float(row["close"])
    vol_ok = row["volume"] > (row["vol_ma"] * VOL_MULTIPLIER)
    if not vol_ok:
        return None

    # --- BUY SETUP ---
    pdh = float(row["pdh"])
    dist_above_pdh_pct = ((close - pdh) / pdh) * 100.0
    buy_breakout = (prev_row["close"] <= pdh) and (close > pdh)
    buy_anti_fomo = 0.0 < dist_above_pdh_pct <= MAX_BREAKOUT_DIST_PCT
    buy_trend = (close > row["vwap"]) and (row["ema_fast"] > row["ema_slow"])
    buy_momentum = (52.0 <= row["rsi"] <= 72.0) and (row["macd"] > row["macd_signal"]) and (row["macd_hist"] > 0)

    if buy_breakout and buy_anti_fomo and buy_trend and buy_momentum:
        raw_sl = min(float(row["low"]), float(row["ema_fast"]))
        risk_pct = max(MIN_RISK_PCT, min(MAX_RISK_PCT, ((close - raw_sl) / close) * 100.0))
        sl = close * (1.0 - risk_pct / 100.0)
        risk = close - sl
        return {
            "direction": "BUY",
            "entry_ts": str(row["Timestamp"]),
            "entry_price": close,
            "sl": sl,
            "initial_sl": sl,
            "risk": risk,
            "locked_rr": 0.0,
            "target": close + (TARGET_RR * risk),
            "reason": f"PDH Breakout (+{dist_above_pdh_pct:.2f}%), VWAP+EMA Bullish, RSI {row['rsi']:.1f}, MACD+",
        }

    # --- SELL SETUP ---
    pdl = float(row["pdl"])
    dist_below_pdl_pct = ((pdl - close) / pdl) * 100.0
    sell_breakout = (prev_row["close"] >= pdl) and (close < pdl)
    sell_anti_fomo = 0.0 < dist_below_pdl_pct <= MAX_BREAKOUT_DIST_PCT
    sell_trend = (close < row["vwap"]) and (row["ema_fast"] < row["ema_slow"])
    sell_momentum = (28.0 <= row["rsi"] <= 48.0) and (row["macd"] < row["macd_signal"]) and (row["macd_hist"] < 0)

    if sell_breakout and sell_anti_fomo and sell_trend and sell_momentum:
        raw_sl = max(float(row["high"]), float(row["ema_fast"]))
        risk_pct = max(MIN_RISK_PCT, min(MAX_RISK_PCT, ((raw_sl - close) / close) * 100.0))
        sl = close * (1.0 + risk_pct / 100.0)
        risk = sl - close
        return {
            "direction": "SELL",
            "entry_ts": str(row["Timestamp"]),
            "entry_price": close,
            "sl": sl,
            "initial_sl": sl,
            "risk": risk,
            "locked_rr": 0.0,
            "target": close - (TARGET_RR * risk),
            "reason": f"PDL Breakdown (-{dist_below_pdl_pct:.2f}%), VWAP+EMA Bearish, RSI {row['rsi']:.1f}, MACD-",
        }

    return None


# ==============================================================================
# TRADE MANAGEMENT & EXIT ENGINE (Hard SL + Tiered Lock + Exhaustion Exit)
# ==============================================================================
def manage_open_trade(row: pd.Series, trade: dict) -> tuple[str, float, dict | None]:
    """
    Update an open trade on each candle:
    1. Hard Stop Loss / Locked Stop check (immediate protection)
    2. Target 1:5 R:R check
    3. Tiered Trailing Profit Lock (1:2, 1:3, 1:4)
    4. Momentum Exhaustion Exit (if in profit > 1R and RSI + MACD flip against trade)
    """
    is_buy = trade["direction"] == "BUY"
    entry = trade["entry_price"]
    risk = trade["risk"]
    sl = trade["sl"]
    hi = float(row["high"])
    lo = float(row["low"])
    close = float(row["close"])

    # 1. Check Hard SL or Locked Stop Hit
    if is_buy and lo <= sl:
        pnl_r = (sl - entry) / risk
        label = "LOCKED_SL_EXIT" if trade["locked_rr"] > 0 else "HARD_SL_EXIT"
        return label, round(pnl_r, 2), None
    if not is_buy and hi >= sl:
        pnl_r = (entry - sl) / risk
        label = "LOCKED_SL_EXIT" if trade["locked_rr"] > 0 else "HARD_SL_EXIT"
        return label, round(pnl_r, 2), None

    # 2. Check Final Target (1:5 R:R)
    best_r = ((hi - entry) / risk) if is_buy else ((entry - lo) / risk)
    if best_r >= TARGET_RR:
        return "TARGET_1:5_EXIT", TARGET_RR, None

    # 3. Tiered Profit Lock (1:2 -> 1:3 -> 1:4)
    for lvl in LOCK_LEVELS:
        if best_r >= lvl and trade["locked_rr"] < lvl:
            trade["locked_rr"] = lvl
            trade["sl"] = (entry + lvl * risk) if is_buy else (entry - lvl * risk)

    # 4. Momentum Exhaustion Exit (protect open profit if RSI extreme + MACD flips)
    open_r = ((close - entry) / risk) if is_buy else ((entry - close) / risk)
    if open_r >= 1.5:
        if is_buy and row["rsi"] > 78 and row["macd_hist"] < 0:
            return "EXHAUSTION_PROFIT_EXIT", round(open_r, 2), None
        if not is_buy and row["rsi"] < 22 and row["macd_hist"] > 0:
            return "EXHAUSTION_PROFIT_EXIT", round(open_r, 2), None

    return "HOLD", round(open_r, 2), trade


# ==============================================================================
# BACKTEST & LIVE SCANNER RUNNER
# ==============================================================================
def run_backtest(pair: str = "B-BTC_USDT", lookback_hours: int = 720):
    """Run a historical simulation on CoinDCX 15m candles."""
    df = fetch_candles(pair, resolution="15", lookback_hours=lookback_hours)
    if df.empty:
        print(f"No data fetched for {pair}")
        return

    df = add_indicators(df)
    active_trade = None
    history = []

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev_row = df.iloc[i - 1]

        if active_trade is not None:
            status, pnl_r, active_trade = manage_open_trade(row, active_trade)
            if active_trade is None:
                history.append({
                    "pair": pair,
                    "exit_ts": str(row["Timestamp"]),
                    "status": status,
                    "pnl_r": pnl_r,
                })
        else:
            signal = check_entry_signal(row, prev_row)
            if signal:
                active_trade = signal

    res = pd.DataFrame(history)
    print(f"=== Backtest Summary: {pair} ({lookback_hours}h) ===")
    if res.empty:
        print("No trades triggered with strict multi-confluence filters.")
    else:
        total_r = res["pnl_r"].sum()
        win_rate = (res["pnl_r"] > 0).mean() * 100
        print(f"Total Trades : {len(res)}")
        print(f"Win Rate     : {win_rate:.1f}%")
        print(f"Net Return   : {total_r:+.2f} R")
        print(res.tail(10).to_string(index=False))


def send_telegram(msg: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TG_CHAT_ID, "text": msg}, timeout=10)


def scan_pairs(pairs: list[str]):
    """Scan latest closed 15m candle across multiple pairs and alert on signals."""
    for pair in pairs:
        try:
            df = fetch_candles(pair, resolution="15", lookback_hours=72)
            if len(df) < 60:
                continue
            df = add_indicators(df)
            # Use -2 as the latest fully closed 15m candle
            sig = check_entry_signal(df.iloc[-2], df.iloc[-3])
            if sig:
                msg = (
                    f"[EXPERT SIGNAL] {sig['direction']} {pair}\n"
                    f"Entry: {sig['entry_price']:.6g} | Hard SL: {sig['sl']:.6g} | Target: {sig['target']:.6g}\n"
                    f"Reason: {sig['reason']}"
                )
                print(msg)
                send_telegram(msg)
            else:
                print(f"{pair}: No setup on latest closed candle.")
        except Exception as e:
            print(f"{pair}: Error -> {e}")


if __name__ == "__main__":
    WATCHLIST = ["B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT", "B-BNB_USDT"]
    # 1. Quick backtest on BTC (30 days = 720 hours)
    run_backtest("B-BTC_USDT", lookback_hours=720)
    print("-" * 50)
    # 2. Scan latest closed candle across watchlist
    scan_pairs(WATCHLIST)
