# -*- coding: utf-8 -*-
"""
Smart Money Concepts (SMC) + Volume Profile Intraday Trading Strategy
Includes: Sensex Market Filter, Sectorial Relative Strength, 5-Minute Entry, 
and Step-by-Step Step-Lock Trailing Stop-Loss Logic (1:2 -> 1:3 -> 1:4 -> 1:5 Exit).
"""

import pandas as pd
import numpy as np

class SMCSectorialStrategy:
    def __init__(self):
        self.active_positions = {}
        
    def analyze_market_filters(self, sensex_df, sectoral_data):
        """
        Phase 1: Analyzes the opening market filters using Sensex direction
        and sectoral gainers/losers matrix.
        """
        sensex_open = sensex_df['open'].iloc[0]
        sensex_current = sensex_df['close'].iloc[-1]
        
        if sensex_current > sensex_open:
            market_bias = "BULLISH"
        else:
            market_bias = "BEARISH"
            
        top_gaining_sector = max(sectoral_data, key=sectoral_data.get)
        top_losing_sector = min(sectoral_data, key=sectoral_data.get)
        
        return {
            "market_bias": market_bias,
            "top_gaining_sector": top_gaining_sector,
            "top_losing_sector": top_losing_sector
        }

    def calculate_volume_profile_poc(self, df_5m, bins=20):
        """
        Phase 2: Calculates the Point of Control (POC) using Volume Profile on 5m candles.
        """
        price_min = df_5m['close'].min()
        price_max = df_5m['close'].max()
        
        if price_min == price_max:
            return price_min
            
        price_bins = np.linspace(price_min, price_max, bins + 1)
        volume_profile = np.zeros(bins)
        
        for idx, row in df_5m.iterrows():
            bin_idx = np.digitize(row['close'], price_bins) - 1
            if 0 <= bin_idx < bins:
                volume_profile[bin_idx] += row['volume']
                
        max_vol_idx = np.argmax(volume_profile)
        poc_price = (price_bins[max_vol_idx] + price_bins[max_vol_idx + 1]) / 2
        return poc_price

    def check_entry_signal_5m(self, df_5m, market_filters, stock_sector):
        """
        Phase 2 & 3: Detects 5-minute CHoCH structural breakout/breakdown 
        aligned with the order block and POC confluence zones.
        """
        market_bias = market_filters["market_bias"]
        top_gain = market_filters["top_gaining_sector"]
        top_lose = market_filters["top_losing_sector"]
        
        poc_price = self.calculate_volume_profile_poc(df_5m)
        current_price = df_5m['close'].iloc[-1]
        
        # 1. LONG ENTRY CONFLUENCE CRITERIA
        if market_bias == "BULLISH" and stock_sector == top_gain:
            recent_highs = df_5m['high'].iloc[-6:-1].tolist()
            if current_price > max(recent_highs): # 5-Min CHoCH Breakout
                bearish_candles = df_5m[df_5m['close'] < df_5m['open']]
                if not bearish_candles.empty:
                    ob_low = bearish_candles['low'].iloc[-1]
                    ob_high = bearish_candles['high'].iloc[-1]
                    
                    if ob_low <= poc_price <= ob_high:
                        initial_sl = ob_low - (ob_low * 0.0005)
                        return {
                            "signal": "LONG",
                            "entry_price": current_price,
                            "initial_sl": initial_sl,
                            "poc": poc_price
                        }
                        
        # 2. SHORT ENTRY CONFLUENCE CRITERIA
        elif market_bias == "BEARISH" and stock_sector == top_lose:
            recent_lows = df_5m['low'].iloc[-6:-1].tolist()
            if current_price < min(recent_lows): # 5-Min CHoCH Breakdown
                bullish_candles = df_5m[df_5m['close'] > df_5m['open']]
                if not bullish_candles.empty:
                    ob_high = bullish_candles['high'].iloc[-1]
                    ob_low = bullish_candles['low'].iloc[-1]
                    
                    if ob_low <= poc_price <= ob_high:
                        initial_sl = ob_high + (ob_high * 0.0005)
                        return {
                            "signal": "SHORT",
                            "entry_price": current_price,
                            "initial_sl": initial_sl,
                            "poc": poc_price
                        }
                        
        return None

    def process_step_lock_trailing_sl(self, current_price, position):
        """
        Phase 3: Executes step-by-step lock mechanism trailing stop-loss logic.
        Triggers: Moves SL to 1:2 -> 1:3 -> 1:4 -> 1:5 Full Exit.
        """
        entry_price = position["entry_price"]
        initial_sl = position["initial_sl"]
        pos_type = position["signal"]
        current_sl = position.get("current_sl", initial_sl)
        
        risk = abs(entry_price - initial_sl)
        
        if pos_type == "LONG":
            target_2 = entry_price + (2 * risk)
            target_3 = entry_price + (3 * risk)
            target_4 = entry_price + (4 * risk)
            target_5 = entry_price + (5 * risk) # 1:5 Target Added
            
            if current_price >= target_5:
                return {"action": "EXIT", "execution_price": target_5, "reason": "Target 1:5 Fully Booked"}
            elif current_price >= target_4 and current_sl < target_4:
                return {"action": "TRAIL_LOCK", "new_sl": target_4, "reason": "SL Stepped & Locked at 1:4"}
            elif current_price >= target_3 and current_sl < target_3:
                return {"action": "TRAIL_LOCK", "new_sl": target_3, "reason": "SL Stepped & Locked at 1:3"}
            elif current_price >= target_2 and current_sl < target_2:
                return {"action": "TRAIL_LOCK", "new_sl": target_2, "reason": "SL Stepped & Locked at 1:2"}
                
        elif pos_type == "SHORT":
            target_2 = entry_price - (2 * risk)
            target_3 = entry_price - (3 * risk)
            target_4 = entry_price - (4 * risk)
            target_5 = entry_price - (5 * risk) # 1:5 Target Added
            
            if current_price <= target_5:
                return {"action": "EXIT", "execution_price": target_5, "reason": "Target 1:5 Fully Booked"}
            elif current_price <= target_4 and current_sl > target_4:
                return {"action": "TRAIL_LOCK", "new_sl": target_4, "reason": "SL Stepped & Locked at 1:4"}
            elif current_price <= target_3 and current_sl > target_3:
                return {"action": "TRAIL_LOCK", "new_sl": target_3, "reason": "SL Stepped & Locked at 1:3"}
            elif current_price <= target_2 and current_sl > target_2:
                return {"action": "TRAIL_LOCK", "new_sl": target_2, "reason": "SL Stepped & Locked at 1:2"}
                
        # Check standard stop loss trigger
        if pos_type == "LONG" and current_price <= current_sl:
            return {"action": "EXIT", "execution_price": current_sl, "reason": "Stop Loss Hit"}
        elif pos_type == "SHORT" and current_price >= current_sl:
            return {"action": "EXIT", "execution_price": current_sl, "reason": "Stop Loss Hit"}
            
        return {"action": "HOLD", "current_sl": current_sl}

