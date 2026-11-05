"""NIFTY 50 GOLD strategy adapted from GOLD_FINAL_PAPER_V2.py.
Angel One SmartAPI data only. PAPER TRADING ONLY: no order placement methods are used.
"""
import os, json, csv, time, sys, datetime as dt
from zoneinfo import ZoneInfo
import requests, pandas as pd, numpy as np, pyotp
from SmartApi import SmartConnect

IST=ZoneInfo('Asia/Kolkata'); UTC=dt.timezone.utc
STATE='NIFTY_50_GOLD_STATE.json'; HISTORY='NIFTY_50_GOLD_TRADES.csv'
CAPITAL=float(os.getenv('PAPER_CAPITAL','100000'))
RISK_PCT=float(os.getenv('RISK_PCT','0.01'))
MAX_POSITIONS=int(os.getenv('MAX_POSITIONS','3'))
MAX_DAILY_TRADES=int(os.getenv('MAX_DAILY_TRADES','5'))
MAX_DAILY_LOSS=float(os.getenv('MAX_DAILY_LOSS','0.02'))
FEE=float(os.getenv('ESTIMATED_FEE_RATE','0.0003'))
SLIPPAGE=float(os.getenv('SLIPPAGE_RATE','0.0002'))
TG_TOKEN=os.getenv('TG_TOKEN') or os.getenv('TELEGRAM_BOT_TOKEN') or os.getenv('BOT_TOKEN')
TG_CHAT=os.getenv('TG_CHAT_ID') or os.getenv('TELEGRAM_CHAT_ID') or os.getenv('CHAT_ID')
SYMBOLS='ADANIENT ADANIPORTS APOLLOHOSP ASIANPAINT AXISBANK BAJAJ-AUTO BAJFINANCE BAJAJFINSV BEL BHARTIARTL CIPLA COALINDIA DRREDDY EICHERMOT ETERNAL GRASIM HCLTECH HDFCBANK HDFCLIFE HINDALCO HINDUNILVR ICICIBANK INDUSINDBK INFY ITC JIOFIN JSWSTEEL KOTAKBANK LT M&M MARUTI NESTLEIND NTPC ONGC POWERGRID RELIANCE SBIN SHRIRAMFIN SUNPHARMA TATACONSUM TATASTEEL TCS TECHM TITAN TRENT ULTRACEMCO WIPRO ZYDUSLIFE'.split()

def notify(msg, required=False):
    print(msg,flush=True)
    if not TG_TOKEN or not TG_CHAT:
        if required: raise RuntimeError('Telegram secrets missing: TG_TOKEN/TG_CHAT_ID (or aliases)')
        return False
    r=requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',data={'chat_id':TG_CHAT,'text':msg},timeout=20)
    data=r.json(); r.raise_for_status()
    if not data.get('ok'): raise RuntimeError('Telegram API rejected message: '+str(data))
    return True

def load_state():
    if os.path.exists(STATE):
        with open(STATE) as f: s=json.load(f)
    else: s={'equity':CAPITAL,'peak':CAPITAL,'positions':{},'date':'','trades_today':0,'day_start':CAPITAL}
    today=dt.datetime.now(IST).date().isoformat()
    if s.get('date')!=today: s.update(date=today,trades_today=0,day_start=s['equity'])
    return s

def save_state(s):
    with open(STATE,'w') as f: json.dump(s,f,indent=2)

def log_trade(row):
    exists=os.path.exists(HISTORY)
    with open(HISTORY,'a',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['time','symbol','setup','side','entry','exit','qty','reason','pnl','r'])
        if not exists:w.writeheader()
        w.writerow(row)

def indicators(d):
    d=d.copy(); d['ema20']=d.close.ewm(span=20,adjust=False).mean(); d['ema50']=d.close.ewm(span=50,adjust=False).mean(); d['ema200']=d.close.ewm(span=200,adjust=False).mean()
    pc=d.close.shift(); tr=pd.concat([d.high-d.low,(d.high-pc).abs(),(d.low-pc).abs()],axis=1).max(axis=1)
    d['atr']=tr.ewm(alpha=1/14,adjust=False).mean(); up=d.high.diff(); dn=-d.low.diff(); plus=up.where((up>dn)&(up>0),0).ewm(alpha=1/14,adjust=False).mean(); minus=dn.where((dn>up)&(dn>0),0).ewm(alpha=1/14,adjust=False).mean(); d['adx']=(100*(plus-minus).abs()/(plus+minus).replace(0,np.nan)).ewm(alpha=1/14,adjust=False).mean()
    tp=(d.high+d.low+d.close)/3; day=d.ts.dt.date; d['vwap']=(tp*d.volume).groupby(day).cumsum()/d.volume.groupby(day).cumsum().replace(0,np.nan)
    mid=d.close.rolling(20).mean(); sd=d.close.rolling(20).std(ddof=0); d['bb_up']=mid+2*sd; d['bb_dn']=mid-2*sd
    # Keltner Channel: EMA20 +/- 1.5 ATR, used to identify a genuine volatility squeeze.
    d['kc_mid']=d.close.ewm(span=20,adjust=False).mean()
    d['kc_up']=d.kc_mid+1.5*d.atr; d['kc_dn']=d.kc_mid-1.5*d.atr
    return d

