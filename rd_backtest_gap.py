"""
WHY paper profit but live loss?  Step-by-step "waterfall" on the same trades (last BT_DAYS days, 1m data),
plus the fixes to test.  All numbers in USDT at 25 USDT per trade (live: 5 USDT x 5x) and in R.

Waterfall (each step adds ONE real-life effect to the previous one):
  P1 paper as it books today (loss capped at 100 USDT on 3000 = 3.33%)
  P2 paper WITHOUT the loss cap (live has no cap)
  L1 live engine, ideal: fill = paper price, no fees, no emergency stop (lock timing = FAST, like live)
  L2 + 15% emergency stop (now)
  L3 + real fill (price ~1 minute after the candle close, 0.75R late-entry guard)
  L4 + fees 0.118%  = today's live
Fixes tested on top of L4 (all with fees, real fill, 15% stop):
  lock one step behind (lag 1R), half step (lag 0.5R), breakeven at 1:2 then lag 1, candle-close lock,
  and min SL 0.5% / 0.75% (fees eat less of wider trades).
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

import rd_trading as rd
import rd_backtest_stops as bs
import rd_backtest_live as bl

OUT = os.getenv("BT_OUT", "backtest_out")
SIZE = 25.0
bl.LIVE_NOTIONAL = SIZE


def main():
    os.makedirs(OUT, exist_ok=True)
    order_all = [p["pair"] for p in rd.fetch_liquid_pairs()]
    data = {}
    with ThreadPoolExecutor(max_workers=bl.WORKERS) as ex:
        for pair, res in ex.map(bs.load_pair, order_all):
            if res is not None:
                data[pair] = res
    order = [p for p in order_all if p in data]

    def paper(cap):
        rd.MAX_LOSS_USDT = cap
        return [t for t in bl.paper_trades(data, order) if "exit_ts" in t]

    tr_cap = paper(100.0)
    tr_nocap = paper(1e9)
    rd.MAX_LOSS_USDT = 100.0
    with ThreadPoolExecutor(max_workers=bl.WORKERS) as ex:
        m1s = list(ex.map(bl.load_1m, tr_nocap))
    use = [(t, m) for t, m in zip(tr_nocap, m1s) if m is not None and len(m) >= 2]
    keys = {(t["pair"], t["entry_ts"]) for t, _ in use}
    t0, t1 = min(t["entry_ts"] for t, _ in use), max(t["exit_ts"] for t, _ in use)

    def paper_rows(trs):
        rows = []
        for t in trs:
            if (t["pair"], t["entry_ts"]) not in keys:
                continue
            r_usdt = rd.POSITION_USDT / t["E"] * t["R"]
            rows.append({"R": t["pnl"] / r_usdt, "usdt": SIZE / rd.POSITION_USDT * t["pnl"], "exit": t["status"]})
        return rows

    def live(mode="FAST", ideal=False, fees=True, emerg=0.15, guard=0.75, lag=0.0, be=False, min_risk=None):
        bl.EMERG = emerg
        rows = []
        for t, m in use:
            if min_risk is not None and t["R"] < t["E"] * min_risk:
                continue
            recs, daily = data[t["pair"]]
            r = bl.sim_live(t, recs, daily, m, mode, guard, ideal_fill=ideal, fees=fees, lock_lag=lag, be=be)
            if r is not None:
                rows.append(r)
        return rows

    def row(name, rows):
        s, u = bl.stats(rows, "R"), bl.stats(rows, "usdt")
        x = pd.DataFrame(rows)
        h = len(x) // 2
        return (f"| {name} | {s['n']} | {s['win']} | {s['net']} | {u['net']} | {u['pf']} | {u['dd']} | "
                f"{x['usdt'].iloc[:h].sum():.2f} | {x['usdt'].iloc[h:].sum():.2f} |")

    head = ["| Step | Trades | Win % | Net R | Net USDT @25 | PF | Max DD USDT | 1st half | 2nd half |",
            "|---|---|---|---|---|---|---|---|---|"]
    L4 = live()
    lines = [f"Paper vs live gap. {len(order)} coins, {len(use)} paper trades with 1m data, {t0} -> {t1}. "
             f"Size {SIZE:g} USDT per trade.", "", "### Waterfall (paper -> live, one effect at a time)", ""] + head + [
        row("P1 paper as booked (loss cap 100)", paper_rows(tr_cap)),
        row("P2 paper, NO loss cap", paper_rows(tr_nocap)),
        row("L1 live engine ideal (paper fill, no fees, no emergency)", live(ideal=True, fees=False, emerg=0.99, guard=None)),
        row("L2 + 15% emergency stop", live(ideal=True, fees=False, emerg=0.15, guard=None)),
        row("L3 + real fill + 0.75R guard", live(fees=False)),
        row("L4 + fees = TODAY'S LIVE", L4),
    ]
    ex = pd.DataFrame(L4)
    lines += ["", "Exit types in today's live (L4):", "", "| Exit | Trades | Net USDT @25 |", "|---|---|---|"]
    for k, g in ex.groupby(ex["exit"].str.replace(r"_1:.*", "", regex=True)):
        lines.append(f"| {k} | {len(g)} | {g['usdt'].sum():.2f} |")
    pp = pd.DataFrame(paper_rows(tr_cap))
    lines += ["", "Exit types in paper (P1):", "", "| Exit | Trades | Net USDT @25 |", "|---|---|---|"]
    for k, g in pp.groupby(pp["exit"].str.replace(r"_1:.*", "", regex=True)):
        lines.append(f"| {k} | {len(g)} | {g['usdt'].sum():.2f} |")
    lines += ["", "### Fixes (each on top of today's live L4)", ""] + head + [
        row("L4 today's live", L4),
        row("Lock one step behind (1:2->SL 1:1, 1:3->1:2, 1:4->1:3)", live(lag=1.0)),
        row("Lock half step behind (1:2->SL 1:1.5 ...)", live(lag=0.5)),
        row("1:2 -> breakeven, then one step behind", live(lag=1.0, be=True)),
        row("Lock on 15m candle close (like paper)", live(mode="CANDLE")),
        row("Min SL 0.5% (skip tighter trades)", live(min_risk=0.005)),
        row("Min SL 0.75%", live(min_risk=0.0075)),
        row("Lock half step behind + min SL 0.5%", live(lag=0.5, min_risk=0.005)),
        row("Lock one step behind + min SL 0.5%", live(lag=1.0, min_risk=0.005)),
    ]
    text = "\n".join(lines) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print("\n=== RESULT ===\n" + text, flush=True)


if __name__ == "__main__":
    main()
