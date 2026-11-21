"""
GOLD: Asian range breakout traded ONLY in the London-NY overlap (18:30-22:30 IST), wide SL, fees included.
Asian range = high/low of 05:30-13:30 IST (same trading day). Entry on a 15m CLOSE beyond the range inside the
overlap window. One trade per day. Exit: SL, target, or time exit at 05:30 IST (end of NY).
Variants:
  fresh  : the breakout candle must be the first close outside (previous close inside the range)
  any    : first overlap candle that closes outside the range (even if price was already outside)
  trend  : 'any' + close on the right side of the 15m EMA200 (~1h EMA50)
SL: fixed % from entry (0.5 / 0.7 / 1.0) or Asian range midpoint ("mid"); target 1:1.5 / 1:2 / 1:3.
If SL and target fall in the same 15m candle, SL is assumed first (conservative). Fees 0.118% round trip.
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


def mins(ts):
    return ts.hour * 60 + ts.minute


def build(pair):
    _, res = bs.load_pair(pair)
    if res is None:
        return None
    df = pd.DataFrame(res[0])
    df["Timestamp"] = pd.to_datetime(df["Timestamp"])
    df["m"] = df["Timestamp"].apply(mins)
    df["D"] = df["Date"].astype(str)
    df["EMA200"] = df["Close"].ewm(span=200, adjust=False).mean()
    return df


def run(df, mode, sl_kind, rr):
    out = []
    for d, day in df.groupby("D", sort=True):
        a = day[(day.m >= 330) & (day.m < 810)]
        if len(a) < 28:
            continue
        ah, al = a.High.max(), a.Low.min()
        mid = (ah + al) / 2
        after = day[day.Timestamp > a.Timestamp.max()].reset_index(drop=True)
        ov = after.index[(after.m >= 1110) & (after.m < 1350)]
        if len(ov) == 0:
            continue
        trade = None
        for i in ov:
            row = after.iloc[i]
            prev_close = after.iloc[i - 1].Close if i > 0 else a.Close.iloc[-1]
            sgn = 1 if row.Close > ah else (-1 if row.Close < al else 0)
            if not sgn:
                continue
            if mode == "fresh" and ((sgn == 1 and prev_close > ah) or (sgn == -1 and prev_close < al)):
                continue
            if mode == "trend" and ((sgn == 1 and row.Close < row.EMA200) or (sgn == -1 and row.Close > row.EMA200)):
                continue
            e = row.Close
            if sl_kind == "mid":
                sl = mid
            else:
                sl = e * (1 - sgn * sl_kind / 100)
            R = sgn * (e - sl)
            if R <= 0:
                break
            trade = (i, sgn, e, sl, R)
            break
        if trade is None:
            continue
        i, sgn, e, sl, R = trade
        tgt = e + sgn * rr * R
        exit_px, why = None, None
        for j in range(i + 1, len(after)):
            r = after.iloc[j]
            if (sgn == 1 and r.Low <= sl) or (sgn == -1 and r.High >= sl):
                exit_px, why = sl, "SL"; break
            if (sgn == 1 and r.High >= tgt) or (sgn == -1 and r.Low <= tgt):
                exit_px, why = tgt, "TGT"; break
        if exit_px is None:
            exit_px, why = after.Close.iloc[-1], "TIME"
        pct = sgn * (exit_px - e) / e - FEE
        out.append({"D": d, "dir": sgn, "why": why, "pct": pct, "R": pct * e / R, "sl_pct": 100 * R / e})
    return pd.DataFrame(out)


def line(name, x):
    if x.empty:
        return f"| {name} | 0 |"
    usdt = NOTIONAL * x.pct
    eq = usdt.cumsum()
    dd = (eq - eq.cummax()).min()
    h = len(x) // 2
    return (f"| {name} | {len(x)} | {100 * (x.pct > 0).mean():.0f}% | {x.R.sum():.1f} | {usdt.sum():.2f} | {dd:.2f} | "
            f"{usdt.iloc[:h].sum():.2f} | {usdt.iloc[h:].sum():.2f} | {x.sl_pct.mean():.2f} |")


def main():
    os.makedirs(OUT, exist_ok=True)
    text = [f"Gold Asian-range breakout in the London-NY overlap (18:30-22:30 IST), CoinDCX, last {DAYS} days, "
            f"15m, fees 0.118%, {NOTIONAL:g} USDT/trade.", ""]
    for pair in ("B-XAU_USDT", "B-PAXG_USDT"):
        df = build(pair)
        if df is None:
            text.append(f"{pair}: no data")
            continue
        text += [f"## {pair} ({df.D.nunique()} days)", "",
                 "| Variant | Trades | Win % | Net R (after fees) | USDT @10 | Max DD | 1st half | 2nd half | Avg SL % |",
                 "|---|---|---|---|---|---|---|---|---|"]
        best = []
        for mode in ("fresh", "any", "trend"):
            for sl in (0.5, 0.7, 1.0, "mid"):
                for rr in (1.5, 2.0, 3.0):
                    x = run(df, mode, sl, rr)
                    name = f"{mode} SL {sl}{'%' if sl != 'mid' else ''} 1:{rr:g}"
                    text.append(line(name, x))
                    if not x.empty:
                        best.append((NOTIONAL * x.pct.sum(), name))
        best.sort(reverse=True)
        text += ["", "Top 5 by USDT: " + "; ".join(f"{n} ({u:.2f})" for u, n in best[:5]), ""]
    t = "\n".join(text) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(t)
    print("\n=== RESULT ===\n" + t, flush=True)


if __name__ == "__main__":
    main()
