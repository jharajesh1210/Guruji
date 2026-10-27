import os, json, csv, time
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
import pandas as pd
import numpy as np
import yfinance as yf

SYMBOL = 'RELIANCE.NS'
TZ = ZoneInfo('Asia/Kolkata')
STATE_FILE = Path('RIL_CANDLESTICK_BIBLE_state.json')
TRADES_FILE = Path('RIL_CANDLESTICK_BIBLE_trades.csv')
RISK_RUPEES = float(os.getenv('RIL_PAPER_RISK_RUPEES', '100'))
FEE_BPS = float(os.getenv('RIL_PAPER_FEE_BPS', '0'))
HEADERS = ['trade_id','strategy','side','signal_time','entry_time','entry','initial_sl','quantity','risk_per_share','tp2','tp3','tp4','status','stage','stop','exit_time','exit_price','exit_reason','gross_pnl','net_pnl']

def notify(msg):
    token = os.getenv('TELEGRAM_BOT_TOKEN', '')
    chat = os.getenv('TELEGRAM_CHAT_ID', '')
    print(msg)
    if token and chat:
        try:
            r = requests.post(f'https://api.telegram.org/bot{token}/sendMessage', json={'chat_id':chat,'text':msg},timeout=15)
            r.raise_for_status()
        except Exception as exc:
            print('Telegram error:', exc)

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    return {'trade':None, 'last_signal_id':None, 'last_bar':None}

def save_state(state):
    temp = STATE_FILE.with_suffix('.tmp')
    temp.write_text(json.dumps(state,indent=2),encoding='utf-8')
    temp.replace(STATE_FILE)

def log_trade(trade):
    exists = TRADES_FILE.exists()
    with TRADES_FILE.open('a',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=HEADERS)
        if not exists: w.writeheader()
        w.writerow({k:trade.get(k,'') for k in HEADERS})

def get_data():
    d = yf.download(SYMBOL, period='45d', interval='5m', auto_adjust=True, progress=False, threads=False)
    if d.empty: raise RuntimeError('Yahoo returned no data')
    if isinstance(d.columns,pd.MultiIndex): d.columns=d.columns.get_level_values(0)
    d=d.rename(columns=str.lower)[['open','high','low','close','volume']].dropna()
    if d.index.tz is None: d.index=d.index.tz_localize('UTC')
    d.index=d.index.tz_convert(TZ)
    d=d[~d.index.duplicated(keep='last')].sort_index()
    return d

def add_indicators(d):
    d=d.copy()
    d['ma8']=d.close.rolling(8).mean()
    d['ma21']=d.close.rolling(21).mean()
    d['sr_high']=d.high.shift(1).rolling(24).max()
    d['sr_low']=d.low.shift(1).rolling(24).min()
    d['range']=d.high-d.low
    d['body']=(d.close-d.open).abs()
    d['upper']=d.high-d[['open','close']].max(axis=1)
    d['lower']=d[['open','close']].min(axis=1)-d.low
    return d

def trend_hourly(d):
    h=d.resample('60min',origin='start_day',offset='15min').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
    # Only completed 60-minute blocks; the last may be incomplete.
    last5=d.index[-1]
    h=h[h.index+pd.Timedelta(minutes=60)<=last5+pd.Timedelta(minutes=5)]
    if len(h)<25: return 'FLAT'
    a=h.close.rolling(8).mean().iloc[-1]
    b=h.close.rolling(21).mean().iloc[-1]
    if a>b and h.close.iloc[-1]>b: return 'UP'
    if a<b and h.close.iloc[-1]<b: return 'DOWN'
    return 'FLAT'

def near(a,b,tolerance):
    return pd.notna(b) and abs(a-b)<=tolerance

