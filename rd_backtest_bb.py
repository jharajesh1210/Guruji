"""
Bollinger Band (20, 2, 15m) rules on top of the current live rules (v2, live = paper, 1 USDT x 10x):
  NONE        : now
  OUTSIDE     : BUY only if close > upper band  (SELL: close < lower band)   - strong momentum breakout
  INSIDE      : BUY only if close <= upper band (SELL: close >= lower band)  - not over-extended
  SQUEEZE     : breakout only if band width before it was narrow (width < its 100-candle median)
  MID_TREND   : BUY only if close > middle band and middle band rising (SELL mirror)
  SQUEEZE+OUT : squeeze and close outside the band
90 days, all coins, caps 5 open; live-style on 1m: real fill, candle-close locks, loss cap 1.5R/3%, fees, 10 USDT/trade.
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
F = {"mode": "NONE"}
_step = rd.step_candle


def blocked(row, d):
    m = F["mode"]
    if m == "NONE":
        return False
    c, up, lo, mid, slope, sq = (row.get(k) for k in ("Close", "BBU", "BBL", "BBM", "BBM_SLOPE", "SQUEEZE"))
    if up != up:
        return True
    out = (c > up) if d == "BUY" else (c < lo)
    if m == "OUTSIDE":
        return not out
    if m == "INSIDE":
        return out
    if m == "SQUEEZE":
        return not sq
    if m == "MID_TREND":
        return not ((c > mid and slope > 0) if d == "BUY" else (c < mid and slope < 0))
    if m == "SQUEEZE+OUT":
        return not (sq and out)
    return False


def step_f(row, trade, *a, **k):
    out = _step(row, trade, *a, **k)
    if trade is None and out[2] is not None and blocked(row, out[2]["direction"]):
        return "Entry_Skipped_BB", 0.0, None, out[3], out[4]
    return out


rd.step_candle = step_f


def add_bb(recs):
    c = pd.Series([r["Close"] for r in recs])
    mid = c.rolling(20).mean()
    sd = c.rolling(20).std(ddof=0)
    up, lo = mid + 2 * sd, mid - 2 * sd
    width = (up - lo) / mid
    prevw = width.shift(1)
    sq = prevw < prevw.rolling(100, min_periods=50).median()
    slope = mid.diff(3)
    for r, a, b, m, s, q in zip(recs, up.values, lo.values, mid.values, slope.values, sq.values):
        r["BBU"], r["BBL"], r["BBM"], r["BBM_SLOPE"], r["SQUEEZE"] = a, b, m, s, bool(q)


def main():
    os.makedirs(OUT, exist_ok=True)
    order_all = [p["pair"] for p in rd.fetch_liquid_pairs()]
    data = {}
    with ThreadPoolExecutor(max_workers=bl.WORKERS) as ex:
        for pair, res in ex.map(bs.load_pair, order_all):
            if res is not None:
                add_bb(res[0])
                data[pair] = res
    order = [p for p in order_all if p in data]
    modes = ["NONE", "OUTSIDE", "INSIDE", "SQUEEZE", "MID_TREND", "SQUEEZE+OUT"]
    trs = {}
    for m in modes:
        F["mode"] = m
        trs[m] = [t for t in bl.paper_trades(data, order) if "exit_ts" in t]
        print(m, len(trs[m]), flush=True)
    F["mode"] = "NONE"
    allt = {(t["pair"], t["entry_ts"], t["exit_ts"]): t for v in trs.values() for t in v}
    cache = {}
    need = list(allt.values())
    with ThreadPoolExecutor(max_workers=8) as ex:
        for t, m1 in zip(need, ex.map(bl.load_1m, need)):
            cache[(t["pair"], t["entry_ts"], t["exit_ts"])] = m1
    lines = [f"Bollinger rules on PDH/PDL breakout (v2 live rules), {len(order)} coins, {DAYS} days, live-style 1m, 10 USDT/trade, fees.", "",
             "| Rule | Trades | Win % | Net R | Net USDT @10 | PF | Max DD | 1st half | 2nd half | by month |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for m in modes:
        rows = []
        for t in trs[m]:
            m1 = cache.get((t["pair"], t["entry_ts"], t["exit_ts"]))
            if m1 is None or len(m1) < 2:
                continue
            recs, daily = data[t["pair"]]
            r = bl.sim_live(t, recs, daily, m1, "CANDLE", None, cap_r=rd.LOSS_CAP_R, cap_pct=rd.LOSS_CAP_PCT / 100)
            if r is not None:
                rows.append(r)
        if not rows:
            lines.append(f"| {m} | 0 |")
            continue
        s, u = bl.stats(rows, "R"), bl.stats(rows, "usdt")
        x = pd.DataFrame(rows).sort_values("entry_ts")
        x["mo"] = pd.to_datetime(x["entry_ts"]).dt.to_period("M")
        h = len(x) // 2
        bym = ", ".join(f"{k}: {v:.1f}" for k, v in x.groupby("mo")["usdt"].sum().items())
        lines.append(f"| {m}{' (now)' if m == 'NONE' else ''} | {s['n']} | {s['win']} | {s['net']} | {u['net']} | {u['pf']} | {u['dd']} | "
                     f"{x['usdt'].iloc[:h].sum():.1f} | {x['usdt'].iloc[h:].sum():.1f} | {bym} |")
        print(lines[-1], flush=True)
    text = "\n".join(lines) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print("\n=== RESULT ===\n" + text, flush=True)


if __name__ == "__main__":
    main()
