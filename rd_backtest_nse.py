"""
Same PDH/PDL breakout strategy on Indian share market (NSE cash, INTRADAY / MIS), NIFTY 50 stocks.

Data : Yahoo Finance 15m candles, last ~60 days (Yahoo's limit for 15m).
Rules: like mother - high & close beyond previous day's high/low, volume > 1.5 x 20-bar average,
       wick < 40%, SL = breakout candle low/high (0.3%-2%), lock 1:2/1:3/1:4, 1:5 exit,
       loss exit after MACD(12,26,9) confirm (or hard SL variant).
Intraday changes (cash market): market 09:15-15:30 IST, entries only on candles closing by 15:00,
       every open trade squared off at the 15:15 candle close (no overnight), one trade per stock.
Fill at the next candle open, SL checked before profit inside a candle (conservative).
Costs: 0.10% round trip (brokerage 0.03%/side + STT + exchange + stamp + GST, typical discount broker);
       also shown at 0.05%. Size: Rs 1,00,000 per trade (MIS 5x = Rs 20,000 margin).
"""
import itertools
import os
import time
from datetime import time as dtime

import numpy as np
import pandas as pd
import requests

OUT = os.getenv("BT_OUT", "backtest_out")
SIZE = 100000.0
MIN_RISK, MAX_RISK = 0.003, 0.02
NIFTY50 = ("ADANIENT ADANIPORTS APOLLOHOSP ASIANPAINT AXISBANK BAJAJ-AUTO BAJFINANCE BAJAJFINSV BEL BHARTIARTL "
           "CIPLA COALINDIA DRREDDY EICHERMOT ETERNAL GRASIM HCLTECH HDFCBANK HDFCLIFE HEROMOTOCO HINDALCO "
           "HINDUNILVR ICICIBANK INDUSINDBK INFY ITC JIOFIN JSWSTEEL KOTAKBANK LT M&M MARUTI NESTLEIND NTPC "
           "ONGC POWERGRID RELIANCE SBILIFE SBIN SHRIRAMFIN SUNPHARMA TATACONSUM TATAMOTORS TATASTEEL TCS "
           "TECHM TITAN TRENT ULTRACEMCO WIPRO").split()


