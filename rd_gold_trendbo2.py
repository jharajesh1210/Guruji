"""
GOLD strategy B (trend breakout of the Asian range) - improvement study. CoinDCX XAU + PAXG, 180 days, fees 0.118%.
BASE: Asian range 05:30-13:30 IST; daily trend = prev daily close vs 20-day avg; entry window 18:30-22:30 IST on a 15m
close beyond the Asian high (uptrend) / low (downtrend); SL 1%; stop to entry at 1R; target 1:3; time exit 05:30 IST.
One change at a time, then combinations:
  trend : sma20 | sma50 | sma20 + 4h EMA50 agree
  window: overlap 18:30-22:30 | london+overlap 13:30-22:30 | full NY 18:30-03:30
  sl    : 1% | 1.3% | 0.5 x daily ATR(14)
  exit  : t3be (1:3 + BE) | t4be | ladder (BE at 1R, lock 1R@2R, 2R@3R, 3R@4R, exit 1:5, time exit 05:30)
          | hold (same ladder, no time exit, max 3 days)
  vol   : breakout candle volume >= 1.5x its 20-candle average
  retest: after the break, enter only when price comes back to the Asian level (limit fill at the level)
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
BASE = dict(trend="sma20", window="ov", sl="1", exit="t3be", vol=False, retest=False)


def build(pair):
    _, res = bs.load_pair(pair)
    if res is None:
        return None
    df = pd.DataFrame(res[0])
    df["Timestamp"] = pd.to_datetime(df["Timestamp"])
    df["m"] = df["Timestamp"].dt.hour * 60 + df["Timestamp"].dt.minute
    df["D"] = df["Date"].astype(str)
    df["VMA"] = df["Volume"].shift(1).rolling(20, min_periods=10).mean()
    df = df.reset_index(drop=True)
    daily = df.groupby("D").agg(C=("Close", "last"), H=("High", "max"), L=("Low", "min"))
    pc = daily.C.shift(1)
    tr = pd.concat([daily.H - daily.L, (daily.H - pc).abs(), (daily.L - pc).abs()], axis=1).max(axis=1)
    info = pd.DataFrame({
        "t20": np.sign(daily.C - daily.C.rolling(20, min_periods=15).mean()).shift(1),
        "t50": np.sign(daily.C - daily.C.rolling(50, min_periods=35).mean()).shift(1),
        "datr": (tr.rolling(14, min_periods=10).mean() / daily.C).shift(1),
    })
    h4 = df.set_index("Timestamp").Close.resample("4h", offset="1h30min", label="right", closed="left").last().dropna()
    e4 = h4.ewm(span=50, adjust=False).mean()
    t4 = np.sign(h4 - e4)
    # 4h trend known at 13:30 IST (end of the Asian session) of each day
    df["T4"] = t4.reindex(df["Timestamp"], method="ffill").values   # last COMPLETED 4h bar
    return df, info.to_dict("index")


def in_window(m, w):
    if w == "ov":
        return 1110 <= m < 1350
    if w == "lov":
        return 810 <= m < 1350
    return m >= 1110 or m < 210          # full NY 18:30-03:30


def manage(rows, sgn, e, R, mode):
    """Returns (exit_px or None, exit timestamp or None)."""
    sl = e - sgn * R
    stop, locked = sl, 0.0
    tgt = {"t2be": 2, "t3be": 3, "t4be": 4}.get(mode, 5)
    for r in rows:
        if (sgn == 1 and r.Low <= stop) or (sgn == -1 and r.High >= stop):
            return stop, r.Timestamp
        best = sgn * ((r.High if sgn == 1 else r.Low) - e) / R
        if best >= tgt:
            return e + sgn * tgt * R, r.Timestamp
        if mode in ("t2be", "t3be", "t4be"):
            if best >= 1 and locked < 0.001:
                locked, stop = 0.001, e
        else:   # ladder
            new = 0.0
            for lvl, lk in ((1, 0.0), (2, 1.0), (3, 2.0), (4, 3.0)):
                if best >= lvl:
                    new = lk + 0.001
            if new > locked:
                locked, stop = new, e + sgn * (new - 0.001) * R
    return None, None


def run(df, info, cfg):
    out = []
    days = sorted(df.D.unique())
    by_day = {d: g for d, g in df.groupby("D")}
    busy_until = None
    for di, d in enumerate(days):
        day = by_day[d]
        inf = info.get(d, {})
        t = inf.get("t20") if cfg["trend"] in ("sma20", "sma20_4h") else inf.get("t50")
        if not t or t != t:
            continue
        a = day[(day.m >= 330) & (day.m < 810)]
        if len(a) < 28:
            continue
        if busy_until is not None and a.Timestamp.max() <= busy_until:
            continue
        ah, al = a.High.max(), a.Low.min()
        level = ah if t > 0 else al
        after = day[day.Timestamp > a.Timestamp.max()]
        trade, broke = None, False
        for idx, r in after.iterrows():
            if not in_window(r.m, cfg["window"]):
                if broke and cfg["retest"]:
                    continue
                continue
            if cfg["trend"] == "sma20_4h" and r.T4 != t:
                continue
            if not cfg["retest"]:
                if (t > 0 and r.Close > ah) or (t < 0 and r.Close < al):
                    if cfg["vol"] and not (r.VMA and r.Volume >= 1.5 * r.VMA):
                        continue
                    trade = (idx, r.Close)
                    break
            else:
                if not broke:
                    if (t > 0 and r.Close > ah) or (t < 0 and r.Close < al):
                        broke = True
                    continue
                if (t > 0 and r.Low <= level) or (t < 0 and r.High >= level):
                    trade = (idx, level)
                    break
        if trade is None:
            continue
        idx, e = trade
        if cfg["sl"] == "atr":
            dv = inf.get("datr")
            if not dv or dv != dv:
                continue
            R = 0.5 * dv * e
        else:
            R = float(cfg["sl"]) / 100 * e
        sgn = int(t)
        if cfg["exit"] == "hold":
            rest = df.loc[idx + 1: idx + 1 + 3 * 96]
        else:
            rest = after.loc[idx + 1:]
        x, end_ts = manage(rest.itertuples(index=False), sgn, e, R, cfg["exit"])
        if x is None:
            x = rest.Close.iloc[-1] if len(rest) else e
            end_ts = rest.Timestamp.iloc[-1] if len(rest) else None
        if cfg["exit"] == "hold":
            busy_until = end_ts
        pct = sgn * (x - e) / e - FEE
        out.append({"D": d, "pct": pct, "R": pct * e / R})
    return pd.DataFrame(out)


def line(name, x):
    if x.empty:
        return f"| {name} | 0 | | | | | | |", -9
    u = NOTIONAL * x.pct
    eq = u.cumsum()
    h = len(x) // 2
    return (f"| {name} | {len(x)} | {100 * (x.pct > 0).mean():.0f}% | {x.R.sum():.1f} | **{u.sum():.2f}** | "
            f"{(eq - eq.cummax()).min():.2f} | {u.iloc[:h].sum():.2f} | {u.iloc[h:].sum():.2f} |"), u.sum()


def main():
    os.makedirs(OUT, exist_ok=True)
    single = [("BASE (B)", {}), ("L1 london+overlap", {"window": "lov"})]
    combos = [("L2 lov + 4h trend", {"window": "lov", "trend": "sma20_4h"}),
              ("L3 lov + hold 3 days", {"window": "lov", "exit": "hold"}),
              ("L4 lov + 4h trend + hold", {"window": "lov", "trend": "sma20_4h", "exit": "hold"}),
              ("L5 lov + SL 1.3%", {"window": "lov", "sl": "1.3"}),
              ("L6 lov + 1:2 + BE", {"window": "lov", "exit": "t2be"}),
              ("L7 lov + retest", {"window": "lov", "retest": True})]
    text = [f"Gold strategy B improvement study, CoinDCX, last {DAYS} days, fees 0.118%, {NOTIONAL:g} USDT/trade.", ""]
    for pair in ("B-XAU_USDT", "B-PAXG_USDT"):
        res = build(pair)
        if res is None:
            text.append(f"{pair}: no data"); continue
        df, info = res
        text += [f"## {pair}", "", "| Variant | Trades | Win % | Net R | USDT @10 | Max DD | 1st half | 2nd half |",
                 "|---|---|---|---|---|---|---|---|"]
        scores = []
        for name, ch in single + combos:
            ln, sc = line(name, run(df, info, {**BASE, **ch}))
            text.append(ln)
            scores.append((sc, name))
        scores.sort(reverse=True)
        text += ["", "Top 5: " + "; ".join(f"{n} ({s:.2f})" for s, n in scores[:5]), ""]
    t = "\n".join(text) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(t)
    print("\n=== RESULT ===\n" + t, flush=True)


if __name__ == "__main__":
    main()
