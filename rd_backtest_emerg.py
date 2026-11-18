"""
Emergency stop width backtest (live-style, 1m data, last BT_DAYS days, all liquid coins).
Same engine as rd_backtest_live.py: paper trades from rd_trading logic (caps 5 open), live simulated on
1m candles with FAST lock, 0.75R late-entry guard, fees 0.118%, 20 USDT notional per trade.
Only the exchange emergency stop changes (it acts only while no profit is locked):
   7% (now, 10x)  | 10% (needs <= 8x) | 15% (needs <= 5x) | 20% (needs <= 4x) | none
Same notional in every case -> same USDT profit/loss per % move; only margin per trade differs
(20 USDT / leverage).
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

import rd_trading as rd
import rd_backtest_stops as bs
import rd_backtest_live as bl

OUT = os.getenv("BT_OUT", "backtest_out")
CASES = [(0.07, "7% (now, 10x, margin 2 USDT)"), (0.10, "10% (8x, margin 2.5 USDT)"),
         (0.15, "15% (5x, margin 4 USDT)"), (0.20, "20% (4x, margin 5 USDT)"), (0.99, "no emergency stop")]


def main():
    os.makedirs(OUT, exist_ok=True)
    order_all = [p["pair"] for p in rd.fetch_liquid_pairs()]
    data = {}
    with ThreadPoolExecutor(max_workers=bl.WORKERS) as ex:
        for pair, res in ex.map(bs.load_pair, order_all):
            if res is not None:
                data[pair] = res
    order = [p for p in order_all if p in data]
    trades = [t for t in bl.paper_trades(data, order) if "exit_ts" in t]
    print(f"{len(order)} coins, paper trades {len(trades)}", flush=True)
    with ThreadPoolExecutor(max_workers=bl.WORKERS) as ex:
        m1s = list(ex.map(bl.load_1m, trades))
    usable = [(t, m) for t, m in zip(trades, m1s) if m is not None and len(m) >= 2]
    if not usable:
        print("NO 1m DATA")
        sys.exit(1)
    t0 = min(t["entry_ts"] for t, _ in usable)
    t1 = max(t["exit_ts"] for t, _ in usable)
    lines = [f"Emergency stop backtest, {len(order)} coins, {len(usable)} paper trades with 1m data, {t0} -> {t1}.",
             "Live-style: FAST lock, 0.75R guard, fees 0.118%, 20 USDT notional/trade.", "",
             "| Emergency stop | Live trades | Win % | Net R | Net USDT @20 | PF | Max DD USDT | Emergency exits | Emergency USDT | 1st half USDT | 2nd half USDT |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for e, name in CASES:
        bl.EMERG = e
        rows = []
        for t, m in usable:
            recs, daily = data[t["pair"]]
            r = bl.sim_live(t, recs, daily, m, "FAST", 0.75)
            if r is not None:
                rows.append(r)
        x = pd.DataFrame(rows).sort_values("entry_ts")
        s, su = bl.stats(rows, "R"), bl.stats(rows, "usdt")
        em = x[x["exit"] == "emergency_7pct"]
        h = len(x) // 2
        lines.append(f"| {name} | {s['n']} | {s['win']} | {s['net']} | {su['net']} | {su['pf']} | {su['dd']} | "
                     f"{len(em)} | {em['usdt'].sum():.2f} | {x['usdt'].iloc[:h].sum():.2f} | {x['usdt'].iloc[h:].sum():.2f} |")
        print(lines[-1], flush=True)
        # worst single trade (risk view)
        lines.append(f"|   worst single trade: {x['usdt'].min():.2f} USDT ({x.loc[x['usdt'].idxmin(), 'pair']}) | | | | | | | | | | |")
    text = "\n".join(lines) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print("\n=== RESULT ===\n" + text, flush=True)


if __name__ == "__main__":
    main()
