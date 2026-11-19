"""
90-day check of the two proposed changes (nothing is changed in live/paper by this script):
 (1) paper loss cap 100 USDT -> no cap  (paper PnL becomes honest; trades unchanged)
 (2) new lock ladder: 1:2 -> SL to entry (breakeven), 1:3 -> SL 1:2, 1:4 -> SL 1:3, 1:5 exit
     vs today's ladder (1:2 -> SL 1:2, 1:3 -> 1:3, 1:4 -> 1:4) and 'one step behind' (1:2 -> 1:1 ...).
Live-style on 1m data: real fill, 0.75R guard, FAST lock, 15% emergency, fees 0.118%, 25 USDT per trade.
Results per month to check consistency.
"""
import os
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

import rd_trading as rd
import rd_backtest_stops as bs
import rd_backtest_live as bl

OUT = os.getenv("BT_OUT", "backtest_out")
DAYS = int(os.getenv("BT_DAYS", "90"))
bs.DAYS = DAYS + 2
SIZE = 25.0
bl.LIVE_NOTIONAL = SIZE
bl.EMERG = 0.15


def main():
    os.makedirs(OUT, exist_ok=True)
    order_all = [p["pair"] for p in rd.fetch_liquid_pairs()]
    data = {}
    with ThreadPoolExecutor(max_workers=bl.WORKERS) as ex:
        for pair, res in ex.map(bs.load_pair, order_all):
            if res is not None:
                data[pair] = res
    order = [p for p in order_all if p in data]
    rd.MAX_LOSS_USDT = 100.0
    tr_cap = [t for t in bl.paper_trades(data, order) if "exit_ts" in t]
    rd.MAX_LOSS_USDT = 1e9
    tr = [t for t in bl.paper_trades(data, order) if "exit_ts" in t]
    rd.MAX_LOSS_USDT = 100.0
    print(f"{len(order)} coins, paper trades {len(tr)}", flush=True)
    m1s = []
    with ThreadPoolExecutor(max_workers=bl.WORKERS) as ex:
        for k, m in enumerate(ex.map(bl.load_1m, tr), 1):
            m1s.append(m)
            if k % 300 == 0:
                print(f"  1m {k}/{len(tr)}", flush=True)
    use = [(t, m) for t, m in zip(tr, m1s) if m is not None and len(m) >= 2]
    keys = {(t["pair"], t["entry_ts"]) for t, _ in use}
    t0 = min(t["entry_ts"] for t, _ in use)
    t1 = max(t["exit_ts"] for t, _ in use)

    def prow(trs):
        out = []
        for t in trs:
            if (t["pair"], t["entry_ts"]) in keys:
                out.append({"entry_ts": t["entry_ts"], "usdt": SIZE / rd.POSITION_USDT * t["pnl"],
                            "R": t["pnl"] / (rd.POSITION_USDT / t["E"] * t["R"])})
        return out

    def live(lag=0.0, be=False):
        rows = []
        for t, m in use:
            recs, daily = data[t["pair"]]
            r = bl.sim_live(t, recs, daily, m, "FAST", 0.75, lock_lag=lag, be=be)
            if r is not None:
                rows.append(r)
        return rows

    cases = [("Paper as booked (loss cap 100)", prow(tr_cap)), ("Paper, NO loss cap (honest)", prow(tr)),
             ("LIVE today (1:2->SL 1:2 ...)", live()), ("LIVE new: 1:2->breakeven, 1:3->1:2, 1:4->1:3", live(1.0, True)),
             ("LIVE one step behind (1:2->1:1 ...)", live(1.0, False))]
    months = sorted({pd.Timestamp(r["entry_ts"]).to_period("M") for _, rows in cases for r in rows})
    lines = [f"90-day check. {len(order)} coins, {len(use)} paper trades with 1m data (of {len(tr)}), {t0} -> {t1}. "
             f"25 USDT/trade, fees + 15% emergency in LIVE rows.", "",
             "| Case | Trades | Win % | Net R | Net USDT @25 | PF | Max DD USDT | " + " | ".join(str(m) for m in months) + " |",
             "|---|---|---|---|---|---|---|" + "---|" * len(months)]
    for name, rows in cases:
        s, u = bl.stats(rows, "R"), bl.stats(rows, "usdt")
        x = pd.DataFrame(rows)
        x["m"] = pd.to_datetime(x["entry_ts"]).dt.to_period("M")
        bym = x.groupby("m")["usdt"].sum()
        lines.append(f"| {name} | {s['n']} | {s['win']} | {s['net']} | {u['net']} | {u['pf']} | {u['dd']} | "
                     + " | ".join(f"{bym.get(m, 0):.1f}" for m in months) + " |")
    text = "\n".join(lines) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print("\n=== RESULT ===\n" + text, flush=True)


if __name__ == "__main__":
    main()
