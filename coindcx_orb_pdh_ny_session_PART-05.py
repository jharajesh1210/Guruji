"""
CoinDCX Futures ORB + PDH/PDL (Long & Short), NY + London + Asia sessions  --  PART-05
-------------------------------------------------------------------------------------
Same entry / exit logic as the original script (PART-01), with these updates:

  1) NY session is now DST-aware (America/New_York, 09:30 - 16:00 ET), like London.
     Before: fixed 13:30 UTC, which since the US clock change (1 Nov) is 08:30 ET (pre-market + US data
     releases), so the ORB was built on the wrong candles.
  2) Fixed-risk position sizing: notional = RISK_PER_TRADE_USDT / (risk as % of entry), capped at
     MARGIN_USDT x LEVERAGE. Before: every trade had a fixed 3000 USDT notional, so a wide stop meant a
     huge USDT loss.
  3) Risk filter (NY + ASIA): skip trades whose stop distance is outside MIN_RISK_PCT .. MAX_RISK_PCT
     of the entry price.
  4) Correlation limits (NY + ASIA): max MAX_OPEN_PER_SESSION open trades per session and
     MAX_ENTRIES_PER_SESSION_DAY new entries per session per day (pairs are scanned from the most
     liquid downwards, so liquid pairs get priority).
  5) New ASIA session (Asia/Tokyo 09:00 - 15:30, same rules as the other sessions).
  6) Loss reduction (after the first paper results): London now also gets the risk filter and limits, plus a
     cluster guard (max MAX_SAME_DIR_PER_BAR same-direction entries per session per 5m bar) and a
     session-day loss breaker (no new entries once realized PnL <= -SESSION_DAY_LOSS_LIMIT_USDT).

PART-05 = PART-04 (incl. update 6 above) plus:

  A) Stronger candle body: BODY_MIN 0.5 -> 0.65 (breakout candle must close with a real body, not a long wick).
  B) Trend filter (1h EMA50, closed 1h bars only): LONG only when the last closed 1h close is ABOVE its EMA50,
     SHORT only when it is BELOW. Applies to every entry (breakout and reversal).
  C) Fake-breakout logic. Every valid breakout signal (volume spike + strong candle + level break) opens a "watch".
     If within FAKE_WINDOW candles a candle closes back through the broken level against the breakout
     (LONG breakout: red candle closing below the level; SHORT breakout: green candle closing above it) the
     breakout is treated as FAKE:
       - an open breakout trade is closed at that close (reason FAKE_BREAKOUT_EXIT) -> cuts the loss early
         instead of waiting for the far stop;
       - then the OPPOSITE trade is taken (kind FAKE_REV) if the trend filter, risk filter and limits allow it.
         SL = extreme of the failed breakout (+0.1% buffer), same 2R / 3R / 4R stop ladder.
     The opposite trade is only taken after the breakout is CONFIRMED fake (not blindly at the signal),
     so a real breakout is never faded.

Rules (unchanged):
- Session        : each session has its own ORB and its own previous-session PDH.
- ORB            : first 3 five-minute candles of the session. Entries only after the ORB is complete.
- Volume spike   : candle volume >= 2x average of the previous 20 candles (current candle excluded).
- LONG           : close > max(ORB high, PDH), volume spike, green candle, body >= BODY_MIN of range,
                   close in TOP 25% of the candle range.  SL = ORB low.
- SHORT          : PDH touched in session, close < ORB low, session high < PDH * 1.01, volume spike,
                   red candle, body >= BODY_MIN of range, close in BOTTOM 25% of the candle range.
                   SL = session high so far * 1.001.
- Stop ladder    : 2R reached -> SL locks +2R | 3R reached -> SL locks +3R | 4R -> final target exit.
- Session end    : no new entries in the last ENTRY_CUTOFF_MIN_BEFORE_END min, TIME_STOP after
                   MAX_HOLD_CANDLES if still < TIME_STOP_MIN_R, profit protection near the close,
                   any open trade is closed at the last session candle's close.
- Fees           : FEE_PCT_PER_SIDE is charged on entry and exit and shown in R.

PAPER TRADING ONLY. This script never places real orders and has no API key / order-placement code.
It only reads public CoinDCX market data, tracks simulated positions in orb_pdh_part5_state.json
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


# ----------------------------- CONFIG -----------------------------
CANDLE_MIN = 5
ORB_CANDLES = 3              # 3 x 5m = 15 min
VOL_MULT = 2.0
VOL_LOOKBACK = 20
CLOSE_EXTREME_PCT = 0.25     # LONG: close in top 25% of candle range; SHORT: close in bottom 25%
BODY_MIN = 0.65              # PART-05: candle body must be >= 65% of its range (was 0.5)
TARGET_R = 4.0
SHORT_SL_BUFFER = 0.001
FEE_PCT_PER_SIDE = 0.05      # taker fee estimate, % of notional per side
MIN_SESSION_FILL = 0.95      # share of expected candles a session needs to count as complete

# --- Session-end handling ---
ENTRY_CUTOFF_MIN_BEFORE_END = 90   # no NEW entries in the last N minutes of the session
MAX_HOLD_CANDLES = 24              # 24 x 5m = 2 hours
TIME_STOP_MIN_R = 1.0              # exit at close if still below this R after MAX_HOLD_CANDLES
PROTECT_MIN_BEFORE_END = 45        # in the last N min, lock profit on winners (0 = disable)
PROTECT_MIN_R = 0.5
PROTECT_LOCK_R = 0.0               # 0.0 = breakeven

# --- PART-04 update 2: fixed-risk position sizing ---
RISK_PER_TRADE_USDT = 15.0         # USDT lost if the stop is hit (before fees)
MARGIN_USDT = 100.0
LEVERAGE = 30.0
MAX_NOTIONAL_USDT = MARGIN_USDT * LEVERAGE   # hard cap on position size

# --- PART-04 update 3: risk filter (only for sessions with limits=True) ---
MIN_RISK_PCT = 0.5                 # stop distance must be >= 0.5% of entry (fees eat tiny stops)
MAX_RISK_PCT = 2.0                 # ...and <= 2.0% of entry (wide stops make 2R/4R unrealistic)

# --- PART-04 update 4: correlation limits (only for sessions with limits=True) ---
MAX_OPEN_PER_SESSION = 4           # max simultaneously open paper trades per session
MAX_ENTRIES_PER_SESSION_DAY = 8    # max new entries per session per day

# --- PART-04 update 6 (loss reduction): market-wide cluster guard + session-day loss breaker ---
# On 13 Nov, 20 LONG entries fired on the SAME 5m bar (13:30 UTC = 08:30 ET news candle) across unrelated coins,
# then reversed together: that is one bet taken 20 times. Pairs are scanned most-liquid first, so the first
# MAX_SAME_DIR_PER_BAR signals on a bar get the trade and the rest are skipped.
MAX_SAME_DIR_PER_BAR = 2           # max new entries per session, per direction, on the same 5m bar
SESSION_DAY_LOSS_LIMIT_USDT = 45.0 # stop NEW entries in a session for the day once realized net PnL <= -this (3 x risk)

# --- PART-05: trend filter + fake-breakout logic ---
TREND_EMA = 50                     # EMA length on closed 1h bars
TREND_MIN_BARS = 50                # need at least this many closed 1h bars, else trend = unknown -> no trade
FAKE_WINDOW = 3                    # candles after the breakout candle in which a close back through the level = fake
FAKE_EXIT_ENABLED = True           # close an open breakout trade when the breakout turns out fake
FAKE_REVERSE_ENABLED = True        # then take the opposite trade (trend/risk filters + limits still apply)
# -------------------------------------------------------------------

LONDON_TZ = ZoneInfo("Europe/London")
NY_TZ = ZoneInfo("America/New_York")
TOKYO_TZ = ZoneInfo("Asia/Tokyo")
# Session times are LOCAL times of each session's timezone, so DST is handled automatically.
SESSION_CONFIG = {
    "NY": {"tz": NY_TZ, "start": "09:30", "end": "16:00", "limits": True},
    "LONDON": {"tz": LONDON_TZ, "start": "08:00", "end": "15:30", "limits": True},   # was False: London made ~all of the losses
    "ASIA": {"tz": TOKYO_TZ, "start": "09:00", "end": "15:30", "limits": True},
}

STATE_FILE = "orb_pdh_part5_state.json"
TRADES_FILE = "orb_pdh_part5_trades.csv"
UNIVERSE_FILE = "eligible_futures_universe_part5.csv"


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


def position_notional(entry, risk):
    """Fixed-risk sizing: lose ~RISK_PER_TRADE_USDT if the stop is hit, capped at MAX_NOTIONAL_USDT."""
    return min(RISK_PER_TRADE_USDT / (risk / entry), MAX_NOTIONAL_USDT)


def record_trade(state, trade, pair, session, notional, kind=None):
    """Attach sizing / PnL fields to a closed trade and store it."""
    entry, risk = float(trade["entry"]), float(trade["risk"])
    usdt_per_r = risk * (notional / entry)
    trade.update({
        "pair": pair, "session": session, "kind": kind or "BREAKOUT",
        "margin_usdt": notional / LEVERAGE, "leverage": LEVERAGE, "notional_usdt": notional,
        "risk_pct": risk / entry * 100,
        "pnl_usdt_gross": trade["r_gross"] * usdt_per_r,
        "pnl_usdt_net": trade["r_net"] * usdt_per_r,
    })
    state["trades"].append(trade)
    # Realized net PnL per session-day (feeds SESSION_DAY_LOSS_LIMIT_USDT)
    local_day = pd.Timestamp(trade["entry_time"]).tz_convert(SESSION_CONFIG[session]["tz"]).date()
    day_key = f"{session}:{local_day}"
    state["day_pnl"][day_key] = state["day_pnl"].get(day_key, 0.0) + float(trade["pnl_usdt_net"])
    return trade


def mins_to_session_end(ts, session):
    """Minutes left until SESSION_END for a candle that opens at ts (candle's own length included)."""
    cfg = SESSION_CONFIG[session]
    lt = ts.tz_convert(cfg["tz"])
    return _hm(cfg["end"]) - (lt.hour * 60 + lt.minute)


def send_telegram(message):
    message = f"[PART-05] {message}"   # every alert starts with the bot name
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
        return {"positions": {}, "last_bar": {}, "trades": [], "entries": {}, "bar_entries": {}, "day_pnl": {}, "fake_watch": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("positions", {})
    state.setdefault("last_bar", {})
    state.setdefault("trades", [])
    state.setdefault("entries", {})
    state.setdefault("bar_entries", {})
    state.setdefault("day_pnl", {})
    state.setdefault("fake_watch", {})
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)
    pd.DataFrame(state.get("trades", [])).to_csv(TRADES_FILE, index=False)


def hourly_trend_frame(d):
    """Closed-bar 1h closes and their EMA(TREND_EMA), built from the 5m candles."""
    hourly = d["close"].resample("1h").last().dropna()
    return hourly, hourly.ewm(span=TREND_EMA, adjust=False).mean()


def trend_at(hourly, ema, ts):
    """+1 bullish / -1 bearish / 0 unknown, using only 1h bars that are fully closed when the 5m bar at ts closes."""
    cutoff = ts + pd.Timedelta(minutes=CANDLE_MIN)
    n = int((hourly.index + pd.Timedelta(hours=1) <= cutoff).sum())
    if n < TREND_MIN_BARS:
        return 0
    last, avg = float(hourly.iloc[n - 1]), float(ema.iloc[n - 1])
    return 1 if last > avg else (-1 if last < avg else 0)


def breakout_failed(watch_side, level, o, c):
    """Fake breakout: a candle closes back through the broken level, against the breakout direction."""
    return (c < level and c < o) if watch_side == "LONG" else (c > level and c > o)


def entry_allowed(state, cfg, session, today, stamp, side, risk, entry):
    """Risk filter + correlation limits + cluster guard + session-day loss breaker (sessions with limits=True)."""
    if not cfg.get("limits"):
        return True
    risk_pct = risk / entry * 100
    day_key = f"{session}:{today}"
    opened_today = state["entries"].get(day_key, 0)
    open_now = sum(1 for p in state["positions"].values() if p.get("session") == session)
    same_bar_dir = state["bar_entries"].get(f"{session}:{stamp}:{side}", 0)
    day_loss = state["day_pnl"].get(day_key, 0.0)
    return not (not (MIN_RISK_PCT <= risk_pct <= MAX_RISK_PCT)
                or open_now >= MAX_OPEN_PER_SESSION
                or opened_today >= MAX_ENTRIES_PER_SESSION_DAY
                or same_bar_dir >= MAX_SAME_DIR_PER_BAR
                or day_loss <= -SESSION_DAY_LOSS_LIMIT_USDT)


def open_position(state, key, pair, session, today, side, entry, sl, risk, stamp, pdh, orb_high, orb_low, kind, level):
    notional = position_notional(entry, risk)
    pos = {"side": side, "entry": entry, "sl": float(sl), "risk": float(risk),
           "tp": float(entry + TARGET_R * risk if side == "LONG" else entry - TARGET_R * risk),
           "t0": stamp, "entry_bar": stamp, "pdh": pdh, "orb_high": orb_high,
           "orb_low": orb_low, "margin": notional / LEVERAGE, "leverage": LEVERAGE,
           "notional": notional, "session": session, "kind": kind, "level": float(level)}
    state["positions"][key] = pos
    day_key = f"{session}:{today}"
    state["entries"][day_key] = state["entries"].get(day_key, 0) + 1
    bar_key = f"{session}:{stamp}:{side}"
    state["bar_entries"][bar_key] = state["bar_entries"].get(bar_key, 0) + 1
    qty = notional / entry
    send_telegram(f"COINDCX {session} {kind} {side} PAPER\n{pair}\nEntry: {entry}\nSL: {sl}\nTP: {pos['tp']}\nRisk: {RISK_PER_TRADE_USDT} USDT ({risk / entry * 100:.2f}% stop) | Notional: {notional:.2f} USDT | Leverage cap: {LEVERAGE}x\nQty: {qty:.8f}")
    return pos


def try_reversal(state, key, pair, session, cfg, today, stamp, ts, h, l, c, trend, pdh, orb_high, orb_low):
    """The watched breakout just failed. Always clears the watch; returns the new FAKE_REV position or None."""
    watch = state["fake_watch"].pop(key, None)
    if not watch or not FAKE_REVERSE_ENABLED:
        return None
    if mins_to_session_end(ts, session) <= ENTRY_CUTOFF_MIN_BEFORE_END:
        return None
    side = "SHORT" if watch["side"] == "LONG" else "LONG"
    if (side == "LONG" and trend != 1) or (side == "SHORT" and trend != -1):
        return None
    if side == "SHORT":
        sl = max(float(watch["ext_hi"]), h) * (1 + SHORT_SL_BUFFER)
        risk = sl - c
    else:
        sl = min(float(watch["ext_lo"]), l) * (1 - SHORT_SL_BUFFER)
        risk = c - sl
    if risk <= 0 or not entry_allowed(state, cfg, session, today, stamp, side, risk, c):
        return None
    return open_position(state, key, pair, session, today, side, c, sl, risk, stamp, pdh, orb_high, orb_low,
                         "FAKE_REV", watch["level"])


def live_process_pair(pair, state, session, data=None):
    # Pull enough candles for prior-session PDH, the 20-bar volume baseline and the 1h EMA50 trend.
    if data is None:
        data = fetch_coindcx_candles(pair=pair, interval="5", limit_days=4)
    if data.empty:
        return
    d = data.copy()
    d["vol_avg"] = d["volume"].rolling(VOL_LOOKBACK, min_periods=VOL_LOOKBACK).mean().shift(1)
    hourly, ema = hourly_trend_frame(d)
    # For live mode, today's session is intentionally partial; only the prior
    # session must pass the complete-session validation.
    now = datetime.now(timezone.utc)
    cfg = SESSION_CONFIG[session]
    local_now = now.astimezone(cfg["tz"])
    today = local_now.date()
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
    pos = state["positions"].get(key)
    last_seen = state["last_bar"].get(key)
    # At/after the session close, flatten any remaining paper position at the
    # final available session candle close; do not create late entries.
    if local_now.hour * 60 + local_now.minute >= _hm(cfg["end"]):
        state["fake_watch"].pop(key, None)
        if pos is not None:
            last = g.iloc[-1]
            exit_price = float(last["close"])
            trade = _close_trade(pos["side"], float(pos["entry"]), float(pos["risk"]),
                                 exit_price, pos["t0"], g.index[-1].isoformat(), "SESSION_END")
            record_trade(state, trade, pair, session, float(pos.get("notional", MAX_NOTIONAL_USDT)), pos.get("kind"))
            send_telegram(f"COINDCX {session} {pos['side']} PAPER EXIT\n{pair}\nExit: {exit_price}\nReason: SESSION_END\nNet PnL est.: {trade['pnl_usdt_net']:.4f} USDT")
            state["positions"].pop(key, None)
        return
    # Process only fully closed 5-minute bars, and never replay a bar after an Actions rerun.
    closed = g.iloc[:-1]
    if closed.empty:
        return
    for ts, bar in closed.iterrows():
        stamp = ts.isoformat()
        if last_seen and stamp <= last_seen:
            continue
        lts = ts.tz_convert(cfg["tz"])
        if lts.date() != today or (lts.hour * 60 + lts.minute) < _hm(cfg["start"]) + ORB_CANDLES * CANDLE_MIN:
            state["last_bar"][key] = stamp
            last_seen = stamp
            continue
        o, h, l, c, v = map(float, (bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]))
        upto = g.loc[:ts]
        run_high = float(upto["high"].max())
        left = mins_to_session_end(ts, session)
        trend = trend_at(hourly, ema, ts)

        # --- fake-breakout watch: age it, track the failed-breakout extreme, detect the failure ---
        watch = state["fake_watch"].get(key)
        age = 0
        if watch:
            age = int((ts - pd.Timestamp(watch["bar"])).total_seconds() // (CANDLE_MIN * 60))
            if age > FAKE_WINDOW:
                state["fake_watch"].pop(key, None)
                watch = None
            elif age >= 1:
                watch["ext_hi"] = max(float(watch["ext_hi"]), h)
                watch["ext_lo"] = min(float(watch["ext_lo"]), l)
        failed = bool(watch) and age >= 1 and breakout_failed(watch["side"], float(watch["level"]), o, c)

        if pos is None:
            if failed:
                pos = try_reversal(state, key, pair, session, cfg, today, stamp, ts, h, l, c, trend,
                                   pdh, orb_high, orb_low)
                state["last_bar"][key] = stamp
                last_seen = stamp
                continue
            rng = h - l
            strong = rng > 0 and abs(c - o) / rng >= BODY_MIN
            va = bar["vol_avg"]
            spike = (not pd.isna(va)) and v >= VOL_MULT * float(va)
            if strong and spike and left > ENTRY_CUTOFF_MIN_BEFORE_END:
                long_ok = c > max(orb_high, pdh) and c > o and (c - l) / rng >= 1 - CLOSE_EXTREME_PCT
                short_ok = run_high >= pdh * 0.999 and c < orb_low and run_high < pdh * 1.01 and c < o and (h - c) / rng >= 1 - CLOSE_EXTREME_PCT
                side = None
                if long_ok:
                    sl = orb_low
                    risk = c - sl
                    if risk > 0:
                        side = "LONG"
                elif short_ok:
                    sl = run_high * (1 + SHORT_SL_BUFFER)
                    risk = sl - c
                    if risk > 0:
                        side = "SHORT"
                if side:
                    level = max(orb_high, pdh) if side == "LONG" else orb_low
                    # Every valid breakout signal is watched for a fake, even if the trend filter blocks the trade itself.
                    if key not in state["fake_watch"]:
                        state["fake_watch"][key] = {"side": side, "level": float(level), "bar": stamp,
                                                    "ext_hi": h, "ext_lo": l}
                    # Trend filter: LONG only above the 1h EMA50, SHORT only below it.
                    if (side == "LONG" and trend != 1) or (side == "SHORT" and trend != -1):
                        side = None
                if side and entry_allowed(state, cfg, session, today, stamp, side, risk, c):
                    pos = open_position(state, key, pair, session, today, side, c, sl, risk, stamp,
                                        pdh, orb_high, orb_low, "BREAKOUT", level)
            state["last_bar"][key] = stamp
            last_seen = stamp
            continue

        side, entry, risk, sl, tp = pos["side"], float(pos["entry"]), float(pos["risk"]), float(pos["sl"]), float(pos["tp"])
        notional = float(pos.get("notional", MAX_NOTIONAL_USDT))
        kind = pos.get("kind", "BREAKOUT")
        is_long = side == "LONG"
        hit_sl = (l <= sl) if is_long else (h >= sl)
        hit_tp = (h >= tp) if is_long else (l <= tp)
        if hit_sl or hit_tp:
            exit_price = sl if hit_sl else tp
            reason = "TARGET_4R" if not hit_sl else ("BREAKEVEN" if sl == entry else ("LOCKED_PROFIT" if ((sl > entry) if is_long else (sl < entry)) else "SL"))
            trade = _close_trade(side, entry, risk, exit_price, pos["t0"], stamp, reason)
            record_trade(state, trade, pair, session, notional, kind)
            send_telegram(f"COINDCX {session} {side} PAPER EXIT\n{pair}\nExit: {exit_price}\nReason: {reason}\nNet PnL est.: {trade['pnl_usdt_net']:.4f} USDT")
            pos = None
            state["positions"].pop(key, None)
        elif (FAKE_EXIT_ENABLED and failed and kind == "BREAKOUT" and watch.get("bar") == pos.get("entry_bar")):
            # The breakout we are in just turned out fake: leave at this close, then (if allowed) take the other side.
            trade = _close_trade(side, entry, risk, c, pos["t0"], stamp, "FAKE_BREAKOUT_EXIT")
            record_trade(state, trade, pair, session, notional, kind)
            send_telegram(f"COINDCX {session} {side} PAPER EXIT\n{pair}\nExit: {c}\nReason: FAKE_BREAKOUT_EXIT\nNet PnL est.: {trade['pnl_usdt_net']:.4f} USDT")
            state["positions"].pop(key, None)
            pos = try_reversal(state, key, pair, session, cfg, today, stamp, ts, h, l, c, trend,
                               pdh, orb_high, orb_low)
            state["last_bar"][key] = stamp
            last_seen = stamp
            continue
        else:
            close_r = ((c - entry) if is_long else (entry - c)) / risk
            held = int((ts - pd.Timestamp(pos["entry_bar"])).total_seconds() // (CANDLE_MIN * 60))
            if held >= MAX_HOLD_CANDLES and close_r < TIME_STOP_MIN_R:
                trade = _close_trade(side, entry, risk, c, pos["t0"], stamp, "TIME_STOP")
                record_trade(state, trade, pair, session, notional, kind)
                send_telegram(f"COINDCX {session} {side} PAPER EXIT\n{pair}\nExit: {c}\nReason: TIME_STOP\nNet PnL est.: {trade['pnl_usdt_net']:.4f} USDT")
                pos = None
                state["positions"].pop(key, None)
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
    print(f"Live-paper scan: {len(universe)} pairs | risk/trade={RISK_PER_TRADE_USDT} USDT | max notional={MAX_NOTIONAL_USDT} USDT | sessions={list(SESSION_CONFIG)}")
    for i, coin in enumerate(universe, 1):
        pair = coin["pair"]
        print(f"[{i}/{len(universe)}] {pair}")
        try:
            data = fetch_coindcx_candles(pair=pair, interval="5", limit_days=4)
            for session in SESSION_CONFIG:
                live_process_pair(pair, state, session, data=data)
        except Exception as exc:
            print(f"[PAIR ERROR] {pair}: {exc}")
        time.sleep(0.25)
    save_state(state)
    print(f"Open paper positions: {len(state['positions'])}; closed paper trades: {len(state['trades'])}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="CoinDCX ORB + PDH/PDL NY/London/Asia session PAPER trading, PART-05 (no real orders)")
    ap.add_argument("--mode", default="live-paper", choices=["live-paper"],
                    help="Run mode (only live-paper exists; the GitHub workflow passes --mode live-paper)")
    ap.parse_args()
    run_live_paper()
