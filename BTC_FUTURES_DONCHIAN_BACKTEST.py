"""CoinDCX BTC perpetual 5m Donchian/EMA/ATR research backtest. No orders."""
import argparse, time
from pathlib import Path
import numpy as np
import pandas as pd
import requests

URL = "https://public.coindcx.com/market_data/candlesticks"

def get_data(days):
    end = int(time.time()) // 300 * 300
    start = end - days * 86400
    rows, session = [], requests.Session()
    for a in range(start, end, 86400):
        b = min(a + 86400, end)
        for attempt in range(4):
            try:
                r = session.get(URL, params={"pair":"B-BTC_USDT","from":a,"to":b,"resolution":"5","pcode":"f"}, timeout=25)
                r.raise_for_status()
                payload = r.json()
                if isinstance(payload, dict):
                    payload = payload.get("data", payload.get("candles", []))
                if not isinstance(payload, list):
                    raise ValueError(f"Unexpected candle response: {str(payload)[:200]}")
                rows.extend(payload)
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
        time.sleep(.12)
    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("No CoinDCX candles received")
    if "time" not in df.columns:
        raise RuntimeError(f"Missing time column; returned columns: {list(df.columns)}")
    df["time"] = pd.to_numeric(df["time"], errors="coerce")
    if df["time"].median() > 1e11:
        df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    else:
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    for col in ("open","high","low","close","volume"):
        if col not in df.columns:
            df[col] = 0 if col == "volume" else np.nan
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["time","open","high","low","close"])
    df = df.drop_duplicates("time").sort_values("time").set_index("time")
    df = df[df.index < pd.Timestamp.now(tz="UTC").floor("5min")]
    if len(df) < 300:
        raise RuntimeError(f"Insufficient candles returned: {len(df)}")
    return df

def indicators(df, lookback):
    d = df.copy()
    d["ema200"] = d.close.ewm(span=200, adjust=False, min_periods=200).mean()
    d["upper"] = d.high.shift(1).rolling(lookback).max()
    d["lower"] = d.low.shift(1).rolling(lookback).min()
    prev = d.close.shift(1)
    tr = pd.concat([(d.high-d.low),(d.high-prev).abs(),(d.low-prev).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    return d

def simulate(d, capital, notional, fee, slip, atrmult):
    cash, pos, trades, equity = capital, None, [], []
    for i in range(201, len(d)-1):
        bar, nxt = d.iloc[i], d.iloc[i+1]
        if not np.isfinite([bar.ema200,bar.upper,bar.lower,bar.atr]).all() or bar.atr <= 0:
            continue
        if pos is not None:
            side, entry, stop, qty, entry_time = pos
            hit = bar.low <= stop if side == 1 else bar.high >= stop
            if hit:
                raw = min(bar.open, stop) if side == 1 else max(bar.open, stop)
                exit_price = raw * (1-slip*side)
                pnl = (exit_price-entry)*qty*side - (entry+exit_price)*qty*fee
                cash += pnl
                trades.append({"entry_time":entry_time,"exit_time":d.index[i],"side":"LONG" if side==1 else "SHORT","entry":entry,"exit":exit_price,"net_pnl_usdt":pnl,"reason":"ATR_STOP"})
                pos = None
        if pos is None and cash > 0:
            side = 1 if bar.close > bar.upper and bar.close > bar.ema200 else (-1 if bar.close < bar.lower and bar.close < bar.ema200 else 0)
            if side:
                entry = nxt.open * (1+side*slip)
                qty = min(notional, cash) / entry
                stop = entry - side * atrmult * bar.atr
                pos = (side,entry,stop,qty,d.index[i+1])
        marked = cash
        if pos:
            side,entry,stop,qty,_ = pos
            marked += (bar.close-entry)*qty*side - (entry+bar.close)*qty*fee
        equity.append({"time":d.index[i],"equity_usdt":marked})
    if pos:
        side,entry,stop,qty,entry_time = pos
        px = d.iloc[-1].close*(1-side*slip)
        pnl = (px-entry)*qty*side - (px+entry)*qty*fee
        cash += pnl
        trades.append({"entry_time":entry_time,"exit_time":d.index[-1],"side":"LONG" if side==1 else "SHORT","entry":entry,"exit":px,"net_pnl_usdt":pnl,"reason":"END_OF_TEST"})
    t, e = pd.DataFrame(trades), pd.DataFrame(equity)
    dd = ((e.equity_usdt/e.equity_usdt.cummax())-1).min()*100 if not e.empty else 0
    profits = t.net_pnl_usdt if not t.empty else pd.Series(dtype=float)
    wins, losses = profits[profits>0].sum(), -profits[profits<0].sum()
    summary = {"trades":len(t),"win_rate_pct":round((profits>0).mean()*100,2) if len(t) else 0,
        "net_profit_usdt":round(cash-capital,2),"return_pct":round((cash/capital-1)*100,2),
        "max_drawdown_pct":round(float(dd),2),"profit_factor":round(float(wins/losses),3) if losses else ("inf" if wins else "N/A")}
    return t,e,summary

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--days",type=int,default=365)
    p.add_argument("--capital",type=float,default=1000)
    p.add_argument("--notional",type=float,default=100)
    p.add_argument("--fee",type=float,default=.0005,help="fee per side")
    p.add_argument("--slippage",type=float,default=.0002,help="slippage per side")
    p.add_argument("--out",default="btc_donchian_results")
    a=p.parse_args()
    if a.days < 3 or a.capital <= 0 or a.notional <= 0 or a.fee < 0 or a.slippage < 0:
        p.error("Invalid days/capital/notional/fee/slippage")
    out=Path(a.out); out.mkdir(parents=True,exist_ok=True)
    df=get_data(a.days); df.to_csv(out/"BTC_5M_candles.csv")
    results=[]
    for lookback in (20,55):
        d=indicators(df,lookback)
        for mult in (2.,3.):
            trades,eq,s=simulate(d,a.capital,a.notional,a.fee,a.slippage,mult)
            tag=f"DONCHIAN_{lookback}_ATR_{int(mult)}"
            trades.to_csv(out/f"{tag}_trades.csv",index=False)
            eq.to_csv(out/f"{tag}_equity.csv",index=False)
            results.append({"variant":tag,"candles":len(df),"first_candle":str(df.index.min()),"last_candle":str(df.index.max()),**s})
    comparison=pd.DataFrame(results)
    comparison.to_csv(out/"comparison.csv",index=False)
    print(comparison.to_string(index=False))
    print("Files saved:",out.resolve())
    print("NOTE: Funding, liquidation, spread dynamics and exchange-specific fee tiers are NOT modeled.")

if __name__ == "__main__":
    main()
