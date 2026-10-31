#!/usr/bin/env python3
"""CoinDCX 365-day 5m GOLD BOOK backtest. Paper simulation only."""
import argparse, time, json, os, sys
from datetime import datetime, timezone
from pathlib import Path
import requests
import pandas as pd
import numpy as np
from collections import defaultdict

API="https://public.coindcx.com/market_data/candlesticks"
INSTRUMENTS="https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments"
CONFIG={"XAUUSDT":{"long":True,"short":True,"lookback":12,"rsi_short":50,"rsi_long":52,"priority":1},
        "PAXGUSDT":{"long":False,"short":True,"lookback":18,"rsi_short":45,"rsi_long":55,"priority":2}}
def fetch_json(url,params=None):
    err=None
    for attempt in range(4):
        try:
            r=requests.get(url,params=params,timeout=30)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException,ValueError) as e:
            err=e;time.sleep(1+attempt*2)
    raise RuntimeError(f"CoinDCX request failed: {url}: {err}")
def instruments():
    x=fetch_json(INSTRUMENTS)
    if isinstance(x,dict):x=x.get("data",x.get("instruments",[]))
    out=set()
    for y in x:
        p=y if isinstance(y,str) else (y.get("pair") or y.get("instrument") or y.get("symbol") or y.get("coindcx_name") or "")
        out.add(str(p).upper())
    return out
def candles(pair,days):
    end=int(time.time())
    start=end-days*86400-60*86400 # extra warmup for daily EMA50
    step=7*86400
    rows=[]
    for t in range(start,end,step):
        hi=min(end,t+step)
        obj=fetch_json(API,{"pair":pair,"pcode":"f","resolution":"5","from":t,"to":hi})
        batch=obj.get("data",[]) if isinstance(obj,dict) else []
        if not isinstance(batch,list):raise RuntimeError(f"{pair}: malformed response")
        rows.extend(batch)
        print(f"{pair} {datetime.fromtimestamp(t,timezone.utc).date()} candles={len(batch)}",flush=True)
        time.sleep(0.15)
    if not rows:raise RuntimeError(f"{pair}: no CoinDCX candles")
    d=pd.DataFrame(rows)
    for k in ("time","open","high","low","close","volume"):d[k]=pd.to_numeric(d[k],errors="coerce")
    d=d.dropna(subset=["time","open","high","low","close","volume"])
    unit="ms" if d.time.median()>1e11 else "s"
    d.index=pd.to_datetime(d.time,unit=unit,utc=True)
    d=d[["open","high","low","close","volume"]].sort_index()
    d=d[~d.index.duplicated(keep="last")]
    d=d[d.index+pd.Timedelta(minutes=5)<=pd.Timestamp.now(tz="UTC")]
    if len(d)<10000:raise RuntimeError(f"{pair}: insufficient candles ({len(d)})")
    return d
