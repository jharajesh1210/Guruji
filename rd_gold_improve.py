"""
GOLD (CoinDCX B-XAU_USDT, B-PAXG_USDT) - improved ideas after the overlap breakout failed. 180 days, fees 0.118%.
A. SWEEP REVERSAL (overlap 18:30-22:30 IST): candle wicks beyond the Asian high/low but CLOSES back inside ->
   trade back into the range. SL = sweep candle extreme + buffer, target = Asian mid / opposite side, time exit 05:30.
B. TREND BREAKOUT: Asian range breakout in the overlap only in the direction of the daily trend
   (previous daily close vs 20-day average), SL 1%, stop to breakeven at 1R, target 1:3, time exit 05:30.
C. 4H SWING (Donchian): 4h close above the 20-bar high (below 20-bar low) with 4h EMA50 filter;
   exit on a 4h close beyond the 10-bar opposite channel or a 2.5x ATR stop. Holds for days.
D. ASIAN MEAN REVERSION: in the Asian session (05:30-13:30 IST) a 15m close outside BB(20,2.5) -> trade back
   to the middle band; SL 1.5x ATR(14); exit at session end.
Conservative fills: if SL and target are in the same candle, SL first.
"""
import os

import numpy as np
import pandas as pd

import rd_backtest_stops as bs

DAYS = int(os.getenv("BT_DAYS", "180"))
bs.DAYS = DAYS + 2
OUT = os.getenv("BT_OUT", "backtest_out")
FEE = 0.00118
NOTIONAL = 10.0


