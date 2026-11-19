"""
Breakout level: PDH/PDL (previous trading day, 96 x 15m candles - now) vs the high/low of the
last N 15m candles (rolling, current candle excluded): N = 24 (6h), 40 (10h), 60 (15h), 96 (24h rolling).
Everything else like mother: 15m entry, volume > 1.5x, wick < 40%, SL 0.3-2%, MACD 15m loss exit,
lock 1:2/1:3/1:4, 1:5 exit. Fill at next candle open, fees 0.118%, 20 USDT notional, one trade per coin.
"""
import os
from concurrent.futures import ThreadPoolExecutor

import rd_trading as rd
import rd_backtest_gold2 as g

DAYS = int(os.getenv("BT_DAYS", "30"))
OUT = os.getenv("BT_OUT", "backtest_out")
g.DAYS = DAYS + 2
g.MIN_RISK, g.MAX_RISK = rd.MIN_RISK_PCT / 100, 0.02
VARIANTS = ["PDH/PDL"] + [int(x) for x in os.getenv("BT_LOOKBACKS", "24,40,60,96").split(",") if x.strip()]


def run(pair):
    d = g.load(pair)
    if d.empty or len(d) < 96 * 4:
        return pair, None
    base = g.prep(d)
    res = {}
    for v in VARIANTS:
        p = base.copy()
        if v != "PDH/PDL":
            p["PDH"] = p["High"].shift(1).rolling(v, min_periods=v).max()
            p["PDL"] = p["Low"].shift(1).rolling(v, min_periods=v).min()
        p = p[p["Timestamp"] >= p["Timestamp"].iloc[0] + __import__("pandas").Timedelta(days=2)].reset_index(drop=True)
        res[v] = g.simulate(p, "candle", "macd", "ladder5", True)
    return pair, res


def main():
    os.makedirs(OUT, exist_ok=True)
    pairs = [p["pair"] for p in rd.fetch_liquid_pairs()]
    allr = {v: [] for v in VARIANTS}
    used = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for k, (pair, res) in enumerate(ex.map(run, pairs), 1):
            if res:
                used += 1
                for v in VARIANTS:
                    allr[v] += res[v]
            if k % 20 == 0:
                print(f"  {k}/{len(pairs)}", flush=True)
    lines = [f"Breakout level backtest, {used} coins, last {DAYS} days, fees 0.118%, 20 USDT/trade.", "",
             "| Level | Trades | Win % | Net R | Net USDT @20 | PF | Max DD USDT | 1st half | 2nd half |",
             "|---|---|---|---|---|---|---|---|---|"]
    for v in VARIANTS:
        s = g.summ(allr[v])
        name = "PDH/PDL (now, previous day)" if v == "PDH/PDL" else f"last {v} candles ({v/4:g}h)"
        lines.append(f"| {name} | {s['n']} | {s['win']} | {s['R']} | {s['usdt']} | {s['pf']} | {s['dd']} | {s['h1']} | {s['h2']} |")
    text = "\n".join(lines) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print("\n=== RESULT ===\n" + text, flush=True)


if __name__ == "__main__":
    main()
