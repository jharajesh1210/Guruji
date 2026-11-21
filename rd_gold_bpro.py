"""
GOLD B-Pro polish round 2 (CoinDCX XAU + PAXG, up to 365 days, fees 0.118%, 10 USDT/trade).
B-Pro: Asian range 05:30-13:30 IST; daily trend = prev close vs 20-day SMA; window 13:30-22:30 IST; stop-order entry at
Asian high/low +/- 0.05%; SL = other side of range (min 0.4%, max 1%); target 2.5R; stop to entry at 1.5R;
time exit 05:30 IST; one trade per day.
One change at a time (robustness check = both halves of the period):
  trend: sma10 | ema20 | sma20 & rising | prev-day close above prev-day high (momentum)
  window end: 20:30 | 00:30
  buffer 0.02% | 0.10% ; SL min 0.3/0.5, max 0.8/1.2
  exits: lock ladder (1R@2R) to 2.5R | partial 50% at 1.5R + rest to 3R | target 2R | 3R
  day filter: skip Monday | skip Friday
  volatility: skip if previous day range < 1.0% | skip if Asian range > 1.2x its 20-day average
  re-entry: allow a 2nd trade the same day after a stop-out
"""
import os

import numpy as np
import pandas as pd

import rd_backtest_stops as bs

DAYS = int(os.getenv("BT_DAYS", "365"))
bs.DAYS = DAYS + 2
OUT = os.getenv("BT_OUT", "backtest_out")
FEE = 0.00118
NOTIONAL = 10.0
BASE = dict(trend="sma20", ws=810, we=1350, buf=0.0005, slmin=0.004, slmax=0.01, tp=2.5, be=1.5, exit="std",
            skipdow=None, prevrng=None, arng=None, reentry=False)


def build(pair):
    _, res = bs.load_pair(pair)
    if res is None:
        return None
    df = pd.DataFrame(res[0])
    df["Timestamp"] = pd.to_datetime(df["Timestamp"])
    df["m"] = df["Timestamp"].dt.hour * 60 + df["Timestamp"].dt.minute
    df["D"] = df["Date"].astype(str)
    df = df.reset_index(drop=True)
    d = df.groupby("D").agg(C=("Close", "last"), H=("High", "max"), L=("Low", "min"))
    sma20 = d.C.rolling(20, min_periods=15).mean()
    a = df[(df.m >= 330) & (df.m < 810)].groupby("D").agg(AH=("High", "max"), AL=("Low", "min"))
    arng = (a.AH - a.AL) / a.AL
    info = pd.DataFrame({
        "sma20": np.sign(d.C - sma20).shift(1),
        "sma10": np.sign(d.C - d.C.rolling(10, min_periods=8).mean()).shift(1),
        "ema20": np.sign(d.C - d.C.ewm(span=20, adjust=False).mean()).shift(1),
        "sma20r": (np.sign(d.C - sma20) * ((np.sign(d.C - sma20) == np.sign(sma20.diff(3))).astype(int))).shift(1),
        "mom": np.where(d.C > d.H.shift(1), 1, np.where(d.C < d.L.shift(1), -1, 0)),
        "prevrng": ((d.H - d.L) / d.L).shift(1),
        "arng_ratio": arng / arng.rolling(20, min_periods=10).mean().shift(1),
        "dow": pd.to_datetime(d.index).dayofweek,
    }, index=d.index)
    info["mom"] = pd.Series(info["mom"].values, index=d.index).shift(1)
    return df, info.to_dict("index")


def in_win(m, ws, we):
    if we > 1440:
        return m >= ws or m < we - 1440
    return ws <= m < we


def one_trade(rows, sgn, lvl, opp, cfg):
    """Scan rows for a stop-entry; then manage. Returns (pct, R, exit_row_pos, stopped) or None."""
    trig = lvl * (1 + sgn * cfg["buf"])
    for i, r in enumerate(rows):
        if not in_win(r.m, cfg["ws"], cfg["we"]):
            continue
        if (sgn == 1 and r.High >= trig) or (sgn == -1 and r.Low <= trig):
            e = max(trig, r.Open) if sgn == 1 else min(trig, r.Open)
            R = min(max(abs(e - opp), cfg["slmin"] * e), cfg["slmax"] * e)
            sl = e - sgn * R
            stop, part, x, stopped = sl, None, None, False
            tp = cfg["tp"]
            for j in range(i, len(rows)):
                q = rows[j]
                if (sgn == 1 and q.Low <= stop) or (sgn == -1 and q.High >= stop):
                    x, stopped = stop, stop == sl
                    break
                if j == i:
                    continue            # entry bar: only the stop is checked (conservative)
                best = sgn * ((q.High if sgn == 1 else q.Low) - e) / R
                if cfg["exit"] == "partial":
                    if part is None and best >= 1.5:
                        part, stop = e + sgn * 1.5 * R, e
                    if best >= 3.0:
                        x = e + sgn * 3.0 * R
                        break
                else:
                    if best >= tp:
                        x = e + sgn * tp * R
                        break
                    if cfg["exit"] == "ladder":
                        if best >= 2.0:
                            stop = e + sgn * 1.0 * R if sgn * (stop - e) < R else stop
                        elif best >= cfg["be"] and sgn * (stop - e) < 0:
                            stop = e
                    elif best >= cfg["be"] and sgn * (stop - e) < 0:
                        stop = e
            if x is None:
                x, j = rows[-1].Close, len(rows) - 1
            gross = sgn * (x - e) / e
            if part is not None:
                gross = 0.5 * sgn * (part - e) / e + 0.5 * gross
            return gross - FEE, R / e, j, stopped
    return None


