"""
RD_oldlogic_newadd.py - PAPER ONLY test (no live orders, no Telegram by default).

= rd_trading.py (current old logic, imported unchanged: v2 rules + BB squeeze, loss cap 1.5R/3%,
  MACD loss exit, locks 1:2..1:5, full exit 1:6, profit block)
+ 2 additions:
  1. London skip : no NEW entry in the London session (13:30-18:30 IST). Running trades are managed normally.
  2. Partial 1:1 : when price reaches 1R, book 50% at 1R and move the stop of the other 50% to entry
                   (breakeven). If the rest later hits 1:2+ the normal lock ladder takes over.
     P&L = 50% x (1R) + 50% x (final exit of the rest).
Own state/signals files, so the main paper and live are never touched.
90-day live-style backtest: win 57.4%, +1.2 USDT @10/trade, max DD -4.0 (current: 38.8%, +0.3, -8.4).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("RD_STATE_FILE", os.path.join(HERE, "RD_oldlogic_newadd_state.json"))
os.environ.setdefault("RD_SIGNALS_FILE", os.path.join(HERE, "RD_oldlogic_newadd_signals.csv"))

import rd_trading as rd  # noqa: E402

rd.SCRIPT_NAME = "RD_oldlogic_newadd.py"
rd.STATE_SEED_FILE = os.path.join(HERE, "__no_seed__.json")    # start fresh, never copy the main paper

PARTIAL_R = 1.0
SKIP_SESSIONS = ("London",)

_manage, _step = rd.manage_open_trade, rd.step_candle


def manage_new(trade, candle, day_pdh, day_pdl):
    if not trade:
        return _manage(trade, candle, day_pdh, day_pdl)
    is_buy = trade['direction'] == "BUY"
    entry, risk = trade['entry_price'], trade['risk']
    high, low = float(candle['High']), float(candle['Low'])
    had_part = bool(trade.get('part_done'))

    # Rest 50% at breakeven (only while no profit lock is active / pending)
    if had_part and trade.get('locked_rr', 0) <= 0 and trade.get('pending_sl') is None:
        if (is_buy and low <= entry) or ((not is_buy) and high >= entry):
            return f"BE_Exit_After_Partial_1:{PARTIAL_R:g}", trade['part_pnl'], None

    status, pnl, t = _manage(trade, candle, day_pdh, day_pdl)
    if had_part:
        return status, 0.5 * pnl + trade['part_pnl'], t

    reward = (high - entry) if is_buy else (entry - low)
    if risk > 0 and reward / risk >= PARTIAL_R and not (t is None and pnl < 0):
        part = 0.5 * rd._pnl(entry, PARTIAL_R * risk)
        trade['part_done'], trade['part_pnl'] = True, part
        if t is not None:
            status = f"Partial_50pct_Booked_1:{PARTIAL_R:g}|{status}"
        return status, 0.5 * pnl + part, t
    return status, pnl, t


def step_new(row, trade, *a, **k):
    out = _step(row, trade, *a, **k)
    if trade is None and out[2] is not None and row['Session'] in SKIP_SESSIONS:
        return "Entry_Skipped_London_Session", 0.0, None, out[3], out[4]
    return out


rd.manage_open_trade, rd.step_candle = manage_new, step_new
rd.EXIT_PREFIXES = tuple(rd.EXIT_PREFIXES) + ("BE_Exit_After_Partial",)
rd.LOCK_PREFIXES = tuple(rd.LOCK_PREFIXES) + ("Partial_50pct_Booked",)

if __name__ == "__main__":
    if "--alert" in sys.argv:
        rd.scan_and_alert()
    else:
        print(__doc__)
