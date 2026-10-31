#!/usr/bin/env python3
"""CoinDCX USDT futures PAPER ONLY. No authenticated/order endpoints.

V2 fixes (from the paper run: 15 closed, 4 wins, -165 USDT, of which ONE trade -131):
 1. FIXED RISK PER TRADE. Old code used a fixed 1500 USDT notional, so a wide ATR stop
    (SOPH: ~8.7% away) lost 131 USDT = 2.6x the 50 USDT margin. At 30x a real exchange
    liquidates at roughly 3% against you, so that loss could never have happened live.
    Now qty = RISK_USDT / stop distance (capped by margin x leverage).
 2. MAX_STOP_PCT / MIN_STOP_PCT: skip trades whose stop is wider than liquidation
    allows, or so tight that fees+slippage eat the trade.
 3. LOOK-AHEAD BUG: entry was the 15m bar CLOSE but the position time was the bar OPEN,
    so the 5m candles inside the signal bar (before entry) could trigger a fake STOP/TP.
    Entry now = latest closed 5m close and only candles AFTER it are checked.
 4. Slippage added on entry and exit (SLIP, default 0.02% per side).
 5. 1R lock at +0.25R (like the TOP100 bot) so a trade that reached 1R is not a full
    loss. Set LOCK1_R=0 to switch it off.
 6. Open positions older than MAX_HOLD_HOURS are closed at market (TIMEOUT) so dead
    trades do not block the MAX_OPEN slots.
Old open positions in the existing state file keep working (they carry qty and risk).
"""
import os,json,csv,time,math
from datetime import datetime,timezone
import requests,pandas as pd
from New_crypto_top100_scanner import analyze,decide,ema
API="https://api.coindcx.com"
PUB="https://public.coindcx.com"
STATE="coindcx_top100_paper_state.json"
CSV="coindcx_top100_paper_trades.csv"
MARGIN=50.0
LEV=30
NOTIONAL=MARGIN*LEV
FEE=float(os.getenv("PAPER_FEE","0.0005"))
SLIP=float(os.getenv("PAPER_SLIP","0.0002"))
MIN_SCORE=int(os.getenv("MIN_SCORE","4"))
MAX_OPEN=int(os.getenv("MAX_OPEN","5"))
TOP=int(os.getenv("TOP","100"))
RISK_USDT=float(os.getenv("RISK_USDT","5"))        # loss if the stop is hit (10% of margin)
MAX_STOP_PCT=float(os.getenv("MAX_STOP_PCT","2.5")) # liquidation at 30x is ~3%; stay well inside
MIN_STOP_PCT=float(os.getenv("MIN_STOP_PCT","0.4")) # below this fees+slippage dominate
LOCK1_R=float(os.getenv("LOCK1_R","0.25"))          # SL moves to +0.25R once 1R is reached (0 = off)
MAX_ENTRY_DEV_PCT=float(os.getenv("MAX_ENTRY_DEV_PCT","0.5"))
MAX_HOLD_HOURS=float(os.getenv("MAX_HOLD_HOURS","24"))
s=requests.Session()
def get(url,params=None):
 r=s.get(url,params=params,timeout=25);r.raise_for_status();return r.json()
def alert(msg):
 token=os.getenv("BOT_TOKEN") or os.getenv("TG_TOKEN")
 chat=os.getenv("CHAT_ID") or os.getenv("TG_CHAT_ID")
 print(msg,flush=True)
 if not token or not chat:
  print("TELEGRAM DELIVERY: NOT CONFIGURED",flush=True)
  return
 r=s.post("https://api.telegram.org/bot"+token+"/sendMessage",data={"chat_id":chat,"text":msg},timeout=15)
 r.raise_for_status()
 if not r.json().get("ok"):
  raise RuntimeError("TELEGRAM DELIVERY: API REJECTED")
 print("TELEGRAM DELIVERY: API ACCEPTED",flush=True)
def candles(pair,resolution,days=8):
 end=int(time.time())
 start=end-days*86400
 raw=get(PUB+"/market_data/candlesticks",{"pair":pair,"from":start,"to":end,"resolution":resolution,"pcode":"f"})
 if raw.get("s")!="ok":raise ValueError(str(raw)[:200])
 d=pd.DataFrame(raw["data"])
 if d.empty:return d
 for c in ["open","high","low","close","volume"]:d[c]=pd.to_numeric(d[c])
 d["time"]=pd.to_datetime(d["time"],unit="ms",utc=True)
 d=d.sort_values("time").drop_duplicates("time").set_index("time")
 minutes={"5":5,"60":60}[resolution]
 d=d[d.index+pd.Timedelta(minutes=minutes)<=pd.Timestamp.now(tz="UTC")]
 return d