# Strategy Demonstration Flow Execution
if __name__ == "__main__":
    strategy = SMCSectorialStrategy()
    
    print("--- Running SMC + Volume Profile Framework (1:5 Target Edition) ---\n")
    
    sensex_mock = pd.DataFrame({"open": [75000], "close": [75250]})
    sectorial_returns_mock = {"IT": 1.8, "BANK": 0.5, "AUTO": -1.2}
    
    # 1. Filter Validation
    filters = strategy.analyze_market_filters(sensex_mock, sectorial_returns_mock)
    print(f"Sensex Filter Output: {filters['market_bias']}")
    print(f"Top Gaining Sector: {filters['top_gaining_sector']}\n")
    
    # 2. 5-Min Chart Mock Data
    df_5m_mock = pd.DataFrame({
        "open":  [100, 102, 101, 103, 105],
        "high":  [103, 104, 102, 104, 108],
        "low":   [99,  100, 98,  101, 104],
        "close": [102, 101, 103, 105, 107],
        "volume": [1000, 2500, 1200, 3000, 1500]
    })
    
    # 3. Signals Execution Evaluation
    trade_signal = strategy.check_entry_signal_5m(df_5m_mock, filters, stock_sector="IT")
    if trade_signal:
        print(f"Signal Generated: {trade_signal['signal']} Entry")
        print(f"Entry Trigger Price: {trade_signal['entry_price']}")
        print(f"Initial Stop Loss: {trade_signal['initial_sl']}")
        print(f"Calculated Volume POC: {trade_signal['poc']}\n")
        
        position = {
            "signal": trade_signal["signal"],
            "entry_price": trade_signal["entry_price"],
            "initial_sl": trade_signal["initial_sl"],
            "current_sl": trade_signal["initial_sl"]
        }
        
        risk_val = trade_signal["entry_price"] - trade_signal["initial_sl"]
        
        # Scenario A: Price hits 1:2 RR
        price_at_2rr = trade_signal["entry_price"] + (2.2 * risk_val)
        res_2rr = strategy.process_step_lock_trailing_sl(price_at_2rr, position)
        print(f"Price hits 1:2 Level ({price_at_2rr:.2f}): {res_2rr['action']} -> {res_2rr.get('reason')}")
        if "new_sl" in res_2rr:
            position["current_sl"] = res_2rr["new_sl"]
            
        # Scenario B: Price rallies to reach 1:5 Target Limit
        price_at_5rr = trade_signal["entry_price"] + (5.1 * risk_val)
        res_5rr = strategy.process_step_lock_trailing_sl(price_at_5rr, position)
        print(f"Price reaches 1:5 Target Limit ({price_at_5rr:.2f}): {res_5rr['action']} -> {res_5rr.get('reason')}")
