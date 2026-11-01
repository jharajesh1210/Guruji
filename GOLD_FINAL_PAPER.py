"""CoinDCX GOLD paper engine. NEVER sends orders. Safety filters fail closed."""
import os,json,sys,math,datetime as dt
from zoneinfo import ZoneInfo
import requests,pandas as pd,numpy as np
IST=ZoneInfo("Asia/Kolkata"); UTC=dt.timezone.utc
BASE="https://api.coindcx.com/exchange/v1/derivatives/futures/data"
STATE="GOLD_FINAL_PAPER_STATE.json"; HISTORY="GOLD_FINAL_PAPER_TRADES.csv"
CAPITAL=float(os.getenv("GOLD_CAPITAL","1000"))
FEE=float(os.getenv("GOLD_FEE_RATE","0.0005"))
SLIPPAGE=float(os.getenv("GOLD_SLIPPAGE_RATE","0.0002"))
def get(url,params=None):
 r=requests.get(url,params=params,timeout=25);r.raise_for_status();return r.json()
def notify(s):
 print(s,flush=True)
 if os.getenv("TG_TOKEN") and os.getenv("TG_CHAT_ID"):
  r=requests.post("https://api.telegram.org/bot"+os.environ["TG_TOKEN"]+"/sendMessage",data={"chat_id":os.environ["TG_CHAT_ID"],"text":s},timeout=15)
  r.raise_for_status()
def load():
 if os.path.exists(STATE):
  with open(STATE) as f:return json.load(f)
 return {"equity":CAPITAL,"peak":CAPITAL,"position":None,"trades_today":0,"losses_today":0,"date":"","week":"","week_equity":CAPITAL,"last_exit_ts":None,"last_bar":None}
def save(s):
 with open(STATE,"w") as f:json.dump(s,f,indent=2)
def logtrade(t):
 import csv
 exists=os.path.exists(HISTORY)
 with open(HISTORY,"a",newline="") as f:
  w=csv.DictWriter(f,fieldnames=["time","pair","setup","side","entry","exit","qty","reason","pnl","r"])
  if not exists:w.writeheader()
  w.writerow(t)
def candles(pair,interval,days):
 end=dt.datetime.now(UTC);start=end-dt.timedelta(days=days)
 x=get(BASE+"/candles",{"pair":pair,"interval":interval,"startTime":int(start.timestamp()*1000),"endTime":int(end.timestamp()*1000)})
 if isinstance(x,dict):x=x.get("data",[])
 if not isinstance(x,list) or not x:raise ValueError("No candles "+pair+" "+interval)
 d=pd.DataFrame(x)
 for c in ["open","high","low","close","volume"]:d[c]=pd.to_numeric(d[c])
 t="time" if "time" in d else "timestamp"
 nums=pd.to_numeric(d[t])
 d["ts"]=pd.to_datetime(nums,unit="ms" if nums.median()>1e11 else "s",utc=True)
 d=d.sort_values("ts").drop_duplicates("ts")
 mins={"5m":5,"15m":15,"1h":60}[interval]
 d=d[d.ts+pd.Timedelta(minutes=mins)<=pd.Timestamp.now(tz="UTC")].reset_index(drop=True)
 if len(d)<205:raise ValueError("Not enough closed "+interval+" candles: "+str(len(d)))
 return d
def ema(s,n):return s.ewm(span=n,adjust=False).mean()
def atr(d):
 p=d.close.shift()
 tr=pd.concat([d.high-d.low,(d.high-p).abs(),(d.low-p).abs()],axis=1).max(axis=1)
 return tr.ewm(alpha=1/14,adjust=False).mean()
def adx(d):
 up=d.high.diff();down=-d.low.diff();a=atr(d).replace(0,np.nan)
 plus=100*up.where((up>down)&(up>0),0).ewm(alpha=1/14,adjust=False).mean()/a
 minus=100*down.where((down>up)&(down>0),0).ewm(alpha=1/14,adjust=False).mean()/a
 return (100*(plus-minus).abs()/(plus+minus).replace(0,np.nan)).ewm(alpha=1/14,adjust=False).mean()
