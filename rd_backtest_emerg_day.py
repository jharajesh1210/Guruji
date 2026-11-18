"""
One day check: today's trades (from 05:30 IST) with emergency stop 7% (now) vs 15%.
Same live-style engine as rd_backtest_emerg.py, per-trade output.
"""
import os
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

import rd_trading as rd
import rd_backtest_stops as bs
import rd_backtest_live as bl

OUT = os.getenv("BT_OUT", "backtest_out")
DAY_START = pd.Timestamp(os.getenv("BT_DAY_START", "2026-11-18 05:30"))


def main():
    os.makedirs(OUT, exist_ok=True)
    order_all = [p["pair"] for p in rd.fetch_liquid_pairs()]
    data = {}
    with ThreadPoolExecutor(max_workers=bl.WORKERS) as ex:
        for pair, res in ex.map(bs.load_pair, order_all):
            if res is not None:
                data[pair] = res
    order = [p for p in order_all if p in data]
    trades = [t for t in bl.paper_trades(data, order) if t["entry_ts"] >= DAY_START]
    for t in trades:
        if "exit_ts" not in t:          # still open: simulate up to now
            t["exit_ts"] = data[t["pair"]][0][-1]["Timestamp"]
    with ThreadPoolExecutor(max_workers=bl.WORKERS) as ex:
        m1s = list(ex.map(bl.load_1m, trades))
    rows = []
    for t, m in zip(trades, m1s):
        if m is None or len(m) < 2:
            continue
        recs, daily = data[t["pair"]]
        out = {}
        for e in (0.07, 0.15):
            bl.EMERG = e
            out[e] = bl.sim_live(t, recs, daily, m, "FAST", 0.75)
        if out[0.07] is None:
            continue
        rows.append({"time": t["entry_ts"], "pair": t["pair"], "dir": t["dir"],
                     "exit7": out[0.07]["exit"], "usdt7": round(out[0.07]["usdt"], 3),
                     "exit15": out[0.15]["exit"], "usdt15": round(out[0.15]["usdt"], 3)})
    x = pd.DataFrame(rows).sort_values("time")
    diff = x[(x.exit7 != x.exit15) | ((x.usdt7 - x.usdt15).abs() > 1e-9)]
    lines = [f"Today from {DAY_START}: {len(x)} live-style trades (20 USDT/trade, fees incl.).", "",
             f"| Total | 7% stop (now) | 15% stop |", "|---|---|---|",
             f"| Net USDT | {x.usdt7.sum():.2f} | {x.usdt15.sum():.2f} |",
             f"| Emergency exits | {(x.exit7 == 'emergency_7pct').sum()} | {(x.exit15 == 'emergency_7pct').sum()} |", "",
             "Trades where the result differs:", "",
             "| Entry (IST) | Coin | Dir | Exit with 7% | USDT | Exit with 15% | USDT |", "|---|---|---|---|---|---|---|"]
    for r in diff.itertuples():
        lines.append(f"| {r.time} | {r.pair} | {r.dir} | {r.exit7} | {r.usdt7} | {r.exit15} | {r.usdt15} |")
    lines += ["", "All trades:", "", "| Entry | Coin | Dir | Exit 7% | USDT 7% | Exit 15% | USDT 15% |", "|---|---|---|---|---|---|---|"]
    for r in x.itertuples():
        lines.append(f"| {r.time} | {r.pair} | {r.dir} | {r.exit7} | {r.usdt7} | {r.exit15} | {r.usdt15} |")
    text = "\n".join(lines) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print("\n=== RESULT ===\n" + text, flush=True)


if __name__ == "__main__":
    main()
