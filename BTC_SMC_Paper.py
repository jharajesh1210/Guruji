# ============================================================
# BTC_SMC_Paper.py
# V24 - COINDCX BTC USDT FUTURES - PAPER TRADING ONLY
# 5 MIN ENTRY + 1 HOUR STRUCTURE
# LIQUIDITY SWEEP + CHOCH + FVG RETEST
#
# V23 -> V24 FIXES (loss reduce karne ke liye):
#  1. Fee/slippage filter: trade tabhi jab cost <= MAX_COST_R
#  2. Risk range realistic: 0.30% - 1.20% (pehle 0.05% - 2.5%)
#  3. Stop ke liye buffer (wick pe exact stop = stop hunt)
#  4. 5m sweep ke liye bada swing (6) - micro swing noise hata
#  5. 1H bias + EMA50 confirm (range/chop mein trade kam)
#  6. Session filter (London + NY hours, UTC)
#  7. Loss ke baad cooldown
#  8. Trailing: 1.5R -> BE+, 2R -> lock 1R, 3R -> lock 2R, 4R exit
#  9. Stale candle pe naya entry nahi (catch-up ke time)
# 10. Stats R-multiple aur USDT mein (leverage ke saath)
# 11. "Too many missed candles" pe bot permanently atakta tha - fixed
# ============================================================

import os
import json
import time
from pathlib import Path
from datetime import datetime, timezone

import requests
import pandas as pd


# =========================
# 1. SETTINGS
# =========================

PAIR = "B-BTC_USDT"

FUTURES_API = "https://public.coindcx.com/market_data/candlesticks"

STATE_FILE = Path("BTC_SMC_Paper_state.json")
TRADES_FILE = Path("BTC_SMC_Paper_trades.csv")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

SWING_LENGTH = 3          # 1H structure swings
SWEEP_SWING_LENGTH = 6    # 5m liquidity swings (bada = real liquidity)
SETUP_EXPIRY = 18         # candles
FVG_EXPIRY = 12           # candles

TARGET_RR = 4
# (reach R, lock R): price reach_R tak aaye to stop lock_R pe shift
TRAIL_RULES = [(1.5, 0.2), (2.0, 1.0), (3.0, 2.0)]

MARGIN_USDT = 100.0
LEVERAGE = 30

# Costs (price % ke roop mein). Apne exchange ke actual fee se check karo.
ROUND_TRIP_FEE = 0.001
SLIPPAGE_PER_SIDE = 0.0002

STOP_BUFFER_PCT = 0.05    # sweep extreme ke bahar extra buffer (%)
MIN_RISK_PERCENT = 0.30
MAX_RISK_PERCENT = 1.20
MAX_COST_R = 0.25         # fee+slippage risk ka max 25% ho

H1_EMA = 50
TRADE_HOURS_UTC = (7, 20)  # start inclusive, end exclusive
COOLDOWN_BARS = 12         # loss ke baad 12 x 5m = 1 ghanta

MAX_ENTRY_LAG_BARS = 2     # purani candle pe naya entry nahi
MAX_CATCHUP = 288          # isse bada gap: no position = fast-forward, position = sab process

CANDLE_5M_MS = 5 * 60 * 1000
CANDLE_1H_MS = 60 * 60 * 1000

COST_PCT = (ROUND_TRIP_FEE + 2 * SLIPPAGE_PER_SIDE) * 100


# =========================
# 2. TELEGRAM
# =========================

def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram not configured")
        print(message)
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            json={"chat_id": CHAT_ID, "text": message},
            timeout=20
        )
        response.raise_for_status()
        print("Telegram message sent")
    except requests.RequestException as error:
        print("Telegram error:", str(error))


# =========================
# 3. COINDCX FUTURES DATA
# =========================

