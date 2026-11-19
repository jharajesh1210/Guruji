"""
Gold (XAU, PAXG) FADE strategy backtest - the opposite of the breakout:
  SELL when a candle goes above the previous day's high (PDH) but CLOSES back below it,
  BUY  when a candle goes below the previous day's low (PDL) but CLOSES back above it.
SL = the sweep candle's high (SELL) / low (BUY), or 1 x ATR14. Hard SL (no MACD wait).
Grid: timeframe 15m|1h x SL candle|atr x target 1:1|1:1.5|1:2|1:3 x Asian on|off x volume filter on|off
      x rejection wick (>=40%) required|not.
Same execution as rd_backtest_gold2: next-bar-open fill, 0.118% fees, SL before profit inside a bar,
20 USDT notional, max hold 3 days. h1/h2 = first/second half of trades (consistency).
"""
import itertools
import os

import pandas as pd

import rd_backtest_gold2 as g

OUT = os.getenv("BT_OUT", "backtest_out")


def main():
    os.makedirs(OUT, exist_ok=True)
    data = {}
    for p in g.PAIRS:
        d15 = g.load(p)
        if d15.empty or len(d15) < 500:
            print(f"{p}: no data")
            continue
        data[p] = {tf: (g.prep(d15) if tf == "15m" else g.prep(g.to_1h(d15, tf))) for tf in g.TFS}
        print(f"{p}: {d15['Timestamp'].iloc[0]} -> {d15['Timestamp'].iloc[-1]}", flush=True)
    rows = []
    for tf, slm, tg, asian, vol, wick in itertools.product(g.TFS, ["candle", "atr"], ["t1", "t1.5", "t2", "t3"],
                                                           [True, False], [True, False], [True, False]):
        per, allt = {}, []
        for p in data:
            t = g.simulate(data[p][tf], slm, "hard", tg, asian, signal="fade", vol_req=vol, wick_req=wick)
            per[p] = g.summ(t)["usdt"]
            allt += t
        s = g.summ(allt)
        rows.append({"tf": tf, "sl": slm, "target": tg, "asian": "on" if asian else "off",
                     "vol": "on" if vol else "off", "wick": "on" if wick else "off", **s,
                     **{f"usdt_{p.split('-')[1].split('_')[0]}": v for p, v in per.items()}})
    res = pd.DataFrame(rows).sort_values("usdt", ascending=False)
    res.to_csv(os.path.join(OUT, "gold_fade_grid.csv"), index=False)
    cols = list(res.columns)

    def table(df):
        return ("| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
                + "\n".join("| " + " | ".join(str(v) for v in r) + " |" for r in df[cols].values))
    pos = res[(res.usdt > 0)]
    robust = res[(res.h1 > 0) & (res.h2 > 0) & (res.filter(like="usdt_").gt(0).all(axis=1))]
    text = (f"FADE grid on {list(data)}. {len(res)} combinations, {len(pos)} positive after fees, "
            f"{len(robust)} positive in BOTH halves AND on BOTH coins.\n\n### Top 15\n\n" + table(res.head(15))
            + "\n\n### Robust (both halves + both coins positive)\n\n" + (table(robust.head(15)) if len(robust) else "none")
            + "\n\n### Worst 5\n\n" + table(res.tail(5)) + "\n")
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text, flush=True)


if __name__ == "__main__":
    main()
