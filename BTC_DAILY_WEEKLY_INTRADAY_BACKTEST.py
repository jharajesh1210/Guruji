#!/usr/bin/env python3
"""Historical backtest for BTC_DAILY_WEEKLY_INTRADAY_PAPER strategy."""
import os, sys, csv
from pathlib import Path
import numpy as np
import pandas as pd

os.environ.setdefault("HISTORY_DAYS", "30")
import BTC_DAILY_WEEKLY_INTRADAY_PAPER as strat

FEE_RATE = float(os.getenv("FEE_RATE", "0.0005"))       # 0.05% each side
SLIPPAGE = float(os.getenv("SLIPPAGE_RATE", "0.0002")) # 0.02% each side
CAPITAL = float(os.getenv("CAPITAL_PER_TRADE_USDT", "100"))
OUT = Path("backtest_results")
OUT.mkdir(exist_ok=True)

def fill_price(price, side, action):
    # adverse execution: BUY pays up, SELL receives less
    buy = (side == "LONG" and action == "ENTRY") or (side == "SHORT" and action == "EXIT")
    return price * (1 + SLIPPAGE if buy else 1 - SLIPPAGE)

def condition_diagnostics(x):
    """Count each original setup condition independently and cumulatively; does not alter signals."""
    counts = {"shared_gate_candidates": 0, "setups": {}}
    names = {
      "WEEKLY_VWAP_RECLAIM": ["prev_below_prev_daily_close","prev_below_weekly_vwap","prev_below_bb_mid","close_above_weekly_vwap","bull_strong","macd_above_signal","macd_hist_positive","rsi_above_57","ema9_above_ema21","volume_spike","all_setup_conditions"],
      "BB_MIDDLE_WEEKLY_TOUCH": ["close_above_bb_mid","bull_marubozu","weekly_touch","macd_above_signal","macd_hist_positive","rsi_above_57","ema9_above_ema21","volume_spike","all_setup_conditions"],
      "WEEKLY_HIGH_EXHAUSTION": ["rising_highs_4","prior_breakout","high_above_prev_week_high","close_below_prev_week_high","bear_strong","rsi_prev_above_70_falling","macd_bear_turn","macd_hist_negative","opening_green_strong","volume_spike","all_setup_conditions"],
      "BB_MIDDLE_REJECTION": ["bb_touches_12_between_2_3","high_near_bb_mid","close_below_bb_mid","bear_strong","macd_below_signal","macd_hist_negative","rsi_between_45_52","rsi_falling","volume_spike","all_setup_conditions"]
    }
    counts["setups"] = {k: {"conditions": {n: 0 for n in v}, "cumulative_funnel": {}} for k,v in names.items()}
    for i in range(300, len(x)):
        b=x.iloc[i]; p=x.iloc[i-1]
        if not bool(b.get("or_filter",False) and b.get("near_weekly_level",False) and b.get("liquid_24h",False)):
            continue
        counts["shared_gate_candidates"] += 1
        # Same setup-1/2 LONG confirmation gate as detect_signal().
        long_conf = [
          bool(b["macd"] > b["macd_signal"]),
          bool(b["macd_hist"] > 0),
          bool(b["rsi"] > 57),
          bool(b["ema9"] > b["ema21"]),
          bool(b["volume_spike"])
        ]
        s1 = [
          bool(p["close"] < b["previous_daily_close"]),
          bool(p["close"] < p["weekly_vwap"]),
          bool(p["close"] < p["bb_mid"]),
          bool(b["close"] > b["weekly_vwap"]),
          bool(b["bull_strong"]),
          *long_conf
        ]
        last3=x.iloc[i-2:i+1]
        weekly_touch=any(((last3["low"] <= level+0.25*float(b["atr"])) & (last3["high"] >= level-0.25*float(b["atr"]))).any() for level in [float(b["weekly_open"]),float(b["weekly_vwap"])])
        s2=[bool(b["close"]>b["bb_mid"]),bool(b["bull_marubozu"]),bool(weekly_touch),*long_conf]
        last4=x.iloc[i-3:i+1]
        rising=all(float(last4.iloc[k]["high"])>float(last4.iloc[k-1]["high"]) for k in range(1,len(last4)))
        breakout=(float(last4.iloc[-2]["high"])>float(b["previous_week_high"]) or float(last4.iloc[-3]["high"])>float(b["previous_week_high"]))
        exhaustion=bool(p["rsi"]>70 and b["rsi"]<p["rsi"])
        bear_turn=bool(b["macd_hist"]<p["macd_hist"] and b["macd"]<p["macd"])
        s3=[bool(rising),bool(breakout),bool(b["high"]>b["previous_week_high"]),bool(b["close"]<b["previous_week_high"]),bool(b["bear_strong"]),exhaustion,bear_turn,bool(b["macd_hist"]<0),bool(b["opening_green_strong"]),bool(b["volume_spike"])]
        s4=[bool(2<=float(b["bb_touches_12"])<=3),bool(b["high"]>=b["bb_mid"]-0.15*b["atr"]),bool(b["close"]<b["bb_mid"]),bool(b["bear_strong"]),bool(b["macd"]<b["macd_signal"]),bool(b["macd_hist"]<0),bool(45<=b["rsi"]<=52),bool(b["rsi"]<p["rsi"]),bool(b["volume_spike"])]
        for setup, vals in zip(names,[s1,s2,s3,s4]):
            c=counts["setups"][setup]["conditions"]
            keys=names[setup][:-1]
            for key,val in zip(keys,vals):
                c[key]+=int(val)
            c["all_setup_conditions"]+=int(all(vals))
            # Ordered funnel: count how many candidates survive each successive original condition.
            funnel=counts["setups"][setup]["cumulative_funnel"]
            running=True
            for key,val in zip(keys,vals):
                running = running and bool(val)
                funnel[key] = funnel.get(key,0) + int(running)
            funnel["all_conditions"] = funnel.get("all_conditions",0) + int(all(vals))
    return counts


