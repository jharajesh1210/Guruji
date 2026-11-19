"""
Gold (XAU, PAXG) - which changes make the PDH/PDL breakout work on gold?
Mother strategy is NOT touched; this is a separate parametric simulator.

Grid (every combination):
  timeframe : 15m | 1h
  SL        : candle (breakout candle low/high, like mother) | atr (1 x ATR14 from entry)
  loss exit : macd (wait for MACD confirm, like mother) | hard (exit at SL at once)
  target    : ladder5 (lock 1:2/1:3/1:4, exit 1:5, like mother) | t3 (lock 1:2, exit 1:3) | t2 (exit 1:2)
  asian     : on | off (no new entries in the Asian session 05:30-13:30 IST)

Same entry signal as mother: high & close beyond previous day's high/low, volume > 1.5 x 20-bar average,
wick < 40%. Entry at the NEXT bar's open (real fill), fees 0.118% round trip, SL checked before profit
inside a bar (conservative), lock level fills at the lock price, max hold 3 days, one trade per coin,
min SL 0.15%, max SL 2%, profit exit -> no same-direction re-entry in that session.
Result per trade in R and in USDT for 20 USDT notional (live size 2 USDT x 10x).
"""
import itertools
import os
from datetime import time as dtime, timedelta

import numpy as np
import pandas as pd

import rd_trading as rd
import rd_backtest_stops as bs

PAIRS = os.getenv("BT_GOLD_PAIRS", "B-XAU_USDT,B-PAXG_USDT").split(",")
DAYS = int(os.getenv("BT_DAYS", "180"))
OUT = os.getenv("BT_OUT", "backtest_out")
FEE = 0.00118
NOTIONAL = 20.0
MIN_RISK, MAX_RISK = 0.0015, 0.02
MAX_HOLD = timedelta(days=3)


def load(pair):
    bs.DAYS = DAYS
    now = int(pd.Timestamp.utcnow().timestamp())
    frames, end, empty = [], now, 0
    while end > now - DAYS * 86400:
        beg = max(now - DAYS * 86400, end - 5 * 86400)
        raw = bs.fetch_chunk(pair, beg, end)
        if raw:
            frames.append(pd.DataFrame(raw))
            empty = 0
        else:
            empty += 1
            if empty >= 2:
                break
        end = beg
    return bs.to_df(frames)


def session(ts):
    t = ts.time()
    if dtime(5, 30) <= t < dtime(13, 30):
        return "Asian"
    if dtime(13, 30) <= t < dtime(18, 30):
        return "London"
    return "NY"


def prep(df):
    df = df.copy().reset_index(drop=True)
    df["Session"] = df["Timestamp"].apply(session)
    late_ny = (df["Session"] == "NY") & (df["Timestamp"].dt.time < dtime(5, 30))
    df["Date"] = (df["Timestamp"] - pd.to_timedelta(late_ny.astype(int), unit="D")).dt.date
    df["VolMA"] = df["Volume"].shift(1).rolling(20, min_periods=10).mean()
    e1, e2 = df["Close"].ewm(span=12, adjust=False).mean(), df["Close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = e1 - e2
    df["Sig"] = df["MACD"].ewm(span=9, adjust=False).mean()
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - df["Close"].shift()).abs(),
                    (df["Low"] - df["Close"].shift()).abs()], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(14).mean()
    daily = df.groupby("Date").agg(DH=("High", "max"), DL=("Low", "min"))
    prev = {d: (daily.at[d - timedelta(days=1), "DH"], daily.at[d - timedelta(days=1), "DL"])
            for d in daily.index if (d - timedelta(days=1)) in daily.index}
    df["PDH"] = df["Date"].map(lambda d: prev.get(d, (np.nan, np.nan))[0])
    df["PDL"] = df["Date"].map(lambda d: prev.get(d, (np.nan, np.nan))[1])
    return df


TFS = [x.strip() for x in os.getenv("BT_TFS", "15m,1h").split(",") if x.strip()]


