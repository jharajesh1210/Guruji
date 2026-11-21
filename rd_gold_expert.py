"""
GOLD - polish of improved strategy B with ideas from published international gold research. CoinDCX, 180 days, fees.
BASE (B improved): Asian range 05:30-13:30 IST, daily trend (prev close vs 20-day avg), entry window 13:30-22:30 IST
on a 15m close beyond the Asian high (up-trend) / low (down-trend), SL 1%, stop to entry at 1R, target 1:3,
time exit 05:30 IST, one trade per day.
Expert ideas tested one at a time and combined:
  RF  : range filter - skip days where the Asian range is < 0.30% or > 1.60% of price
  STOP: stop-order entry at the Asian level + 0.05% buffer (instead of waiting for the 15m close)
  SLR : SL = opposite side of the Asian range, capped at 1% (min 0.4%)
  TP25: target 2.5R, stop to entry at 1.5R
  TX  : time exit 00:30 IST (close before the end of NY) instead of 05:30
And the timing rule: ASIA LONG = buy gold at 04:30 IST, sell at 13:30 IST every day (optionally only in up-trend,
optionally with a 1% SL).
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
    df = df.reset_index(drop=True)
    dc = df.groupby("D").Close.last()
    trend = np.sign(dc - dc.rolling(20, min_periods=15).mean()).shift(1).to_dict()
    return df, trend


def b_run(df, trend, rf=False, stop=False, slr=False, tp25=False, tx=False):
    out = []
    for d, day in df.groupby("D", sort=True):
        t = trend.get(d)
        if not t or t != t:
            continue
        a = day[(day.m >= 330) & (day.m < 810)]
        if len(a) < 28:
            continue
        ah, al = a.High.max(), a.Low.min()
        rng_pct = 100 * (ah - al) / al
        if rf and not (0.30 <= rng_pct <= 1.60):
            continue
        after = day[day.Timestamp > a.Timestamp.max()]
        if tx:
            after = after[(after.m >= 810) | (after.m < 30)]       # flat by 00:30 IST
        sgn = int(t)
        lvl = ah if sgn == 1 else al
        entry = None
        for idx, r in after.iterrows():
            if not (810 <= r.m < 1350):
                continue
            if stop:
                trig = lvl * (1 + sgn * 0.0005)
                if (sgn == 1 and r.High >= trig) or (sgn == -1 and r.Low <= trig):
                    px = max(trig, r.Open) if sgn == 1 else min(trig, r.Open)
                    entry = (idx, px, True)
                    break
            elif (sgn == 1 and r.Close > ah) or (sgn == -1 and r.Close < al):
                entry = (idx, r.Close, False)
                break
        if entry is None:
            continue
        idx, e, same_bar = entry
        if slr:
            opp = al if sgn == 1 else ah
            dist = min(abs(e - opp), 0.01 * e)
            R = max(dist, 0.004 * e)
        else:
            R = 0.01 * e
        sl = e - sgn * R
        tgt_r, be_r = (2.5, 1.5) if tp25 else (3.0, 1.0)
        tgt = e + sgn * tgt_r * R
        stop_px, x = sl, None
        rows = after.loc[idx:] if same_bar else after.loc[idx + 1:]
        first = True
        for r in rows.itertuples(index=False):
            if not (first and same_bar):        # on the stop-entry bar only check the stop (conservative)
                pass
            if (sgn == 1 and r.Low <= stop_px) or (sgn == -1 and r.High >= stop_px):
                x = stop_px
                break
            if not first or not same_bar:
                if (sgn == 1 and r.High >= tgt) or (sgn == -1 and r.Low <= tgt):
                    x = tgt
                    break
                if stop_px == sl and sgn * ((r.High if sgn == 1 else r.Low) - e) >= be_r * R:
                    stop_px = e
            first = False
        if x is None:
            x = rows.Close.iloc[-1] if len(rows) else e
        pct = sgn * (x - e) / e - FEE
        out.append({"D": d, "pct": pct, "R": pct * e / R})
    return pd.DataFrame(out)


def asia_long(df, trend, only_up=False, sl_pct=None):
    out = []
    ts = df.set_index("Timestamp")
    for d in sorted(df.D.unique()):
        if only_up and trend.get(d) != 1:
            continue
        day = pd.Timestamp(d)
        start, end = day + pd.Timedelta(hours=4, minutes=30), day + pd.Timedelta(hours=13, minutes=30)
        win = ts.loc[start:end - pd.Timedelta(minutes=15)]
        if len(win) < 30:
            continue
        e = win.Open.iloc[0]
        x = win.Close.iloc[-1]
        if sl_pct:
            sl = e * (1 - sl_pct / 100)
            hit = win[win.Low <= sl]
            if len(hit):
                x = sl
        pct = (x - e) / e - FEE
        out.append({"D": d, "pct": pct, "R": pct / 0.01})
    return pd.DataFrame(out)


def line(name, x):
    if x.empty:
        return f"| {name} | 0 | | | | | |"
    u = NOTIONAL * x.pct
    eq = u.cumsum()
    h = len(x) // 2
    return (f"| {name} | {len(x)} | {100 * (x.pct > 0).mean():.0f}% | **{u.sum():.2f}** | "
            f"{(eq - eq.cummax()).min():.2f} | {u.iloc[:h].sum():.2f} | {u.iloc[h:].sum():.2f} |")


def main():
    os.makedirs(OUT, exist_ok=True)
    text = [f"Gold - expert polish of strategy B, CoinDCX, last {DAYS} days, fees 0.118%, {NOTIONAL:g} USDT/trade.", ""]
    for pair in ("B-XAU_USDT", "B-PAXG_USDT"):
        res = build(pair)
        if res is None:
            text.append(f"{pair}: no data"); continue
        df, tr = res
        text += [f"## {pair}", "", "| Variant | Trades | Win % | USDT @10 | Max DD | 1st half | 2nd half |",
                 "|---|---|---|---|---|---|---|",
                 line("BASE (B improved)", b_run(df, tr)),
                 line("+ RF range filter 0.3-1.6%", b_run(df, tr, rf=True)),
                 line("+ STOP entry at level", b_run(df, tr, stop=True)),
                 line("+ SLR SL = range other side (max 1%)", b_run(df, tr, slr=True)),
                 line("+ TP 2.5R, BE 1.5R", b_run(df, tr, tp25=True)),
                 line("+ TX close by 00:30 IST", b_run(df, tr, tx=True)),
                 line("RF + TP2.5", b_run(df, tr, rf=True, tp25=True)),
                 line("RF + TX", b_run(df, tr, rf=True, tx=True)),
                 line("RF + STOP + TP2.5 + TX (London-breakout style)", b_run(df, tr, rf=True, stop=True, tp25=True, tx=True)),
                 line("ASIA LONG 04:30->13:30 every day", asia_long(df, tr)),
                 line("ASIA LONG only in up-trend", asia_long(df, tr, only_up=True)),
                 line("ASIA LONG every day, SL 1%", asia_long(df, tr, sl_pct=1.0)),
                 ""]
    t = "\n".join(text) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(t)
    print("\n=== RESULT ===\n" + t, flush=True)


if __name__ == "__main__":
    main()
