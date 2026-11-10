#!/usr/bin/env python3
"""NIFTY50 Intraday Strategy V1.0 — Angel One data, PAPER ONLY (no live orders)."""
import os, sys, json, csv, time, logging
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
try:
    import pyotp
    from SmartApi import SmartConnect
except ImportError as _import_err:
    pyotp = None
    SmartConnect = None
    IMPORT_ERROR = repr(_import_err)
else:
    IMPORT_ERROR = ""

IST=ZoneInfo("Asia/Kolkata")
ROOT=Path(__file__).resolve().parent
OUT=ROOT/"nifty50_output"
CFG_PATH=ROOT/"nifty50_config.json"
NSE_URL="https://www.nseindia.com/api/market-data-pre-open?key=NIFTY"
MASTER_URL="https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
DEFAULT={
 "mode":"PAPER","selected_sector_count":3,"top_gainers_losers_count":5,
 "entry_start":"09:30","entry_cutoff":"14:55","square_off":"15:20",
 "poll_seconds":25,"paper_capital_per_trade":100000,
 "initial_sl_method":"SIGNAL_CANDLE_EXTREME","initial_sl_buffer_pct":0.0,
 "clear_reverse_method":"OPPOSITE_COLOUR_CANDLE_CLOSE",
 "symbols":["ADANIENT","ADANIPORTS","APOLLOHOSP","ASIANPAINT","AXISBANK","BAJAJ-AUTO","BAJFINANCE","BAJAJFINSV","BEL","BHARTIARTL","CIPLA","COALINDIA","DRREDDY","EICHERMOT","ETERNAL","GRASIM","HCLTECH","HDFCBANK","HDFCLIFE","HEROMOTOCO","HINDALCO","HINDUNILVR","ICICIBANK","INDUSINDBK","INFY","ITC","JIOFIN","JSWSTEEL","KOTAKBANK","LT","M&M","MARUTI","NESTLEIND","NTPC","ONGC","POWERGRID","RELIANCE","SBILIFE","SBIN","SHRIRAMFIN","SUNPHARMA","TATACONSUM","TATASTEEL","TCS","TECHM","TITAN","TRENT","ULTRACEMCO","WIPRO"],
 "sector_map":{"ADANIENT":"Metals/Mining","ADANIPORTS":"Infrastructure","APOLLOHOSP":"Healthcare","ASIANPAINT":"Consumer","AXISBANK":"Banking","BAJAJ-AUTO":"Automobile","BAJFINANCE":"Financials","BAJAJFINSV":"Financials","BEL":"Industrials","BHARTIARTL":"Telecom","CIPLA":"Healthcare","COALINDIA":"Energy/Mining","DRREDDY":"Healthcare","EICHERMOT":"Automobile","ETERNAL":"Consumer/Internet","GRASIM":"Cement/Chemicals","HCLTECH":"IT","HDFCBANK":"Banking","HDFCLIFE":"Insurance","HEROMOTOCO":"Automobile","HINDALCO":"Metals","HINDUNILVR":"FMCG","ICICIBANK":"Banking","INDUSINDBK":"Banking","INFY":"IT","ITC":"FMCG","JIOFIN":"Financials","JSWSTEEL":"Metals","KOTAKBANK":"Banking","LT":"Infrastructure","M&M":"Automobile","MARUTI":"Automobile","NESTLEIND":"FMCG","NTPC":"Power","ONGC":"Energy","POWERGRID":"Power","RELIANCE":"Energy/Retail","SBILIFE":"Insurance","SBIN":"Banking","SHRIRAMFIN":"Financials","SUNPHARMA":"Healthcare","TATACONSUM":"FMCG","TATASTEEL":"Metals","TCS":"IT","TECHM":"IT","TITAN":"Consumer","TRENT":"Retail","ULTRACEMCO":"Cement","WIPRO":"IT"}
}
def now(): return datetime.now(IST)
def hhmm(s): return datetime.strptime(s,"%H:%M").time()
def load_cfg():
 if not CFG_PATH.exists(): CFG_PATH.write_text(json.dumps(DEFAULT,indent=2),encoding="utf-8")
 x=json.loads(CFG_PATH.read_text(encoding="utf-8")); c=dict(DEFAULT); c.update(x)
 c["symbols"]=x.get("symbols",DEFAULT["symbols"]); c["sector_map"]=x.get("sector_map",DEFAULT["sector_map"])
 return c
