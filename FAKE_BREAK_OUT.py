"""
PART-03: FAKE BREAKOUT version of the CoinDCX ORB + PDH/PDL strategy (NY + London sessions).
PART-01 and PART-02 files are untouched and keep running; this file has its own state / trades / universe files.

Everything is copied from PART-02 (universe filter, candle fetch, sessions, ORB, PDH/PDL, setup detection,
volume spike, candle-body rule, 1 trade per pair per session per day, 4R target with 2R/3R lock ladder,
time stop, pre-close protection, fees, paper-trade bookkeeping) EXCEPT the entry logic:

  PART-02 : NY entered the opposite side of the breakout candle immediately at its close (stop = mirrored risk),
            London entered the breakout direction.
  PART-03 : BOTH sessions trade only a CONFIRMED FAKE BREAKOUT.

FAKE BREAKOUT RULE
  1) A breakout candle is detected exactly as in PART-02 (volume spike, strong body, close at the candle extreme):
       UP   : close > max(ORB high, PDH)                    -> level = max(ORB high, PDH)
       DOWN : PDH touched, close < ORB low (PART-02 filters)  -> level = ORB low
     No trade is opened on this candle. It only arms a "pending" fake-breakout watch.
  2) Within the next FAKE_WINDOW_CANDLES closed candles, a candle must CLOSE BACK INSIDE the broken level
     (UP breakout: close < level, DOWN breakout: close > level), with a strong opposite-coloured body
     (>= FAKE_CONFIRM_BODY_MIN of its range). That candle confirms the fake breakout.
       -> entry at the close of the confirming candle, in the OPPOSITE direction of the breakout:
            fake UP breakout   -> SHORT
            fake DOWN breakout -> LONG
  3) STOP is placed beyond the spike extreme (highest high / lowest low from the breakout candle up to the
     confirming candle) +/- FAKE_SL_BUFFER, so the wick that traps the breakout traders is not what stops us.
     risk (1R) = distance entry -> stop.  Target 4R, same 2R/3R lock ladder as PART-02.
  4) If no confirming candle arrives within the window, the breakout is treated as REAL and nothing is traded.

EXTRA FILTERS (all configurable below)
  * MIN_RISK_PCT / MAX_RISK_PCT : skip trades whose stop distance is too small (fees + noise dominate)
    or too large (at 30x leverage a ~3.3% adverse move is liquidation in real trading).
  * ENTRY_CUTOFF_MIN_BEFORE_END : no new entries in the last 120 min of the session.
  * NO_PROGRESS exit : if after NO_PROGRESS_CANDLES candles the trade never reached +NO_PROGRESS_MIN_R,
    exit at that candle's close (dead trades only pay fees).
  * mfe_r / mae_r columns in the trades CSV (best / worst excursion in R, approximate: uses candle high/low)
    so time stops and stops can be tuned from data later. Also "setup" and "risk_pct" columns.

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


# ----------------------------- CONFIG -----------------------------
RUN_SESSIONS = ["NY", "LONDON"]
CANDLE_MIN = 5
ORB_CANDLES = 3              # 3 x 5m = 15 min
VOL_MULT = 2.0
VOL_LOOKBACK = 20
CLOSE_EXTREME_PCT = 0.25     # UP breakout: close in top 25% of candle range; DOWN: bottom 25%
BODY_MIN = 0.4               # breakout candle body must be >= 40% of its range
# Both sessions use the same fake-breakout logic and the same "max 1 trade per pair per day" rule.
SESSION_RULES = {
    "NY":     {"body_min": BODY_MIN, "one_trade_per_day": True},
    "LONDON": {"body_min": BODY_MIN, "one_trade_per_day": True},
}
TARGET_R = 4.0
FEE_PCT_PER_SIDE = 0.05      # taker fee estimate, % of notional per side
MIN_SESSION_FILL = 0.95      # share of expected candles a session needs to count as complete

# --- Fake breakout settings ---
FAKE_WINDOW_CANDLES = 3          # confirming candle must close back inside within this many candles (3 x 5m = 15 min)
FAKE_CONFIRM_BODY_MIN = BODY_MIN # confirming candle body must be >= this share of its range
FAKE_NEED_OPPOSITE_COLOR = True  # confirming candle must be red (fake UP) / green (fake DOWN)
FAKE_SL_BUFFER = 0.001           # stop = spike extreme +/- 0.1%
MIN_RISK_PCT = 0.8               # skip if stop distance < 0.8% of entry (fees + noise dominate)
MAX_RISK_PCT = 3.0               # skip if stop distance > 3% of entry (30x leverage liquidates near 3.3%)

# --- Session-end / dead-trade handling ---
ENTRY_CUTOFF_MIN_BEFORE_END = 120   # no NEW entries in the last 120 min of the session
MAX_HOLD_CANDLES = 24               # 24 x 5m = 2 hours
TIME_STOP_MIN_R = 1.0               # time stop if still below this R (at close) after MAX_HOLD_CANDLES
NO_PROGRESS_CANDLES = 12            # 12 x 5m = 60 min; set 0 to disable
NO_PROGRESS_MIN_R = 0.5             # exit if best excursion (MFE) is still below this R after NO_PROGRESS_CANDLES
PROTECT_MIN_BEFORE_END = 45         # in the last 45 min a profitable trade gets its SL moved up; 0 disables
PROTECT_MIN_R = 0.5
PROTECT_LOCK_R = 0.0                # 0.0 = breakeven
# -------------------------------------------------------------------

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
    """Close a paper position: build the trade row, store it, alert, and remove the position."""
    entry, risk = float(pos["entry"]), float(pos["risk"])
    trade = _close_trade(pos["side"], entry, risk, exit_price, pos["t0"], exit_time, reason)
    per_r_usdt = risk * (NOTIONAL_USDT / entry)
    trade.update({
        "pair": pair, "session": session, "margin_usdt": MARGIN_USDT, "leverage": LEVERAGE,
        "notional_usdt": NOTIONAL_USDT,
        "pnl_usdt_gross": trade["r_gross"] * per_r_usdt,
        "pnl_usdt_net": trade["r_net"] * per_r_usdt,
        "setup": pos.get("setup"),
        "risk_pct": risk / entry * 100,
        "mfe_r": round(float(pos.get("mfe_r", 0.0)), 3),
        "mae_r": round(float(pos.get("mae_r", 0.0)), 3),
    })
    state["trades"].append(trade)
    send_telegram(f"COINDCX PART-03 FAKE_BREAK_OUT {session} {pos['side']} PAPER EXIT\n{pair}\nExit: {exit_price}\n"
                  f"Reason: {reason}\nNet PnL est.: {trade['pnl_usdt_net']:.4f} USDT")
    state["positions"].pop(key, None)
    return trade


def live_process_pair(pair, state, session, data=None, now=None):
    # Pull enough candles for prior-session PDH and the rolling 20-bar volume baseline.
    if data is None:
        data = fetch_coindcx_candles(pair=pair, interval="5", limit_days=4)
    if data.empty:
        return
    d = data.copy()
    d["vol_avg"] = d["volume"].rolling(VOL_LOOKBACK, min_periods=VOL_LOOKBACK).mean().shift(1)
    # For live mode, today's session is intentionally partial; only the prior
    # session must pass the complete-session validation.
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

    # At/after the session close, flatten any remaining paper position at the
    # final available session candle close; do not create late entries.
    if local_now.hour * 60 + local_now.minute >= _hm(cfg["end"]):
        pending.pop(key, None)
        if pos is not None:
            last = g.iloc[-1]
            _record_exit(state, key, pair, session, pos, float(last["close"]), g.index[-1].isoformat(), "SESSION_END")
        return

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
        upto = g.loc[:ts]
        run_high = float(upto["high"].max())
        left = mins_to_session_end(ts, session)

        # ------------------------- no open position: fake-breakout engine -------------------------
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
            va = bar["vol_avg"]
            spike = (not pd.isna(va)) and v >= VOL_MULT * float(va)

            # (1) is this candle the confirmation of an armed fake breakout?
            if pend:
                up = pend["dir"] == "UP"
                pend["bars"] += 1
                pend["extreme"] = max(float(pend["extreme"]), h) if up else min(float(pend["extreme"]), l)
                back_inside = (c < float(pend["level"])) if up else (c > float(pend["level"]))
                color_ok = (c < o) if up else (c > o)
                confirmed = (back_inside and body_ratio >= FAKE_CONFIRM_BODY_MIN
                             and (color_ok or not FAKE_NEED_OPPOSITE_COLOR))
                if confirmed:
                    extreme = float(pend["extreme"])
                    if up:       # fake UP breakout -> SHORT, stop above the spike high
                        side, setup, sl = "SHORT", "FAKE_UP_BREAKOUT", extreme * (1 + FAKE_SL_BUFFER)
                        risk = sl - c
                    else:        # fake DOWN breakout -> LONG, stop below the spike low
                        side, setup, sl = "LONG", "FAKE_DOWN_BREAKOUT", extreme * (1 - FAKE_SL_BUFFER)
                        risk = c - sl
                    risk_pct = risk / c * 100 if risk > 0 else 0.0
                    if left <= ENTRY_CUTOFF_MIN_BEFORE_END:
                        print(f"[{session}] {pair} fake breakout confirmed but too late in session - skipped")
                    elif not (MIN_RISK_PCT <= risk_pct <= MAX_RISK_PCT):
                        print(f"[{session}] {pair} fake breakout skipped: risk {risk_pct:.2f}% outside "
                              f"{MIN_RISK_PCT}-{MAX_RISK_PCT}%")
                    else:
                        tp = c + TARGET_R * risk if side == "LONG" else c - TARGET_R * risk
                        pos = {"side": side, "setup": setup, "entry": c, "sl": float(sl), "risk": float(risk),
                               "tp": float(tp), "t0": stamp, "entry_bar": stamp, "pdh": pdh,
                               "orb_high": orb_high, "orb_low": orb_low,
                               "level": float(pend["level"]), "spike_extreme": extreme,
                               "margin": MARGIN_USDT, "leverage": LEVERAGE, "notional": NOTIONAL_USDT,
                               "session": session, "mfe_r": 0.0, "mae_r": 0.0}
                        state["positions"][key] = pos
                        traded_days[key] = today_iso
                        qty = NOTIONAL_USDT / c
                        send_telegram(f"COINDCX PART-03 FAKE_BREAK_OUT {session} {side} PAPER ({setup})\n{pair}\n"
                                      f"Entry: {c}\nSL: {sl}\nTP: {tp}\nLevel: {pend['level']}\n"
                                      f"Spike extreme: {extreme}\nRisk: {risk_pct:.2f}%\n"
                                      f"Margin: {MARGIN_USDT} USDT | Leverage: {LEVERAGE}x | Notional: {NOTIONAL_USDT} USDT\n"
                                      f"Qty: {qty:.8f}")
                    pending.pop(key, None)
                    pend = None
                elif pend["bars"] >= FAKE_WINDOW_CANDLES:
                    print(f"[{session}] {pair} breakout held for {FAKE_WINDOW_CANDLES} candles - treated as real, no trade")
                    pending.pop(key, None)
                    pend = None
                else:
                    pending[key] = pend

            # (2) is this candle itself a new breakout candle? (setup detection identical to PART-02)
            if pos is None:
                strong = body_ratio >= rules["body_min"]
                if strong and spike:
                    long_ok = c > max(orb_high, pdh) and c > o and (c - l) / rng >= 1 - CLOSE_EXTREME_PCT
                    short_ok = (run_high >= pdh * 0.999 and c < orb_low and run_high < pdh * 1.01
                                and c < o and (h - c) / rng >= 1 - CLOSE_EXTREME_PCT)
                    new_dir = "UP" if long_ok else ("DOWN" if short_ok else None)
                    if new_dir:
                        level = max(orb_high, pdh) if new_dir == "UP" else orb_low
                        old = pending.get(key)
                        if old and old["dir"] == new_dir:      # same-direction repeat: keep the spike extreme
                            extreme = max(float(old["extreme"]), h) if new_dir == "UP" else min(float(old["extreme"]), l)
                        else:
                            extreme = h if new_dir == "UP" else l
                        pending[key] = {"dir": new_dir, "level": float(level), "extreme": float(extreme),
                                        "bars": 0, "t_break": stamp, "day": today_iso}
                        print(f"[{session}] {pair} {new_dir} breakout candle at {stamp} - watching for a fake "
                              f"(level {level})")
            state["last_bar"][key] = stamp
            last_seen = stamp
            continue

        # ------------------------- open position management -------------------------
        side, entry, risk, sl, tp = pos["side"], float(pos["entry"]), float(pos["risk"]), float(pos["sl"]), float(pos["tp"])
        is_long = side == "LONG"
        fav = ((h - entry) if is_long else (entry - l)) / risk      # best excursion this candle, in R
        adv = ((entry - l) if is_long else (h - entry)) / risk      # worst excursion this candle, in R
        pos["mfe_r"] = max(float(pos.get("mfe_r", 0.0)), fav)
        pos["mae_r"] = max(float(pos.get("mae_r", 0.0)), adv)

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
            exit_reason = None
            if NO_PROGRESS_CANDLES and held >= NO_PROGRESS_CANDLES and float(pos["mfe_r"]) < NO_PROGRESS_MIN_R:
                exit_reason = "NO_PROGRESS"
            elif held >= MAX_HOLD_CANDLES and close_r < TIME_STOP_MIN_R:
                exit_reason = "TIME_STOP"
            if exit_reason:
                _record_exit(state, key, pair, session, pos, c, stamp, exit_reason)
                pos = None
                state["last_bar"][key] = stamp
                last_seen = stamp
                continue
            new_sl = sl
            if fav >= 3.0:
                new_sl = entry + 3.0 * risk if is_long else entry - 3.0 * risk
            elif fav >= 2.0:
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
    ap = argparse.ArgumentParser(description="CoinDCX FAKE BREAKOUT (ORB + PDH/PDL) NY/London session PAPER trading (no real orders)")
    ap.add_argument("--mode", default="live-paper", choices=["live-paper"],
                    help="Run mode (only live-paper exists; the GitHub workflow passes --mode live-paper)")
    ap.parse_args()
    run_live_paper()