def get_candles(resolution, days):
    now_seconds = int(time.time())
    start_seconds = now_seconds - days * 86400

    if resolution == "5":
        chunk_seconds = 2 * 86400
        candle_ms = CANDLE_5M_MS
    elif resolution == "60":
        chunk_seconds = 10 * 86400
        candle_ms = CANDLE_1H_MS
    else:
        raise ValueError("Unsupported resolution")

    all_rows = []

    for left in range(start_seconds, now_seconds, chunk_seconds):
        right = min(left + chunk_seconds, now_seconds)

        params = {
            "pair": PAIR,
            "from": int(left),
            "to": int(right),
            "resolution": resolution,
            "pcode": "f"
        }

        response = requests.get(FUTURES_API, params=params, timeout=30)

        if response.status_code != 200:
            print("CoinDCX API error:", response.status_code,
                  response.text[:500])

        response.raise_for_status()
        result = response.json()

        if not isinstance(result, dict):
            raise RuntimeError("Unexpected Futures API response")
        if result.get("s") != "ok":
            raise RuntimeError("CoinDCX Futures API: " + str(result)[:500])

        payload = result.get("data", [])
        if not isinstance(payload, list):
            raise RuntimeError("Invalid Futures candle payload")

        all_rows.extend(payload)
        time.sleep(0.2)

    if not all_rows:
        raise RuntimeError("No CoinDCX Futures candles received")

    df = pd.DataFrame(all_rows)

    required = ["time", "open", "high", "low", "close"]

    for column in required:
        if column not in df.columns:
            raise RuntimeError("Missing candle column: " + column)
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df = df.dropna(subset=required)

    # seconds ya milliseconds dono support
    df["time"] = df["time"].where(df["time"] > 1e11, df["time"] * 1000)

    df = (
        df.drop_duplicates("time")
        .sort_values("time")
        .reset_index(drop=True)
    )

    # sirf poori band candles
    now_ms = int(time.time() * 1000)
    df = df[df["time"] + candle_ms <= now_ms].reset_index(drop=True)

    minimum = 100 if resolution == "5" else max(30, H1_EMA)

    if len(df) < minimum:
        raise RuntimeError(
            f"Insufficient {resolution} candles: {len(df)}"
        )

    print(f"Futures {resolution} candles loaded:", len(df))
    return df


# =========================
# 4. SWING HIGHS / LOWS
# =========================

def find_swings(df, length=SWING_LENGTH):
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)

    swing_highs = []
    swing_lows = []

    for i in range(length, len(df) - length):
        high_window = highs[i - length:i + length + 1]
        low_window = lows[i - length:i + length + 1]

        if highs[i] == high_window.max() and (high_window == highs[i]).sum() == 1:
            swing_highs.append((i, float(highs[i])))

        if lows[i] == low_window.min() and (low_window == lows[i]).sum() == 1:
            swing_lows.append((i, float(lows[i])))

    return swing_highs, swing_lows


# =========================
# 5. ONE HOUR MARKET STRUCTURE (+ EMA confirm)
# =========================

def get_h1_bias(df):
    highs, lows = find_swings(df, SWING_LENGTH)

    if len(highs) < 2 or len(lows) < 2 or len(df) < H1_EMA:
        return "NEUTRAL"

    ema = df["close"].ewm(span=H1_EMA, adjust=False).mean().iloc[-1]
    last_close = float(df["close"].iloc[-1])

    higher_high = highs[-1][1] > highs[-2][1]
    higher_low = lows[-1][1] > lows[-2][1]
    lower_high = highs[-1][1] < highs[-2][1]
    lower_low = lows[-1][1] < lows[-2][1]

    if higher_high and higher_low and last_close > ema:
        return "LONG"

    if lower_high and lower_low and last_close < ema:
        return "SHORT"

    return "NEUTRAL"


# =========================
# 6. PAPER STATE
# =========================