def vwap(d):
 local=d.ts.dt.tz_convert(IST)
 # 05:30 IST reset equals midnight UTC
 grp=d.ts.dt.floor("D");tp=(d.high+d.low+d.close)/3
 return (tp*d.volume).groupby(grp).cumsum()/d.volume.groupby(grp).cumsum().replace(0,np.nan)
def session(now):
 if now.weekday()>4:return None
 m=now.hour*60+now.minute
 if 13*60+30<=m<16*60+30:return "LONDON"
 if 18*60+30<=m<22*60+30:return "NEW_YORK"
 return None
def news_verified(now):
 # Verified date-specific UTC events: {"verified_until":"ISO UTC","events":[{"start":"ISO UTC","end":"ISO UTC"}]}
 try:
  x=json.load(open("GOLD_NEWS_WINDOWS.json"))
  until=dt.datetime.fromisoformat(x["verified_until"].replace("Z","+00:00"))
  if until<now.astimezone(UTC):return False
  for e in x["events"]:
   a=dt.datetime.fromisoformat(e["start"].replace("Z","+00:00"))-dt.timedelta(minutes=30)
   b=dt.datetime.fromisoformat(e["end"].replace("Z","+00:00"))+dt.timedelta(minutes=30)
   if a<=now.astimezone(UTC)<=b:return False
  return True
 except (FileNotFoundError,ValueError,KeyError,TypeError):return False
def setup_b(d,b,h):
 # last closed breakout bar, previous candle had squeeze; next candle confirmation
 if len(d)<205:return None
 x=d.iloc[-2];confirm=d.iloc[-1];prior=d.iloc[:-2]
 mid=d.close.rolling(20).mean();sd=d.close.rolling(20).std(ddof=0)
 upper=mid+2*sd;lower=mid-2*sd
 width=(upper-lower)/mid.replace(0,np.nan)
 squeeze=float(width.iloc[-3])<=float(width.iloc[-103:-3].quantile(.2))
 a=atr(d);atrval=float(a.iloc[-2])
 kelt_upper=ema(d.close,20)+1.5*a;kelt_lower=ema(d.close,20)-1.5*a
 squeeze=squeeze or (upper.iloc[-3]<kelt_upper.iloc[-3] and lower.iloc[-3]>kelt_lower.iloc[-3])
 if not squeeze:return None
 if abs(x.close-x.open)/max(x.high-x.low,1e-12)<.6:return None
 if x.volume<=1.5*float(d.volume.iloc[-22:-2].mean()):return None
 direction=1 if x.close>upper.iloc[-2] and confirm.high>x.high else -1 if x.close<lower.iloc[-2] and confirm.low<x.low else 0
 if not direction:return None
 if direction*(x.close-x.open)<=0:return None
 if direction*(x.close-ema(d.close,200).iloc[-2])<=0:return None
 if direction*(b.close.iloc[-1]-ema(b.close,200).iloc[-1])<=0:return None
 if direction*(b.close.iloc[-1]-b.open.iloc[-1])<=0:return None
 if direction*(ema(h.close,50).iloc[-1]-ema(h.close,200).iloc[-1])<=0:return None
 if direction*(x.close-vwap(d).iloc[-2])<=0:return None
 if not adx(d).iloc[-2]>20:return None
 entry=float(x.high if direction>0 else x.low)
 # A paper fill on the confirmation candle extreme only; conservative slippage.
 entry*=1+direction*SLIPPAGE
 stop_candle=float(x.low if direction>0 else x.high)
 stop_atr=entry-direction*1.5*atrval
 sl=max(stop_candle,stop_atr) if direction>0 else min(stop_candle,stop_atr)
 risk=abs(entry-sl)
 if risk<=0 or risk>1.5*atrval:return None
 # Roundtrip fees + spread/slippage must be <= 15% of one R
 if (2*FEE+2*SLIPPAGE)*entry>.15*risk:return None
 return {"setup":"B","side":"LONG" if direction>0 else "SHORT","direction":direction,"entry":entry,"stop":sl,"risk":risk,"bar":str(confirm.ts),"start":str(confirm.ts),"bars":0,"level":0}
