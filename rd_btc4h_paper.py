"""
rd_btc4h_paper.py - PAPER trading (no real orders) of the best BTC logic from the backtest
(rd_backtest_btc2.md, 4h + ATR SL + MACD loss exit + 1:5 ladder: +6.85 USDT / 6 months at 20 USDT, PF 1.88).

Rules (4h candles, bars start 05:30 / 09:30 / 13:30 / 17:30 / 21:30 / 01:30 IST):
  ENTRY  BUY : 4h high > PDH and 4h close > PDH, volume > 1.5 x avg of previous 20 4h bars,
               upper wick < 40% of the candle.     SELL: mirror with PDL.
         PDH/PDL = previous trading day (05:30-05:30 IST) high/low.
         Entry price = the 4h candle close.
  SL     entry -/+ 1 x ATR(14) of 4h (min 0.3%, max 2% of price).
  LOSS   SL touched by a 4h candle AND MACD(12,26,9) on 4h confirms (BUY: MACD < signal,
         SELL: MACD > signal) -> exit at the worse of SL and close; otherwise wait.
  PROFIT lock 1:2 / 1:3 / 1:4 (applies from the next candle; if the lock candle closes back through
         the lock -> exit at the lock), 1:5 full exit.
  MAX HOLD 3 days -> exit at close.  One trade per coin.  After a profit exit, no same-direction
         re-entry in the same session (Asian/London/NY).
Paper size: 20 USDT notional per trade (live-like), fees 0.118% round trip included in PnL.
Files: rd_btc4h_state.json (open trade, last bar), rd_btc4h_trades.csv (all events).
"""
import csv
import json
import os
import sys
import time
from datetime import time as dtime, timedelta

import numpy as np
import pandas as pd
import requests

import rd_trading as rd

HERE = os.path.dirname(os.path.abspath(__file__))
PAIRS = [p.strip() for p in os.getenv("BTC4H_PAIRS", "B-BTC_USDT").split(",") if p.strip()]
STATE_FILE = os.path.join(HERE, "rd_btc4h_state.json")
TRADES_FILE = os.path.join(HERE, "rd_btc4h_trades.csv")
NOTIONAL, FEE = 20.0, 0.00118
MIN_RISK, MAX_RISK = 0.003, 0.02
LOCKS, TARGET = (2.0, 3.0, 4.0), 5.0
MAX_HOLD = timedelta(days=3)
TAG = "[rd_btc4h_paper.py] PAPER"


# ------------------------------------------------------------------ data
def fetch_15m(pair, days=25):
    now = int(time.time())
    frames, end = [], now
    while end > now - days * 86400:
        beg = max(now - days * 86400, end - 5 * 86400)
        for attempt in range(3):
            try:
                r = requests.get(rd.CANDLE_URL, params={"pair": pair, "from": beg, "to": end, "resolution": "15",
                                                        "pcode": "f"}, headers={"User-Agent": "RD-BTC4H/1.0"}, timeout=25)
                r.raise_for_status()
                j = r.json()
                raw = (j.get("data", j.get("candles", [])) if isinstance(j, dict) else j) or []
                if raw:
                    frames.append(pd.DataFrame(raw))
                break
            except Exception:
                time.sleep(2 + attempt)
        end = beg
    if not frames:
        return pd.DataFrame()
    d = pd.concat(frames, ignore_index=True)
    for c in ("open", "high", "low", "close", "volume"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=["open", "high", "low", "close", "volume"]).drop_duplicates("time").sort_values("time")
    d["Timestamp"] = pd.to_datetime(d["time"], unit="ms", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    return d.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})[
        ["Timestamp", "Open", "High", "Low", "Close", "Volume"]].reset_index(drop=True)


def session(ts):
    t = ts.time()
    if dtime(5, 30) <= t < dtime(13, 30):
        return "Asian"
    if dtime(13, 30) <= t < dtime(18, 30):
        return "London"
    return "NY"