def frame(pair):
 d=candles(pair,"5",10)
 if len(d)<800:return None,None
 bars=d.resample("15min",label="left",closed="left").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"})
 count=d["close"].resample("15min").count()
 bars=bars[count==3].dropna().reset_index()
 if len(bars)<260:return None,None
 # CoinDCX candles do not supply taker-buy volume: CVD proxy unavailable.
 # Neutral 50% proxy ensures CVD-dependent strategies cannot trigger.
 bars["taker_buy"]=bars["volume"]*0.5
 return d,bars
def load():
 if os.path.exists(STATE):
  with open(STATE) as f:return json.load(f)
 return {"positions":{},"closed":0,"wins":0,"realized_pnl":0.0,"last_candle":{}}
def save(st):
 with open(STATE,"w") as f:json.dump(st,f,indent=2)
def close(st,pair,p,price,why,t):
 d=1 if p["side"]=="LONG" else -1
 price=price*(1-d*SLIP)  # exit slippage works against the position
 gross=(price-p["entry"])*p["qty"]*d
 fees=(price+p["entry"])*p["qty"]*FEE
 pnl=gross-fees
 st["realized_pnl"]+=pnl;st["closed"]+=1;st["wins"]+=int(pnl>0)
 row={"pair":pair,"side":p["side"],"entry":p["entry"],"exit":price,"qty":p["qty"],"margin":MARGIN,"leverage":LEV,"pnl":round(pnl,4),"reason":why,"entry_time":p["time"],"exit_time":t,"score":p.get("score",""),"strategies":",".join(p.get("strategies",[]))}
 exists=os.path.exists(CSV)
 with open(CSV,"a",newline="") as f:
  fields=list(row.keys())
  if exists:
   with open(CSV,newline="") as oldf:
    reader=csv.DictReader(oldf)
    oldfields=reader.fieldnames or []
    if oldfields and oldfields!=fields:
     oldrows=list(reader)
     with open(CSV,"w",newline="") as newf:
      writer=csv.DictWriter(newf,fieldnames=fields,extrasaction="ignore")
      writer.writeheader()
      writer.writerows(oldrows)
  w=csv.DictWriter(f,fieldnames=fields)
  if not exists:w.writeheader()
  w.writerow(row)
 del st["positions"][pair]
 alert(f"COINDCX PAPER CLOSE {p['side']} {pair} {why} | Strategies {','.join(p.get('strategies',[])) or 'legacy/unknown'} | Score {p.get('score','unknown')} | PnL {pnl:+.2f} USDT | Total {st['realized_pnl']:+.2f}")
def manage(st,pair,d):
 if pair not in st["positions"]:return
 p=st["positions"][pair];direction=1 if p["side"]=="LONG" else -1
 fresh=d[d.index>pd.Timestamp(p["time"])]
 for t,k in fresh.iterrows():
  if pair not in st["positions"]:break
  high,low=float(k["high"]),float(k["low"])
  stop=(low<=p["sl"]) if direction==1 else (high>=p["sl"])
  if stop:
   close(st,pair,p,p["sl"],"STOP" if p["lock"]==0 else "LOCK_STOP",str(t));break
  reach=(high-p["entry"]) if direction==1 else (p["entry"]-low)
  if reach>=4*p["risk"]:
   close(st,pair,p,p["entry"]+direction*4*p["risk"],"4R_TP",str(t));break
  if reach>=3*p["risk"] and p["lock"]<3:
   p["sl"]=p["entry"]+direction*3*p["risk"];p["lock"]=3
   alert(f"COINDCX PAPER {pair} 3R SL LOCK")
  elif reach>=2*p["risk"] and p["lock"]<2:
   p["sl"]=p["entry"]+direction*2*p["risk"];p["lock"]=2
   alert(f"COINDCX PAPER {pair} 2R SL LOCK")
  elif LOCK1_R>0 and reach>=p["risk"] and p["lock"]<1:
   p["sl"]=p["entry"]+direction*LOCK1_R*p["risk"];p["lock"]=1
   alert(f"COINDCX PAPER {pair} 1R SL LOCK (+{LOCK1_R}R)")
  p["time"]=str(t)
 # Close dead trades so they do not block MAX_OPEN slots.
 if pair in st["positions"] and p.get("opened") and not d.empty:
  held=(pd.Timestamp.now(tz="UTC")-pd.Timestamp(p["opened"])).total_seconds()/3600
  if held>MAX_HOLD_HOURS:
   close(st,pair,p,float(d["close"].iloc[-1]),"TIMEOUT",str(d.index[-1]))