def to_1h(df15, rule="1h"):
    g = df15.set_index("Timestamp").resample(rule, origin="start_day", offset="330min")
    h = g.agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
    return h.reset_index()


def simulate(df, sl_mode, loss_mode, target, asian, signal="breakout", vol_req=True, wick_req=False):
    T = {"ladder5": 5.0, "t3": 3.0, "t2": 2.0, "t1.5": 1.5, "t1": 1.0}[target]
    locks = [l for l in (2.0, 3.0, 4.0) if l < T]
    O, H, L, C = (df[c].values for c in ("Open", "High", "Low", "Close"))
    trades, i, n = [], 1, len(df)
    block = {}
    while i < n - 1:
        r = df.iloc[i]
        pdh, pdl = r["PDH"], r["PDL"]
        if pdh != pdh or r["VolMA"] != r["VolMA"]:
            i += 1
            continue
        rng = r["High"] - r["Low"]
        up_w = (r["High"] - max(r["Open"], r["Close"])) / rng if rng > 0 else 0
        dn_w = (min(r["Open"], r["Close"]) - r["Low"]) / rng if rng > 0 else 0
        hv = r["Volume"] > 1.5 * r["VolMA"]
        d = None
        if signal == "breakout":
            if r["High"] > pdh and r["Close"] > pdh and hv and up_w < 0.4:
                d = 1
            elif r["Low"] < pdl and r["Close"] < pdl and hv and dn_w < 0.4:
                d = -1
        else:   # fade: price pokes beyond PDH/PDL but the candle closes back inside -> trade back into the range
            vok = hv or not vol_req
            if r["High"] > pdh and r["Close"] < pdh and vok and (up_w >= 0.4 or not wick_req):
                d = -1
            elif r["Low"] < pdl and r["Close"] > pdl and vok and (dn_w >= 0.4 or not wick_req):
                d = 1
        skey = f"{r['Date']}|{r['Session']}"
        if d is None or (not asian and r["Session"] == "Asian") or block.get(d) == skey:
            i += 1
            continue
        E = O[i + 1]                                        # next bar open = real fill
        if sl_mode == "candle":
            sl = r["Low"] if d == 1 else r["High"]
        else:
            atr = r["ATR"]
            if atr != atr:
                i += 1
                continue
            sl = E - d * atr
        risk = d * (E - sl)
        if risk <= 0 or risk < E * MIN_RISK:
            i += 1
            continue
        if risk > E * MAX_RISK:
            risk = E * MAX_RISK
            sl = E - d * risk
        locked, pend, exit_px, why, j = 0.0, 0.0, None, None, i + 1
        t_in = df["Timestamp"].iloc[i + 1]
        while j < n:
            if pend > locked:
                locked = pend
            stop = E + d * locked * risk if locked > 0 else sl
            hit = (L[j] <= stop) if d == 1 else (H[j] >= stop)
            if hit:
                gap = (O[j] < stop) if d == 1 else (O[j] > stop)
                if locked > 0:
                    exit_px, why = (O[j] if gap else stop), f"lock_{locked:g}"
                    break
                if loss_mode == "hard":
                    exit_px, why = (O[j] if gap else stop), "hard_sl"
                    break
                macd_ok = (df["MACD"].iloc[j] < df["Sig"].iloc[j]) if d == 1 else (df["MACD"].iloc[j] > df["Sig"].iloc[j])
                if macd_ok:
                    exit_px = min(stop, C[j]) if d == 1 else max(stop, C[j])
                    why = "macd_loss"
                    break
            best = d * ((H[j] if d == 1 else L[j]) - E) / risk
            if best >= T:
                exit_px, why = E + d * T * risk, f"target_{T:g}"
                break
            new = max([l for l in locks if best >= l], default=0.0)
            if new > max(locked, pend):
                lock_px = E + d * new * risk
                if (C[j] <= lock_px) if d == 1 else (C[j] >= lock_px):
                    exit_px, why = lock_px, f"lock_{new:g}"
                    break
                pend = new
            if df["Timestamp"].iloc[j] - t_in >= MAX_HOLD:
                exit_px, why = C[j], "max_hold"
                break
            j += 1
        if exit_px is None:
            break
        gross = d * (exit_px - E)
        R_ = gross / risk - FEE * E / risk
        usd = NOTIONAL * gross / E - NOTIONAL * FEE
        trades.append({"entry": t_in, "dir": d, "R": R_, "usdt": usd, "exit": why})
        if usd > 0:
            block[d] = f"{df['Date'].iloc[j]}|{df['Session'].iloc[j]}"
        i = j + 1
    return trades


