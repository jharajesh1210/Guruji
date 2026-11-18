"""
PAPER ONLY test of the "rolling 96-candle level" idea (no live orders, no Telegram by default).

Exactly rd_trading.py (mother logic, imported unchanged) except the breakout level:
PDH/PDL (previous trading day) is replaced by the high/low of the LAST 96 closed 15m candles
(24 hours, updated every candle). Everything else is identical: volume, wick, SL 0.3-2%, MACD loss
exit, locks, 1:5, caps, profit block. Own state/signals files so it never touches the main paper.
Compare with:  python compare_paper96.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("RD_STATE_FILE", os.path.join(HERE, "rd_paper96_state.json"))
os.environ.setdefault("RD_SIGNALS_FILE", os.path.join(HERE, "rd_paper96_signals.csv"))

import rd_trading as rd  # noqa: E402

N = 96
rd.SCRIPT_NAME = "rd_paper96.py"
rd.STATE_SEED_FILE = os.path.join(HERE, "__no_seed__.json")    # start fresh, never copy the main paper

_prepare, _step, _levels = rd.prepare, rd.step_candle, rd.levels_for
CUR = {"row": None}


def prepare96(df):
    out, daily = _prepare(df)
    out = out.copy()
    out["RH"] = out["High"].shift(1).rolling(N, min_periods=N).max()
    out["RL"] = out["Low"].shift(1).rolling(N, min_periods=N).min()
    return out, daily


def step96(row, *a, **k):
    CUR["row"] = row
    try:
        return _step(row, *a, **k)
    finally:
        CUR["row"] = None


def levels96(date, daily):
    r = CUR["row"]
    if r is None:
        return _levels(date, daily)
    h, l = r.get("RH"), r.get("RL")
    if h is None or l is None or h != h or l != l:
        return None, None
    return float(h), float(l)


rd.prepare, rd.step_candle, rd.levels_for = prepare96, step96, levels96

if __name__ == "__main__":
    if "--alert" in sys.argv:
        rd.scan_and_alert()
    else:
        print(__doc__)
