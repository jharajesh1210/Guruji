#!/usr/bin/env python3
"""BTCUSDT CoinDCX Futures 5m LIVE-DATA PAPER scanner. Never places exchange orders."""
import os, json, time, csv, requests
from pathlib import Path
import pandas as pd
import numpy as np

PAIR=os.getenv("COINDCX_PAIR","B-BTC_USDT")
CAPITAL=float(os.getenv("CAPITAL_PER_TRADE_USDT","100"))
STATE=Path("BTC_5M_BB_SQUEEZE_state.json")
TRADES=Path("BTC_5M_BB_SQUEEZE_trades.csv")
SIGNALS=Path("BTC_5M_BB_SQUEEZE_signals.csv")
API="https://public.coindcx.com/market_data/candlesticks"

def tg(msg):
    token=os.getenv("TG_TOKEN") or os.getenv("BOT_TOKEN")
    chat=os.getenv("TG_CHAT_ID") or os.getenv("CHAT_ID")
    if token and chat:
        try: requests.post(f"https://api.telegram.org/bot{token}/sendMessage",json={"chat_id":chat,"text":msg},timeout=15).raise_for_status()
        except Exception as e: print("Telegram warning:",e)

def candles():
    now=int(time.time())
    r=requests.get(API,params={"pair":PAIR,"from":now-60*24*3600,"to":now,"resolution":"5","pcode":"f"},timeout=30)
    r.raise_for_status()
    raw=r.json()
    if isinstance(raw,dict): raw=raw.get("data",raw.get("candlesticks",[]))
    d=pd.DataFrame(raw)
    if d.empty: raise RuntimeError("CoinDCX returned no candles")
    aliases={"time":"timestamp","open":"open","high":"high","low":"low","close":"close","volume":"volume"}
    d=d.rename(columns=aliases)
    # CoinDCX commonly returns time in seconds or milliseconds.
    if "timestamp" not in d and "time" in d: d["timestamp"]=d["time"]
    for c in ["open","high","low","close","volume"]: d[c]=pd.to_numeric(d[c],errors="coerce")
    ts=pd.to_numeric(d["timestamp"],errors="coerce")
    unit="ms" if ts.dropna().median()>1e12 else "s"
    d["timestamp"]=pd.to_datetime(ts,unit=unit,utc=True,errors="coerce")
    d=d.dropna(subset=["timestamp","open","high","low","close","volume"]).sort_values("timestamp").drop_duplicates("timestamp")
    # Ignore the currently forming 5m candle.
    cutoff=pd.Timestamp.now(tz="UTC").floor("5min")
    d=d[d.timestamp < cutoff].reset_index(drop=True)
    if len(d)<150: raise RuntimeError(f"Not enough closed candles: {len(d)}")
    return d

def indicators(d):
    x=d.copy(); c=x.close; typ=(x.high+x.low+c)/3
    x["ema9"]=c.ewm(span=9,adjust=False).mean()
    x["mid"]=c.rolling(20).mean(); sd=c.rolling(20).std(ddof=0)
    x["upper"]=x.mid+2*sd; x["lower"]=x.mid-2*sd
    x["width"]=(x.upper-x.lower)/x.mid
    x["squeeze"]=x.width <= x.width.shift(1).rolling(100,min_periods=40).quantile(.20)
    x["recent_sq"]=x.squeeze.shift(1).rolling(12,min_periods=1).max().fillna(0).astype(bool)
    delta=c.diff(); gain=delta.clip(lower=0); loss=-delta.clip(upper=0)
    ag=gain.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    al=loss.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    rs=ag/al.replace(0,np.nan); x["rsi"]=100-100/(1+rs)
    x.loc[(al==0)&(ag>0),"rsi"]=100; x.loc[(ag==0)&(al>0),"rsi"]=0
    day=x.timestamp.dt.floor("D"); pv=typ*x.volume
    x["vwap"]=pv.groupby(day).cumsum()/x.volume.groupby(day).cumsum().replace(0,np.nan)
    x["vavg"]=x.volume.rolling(20).mean()
    x["rh"]=x.high.shift(1).rolling(10).max(); x["rl"]=x.low.shift(1).rolling(10).min()
    x["ema_up"]=x.ema9>x.ema9.shift(2); x["ema_dn"]=x.ema9<x.ema9.shift(2)
    x["up_expand"]=(x.upper>x.upper.shift(1))&(x.width>x.width.shift(1))
    x["dn_expand"]=(x.lower<x.lower.shift(1))&(x.width>x.width.shift(1))
    x["vwap_reclaim"]=(x.close>x.vwap)&(x.close.shift(1)<=x.vwap.shift(1))
    x["vwap_reject"]=(x.high>=x.vwap)&(x.close<x.vwap)
    x["rsi17"]=(x.rsi.shift(1)<=17)&(x.rsi>17)
    x["lower_recovery"]=(x.close>x.lower)&(x.close.shift(1)<=x.lower.shift(1))
    x["buy_sq"]=x.recent_sq&x.ema_up&(x.close>x.ema9)&x.vwap_reclaim&(x.close>x.rh)&(x.volume>x.vavg)&x.up_expand
    x["buy_os"]=x.rsi17&x.lower_recovery&x.ema_up&(x.close>x.ema9)&x.vwap_reclaim&(x.close>x.rh)&(x.volume>x.vavg)&x.up_expand
    x["sell"]=x.recent_sq&(x.close<x.ema9)&x.ema_dn&(x.close<x.mid)&((x.close<x.vwap)|x.vwap_reject)&(x.rsi<45)&(x.close<x.rl)&x.dn_expand
    x["signal"]=""
    x.loc[x.buy_sq,"signal"]="BUY_SQUEEZE"; x.loc[x.buy_os,"signal"]="BUY_OVERSOLD"; x.loc[x.sell,"signal"]="SELL"
    return x