def threshold_variant_scan(data):
    """
    Controlled sensitivity scan. Original strategy module is never edited.
    Each row changes ONE threshold family at a time; emits qualifying signal counts
    and timestamps for comparison before any variant is promoted to P&L testing.
    """
    variants = [
      ("BASELINE_ORIGINAL", {}),
      ("LONG_RSI_52", {"long_rsi": 52}),
      ("LONG_RSI_55", {"long_rsi": 55}),
      ("BB_MARUBOZU_BODY_0_65", {"marubozu_body": 0.65}),
      ("WEEKLY_EXHAUSTION_OPEN_BODY_0_40", {"opening_body": 0.40}),
      ("BB_REJECTION_VOLUME_0_90X", {"volume_mult": 0.90}),
      ("BB_REJECTION_VOLUME_0_80X", {"volume_mult": 0.80}),
    ]
    results=[]
    signal_rows=[]
    for name, cfg in variants:
        x=data.copy()
        if "long_rsi" in cfg:
            # Store alternate confirmation threshold for this isolated sensitivity test.
            x["rsi"] = x["rsi"]
        if "marubozu_body" in cfg:
            cr=(x["high"]-x["low"]).replace(0,np.nan)
            body=(x["close"]-x["open"]).abs()
            x["bull_marubozu"]=(x["close"]>x["open"]) & (body/cr>=cfg["marubozu_body"]) & ((x["high"]-x["close"])/cr<=0.15) & ((x["open"]-x["low"])/cr<=0.15)
        if "opening_body" in cfg:
            # Re-evaluate first candle body ratio with a lower exploratory threshold.
            x["opening_green_strong"]=False
            for _, ix in x.groupby("session_date", sort=False).groups.items():
                first=x.loc[list(ix)[:1]]
                if len(first):
                    z=first.iloc[0]; rng=float(z["high"]-z["low"])
                    x.loc[first.index[0],"opening_green_strong"]=bool(rng>0 and z["close"]>z["open"] and (z["close"]-z["open"])/rng>=cfg["opening_body"])
        if "volume_mult" in cfg:
            x["volume_spike"]=x["volume"] >= x["volume_avg20"]*cfg["volume_mult"]
        hits={}
        for i in range(300,len(x)):
            window=x.iloc[:i+1]
            # RSI variants are applied by a scoped copy of the detector to preserve all other rules.
            if "long_rsi" in cfg:
                original=strat.detect_signal
                def detector(w, threshold=cfg["long_rsi"]):
                    w=w.copy()
                    # The original detector reads the latest RSI directly for both LONG setups.
                    # Raise only the comparison eligibility by using a temporary proxy RSI.
                    current=float(w.iloc[-1]["rsi"])\n                    if threshold < current <= 57:\n                        w.iloc[-1,w.columns.get_loc("rsi")]=57.000001
                    return original(w)
                sig=detector(window)
            else:
                sig=strat.detect_signal(window)
            if sig:
                hits[sig["setup"]]=hits.get(sig["setup"],0)+1
                signal_rows.append({"variant":name,"time":x.iloc[i]["timestamp"].isoformat(),"setup":sig["setup"],"side":sig["side"],"close":float(x.iloc[i]["close"])})
        results.append({"variant":name,"signals_total":sum(hits.values()),"signals_by_setup":hits,"changed_parameter":str(cfg)})
    pd.DataFrame(results).to_csv(OUT/"BTC_5M_threshold_variant_comparison.csv",index=False)
    pd.DataFrame(signal_rows,columns=["variant","time","setup","side","close"]).to_csv(OUT/"BTC_5M_threshold_variant_signals.csv",index=False)
    print("===== THRESHOLD VARIANT SIGNAL COMPARISON (screening only) =====")
    print(pd.DataFrame(results).to_string(index=False))