def detect(d,trend):
    if len(d)<60 or trend=='FLAT': return None
    c=d.iloc[-1]; p=d.iloc[-2]; q=d.iloc[-3]
    price=float(c.close)
    tol=max(price*0.003,float(d['range'].tail(20).median())*0.8)
    fib_hi=float(d.high.iloc[-49:-1].max()); fib_lo=float(d.low.iloc[-49:-1].min())
    fib50=(fib_hi+fib_lo)/2
    fib618=fib_hi-(fib_hi-fib_lo)*0.618 if trend=='UP' else fib_lo+(fib_hi-fib_lo)*0.618
    level_support=any(near(float(c.low),float(v),tol) for v in [c.sr_low,c.ma21,fib50,fib618])
    level_resistance=any(near(float(c.high),float(v),tol) for v in [c.sr_high,c.ma21,fib50,fib618])
    bullish_pin=(c.lower>=2*max(c.body,0.01) and c.upper<=max(c.body,0.01)*1.2 and c.close>c.low+c['range']*0.6)
    bearish_pin=(c.upper>=2*max(c.body,0.01) and c.lower<=max(c.body,0.01)*1.2 and c.close<c.low+c['range']*0.4)
    bullish_engulf=(p.close<p.open and c.close>c.open and c.open<=p.close and c.close>=p.open)
    bearish_engulf=(p.close>p.open and c.close<c.open and c.open>=p.close and c.close<=p.open)
    inside_prev=(p.high<q.high and p.low>q.low)
    inside_earlier=(q.high<d.iloc[-4].high and q.low>d.iloc[-4].low)
    bullish_inside=inside_prev and c.close>q.high
    bearish_inside=inside_prev and c.close<q.low
    bullish_fake=inside_prev and c.low<q.low and c.close>q.low and c.close>c.open
    bearish_fake=inside_prev and c.high>q.high and c.close<q.high and c.close<c.open
    # In the false breakout pattern, the last candle sweeps the mother candle and closes back inside.
    patterns=[]
    if trend=='UP' and level_support:
        if bullish_fake: patterns.append(('INSIDE_FALSE_BREAKOUT',float(min(c.low,q.low))))
        if bullish_pin: patterns.append(('PIN_BAR',float(c.low)))
        if bullish_engulf: patterns.append(('ENGULFING',float(min(c.low,p.low))))
        if bullish_inside: patterns.append(('INSIDE_BAR',float(min(q.low,p.low,c.low))))
    if trend=='DOWN' and level_resistance:
        if bearish_fake: patterns.append(('INSIDE_FALSE_BREAKOUT',float(max(c.high,q.high))))
        if bearish_pin: patterns.append(('PIN_BAR',float(c.high)))
        if bearish_engulf: patterns.append(('ENGULFING',float(max(c.high,p.high))))
        if bearish_inside: patterns.append(('INSIDE_BAR',float(max(q.high,p.high,c.high))))
    if not patterns: return None
    strategy,sl=patterns[0]
    side='BUY' if trend=='UP' else 'SELL'
    risk=(price-sl) if side=='BUY' else (sl-price)
    if risk<=0 or risk/price>0.02 or risk/price<0.0005: return None
    # A potential 2R path to recent opposing swing level is required.
    opposing=float(d.high.iloc[-49:-1].max()) if side=='BUY' else float(d.low.iloc[-49:-1].min())
    if (opposing-price if side=='BUY' else price-opposing)<2*risk: return None
    return {'strategy':strategy,'side':side,'entry':price,'initial_sl':sl,'risk_per_share':risk}

def new_trade(sig,bar):
    side=sig['side']; sign=1 if side=='BUY' else -1
    entry=sig['entry']; risk=sig['risk_per_share']
    qty=max(1,int(RISK_RUPEES/risk)) if risk<=RISK_RUPEES else 0
    if qty==0: return None
    return {'trade_id':f'{bar.isoformat()}_{side}_{sig["strategy"]}', 'strategy':sig['strategy'],'side':side,'signal_time':bar.isoformat(),'entry_time':bar.isoformat(), 'entry':round(entry,4),'initial_sl':round(sig['initial_sl'],4),'quantity':qty,'risk_per_share':round(risk,4),'tp2':round(entry+sign*2*risk,4),'tp3':round(entry+sign*3*risk,4),'tp4':round(entry+sign*4*risk,4),'status':'OPEN','stage':0,'stop':round(sig['initial_sl'],4)}