def build_4h(d15):
    now = pd.Timestamp.now("Asia/Kolkata").tz_localize(None)
    d15 = d15[d15["Timestamp"] + pd.Timedelta(minutes=15) <= now]                  # closed 15m only
    g = d15.set_index("Timestamp").resample("4h", origin="start_day", offset="330min")
    h = g.agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
    cnt = g["Close"].count()
    h = h[cnt.reindex(h.index) == 16]                                              # complete 4h bars only
    h = h[h.index + pd.Timedelta(hours=4) <= now].reset_index()
    late = h["Timestamp"].dt.time < dtime(5, 30)
    h["Date"] = (h["Timestamp"] - pd.to_timedelta(late.astype(int), unit="D")).dt.date
    h["Session"] = h["Timestamp"].apply(session)
    h["VolMA"] = h["Volume"].shift(1).rolling(20, min_periods=10).mean()
    e1, e2 = h["Close"].ewm(span=12, adjust=False).mean(), h["Close"].ewm(span=26, adjust=False).mean()
    h["MACD"] = e1 - e2
    h["Sig"] = h["MACD"].ewm(span=9, adjust=False).mean()
    tr = pd.concat([h["High"] - h["Low"], (h["High"] - h["Close"].shift()).abs(), (h["Low"] - h["Close"].shift()).abs()], axis=1).max(axis=1)
    h["ATR"] = tr.rolling(14).mean()
    # PDH/PDL from 15m data (previous trading day 05:30-05:30)
    late15 = d15["Timestamp"].dt.time < dtime(5, 30)
    d15 = d15.assign(Date=(d15["Timestamp"] - pd.to_timedelta(late15.astype(int), unit="D")).dt.date)
    daily = d15.groupby("Date").agg(DH=("High", "max"), DL=("Low", "min"), N=("High", "size"))
    prev = {}
    for d in daily.index:
        p = d - timedelta(days=1)
        if p in daily.index and daily.at[p, "N"] >= 90:
            prev[d] = (daily.at[p, "DH"], daily.at[p, "DL"])
    h["PDH"] = h["Date"].map(lambda d: prev.get(d, (np.nan, np.nan))[0])
    h["PDL"] = h["Date"].map(lambda d: prev.get(d, (np.nan, np.nan))[1])
    return h


# ------------------------------------------------------------------ state / output
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("trades", {})
    st.setdefault("last_ts", {})
    st.setdefault("block", {})
    return st


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, default=str)


