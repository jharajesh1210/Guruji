"""90-day test: PDH/PDL vs rolling 96 / 110 / 200 candles (same engine as rd_backtest_level96.py)."""
import os
os.environ.setdefault("BT_LEVELS", "96,110,200")
import rd_backtest_level96 as lv  # noqa: E402

if __name__ == "__main__":
    lv.main()
