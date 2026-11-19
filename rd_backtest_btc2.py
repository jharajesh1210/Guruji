"""BTC: what change could make it profitable? 180 days, B-BTC_USDT.
 A) breakout grid (mother family): timeframe 15m/1h/4h x SL candle/ATR x MACD/hard x target x Asian on/off
 B) FADE grid (failed PDH/PDL break, trade back into the range) on 15m/1h/4h
Both: next-open fill, fees 0.118%, 20 USDT/trade, min SL 0.3%. 'robust' = both halves positive."""
import os
os.environ["BT_GOLD_PAIRS"] = "B-BTC_USDT"
os.environ.setdefault("BT_DAYS", "180")
os.environ["BT_TFS"] = "15m,1h,4h"
OUT = os.getenv("BT_OUT", "backtest_out")
import rd_backtest_gold2 as g          # noqa: E402
import rd_backtest_gold_fade as f      # noqa: E402
g.MIN_RISK = 0.003

if __name__ == "__main__":
    parts = []
    for name, mod in (("A) BREAKOUT grid", g), ("B) FADE grid", f)):
        mod.OUT = os.path.join(OUT, name[0])
        mod.main()
        parts.append(f"## {name}\n\n" + open(os.path.join(mod.OUT, "backtest_results.md"), encoding="utf-8").read())
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as fh:
        fh.write("\n\n".join(parts))
