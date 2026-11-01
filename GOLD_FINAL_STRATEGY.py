"""GOLD v2 paper-only scanner. Fail-closed: missing verified data means NO TRADE."""
import os,json,requests,datetime as dt
from zoneinfo import ZoneInfo
import numpy as np,pandas as pd
UTC=dt.timezone.utc
IST=ZoneInfo("Asia/Kolkata")
BASE="https://api.coindcx.com/exchange/v1/derivatives/futures/data/candles"
STATE="GOLD_FINAL_STATE.json"
PAIRS=["XAUUSDT","PAXGUSDT"]
def alert(msg):
 print(msg,flush=True)
 token=os.getenv("TG_TOKEN"); chat=os.getenv("TG_CHAT_ID")
 if token and chat:
  requests.post("https://api.telegram.org/bot"+token+"/sendMessage",data={"chat_id":chat,"text":msg},timeout=15).raise_for_status()
def candles(pair,tf,n=350):
 r=requests.get(BASE,params={"pair":pair,"interval":tf,"limit":n},timeout=25)
 r.raise_for_status(); x=r.json()
 if isinstance(x,dict): x=x.get("data",[])
 if not isinstance(x,list) or not x: raise ValueError("Missing CoinDCX futures candles "+pair+" "+tf)
 d=pd.DataFrame(x)
 for c in ("open","high","low","close","volume"):
  d[c]=pd.to_numeric(d[c],errors="raise")
 t="time" if "time" in d else "timestamp"
 d["ts"]=pd.to_datetime(pd.to_numeric(d[t]),unit="ms",utc=True)
 d=d.sort_values("ts").drop_duplicates("ts")
 minutes={"5m":5,"15m":15,"1h":60}[tf]
 d=d[d.ts+pd.Timedelta(minutes=minutes)<=pd.Timestamp.now(tz="UTC")].reset_index(drop=True)
 if len(d)<220: raise ValueError("Insufficient closed candles")
 return d
def ema(s,n):return s.ewm(span=n,adjust=False).mean()
def atr(d):
 p=d.close.shift()
 tr=pd.concat([d.high-d.low,(d.high-p).abs(),(d.low-p).abs()],axis=1).max(axis=1)
 return tr.ewm(alpha=1/14,adjust=False).mean()
def adx(d):
 up=d.high.diff();dn=-d.low.diff();a=atr(d)
 p=100*up.where((up>dn)&(up>0),0).ewm(alpha=1/14,adjust=False).mean()/a.replace(0,np.nan)
 m=100*dn.where((dn>up)&(dn>0),0).ewm(alpha=1/14,adjust=False).mean()/a.replace(0,np.nan)
 return (100*(p-m).abs()/(p+m).replace(0,np.nan)).ewm(alpha=1/14,adjust=False).mean()
def vwap(d):
 g=d.ts.dt.floor("D");tp=(d.high+d.low+d.close)/3
 return (tp*d.volume).groupby(g).cumsum()/d.volume.groupby(g).cumsum().replace(0,np.nan)
def session(now):
 if now.weekday()>4:return None
 m=now.hour*60+now.minute
 if 13*60+30<=m<16*60+30:return "LONDON"
 if 18*60+30<=m<22*60+30:return "NEW_YORK"
 return None
def news_ok(now):
 # Only explicitly verified UTC blackout windows; absent/stale => no new trades.
 path="GOLD_NEWS_WINDOWS.json"
 if not os.path.exists(path):return False,"verified news calendar missing"
 try:
  data=json.load(open(path,encoding="utf-8"))
  checked=dt.datetime.fromisoformat(data["verified_until"].replace("Z","+00:00"))
  if checked<now.astimezone(UTC):return False,"news calendar expired"
  for e in data["events"]:
   start=dt.datetime.fromisoformat(e["start"].replace("Z","+00:00"))
   end=dt.datetime.fromisoformat(e["end"].replace("Z","+00:00"))
   if start-dt.timedelta(minutes=30)<=now.astimezone(UTC)<=end+dt.timedelta(minutes=30):
    return False,"high-impact news blackout"
  return True,"OK"
 except Exception as exc:return False,"invalid news calendar: "+str(exc)
def candidate(d,b,c):
 x=d.iloc[-1]; prev=d.iloc[-2]; price=float(x.close)
 trend=1 if price>ema(d.close,200).iloc[-1] else -1
 if (float(b.close.iloc[-1])-float(ema(b.close,200).iloc[-1]))*trend<=0:return None
 if (float(b.close.iloc[-1])-float(b.open.iloc[-1]))*trend<=0:return None
 if (float(ema(c.close,50).iloc[-1])-float(ema(c.close,200).iloc[-1]))*trend<=0:return None
 if (price-float(vwap(d).iloc[-1]))*trend<=0:return None
 if not adx(d).iloc[-1]>20:return None
 body=abs(float(x.close-x.open))/max(float(x.high-x.low),1e-12)
 if body<.6 or float(x.volume)<=1.5*float(d.volume.iloc[-21:-1].mean()):return None
 if (x.close-x.open)*trend<=0:return None
 # Volume-profile requires actual price-level traded volume, not candle-volume approximation.
 # Fail closed until verified exchange trade-level profile and basis/funding feeds are integrated.
 return {"direction":"BUY" if trend>0 else "SELL","reason":"TREND_VOLUME_ONLY_NOT_EXECUTABLE"}
def main():
 now=dt.datetime.now(IST);sess=session(now)
 if not sess:print("NO TRADE: outside permitted session/weekend");return
 ok,reason=news_ok(now)
 if not ok:print("NO TRADE:",reason);return
 # Do not create paper entries without all original strategy prerequisites.
 for pair in PAIRS:
  try:
   d=candles(pair,"5m");b=candles(pair,"15m");c=candles(pair,"1h")
   sig=candidate(d,b,c)
   print(pair,"NO TRADE: POC/VAH/VAL, squeeze, basis/funding and full R-ladder not yet validated",sig)
  except Exception as exc:print(pair,"NO TRADE: exchange validation failed:",str(exc))
if __name__=="__main__":main()
