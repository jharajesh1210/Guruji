"""
PART-03: PART-02 + FAKE BREAKOUT FILTER (CoinDCX ORB + PDH/PDL, NY and London sessions).
PART-01 and PART-02 files are untouched and keep running; this file has its own state / trades / universe files.

EVERYTHING is exactly as in PART-02 (universe filter, candle fetch, sessions, ORB, PDH/PDL, setup detection,
volume spike, candle body >= 40%, close at the candle extreme, one trade per pair per session per day,
NY direction flipped / London original direction, stop and risk formulas, 4R target with 2R/3R lock ladder,
time stop, pre-close protection, 90-min entry cutoff, fees, paper-trade bookkeeping, CSV columns)
EXCEPT ONE THING: the PART-02 entry is no longer taken on the breakout candle itself. It is taken only after
the next candles tell us whether the breakout was FAKE or REAL, and only if that supports the trade direction.

HOW THE FILTER WORKS (SESSION_RULES[...]["filter"])
  Breakout candle = the PART-02 setup candle:
      UP   : PART-02 "long setup"  (close > max(ORB high, PDH) ...)   level = max(ORB high, PDH)
      DOWN : PART-02 "short setup" (PDH touched, close < ORB low ...) level = ORB low
  A candle is a "FAKE candle" when it closes BACK INSIDE the level (UP: close < level, DOWN: close > level),
  is coloured opposite to the breakout and has a body >= FAKE_CONFIRM_BODY_MIN of its range.
  The watch lasts FAKE_WINDOW_CANDLES closed candles (3 x 5m = 15 min) after the breakout candle.

  "FAKE"  (NY, direction flipped / fade):  PART-02 NY trade is taken only if the breakout turns out FAKE.
          Entry = close of the first FAKE candle. If no FAKE candle appears in the window the breakout was real -> no trade.
          (UP breakout -> SHORT, DOWN breakout -> LONG, exactly the PART-02 NY mapping.)
  "REAL"  (London, original direction):    PART-02 London trade is taken only if the breakout HOLDS.
          If a FAKE candle appears in the window the breakout failed -> no trade.
          Otherwise entry = close of the last candle of the window.
          (UP breakout -> LONG, DOWN breakout -> SHORT, exactly the PART-02 London mapping.)
  "NONE"  no filter: entry on the breakout candle itself = PART-02 behaviour (use it to check that this file
          reproduces PART-02 exactly).

  At entry time risk / stop / target are computed with the SAME PART-02 formulas on the entry candle's close:
      long setup : risk = close - ORB low;           NY SHORT sl = close + risk | London LONG sl = ORB low
      short setup: sl0 = session high * (1+0.1%), risk = sl0 - close;  NY LONG sl = close - risk | London SHORT sl = sl0

PAPER TRADING ONLY. This script never places real orders and has no API key / order-placement code.
It only reads public CoinDCX market data, tracks simulated positions in fake_break_out_state.json
and sends Telegram alerts.
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests

URL = "https://public.coindcx.com/market_data/candlesticks"

ACTIVE_FUTURES_URL = "https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments?margin_currency_short_name[]=USDT"
FUTURES_PRICES_URL = "https://public.coindcx.com/market_data/v3/current_prices/futures/rt"
MIN_COIN_PRICE_USDT = 1.0
MIN_24H_VOLUME_USDT = 10_000_000.0


def fetch_eligible_futures_pairs():
    """Return active USDT perpetual futures with LTP > $1 and estimated 24h quote turnover > $10m."""
    instruments_response = requests.get(ACTIVE_FUTURES_URL, timeout=25)
    instruments_response.raise_for_status()
    instruments = instruments_response.json()
    prices_response = requests.get(FUTURES_PRICES_URL, timeout=25)
    prices_response.raise_for_status()
    price_payload = prices_response.json()
    prices = price_payload.get("prices", {}) if isinstance(price_payload, dict) else {}

    eligible = []
    for pair in instruments:
        quote = prices.get(pair)
        if not isinstance(quote, dict):
            continue
        try:
            last_price = float(quote.get("ls", 0))
            base_volume_24h = float(quote.get("v", 0))
            volume_usdt_24h = last_price * base_volume_24h
        except (TypeError, ValueError):
            continue
        if last_price > MIN_COIN_PRICE_USDT and volume_usdt_24h > MIN_24H_VOLUME_USDT:
            eligible.append({
                "pair": pair,
                "last_price_usdt": last_price,
                "volume_24h_usdt_est": volume_usdt_24h,
            })

    eligible.sort(key=lambda x: x["volume_24h_usdt_est"], reverse=True)
    return eligible


# ----------------------------- CONFIG (same values as PART-02) -----------------------------
RUN_SESSIONS = ["NY", "LONDON"]
CANDLE_MIN = 5
ORB_CANDLES = 3              # 3 x 5m = 15 min
VOL_MULT = 2.0
VOL_LOOKBACK = 20
CLOSE_EXTREME_PCT = 0.25     # LONG setup: close in top 25% of candle range; SHORT setup: bottom 25%
BODY_MIN = 0.4               # candle body must be >= 40% of its range
# Same as PART-02: only NY flips the direction; both sessions max 1 trade per pair per day.
# "filter" is the ONLY new thing in PART-03: "FAKE" | "REAL" | "NONE" (see docstring).
SESSION_RULES = {
    "NY":     {"invert": True,  "body_min": BODY_MIN, "one_trade_per_day": True, "filter": "FAKE"},
    "LONDON": {"invert": False, "body_min": BODY_MIN, "one_trade_per_day": True, "filter": "REAL"},
}
TARGET_R = 4.0
SHORT_SL_BUFFER = 0.001
FEE_PCT_PER_SIDE = 0.05      # taker fee estimate, % of notional per side
MIN_SESSION_FILL = 0.95      # share of expected candles a session needs to count as complete

ENTRY_CUTOFF_MIN_BEFORE_END = 90
MAX_HOLD_CANDLES = 24        # 24 x 5m = 2 hours
TIME_STOP_MIN_R = 1.0
PROTECT_MIN_BEFORE_END = 45
PROTECT_MIN_R = 0.5
PROTECT_LOCK_R = 0.0         # 0.0 = breakeven

# --- Fake breakout filter settings (new in PART-03) ---
FAKE_WINDOW_CANDLES = 3          # candles watched after the breakout candle (3 x 5m = 15 min)
FAKE_CONFIRM_BODY_MIN = BODY_MIN # a FAKE candle must have a body >= this share of its range
FAKE_NEED_OPPOSITE_COLOR = True  # FAKE candle must be red after an UP breakout / green after a DOWN breakout
# -------------------------------------------------------------------------------------------

STATE_FILE = "fake_break_out_state.json"
TRADES_FILE = "fake_break_out_trades.csv"
UNIVERSE_FILE = "eligible_futures_universe_fake_break_out.csv"
MARGIN_USDT = 100.0
LEVERAGE = 30.0
NOTIONAL_USDT = MARGIN_USDT * LEVERAGE

LONDON_TZ = ZoneInfo("Europe/London")
SESSION_CONFIG = {
    "NY": {"tz": timezone.utc, "start": "13:30", "end": "21:00"},
    "LONDON": {"tz": LONDON_TZ, "start": "08:00", "end": "15:30"},
}


def _hm(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def fetch_coindcx_candles(pair="B-BTC_USDT", interval="5", limit_days=14):
    end = int(time.time()) // 300 * 300
    start = end - (limit_days * 86400)
    response = requests.get(
        URL,
        params={"pair": pair, "from": start, "to": end, "resolution": interval, "pcode": "f"},
        timeout=25,
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict):
        payload = payload.get("data", payload.get("candles", []))

    frame = pd.DataFrame(payload)
    if frame.empty:
        raise RuntimeError("CoinDCX se data receive nahi hua.")

    frame["time"] = pd.to_numeric(frame["time"], errors="coerce")
    frame["time"] = pd.to_datetime(frame["time"], unit="ms", utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")

    return frame.dropna().drop_duplicates("time").sort_values("time").set_index("time")


def session_day(df, session, day):
    cfg = SESSION_CONFIG[session]
    local = df.index.tz_convert(cfg["tz"])
    minutes = local.hour * 60 + local.minute
    return df[(local.date == day) & (minutes >= _hm(cfg["start"])) & (minutes < _hm(cfg["end"]))]


def complete_sessions(df, session):
    cfg = SESSION_CONFIG[session]
    local = df.index.tz_convert(cfg["tz"])
    minutes = local.hour * 60 + local.minute
    selected = df[(minutes >= _hm(cfg["start"])) & (minutes < _hm(cfg["end"]))]
    if selected.empty:
        return {}
    expected = (_hm(cfg["end"]) - _hm(cfg["start"])) // CANDLE_MIN
    return {day: group for day, group in selected.groupby(selected.index.tz_convert(cfg["tz"]).date)
            if len(group) >= int(expected * MIN_SESSION_FILL)}


def _close_trade(side, entry, risk, exit_price, entry_time, exit_time, reason):
    gross_r = ((exit_price - entry) if side == "LONG" else (entry - exit_price)) / risk
    fee_r = (2 * FEE_PCT_PER_SIDE / 100 * entry) / risk
    return {
        "entry_time": entry_time, "exit_time": exit_time, "side": side,
        "entry": entry, "exit": exit_price, "risk": risk,
        "r_gross": gross_r, "r_net": gross_r - fee_r, "reason": reason,
    }


def mins_to_session_end(ts, session):
    """Minutes left until the session end for a candle that opens at ts (candle's own length included)."""
    cfg = SESSION_CONFIG[session]
    lt = ts.tz_convert(cfg["tz"])
    return _hm(cfg["end"]) - (lt.hour * 60 + lt.minute)


def send_telegram(message):
    token = os.getenv("TG_TOKEN") or os.getenv("BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID") or os.getenv("CHAT_ID")
    if not token or not chat_id:
        print("[TELEGRAM NOT CONFIGURED] Add TG_TOKEN and TG_CHAT_ID GitHub secrets.")
        return
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": message},
        timeout=20,
    )
    response.raise_for_status()


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"positions": {}, "last_bar": {}, "trades": [], "pending": {}, "traded_days": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)
    pd.DataFrame(state.get("trades", [])).to_csv(TRADES_FILE, index=False)