def main():
 st=load()
 instruments=get(API+"/exchange/v1/derivatives/futures/data/active_instruments",{"margin_currency_short_name[]":"USDT"})
 active={x for x in instruments if x.startswith("B-") and x.endswith("_USDT")}
 prices=get(PUB+"/market_data/v3/current_prices/futures/rt").get("prices",{})
 ranking=sorted(((p,float(v.get("v",0))) for p,v in prices.items() if p in active and isinstance(v,dict)),key=lambda x:x[1],reverse=True)
 # Select only futures with at least 10 million USDT 24h turnover.
 # CoinDCX ticker v is base-asset volume; convert to USDT using last price.
 eligible=[]
 for p,v in ranking:
  ticker=prices[p]
  try:
   last_price=float(ticker.get("ls",0))
   turnover=v*last_price
   if math.isfinite(turnover) and turnover>=10_000_000:
    eligible.append(p)
  except (TypeError,ValueError,OverflowError):
   continue
 selected=eligible[:TOP]
 # Always manage existing paper positions even if volume falls below threshold.
 selected=list(dict.fromkeys(list(st["positions"])+selected))
 print(f"VOLUME FILTER: {len(eligible)} eligible (24h >= 10M USDT); {len(selected)} selected including open positions",flush=True)
 if not selected:raise RuntimeError("No CoinDCX USDT futures instruments returned")
 for pair in selected:
  try:
   d,bars=frame(pair)
   if bars is None:continue
   manage(st,pair,d)
   last=str(bars["time"].iloc[-1])
   if pair in st["positions"] or st["last_candle"].get(pair)==last:continue
   st["last_candle"][pair]=last
   if len(st["positions"])>=MAX_OPEN:continue
   res=analyze(bars)
   if not res:continue
   # HTF: 1-hour EMA alignment
   h=candles(pair,"60",12)
   if len(h)<200:continue
   c=h["close"];e50=float(ema(c,50).iloc[-1]);e200=float(ema(c,200).iloc[-1])
   bias=1 if c.iloc[-1]>e50>e200 else -1 if c.iloc[-1]<e50<e200 else 0
   side=decide(res,MIN_SCORE,bias)
   if not side:continue
   direction=1 if side=="LONG" else -1
   # Enter at the latest CLOSED 5m close (not the older 15m close) and only judge
   # candles after it, so nothing before the entry can trigger the stop.
   price5=float(d["close"].iloc[-1])
   if abs(price5/float(res["price"])-1)*100>MAX_ENTRY_DEV_PCT:
    print("SKIP",pair,"entry_ran_away",flush=True);continue
   risk=1.5*float(res["atr"])
   if not math.isfinite(risk) or risk<=0:continue
   stop_pct=risk/price5*100
   if stop_pct>MAX_STOP_PCT:
    print(f"SKIP {pair} stop_too_wide {stop_pct:.2f}%",flush=True);continue
   if stop_pct<MIN_STOP_PCT:
    print(f"SKIP {pair} stop_too_tight {stop_pct:.2f}%",flush=True);continue
   entry=price5*(1+direction*SLIP)  # entry slippage works against the position
   qty=min(RISK_USDT/risk,NOTIONAL/entry)
   matched=[name for name,value in res["scores"].items() if value==direction]
   p={"side":side,"entry":entry,"risk":risk,"qty":qty,"sl":entry-direction*risk,"lock":0,"time":str(d.index[-1]),"opened":str(d.index[-1]),"score":res["score"],"strategies":matched}
   st["positions"][pair]=p
   alert(f"COINDCX PAPER OPEN {side} {pair} @ {entry:.6g} | Score {res['score']} | Strategies {','.join(matched)} | Risk {RISK_USDT:g} USDT, notional {qty*entry:.0f} | SL {p['sl']:.6g} ({stop_pct:.2f}%) | TP {entry+direction*4*risk:.6g}")
  except Exception as exc:
   print("SKIP",pair,type(exc).__name__,str(exc)[:160],flush=True)
  finally:save(st)
 print(f"PAPER SUMMARY open={len(st['positions'])} closed={st['closed']} wins={st['wins']} realized={st['realized_pnl']:+.2f} USDT")
if __name__=="__main__":main()