def fetch(sym):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}.NS"
    for attempt in range(3):
        try:
            r = requests.get(url, params={"interval": "15m", "range": "60d"},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            df = pd.DataFrame({"Timestamp": pd.to_datetime(res["timestamp"], unit="s", utc=True)
                               .tz_convert("Asia/Kolkata").tz_localize(None),
                               "Open": q["open"], "High": q["high"], "Low": q["low"], "Close": q["close"],
                               "Volume": q["volume"]}).dropna()
            df = df[(df["Timestamp"].dt.time >= dtime(9, 15)) & (df["Timestamp"].dt.time < dtime(15, 30))]
            return df.reset_index(drop=True)
        except Exception:
            time.sleep(2 + 2 * attempt)
    return pd.DataFrame()


def prep(df):
    df = df.copy()
    df["Date"] = df["Timestamp"].dt.date
    df["VolMA"] = df["Volume"].shift(1).rolling(20, min_periods=10).mean()
    e1, e2 = df["Close"].ewm(span=12, adjust=False).mean(), df["Close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = e1 - e2
    df["Sig"] = df["MACD"].ewm(span=9, adjust=False).mean()
    daily = df.groupby("Date").agg(DH=("High", "max"), DL=("Low", "min"))
    days = list(daily.index)
    prev = {days[k]: (daily.iloc[k - 1]["DH"], daily.iloc[k - 1]["DL"]) for k in range(1, len(days))}
    df["PDH"] = df["Date"].map(lambda d: prev.get(d, (np.nan, np.nan))[0])
    df["PDL"] = df["Date"].map(lambda d: prev.get(d, (np.nan, np.nan))[1])
    return df


def simulate(df, loss_mode, target, fee):
    T = {"ladder5": 5.0, "t3": 3.0, "t2": 2.0}[target]
    locks = [l for l in (2.0, 3.0, 4.0) if l < T]
    O, H, L, C = (df[c].values for c in ("Open", "High", "Low", "Close"))
    M, S = df["MACD"].values, df["Sig"].values
    TS = df["Timestamp"]
    trades, i, n, block = [], 1, len(df), {}
    while i < n - 1:
        r = df.iloc[i]
        t = r["Timestamp"].time()
        if r["PDH"] != r["PDH"] or r["VolMA"] != r["VolMA"] or t > dtime(14, 45):
            i += 1
            continue
        if df["Date"].iloc[i + 1] != r["Date"]:
            i += 1
            continue
        rng = r["High"] - r["Low"]
        up_w = (r["High"] - max(r["Open"], r["Close"])) / rng if rng > 0 else 0
        dn_w = (min(r["Open"], r["Close"]) - r["Low"]) / rng if rng > 0 else 0
        hv = r["Volume"] > 1.5 * r["VolMA"]
        d = 1 if (r["High"] > r["PDH"] and r["Close"] > r["PDH"] and hv and up_w < 0.4) else (
            -1 if (r["Low"] < r["PDL"] and r["Close"] < r["PDL"] and hv and dn_w < 0.4) else None)
        if d is None or block.get(d) == r["Date"]:
            i += 1
            continue
        E = O[i + 1]
        sl = r["Low"] if d == 1 else r["High"]
        risk = d * (E - sl)
        if risk <= 0 or risk < E * MIN_RISK:
            i += 1
            continue
        if risk > E * MAX_RISK:
            risk = E * MAX_RISK
            sl = E - d * risk
        locked = pend = 0.0
        exit_px = why = None
        j = i + 1
        while j < n:
            if df["Date"].iloc[j] != r["Date"]:          # should not happen (EOD exit below)
                exit_px, why = C[j - 1], "eod"
                j -= 1
                break
            if pend > locked:
                locked = pend
            stop = E + d * locked * risk if locked > 0 else sl
            hit = (L[j] <= stop) if d == 1 else (H[j] >= stop)
            if hit:
                gap = (O[j] < stop) if d == 1 else (O[j] > stop)
                if locked > 0:
                    exit_px, why = (O[j] if gap else stop), "lock"
                    break
                if loss_mode == "hard":
                    exit_px, why = (O[j] if gap else stop), "hard_sl"
                    break
                if (M[j] < S[j]) if d == 1 else (M[j] > S[j]):
                    exit_px, why = (min(stop, C[j]) if d == 1 else max(stop, C[j])), "macd_loss"
                    break
            best = d * ((H[j] if d == 1 else L[j]) - E) / risk
            if best >= T:
                exit_px, why = E + d * T * risk, "target"
                break
            new = max([l for l in locks if best >= l], default=0.0)
            if new > max(locked, pend):
                lp = E + d * new * risk
                if (C[j] <= lp) if d == 1 else (C[j] >= lp):
                    exit_px, why = lp, "lock"
                    break
                pend = new
            if TS.iloc[j].time() >= dtime(15, 15):        # intraday square-off
                exit_px, why = C[j], "eod_squareoff"
                break
            j += 1
        if exit_px is None:
            break
        gross = d * (exit_px - E)
        rs = SIZE * gross / E - SIZE * fee
        trades.append({"date": r["Date"], "R": gross / risk - fee * E / risk, "rs": rs, "exit": why})
        if rs > 0:
            block[d] = r["Date"]
        i = j + 1
    return trades


def summ(tr):
    if not tr:
        return dict(n=0, win=0, R=0, rs=0, pf=0, dd=0, h1=0, h2=0)
    x = pd.DataFrame(tr).sort_values("date")
    w, l = x.rs[x.rs > 0], x.rs[x.rs < 0]
    cum = x.rs.cumsum()
    h = len(x) // 2
    return dict(n=len(x), win=round(100 * len(w) / len(x), 1), R=round(x.R.sum(), 1), rs=round(x.rs.sum()),
                pf=round(w.sum() / -l.sum(), 2) if len(l) else 99, dd=round((cum - cum.cummax()).min()),
                h1=round(x.rs.iloc[:h].sum()), h2=round(x.rs.iloc[h:].sum()))


def main():
    os.makedirs(OUT, exist_ok=True)
    data = {}
    for s in NIFTY50:
        df = fetch(s)
        if len(df) > 200:
            data[s] = prep(df)
        time.sleep(0.5)
    print(f"stocks with data: {len(data)}", flush=True)
    if not data:
        raise SystemExit("no data")
    span = f"{min(d['Timestamp'].iloc[0] for d in data.values())} -> {max(d['Timestamp'].iloc[-1] for d in data.values())}"
    lines = [f"NSE cash intraday (NIFTY 50, {len(data)} stocks), 15m, {span}. Rs 1,00,000 per trade, "
             "square-off 15:15, costs as noted.", "",
             "| Loss exit | Target | Costs | Trades | Win % | Net R | Net Rs | PF | Max DD Rs | 1st half Rs | 2nd half Rs |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    exits = {}
    for lm, tg, fee in itertools.product(["macd", "hard"], ["ladder5", "t3", "t2"], [0.0010, 0.0005]):
        allt = []
        for s, df in data.items():
            allt += simulate(df, lm, tg, fee)
        sm = summ(allt)
        tag = "MOTHER-like" if (lm, tg) == ("macd", "ladder5") else ""
        lines.append(f"| {lm} {tag} | {tg} | {fee*100:.2f}% | {sm['n']} | {sm['win']} | {sm['R']} | {sm['rs']} | "
                     f"{sm['pf']} | {sm['dd']} | {sm['h1']} | {sm['h2']} |")
        if (lm, tg, fee) == ("macd", "ladder5", 0.0010):
            exits = pd.DataFrame(allt).groupby("exit")["rs"].agg(["count", "sum"]).round(0)
    lines += ["", "Exit types (mother-like, 0.10% costs):", "", "| Exit | Trades | Net Rs |", "|---|---|---|"]
    for k, r in exits.iterrows():
        lines.append(f"| {k} | {int(r['count'])} | {int(r['sum'])} |")
    text = "\n".join(lines) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print("\n=== RESULT ===\n" + text, flush=True)


if __name__ == "__main__":
    main()
