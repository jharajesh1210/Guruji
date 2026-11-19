"""Lookback sweep: PDH/PDL vs rolling 96/100/105/110/120/150/200 candles (same engine as rd_backtest_lookback.py)."""
import os
os.environ.setdefault("BT_LOOKBACKS", "96,100,105,110,120,150,200")
import rd_backtest_lookback as lb  # noqa: E402

if __name__ == "__main__":
    lb.main()
