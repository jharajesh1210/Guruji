import time
import requests
import yaml
import pandas as pd
import pandas_ta as ta

# Load Config
with open("config.yml", "r") as file:
    config = yaml.safe_load(file)

SYMBOL = config['trading']['symbol']
BASE_URL = "https://public.coindcx.com"

def fetch_candles():
    url = f"{BASE_URL}/market_data/candles?pair={SYMBOL}&interval={config['trading']['timeframe']}&limit=100"
    response = requests.get(url)
    data = response.json()
    df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['close'] = df['close'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['volume'] = df['volume'].astype(float)
    return df

def apply_indicators(df):
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
    
    # Bullish Signal
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
    Aapka Logic:
    - Max height track karega.
    - Agar price 1:4 touch kiya -> Final Exit.
    - Agar price 1:3 gaya aur wapas niche aaya -> 1:3 par exit.
    - Agar price 1:2 gaya aur wapas niche aaya -> 1:2 par exit.
    """
    risk_unit = abs(entry_price - initial_sl)
    
    if position_type == "BUY":
        pips_gained = current_price - entry_price
        current_units = pips_gained / risk_unit
        
        # Maximum height reached track karein
        if current_units > trade_state['max_units']:
            trade_state['max_units'] = current_units
            
        # 1:4 Target Hit -> Final Exit
        if trade_state['max_units'] >= 4.0 or current_units >= 4.0:
            return "EXIT_FINAL_1_4"
            
        # Agar 1:3 ya usse upar gaya tha, aur ab wapas gir kar 1:3 par aa gaya
        elif trade_state['max_units'] >= 3.0 and current_units <= 3.0:
            return "EXIT_LOCKED_1_3"
            
        # Agar 1:2 ya usse upar gaya tha, aur ab wapas gir kar 1:2 par aa gaya
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

def run_bot():
    print("CoinDCX Advanced Profit-Locking Bot Started...")
    
    # Example active trade state (Jab trade active ho tab ise use karein)
    active_trade = None 
    
    while True:
        try:
            df = fetch_candles()
            df = apply_indicators(df)
            signal = check_signal(df)
            current_price = df.iloc[-1]['close']
            
            print(f"Current Price: {current_price} | Signal: {signal}")
            
            # Agar koi active trade chal rahi hai toh state check karein:
            # if active_trade:
            #     action = manage_risk_and_exits(active_trade['entry'], current_price, active_trade['type'], active_trade['sl'], active_trade['state'])
            #     print(f"Trade Status: {action}")
            #     if "EXIT" in action:
            #         print("Closing position due to:", action)
            #         active_trade = None
            
            time.sleep(900) # 15-minute polling
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_bot()
