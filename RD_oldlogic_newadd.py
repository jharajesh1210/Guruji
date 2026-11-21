"""
RD_oldlogic_newadd.py - PAPER ONLY test (no live orders; Telegram alerts ON via workflow).

= rd_trading.py (current old logic, imported unchanged: v2 rules + BB squeeze, loss cap 1.5R/3%,
  MACD loss exit, locks 1:2..1:5, full exit 1:6, profit block)
+ 1 addition (21 Nov 11:35 IST update: London session ON again, partial moved 1R -> 1.25R):
  Partial 1:1.25 : when price reaches 1.25R, book 50% at 1.25R and move the stop of the other 50% to entry
                   (breakeven). If the rest later hits 1:2+ the normal lock ladder takes over.
     P&L = 50% x (1.25R) + 50% x (final exit of the rest).
+ 2nd addition (21 Nov 12:55 IST): session 30-minute high/low in ASIAN and LONDON
  Each Asian / London session marks the high/low of its first 30 minutes (first 2 x 15m candles).
  In those sessions a BUY needs the breakout of max(PDH, 30m high), a SELL of min(PDL, 30m low)
  (both levels in the same direction); no new entry during the first 30 minutes of Asian / London.
  NY session = plain PDH/PDL (unchanged). Running trades are managed with the normal PDH/PDL.
  90-day backtest of this filter alone: +2.8 vs +0.6 USDT @10/trade, win 39.9% vs 38.7%.
Own state/signals files, so the main paper and live are never touched.
90-day live-style backtest: win 51.1%, +1.7 USDT @10/trade, max DD -5.3 (current: 38.8%, +0.4, -8.3).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("RD_STATE_FILE", os.path.join(HERE, "RD_oldlogic_newadd_state.json"))
os.environ.setdefault("RD_SIGNALS_FILE", os.path.join(HERE, "RD_oldlogic_newadd_signals.csv"))

import rd_trading as rd  # noqa: E402

rd.SCRIPT_NAME = "RD_oldlogic_newadd.py"
rd.STATE_SEED_FILE = os.path.join(HERE, "__no_seed__.json")    # start fresh, never copy the main paper

PARTIAL_R = 1.25
SKIP_SESSIONS = ()          # London skip removed (all sessions trade)
ORB_SESSIONS = ("Asian", "London")   # 30-minute session high/low used together with PDH/PDL
ORB_BARS = 2                          # first 2 x 15m candles = 30 minutes

_manage, _step = rd.manage_open_trade, rd.step_candle
_prepare, _levels = rd.prepare, rd.levels_for
CUR = {"row": None}


def prepare_new(df):
    out, daily = _prepare(df)
    out = out.copy()
    g = out.groupby([out['Date'].astype(str), out['Session']], sort=False)
    k = g.cumcount()
    oh = g['High'].transform(lambda s: s.iloc[:ORB_BARS].max())
    ol = g['Low'].transform(lambda s: s.iloc[:ORB_BARS].min())
    out['ORB_H'] = oh.where(k >= ORB_BARS)
    out['ORB_L'] = ol.where(k >= ORB_BARS)
    return out, daily


def levels_new(date, daily):
    pdh, pdl = _levels(date, daily)
    r = CUR["row"]
    if r is None or pdh is None or r['Session'] not in ORB_SESSIONS:
        return pdh, pdl
    oh, ol = r.get('ORB_H'), r.get('ORB_L')
    if oh is None or ol is None or oh != oh or ol != ol:
        return None, None            # first 30 minutes of Asian / London: no new entry
    return max(pdh, float(oh)), min(pdl, float(ol))


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
    CUR["row"] = row if trade is None else None      # session 30m level only for NEW entries
    try:
        out = _step(row, trade, *a, **k)
    finally:
        CUR["row"] = None
    if trade is None and out[2] is not None and row['Session'] in SKIP_SESSIONS:
        return "Entry_Skipped_London_Session", 0.0, None, out[3], out[4]
    return out


rd.manage_open_trade, rd.step_candle = manage_new, step_new
rd.prepare, rd.levels_for = prepare_new, levels_new
rd.EXIT_PREFIXES = tuple(rd.EXIT_PREFIXES) + ("BE_Exit_After_Partial",)
rd.LOCK_PREFIXES = tuple(rd.LOCK_PREFIXES) + ("Partial_50pct_Booked",)

if __name__ == "__main__":
    if "--alert" in sys.argv:
        rd.scan_and_alert()
    else:
        print(__doc__)
