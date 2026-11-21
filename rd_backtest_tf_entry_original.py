"""Same entry-timeframe test (1m/2m/5m/10m/15m) on the ORIGINAL mother strategy rules:
no max distance from PDH/PDL, min SL 0.3%, loss exit only after 15m MACD confirm (no loss cap),
lock 1:2/1:3/1:4, full exit 1:5. See rd_backtest_tf_entry.py for the engine."""
import os
os.environ["BT_ORIGINAL"] = "1"
import rd_backtest_tf_entry as t  # noqa: E402

if __name__ == "__main__":
    t.main()