def new_state():
    return {
        "last_bar": 0,
        "setup": None,
        "positions": [],
        "closed": 0,
        "wins": 0,
        "losses": 0,
        "net_pct": 0.0,
        "r_sum": 0.0,
        "pnl_usdt": 0.0,
        "cooldown_until": 0
    }


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
        # purane state file mein naye keys add karo
        for key, value in new_state().items():
            state.setdefault(key, value)
        return state

    return new_state()


def save_state(state):
    temp_file = STATE_FILE.with_suffix(".tmp")

    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)

    temp_file.replace(STATE_FILE)


# =========================
# 7. CSV TRADE HISTORY
# =========================

def save_trade(trade):
    columns = [
        "time_utc", "side", "rr", "entry", "stop", "initial_stop",
        "target", "exit", "reason", "gross_pct", "net_pct",
        "r_multiple", "pnl_usdt", "risk_pct", "margin_usdt",
        "trade_mode", "max_rr_reached"
    ]

    row = {column: trade.get(column, "") for column in columns}

    pd.DataFrame([row]).to_csv(
        TRADES_FILE,
        mode="a",
        header=not TRADES_FILE.exists(),
        index=False
    )


# =========================
# 8. PAPER TRADE EXIT
# =========================

def check_exits(state, bar):
    """Ek full position. Milestones sirf stop shift karte hain (no partial).

    Same candle mein stop aur target dono touch ho to stop jeetta hai.
    Naya trail stop agli completed candle se active hota hai.
    """
    remaining = []

    for position in state["positions"]:
        if bar["time"] <= position["entry_time"]:
            remaining.append(position)
            continue

        is_long = position["side"] == "LONG"
        direction = 1 if is_long else -1
        high, low = float(bar["high"]), float(bar["low"])
        entry, stop = float(position["entry"]), float(position["stop"])
        target = float(position["target"])
        is_trail_mode = position.get("trade_mode") == "SINGLE_TRAIL_4R"

        stop_hit = low <= stop if is_long else high >= stop
        target_hit = high >= target if is_long else low <= target
        exit_price, reason = None, None

        if stop_hit:
            exit_price = stop
            moved = float(position.get("max_rr_reached", 0)) > 0
            reason = "TRAIL STOP" if (is_trail_mode and moved) else "STOP LOSS"
        elif target_hit:
            exit_price = target
            reason = f"FINAL TARGET {TARGET_RR}R" if is_trail_mode else "TARGET HIT"
        elif is_trail_mode:
            risk = float(position["initial_risk"])
            reached = float(position.get("max_rr_reached", 0))

            for reach_rr, lock_rr in sorted(TRAIL_RULES, reverse=True):
                milestone = entry + direction * reach_rr * risk
                touched = high >= milestone if is_long else low <= milestone

                if touched and reach_rr > reached:
                    new_stop = entry + direction * lock_rr * risk
                    position["stop"] = new_stop
                    position["max_rr_reached"] = reach_rr

                    alert = (
                        "BTC SMC PAPER - TRAILING STOP UPDATED\n"
                        f"Side: {position['side']}\n"
                        f"Reached: {reach_rr}R\n"
                        f"New Stop Loss: {new_stop:.2f} (locks {lock_rr}R)\n"
                        f"Final Target {TARGET_RR}R: {target:.2f}\n"
                        "PAPER TRADING ONLY"
                    )
                    print(alert)
                    send_telegram(alert)
                    break

        if exit_price is None:
            remaining.append(position)
            continue

        gross_pct = (exit_price / entry - 1) * 100 * direction
        net_pct = gross_pct - COST_PCT

        initial_risk = float(position.get("initial_risk", abs(entry - stop)))
        risk_pct = initial_risk / entry * 100
        r_multiple = net_pct / risk_pct if risk_pct > 0 else 0.0

        margin = float(position.get("margin_usdt", MARGIN_USDT))
        pnl_usdt = net_pct / 100 * margin * LEVERAGE

        state["closed"] += 1
        if net_pct > 0:
            state["wins"] += 1
        else:
            state["losses"] += 1
            state["cooldown_until"] = int(
                bar["time"] + COOLDOWN_BARS * CANDLE_5M_MS
            )

        state["net_pct"] = round(state["net_pct"] + net_pct, 6)
        state["r_sum"] = round(state["r_sum"] + r_multiple, 4)
        state["pnl_usdt"] = round(state["pnl_usdt"] + pnl_usdt, 4)

        timestamp = datetime.fromtimestamp(
            bar["time"] / 1000, timezone.utc
        ).isoformat()

        trade = {
            **position,
            "time_utc": timestamp,
            "exit": exit_price,
            "reason": reason,
            "gross_pct": round(gross_pct, 4),
            "net_pct": round(net_pct, 4),
            "r_multiple": round(r_multiple, 3),
            "pnl_usdt": round(pnl_usdt, 3),
            "risk_pct": round(risk_pct, 4),
            "margin_usdt": margin
        }
        save_trade(trade)

        closed = state["closed"]
        win_rate = state["wins"] / closed * 100

        message = (
            "BTC SMC PAPER - TRADE CLOSED\n"
            f"Side: {position['side']}\n"
            f"Result: {reason}\n"
            f"Entry: {entry:.2f}\n"
            f"Exit: {exit_price:.2f}\n"
            f"Net: {net_pct:+.3f}% | {r_multiple:+.2f}R | "
            f"{pnl_usdt:+.2f} USDT\n\n"
            f"Closed: {closed} | Wins: {state['wins']} | "
            f"Losses: {state['losses']}\n"
            f"Win Rate: {win_rate:.2f}%\n"
            f"Total R: {state['r_sum']:+.2f} | "
            f"Total PnL: {state['pnl_usdt']:+.2f} USDT\n"
            "PAPER TRADING ONLY"
        )
        print(message)
        send_telegram(message)

    state["positions"] = remaining


