import time
from datetime import datetime, time as dtime
import pytz
import pandas as pd
import numpy as np
import ta

# ==========================================
# CONFIGURATION & PARAMETERS
# ==========================================
SYMBOL = "XAUUSDT"  # Ya PAXGUSDT / XAUUSDT futures on CoinDCX
EXCHANGE = "CoinDCX"
TIMEFRAME_5M = "5m"
TIMEFRAME_15M = "15m"
TIMEFRAME_1H = "1h"

CAPITAL = 1000.0  # Apni capital yahan set karein
MAX_RISK_PER_TRADE = 0.01  # 1% risk per trade
LEVERAGE = 5

IST = pytz.timezone("Asia/Kolkata")


# ==========================================
# 1. TIMING & SESSION FILTERS
# ==========================================
def check_trading_timing() -> tuple[bool, str]:
  """Checks if current time falls within London or NY sessions (IST) and avoids weekends."""
  now_utc = datetime.now(pytz.utc)
  now_ist = now_utc.astimezone(IST)

  # Check Weekend (Saturday / Sunday)
  if now_ist.weekday() >= 5:
    return False, "Weekend: Market Closed"

  current_time = now_ist.time()

  # New York Session (BEST): 6:30 PM - 10:30 PM IST (Full Size)
  ny_start = dtime(18, 30)
  ny_end = dtime(22, 30)

  # London Session (Medium): 1:30 PM - 4:30 PM IST (Half Size)
  london_start = dtime(13, 30)
  london_end = dtime(16, 30)

  if ny_start <= current_time <= ny_end:
    return True, "NY_SESSION"
  elif london_start <= current_time <= london_end:
    return True, "LONDON_SESSION"
  else:
    return False, "Outside Active Trading Sessions / Asian Session"


# ==========================================
# 7. HIGH-IMPACT NEWS FILTER
# ==========================================
def check_news_window() -> bool:
  """Skips trades 15-30 mins around major US data releases (CPI, PPI, NFP, FOMC)."""
  now_ist = datetime.now(pytz.utc).astimezone(IST)
  current_time = now_ist.time()

  # Approximate high-impact news windows in IST (e.g., ~7:00 PM and ~12:30 AM)
  news_times = [
      dtime(18, 45),
      dtime(19, 15),  # Around 7:00 PM CPI/NFP window
      dtime(23, 45),
      dtime(
          0, 30
      ),  # Around 12:30 AM FOMC window (Note: handles next day roughly)
  ]

  # Check if within 30 minutes of any high-impact slot
  for n_time in news_times:
    # Convert times to minutes for easy comparison
    curr_min = current_time.hour * 60 + current_time.minute
    news_min = n_time.hour * 60 + n_time.minute
    if abs(curr_min - news_min) <= 30:
      return True  # True means News Window is active -> DO NOT TRADE

  return False


# ==========================================
# 2. TREND FILTERS & INDICATORS
# ==========================================
def calculate_indicators(df_5m, df_15m, df_1h):
  """Computes all technical indicators required for strategy filters."""
  # 5-Min Indicators
  df_5m["ema_200"] = ta.trend.ema_indicator(df_5m["close"], window=200)
  df_5m["adx"] = ta.trend.adx(
      df_5m["high"], df_5m["low"], df_5m["close"], window=14
  )
  df_5m["atr"] = ta.volatility.average_true_range(
      df_5m["high"], df_5m["low"], df_5m["close"], window=14
  )
  df_5m["vol_sma"] = df_5m["volume"].rolling(window=20).mean()
  df_5m["body_pct"] = (
      abs(df_5m["close"] - df_5m["open"])
      / (df_5m["high"] - df_5m["low"] + 1e-9)
  ) * 100

  # Bollinger Bands (20, 2) & Bandwidth for Setup B
  bb = ta.volatility.BollingerBands(df_5m["close"], window=20, window_dev=2)
  df_5m["bb_upper"] = bb.bollinger_hband()
  df_5m["bb_lower"] = bb.bollinger_lband()
  df_5m["bb_width"] = bb.bollinger_wband()
  df_5m["bb_width_percentile"] = df_5m["bb_width"].rolling(window=100).apply(
      lambda x: pd.Series(x).rank(pct=True).iloc[-1]
  )

  # Session VWAP approximation
  df_5m["vwap"] = (
      (df_5m["volume"] * (df_5m["high"] + df_5m["low"] + df_5m["close"]) / 3)
      .cumsum()
      / df_5m["volume"].cumsum()
  )

  # 15-Min Confirmation Indicator
  df_15m["ema_200"] = ta.trend.ema_indicator(df_15m["close"], window=200)

  # 1H Trend Filter
  df_1h["ema_50"] = ta.trend.ema_indicator(df_1h["close"], window=50)
  df_1h["ema_200"] = ta.trend.ema_indicator(df_1h["close"], window=200)

  return df_5m, df_15m, df_1h


def verify_trend_filters(df_5m, df_15m, df_1h, direction):
  """Validates all 5 conditions in Section 2."""
  last_5m = df_5m.iloc[-1]
  last_15m = df_15m.iloc[-1]
  last_1h = df_1h.iloc[-1]

  # ADX Filter (> 20)
  if last_5m["adx"] < 20:
    return False

  if direction == "BUY":
    # 1. EMA 200 (5m)
    if last_5m["close"] <= last_5m["ema_200"]:
      return False
    # 2. 15-Min Confirmation
    if (
        last_15m["close"] <= last_15m["ema_200"]
        or last_15m["close"] <= last_15m["open"]
    ):
      return False
    # 3. 1H Trend (EMA 50 > EMA 200)
    if last_1h["ema_50"] <= last_1h["ema_200"]:
      return False
    # 4. VWAP Filter
    if last_5m["close"] <= last_5m["vwap"]:
      return False

  elif direction == "SELL":
    # 1. EMA 200 (5m)
    if last_5m["close"] >= last_5m["ema_200"]:
      return False
    # 2. 15-Min Confirmation
    if (
        last_15m["close"] >= last_15m["ema_200"]
        or last_15m["close"] >= last_15m["open"]
    ):
      return False
    # 3. 1H Trend (EMA 50 < EMA 200)
    if last_1h["ema_50"] >= last_1h["ema_200"]:
      return False
    # 4. VWAP Filter
    if last_5m["close"] >= last_5m["vwap"]:
      return False

  return True