def manage(trade,bar,c):
    side=trade['side']; sign=1 if side=='BUY' else -1
    high=float(c.high); low=float(c.low)
    def hit_stop(): return low<=trade['stop'] if side=='BUY' else high>=trade['stop']
    def hit_target(v): return high>=v if side=='BUY' else low<=v
    # Conservative intrabar ordering: stop first, then targets. Never infer tick path from OHLC.
    if hit_stop():
        return close_trade(trade,bar,trade['stop'],'STOP_LOSS')
    if hit_target(trade['tp4']):
        return close_trade(trade,bar,trade['tp4'],'TARGET_4R')
    if hit_target(trade['tp3']) and trade['stage']<3:
        trade['stage']=3; trade['stop']=trade['tp3']; notify(f'RIL PAPER {side}: 3R reached, SL locked at 3R: {trade["stop"]}')
    elif hit_target(trade['tp2']) and trade['stage']<2:
        trade['stage']=2; trade['stop']=trade['tp2']; notify(f'RIL PAPER {side}: 2R reached, SL locked at 2R: {trade["stop"]}')
    return trade

def close_trade(trade,bar,price,reason):
    sign=1 if trade['side']=='BUY' else -1
    gross=sign*(price-trade['entry'])*trade['quantity']
    fee=(abs(price)+abs(trade['entry']))*trade['quantity']*FEE_BPS/10000
    trade.update(status='CLOSED',exit_time=bar.isoformat(),exit_price=round(price,4),exit_reason=reason,gross_pnl=round(gross,2),net_pnl=round(gross-fee,2))
    log_trade(trade)
    notify(f'RIL BIBLE PAPER EXIT {trade["side"]} | {reason}\nExit: {price:.2f} | Qty: {trade["quantity"]}\nNet P&L: Rs {trade["net_pnl"]:.2f}')
    return None

def main():
    now=datetime.now(TZ)
    d=get_data()
    # Use only candles whose 5-minute interval has ended. Yahoo labels bars by opening time.
    cutoff=pd.Timestamp(now).floor('5min')
    d=d[d.index+pd.Timedelta(minutes=5)<=cutoff]
    d=d[d.index.dayofweek<5]
    if d.empty: raise RuntimeError('No completed 5-minute candles')
    d=add_indicators(d)
    bar=d.index[-1]
    state=load_state()
    # Avoid stale signal entries; data freshness <= 20 min during market session.
    market_open=(now.weekday()<5 and now.replace(hour=9,minute=20,second=0,microsecond=0)<=now<=now.replace(hour=15,minute=35,second=0,microsecond=0))
    fresh=(pd.Timestamp(now)-bar)<=pd.Timedelta(minutes=20)
    last_processed=state.get('last_bar')
    if last_processed==bar.isoformat():
        print('Already processed:',bar)
        return
    # Do not retroactively enter on old signals. Manage existing positions with all new bars.
    if state.get('trade'):
        trade=state['trade']
        start=pd.Timestamp(last_processed) if last_processed else pd.Timestamp(trade['entry_time'])
        for idx,c in d[d.index>start].iterrows():
            trade=manage(trade,idx,c)
            if trade is None: break
        state['trade']=trade
    if market_open and fresh and state.get('trade') is None:
        trend=trend_hourly(d)
        sig=detect(d,trend)
        if sig:
            candidate=new_trade(sig,bar)
            if candidate and candidate['trade_id']!=state.get('last_signal_id'):
                state['trade']=candidate
                state['last_signal_id']=candidate['trade_id']
                notify(f'RIL CANDLESTICK BIBLE PAPER ENTRY\n{candidate["side"]} {candidate["strategy"]}\nTime: {bar.strftime("%d-%m-%Y %H:%M IST")}\nEntry: {candidate["entry"]} | SL: {candidate["stop"]}\n2R: {candidate["tp2"]} | 3R: {candidate["tp3"]} | 4R: {candidate["tp4"]}\nQty: {candidate["quantity"]} | No real order')
    state['last_bar']=bar.isoformat()
    save_state(state)
    print('Last bar:',bar,'Position:',state.get('trade',{}).get('status') if state.get('trade') else 'NONE')

if __name__=='__main__':
    main()