# =========================
# 9. LIQUIDITY SWEEP
# =========================

def detect_sweep(df, i, side):
    previous = df.iloc[:i]
    highs, lows = find_swings(previous, SWEEP_SWING_LENGTH)
    bar = df.iloc[i]

    if side == "LONG" and lows:
        swing_low = lows[-1][1]

        if bar["low"] < swing_low and bar["close"] > swing_low:
            return {
                "side": "LONG",
                "stage": "SWEEP",
                "sweep_time": int(bar["time"]),
                "extreme": float(bar["low"]),
                "break_level": highs[-1][1] if highs else None
            }

    if side == "SHORT" and highs:
        swing_high = highs[-1][1]

        if bar["high"] > swing_high and bar["close"] < swing_high:
            return {
                "side": "SHORT",
                "stage": "SWEEP",
                "sweep_time": int(bar["time"]),
                "extreme": float(bar["high"]),
                "break_level": lows[-1][1] if lows else None
            }

    return None


# =========================
# 10. SMC SETUP
# =========================

def in_session(bar_time_ms):
    hour = datetime.fromtimestamp(bar_time_ms / 1000, timezone.utc).hour
    return TRADE_HOURS_UTC[0] <= hour < TRADE_HOURS_UTC[1]


def process_smc(df, i, state, bias, fresh=True):
    bar = df.iloc[i]
    setup = state.get("setup")

    if setup:
        expired = (
            bar["time"] - setup["sweep_time"] > SETUP_EXPIRY * CANDLE_5M_MS
        )
        if expired or setup["side"] != bias:
            setup = None

    if setup:
        is_long = setup["side"] == "LONG"

        invalidated = (
            bar["low"] < setup["extreme"]
            if is_long
            else bar["high"] > setup["extreme"]
        )

        if invalidated:
            setup = None

        elif setup["stage"] == "SWEEP":
            level = setup["break_level"]

            if level is not None:
                choch = (
                    bar["close"] > level if is_long else bar["close"] < level
                )
                if choch:
                    setup["stage"] = "CHOCH"
                    setup["choch_time"] = int(bar["time"])
                    print("SMC CHOCH:", setup["side"])

        elif setup["stage"] == "CHOCH":
            if i >= 2:
                first = df.iloc[i - 2]

                bullish_fvg = bar["low"] > first["high"]
                bearish_fvg = bar["high"] < first["low"]

                if is_long and bullish_fvg:
                    setup.update({
                        "stage": "FVG",
                        "fvg_low": float(first["high"]),
                        "fvg_high": float(bar["low"]),
                        "fvg_time": int(bar["time"])
                    })
                elif not is_long and bearish_fvg:
                    setup.update({
                        "stage": "FVG",
                        "fvg_low": float(bar["high"]),
                        "fvg_high": float(first["low"]),
                        "fvg_time": int(bar["time"])
                    })

        elif setup["stage"] == "FVG":
            fvg_expired = (
                bar["time"] - setup["fvg_time"] > FVG_EXPIRY * CANDLE_5M_MS
            )

            if fvg_expired:
                setup = None
            else:
                touched = (
                    bar["low"] <= setup["fvg_high"]
                    and bar["high"] >= setup["fvg_low"]
                )

                if is_long:
                    rejection = (
                        bar["close"] > bar["open"]
                        and bar["close"] > setup["fvg_high"]
                    )
                else:
                    rejection = (
                        bar["close"] < bar["open"]
                        and bar["close"] < setup["fvg_low"]
                    )

                if touched and rejection:
                    entry = float(bar["close"])
                    buffer = entry * STOP_BUFFER_PCT / 100
                    extreme = float(setup["extreme"])
                    stop = extreme - buffer if is_long else extreme + buffer

                    risk = abs(entry - stop)
                    risk_percent = risk / entry * 100

                    valid_direction = stop < entry if is_long else stop > entry
                    valid_risk = (
                        MIN_RISK_PERCENT <= risk_percent <= MAX_RISK_PERCENT
                    )
                    cost_r = COST_PCT / risk_percent if risk_percent > 0 else 99
                    valid_cost = cost_r <= MAX_COST_R

                    allowed = (
                        fresh
                        and in_session(bar["time"])
                        and bar["time"] >= state.get("cooldown_until", 0)
                        and not state["positions"]
                    )

                    if valid_direction and valid_risk and valid_cost and allowed:
                        sign = 1 if is_long else -1
                        target = entry + sign * TARGET_RR * risk

                        position = {
                            "side": setup["side"],
                            "rr": TARGET_RR,
                            "entry": entry,
                            "stop": stop,
                            "initial_stop": stop,
                            "initial_risk": risk,
                            "target": target,
                            "entry_time": int(bar["time"]),
                            "trade_mode": "SINGLE_TRAIL_4R",
                            "max_rr_reached": 0,
                            "margin_usdt": MARGIN_USDT
                        }
                        state["positions"].append(position)

                        timestamp = datetime.fromtimestamp(
                            bar["time"] / 1000, timezone.utc
                        ).isoformat()

                        trail_text = " | ".join(
                            f"{r}R->lock {l}R" for r, l in TRAIL_RULES
                        )

                        message = (
                            "BTC SMC PAPER ENTRY\n"
                            f"Time UTC: {timestamp}\n"
                            f"Pair: {PAIR}\n"
                            f"Side: {setup['side']}\n"
                            f"1H Bias: {bias}\n"
                            "Setup: Sweep + CHOCH + FVG Retest\n"
                            f"Entry: {entry:.2f}\n"
                            f"Stop Loss: {stop:.2f} ({risk_percent:.2f}%)\n"
                            f"Cost: {cost_r:.2f}R\n"
                            f"Target {TARGET_RR}R: {target:.2f}\n"
                            f"Trail: {trail_text}\n"
                            f"Margin: ${MARGIN_USDT:.0f}, "
                            f"Leverage: {LEVERAGE}x\n"
                            "PAPER TRADING ONLY"
                        )
                        print(message)
                        send_telegram(message)
                    else:
                        print(
                            "ENTRY SKIPPED:",
                            f"risk={risk_percent:.3f}%",
                            f"costR={cost_r:.2f}",
                            f"fresh={fresh}",
                            f"session={in_session(bar['time'])}"
                        )

                    setup = None

    if (
        setup is None
        and bias in ("LONG", "SHORT")
        and not state["positions"]
        and in_session(bar["time"])
        and bar["time"] >= state.get("cooldown_until", 0)
    ):
        setup = detect_sweep(df, i, bias)
        if setup:
            print("Liquidity Sweep:", setup["side"])

    state["setup"] = setup