def smartapi_login():
    api=os.getenv('ANGEL_API_KEY'); client=os.getenv('ANGEL_CLIENT_CODE'); pin=os.getenv('ANGEL_PIN'); secret=os.getenv('ANGEL_TOTP_SECRET')
    if not all([api,client,pin,secret]): raise RuntimeError('Missing Angel One secrets: ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PIN, ANGEL_TOTP_SECRET')
    obj=SmartConnect(api_key=api); session=obj.generateSession(client,pin,pyotp.TOTP(secret).now())
    if not session or not session.get('status'): raise RuntimeError('Angel One login failed: '+str(session))
    return obj

def instrument_map():
    r=requests.get('https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json',timeout=45); r.raise_for_status(); rows=r.json(); out={}
    for x in rows:
        if x.get('exch_seg')=='NSE' and x.get('symbol','').endswith('-EQ'):
            sym=x['symbol'][:-3]
            if sym in SYMBOLS: out[sym]=str(x['token'])
    missing=sorted(set(SYMBOLS)-set(out)); print('UNRESOLVED SYMBOLS:',missing)
    return out

def get_candles(api, token, interval, days):
    now=dt.datetime.now(IST); frm=now-dt.timedelta(days=days)
    params={'exchange':'NSE','symboltoken':token,'interval':interval,'fromdate':frm.strftime('%Y-%m-%d %H:%M'),'todate':now.strftime('%Y-%m-%d %H:%M')}
    res=api.getCandleData(params)
    if not res or not res.get('status') or not res.get('data'): raise RuntimeError('Candle fetch failed '+str(res)[:200])
    d=pd.DataFrame(res['data'],columns=['ts','open','high','low','close','volume'])
    d['ts']=pd.to_datetime(d.ts).dt.tz_localize(None).dt.tz_localize(IST).dt.tz_convert(UTC)
    for c in ['open','high','low','close','volume']: d[c]=pd.to_numeric(d[c])
    mins={'FIVE_MINUTE':5,'FIFTEEN_MINUTE':15,'ONE_HOUR':60}[interval]
    d=d.sort_values('ts').drop_duplicates('ts'); d=d[d.ts+pd.Timedelta(minutes=mins)<=pd.Timestamp.now(tz='UTC')].reset_index(drop=True)
    if len(d)<205: raise RuntimeError('Insufficient closed candles '+interval+': '+str(len(d)))
    return indicators(d)

def signal(d5,d15,d60):
    # x is the breakout candle; c is the next fully closed confirmation candle.
    x=d5.iloc[-2]; c=d5.iloc[-1]
    if len(d5)<310:return None
    body=abs(float(x.close-x.open))/max(float(x.high-x.low),1e-9)
    volbase=float(d5.volume.iloc[-22:-2].mean())
    if body<0.60 or x.volume<1.5*volbase:return None

    # Setup A: real 288-bar rolling range breakout (exclude the breakout candle itself).
    prior_high=float(d5.high.iloc[-290:-2].max())
    prior_low=float(d5.low.iloc[-290:-2].min())
    a_dir=1 if x.close>prior_high and c.high>x.high else -1 if x.close<prior_low and c.low<x.low else 0

    # Setup B: BB inside Keltner squeeze on the preceding candle, followed by BB breakout.
    squeeze_prev=bool(d5.bb_up.iloc[-3]<d5.kc_up.iloc[-3] and d5.bb_dn.iloc[-3]>d5.kc_dn.iloc[-3])
    b_dir=1 if squeeze_prev and x.close>x.bb_up and c.high>x.high else -1 if squeeze_prev and x.close<x.bb_dn and c.low<x.low else 0
    if a_dir and b_dir and a_dir!=b_dir:return None
    direction=a_dir or b_dir
    if not direction:return None
    setup='A_288_BAR_RANGE_BREAKOUT' if a_dir else 'B_BB_KELTNER_SQUEEZE_BREAKOUT'

    # Multi-timeframe trend, VWAP and momentum confirmation.
    if direction*(x.close-x.ema200)<=0 or direction*(d15.close.iloc[-1]-d15.ema200.iloc[-1])<=0:return None
    if direction*(d15.close.iloc[-1]-d15.open.iloc[-1])<=0:return None
    if direction*(d60.ema50.iloc[-1]-d60.ema200.iloc[-1])<=0:return None
    if direction*(x.close-x.vwap)<=0 or x.adx<=20:return None

    atr=float(x.atr); entry=float(x.high if direction>0 else x.low)*(1+direction*SLIPPAGE)
    candle_stop=float(x.low if direction>0 else x.high); atr_stop=entry-direction*1.5*atr
    stop=max(candle_stop,atr_stop) if direction>0 else min(candle_stop,atr_stop)
    risk=abs(entry-stop)
    if risk<=0 or risk>1.5*atr:return None
    cost_r=(2*FEE+2*SLIPPAGE)*entry/risk
    if cost_r>1:return None
    return {'side':'BUY' if direction>0 else 'SELL','direction':direction,'setup':setup,
            'entry':entry,'stop':stop,'risk':risk,'cost_r':round(cost_r,3),'bar':str(c.ts)}