def build(pair):
    _, res = bs.load_pair(pair)
    if res is None:
        return None
    df = pd.DataFrame(res[0])
    df["Timestamp"] = pd.to_datetime(df["Timestamp"])
    df["m"] = df["Timestamp"].dt.hour * 60 + df["Timestamp"].dt.minute
    df["D"] = df["Date"].astype(str)
    tr = pd.concat([df.High - df.Low, (df.High - df.Close.shift()).abs(), (df.Low - df.Close.shift()).abs()], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(14).mean()
    mid = df.Close.rolling(20).mean()
    sd = df.Close.rolling(20).std(ddof=0)
    df["BBM"], df["BBU"], df["BBL"] = mid, mid + 2.5 * sd, mid - 2.5 * sd
    return df.reset_index(drop=True)


def walk(rows, sgn, e, sl, tgt, be_at=None):
    """rows: iterable of candles after entry. Returns (exit_px, why)."""
    stop = sl
    for r in rows:
        if (sgn == 1 and r.Low <= stop) or (sgn == -1 and r.High >= stop):
            return stop, "SL" if stop == sl else "BE"
        if tgt is not None and ((sgn == 1 and r.High >= tgt) or (sgn == -1 and r.Low <= tgt)):
            return tgt, "TGT"
        if be_at is not None and stop == sl and ((sgn == 1 and r.High >= be_at) or (sgn == -1 and r.Low <= be_at)):
            stop = e
    return None, "TIME"


def rec(out, d, sgn, e, x, R):
    pct = sgn * (x - e) / e - FEE
    out.append({"D": d, "pct": pct, "R": pct * e / R if R else 0})


def daily_trend(df):
    dc = df.groupby("D").Close.last()
    sma = dc.rolling(20, min_periods=15).mean()
    t = np.sign(dc - sma).shift(1)          # previous day's close vs its 20-day average
    return t.to_dict()


def strat_A(df, target="mid", buf=0.1):
    out = []
    for d, day in df.groupby("D", sort=True):
        a = day[(day.m >= 330) & (day.m < 810)]
        if len(a) < 28:
            continue
        ah, al = a.High.max(), a.Low.min()
        mid = (ah + al) / 2
        after = day[day.Timestamp > a.Timestamp.max()]
        ov = after[(after.m >= 1110) & (after.m < 1350)]
        for idx, r in ov.iterrows():
            sgn = 0
            if r.High > ah and r.Close < ah:
                sgn, ext = -1, r.High
            elif r.Low < al and r.Close > al:
                sgn, ext = 1, r.Low
            if not sgn:
                continue
            e = r.Close
            sl = ext * (1 + buf / 100) if sgn == -1 else ext * (1 - buf / 100)
            R = abs(e - sl)
            tgt = mid if target == "mid" else (al if sgn == -1 else ah)
            if sgn * (tgt - e) <= 0 or R <= 0:
                break
            x, why = walk(after.loc[idx + 1:].itertuples(index=False), sgn, e, sl, tgt)
            if x is None:
                x = after.Close.iloc[-1]
            rec(out, d, sgn, e, x, R)
            break
    return pd.DataFrame(out)


def strat_B(df, sl_pct=1.0, rr=3.0, be=True):
    tr = daily_trend(df)
    out = []
    for d, day in df.groupby("D", sort=True):
        t = tr.get(d)
        if not t or t != t:
            continue
        a = day[(day.m >= 330) & (day.m < 810)]
        if len(a) < 28:
            continue
        ah, al = a.High.max(), a.Low.min()
        after = day[day.Timestamp > a.Timestamp.max()]
        ov = after[(after.m >= 1110) & (after.m < 1350)]
        for idx, r in ov.iterrows():
            sgn = 1 if r.Close > ah else (-1 if r.Close < al else 0)
            if sgn != t:
                continue
            e = r.Close
            sl = e * (1 - sgn * sl_pct / 100)
            R = abs(e - sl)
            x, why = walk(after.loc[idx + 1:].itertuples(index=False), sgn, e, sl, e + sgn * rr * R,
                          e + sgn * R if be else None)
            if x is None:
                x = after.Close.iloc[-1]
            rec(out, d, sgn, e, x, R)
            break
    return pd.DataFrame(out)


def strat_C(df, n_in=20, n_out=10, atr_mult=2.5, use_ema=True):
    h4 = df.set_index("Timestamp").resample("4h", offset="1h30min").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna().reset_index()
    tr = pd.concat([h4.High - h4.Low, (h4.High - h4.Close.shift()).abs(), (h4.Low - h4.Close.shift()).abs()], axis=1).max(axis=1)
    h4["ATR"] = tr.rolling(14).mean()
    h4["EMA"] = h4.Close.ewm(span=50, adjust=False).mean()
    h4["HI"] = h4.High.shift(1).rolling(n_in).max()
    h4["LO"] = h4.Low.shift(1).rolling(n_in).min()
    h4["XH"] = h4.High.shift(1).rolling(n_out).max()
    h4["XL"] = h4.Low.shift(1).rolling(n_out).min()
    out, pos = [], None
    for r in h4.itertuples(index=False):
        if pos:
            sgn, e, stop, R, d = pos
            if (sgn == 1 and r.Low <= stop) or (sgn == -1 and r.High >= stop):
                rec(out, d, sgn, e, stop, R); pos = None
            elif (sgn == 1 and r.Close < r.XL) or (sgn == -1 and r.Close > r.XH):
                rec(out, d, sgn, e, r.Close, R); pos = None
            continue
        if r.ATR != r.ATR or r.HI != r.HI:
            continue
        sgn = 1 if r.Close > r.HI else (-1 if r.Close < r.LO else 0)
        if sgn and use_ema and ((sgn == 1 and r.Close < r.EMA) or (sgn == -1 and r.Close > r.EMA)):
            sgn = 0
        if sgn:
            R = atr_mult * r.ATR
            pos = (sgn, r.Close, r.Close - sgn * R, R, str(r.Timestamp.date()))
    return pd.DataFrame(out)


def strat_D(df, atr_mult=1.5):
    out = []
    for d, day in df.groupby("D", sort=True):
        a = day[(day.m >= 330) & (day.m < 810)]
        if len(a) < 28:
            continue
        for idx, r in a.iterrows():
            if r.BBU != r.BBU or r.ATR != r.ATR:
                continue
            sgn = -1 if r.Close > r.BBU else (1 if r.Close < r.BBL else 0)
            if not sgn:
                continue
            e = r.Close
            sl = e - sgn * atr_mult * r.ATR
            R = abs(e - sl)
            rest = a.loc[idx + 1:]
            x, why = None, "TIME"
            for q in rest.itertuples(index=False):
                if (sgn == 1 and q.Low <= sl) or (sgn == -1 and q.High >= sl):
                    x, why = sl, "SL"; break
                if (sgn == 1 and q.High >= q.BBM) or (sgn == -1 and q.Low <= q.BBM):
                    x, why = q.BBM, "MID"; break
            if x is None:
                x = rest.Close.iloc[-1] if len(rest) else e
            rec(out, d, sgn, e, x, R)
            break
    return pd.DataFrame(out)


def line(name, x):
    if x is None or x.empty:
        return f"| {name} | 0 | | | | | | |"
    u = NOTIONAL * x.pct
    eq = u.cumsum()
    h = len(x) // 2
    return (f"| {name} | {len(x)} | {100 * (x.pct > 0).mean():.0f}% | {x.R.sum():.1f} | **{u.sum():.2f}** | "
            f"{(eq - eq.cummax()).min():.2f} | {u.iloc[:h].sum():.2f} | {u.iloc[h:].sum():.2f} |")


def main():
    os.makedirs(OUT, exist_ok=True)
    text = [f"Gold - improved strategies, CoinDCX, last {DAYS} days, fees 0.118%, {NOTIONAL:g} USDT/trade.", ""]
    for pair in ("B-XAU_USDT", "B-PAXG_USDT"):
        df = build(pair)
        if df is None:
            text.append(f"{pair}: no data"); continue
        text += [f"## {pair} ({df.D.nunique()} days)", "",
                 "| Strategy | Trades | Win % | Net R | USDT @10 | Max DD | 1st half | 2nd half |",
                 "|---|---|---|---|---|---|---|---|",
                 line("A. Sweep reversal -> Asian mid", strat_A(df, "mid")),
                 line("A2. Sweep reversal -> other side", strat_A(df, "other")),
                 line("A3. Sweep reversal -> mid, buffer 0.3%", strat_A(df, "mid", 0.3)),
                 line("B. Trend breakout SL1% 1:3 + BE", strat_B(df, 1.0, 3.0, True)),
                 line("B2. Trend breakout SL1% 1:2 no BE", strat_B(df, 1.0, 2.0, False)),
                 line("B3. Trend breakout SL0.7% 1:3 + BE", strat_B(df, 0.7, 3.0, True)),
                 line("C. 4h Donchian 20/10, ATR2.5, EMA50", strat_C(df)),
                 line("C2. 4h Donchian 20/10, ATR2.5, no EMA", strat_C(df, use_ema=False)),
                 line("C3. 4h Donchian 30/15, ATR3", strat_C(df, 30, 15, 3.0)),
                 line("C4. 4h Donchian 10/5, ATR2", strat_C(df, 10, 5, 2.0)),
                 line("D. Asian BB2.5 mean reversion, SL1.5ATR", strat_D(df)),
                 line("D2. Asian BB2.5 mean reversion, SL2.5ATR", strat_D(df, 2.5)),
                 ""]
    t = "\n".join(text) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(t)
    print("\n=== RESULT ===\n" + t, flush=True)


if __name__ == "__main__":
    main()