def fnum(v,default=None):
 try:return float(str(v).replace(",","").replace("%","").strip())
 except (TypeError,ValueError):return default
def login():
 names=["ANGEL_API_KEY","ANGEL_CLIENT_CODE","ANGEL_PASSWORD","ANGEL_TOTP_SECRET"]
 missing=[x for x in names if not os.getenv(x)]
 if missing: raise RuntimeError("Missing GitHub Secrets: "+", ".join(missing))
 if SmartConnect is None or pyotp is None: raise RuntimeError(f"Dependencies missing; install requirements file. Import error: {IMPORT_ERROR}")
 api=SmartConnect(api_key=os.environ["ANGEL_API_KEY"])
 r=api.generateSession(os.environ["ANGEL_CLIENT_CODE"],os.environ["ANGEL_PASSWORD"],pyotp.TOTP(os.environ["ANGEL_TOTP_SECRET"]).now())
 if not r or r.get("status") is not True: raise RuntimeError("Angel One login failed: "+str(r))
 return api
def preopen(symbols):
 s=requests.Session(); s.headers.update({"User-Agent":"Mozilla/5.0 Chrome/122 Safari/537.36","Accept":"application/json, text/plain, */*","Referer":"https://www.nseindia.com/"})
 s.get("https://www.nseindia.com",timeout=20); r=s.get(NSE_URL,timeout=25); r.raise_for_status()
 rows=r.json().get("data")
 if not isinstance(rows,list): raise RuntimeError("NSE pre-open response schema changed.")
 out={}
 for z in rows:
  m=z.get("metadata") or {}; d=z.get("detail") or {}; p=d.get("preOpenMarket") or {}
  sym=str(m.get("symbol") or d.get("symbol") or "").upper()
  if sym not in symbols: continue
  px=fnum(m.get("iep",p.get("iep",m.get("lastPrice")))); prev=fnum(m.get("previousClose",p.get("previousClose")))
  if not px or not prev: continue
  out[sym]={"symbol":sym,"preopen_price":px,"previous_close":prev,"change_pct":(px-prev)/prev*100}
 if len(out)<35: raise RuntimeError(f"Only {len(out)} NIFTY50 pre-open rows parsed ({len(out)}); refusing partial data.")
 return out
def instrument_tokens(cfg):
 OUT.mkdir(exist_ok=True); cache=OUT/"OpenAPIScripMaster.json"
 if cache.exists() and time.time()-cache.stat().st_mtime<86400: data=json.loads(cache.read_text(encoding="utf-8"))
 else:
  r=requests.get(MASTER_URL,timeout=45); r.raise_for_status(); data=r.json()
  if not isinstance(data,list): raise RuntimeError("Angel One instrument master schema changed.")
  cache.write_text(json.dumps(data),encoding="utf-8")
 wanted=set(cfg["symbols"]); result={}
 for x in data:
  sym=str(x.get("symbol","")).upper()
  if x.get("exch_seg")=="NSE" and sym.endswith("-EQ") and sym[:-3] in wanted:
   result[sym[:-3]]={"token":str(x["token"]),"tradingsymbol":sym}
 if len(result)<40: raise RuntimeError(f"Only {len(result)} NSE cash tokens mapped.")
 return result
def ltp(api,tokens,symbols):
 out={}
 for sym in symbols:
  t=tokens.get(sym)
  if not t: continue
  try:
   r=api.ltpData("NSE",t["tradingsymbol"],t["token"])
   if r and r.get("status") and r.get("data"): out[sym]=float(r["data"]["ltp"])
  except Exception as e: logging.warning("LTP %s failed: %s",sym,e)
  time.sleep(.12)
 return out
def select(pre,cfg):
 a=sum(x["change_pct"]>0 for x in pre.values()); d=sum(x["change_pct"]<0 for x in pre.values()); u=len(pre)-a-d
 bias="BUY" if a>d else "SELL" if d>a else "NEUTRAL"
 sector_vals={}
 for sym,x in pre.items(): sector_vals.setdefault(cfg["sector_map"].get(sym,"Unknown"),[]).append(x["change_pct"])
 avgs={k:sum(v)/len(v) for k,v in sector_vals.items()}
 order=sorted(avgs.items(),key=lambda z:z[1],reverse=(bias=="BUY"))
 sectors=[s for s,_ in order[:int(cfg["selected_sector_count"])]] if bias!="NEUTRAL" else []
 candidates=[]
 for sym,x in pre.items():
  ch=x["change_pct"]; sec=cfg["sector_map"].get(sym,"Unknown")
  if sec not in sectors: continue
  candidates.append({**x,"sector":sec,"direction":bias})
 return bias,a,d,u,candidates,{"selected_sectors":sectors,"sector_average_change_pct":avgs}