def manage_position(s,sym,d,force_exit=False):
    p=s['positions'].get(sym)
    if not p:return
    bar=d.iloc[-1]; direction=p['direction']; risk=float(p['risk'])
    hi=float(bar.high); lo=float(bar.low); close=float(bar.close)
    old_stop=float(p['stop'])
    # Conservative OHLC handling: if stop and a profit milestone are both touched
    # in one candle, assume the stop was hit first (no optimistic intrabar ordering).
    stop_hit=(lo<=old_stop) if direction>0 else (hi>=old_stop)
    if stop_hit:
        reason='STOP_LOCK' if p.get('level',0)>0 else 'INITIAL_STOP'
        exit_price=old_stop
    elif force_exit:
        reason='EOD_SQUARE_OFF'; exit_price=close
    else:
        favorable=(hi-p['entry'])/risk if direction>0 else (p['entry']-lo)/risk
        if favorable>=5:
            reason='5R_FINAL'; exit_price=p['entry']+direction*5*risk
        else:
            reached=min(4,max(0,int(np.floor(favorable+1e-9))))
            if reached>p.get('level',0):
                p['level']=reached
                p['stop']=p['entry']+direction*max(0,reached-1)*risk
            p['bars']=p.get('bars',0)+1
            if p['bars']>=30 and p.get('level',0)==0:
                reason='STALL_EXIT'; exit_price=close
            else:
                return
    exit_price*=1-direction*SLIPPAGE
    pnl=direction*(exit_price-p['entry'])*p['qty']-(exit_price+p['entry'])*p['qty']*FEE
    r=pnl/(risk*p['qty']) if risk*p['qty'] else 0
    s['equity']+=pnl
    log_trade({'time':dt.datetime.now(IST).isoformat(),'symbol':sym,'setup':p['setup'],'side':p['side'],
               'entry':p['entry'],'exit':exit_price,'qty':p['qty'],'reason':reason,'pnl':round(pnl,2),'r':round(r,3)})
    notify(f'NIFTY 50 GOLD PAPER EXIT | {sym} | {p["side"]} | {reason} | P&L ₹{pnl:.2f} | Equity ₹{s["equity"]:.2f}')
    del s['positions'][sym]
def run():
    if os.getenv('GITHUB_EVENT_NAME')=='workflow_dispatch' and os.getenv('TELEGRAM_TEST','1')=='1':
        notify('NIFTY 50 GOLD | Angel One PAPER scanner Telegram test successful | No live orders.',required=True)
    now=dt.datetime.now(IST)
    if now.weekday()>4 or now.time()<dt.time(9,20) or now.time()>dt.time(15,30):
        print('Outside NSE trading window; state unchanged.'); return
    s=load_state()
    risk_guard=s['trades_today']>=MAX_DAILY_TRADES or s['equity']<=s['day_start']*(1-MAX_DAILY_LOSS)
    if risk_guard: print('Risk guard active: new entries disabled; managing existing paper positions only')
    api=smartapi_login(); tokens=instrument_map(); count=0
    force_exit=now.time()>=dt.time(15,15)
    for sym,token in tokens.items():
        try:
            d5=get_candles(api,token,'FIVE_MINUTE',5)
            if sym in s['positions']:
                manage_position(s,sym,d5,force_exit=force_exit)
                continue
            # No fresh entries in the final 15 minutes; all paper shorts are intraday only.
            if risk_guard or force_exit or len(s['positions'])>=MAX_POSITIONS or s['trades_today']>=MAX_DAILY_TRADES:
                continue
            d15=get_candles(api,token,'FIFTEEN_MINUTE',15); d60=get_candles(api,token,'ONE_HOUR',45)
            sig=signal(d5,d15,d60)
            if not sig or sig['bar']==s.get('last_entry_bar',{}).get(sym):continue
            qty=min((s['equity']*RISK_PCT)/sig['risk'],(s['equity']*2)/sig['entry'])
            if qty<=0:continue
            p={**sig,'qty':qty,'bars':0,'level':0}
            s['positions'][sym]=p; s['trades_today']+=1
            s.setdefault('last_entry_bar',{})[sym]=sig['bar']; count+=1
            notify(f'NIFTY 50 GOLD PAPER ENTRY | {sym} | {sig["side"]} | {sig["setup"]} | Entry ₹{sig["entry"]:.2f} | SL ₹{sig["stop"]:.2f} | Qty {qty:.2f} | PAPER INTRADAY ONLY')
        except Exception as e: print('SCAN ERROR',sym,repr(e))
        time.sleep(.15)
    save_state(s)
    print('SCAN COMPLETE | entries',count,'| open',len(s['positions']),'| equity',round(s['equity'],2))