def finish(s,p,price,reason,now):
 direction=p["direction"];qty=p["qty"];entry=p["entry"]
 exit_price=price*(1-direction*SLIPPAGE)
 pnl=direction*(exit_price-entry)*qty-(exit_price+entry)*qty*FEE
 r=pnl/(p["risk"]*qty) if p["risk"]*qty else 0
 s["equity"]+=pnl;s["peak"]=max(s["peak"],s["equity"])
 if pnl<0:s["losses_today"]+=1
 s["last_exit_ts"]=now.isoformat()
 logtrade({"time":now.isoformat(),"pair":p["pair"],"setup":p["setup"],"side":p["side"],"entry":entry,"exit":exit_price,"qty":qty,"reason":reason,"pnl":round(pnl,4),"r":round(r,3)})
 s["position"]=None
 notify("GOLD PAPER EXIT "+p["pair"]+" "+p["side"]+" "+reason+" | P&L "+str(round(pnl,2))+" USDT | Equity "+str(round(s["equity"],2)))
def manage(s,d,now):
 p=s.get("position")
 if not p:return
 local=now.astimezone(IST);m=local.hour*60+local.minute
 end=16*60+30 if p["session"]=="LONDON" else 22*60+30
 price=float(d.close.iloc[-1]);direction=p["direction"];r=p["risk"]
 if m>=end or local.weekday()>4:
  finish(s,p,price,"SESSION_EXIT",now);return
 if not news_verified(now):
  # open position protection: exit if calendar is not verified or blackout
  finish(s,p,price,"NEWS_SAFETY_EXIT",now);return
 # Original specification uses CLOSED candle for lock levels
 multiple=direction*(price-p["entry"])/r
 if multiple>=5:
  finish(s,p,price,"5R_FINAL",now);return
 level=min(4,max(0,math.floor(multiple+1e-9)))
 if level>p["level"]:
  p["level"]=level
  p["stop"]=p["entry"]+direction*max(0,level-1)*r
  notify("GOLD PAPER SL LOCK "+p["pair"]+" "+str(level)+"R | SL "+str(round(p["stop"],4)))
 if direction*(price-p["stop"])<=0:
  finish(s,p,price,"STOP_LOCK",now);return
 p["bars"]+=1
 if p["bars"]>=30 and p["level"]==0:finish(s,p,price,"STALL_EXIT",now)

def source_news_window(now):
 # Faithful to uploaded GOLD_FINAL_STRATEGY(1).py: approximate IST slots,
 # NOT an actual economic calendar. These are additional blocks.
 local=now.astimezone(IST); minute=local.hour*60+local.minute
 for slot in (18*60+45,19*60+15,23*60+45,30):
  if min(abs(minute-slot),1440-abs(minute-slot))<=30:return True
 return False
def setup_a_source_proxy(d,b,h):
 # Uploaded PY explicitly defines this as a rolling-quantile PROXY, not true VP.
 x=d.iloc[-2];confirm=d.iloc[-1]
 if len(d)<290:return None
 if abs(x.close-x.open)/max(x.high-x.low,1e-12)<.6:return None
 if x.volume<=1.5*float(d.volume.iloc[-22:-2].mean()):return None
 vah=float(d.high.rolling(288).quantile(.7).iloc[-2])
 val=float(d.low.rolling(288).quantile(.3).iloc[-2])
 direction=1 if x.close>vah and confirm.high>x.high else -1 if x.close<val and confirm.low<x.low else 0
 if not direction or direction*(x.close-x.open)<=0:return None
 if direction*(x.close-ema(d.close,200).iloc[-2])<=0:return None
 if direction*(b.close.iloc[-1]-ema(b.close,200).iloc[-1])<=0:return None
 if direction*(b.close.iloc[-1]-b.open.iloc[-1])<=0:return None
 if direction*(ema(h.close,50).iloc[-1]-ema(h.close,200).iloc[-1])<=0:return None
 if direction*(x.close-vwap(d).iloc[-2])<=0 or adx(d).iloc[-2]<=20:return None
 entry=float(x.high if direction>0 else x.low)*(1+direction*SLIPPAGE)
 stop_candle=float(x.low if direction>0 else x.high)
 stop_atr=entry-direction*1.5*float(atr(d).iloc[-2])
 stop=max(stop_candle,stop_atr) if direction>0 else min(stop_candle,stop_atr)
 risk=abs(entry-stop)
 if risk<=0 or (2*FEE+2*SLIPPAGE)*entry>.15*risk:return None
 return {"setup":"A_SOURCE_PROXY","side":"LONG" if direction>0 else "SHORT","direction":direction,"entry":entry,"stop":stop,"risk":risk,"bar":str(confirm.ts),"start":str(confirm.ts),"bars":0,"level":0}

