"""
Long test: PDH/PDL (now) vs ROLLING last-96-candle high/low (24h, updates every 15m candle).

Uses the real rd_trading.py logic (step_candle: volume, wick, SL 0.3-2%, MACD loss exit, locks, 1:5,
profit block) with the real portfolio caps (max 5 open, max 5 new per scan), all liquid coins.
Only the breakout level is swapped. Mother code is NOT changed (levels are patched in memory here).

Stage 1: last BT_DAYS (90) days, 15m data. Paper result + a live estimate
         (fill at next candle open instead of close, fees 0.118%, 20 USDT notional).
Stage 2: last 30 days, live simulation on 1m data (fast lock, 0.75R late-entry guard, fees),
         same engine as rd_backtest_live.py.
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

import rd_trading as rd
import rd_backtest_stops as bs
import rd_backtest_live as bl

DAYS = int(os.getenv("BT_DAYS", "90"))
LIVE_DAYS = int(os.getenv("BT_LIVE_DAYS", "30"))
OUT = os.getenv("BT_OUT", "backtest_out")
bs.DAYS = DAYS + 2
LEVELS = [int(x) for x in os.getenv("BT_LEVELS", "96").split(",") if x.strip()]

_orig_levels = rd.levels_for
CUR = {"row": None, "mode": "pdh", "n": 96}


def levels_patched(date, daily):
    if CUR["mode"] == "roll" and CUR["row"] is not None:
        r = CUR["row"]
        h, l = r.get(f"RH{CUR['n']}"), r.get(f"RL{CUR['n']}")
        if h == h and l == l and h is not None:
            return float(h), float(l)
        return None, None
    return _orig_levels(date, daily)


rd.levels_for = levels_patched


def add_rolling(recs):
    s = pd.DataFrame({"H": [r["High"] for r in recs], "L": [r["Low"] for r in recs]})
    for n in LEVELS:
        rh = s["H"].shift(1).rolling(n, min_periods=n).max().values
        rl = s["L"].shift(1).rolling(n, min_periods=n).min().values
        for r, a, b in zip(recs, rh, rl):
            r[f"RH{n}"], r[f"RL{n}"] = a, b


def paper_trades(data, order, mode):
    """Same as rd_backtest_live.paper_trades, plus the level mode ("pdh" or "roll<N>")."""
    CUR["mode"] = "roll" if mode.startswith("roll") else "pdh"
    if mode.startswith("roll"):
        CUR["n"] = int(mode[4:])
    ev = []
    for pi, pair in enumerate(order):
        for ri, r in enumerate(data[pair][0]):
            ev.append((r["Timestamp"], pi, ri))
    ev.sort()
    open_tr, out, blocks = {}, [], {p: {} for p in order}
    i, n = 0, len(ev)
    while i < n:
        ts = ev[i][0]
        j = i
        while j < n and ev[j][0] == ts:
            j += 1
        group = sorted(ev[i:j], key=lambda e: (0 if order[e[1]] in open_tr else 1, e[1]))
        i = j
        new_entries = 0
        for _, pi, ri in group:
            pair = order[pi]
            recs, daily = data[pair]
            row = recs[ri]
            CUR["row"] = row
            cur = open_tr.get(pair)
            trade = cur["t"] if cur else None

            def allow():
                qv = row.get("QV24")
                return (qv == qv and qv >= rd.MIN_24H_VOLUME_USDT and len(open_tr) < rd.MAX_OPEN_TRADES
                        and new_entries < rd.MAX_NEW_ENTRIES_PER_CYCLE)

            status, profit, trade2, _, _ = rd.step_candle(row, trade, daily, allow, blocks[pair])
            if cur is not None:
                if trade2 is None:
                    cur.update(exit_ts=ts, exit_ri=ri, status=status, pnl=float(profit))
                    out.append(cur)
                    del open_tr[pair]
                else:
                    cur["t"] = trade2
                continue
            if trade2 is not None:
                open_tr[pair] = {"pair": pair, "entry_ts": ts, "entry_ri": ri, "dir": trade2["direction"],
                                 "E": trade2["entry_price"], "SL": trade2["sl"], "R": trade2["risk"],
                                 "date": row["Date"], "t": trade2}
                new_entries += 1
    CUR["mode"] = "pdh"
    return out


def stage1_rows(trades, data):
    rows = []
    for t in trades:
        recs = data[t["pair"]][0]
        nxt = recs[t["entry_ri"] + 1]["Open"] if t["entry_ri"] + 1 < len(recs) else t["E"]
        sgn = 1 if t["dir"] == "BUY" else -1
        r_usdt = rd.POSITION_USDT / t["E"] * t["R"]
        paper_R = t["pnl"] / r_usdt
        pct = t["pnl"] / rd.POSITION_USDT - sgn * (nxt - t["E"]) / t["E"] - bl.FEE_RT
        rows.append({"entry": t["entry_ts"], "paperR": paper_R, "usdt": bl.LIVE_NOTIONAL * pct,
                     "R": paper_R - sgn * (nxt - t["E"]) / t["R"] - bl.FEE_RT * t["E"] / t["R"]})
    return rows


def summ(rows, key):
    if not rows:
        return dict(n=0, win=0, net=0, pf=0, dd=0, h1=0, h2=0)
    x = pd.DataFrame(rows).sort_values("entry")
    s = x[key]
    w, l = s[s > 0], s[s < 0]
    cum = s.cumsum()
    h = len(s) // 2
    return dict(n=len(s), win=round(100 * len(w) / len(s), 1), net=round(s.sum(), 2),
                pf=round(w.sum() / -l.sum(), 2) if len(l) else 99, dd=round((cum - cum.cummax()).min(), 2),
                h1=round(s.iloc[:h].sum(), 2), h2=round(s.iloc[h:].sum(), 2))


def main():
    os.makedirs(OUT, exist_ok=True)
    order_all = [p["pair"] for p in rd.fetch_liquid_pairs()]
    data = {}
    with ThreadPoolExecutor(max_workers=bl.WORKERS) as ex:
        for k, (pair, res) in enumerate(ex.map(bs.load_pair, order_all), 1):
            if res is not None:
                add_rolling(res[0])
                data[pair] = res
            if k % 20 == 0:
                print(f"  15m loaded {k}/{len(order_all)}", flush=True)
    order = [p for p in order_all if p in data]
    t0 = min(data[p][0][0]["Timestamp"] for p in order)
    t1 = max(data[p][0][-1]["Timestamp"] for p in order)
    print(f"{len(order)} coins, {t0} -> {t1}", flush=True)

    res = {}
    MODES = [("pdh", "PDH/PDL (now)")] + [(f"roll{n}", f"Rolling last {n} candles") for n in LEVELS]
    for mode, name in MODES:
        tr = [t for t in paper_trades(data, order, mode) if "exit_ts" in t]
        res[mode] = (name, tr)
        print(f"{name}: {len(tr)} trades", flush=True)

    lines = [f"PDH/PDL vs rolling N-candle levels {LEVELS}. {len(order)} coins, data {t0} -> {t1}. "
             f"Real rd_trading logic + caps (5 open). Live estimate = next-open fill + 0.118% fees, 20 USDT/trade.", "",
             f"### Stage 1 - {DAYS} days (15m)", "",
             "| Level | Trades | Paper win % | Paper net R | Live-est net R | Live-est USDT @20 | PF (USDT) | Max DD USDT | 1st half USDT | 2nd half USDT |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for mode, _ in MODES:
        name, tr = res[mode]
        rows = stage1_rows(tr, data)
        p, r, u = summ(rows, "paperR"), summ(rows, "R"), summ(rows, "usdt")
        lines.append(f"| {name} | {p['n']} | {p['win']} | {p['net']} | {r['net']} | {u['net']} | {u['pf']} | {u['dd']} | {u['h1']} | {u['h2']} |")
        # month by month
        x = pd.DataFrame(rows)
        x["m"] = pd.to_datetime(x["entry"]).dt.to_period("M")
        lines.append("|  by month: " + ", ".join(f"{k}: {v:.1f}" for k, v in x.groupby("m")["usdt"].sum().items()) + " | | | | | | | | | |")

    # stage 2: live simulation on 1m, last LIVE_DAYS
    cut = t1 - pd.Timedelta(days=LIVE_DAYS)
    lines += ["", f"### Stage 2 - last {LIVE_DAYS} days, live simulation on 1m (fast lock, 0.75R guard, fees)", "",
              "| Level | Live trades | Skipped late | Win % | Net R | Net USDT @20 | PF | Max DD R |", "|---|---|---|---|---|---|---|---|"]
    for mode, _ in MODES:
        name, tr = res[mode]
        recent = [t for t in tr if t["entry_ts"] >= cut]
        with ThreadPoolExecutor(max_workers=bl.WORKERS) as ex:
            m1s = list(ex.map(bl.load_1m, recent))
        rows, skipped = [], 0
        for t, m in zip(recent, m1s):
            if m is None or len(m) < 2:
                continue
            CUR["mode"], CUR["row"] = ("roll" if mode.startswith("roll") else "pdh"), None
            recs, daily = data[t["pair"]]
            r = bl.sim_live(t, recs, daily, m, "FAST", 0.75)
            if r is None:
                skipped += 1
                continue
            rows.append(r)
        CUR["mode"] = "pdh"
        s, su = bl.stats(rows, "R"), bl.stats(rows, "usdt")
        lines.append(f"| {name} | {s['n']} | {skipped} | {s['win']} | {s['net']} | {su['net']} | {su['pf']} | {s['dd']} |")
        print(f"stage2 {name}: {su['net']} USDT", flush=True)
    text = "\n".join(lines) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print("\n=== RESULT ===\n" + text, flush=True)


if __name__ == "__main__":
    main()
