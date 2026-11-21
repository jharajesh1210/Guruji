"""
Breakout level test on the CURRENT live rules (v2 + Bollinger squeeze, live = paper, 1 USDT x 10x):
  PDH/PDL (now)          : previous trading day's high / low
  last 1h (4 candles)    : high / low of the previous 4 closed 15m candles (= last 1 hour)
  last 4h (16 candles)   : for context
Everything else unchanged. 90 days, all coins, caps 5 open; live-style on 1m: real fill, candle-close locks,
loss cap 1.5R/3%, fees, 10 USDT/trade.
"""
import os
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

os.environ["BT_LEVELS"] = "4,16"
import rd_trading as rd                 # noqa: E402
import rd_backtest_stops as bs          # noqa: E402
import rd_backtest_live as bl           # noqa: E402
import rd_backtest_level96 as lv        # noqa: E402  (patches rd.levels_for, rolling N-candle levels)

OUT = os.getenv("BT_OUT", "backtest_out")
DAYS = int(os.getenv("BT_DAYS", "90"))
bs.DAYS = DAYS + 2
bl.LIVE_NOTIONAL = 10.0
bl.EMERG = 0.15
rd.MAX_LOSS_USDT = 1e9


def main():
    os.makedirs(OUT, exist_ok=True)
    order_all = [p["pair"] for p in rd.fetch_liquid_pairs()]
    data = {}
    with ThreadPoolExecutor(max_workers=bl.WORKERS) as ex:
        for pair, res in ex.map(bs.load_pair, order_all):
            if res is not None:
                lv.add_rolling(res[0])
                data[pair] = res
    order = [p for p in order_all if p in data]
    modes = [("pdh", "PDH/PDL (now)"), ("roll4", "last 1h high/low (4 x 15m)"), ("roll16", "last 4h high/low (16 x 15m)")]
    trs = {}
    for m, name in modes:
        trs[m] = [t for t in lv.paper_trades(data, order, m) if "exit_ts" in t]
        print(name, len(trs[m]), flush=True)
    allt = {(t["pair"], t["entry_ts"], t["exit_ts"]): t for v in trs.values() for t in v}
    cache = {}
    need = list(allt.values())
    with ThreadPoolExecutor(max_workers=8) as ex:
        for t, m1 in zip(need, ex.map(bl.load_1m, need)):
            cache[(t["pair"], t["entry_ts"], t["exit_ts"])] = m1
    lines = [f"Breakout level test (current live rules incl. BB squeeze), {len(order)} coins, {DAYS} days, live-style 1m, 10 USDT/trade, fees.", "",
             "| Level | Trades | Win % | Net R | Net USDT @10 | PF | Max DD | 1st half | 2nd half | by month |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for m, name in modes:
        rows = []
        for t in trs[m]:
            m1 = cache.get((t["pair"], t["entry_ts"], t["exit_ts"]))
            if m1 is None or len(m1) < 2:
                continue
            lv.CUR["mode"], lv.CUR["row"] = "pdh", None
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
                     f"{x['usdt'].iloc[:h].sum():.1f} | {x['usdt'].iloc[h:].sum():.1f} | {bym} |")
        print(lines[-1], flush=True)
    text = "\n".join(lines) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print("\n=== RESULT ===\n" + text, flush=True)


if __name__ == "__main__":
    main()