# =========================
# 11. MAIN SCANNER
# =========================

def main():
    print("=" * 55)
    print("BTC SMC PAPER - V24")
    print("COINDCX BTC USDT FUTURES")
    print("5M ENTRY + 1H MARKET STRUCTURE")
    print("PAPER TRADING ONLY")
    print("=" * 55)

    df5 = get_candles(resolution="5", days=6)
    df1 = get_candles(resolution="60", days=18)

    state = load_state()

    if not state["last_bar"]:
        state["last_bar"] = int(df5.iloc[-1]["time"])
        save_state(state)
        print("INITIALIZED SUCCESSFULLY")
        print("Waiting for next completed 5m candle")
        return

    new_indices = df5.index[df5["time"] > state["last_bar"]].tolist()

    if not new_indices:
        print("NO NEW COMPLETED 5M CANDLE")
        return

    latest_time = int(df5.iloc[-1]["time"])

    if len(new_indices) > MAX_CATCHUP:
        if not state["positions"]:
            # Koi open position nahi: purani candles process karne ka
            # fayda nahi (entry stale hoti). Seedha latest candle tak
            # fast-forward, purana adhura setup hata do.
            print(
                f"INFO: {len(new_indices)} candles ka gap, koi open "
                "position nahi - fast-forward to latest candle"
            )
            state["setup"] = None
            state["last_bar"] = latest_time
            save_state(state)
            print("Waiting for next completed 5m candle")
            return

        # Open position hai: saari candles process karo taaki
        # stop / target / trail miss na ho. Naye entry 'fresh' check
        # se apne aap block rahenge.
        print(
            f"INFO: {len(new_indices)} candles ka gap, open position hai "
            "- sab candles process ho rahi hain (exits ke liye)"
        )

    for i in new_indices:
        bar = df5.iloc[i]

        # Future-data leakage nahi: sirf band ho chuki 1H candles
        eligible_h1 = df1[
            df1["time"] + CANDLE_1H_MS <= bar["time"] + CANDLE_5M_MS
        ]
        bias = get_h1_bias(eligible_h1)

        fresh = (latest_time - int(bar["time"])) <= MAX_ENTRY_LAG_BARS * CANDLE_5M_MS

        check_exits(state, bar)
        process_smc(df5, i, state, bias, fresh)

        state["last_bar"] = int(bar["time"])
        save_state(state)

        print(
            "5M:",
            datetime.fromtimestamp(bar["time"] / 1000, timezone.utc).isoformat(),
            "| 1H:", bias,
            "| Open:", len(state["positions"])
        )

    closed = state["closed"]
    win_rate = (state["wins"] / closed * 100) if closed else 0.0

    print("=" * 55)
    print("PAPER TRADING SUMMARY")
    print("Closed:", closed, "| Wins:", state["wins"],
          "| Losses:", state["losses"], f"| WR: {win_rate:.1f}%")
    print("Total R:", round(state["r_sum"], 3),
          "| PnL USDT:", round(state["pnl_usdt"], 2))
    print("V24 SCAN COMPLETE")
    print("=" * 55)


if __name__ == "__main__":
    main()