def prepare(df,cfg):
    d=df.copy();c=d.close
    tr=pd.concat([d.high-d.low,(d.high-c.shift()).abs(),(d.low-c.shift()).abs()],axis=1).max(axis=1)
    d["atr"]=tr.ewm(alpha=1/14,adjust=False).mean()
    delta=c.diff();up=delta.clip(lower=0).ewm(alpha=1/14,adjust=False).mean()
    dn=(-delta.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
    d["rsi"]=100-100/(1+up/dn.replace(0,np.nan))
    h=c.resample("1h").last().dropna();he=h.ewm(span=50,adjust=False).mean()
    d["h1c"]=h.shift(1).reindex(d.index,method="ffill")
    d["h1ema"]=he.shift(1).reindex(d.index,method="ffill")
    day=c.resample("1D").last().dropna();de=day.ewm(span=50,adjust=False).mean()
    d["dc"]=day.shift(1).reindex(d.index,method="ffill")
    d["dema"]=de.shift(1).reindex(d.index,method="ffill")
    d["hh"]=d.high.rolling(cfg["lookback"]).max().shift(1)
    d["ll"]=d.low.rolling(cfg["lookback"]).min().shift(1)
    win=(d.index.hour>=16)&(d.index.hour<20)
    sh=cfg["short"]&win&(c<d.ll)&(d.rsi<cfg["rsi_short"])&(d.h1c<d.h1ema)
    lo=cfg["long"]&win&(c>d.hh)&(d.rsi>cfg["rsi_long"])&(d.h1c>d.h1ema)&(d.dc>d.dema)
    d["sig"]=np.where(lo,1,np.where(sh,-1,0))
    return d
def backtest(frames,risk_usd,start):
    idx=pd.DatetimeIndex(sorted(set().union(*(set(f.index) for f in frames.values()))))
    arrays={s:{k:f.reindex(idx)[k].to_numpy() for k in ("open","high","low","close","atr","sig")} for s,f in frames.items()}
    order=sorted(frames,key=lambda s:CONFIG[s]["priority"])
    trades=[];pos=None;sl_count=defaultdict(int)
    for i,ts in enumerate(idx):
        if ts<start:continue
        if pos is None and i>0 and sl_count[ts.date()]<2:
            for sym in order:
                a=arrays[sym];signal=a["sig"][i-1];op=a["open"][i];atr=a["atr"][i-1]
                if not np.isfinite(signal) or signal==0 or not np.isfinite(op) or not np.isfinite(atr):continue
                risk=float(np.clip(atr*3,op*.0035,op*.006));direction=int(signal)
                pos=dict(sym=sym,side=direction,entry=op,risk=risk,sl=op-direction*risk,sl0=op-direction*risk,
                         tp1=op+direction*risk*2,tp2=op+direction*risk*3,stage=0,real=0,
                         t0=ts,deadline=ts+pd.Timedelta(hours=24))
                break
        if pos is None:continue
        a=arrays[pos["sym"]];hi=a["high"][i];low=a["low"][i];cl=a["close"][i]
        if not np.isfinite(cl):continue
        p=pos;direction=p["side"]
        sl_hit=(low<=p["sl"]) if direction==1 else (hi>=p["sl"])
        tp1_hit=(hi>=p["tp1"]) if direction==1 else (low<=p["tp1"])
        tp2_hit=(hi>=p["tp2"]) if direction==1 else (low<=p["tp2"])
        reason=None;rest=0
        if sl_hit:
            reason="SL" if p["stage"]==0 else "BE/SL";rest=-1 if p["stage"]==0 else 0
        elif p["stage"]==0 and tp1_hit:
            p["real"]+=1;p["stage"]=1;p["sl"]=p["entry"]
        elif p["stage"]==1 and tp2_hit:
            reason="TP2";rest=1.5
        if reason is None and ts>=p["deadline"]:
            reason="TIME";rest=(1 if p["stage"]==0 else .5)*(cl-p["entry"])*direction/p["risk"]
        if reason is not None:
            R=p["real"]+rest-.0008*p["entry"]/p["risk"]
            trades.append({"symbol":p["sym"],"entry_time":p["t0"].isoformat(),"exit_time":ts.isoformat(),
                           "side":"LONG" if direction==1 else "SHORT","entry":round(p["entry"],2),
                           "sl":round(p["sl0"],2),"tp1":round(p["tp1"],2),"tp2":round(p["tp2"],2),
                           "pnl":round(R*risk_usd,2),"R":round(R,3),"reason":reason})
            if reason=="SL":sl_count[ts.date()]+=1
            pos=None
    return pd.DataFrame(trades)
def main():
    ap=argparse.ArgumentParser();ap.add_argument("--days",type=int,default=365)
    ap.add_argument("--risk-usd",type=float,default=10);a=ap.parse_args()
    end=pd.Timestamp.now(tz="UTC");start=end-pd.Timedelta(days=a.days)
    pairs=instruments()
    frames={};errors={}
    for sym in CONFIG:
        pair="B-"+sym[:-4]+"_USDT"
        if pair not in pairs:
            errors[sym]=f"{pair} not listed in CoinDCX active futures instruments"
            print("UNAVAILABLE:",errors[sym],flush=True);continue
        try:frames[sym]=prepare(candles(pair,a.days),CONFIG[sym])
        except Exception as e:errors[sym]=str(e);print("ERROR:",sym,e,flush=True)
    Path("GOLD_BOOK_DATA_STATUS.json").write_text(json.dumps({"symbols_loaded":list(frames),"errors":errors,"period_days":a.days},indent=2))
    if not frames:raise RuntimeError("No usable CoinDCX futures gold symbol; no fabricated results")
    trades=backtest(frames,a.risk_usd,start)
    if trades.empty:
        print("No trades triggered");return
    trades.to_csv("GOLD_BOOK_ALL_TRADES.csv",index=False)
    for sym in frames:
        trades[trades.symbol==sym].to_csv(sym+"_TRADES.csv",index=False)
    summary=[]
    for sym,t in [("COMBINED",trades)]+[(s,trades[trades.symbol==s]) for s in frames]:
        summary.append({"symbol":sym,"trades":len(t),"wins":int((t.R>0).sum()),"win_rate_pct":round((t.R>0).mean()*100,2) if len(t) else 0,
                        "total_R":round(t.R.sum(),3),"pnl_usd":round(t.pnl.sum(),2)})
    pd.DataFrame(summary).to_csv("GOLD_BOOK_SUMMARY.csv",index=False)
    print(pd.DataFrame(summary).to_string(index=False),flush=True)
    if errors:print("PARTIAL BACKTEST: missing symbols",errors,flush=True)
if __name__=="__main__":main()