def main():
 now=dt.datetime.now(UTC);local=now.astimezone(IST)
 s=load()
 day=local.date().isoformat();week=str(local.isocalendar().year)+"-"+str(local.isocalendar().week)
 if s["date"]!=day:s["date"]=day;s["trades_today"]=0;s["losses_today"]=0;s["day_equity"]=s["equity"]
 if s["week"]!=week:s["week"]=week;s["week_equity"]=s["equity"]
 active=get(BASE+"/active_instruments",{"margin_currency_short_name[]":"USDT"})
 if isinstance(active,dict):active=active.get("data",[])
 pairs=set(active)
 target={}
 for sym in ("XAU","PAXG"):
  for p in pairs:
   if isinstance(p,str) and p.endswith("-"+sym+"_USDT"):target[sym]=p;break
 if s.get("position"):
  p=s["position"];d=candles(p["pair"],"5m",5)
  if str(d.ts.iloc[-1])!=s.get("last_bar"):
   s["last_bar"]=str(d.ts.iloc[-1]);manage(s,d,now)
  save(s);return
 sess=session(local)
 if not sess:print("NO ENTRY: session closed");save(s);return
 if not news_verified(now):print("NO ENTRY: verified economic news calendar missing/blackout");save(s);return
 if s["trades_today"]>=3 or s["losses_today"]>=2 or s["equity"]<=s.get("day_equity",CAPITAL)*.97 or s["equity"]<=s["week_equity"]*.95 or s["equity"]<=s["peak"]*.9:
  print("NO ENTRY: risk guard");save(s);return
 if s.get("last_exit_ts") and (now-dt.datetime.fromisoformat(s["last_exit_ts"])).total_seconds()<25*60:
  print("NO ENTRY: cooldown");save(s);return
 # The uploaded Python does not define a numerical funding/basis threshold.
 # Do not silently invent one. Source-only mode can simulate Setup A proxy/B,
 # but this is not equivalent to the complete v2 spec.
 if set(target)!={"XAU","PAXG"}:
  print("NO ENTRY: XAU and PAXG futures must both be available");save(s);return
 if source_news_window(now):
  print("NO ENTRY: uploaded Python approximate high-impact news window");save(s);return
 # Full-v2 compliance requires a real calendar; without it new entries remain blocked.
 if not news_verified(now):
  print("NO ENTRY: actual economic calendar unavailable; original full-v2 safety rule enforced");save(s);return
 for symbol in ("XAU","PAXG"):
  pair=target[symbol]
  try:
   d=candles(pair,"5m",5);b=candles(pair,"15m",12);h=candles(pair,"1h",40)
   bar=str(d.ts.iloc[-1])
   if s.get("last_entry_bar")==bar:continue
   sig=setup_b(d,b,h) or setup_a_source_proxy(d,b,h)
   if not sig:continue
   # Full-v2 basis/funding validation not yet provided. Never bypass these.
   print("CANDIDATE",symbol,sig["setup"],sig["side"],"BLOCKED: funding/basis validation absent")
  except Exception as e:print("NO ENTRY",symbol,repr(e))
 save(s)
if __name__=="__main__":
 try:main()
 except Exception as e:
  print("GOLD PAPER ERROR:",repr(e),file=sys.stderr);sys.exit(1)