# ==========================================
# 3 & 4. SETUP A & SETUP B LOGIC
# ==========================================
def check_setups(df_5m):
  """Identifies Setup A (Volume Profile Breakout) or Setup B (Bollinger Squeeze)."""
  prev = df_5m.iloc[-2]  # Breakout candle
  curr = df_5m.iloc[-1]  # Current confirmation candle

  # Common Volume & Body constraints
  valid_candle = (prev["volume"] > 1.5 * prev["vol_sma"]) and (
      prev["body_pct"] >= 60.0
  )
  if not valid_candle:
    return None

  # --- SETUP B: Bollinger Squeeze Breakout ---
  is_squeeze = (
      prev["bb_width_percentile"] <= 0.20
  )  # Bottom 20% bandwidth of last 100 candles

  if is_squeeze:
    # LONG Setup B
    if (
        prev["close"] > prev["bb_upper"]
        and curr["high"] > prev["high"]
        and verify_trend_filters(df_5m, df_15m_global, df_1h_global, "BUY")
    ):
      return "BUY_SETUP_B"
    # SHORT Setup B
    if (
        prev["close"] < prev["bb_lower"]
        and curr["low"] < prev["low"]
        and verify_trend_filters(df_5m, df_15m_global, df_1h_global, "SELL")
    ):
      return "SELL_SETUP_B"

  # --- SETUP A: Volume Profile Breakout (Simplified proxy via POC/VAH/VAL) ---
  # Assuming VAH/VAL calculated externally or via rolling bounds
  vah = df_5m["high"].rolling(288).quantile(0.70).iloc[-2]
  val = df_5m["low"].rolling(288).quantile(0.30).iloc[-2]

  # LONG Setup A
  if (
      prev["close"] > vah
      and curr["high"] > prev["high"]
      and verify_trend_filters(df_5m, df_15m_global, df_1h_global, "BUY")
  ):
    return "BUY_SETUP_A"
  # SHORT Setup A
  if (
      prev["close"] < val
      and curr["low"] < prev["low"]
      and verify_trend_filters(df_5m, df_15m_global, df_1h_global, "SELL")
  ):
    return "SELL_SETUP_A"

  return None


# ==========================================
# 5 & 6. RISK MANAGEMENT & EXECUTION SIMULATOR
# ==========================================
def calculate_position_size(entry_price, sl_price):
  """Calculates position size based on max 1% risk rule and 5x leverage constraint."""
  risk_amount = CAPITAL * MAX_RISK_PER_TRADE
  sl_distance = abs(entry_price - sl_price)

  if sl_distance == 0:
    return 0

  size_by_risk = risk_amount / sl_distance
  max_leverage_size = (CAPITAL * LEVERAGE) / entry_price

  # Minimum of risk-based size and max leverage size
  final_size = min(size_by_risk, max_leverage_size)
  return round(final_size, 4)


def execute_coindcx_order(signal, current_price, atr_value):
  """Places the order on CoinDCX futures following checklist rules."""
  is_time_valid, session_name = check_trading_timing()
  if not is_time_valid:
    print(f"[{session_name}] Skipping trade: Outside allowed window.")
    return

  if check_news_window():
    print("[NEWS FILTER] Skipping trade: High-impact news window active.")
    return

  if signal is None:
    return

  # Define Stop-Loss (1.5 * ATR)
  if "BUY" in signal:
    sl_price = current_price - (1.5 * atr_value)
    position_size = calculate_position_size(current_price, sl_price)
    print(
        f"--- COINDCX {signal} EXECUTION --- | Price: {current_price} | SL:"
        f" {sl_price:.2f} | Size: {position_size}"
    )
    # TODO: Add live CoinDCX API order placement call here

  elif "SELL" in signal:
    sl_price = current_price + (1.5 * atr_value)
    position_size = calculate_position_size(current_price, sl_price)
    print(
        f"--- COINDCX {signal} EXECUTION --- | Price: {current_price} | SL:"
        f" {sl_price:.2f} | Size: {position_size}"
    )
    # TODO: Add live CoinDCX API order placement call here


# ==========================================
# MAIN LOOP (Runs every 5 minutes)
# ==========================================
if __name__ == "__main__":
  print(
      f"Starting CoinDCX Automated Strategy for {SYMBOL} based on GOLD_FINAL_STRATEGY v2..."
  )

  # Placeholder global variables for multi-timeframe frames in live loop
  global df_15m_global, df_1h_global

  while True:
    try:
      # 1. Fetch live OHLCV candles from exchange (CoinDCX API integration point)
      # df_5m = fetch_coindcx_candles(SYMBOL, '5m')
      # df_15m = fetch_coindcx_candles(SYMBOL, '15m')
      # df_1h = fetch_coindcx_candles(SYMBOL, '1H')

      # Mock structure for syntax validation purposes:
      pass

      time.sleep(300)  # Check every 5 minutes
    except KeyboardInterrupt:
      print("Bot stopped by user.")
      break
    except Exception as e:
      print(f"Error encountered: {e}")
      time.sleep(30)