def load_state():
    if STATE.exists(): return json.loads(STATE.read_text())
    return {"last_candle":"","position":None,"capital_per_trade":CAPITAL}

def save_state(s): STATE.write_text(json.dumps(s,indent=2))

def append_trade(row):
    exists=TRADES.exists()
    with TRADES.open("a",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(row)); 
        if not exists: w.writeheader()
        w.writerow(row)

def manage(pos,bar,stamp):
    side=pos["side"]; entry=pos["entry"]; risk=pos["risk"]
    best=(bar["high"]-entry)/risk if side=="LONG" else (entry-bar["low"])/risk
    lock=pos.get("lock",0)
    if lock==0:
        stop=pos["initial_sl"]
    else:
        stop=entry+lock*risk if side=="LONG" else entry-lock*risk
    hit=bar["low"]<=stop if side=="LONG" else bar["high"]>=stop
    if hit:
        return {"exit":True,"price":stop,"reason":"INITIAL_SL" if lock==0 else f"REVERSE_EXIT_{lock}R","best":pos.get("best_r",0)}
    # Milestone reached on this candle arms the lock for next completed candle.
    pos["best_r"]=max(pos.get("best_r",0),best)
    new=4 if pos["best_r"]>=5 else 3 if pos["best_r"]>=4 else 2 if pos["best_r"]>=3 else 1 if pos["best_r"]>=2 else 0
    pos["lock"]=max(lock,new)
    return None

def main():
    d=indicators(candles()); d.to_csv(SIGNALS,index=False)
    state=load_state(); bar=d.iloc[-1]; stamp=bar.timestamp.isoformat()
    if state.get("last_candle")==stamp:
        print("Already processed candle:",stamp); return
    pos=state.get("position")
    if pos:
        ex=manage(pos,bar,stamp)
        if ex:
            side=pos["side"]; qty=pos["qty"]; pnl=((ex["price"]-pos["entry"])*qty if side=="LONG" else (pos["entry"]-ex["price"])*qty)
            append_trade({"entry_time":pos["entry_time"],"exit_time":stamp,"side":side,"setup":pos["setup"],"entry":pos["entry"],"exit":ex["price"],"qty":qty,"notional_usdt":CAPITAL,"initial_sl":pos["initial_sl"],"exit_reason":ex["reason"],"pnl_usdt":round(pnl,6),"best_r":ex["best"]})
            msg=f"BTC 5M PAPER EXIT {side}\nReason: {ex['reason']}\nEntry: {pos['entry']}\nExit: {ex['price']}\nPnL: {pnl:.4f} USDT"
            print(msg); tg(msg); state["position"]=None
        else: print("Open paper position:",pos)
    if state.get("position") is None and bar.signal:
        side="LONG" if str(bar.signal).startswith("BUY") else "SHORT"
        entry=float(bar.close); # signal is confirmed at completed candle close
        # Initial SL uses the opposite edge of the 10-candle pre-breakout range.
        sl=float(bar.rl if side=="LONG" else bar.rh)
        risk=entry-sl if side=="LONG" else sl-entry
        if risk>0 and CAPITAL>0:
            qty=CAPITAL/entry
            state["position"]={"side":side,"setup":str(bar.signal),"entry_time":stamp,"entry":entry,"qty":qty,"initial_sl":sl,"risk":risk,"lock":0,"best_r":0}
            msg=f"BTC 5M PAPER {side} ENTRY\nSetup: {bar.signal}\nEntry: {entry}\nInitial SL: {sl}\nCapital per trade: {CAPITAL} USDT\nQty: {qty:.8f} BTC"
            print(msg); tg(msg)
    state["last_candle"]=stamp; state["capital_per_trade"]=CAPITAL; save_state(state)
    print(f"Processed {stamp} | close={bar.close} | RSI={bar.rsi:.2f} | signal={bar.signal or 'NONE'} | capital/trade={CAPITAL} USDT")

if __name__=="__main__": main()
