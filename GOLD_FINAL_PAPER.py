"""GOLD paper monitor: exchange-data checks; no live orders, fail-closed entries."""
import json,os,sys
from datetime import datetime,timezone,timedelta
from zoneinfo import ZoneInfo
import requests,pandas as pd
BASE="https://public.coindcx.com/market_data/candlesticks"
ACTIVE="https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments"
IST=ZoneInfo("Asia/Kolkata")
def fetch(url,params=None):
 r=requests.get(url,params=params,timeout=25);r.raise_for_status();return r.json()
def notify(s):
 print(s,flush=True)
 token=os.getenv("TG_TOKEN");chat=os.getenv("TG_CHAT_ID")
 if token and chat:
  r=requests.post("https://api.telegram.org/bot"+token+"/sendMessage",data={"chat_id":chat,"text":s},timeout=15)
  r.raise_for_status()
def candles(pair,interval):
 now=datetime.now(timezone.utc)
 data=fetch(BASE,{"pair":pair,"from":int((now-timedelta(days=4)).timestamp()),"to":int(now.timestamp()),"resolution":interval,"pcode":"f"})
 if isinstance(data,dict):data=data.get("data",[])
 if not isinstance(data,list) or not data:raise ValueError("Missing candles")
 d=pd.DataFrame(data)
 for col in ("open","high","low","close","volume"):d[col]=pd.to_numeric(d[col])
 d["ts"]=pd.to_datetime(d["time"],unit="ms",utc=True)
 d=d.sort_values("ts").drop_duplicates("ts")
 d=d[d.ts+pd.Timedelta(minutes=int(interval))<=pd.Timestamp.now(tz="UTC")]
 if len(d)<200:raise ValueError("Insufficient closed candles")
 return d
def main():
 now=datetime.now(timezone.utc);local=now.astimezone(IST)
 data=fetch(ACTIVE,{"margin_currency_short_name[]":"USDT"})
 if isinstance(data,dict):data=data.get("data",[])
 if not isinstance(data,list):raise ValueError("Active instrument format invalid")
 pairs={x.get("pair",str(x)) if isinstance(x,dict) else str(x) for x in data}
 status=[]
 for symbol in ("XAUUSDT","PAXGUSDT"):
  pair=next((p for p in pairs if p.endswith("-"+symbol[:-4]+"_USDT")),None)
  if not pair:status.append(symbol+": not listed as active futures");continue
  try:
   d=candles(pair,"5")
   status.append(symbol+": "+str(len(d))+" closed 5m candles; latest "+str(d.ts.iloc[-1]))
  except Exception as e:status.append(symbol+": data error "+str(e))
 print("\n".join(status))
 print("NO PAPER ENTRY: original strategy needs verified true session Volume Profile, Setup C, basis/funding thresholds, and dated economic news. Not bypassing rules.")
 if os.getenv("GOLD_SEND_STATUS")=="1":notify("GOLD PAPER DATA CHECK\n"+"\n".join(status)+"\nEntries blocked until every v2 rule is validated. No live orders.")
if __name__=="__main__":
 try:main()
 except Exception as e:print("GOLD ERROR:",e,file=sys.stderr);sys.exit(1)
