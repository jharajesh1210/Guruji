"""Same strategy ONLY on BTC (B-BTC_USDT), 180 days. Uses the rd_backtest_gold2 grid engine:
mother-like baseline (15m, candle SL, MACD wait, 1:5 ladder) + variants (1h, ATR SL, hard SL, targets, Asian off).
Min SL 0.3% like current live. Next-open fill, fees 0.118%, 20 USDT/trade."""
import os
os.environ["BT_GOLD_PAIRS"] = os.getenv("BT_BTC_PAIRS", "B-BTC_USDT")
os.environ.setdefault("BT_DAYS", "180")
import rd_backtest_gold2 as g  # noqa: E402
g.MIN_RISK = 0.003

if __name__ == "__main__":
    g.main()