def summ(tr):
    if not tr:
        return dict(n=0, win=0, R=0, usdt=0, pf=0, dd=0, h1=0, h2=0)
    x = pd.DataFrame(tr).sort_values("entry")
    w, l = x.usdt[x.usdt > 0], x.usdt[x.usdt < 0]
    cum = x.usdt.cumsum()
    half = len(x) // 2
    return dict(n=len(x), win=round(100 * len(w) / len(x), 1), R=round(x.R.sum(), 1),
                usdt=round(x.usdt.sum(), 2), pf=round(w.sum() / -l.sum(), 2) if len(l) else 99,
                dd=round((cum - cum.cummax()).min(), 2),
                h1=round(x.usdt.iloc[:half].sum(), 2), h2=round(x.usdt.iloc[half:].sum(), 2))


def main():
    os.makedirs(OUT, exist_ok=True)
    data = {}
    for p in PAIRS:
        d15 = load(p)
        if d15.empty or len(d15) < 500:
            print(f"{p}: no data")
            continue
        data[p] = {tf: (prep(d15) if tf == "15m" else prep(to_1h(d15, tf))) for tf in TFS}
        print(f"{p}: 15m {d15['Timestamp'].iloc[0]} -> {d15['Timestamp'].iloc[-1]} ({len(d15)} bars)", flush=True)
    rows = []
    for tf, slm, lm, tg, asian in itertools.product(TFS, ["candle", "atr"], ["macd", "hard"],
                                                    ["ladder5", "t3", "t2"], [True, False]):
        per, allt = {}, []
        for p in data:
            t = simulate(data[p][tf], slm, lm, tg, asian)
            per[p] = summ(t)["usdt"]
            allt += t
        s = summ(allt)
        rows.append({"tf": tf, "sl": slm, "loss": lm, "target": tg, "asian": "on" if asian else "off", **s,
                     **{f"usdt_{p.split('-')[1].split('_')[0]}": v for p, v in per.items()}})
    res = pd.DataFrame(rows).sort_values("usdt", ascending=False)
    res.to_csv(os.path.join(OUT, "gold_grid.csv"), index=False)
    base = res[(res.tf == "15m") & (res.sl == "candle") & (res.loss == "macd") & (res.target == "ladder5") & (res.asian == "on")]
    cols = [c for c in res.columns]
    def table(df):
        head = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
        return head + "\n".join("| " + " | ".join(str(v) for v in r) + " |" for r in df[cols].values)
    span = {p: f"{data[p]['15m']['Timestamp'].iloc[0]} -> {data[p]['15m']['Timestamp'].iloc[-1]}" for p in data}
    text = (f"Grid on {list(data)}. USDT = 20 USDT notional per trade after 0.118% fees. "
            "h1/h2 = USDT in first/second half of the trades (consistency).\n"
            f"Data: {span}\n\n### Mother-like baseline (15m, candle SL, MACD wait, 1:5 ladder, Asian on)\n\n"
            + table(base) + "\n\n### Top 12 combinations\n\n" + table(res.head(12))
            + "\n\n### Worst 5\n\n" + table(res.tail(5)) + "\n")
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text, flush=True)


if __name__ == "__main__":
    main()