def rank(candidates,prices,bias,cfg):
 out=[]
 for x in candidates:
  px=prices.get(x["symbol"])
  if not px: continue
  pct=(px-x["previous_close"])/x["previous_close"]*100
  if bias=="BUY" and not (0<pct<5): continue
  if bias=="SELL" and not (-5<pct<0): continue
  out.append({**x,"selection_price":px,"selection_change_pct":pct})
 out.sort(key=lambda x:x["selection_change_pct"],reverse=(bias=="BUY"))
 return out[:int(cfg["top_gainers_losers_count"])]
def candles(api,t,start,end):
 r=api.getCandleData({"exchange":"NSE","symboltoken":t["token"],"interval":"FIVE_MINUTE","fromdate":start.strftime("%Y-%m-%d %H:%M"),"todate":end.strftime("%Y-%m-%d %H:%M")})
 if not r or r.get("status") is not True: raise RuntimeError("Angel One candle API failed: "+str(r))
 out=[]
 for x in r.get("data") or []:
  ts=datetime.fromisoformat(str(x[0]).replace("Z","+00:00")); ts=ts.replace(tzinfo=IST) if ts.tzinfo is None else ts.astimezone(IST)
  out.append({"time":ts,"open":float(x[1]),"high":float(x[2]),"low":float(x[3]),"close":float(x[4]),"volume":float(x[5])})
 return sorted(out,key=lambda z:z["time"])
def colour(c): return "RED" if c["close"]<c["open"] else "GREEN" if c["close"]>c["open"] else "DOJI"
def append(path,row,fields):
 exists=path.exists()
 with path.open("a",newline="",encoding="utf-8-sig") as f:
  w=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore")
  if not exists:w.writeheader()
  w.writerow(row)
def stop_r(p,r): return p["entry"]+r*p["risk"] if p["side"]=="BUY" else p["entry"]-r*p["risk"]
def manage(p,c,cfg):
 active=p["initial_sl"] if p["locked_r"]==0 else stop_r(p,p["locked_r"])
 if (c["low"]<=active if p["side"]=="BUY" else c["high"]>=active):
  p.update(status="CLOSED",exit_time=c["time"].isoformat(),exit_price=active,exit_reason="INITIAL_SL" if p["locked_r"]==0 else f"LOCK_{p['locked_r']}R"); return
 rr=(c["close"]-p["entry"])/p["risk"] if p["side"]=="BUY" else (p["entry"]-c["close"])/p["risk"]
 for level in (2,3,4,5):
  if rr>=level and p["locked_r"]<level:
   p["locked_r"]=float(level)
   if level==5:p["reached_5r"]=True
 if p["reached_5r"]:
  rev=(p["side"]=="BUY" and colour(c)=="RED") or (p["side"]=="SELL" and colour(c)=="GREEN")
  if cfg["clear_reverse_method"]=="OPPOSITE_COLOUR_CANDLE_CLOSE" and rev:
   p.update(status="CLOSED",exit_time=c["time"].isoformat(),exit_price=stop_r(p,4),exit_reason="CLEAR_REVERSE_4R")
  elif (c["low"]<=stop_r(p,5) if p["side"]=="BUY" else c["high"]>=stop_r(p,5)):
   p.update(status="CLOSED",exit_time=c["time"].isoformat(),exit_price=stop_r(p,5),exit_reason="NORMAL_PULLBACK_5R")
