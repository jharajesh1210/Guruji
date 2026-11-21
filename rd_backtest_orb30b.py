"""
PDH/PDL + session 30-minute ORB (first 2 candles of each session) together.
Modes:  daily  = PDH/PDL only (NOW)
        orb    = BUY level = max(PDH, session 30m high), SELL level = min(PDL, 30m low); no entry in the first 30 min
        orbpdh = same, but in the first 30 min of a session the plain PDH/PDL is used
(old notes below)
Breakout level test - which level is best for the current live rules (v2 + BB squeeze)?
  1. daily   : previous trading day PDH/PDL (NOW, live + paper)
  2. cur     : ORIGINAL 14-Nov rule - running high/low of the CURRENT session (prior candles of that session)
  2b. cur4   : same, but only after the first 4 candles (1 hour) of the session
  3. same    : yesterday's SAME session high/low (Asian->yesterday Asian, London->yesterday London, NY->yesterday NY)
  4. prev    : the session just before (Asian->last NY, London->today Asian, NY->today London)
Everything else = rd_trading.py (volume, wick, SL 0.75-2%, 0.5% distance, BB squeeze, loss cap, MACD exit,
locks 1:2-1:5, 1:6), real caps (5 open / 5 new). 90 days, live-style 1m, 10 USDT/trade, fees.
"""
import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

import rd_trading as rd
import rd_backtest_stops as bs
import rd_backtest_live as bl

OUT = os.getenv("BT_OUT", "backtest_out")
DAYS = int(os.getenv("BT_DAYS", "90"))
bs.DAYS = DAYS + 2
bl.LIVE_NOTIONAL = 10.0
bl.EMERG = 0.15
rd.MAX_LOSS_USDT = 1e9
MIN_SESS_BARS = 12

_levels, _step = rd.levels_for, rd.step_candle
CUR = {"row": None, "mode": "daily"}


def levels_patched(date, daily):
    r, m = CUR["row"], CUR["mode"]
    if m == "daily" or r is None:
        return _levels(date, daily)
    h, l = r.get("LH_" + m), r.get("LL_" + m)
    if m in ("orb_al", "orb_a"):
        okset = ("Asian", "London") if m == "orb_al" else ("Asian",)
        if r.get("Session") not in okset:
            return _levels(date, daily)
        m = "orb"
    if m in ("orb", "orbpdh"):
        ph, pl = _levels(date, daily)
        if ph is None:
            return None, None
        oh, ol = r.get("OH"), r.get("OL")
        if oh != oh or ol != ol:
            return (ph, pl) if m == "orbpdh" else (None, None)
        return max(ph, float(oh)), min(pl, float(ol))
    if h is None or l is None or h != h or l != l:
        return None, None
    return float(h), float(l)


def step_patched(row, *a, **k):
    CUR["row"] = row
    try:
        return _step(row, *a, **k)
    finally:
        CUR["row"] = None


rd.levels_for, rd.step_candle = levels_patched, step_patched
ORDER = {"Asian": 0, "London": 1, "NY": 2}


def add_levels(recs):
    df = pd.DataFrame({"Date": [str(r["Date"]) for r in recs], "S": [r["Session"] for r in recs],
                       "H": [r["High"] for r in recs], "L": [r["Low"] for r in recs]})
    g = df.groupby(["Date", "S"], sort=False)
    # running high/low of the current session from PRIOR candles only
    df["cH"] = g["H"].transform(lambda s: s.shift(1).cummax())
    df["cL"] = g["L"].transform(lambda s: s.shift(1).cummin())
    df["k"] = g.cumcount()
    df["OH"] = g["H"].transform(lambda s: s.iloc[:2].max())
    df["OL"] = g["L"].transform(lambda s: s.iloc[:2].min())
    agg = df.groupby(["Date", "S"], sort=False).agg(H=("H", "max"), L=("L", "min"), n=("H", "size")).reset_index()
    agg["o"] = agg["S"].map(ORDER)
    agg = agg.sort_values(["Date", "o"]).reset_index(drop=True)
    full = agg[agg["n"] >= MIN_SESS_BARS]
    # previous session (time order) and yesterday's same session
    prev = {}
    rows = list(agg.itertuples(index=False))
    for i in range(1, len(rows)):
        p = rows[i - 1]
        if p.n >= MIN_SESS_BARS:
            prev[(rows[i].Date, rows[i].S)] = (p.H, p.L)
    same = {}
    for s, grp in full.groupby("S"):
        gl = list(grp.itertuples(index=False))
        for i in range(1, len(gl)):
            d0 = pd.Timestamp(gl[i - 1].Date)
            d1 = pd.Timestamp(gl[i].Date)
            if (d1 - d0).days <= 3:
                same[(gl[i].Date, s)] = (gl[i - 1].H, gl[i - 1].L)
    for r, (d, s, ch, cl, k, oh, ol) in zip(recs, df[["Date", "S", "cH", "cL", "k", "OH", "OL"]].itertuples(index=False)):
        r["OH"], r["OL"] = (oh, ol) if k >= 2 else (np.nan, np.nan)
        r["LH_cur"], r["LL_cur"] = (ch, cl) if k >= 1 else (np.nan, np.nan)
        r["LH_cur4"], r["LL_cur4"] = (ch, cl) if k >= 4 else (np.nan, np.nan)
        r["LH_same"], r["LL_same"] = same.get((d, s), (np.nan, np.nan))
        r["LH_prev"], r["LL_prev"] = prev.get((d, s), (np.nan, np.nan))


