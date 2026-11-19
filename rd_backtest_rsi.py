"""
RSI filter on LONG entries: skip a BUY if RSI(14) on the 15m signal candle is above 70 / 75 / 80.
SELL entries unchanged. Everything else = current logic (real rd_trading.step_candle, PDH/PDL,
caps 5 open, min SL 0.3%).
Stage 1: 90 days on 15m (paper + live estimate: next-open fill, fees 0.118%, 20 USDT/trade).
Stage 2: last 30 days live simulation on 1m (fast lock, 0.75R guard, 15% emergency stop, fees).
"""
import os
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

import rd_trading as rd
import rd_backtest_stops as bs
import rd_backtest_live as bl
import rd_backtest_level96 as lv

OUT = os.getenv("BT_OUT", "backtest_out")
DAYS = int(os.getenv("BT_DAYS", "90"))
LIVE_DAYS = 30
bs.DAYS = DAYS + 2
bl.EMERG = 0.15
CASES = [(None, "No RSI filter (now)"), (80, "Skip BUY if RSI > 80"), (75, "Skip BUY if RSI > 75"),
         (70, "Skip BUY if RSI > 70")]
F = {"thr": None, "skipped": 0}
_step = rd.step_candle


def step_rsi(row, trade, *a, **k):
    out = _step(row, trade, *a, **k)
    status, profit, trade2, pdh, pdl = out
    if (trade is None and trade2 is not None and F["thr"] is not None and trade2["direction"] == "BUY"
            and row.get("RSI", 0) == row.get("RSI", 0) and row.get("RSI", 0) > F["thr"]):
        F["skipped"] += 1
        return "Entry_Skipped_RSI", 0.0, None, pdh, pdl
    return out


rd.step_candle = step_rsi


def add_rsi(recs, n=14):
    c = pd.Series([r["Close"] for r in recs])
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rsi = (100 - 100 / (1 + up / dn)).values
    for r, v in zip(recs, rsi):
        r["RSI"] = v


def main():
    os.makedirs(OUT, exist_ok=True)
    order_all = [p["pair"] for p in rd.fetch_liquid_pairs()]
    data = {}
    with ThreadPoolExecutor(max_workers=bl.WORKERS) as ex:
        for pair, res in ex.map(bs.load_pair, order_all):
            if res is not None:
                add_rsi(res[0])
                data[pair] = res
    order = [p for p in order_all if p in data]
    t1 = max(data[p][0][-1]["Timestamp"] for p in order)
    t0 = min(data[p][0][0]["Timestamp"] for p in order)
    lines = [f"RSI filter on LONG entries. {len(order)} coins, {t0} -> {t1}. Real rd_trading logic + caps.", "",
             f"### Stage 1 - {DAYS} days (15m), live estimate 20 USDT/trade after fees", "",
             "| Case | Trades | BUY skipped | Paper win % | Live-est net R | Live-est USDT | PF | Max DD USDT | 1st half | 2nd half |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    res = {}
    for thr, name in CASES:
        F["thr"], F["skipped"] = thr, 0
        tr = [t for t in lv.paper_trades(data, order, "pdh") if "exit_ts" in t]
        res[thr] = (name, tr, F["skipped"])
        rows = lv.stage1_rows(tr, data)
        p, r, u = lv.summ(rows, "paperR"), lv.summ(rows, "R"), lv.summ(rows, "usdt")
        buys = [x for x, t in zip(rows, tr) if t["dir"] == "BUY"]
        lines.append(f"| {name} | {p['n']} | {F['skipped']} | {p['win']} | {r['net']} | {u['net']} | {u['pf']} | {u['dd']} | {u['h1']} | {u['h2']} |")
        lines.append(f"|   BUY trades only: {len(buys)} trades, {sum(b['usdt'] for b in buys):.1f} USDT | | | | | | | | | |")
        x = pd.DataFrame(rows)
        x["m"] = pd.to_datetime(x["entry"]).dt.to_period("M")
        lines.append("|   by month: " + ", ".join(f"{k}: {v:.1f}" for k, v in x.groupby("m")["usdt"].sum().items()) + " | | | | | | | | | |")
        print(lines[-3], flush=True)
    F["thr"] = None
    cut = t1 - pd.Timedelta(days=LIVE_DAYS)
    lines += ["", f"### Stage 2 - last {LIVE_DAYS} days, live simulation on 1m (fast lock, 0.75R, 15% emergency, fees)", "",
              "| Case | Live trades | Win % | Net R | Net USDT @20 | PF | Max DD R |", "|---|---|---|---|---|---|---|"]
    cache = {}
    for thr, _ in CASES:
        name, tr, _s = res[thr]
        recent = [t for t in tr if t["entry_ts"] >= cut]
        need = [t for t in recent if (t["pair"], t["entry_ts"]) not in cache]
        with ThreadPoolExecutor(max_workers=bl.WORKERS) as ex:
            for t, m in zip(need, ex.map(bl.load_1m, need)):
                cache[(t["pair"], t["entry_ts"])] = m
        rows = []
        for t in recent:
            m = cache.get((t["pair"], t["entry_ts"]))
            if m is None or len(m) < 2:
                continue
            recs, daily = data[t["pair"]]
            r = bl.sim_live(t, recs, daily, m, "FAST", 0.75)
            if r is not None:
                rows.append(r)
        s, su = bl.stats(rows, "R"), bl.stats(rows, "usdt")
        lines.append(f"| {name} | {s['n']} | {s['win']} | {s['net']} | {su['net']} | {su['pf']} | {s['dd']} |")
    text = "\n".join(lines) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print("\n=== RESULT ===\n" + text, flush=True)


if __name__ == "__main__":
    main()
