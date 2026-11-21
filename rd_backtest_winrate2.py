"""
How to lift the WIN % (now ~38.8%) of the current live rules (v2 + Bollinger squeeze, live = paper,
1 USDT x 10x) towards 50% - without killing the profit.

Part A - trade analysis: what separates winners from losers (session, volume, higher-timeframe trend,
         SL size, distance from level, direction).
Part B - ideas used by professional breakout traders, each tested 90 days live-style on 1m data
         (real fill, candle-close locks, loss cap 1.5R/3%, fees, 10 USDT/trade):
   1. Breakeven at 1R            : stop to entry once price reaches 1:1
   2. Partial profit at 1R       : book 50% at 1:1, rest rides with stop at entry   (classic pro move)
   3. Higher-timeframe trend     : BUY only above a rising 1h EMA50 (= 15m EMA200), SELL mirror
   4. Strong volume              : breakout candle volume >= 2x average (instead of 1.5x)
   5. NY + Asian only            : skip London (weakest session in the 90-day test)
   6..: combinations
Win % = share of trades that end with profit after fees.
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
F = {"htf": False, "vol2": False, "nolondon": False, "vol3": False, "nyonly": False, "sl15": False, "sellonly": False}
_step = rd.step_candle


def blocked(row, d):
    if F["htf"]:
        e, sl = row.get("EMA200"), row.get("EMA200_SLOPE")
        if e != e or sl != sl:
            return True
        if d == "BUY" and not (row["Close"] > e and sl > 0):
            return True
        if d == "SELL" and not (row["Close"] < e and sl < 0):
            return True
    if F["vol2"] and not (row["Volume"] >= 2.0 * row["VolMA20"]):
        return True
    if F["nolondon"] and row.get("Session") == "London":
        return True
    if F["nyonly"] and row.get("Session") != "NY":
        return True
    if F["vol3"] and not (row["Volume"] >= 3.0 * row["VolMA20"]):
        return True
    if F["sellonly"] and d != "SELL":
        return True
    if F["sl15"]:
        sl = row["Low"] if d == "BUY" else row["High"]
        if abs(row["Close"] - sl) / row["Close"] < 0.015:
            return True
    return False


def step_f(row, trade, *a, **k):
    out = _step(row, trade, *a, **k)
    if trade is None and out[2] is not None and blocked(row, out[2]["direction"]):
        return "Entry_Skipped_Filter", 0.0, None, out[3], out[4]
    return out


rd.step_candle = step_f


def add_feats(recs):
    c = pd.Series([r["Close"] for r in recs])
    e200 = c.ewm(span=200, adjust=False).mean()
    slope = e200.diff(8)
    for r, a, b in zip(recs, e200.values, slope.values):
        r["EMA200"], r["EMA200_SLOPE"] = a, b


def main():
    os.makedirs(OUT, exist_ok=True)
    order_all = [p["pair"] for p in rd.fetch_liquid_pairs()]
    data = {}
    with ThreadPoolExecutor(max_workers=bl.WORKERS) as ex:
        for pair, res in ex.map(bs.load_pair, order_all):
            if res is not None:
                add_feats(res[0])
                data[pair] = res
    order = [p for p in order_all if p in data]
    NL = {"nolondon": True}
    entry_sets = {"base": {}, "nl": NL, "nl+vol3": {**NL, "vol3": True}, "ny": {"nyonly": True},
                  "nl+sl15": {**NL, "sl15": True}, "nl+sell": {**NL, "sellonly": True},
                  "ny+vol3": {"nyonly": True, "vol3": True}}
    trs = {}
    for k, flags in entry_sets.items():
        F.update({k2: False for k2 in F}, **flags)
        trs[k] = [t for t in bl.paper_trades(data, order) if "exit_ts" in t]
        print(k, len(trs[k]), flush=True)
    F.update({k2: False for k2 in F})
    allt = {(t["pair"], t["entry_ts"], t["exit_ts"]): t for v in trs.values() for t in v}
    cache = {}
    need = list(allt.values())
    with ThreadPoolExecutor(max_workers=8) as ex:
        for t, m1 in zip(need, ex.map(bl.load_1m, need)):
            cache[(t["pair"], t["entry_ts"], t["exit_ts"])] = m1

    def run(key, be_r=None, partial_r=None, keep=False):
        rows = []
        for t in trs[key]:
            m1 = cache.get((t["pair"], t["entry_ts"], t["exit_ts"]))
            if m1 is None or len(m1) < 2:
                continue
            recs, daily = data[t["pair"]]
            r = bl.sim_live(t, recs, daily, m1, "CANDLE", None, cap_r=rd.LOSS_CAP_R, cap_pct=rd.LOSS_CAP_PCT / 100,
                            be_r=be_r, partial_r=partial_r)
            if r is not None:
                if keep:
                    rec = recs[t["entry_ri"]]
                    pdh, pdl = rd.levels_for(rec["Date"], daily)
                    lvl = pdh if t["dir"] == "BUY" else pdl
                    r.update(session=rec.get("Session"), volx=rec["Volume"] / rec["VolMA20"] if rec["VolMA20"] else np.nan,
                             sl_pct=100 * t["R"] / t["E"], dist_pct=100 * abs(rec["Close"] - lvl) / rec["Close"],
                             htf_ok=bool((rec["Close"] > rec["EMA200"] and rec["EMA200_SLOPE"] > 0) if t["dir"] == "BUY"
                                         else (rec["Close"] < rec["EMA200"] and rec["EMA200_SLOPE"] < 0)))
                rows.append(r)
        return rows

    def row(name, rows):
        if not rows:
            return f"| {name} | 0 |"
        s, u = bl.stats(rows, "R"), bl.stats(rows, "usdt")
        x = pd.DataFrame(rows).sort_values("entry_ts")
        h = len(x) // 2
        return (f"| {name} | {s['n']} | **{u['win']}** | {s['net']} | {u['net']} | {u['pf']} | {u['dd']} | "
                f"{x['usdt'].iloc[:h].sum():.1f} | {x['usdt'].iloc[h:].sum():.1f} |")

    base = run("base", keep=True)
    X = pd.DataFrame(base)
    X["win"] = X["usdt"] > 0
    lines = [f"Win-rate study, current live rules (v2 + BB squeeze), {len(order)} coins, {DAYS} days, live-style 1m, 10 USDT/trade, fees.", "",
             "### Part A - what separates winners from losers (base trades)", "",
             "| Group | Trades | Win % | USDT |", "|---|---|---|---|"]

    def grp(name, m):
        g = X[m]
        if len(g):
            lines.append(f"| {name} | {len(g)} | {100 * g.win.mean():.1f} | {g.usdt.sum():.2f} |")
    grp("ALL", X.usdt == X.usdt)
    for s_ in ("Asian", "London", "NY"):
        grp(f"session {s_}", X.session == s_)
    grp("BUY", X.dir == "BUY")
    grp("SELL", X.dir == "SELL")
    grp("1h trend WITH trade", X.htf_ok)
    grp("1h trend AGAINST trade", ~X.htf_ok)
    grp("volume 1.5-2x", X.volx < 2)
    grp("volume 2-3x", (X.volx >= 2) & (X.volx < 3))
    grp("volume >= 3x", X.volx >= 3)
    grp("SL 0.75-1%", X.sl_pct < 1)
    grp("SL 1-1.5%", (X.sl_pct >= 1) & (X.sl_pct < 1.5))
    grp("SL 1.5-2%", X.sl_pct >= 1.5)
    grp("distance 0-0.25%", X.dist_pct <= 0.25)
    grp("distance 0.25-0.5%", X.dist_pct > 0.25)
    lines += ["", "Exit types (base):", "", "| Exit | Trades | USDT |", "|---|---|---|"]
    for k, g in X.groupby(X["exit"].str.replace(r"_1:.*", "", regex=True)):
        lines.append(f"| {k} | {len(g)} | {g.usdt.sum():.2f} |")

    head = ["| Variant | Trades | Win % | Net R | Net USDT @10 | PF | Max DD | 1st half | 2nd half |",
            "|---|---|---|---|---|---|---|---|---|"]
    lines += ["", "### 70% win hunt (all ideas, live-style)", ""] + head + [
        row("NOW (base)", base),
        row("A. London skip + 50% @1R (new paper)", run("nl", partial_r=1.0)),
        row("B. London skip + 50% @0.75R", run("nl", partial_r=0.75)),
        row("C. London skip + 50% @0.5R", run("nl", partial_r=0.5)),
        row("D. London skip + BE @0.5R (no partial)", run("nl", be_r=0.5)),
        row("E. London skip + vol>=3x + 50% @1R", run("nl+vol3", partial_r=1.0)),
        row("F. London skip + vol>=3x + 50% @0.5R", run("nl+vol3", partial_r=0.5)),
        row("G. NY only + 50% @1R", run("ny", partial_r=1.0)),
        row("H. NY only + 50% @0.5R", run("ny", partial_r=0.5)),
        row("I. NY only + vol>=3x + 50% @0.5R", run("ny+vol3", partial_r=0.5)),
        row("J. London skip + SL>=1.5% + 50% @1R", run("nl+sl15", partial_r=1.0)),
        row("K. London skip + SELL only + 50% @1R", run("nl+sell", partial_r=1.0)),
    ]
    text = "\n".join(lines) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print("\n=== RESULT ===\n" + text, flush=True)


if __name__ == "__main__":
    main()