def main():
    os.makedirs(OUT, exist_ok=True)
    order_all = [p["pair"] for p in rd.fetch_liquid_pairs()]
    data = {}
    with ThreadPoolExecutor(max_workers=bl.WORKERS) as ex:
        for pair, res in ex.map(bs.load_pair, order_all):
            if res is not None:
                add_levels(res[0])
                data[pair] = res
    order = [p for p in order_all if p in data]
    modes = ["daily", "orb", "orb_al", "orb_a"]
    trs = {}
    for m in modes:
        CUR["mode"] = m
        trs[m] = [t for t in bl.paper_trades(data, order) if "exit_ts" in t]
        print(m, len(trs[m]), flush=True)
    CUR["mode"] = "daily"
    need = {(t["pair"], t["entry_ts"], t["exit_ts"]): t for v in trs.values() for t in v}
    cache = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for (k, t), m1 in zip(need.items(), ex.map(bl.load_1m, list(need.values()))):
            cache[k] = m1

    def run(m):
        rows = []
        for t in trs[m]:
            m1 = cache.get((t["pair"], t["entry_ts"], t["exit_ts"]))
            if m1 is None or len(m1) < 2:
                continue
            recs, daily = data[t["pair"]]
            r = bl.sim_live(t, recs, daily, m1, "CANDLE", None, cap_r=rd.LOSS_CAP_R, cap_pct=rd.LOSS_CAP_PCT / 100)
            if r is not None:
                r["session"] = recs[t["entry_ri"]]["Session"]
                r["paper_usdt"] = t["pnl"] / rd.POSITION_USDT * 10.0
                rows.append(r)
        return rows

    names = {"daily": "1. Daily PDH/PDL (NOW)", "cur": "2. ORIGINAL: current session running H/L",
             "cur4": "2b. Current session H/L after 1st hour", "same": "3. Yesterday's SAME session H/L",
             "prev": "4. Previous session H/L", "orb": "2. PDH/PDL + session 30m H/L (skip 1st 30m)",
             "orb_al": "3. 30m H/L only in Asian + London (NY = PDH/PDL)", "orb_a": "4. 30m H/L only in Asian", "orbpdh": "3. PDH/PDL + session 30m H/L (1st 30m: PDH/PDL only)"}
    lines = [f"PDH/PDL + session 30-min high/low test, current live rules, {len(order)} coins, {DAYS} days, live-style 1m, 10 USDT/trade, fees.", "",
             "| Level | Trades | Win % | Net USDT @10 | PF | Max DD | 1st half | 2nd half | Paper USDT (no fees) |",
             "|---|---|---|---|---|---|---|---|---|"]
    sess = ["", "Per session (live-style USDT / win %):", "", "| Level | Asian | London | NY |", "|---|---|---|---|"]
    for m in modes:
        rows = run(m)
        if not rows:
            lines.append(f"| {names[m]} | 0 |")
            continue
        u = bl.stats(rows, "usdt")
        x = pd.DataFrame(rows).sort_values("entry_ts")
        h = len(x) // 2
        lines.append(f"| {names[m]} | {u['n']} | **{u['win']}** | **{u['net']}** | {u['pf']} | {u['dd']} | "
                     f"{x['usdt'].iloc[:h].sum():.1f} | {x['usdt'].iloc[h:].sum():.1f} | {x['paper_usdt'].sum():.1f} |")
        cells = []
        for s in ("Asian", "London", "NY"):
            g = x[x.session == s]
            cells.append(f"{g.usdt.sum():.1f} / {100 * (g.usdt > 0).mean():.0f}% ({len(g)})" if len(g) else "-")
        sess.append(f"| {names[m]} | " + " | ".join(cells) + " |")
    text = "\n".join(lines + sess) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print("\n=== RESULT ===\n" + text, flush=True)


if __name__ == "__main__":
    main()