def main():
 logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
 cfg=load_cfg()
 if cfg["mode"]!="PAPER": raise RuntimeError("This scanner is PAPER ONLY.")
 OUT.mkdir(exist_ok=True); current=now()
 if current.weekday()>4 or current.time()<hhmm("09:00") or current.time()>hhmm("15:30"):
  logging.info("Outside NSE market window."); return
 api=login(); tokens=instrument_tokens(cfg)
 target=current.replace(hour=9,minute=25,second=5,microsecond=0)
 while now()<target:
  logging.info("Waiting for 09:25 IST selection snapshot."); time.sleep(5)
 pre=preopen(set(cfg["symbols"]))
 bias,a,d,u,candidates,sector_info=select(pre,cfg)
 logging.info("Pre-open breadth Advance=%d Decline=%d Unchanged=%d Bias=%s",a,d,u,bias)
 day=now().date()
 if bias=="NEUTRAL": logging.info("Neutral market: no selection."); return
 prices=ltp(api,tokens,[x["symbol"] for x in candidates]); watch=rank(candidates,prices,bias,cfg)
 summary={"date":str(day),"advance":a,"decline":d,"unchanged":u,"bias":bias,**sector_info}
 (OUT/f"market_summary_{day}.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")
 fields=["symbol","direction","sector","selection_price","selection_change_pct","preopen_price","change_pct","previous_close"]
 with (OUT/f"watchlist_{day}.csv").open("w",newline="",encoding="utf-8-sig") as f:
  w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
  for x in watch:w.writerow({k:x.get(k) for k in fields})
 logging.info("Selected %d %s stocks: %s",len(watch),bias,", ".join(x["symbol"] for x in watch))
 if not watch:return
 start=now().replace(hour=9,minute=15,second=0,microsecond=0); seen={}; entered=set(); positions={}
 sf=OUT/f"paper_signals_{day}.csv"; tf=OUT/f"paper_trades_{day}.csv"
 sfields=["time","symbol","side","entry","initial_sl","risk","quantity","volume","reason"]
 tfields=["symbol","side","entry_time","entry","initial_sl","risk","quantity","locked_r","status","exit_time","exit_price","exit_reason"]
 while now().time()<hhmm(cfg["square_off"]):
  current=now()
  for x in watch:
   sym=x["symbol"]; token=tokens.get(sym)
   if not token:continue
   try:
    cs=candles(api,token,start,current); complete=[z for z in cs if z["time"]+timedelta(minutes=5)<=current]
    if not complete:continue
    c=complete[-1]
    if seen.get(sym)==c["time"]:continue
    seen[sym]=c["time"]
    if sym in positions and positions[sym]["status"]=="OPEN":
     manage(positions[sym],c,cfg)
     if positions[sym]["status"]=="CLOSED":
      append(tf,positions[sym],tfields); logging.info("%s EXIT %s",sym,positions[sym]["exit_reason"])
     continue
    if sym in entered or current.time()<hhmm(cfg["entry_start"]) or current.time()>hhmm(cfg["entry_cutoff"]) or c["time"].time()<hhmm("09:30"):continue
    # First three candles count in min-volume comparison, but cannot trigger entry.
    if c["volume"]!=min(z["volume"] for z in complete if z["time"]>=start):continue
    if colour(c)!=("RED" if bias=="BUY" else "GREEN"):continue
    entry=c["close"]
    # PROVISIONAL: initial SL at signal candle extreme; user has not separately confirmed this.
    buf=float(cfg["initial_sl_buffer_pct"])/100
    sl=c["low"]*(1-buf) if bias=="BUY" else c["high"]*(1+buf)
    risk=abs(entry-sl); qty=int(float(cfg["paper_capital_per_trade"])//entry)
    if risk<=0 or qty<1:continue
    p={"symbol":sym,"side":bias,"entry_time":c["time"].isoformat(),"entry":entry,"initial_sl":sl,
       "risk":risk,"quantity":qty,"locked_r":0.0,"reached_5r":False,"status":"OPEN",
       "exit_time":"","exit_price":"","exit_reason":""}
    positions[sym]=p; entered.add(sym)
    append(sf,{"time":p["entry_time"],"symbol":sym,"side":bias,"entry":entry,"initial_sl":sl,
      "risk":risk,"quantity":qty,"volume":c["volume"],"reason":"Lowest completed 5m volume so far + opposite-colour candle close"},sfields)
    logging.info("PAPER %s %s ENTRY %.2f SL %.2f qty=%d",bias,sym,entry,sl,qty)
   except Exception as e:logging.warning("%s candle scan error: %s",sym,e)
  time.sleep(max(5,int(cfg["poll_seconds"])))
 for sym,p in positions.items():
  if p["status"]!="OPEN":continue
  try:
   last=candles(api,tokens[sym],start,now())[-1]
   p.update(status="CLOSED",exit_time=now().isoformat(),exit_price=last["close"],exit_reason="DAY_END_SQUARE_OFF")
   append(tf,p,tfields)
  except Exception as e:logging.error("Square-off record failed %s: %s",sym,e)
 logging.info("Finished. PAPER ONLY; no live orders placed.")
if __name__=="__main__":
 try: main()
 except KeyboardInterrupt: sys.exit(130)
 except Exception as e: logging.exception("Scanner failed: %s",e); sys.exit(1)