def main():
    raw = strat.fetch_futures_candles()
    data = strat.add_indicators(raw).reset_index(drop=True)
    trades, events = [], []
    signal_rows = []
    setup_counts = {}
    pos = None
    for i in range(300, len(data)):
        bar = data.iloc[i]
        stamp = bar["timestamp"].isoformat()
        if pos:
            side, entry, risk = pos["side"], pos["entry"], pos["risk"]
            stop = pos["stop"]
            hi, lo = float(bar.high), float(bar.low)
            stop_hit = lo <= stop if side == "LONG" else hi >= stop
            if stop_hit:
                raw_exit = stop
                exit_px = fill_price(raw_exit, side, "EXIT")
                qty = pos["qty"]
                gross = strat.paper_pnl(side, entry, exit_px, qty)
                fees = (entry * qty + exit_px * qty) * FEE_RATE
                pnl = gross - fees
                trades.append({**pos["row"], "exit_time":stamp,"exit_price":exit_px,"exit_reason":"STOP_OR_LOCKED_STOP","gross_pnl":gross,"fees":fees,"net_pnl":pnl,"r_multiple":pnl/(CAPITAL*risk/entry)})
                pos = None
            else:
                r4 = entry + 4*risk if side=="LONG" else entry - 4*risk
                r2 = entry + 2*risk if side=="LONG" else entry - 2*risk
                r3 = entry + 3*risk if side=="LONG" else entry - 3*risk
                reached4 = hi >= r4 if side=="LONG" else lo <= r4
                reached3 = hi >= r3 if side=="LONG" else lo <= r3
                reached2 = hi >= r2 if side=="LONG" else lo <= r2
                if reached4:
                    exit_px = fill_price(r4, side, "EXIT")
                    qty=pos["qty"]; gross=strat.paper_pnl(side,entry,exit_px,qty)
                    fees=(entry*qty+exit_px*qty)*FEE_RATE; pnl=gross-fees
                    trades.append({**pos["row"],"exit_time":stamp,"exit_price":exit_px,"exit_reason":"4R_FINAL_PROFIT_BOOKING","gross_pnl":gross,"fees":fees,"net_pnl":pnl,"r_multiple":pnl/(CAPITAL*risk/entry)})
                    pos=None
                else:
                    if reached3:
                        pos["stop"] = entry + 2*risk if side=="LONG" else entry - 2*risk
                        pos["highest"]=3
                    elif reached2 and pos["highest"] < 2:
                        pos["stop"] = entry + risk if side=="LONG" else entry - risk
                        pos["highest"]=2
        if pos is None:
            sig = strat.detect_signal(data.iloc[:i+1])
            if sig:
                side=sig["side"]; setup=sig["setup"]
                setup_counts[setup] = setup_counts.get(setup, 0) + 1
                signal_rows.append({"candle_time":stamp,"side":side,"setup":setup,"close":float(bar.close)})
                raw_entry=float(bar.close)
                entry=fill_price(raw_entry,side,"ENTRY")
                stop=strat.make_stop(side,raw_entry,bar)
                risk=raw_entry-stop if side=="LONG" else stop-raw_entry
                if np.isfinite(risk) and risk>0:
                    qty=CAPITAL/entry
                    pos={"side":side,"setup":setup,"entry":entry,"stop":stop,"risk":risk,"qty":qty,"highest":0,
                         "row":{"entry_time":stamp,"side":side,"setup":setup,"entry_price":entry,"initial_stop":stop,"quantity":qty,"notional_usdt":CAPITAL}}
    # Mark any open trade at final candle close, explicitly not a strategy exit.
    if pos:
        bar=data.iloc[-1]; side=pos["side"]; exit_px=fill_price(float(bar.close),side,"EXIT")
        qty=pos["qty"]; gross=strat.paper_pnl(side,pos["entry"],exit_px,qty)
        fees=(pos["entry"]*qty+exit_px*qty)*FEE_RATE; pnl=gross-fees
        trades.append({**pos["row"],"exit_time":bar.timestamp.isoformat(),"exit_price":exit_px,"exit_reason":"END_OF_DATA_MARK_TO_MARK","gross_pnl":gross,"fees":fees,"net_pnl":pnl,"r_multiple":pnl/(CAPITAL*pos["risk"]/pos["entry"])})
    trade_columns=["entry_time","side","setup","entry_price","initial_stop","quantity","notional_usdt","exit_time","exit_price","exit_reason","gross_pnl","fees","net_pnl","r_multiple"]
    df=pd.DataFrame(trades, columns=trade_columns)
    df.to_csv(OUT/"BTC_5M_backtest_trades.csv",index=False)
    pd.DataFrame(signal_rows,columns=["candle_time","side","setup","close"]).to_csv(OUT/"BTC_5M_backtest_signals.csv",index=False)
    condition_counts = condition_diagnostics(data)
    threshold_variant_scan(data)
    diagnostics={
      "candles_loaded":int(len(data)),
      "candles_after_warmup":int(max(0,len(data)-300)),
      "opening_range_filter_true":int(data["or_filter"].fillna(False).sum()),
      "near_weekly_level_true":int(data["near_weekly_level"].fillna(False).sum()),
      "liquid_24h_true":int(data["liquid_24h"].fillna(False).sum()),
      "shared_gate_true":int((data["or_filter"].fillna(False)&data["near_weekly_level"].fillna(False)&data["liquid_24h"].fillna(False)).sum()),
      "signals_by_setup":setup_counts,
      "condition_counts_on_shared_gate_candidates":condition_counts
    }
    pd.Series(diagnostics).to_json(OUT/"BTC_5M_backtest_diagnostics.json",indent=2)
    if len(df):
        wins=int((df.net_pnl>0).sum()); losses=int((df.net_pnl<=0).sum())
        gross_profit=float(df.loc[df.net_pnl>0,"net_pnl"].sum()); gross_loss=float(-df.loc[df.net_pnl<0,"net_pnl"].sum())
        equity=df.net_pnl.cumsum(); peak=equity.cummax(); dd=float((equity-peak).min())
        summary={"pair":strat.PAIR,"timeframe":"5m","history_days":int(os.getenv("HISTORY_DAYS","30")),"candles":len(data),
          "trades":len(df),"wins":wins,"losses":losses,"win_rate_pct":round(100*wins/len(df),2),
          "net_pnl_usdt":round(float(df.net_pnl.sum()),4),"gross_pnl_usdt":round(float(df.gross_pnl.sum()),4),
          "fees_usdt":round(float(df.fees.sum()),4),"profit_factor":round(gross_profit/gross_loss,3) if gross_loss else None,
          "max_drawdown_usdt":round(dd,4),"fee_rate_each_side":FEE_RATE,"slippage_each_side":SLIPPAGE}
    else:
        summary={"pair":strat.PAIR,"timeframe":"5m","candles":len(data),"trades":0,"signals_detected":len(signal_rows),"note":"No completed trades. Check BTC_5M_backtest_diagnostics.json and BTC_5M_backtest_signals.csv to distinguish no signals from open trades at the end of the period."}
    pd.Series(summary).to_json(OUT/"BTC_5M_backtest_summary.json",indent=2)
    print(pd.Series(summary).to_string())
    print("Setup breakdown:")
    if len(df): print(df.groupby("setup").agg(trades=("net_pnl","size"),wins=("net_pnl",lambda s:int((s>0).sum())),net_pnl=("net_pnl","sum")).to_string())

if __name__=="__main__": main()
