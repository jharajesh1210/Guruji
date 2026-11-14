"""
CoinDCX Futures Advanced Liquidity Sweep + ORB + PDH/PDL (Long & Short) -- PART-06
-------------------------------------------------------------------------------------
Updates in PART-06:
1. Version & Artifact naming updated to PART-06.
2. Liquidity Sweep (Fakeout) Filter: Rejects blind breakouts; requires PDH/PDL sweep & rejection.
3. Enhanced Displacement & Volume Filter: Strict body-to-range ratio and volume spikes.
4. Fixed-Risk Sizing & Margin Caps: Controlled USDT risk per trade with leverage boundaries.
5. Cluster Guard & Session Loss Breaker: Prevents over-leveraging same-bar spikes and stops on max daily loss.

Review fixes applied on top of the Gemini draft (the draft compiled but almost never traded):
a) Sweep detection is now multi-candle. The draft needed ONE candle that wicked under ORB-low/PDL AND closed above
   the PDH (or the mirror for shorts), i.e. a giant V-shaped bar; a normal "wick under the level, close back inside,
   then displacement candle" setup never fired. Now: within the last SWEEP_LOOKBACK closed candles of the session
   (after the ORB) a candle must have wicked beyond ORB-low / PDL (or ORB-high / PDH for shorts) AND closed back
   on the right side of that level (rejection). The current displacement candle (strong body, volume spike, close at
   the extreme) must then break the opposite side as before. SL = swing extreme of the sweep window (+0.1% buffer).
b) TIME_STOP was configured but never used -> restored (MAX_HOLD_CANDLES / TIME_STOP_MIN_R).
c) Exit reasons: a locked-profit stop was logged as "SL" -> BREAKEVEN / LOCKED_PROFIT labels restored.
d) Profit protection in the last PROTECT_MIN_BEFORE_END minutes of a session restored (PART-04 behaviour).
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
    """Active USDT perpetual futures with LTP > $1 and 24h volume > $10m."""
    try:
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
    except Exception as e:
        print(f"[UNIVERSE ERROR] Could not fetch eligible pairs: {e}")
        return []


# ----------------------------- CONFIG -----------------------------
CANDLE_MIN = 5
ORB_CANDLES = 3              # 3 x 5m = 15 min opening range
VOL_MULT = 2.2               # stricter volume spike requirement
VOL_LOOKBACK = 20
CLOSE_EXTREME_PCT = 0.20     # close in top/bottom 20% of candle range
BODY_MIN = 0.65              # candle body must be >= 65% of its range (strong momentum)
TARGET_R = 4.0
SHORT_SL_BUFFER = 0.001
FEE_PCT_PER_SIDE = 0.05      # taker fee estimate %

# --- Session-end handling ---
ENTRY_CUTOFF_MIN_BEFORE_END = 90
MAX_HOLD_CANDLES = 24
TIME_STOP_MIN_R = 1.0

# --- Risk Management ---
RISK_PER_TRADE_USDT = 15.0   
MARGIN_USDT = 100.0
LEVERAGE = 30.0
MAX_NOTIONAL_USDT = MARGIN_USDT * LEVERAGE

MIN_RISK_PCT = 0.5
MAX_RISK_PCT = 2.0

MAX_OPEN_PER_SESSION = 3
MAX_ENTRIES_PER_SESSION_DAY = 6
MAX_SAME_DIR_PER_BAR = 1     # strict cluster guard
SESSION_DAY_LOSS_LIMIT_USDT = 45.0

SWEEP_LOOKBACK = 6           # closed candles (incl. the signal candle) in which the liquidity sweep must have happened
PROTECT_MIN_BEFORE_END = 45  # in the last N min of a session, lock profit on winners (0 = disable)
PROTECT_MIN_R = 0.5
PROTECT_LOCK_R = 0.0         # 0.0 = breakeven
# -------------------------------------------------------------------

LONDON_TZ = ZoneInfo("Europe/London")
NY_TZ = ZoneInfo("America/New_York")
TOKYO_TZ = ZoneInfo("Asia/Tokyo")

SESSION_CONFIG = {
    "NY": {"tz": NY_TZ, "start": "09:30", "end": "16:00", "limits": True},
    "LONDON": {"tz": LONDON_TZ, "start": "08:00", "end": "15:30", "limits": True},
    "ASIA": {"tz": TOKYO_TZ, "start": "09:00", "end": "15:30", "limits": True},
}

STATE_FILE = "orb_pdh_part6_state.json"
TRADES_FILE = "orb_pdh_part6_trades.csv"


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
        raise RuntimeError("CoinDCX data fetch failed.")

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
            if len(group) >= int(expected * 0.95)}


def _close_trade(side, entry, risk, exit_price, entry_time, exit_time, reason):
    gross_r = ((exit_price - entry) if side == "LONG" else (entry - exit_price)) / risk
    fee_r = (2 * FEE_PCT_PER_SIDE / 100 * entry) / risk
    return {
        "entry_time": entry_time, "exit_time": exit_time, "side": side,
        "entry": entry, "exit": exit_price, "risk": risk,
        "r_gross": gross_r, "r_net": gross_r - fee_r, "reason": reason,
    }


def position_notional(entry, risk):
    return min(RISK_PER_TRADE_USDT / (risk / entry), MAX_NOTIONAL_USDT)


def record_trade(state, trade, pair, session, notional):
    entry, risk = float(trade["entry"]), float(trade["risk"])
    usdt_per_r = risk * (notional / entry)
    trade.update({
        "pair": pair, "session": session,
        "margin_usdt": notional / LEVERAGE, "leverage": LEVERAGE, "notional_usdt": notional,
        "risk_pct": risk / entry * 100,
        "pnl_usdt_gross": trade["r_gross"] * usdt_per_r,
        "pnl_usdt_net": trade["r_net"] * usdt_per_r,
    })
    state["trades"].append(trade)
    local_day = pd.Timestamp(trade["entry_time"]).tz_convert(SESSION_CONFIG[session]["tz"]).date()
    day_key = f"{session}:{local_day}"
    state["day_pnl"][day_key] = state["day_pnl"].get(day_key, 0.0) + float(trade["pnl_usdt_net"])
    return trade


def mins_to_session_end(ts, session):
    cfg = SESSION_CONFIG[session]
    lt = ts.tz_convert(cfg["tz"])
    return _hm(cfg["end"]) - (lt.hour * 60 + lt.minute)


def send_telegram(message):
    message = f"[PART-06] {message}"   # every alert starts with the bot name
    token = os.getenv("TG_TOKEN") or os.getenv("BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID") or os.getenv("CHAT_ID")
    if not token or not chat_id:
        print("[TELEGRAM NOT CONFIGURED]")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message},
            timeout=20,
        )
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"positions": {}, "last_bar": {}, "trades": [], "entries": {}, "bar_entries": {}, "day_pnl": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("positions", {})
    state.setdefault("last_bar", {})
    state.setdefault("trades", [])
    state.setdefault("entries", {})
    state.setdefault("bar_entries", {})
    state.setdefault("day_pnl", {})
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)
    try:
        pd.DataFrame(state.get("trades", [])).to_csv(TRADES_FILE, index=False)
    except Exception:
        pass


def live_process_pair(pair, state, session, data=None):
    if data is None:
        data = fetch_coindcx_candles(pair=pair, interval="5", limit_days=4)
    if data.empty:
        return
    d = data.copy()
    d["vol_avg"] = d["volume"].rolling(VOL_LOOKBACK, min_periods=VOL_LOOKBACK).mean().shift(1)
    
    now = datetime.now(timezone.utc)
    cfg = SESSION_CONFIG[session]
    local_now = now.astimezone(cfg["tz"])
    today = local_now.date()
    
    complete_prior = complete_sessions(d[d.index.tz_convert(cfg["tz"]).date < today], session)
    prev_days = sorted(day for day in complete_prior if day < today)
    if not prev_days:
        return
        
    g = session_day(d, session, today)
    if g.empty:
        return
        
    prev = complete_prior[prev_days[-1]]
    pdh = float(prev["high"].max())
    pdl = float(prev["low"].min())
    
    orb = g.iloc[:ORB_CANDLES]
    if len(orb) < ORB_CANDLES:
        return
        
    orb_high, orb_low = float(orb["high"].max()), float(orb["low"].min())
    key = f"{session}:{pair}"
    pos = state["positions"].get(key)
    last_seen = state["last_bar"].get(key)
    
    if local_now.hour * 60 + local_now.minute >= _hm(cfg["end"]):
        if pos is not None:
            last = g.iloc[-1]
            exit_price = float(last["close"])
            trade = _close_trade(pos["side"], float(pos["entry"]), float(pos["risk"]),
                                 exit_price, pos["t0"], g.index[-1].isoformat(), "SESSION_END")
            record_trade(state, trade, pair, session, float(pos.get("notional", MAX_NOTIONAL_USDT)))
            send_telegram(f"COINDCX {session} {pos['side']} PAPER EXIT\n{pair}\nExit: {exit_price}\nReason: SESSION_END\nNet PnL: {trade['pnl_usdt_net']:.4f} USDT")
            state["positions"].pop(key, None)
        return

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
        run_low = float(upto["low"].min())
        left = mins_to_session_end(ts, session)
        
        if pos is None:
            rng = h - l
            strong = rng > 0 and abs(c - o) / rng >= BODY_MIN
            va = bar["vol_avg"]
            spike = (not pd.isna(va)) and v >= VOL_MULT * float(va)
            
            if strong and spike and left > ENTRY_CUTOFF_MIN_BEFORE_END:
                # Liquidity sweep: a recent candle wicked beyond a liquidity level and closed back (rejection).
                win = upto.iloc[ORB_CANDLES:].tail(SWEEP_LOOKBACK)
                low_mask = ((win["low"] < orb_low) & (win["close"] > orb_low)) | ((win["low"] < pdl) & (win["close"] > pdl))
                high_mask = ((win["high"] > orb_high) & (win["close"] < orb_high)) | ((win["high"] > pdh) & (win["close"] < pdh))
                swept_low, swept_high = bool(low_mask.any()), bool(high_mask.any())

                long_ok = swept_low and c > max(orb_high, pdh) and c > o and (c - l) / rng >= (1 - CLOSE_EXTREME_PCT)
                short_ok = swept_high and c < orb_low and run_high < pdh * 1.01 and c < o and (h - c) / rng >= (1 - CLOSE_EXTREME_PCT)

                side = None
                if long_ok:
                    first = low_mask[low_mask].index[0]
                    sl = float(win.loc[first:, "low"].min()) * (1 - SHORT_SL_BUFFER)
                    risk = c - sl
                    if risk > 0:
                        side = "LONG"
                elif short_ok:
                    first = high_mask[high_mask].index[0]
                    sl = float(win.loc[first:, "high"].max()) * (1 + SHORT_SL_BUFFER)
                    risk = sl - c
                    if risk > 0:
                        side = "SHORT"

                if side and cfg.get("limits"):
                    risk_pct = risk / c * 100
                    day_key = f"{session}:{today}"
                    opened_today = state["entries"].get(day_key, 0)
                    open_now = sum(1 for p in state["positions"].values() if p.get("session") == session)
                    bar_key = f"{session}:{stamp}:{side}"
                    same_bar_dir = state["bar_entries"].get(bar_key, 0)
                    day_loss = state["day_pnl"].get(day_key, 0.0)
                    
                    if (not (MIN_RISK_PCT <= risk_pct <= MAX_RISK_PCT)
                            or open_now >= MAX_OPEN_PER_SESSION
                            or opened_today >= MAX_ENTRIES_PER_SESSION_DAY
                            or same_bar_dir >= MAX_SAME_DIR_PER_BAR
                            or day_loss <= -SESSION_DAY_LOSS_LIMIT_USDT):
                        side = None
                        
                if side:
                    notional = position_notional(c, risk)
                    pos = {
                        "side": side, "entry": c, "sl": float(sl), "risk": float(risk),
                        "tp": float(c + TARGET_R * risk if side == "LONG" else c - TARGET_R * risk),
                        "t0": stamp, "entry_bar": stamp, "pdh": pdh, "orb_high": orb_high,
                        "orb_low": orb_low, "margin": notional / LEVERAGE, "leverage": LEVERAGE,
                        "notional": notional, "session": session
                    }
                    state["positions"][key] = pos
                    day_key = f"{session}:{today}"
                    state["entries"][day_key] = state["entries"].get(day_key, 0) + 1
                    bar_key = f"{session}:{stamp}:{side}"
                    state["bar_entries"][bar_key] = state["bar_entries"].get(bar_key, 0) + 1
                    qty = notional / c
                    send_telegram(f"COINDCX {session} SWEEP-ORB {side} PAPER\n{pair}\nEntry: {c}\nSL: {sl}\nTP: {pos['tp']}\nRisk: {RISK_PER_TRADE_USDT} USDT ({risk/c*100:.2f}%)")
                    
        else:
            is_long = pos["side"] == "LONG"
            entry, risk, sl, tp = float(pos["entry"]), float(pos["risk"]), float(pos["sl"]), float(pos["tp"])
            notional = float(pos.get("notional", MAX_NOTIONAL_USDT))

            hit_sl = (l <= sl) if is_long else (h >= sl)
            hit_tp = (h >= tp) if is_long else (l <= tp)

            if hit_sl or hit_tp:
                exit_price = sl if hit_sl else tp
                reason = "TARGET_4R" if not hit_sl else ("BREAKEVEN" if sl == entry else ("LOCKED_PROFIT" if ((sl > entry) if is_long else (sl < entry)) else "SL"))
                trade = _close_trade(pos["side"], entry, risk, exit_price, pos["t0"], stamp, reason)
                record_trade(state, trade, pair, session, notional)
                send_telegram(f"COINDCX {session} {pos['side']} EXIT\n{pair}\nReason: {reason}\nNet PnL: {trade['pnl_usdt_net']:.4f} USDT")
                state["positions"].pop(key, None)
            else:
                close_r = ((c - entry) if is_long else (entry - c)) / risk
                held = int((ts - pd.Timestamp(pos["entry_bar"])).total_seconds() // (CANDLE_MIN * 60))
                if held >= MAX_HOLD_CANDLES and close_r < TIME_STOP_MIN_R:
                    trade = _close_trade(pos["side"], entry, risk, c, pos["t0"], stamp, "TIME_STOP")
                    record_trade(state, trade, pair, session, notional)
                    send_telegram(f"COINDCX {session} {pos['side']} EXIT\n{pair}\nReason: TIME_STOP\nNet PnL: {trade['pnl_usdt_net']:.4f} USDT")
                    state["positions"].pop(key, None)
                else:
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
        print("[WARNING] No eligible pairs found. Retrying next cycle.")
        return
    print(f"PART-06 Advanced Sweep-Scan: {len(universe)} pairs loaded.")
    for i, coin in enumerate(universe, 1):
        pair = coin["pair"]
        try:
            data = fetch_coindcx_candles(pair=pair, interval="5", limit_days=4)
            for session in SESSION_CONFIG:
                live_process_pair(pair, state, session, data=data)
        except Exception as exc:
            print(f"[ERROR {pair}]: {exc}")
        time.sleep(0.2)
    save_state(state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="live-paper")
    parser.parse_args()
    run_live_paper()