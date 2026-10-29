import os
import time
import requests
import pandas as pd
import pandas_ta as ta

# GitHub Secrets se credentials uthayega
API_KEY = os.getenv('COINDCX_API_KEY')
SECRET_KEY = os.getenv('COINDCX_SECRET_KEY')

SYMBOL = "B-BTC_USDT"          # CoinDCX Futures Symbol
TIMEFRAME = "15m"              # 15-Minute Timeframe
BASE_URL = "https://public.coindcx.com"

def fetch_candles():
    url = f"{BASE_URL}/market_data/candles?pair={SYMBOL}&interval={TIMEFRAME}&limit=100"
    response = requests.get(url)
    data = response.json()
    df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['close'] = df['close'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['volume'] = df['volume'].astype(float)
    return df

def apply_indicators(df):
    # EMA & ADX Calculation
    df['ema_20'] = ta.ema(df['close'], length=20)
    df['ema_50'] = ta.ema(df['close'], length=50)
    
    adx_df = ta.adx(df['high'], df['low'], df['close'], length=14)
    df['adx'] = adx_df['ADX_14']
    df['plus_di'] = adx_df['DMP_14']
    df['minus_di'] = adx_df['DMN_14']
    return df

def check_signal(df):
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    
    is_strong_trend = latest['adx'] > 20
    is_bullish_di = latest['plus_di'] > latest['minus_di']
    is_bearish_di = latest['minus_di'] > latest['plus_di']
    
    # Bullish Signal (EMA 20 Pullback + EMA 50 Trend Filter + ADX > 20)
    if is_strong_trend and is_bullish_di and latest['close'] > latest['ema_50']:
        if prev['close'] <= prev['ema_20'] and latest['close'] > latest['ema_20']:
            return "BUY"
            
    # Bearish Signal
    elif is_strong_trend and is_bearish_di and latest['close'] < latest['ema_50']:
        if prev['close'] >= prev['ema_20'] and latest['close'] < latest['ema_20']:
            return "SELL"
            
    return "HOLD"

def manage_risk_and_exits(entry_price, current_price, position_type, initial_sl, trade_state):
    """
    Aapka Profit-Locking Logic:
    - 1:2 pahunch kar wapas aaye -> Exit at 1:2 lock
    - 1:3 pahunch kar wapas aaye -> Exit at 1:3 lock
    - 1:4 pahunch jaye -> Final Exit
    """
    risk_unit = abs(entry_price - initial_sl)
    
    if position_type == "BUY":
        pips_gained = current_price - entry_price
        current_units = pips_gained / risk_unit
        
        if current_units > trade_state['max_units']:
            trade_state['max_units'] = current_units
            
        if trade_state['max_units'] >= 4.0 or current_units >= 4.0:
            return "EXIT_FINAL_1_4"
        elif trade_state['max_units'] >= 3.0 and current_units <= 3.0:
            return "EXIT_LOCKED_1_3"
        elif trade_state['max_units'] >= 2.0 and current_units <= 2.0:
            return "EXIT_LOCKED_1_2"
            
    elif position_type == "SELL":
        pips_gained = entry_price - current_price
        current_units = pips_gained / risk_unit
        
        if current_units > trade_state['max_units']:
            trade_state['max_units'] = current_units
            
        if trade_state['max_units'] >= 4.0 or current_units >= 4.0:
            return "EXIT_FINAL_1_4"
        elif trade_state['max_units'] >= 3.0 and current_units <= 3.0:
            return "EXIT_LOCKED_1_3"
        elif trade_state['max_units'] >= 2.0 and current_units <= 2.0:
            return "EXIT_LOCKED_1_2"
            
    return "RUNNING"

def main():
    print("Fetching market data for BTC Futures using coindcx_futures_strategy.py...")
    df = fetch_candles()
    df = apply_indicators(df)
    signal = check_signal(df)
    current_price = df.iloc[-1]['close']
    
    print(f"Current BTC Price: {current_price}")
    print(f"Generated Signal: {signal}")
    print(f"ADX Value: {df.iloc[-1]['adx']:.2f}")

if __name__ == "__main__":
    main()