def run(df, info, cfg):
    out = []
    for d, day in df.groupby("D", sort=True):
        inf = info.get(d)
        if not inf:
            continue
        t = inf.get(cfg["trend"])
        if not t or t != t:
            continue
        if cfg["skipdow"] is not None and inf["dow"] == cfg["skipdow"]:
            continue
        if cfg["prevrng"] and not (inf["prevrng"] == inf["prevrng"] and inf["prevrng"] >= cfg["prevrng"]):
            continue
        if cfg["arng"] and inf["arng_ratio"] == inf["arng_ratio"] and inf["arng_ratio"] > cfg["arng"]:
            continue
        a = day[(day.m >= 330) & (day.m < 810)]
        if len(a) < 28:
            continue
        ah, al = a.High.max(), a.Low.min()
        sgn = int(t)
        lvl, opp = (ah, al) if sgn == 1 else (al, ah)
        rows = list(day[day.Timestamp > a.Timestamp.max()].itertuples(index=False))
        res = one_trade(rows, sgn, lvl, opp, cfg)
        if res is None:
            continue
        pct, rp, j, stopped = res
        out.append({"D": d, "pct": pct})
        if cfg["reentry"] and stopped and j + 1 < len(rows):
            res2 = one_trade(rows[j + 1:], sgn, lvl, opp, cfg)
            if res2:
                out.append({"D": d, "pct": res2[0]})
    return pd.DataFrame(out)


def line(name, x):
    if x.empty:
        return f"| {name} | 0 | | | | | |", -9
    u = NOTIONAL * x.pct
    eq = u.cumsum()
    h = len(x) // 2
    a, b = u.iloc[:h].sum(), u.iloc[h:].sum()
    return (f"| {name} | {len(x)} | {100 * (x.pct > 0).mean():.0f}% | **{u.sum():.2f}** | "
            f"{(eq - eq.cummax()).min():.2f} | {a:.2f} | {b:.2f} |"), min(a, b)


VARIANTS = [
    ("B-Pro (base)", {}),
    ("trend sma10", {"trend": "sma10"}), ("trend ema20", {"trend": "ema20"}),
    ("trend sma20 + rising", {"trend": "sma20r"}), ("trend momentum (close beyond prev H/L)", {"trend": "mom"}),
    ("window to 20:30", {"we": 1230}), ("window to 00:30", {"we": 1470}),
    ("buffer 0.02%", {"buf": 0.0002}), ("buffer 0.10%", {"buf": 0.001}),
    ("SL min 0.3%", {"slmin": 0.003}), ("SL min 0.5%", {"slmin": 0.005}),
    ("SL max 0.8%", {"slmax": 0.008}), ("SL max 1.2%", {"slmax": 0.012}),
    ("exit ladder (1R lock at 2R)", {"exit": "ladder"}), ("exit partial 50% @1.5R, rest 3R", {"exit": "partial"}),
    ("target 2R", {"tp": 2.0}), ("target 3R", {"tp": 3.0}), ("BE at 1R", {"be": 1.0}),
    ("skip Monday", {"skipdow": 0}), ("skip Friday", {"skipdow": 4}),
    ("skip if prev day range < 1%", {"prevrng": 0.01}),
    ("skip if Asian range > 1.2x avg", {"arng": 1.2}),
    ("re-entry after stop-out", {"reentry": True}),
]


def main():
    os.makedirs(OUT, exist_ok=True)
    text = [f"Gold B-Pro polish round 2, CoinDCX, up to {DAYS} days, fees 0.118%, {NOTIONAL:g} USDT/trade.", ""]
    for pair in ("B-XAU_USDT", "B-PAXG_USDT"):
        res = build(pair)
        if res is None:
            text.append(f"{pair}: no data"); continue
        df, info = res
        text += [f"## {pair} ({df.D.nunique()} days, {df.Timestamp.min():%d-%b-%Y} to {df.Timestamp.max():%d-%b-%Y})", "",
                 "| Variant | Trades | Win % | USDT @10 | Max DD | 1st half | 2nd half |", "|---|---|---|---|---|---|---|"]
        robust = []
        for name, ch in VARIANTS:
            ln, worst_half = line(name, run(df, info, {**BASE, **ch}))
            text.append(ln)
            robust.append((worst_half, name))
        robust.sort(reverse=True)
        text += ["", "Most robust (best WORST half): " + "; ".join(f"{n} ({s:.2f})" for s, n in robust[:6]), ""]
    t = "\n".join(text) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(t)
    print("\n=== RESULT ===\n" + t, flush=True)


if __name__ == "__main__":
    main()