def _record_exit(state, key, pair, session, pos, exit_price, exit_time, reason):
    """Close a paper position (same trade row / CSV columns as PART-02) and alert."""
    entry, risk = float(pos["entry"]), float(pos["risk"])
    trade = _close_trade(pos["side"], entry, risk, exit_price, pos["t0"], exit_time, reason)
    per_r_usdt = risk * (NOTIONAL_USDT / entry)
    trade.update({"pair": pair, "session": session, "margin_usdt": MARGIN_USDT, "leverage": LEVERAGE,
                  "notional_usdt": NOTIONAL_USDT,
                  "pnl_usdt_gross": trade["r_gross"] * per_r_usdt,
                  "pnl_usdt_net": trade["r_net"] * per_r_usdt})
    state["trades"].append(trade)
    send_telegram(f"COINDCX PART-03 FAKE_BREAK_OUT {session} {pos['side']} PAPER EXIT\n{pair}\nExit: {exit_price}\n"
                  f"Reason: {reason}\nNet PnL est.: {trade['pnl_usdt_net']:.4f} USDT")
    state["positions"].pop(key, None)
    return trade


def _build_entry(direction, c, orb_low, run_high, invert):
    """PART-02 side / stop / risk formulas, evaluated at close c.
    direction UP = PART-02 'long setup', DOWN = PART-02 'short setup'.
    Returns (side, setup, sl, risk) or None."""
    if direction == "UP":
        risk = c - orb_low
        if risk <= 0:
            return None
        if invert:      # NY: long breakout setup -> SHORT (stop mirrored above the entry)
            return "SHORT", "LONG_BREAKOUT", c + risk, risk
        return "LONG", "LONG_BREAKOUT", orb_low, risk     # London: original behaviour
    sl0 = run_high * (1 + SHORT_SL_BUFFER)
    risk = sl0 - c
    if risk <= 0:
        return None
    if invert:          # NY: short breakdown setup -> LONG (stop mirrored below the entry)
        if c - risk > 0:
            return "LONG", "SHORT_BREAKDOWN", c - risk, risk
        return None
    return "SHORT", "SHORT_BREAKDOWN", sl0, risk           # London: original behaviour


