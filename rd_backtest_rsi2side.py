"""
RSI filter on BOTH sides, current live rules (rd_trading.py v2 + live = paper, 15m candle-close locks):
  skip BUY  if RSI(14, 15m) > 80
  skip SELL if RSI(14, 15m) < 30
compared with no filter (now). Also 75/25 and 85/15 for context.
90 days, all coins, caps 5 open. Live-style on 1m: real fill, CANDLE lock timing, loss cap 1.5R/3%, fees, 10 USDT/trade (1 USDT x 10x).
"""
import os
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

import rd_trading as rd
import rd_backtest_stops as bs
import rd_backtest_live as bl
import rd_backtest_rsi as rs        # add_rsi + patches rd.step_candle (BUY only) -> we replace that patch below

OUT = os.getenv("BT_OUT", "backtest_out")
DAYS = int(os.getenv("BT_DAYS", "90"))
bs.DAYS = DAYS + 2
SIZE = 10.0
bl.LIVE_NOTIONAL = SIZE
bl.EMERG = 0.15
rd.MAX_LOSS_USDT = 1e9
F = {"hi": None, "lo": None}
_step = rs._step                      # original rd.step_candle (before rd_backtest_rsi patched it)


def step_f(row, trade, *a, **k):
    out = _step(row, trade, *a, **k)
    st, p, t2, pdh, pdl = out
    if trade is None and t2 is not None and F["hi"] is not None:
        r = row.get("RSI", 50)
        if r == r and ((t2["direction"] == "BUY" and r > F["hi"]) or (t2["direction"] == "SELL" and r < F["lo"])):
            return "Entry_Skipped_RSI", 0.0, None, pdh, pdl
    return out


rd.step_candle = step_f


def main():
    os.makedirs(OUT, exist_ok=True)
    order_all = [p["pair"] for p in rd.fetch_liquid_pairs()]
    data = {}
    with ThreadPoolExecutor(max_workers=bl.WORKERS) as ex:
        for pair, res in ex.map(bs.load_pair, order_all):
            if res is not None:
                rs.add_rsi(res[0])
                data[pair] = res
    order = [p for p in order_all if p in data]
    cases = [(None, None, "No RSI filter (now)"), (80, 30, "BUY skip RSI>80, SELL skip RSI<30"),
             (75, 25, "BUY skip >75, SELL skip <25"), (85, 15, "BUY skip >85, SELL skip <15")]
    trs = {}
    for hi, lo, name in cases:
        F["hi"], F["lo"] = hi, lo
        trs[name] = [t for t in bl.paper_trades(data, order) if "exit_ts" in t]
        print(name, len(trs[name]), flush=True)
    F["hi"] = None
    allt = {(t["pair"], t["entry_ts"], t["exit_ts"]): t for v in trs.values() for t in v}
    cache = {}
    need = list(allt.values())
    with ThreadPoolExecutor(max_workers=8) as ex:
        for t, m1 in zip(need, ex.map(bl.load_1m, need)):
            cache[(t["pair"], t["entry_ts"], t["exit_ts"])] = m1
    lines = [f"RSI filter both sides, v2 live rules, {len(order)} coins, {DAYS} days, live-style 1m, 10 USDT/trade (1 USDT x 10x), fees.", "",
             "| Case | Trades | Win % | Net R | Net USDT @10 | PF | Max DD | 1st half | 2nd half | BUY USDT | SELL USDT | by month |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for hi, lo, name in cases:
        rows = []
        for t in trs[name]:
            m1 = cache.get((t["pair"], t["entry_ts"], t["exit_ts"]))
            if m1 is None or len(m1) < 2:
                continue
            recs, daily = data[t["pair"]]
            r = bl.sim_live(t, recs, daily, m1, "CANDLE", None, cap_r=rd.LOSS_CAP_R, cap_pct=rd.LOSS_CAP_PCT / 100)
            if r is not None:
                rows.append(r)
        if not rows:
            lines.append(f"| {name} | 0 |")
            continue
        s, u = bl.stats(rows, "R"), bl.stats(rows, "usdt")
        x = pd.DataFrame(rows).sort_values("entry_ts")
        x["mo"] = pd.to_datetime(x["entry_ts"]).dt.to_period("M")
        h = len(x) // 2
        bym = ", ".join(f"{k}: {v:.1f}" for k, v in x.groupby("mo")["usdt"].sum().items())
        lines.append(f"| {name} | {s['n']} | {s['win']} | {s['net']} | {u['net']} | {u['pf']} | {u['dd']} | "
                     f"{x['usdt'].iloc[:h].sum():.1f} | {x['usdt'].iloc[h:].sum():.1f} | "
                     f"{x[x.dir == 'BUY'].usdt.sum():.1f} | {x[x.dir == 'SELL'].usdt.sum():.1f} | {bym} |")
        print(lines[-1], flush=True)
    text = "\n".join(lines) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print("\n=== RESULT ===\n" + text, flush=True)


if __name__ == "__main__":
    main()