def log_event(**kw):
    cols = ["Pair", "Kind", "Bar_IST", "Session", "Direction", "Entry", "SL", "Price", "Status", "R", "PnL_USDT_20"]
    new = not os.path.exists(TRADES_FILE)
    with open(TRADES_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        w.writerow({c: kw.get(c, "") for c in cols})


def alert(msg):
    rd.send_alert(f"{TAG}\n{msg}")


# ------------------------------------------------------------------ logic per closed 4h bar
def step(pair, r, st):
    t = st["trades"].get(pair)
    ts = r["Timestamp"]
    if t is not None:
        d, E, risk, sl = t["d"], t["E"], t["risk"], t["sl"]
        if t.get("pend", 0) > t.get("locked", 0):
            t["locked"] = t["pend"]
        locked = t.get("locked", 0)
        stop = E + d * locked * risk if locked > 0 else sl
        hit = (r["Low"] <= stop) if d == 1 else (r["High"] >= stop)
        exit_px = why = None
        if hit and locked > 0:
            exit_px, why = (r["Open"] if ((r["Open"] < stop) if d == 1 else (r["Open"] > stop)) else stop), f"Locked_1:{locked:g}_Exit"
        elif hit and ((r["MACD"] < r["Sig"]) if d == 1 else (r["MACD"] > r["Sig"])):
            exit_px, why = (min(stop, r["Close"]) if d == 1 else max(stop, r["Close"])), "SL_MACD_Confirmed"
        if exit_px is None:
            best = d * ((r["High"] if d == 1 else r["Low"]) - E) / risk
            if best >= TARGET:
                exit_px, why = E + d * TARGET * risk, "Target_1:5"
            else:
                new = max([l for l in LOCKS if best >= l], default=0.0)
                if new > max(locked, t.get("pend", 0)):
                    lp = E + d * new * risk
                    if (r["Close"] <= lp) if d == 1 else (r["Close"] >= lp):
                        exit_px, why = lp, f"Locked_1:{new:g}_Exit"
                    else:
                        t["pend"] = new
                        log_event(Pair=pair, Kind="LOCK", Bar_IST=ts, Session=r["Session"], Direction=t["dir"],
                                  Entry=E, SL=lp, Status=f"Profit_Locked_1:{new:g}")
                        alert(f"LOCK {pair} {t['dir']}: 1:{new:g} reached -> SL {lp:.2f} from next 4h candle")
        if exit_px is None and pd.Timestamp(ts) - pd.Timestamp(t["entry_bar"]) >= MAX_HOLD:
            exit_px, why = r["Close"], "Max_Hold_3d"
        if exit_px is not None:
            gross = d * (exit_px - E)
            R = gross / risk - FEE * E / risk
            usd = NOTIONAL * gross / E - NOTIONAL * FEE
            log_event(Pair=pair, Kind="EXIT", Bar_IST=ts, Session=r["Session"], Direction=t["dir"], Entry=E, SL=sl,
                      Price=round(exit_px, 2), Status=why, R=round(R, 2), PnL_USDT_20=round(usd, 3))
            alert(f"EXIT {pair} {t['dir']} | {why} | entry {E:.2f} -> {exit_px:.2f} | {R:+.2f}R | {usd:+.3f} USDT (20 USDT size)")
            if usd > 0:
                st["block"][f"{pair}|{t['dir']}"] = f"{r['Date']}|{r['Session']}"
            del st["trades"][pair]
        return
    # ---- entry
    if r["PDH"] != r["PDH"] or r["VolMA"] != r["VolMA"] or r["ATR"] != r["ATR"]:
        return
    rng = r["High"] - r["Low"]
    up_w = (r["High"] - max(r["Open"], r["Close"])) / rng if rng > 0 else 0
    dn_w = (min(r["Open"], r["Close"]) - r["Low"]) / rng if rng > 0 else 0
    hv = r["Volume"] > 1.5 * r["VolMA"]
    d = 1 if (r["High"] > r["PDH"] and r["Close"] > r["PDH"] and hv and up_w < 0.4) else (
        -1 if (r["Low"] < r["PDL"] and r["Close"] < r["PDL"] and hv and dn_w < 0.4) else None)
    if d is None:
        return
    dirn = "BUY" if d == 1 else "SELL"
    if st["block"].get(f"{pair}|{dirn}") == f"{r['Date']}|{r['Session']}":
        return
    E = float(r["Close"])
    risk = float(r["ATR"])
    if risk < E * MIN_RISK:
        return
    risk = min(risk, E * MAX_RISK)
    sl = E - d * risk
    st["trades"][pair] = {"d": d, "dir": dirn, "E": E, "risk": risk, "sl": sl, "locked": 0.0, "pend": 0.0,
                          "entry_bar": str(ts)}
    log_event(Pair=pair, Kind="ENTRY", Bar_IST=ts, Session=r["Session"], Direction=dirn, Entry=E, SL=round(sl, 2),
              Status=f"Breakout_{'PDH' if d == 1 else 'PDL'}")
    alert(f"ENTRY {pair} {dirn} (4h breakout of {'PDH' if d == 1 else 'PDL'} {r['PDH'] if d == 1 else r['PDL']:.2f})\n"
          f"Entry {E:.2f} | SL {sl:.2f} (1xATR {risk/E*100:.2f}%) | 1:2 {E + d*2*risk:.2f} | 1:5 {E + d*5*risk:.2f}")


def run():
    st = load_state()
    for pair in PAIRS:
        d15 = fetch_15m(pair)
        if d15.empty:
            print(f"{pair}: no data")
            continue
        h = build_4h(d15)
        if h.empty:
            continue
        last = st["last_ts"].get(pair)
        new = h[h["Timestamp"] > pd.Timestamp(last)] if last else h.tail(1)
        for _, r in new.iterrows():
            step(pair, r, st)
        st["last_ts"][pair] = str(h["Timestamp"].iloc[-1])
        print(f"{pair}: processed {len(new)} new 4h bar(s), last {st['last_ts'][pair]}, open: {pair in st['trades']}")
    save_state(st)


def report():
    if not os.path.exists(TRADES_FILE):
        print("no trades yet")
        return
    x = pd.read_csv(TRADES_FILE)
    e = x[x["Kind"] == "EXIT"]
    print(f"Entries {int((x['Kind'] == 'ENTRY').sum())}, exits {len(e)}, wins {(e['PnL_USDT_20'] > 0).sum()}, "
          f"net {e['R'].sum():+.2f}R, {e['PnL_USDT_20'].sum():+.3f} USDT (20 USDT size)")


if __name__ == "__main__":
    report() if "--report" in sys.argv else run()