def live_process_pair(pair, state, session, data=None, now=None):
    # Pull enough candles for prior-session PDH and the rolling 20-bar volume baseline.
    if data is None:
        data = fetch_coindcx_candles(pair=pair, interval="5", limit_days=4)
    if data.empty:
        return
    d = data.copy()
    d["vol_avg"] = d["volume"].rolling(VOL_LOOKBACK, min_periods=VOL_LOOKBACK).mean().shift(1)
    # For live mode, today's session is intentionally partial; only the prior
    # session must pass the original complete-session validation.
    now = now or datetime.now(timezone.utc)
    cfg = SESSION_CONFIG[session]
    rules = SESSION_RULES[session]
    local_now = now.astimezone(cfg["tz"])
    today = local_now.date()
    today_iso = today.isoformat()
    complete_prior = complete_sessions(d[d.index.tz_convert(cfg["tz"]).date < today], session)
    prev_days = sorted(day for day in complete_prior if day < today)
    if not prev_days:
        print(f"[{session}] {local_now:%Y-%m-%d %H:%M %Z}: waiting for previous complete session data.")
        return
    g = session_day(d, session, today)
    if g.empty:
        print(f"[{session}] {local_now:%Y-%m-%d %H:%M %Z}: session has not opened yet.")
        return
    prev = complete_prior[prev_days[-1]]
    pdh = float(prev["high"].max())
    orb = g.iloc[:ORB_CANDLES]
    if len(orb) < ORB_CANDLES:
        print(f"[{session}] {local_now:%Y-%m-%d %H:%M %Z}: collecting first {ORB_CANDLES} ORB candles.")
        return
    print(f"[{session}] active | local={local_now:%Y-%m-%d %H:%M %Z} | prior_session={prev_days[-1]} | candles_today={len(g)}")
    orb_high, orb_low = float(orb["high"].max()), float(orb["low"].min())
    key = f"{session}:{pair}"
    traded_days = state.setdefault("traded_days", {})   # one trade per pair per session per day
    pending = state.setdefault("pending", {})           # armed fake-breakout watches
    pos = state["positions"].get(key)
    last_seen = state["last_bar"].get(key)
    mode = rules.get("filter", "NONE")

    # At/after the session close, flatten any remaining paper position at the
    # final available session candle close; do not create late entries.
    if local_now.hour * 60 + local_now.minute >= _hm(cfg["end"]):
        pending.pop(key, None)
        if pos is not None:
            last = g.iloc[-1]
            _record_exit(state, key, pair, session, pos, float(last["close"]), g.index[-1].isoformat(), "SESSION_END")
        return

    def open_position(direction, c, stamp, left, orb_low, run_high, note):
        """Open the PART-02 trade for this breakout direction at close c. Returns the position or None."""
        if left <= ENTRY_CUTOFF_MIN_BEFORE_END:          # no late entries (PART-02 rule, applied at entry time)
            print(f"[{session}] {pair} {note}: too late in session - skipped")
            return None
        built = _build_entry(direction, c, orb_low, run_high, rules["invert"])
        if built is None:
            return None
        side, setup, sl, risk = built
        p = {"side": side, "setup": setup, "entry": c, "sl": float(sl), "risk": float(risk),
             "tp": float(c + TARGET_R * risk if side == "LONG" else c - TARGET_R * risk),
             "t0": stamp, "entry_bar": stamp, "pdh": pdh, "orb_high": orb_high,
             "orb_low": orb_low, "margin": MARGIN_USDT, "leverage": LEVERAGE,
             "notional": NOTIONAL_USDT, "session": session}
        state["positions"][key] = p
        traded_days[key] = today_iso
        qty = NOTIONAL_USDT / c
        fade_txt = "FADE " if rules["invert"] else ""
        send_telegram(f"COINDCX PART-03 FAKE_BREAK_OUT {session} ORB {fade_txt}{side} PAPER (setup {setup}, {note})\n"
                      f"{pair}\nEntry: {c}\nSL: {sl}\nTP: {p['tp']}\n"
                      f"Margin: {MARGIN_USDT} USDT | Leverage: {LEVERAGE}x | Notional: {NOTIONAL_USDT} USDT\n"
                      f"Qty: {qty:.8f}")
        return p

    # Process only fully closed 5-minute bars, and never replay a bar after an Actions rerun.
    closed = g.iloc[:-1]
    if closed.empty:
        return
    for ts, bar in closed.iterrows():
        stamp = ts.isoformat()
        if last_seen and stamp <= last_seen:
            continue
        ts_local = ts.tz_convert(cfg["tz"])
        if ts_local.date() != today or (ts_local.hour * 60 + ts_local.minute) < _hm(cfg["start"]) + ORB_CANDLES * CANDLE_MIN:
            state["last_bar"][key] = stamp
            last_seen = stamp
            continue
        o, h, l, c, v = map(float, (bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]))
        # Setup calculations and thresholds are unchanged from PART-02.
        upto = g.loc[:ts]
        run_high = float(upto["high"].max())
        left = mins_to_session_end(ts, session)

        # ------------------------- no open position -------------------------
        if pos is None:
            pend = pending.get(key)
            if pend and pend.get("day") != today_iso:       # stale watch from a previous day
                pending.pop(key, None)
                pend = None
            if rules["one_trade_per_day"] and traded_days.get(key) == today_iso:
                pending.pop(key, None)
                state["last_bar"][key] = stamp
                last_seen = stamp
                continue

            rng = h - l
            body_ratio = abs(c - o) / rng if rng > 0 else 0.0

            # (1) an armed watch: is this candle a FAKE candle, and is the window over?
            if pend:
                up = pend["dir"] == "UP"
                pend["bars"] += 1
                back_inside = (c < float(pend["level"])) if up else (c > float(pend["level"]))
                color_ok = (c < o) if up else (c > o)
                fake_candle = (back_inside and body_ratio >= FAKE_CONFIRM_BODY_MIN
                               and (color_ok or not FAKE_NEED_OPPOSITE_COLOR))
                window_over = pend["bars"] >= FAKE_WINDOW_CANDLES
                take = False
                if mode == "FAKE":
                    if fake_candle:
                        take, note = True, "fake breakout confirmed"
                    elif window_over:
                        print(f"[{session}] {pair} breakout held {FAKE_WINDOW_CANDLES} candles (real) - no fade trade")
                        pending.pop(key, None)
                    else:
                        pending[key] = pend
                elif mode == "REAL":
                    if fake_candle:
                        print(f"[{session}] {pair} breakout failed (fake) - trade skipped")
                        pending.pop(key, None)
                    elif window_over:
                        take, note = True, "breakout held"
                    else:
                        pending[key] = pend
                if take:
                    pending.pop(key, None)
                    pos = open_position(pend["dir"], c, stamp, left, orb_low, run_high, note)

            # (2) is this candle itself a breakout candle? (setup detection identical to PART-02)
            if pos is None and (key not in pending):
                strong = rng > 0 and body_ratio >= rules["body_min"]
                va = bar["vol_avg"]
                spike = (not pd.isna(va)) and v >= VOL_MULT * float(va)
                if strong and spike and left > ENTRY_CUTOFF_MIN_BEFORE_END:
                    long_ok = c > max(orb_high, pdh) and c > o and (c - l) / rng >= 1 - CLOSE_EXTREME_PCT
                    short_ok = (run_high >= pdh * 0.999 and c < orb_low and run_high < pdh * 1.01
                                and c < o and (h - c) / rng >= 1 - CLOSE_EXTREME_PCT)
                    new_dir = "UP" if long_ok else ("DOWN" if short_ok else None)
                    if new_dir:
                        if mode == "NONE":                    # no filter: PART-02 behaviour
                            pos = open_position(new_dir, c, stamp, left, orb_low, run_high, "no filter")
                        else:
                            level = max(orb_high, pdh) if new_dir == "UP" else orb_low
                            pending[key] = {"dir": new_dir, "level": float(level), "bars": 0,
                                            "t_break": stamp, "day": today_iso}
                            print(f"[{session}] {pair} {new_dir} breakout candle at {stamp} - watching "
                                  f"{FAKE_WINDOW_CANDLES} candles (filter {mode}, level {level})")
            state["last_bar"][key] = stamp
            last_seen = stamp
            continue

        # ------------------------- open position management (same as PART-02) -------------------------
        side, entry, risk, sl, tp = pos["side"], float(pos["entry"]), float(pos["risk"]), float(pos["sl"]), float(pos["tp"])
        is_long = side == "LONG"
        hit_sl = (l <= sl) if is_long else (h >= sl)
        hit_tp = (h >= tp) if is_long else (l <= tp)
        if hit_sl or hit_tp:
            exit_price = sl if hit_sl else tp
            reason = "TARGET_4R" if not hit_sl else ("BREAKEVEN" if sl == entry else (
                "LOCKED_PROFIT" if ((sl > entry) if is_long else (sl < entry)) else "SL"))
            _record_exit(state, key, pair, session, pos, exit_price, stamp, reason)
            pos = None
        else:
            close_r = ((c - entry) if is_long else (entry - c)) / risk
            held = int((ts - pd.Timestamp(pos["entry_bar"])).total_seconds() // (CANDLE_MIN * 60))
            if held >= MAX_HOLD_CANDLES and close_r < TIME_STOP_MIN_R:    # time stop
                _record_exit(state, key, pair, session, pos, c, stamp, "TIME_STOP")
                pos = None
                state["last_bar"][key] = stamp
                last_seen = stamp
                continue
            r_now = ((h - entry) if is_long else (entry - l)) / risk
            new_sl = sl
            if r_now >= 3.0:
                new_sl = entry + 3.0 * risk if is_long else entry - 3.0 * risk
            elif r_now >= 2.0:
                new_sl = entry + 2.0 * risk if is_long else entry - 2.0 * risk
            if PROTECT_MIN_BEFORE_END and left <= PROTECT_MIN_BEFORE_END and close_r >= PROTECT_MIN_R:
                lock = entry + PROTECT_LOCK_R * risk if is_long else entry - PROTECT_LOCK_R * risk
                new_sl = max(new_sl, lock) if is_long else min(new_sl, lock)
            pos["sl"] = max(sl, new_sl) if is_long else min(sl, new_sl)
            state["positions"][key] = pos
        state["last_bar"][key] = stamp
        last_seen = stamp


def run_live_paper():
    state = load_state()
    universe = fetch_eligible_futures_pairs()
    if not universe:
        raise RuntimeError("No eligible USDT futures found; live-paper scan aborted.")
    pd.DataFrame(universe).to_csv(UNIVERSE_FILE, index=False)
    print(f"Live-paper scan: {len(universe)} pairs | margin={MARGIN_USDT} USDT | leverage={LEVERAGE}x | notional={NOTIONAL_USDT} USDT")
    for i, coin in enumerate(universe, 1):
        pair = coin["pair"]
        print(f"[{i}/{len(universe)}] {pair}")
        try:
            data = fetch_coindcx_candles(pair=pair, interval="5", limit_days=4)
            for session in RUN_SESSIONS:
                live_process_pair(pair, state, session, data=data)
        except Exception as exc:
            print(f"[PAIR ERROR] {pair}: {exc}")
        time.sleep(0.25)
    save_state(state)
    print(f"Open paper positions: {len(state['positions'])}; closed paper trades: {len(state['trades'])}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="CoinDCX PART-02 + FAKE BREAKOUT filter, NY/London session PAPER trading (no real orders)")
    ap.add_argument("--mode", default="live-paper", choices=["live-paper"],
                    help="Run mode (only live-paper exists; the GitHub workflow passes --mode live-paper)")
    ap.parse_args()
    run_live_paper()
