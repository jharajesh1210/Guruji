"""
GOLD (CoinDCX B-XAU_USDT and B-PAXG_USDT) - which session moves, and which simple expert setups worked.
Sessions IST: Asian 05:30-13:30, London 13:30-18:30, NY 18:30-05:30; "Overlap" = 18:30-22:30 IST (London-NY overlap).
Part 1: average range per session, share of the day's high/low made in each session.
Part 2: Asian range -> breakout in London / NY: how often price runs 1x the Asian range in the break direction
        before returning to the opposite side of the range.
Part 3: simple 15m session-open range breakout (first 30 min of each session), SL other side of the range,
        target 1:2, else exit at session end. Win %, net R, R after fees (0.118% round trip).
"""
import os

import numpy as np
import pandas as pd

import rd_trading as rd
import rd_backtest_stops as bs

DAYS = int(os.getenv("BT_DAYS", "180"))
bs.DAYS = DAYS + 2
OUT = os.getenv("BT_OUT", "backtest_out")
FEE = 0.00118


def sess_of(ts):
    t = ts.hour * 60 + ts.minute
    if 330 <= t < 810:
        return "Asian"
    if 810 <= t < 1110:
        return "London"
    return "NY"


def analyse(pair):
    _, res = bs.load_pair(pair)
    if res is None:
        return f"{pair}: no data\n"
    df = pd.DataFrame(res[0])
    df["Timestamp"] = pd.to_datetime(df["Timestamp"])
    df["S"] = df["Timestamp"].apply(sess_of)
    df["D"] = df["Date"].astype(str)
    tmin = df["Timestamp"].dt.hour * 60 + df["Timestamp"].dt.minute
    df["OV"] = (tmin >= 1110) & (tmin < 1350)
    days = df["D"].nunique()
    out = [f"## {pair}  ({days} trading days, {df['Timestamp'].min():%d-%b} to {df['Timestamp'].max():%d-%b})", ""]

    # Part 1
    g = df.groupby(["D", "S"]).agg(H=("High", "max"), L=("Low", "min"), O=("Open", "first"), n=("High", "size"))
    g["rng"] = 100 * (g.H - g.L) / g.O
    ov = df[df.OV].groupby("D").agg(H=("High", "max"), L=("Low", "min"), O=("Open", "first"))
    ov_rng = (100 * (ov.H - ov.L) / ov.O).mean()
    dd = df.groupby("D").agg(H=("High", "max"), L=("Low", "min"))
    hi_s = df.loc[df.groupby("D")["High"].idxmax(), ["D", "S"]].set_index("D")["S"]
    lo_s = df.loc[df.groupby("D")["Low"].idxmin(), ["D", "S"]].set_index("D")["S"]
    out += ["| Session | Hours (IST) | Avg range % | Range per hour % | Day HIGH made here | Day LOW made here |",
            "|---|---|---|---|---|---|"]
    hours = {"Asian": 8, "London": 5, "NY": 11}
    for s, hh in (("Asian", "05:30-13:30"), ("London", "13:30-18:30"), ("NY", "18:30-05:30")):
        r = g.xs(s, level="S")["rng"].mean()
        out.append(f"| {s} | {hh} | {r:.2f} | {r / hours[s]:.3f} | {100 * (hi_s == s).mean():.0f}% | {100 * (lo_s == s).mean():.0f}% |")
    out.append(f"| (Overlap) | 18:30-22:30 | {ov_rng:.2f} | {ov_rng / 4:.3f} | - | - |")
    out.append(f"\nAverage full-day range: {(100 * (dd.H - dd.L) / dd.L).mean():.2f}%\n")

    # Part 2: Asian range breakout follow-through
    stats = {"London": [0, 0, 0], "NY": [0, 0, 0]}   # breaks, ran 1x range first, failed (opposite side first)
    for d, day in df.groupby("D"):
        a = day[day.S == "Asian"]
        if len(a) < 28:
            continue
        ah, al = a.High.max(), a.Low.min()
        rng = ah - al
        rest = day[day.Timestamp > a.Timestamp.max()]
        for s in ("London", "NY"):
            part = rest[rest.S == s] if s == "London" else rest
            if s == "NY":
                part = rest[rest.S == "NY"]
            direction, done = None, False
            for row in part.itertuples(index=False):
                if direction is None:
                    if row.Close > ah:
                        direction = 1
                    elif row.Close < al:
                        direction = -1
                    if direction:
                        stats[s][0] += 1
                    continue
                if direction == 1 and row.High >= ah + rng:
                    stats[s][1] += 1; done = True; break
                if direction == 1 and row.Low <= al:
                    stats[s][2] += 1; done = True; break
                if direction == -1 and row.Low <= al - rng:
                    stats[s][1] += 1; done = True; break
                if direction == -1 and row.High >= ah:
                    stats[s][2] += 1; done = True; break
            if direction is not None:
                break     # count only the FIRST session that broke the Asian range
    out += ["Asian range breakout (first 15m close outside the Asian high/low):", "",
            "| Break happened in | Breaks | Ran +1x Asian range first | Went back to the other side first |",
            "|---|---|---|---|"]
    for s, (n, ok, bad) in stats.items():
        if n:
            out.append(f"| {s} | {n} | {100 * ok / n:.0f}% | {100 * bad / n:.0f}% |")

    # Part 3: 30-min opening range breakout per session, 1:2 target
    res3 = {s: [] for s in ("Asian", "London", "NY")}
    for (d, s), part in df.groupby(["D", "S"]):
        part = part.sort_values("Timestamp")
        if len(part) < 8:
            continue
        orh, orl = part.High.iloc[:2].max(), part.Low.iloc[:2].min()
        risk = orh - orl
        if risk <= 0:
            continue
        trade = None
        for row in part.iloc[2:].itertuples(index=False):
            if trade is None:
                if row.Close > orh:
                    trade = (1, row.Close, orl)
                elif row.Close < orl:
                    trade = (-1, row.Close, orh)
                continue
            sgn, e, sl = trade
            R = abs(e - sl)
            tgt = e + sgn * 2 * R
            if (sgn == 1 and row.Low <= sl) or (sgn == -1 and row.High >= sl):
                res3[s].append((-1.0, R / e)); trade = "done"; break
            if (sgn == 1 and row.High >= tgt) or (sgn == -1 and row.Low <= tgt):
                res3[s].append((2.0, R / e)); trade = "done"; break
        if isinstance(trade, tuple):
            sgn, e, sl = trade
            R = abs(e - sl)
            res3[s].append((sgn * (part.Close.iloc[-1] - e) / R, R / e))
    out += ["", "30-min opening-range breakout per session (SL = other side of range, target 1:2, else exit at session end):", "",
            "| Session | Trades | Win % | Net R | Net R after fees | Avg SL % |", "|---|---|---|---|---|---|"]
    for s, v in res3.items():
        if not v:
            continue
        r = np.array([a for a, _ in v]); rp = np.array([b for _, b in v])
        net_fee = (r - FEE / rp).sum()
        out.append(f"| {s} | {len(v)} | {100 * (r > 0).mean():.0f}% | {r.sum():.1f} | {net_fee:.1f} | {100 * rp.mean():.2f} |")
    return "\n".join(out) + "\n\n"


def main():
    os.makedirs(OUT, exist_ok=True)
    text = f"Gold session study, CoinDCX futures, last {DAYS} days, 15m candles.\n\n"
    for p in ("B-XAU_USDT", "B-PAXG_USDT"):
        text += analyse(p)
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print("\n=== RESULT ===\n" + text, flush=True)


if __name__ == "__main__":
    main()
