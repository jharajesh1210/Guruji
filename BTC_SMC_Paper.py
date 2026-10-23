
# ============================================================
# BTC_SMC_Paper.py
# V23 PURE SMC - COINDCX BTC USDT FUTURES
# 5 MIN ENTRY + 1 HOUR STRUCTURE
# LIQUIDITY SWEEP + CHOCH + FVG RETEST
# LONG / SHORT - PAPER TRADING ONLY
# TELEGRAM ENTRY / EXIT / PROFIT LOSS
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

FUTURES_API = (
    "https://public.coindcx.com/market_data/candlesticks"
)

STATE_FILE = Path("BTC_SMC_Paper_state.json")
TRADES_FILE = Path("BTC_SMC_Paper_trades.csv")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

SWING_LENGTH = 3
SETUP_EXPIRY = 18
FVG_EXPIRY = 12

RR_VALUES = [2, 3, 4]  # Milestones, NOT separate positions
MARGIN_USDT = 100.0
LEVERAGE = 30

# Paper trading cost assumptions
ROUND_TRIP_FEE = 0.001
SLIPPAGE_PER_SIDE = 0.0002

MIN_RISK_PERCENT = 0.05
MAX_RISK_PERCENT = 2.50

CANDLE_5M_MS = 5 * 60 * 1000
CANDLE_1H_MS = 60 * 60 * 1000


# =========================
# 2. TELEGRAM
# =========================

def send_telegram(message):

    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram not configured")
        print(message)
        return

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

    try:
        response = requests.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": message
            },
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

    # Download in smaller chunks
    if resolution == "5":
        chunk_seconds = 2 * 86400
        candle_ms = CANDLE_5M_MS

    elif resolution == "60":
        chunk_seconds = 10 * 86400
        candle_ms = CANDLE_1H_MS

    else:
        raise ValueError("Unsupported resolution")

    all_rows = []

    for left in range(
        start_seconds,
        now_seconds,
        chunk_seconds
    ):

        right = min(
            left + chunk_seconds,
            now_seconds
        )

        params = {
            "pair": PAIR,
            "from": int(left),
            "to": int(right),
            "resolution": resolution,
            "pcode": "f"
        }

        response = requests.get(
            FUTURES_API,
            params=params,
            timeout=30
        )

        if response.status_code != 200:
            print(
                "CoinDCX API error:",
                response.status_code,
                response.text[:500]
            )

        response.raise_for_status()

        result = response.json()

        if not isinstance(result, dict):
            raise RuntimeError(
                "Unexpected Futures API response"
            )

        if result.get("s") != "ok":
            raise RuntimeError(
                "CoinDCX Futures API: "
                + str(result)[:500]
            )

        payload = result.get("data", [])

        if not isinstance(payload, list):
            raise RuntimeError(
                "Invalid Futures candle payload"
            )

        all_rows.extend(payload)

        time.sleep(0.2)

    if not all_rows:
        raise RuntimeError(
            "No CoinDCX Futures candles received"
        )

    df = pd.DataFrame(all_rows)

    required = [
        "time", "open", "high", "low", "close"
    ]

    for column in required:
        if column not in df.columns:
            raise RuntimeError(
                "Missing candle column: " + column
            )

        df[column] = pd.to_numeric(
            df[column],
            errors="coerce"
        )

    df = df.dropna(subset=required)

    # Support timestamps in seconds or milliseconds
    df["time"] = df["time"].where(
        df["time"] > 1e11,
        df["time"] * 1000
    )

    df = (
        df.drop_duplicates("time")
        .sort_values("time")
        .reset_index(drop=True)
    )

    # Only fully completed candles
    now_ms = int(time.time() * 1000)

    df = df[
        df["time"] + candle_ms <= now_ms
    ].reset_index(drop=True)

    minimum = 100 if resolution == "5" else 30

    if len(df) < minimum:
        raise RuntimeError(
            f"Insufficient {resolution} candles: "
            f"{len(df)}"
        )

    print(
        f"Futures {resolution} candles loaded:",
        len(df)
    )

    return df


# =========================
# 4. SWING HIGHS / LOWS
# =========================

def find_swings(df, length=SWING_LENGTH):

    swing_highs = []
    swing_lows = []

    for i in range(
        length,
        len(df) - length
    ):

        window = df.iloc[
            i - length:i + length + 1
        ]

        current = df.iloc[i]

        if (
            current["high"] == window["high"].max()
            and
            (window["high"] == current["high"]).sum() == 1
        ):

            swing_highs.append(
                (i, float(current["high"]))
            )

        if (
            current["low"] == window["low"].min()
            and
            (window["low"] == current["low"]).sum() == 1
        ):

            swing_lows.append(
                (i, float(current["low"]))
            )

    return swing_highs, swing_lows


# =========================
# 5. ONE HOUR MARKET STRUCTURE
# =========================

def get_h1_bias(df):

    highs, lows = find_swings(df)

    if len(highs) < 2 or len(lows) < 2:
        return "NEUTRAL"

    higher_high = highs[-1][1] > highs[-2][1]
    higher_low = lows[-1][1] > lows[-2][1]

    lower_high = highs[-1][1] < highs[-2][1]
    lower_low = lows[-1][1] < lows[-2][1]

    if higher_high and higher_low:
        return "LONG"

    if lower_high and lower_low:
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
        "net_pct": 0.0
    }


def load_state():

    if STATE_FILE.exists():

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            return json.load(file)

    return new_state()


def save_state(state):

    temp_file = STATE_FILE.with_suffix(".tmp")

    with open(
        temp_file,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            state,
            file,
            indent=2
        )

    temp_file.replace(STATE_FILE)


# =========================
# 7. CSV TRADE HISTORY
# =========================

def save_trade(trade):

    columns = [
        "time_utc",
        "side",
        "rr",
        "entry",
        "stop",
        "target",
        "exit",
        "reason",
        "gross_pct",
        "net_pct",
        "margin_usdt",
        "trade_mode",
        "max_rr_reached"
    ]

    row = {
        column: trade.get(column, "")
        for column in columns
    }

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
    """One full-size position: 2R -> SL at 2R; 3R -> SL at 3R; exit 4R.

    Existing legacy RR=2/3 positions are allowed to finish unchanged.
    Intrabar OHLC ordering is unknown: a pre-existing stop takes priority.
    New trailing stops activate from the NEXT completed 5m candle, never
    retroactively within the candle that first reached the milestone.
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
        stop_hit = low <= stop if is_long else high >= stop
        target_hit = high >= target if is_long else low <= target
        exit_price, reason = None, None

        if stop_hit:
            # Conservative: stop wins when old stop and target both touched.
            exit_price = stop
            reason = "TRAIL STOP" if position.get("trade_mode") == "SINGLE_TRAIL_4R" and position.get("max_rr_reached", 0) >= 2 else "STOP LOSS"
        elif target_hit:
            exit_price = target
            reason = "FINAL TARGET 4R" if position.get("trade_mode") == "SINGLE_TRAIL_4R" else "TARGET HIT"
        elif position.get("trade_mode") == "SINGLE_TRAIL_4R":
            # Milestones move the full position's stop, never book partial P/L.
            risk = float(position["initial_risk"])
            reached = int(position.get("max_rr_reached", 0))
            for rr in (3, 2):
                milestone = entry + direction * rr * risk
                touched = high >= milestone if is_long else low <= milestone
                if touched and rr > reached:
                    position["stop"] = milestone
                    position["max_rr_reached"] = rr
                    alert = (f"BTC SMC PAPER - TRAILING STOP UPDATED\n"
                             f"Side: {position['side']}\n"
                             f"Reached: 1:{rr}\n"
                             f"New Stop Loss: {milestone:.2f}\n"
                             f"Position Open: 100%\n"
                             f"Final Target 1:4: {target:.2f}\n"
                             "PAPER TRADING ONLY")
                    print(alert)
                    send_telegram(alert)
                    break

        if exit_price is None:
            remaining.append(position)
            continue

        gross_pct = (exit_price / entry - 1) * 100 * direction
        costs_pct = (ROUND_TRIP_FEE + 2 * SLIPPAGE_PER_SIDE) * 100
        net_pct = gross_pct - costs_pct
        state["closed"] += 1
        if net_pct > 0:
            state["wins"] += 1
        else:
            state["losses"] += 1
        state["net_pct"] = round(state["net_pct"] + net_pct, 6)
        timestamp = datetime.fromtimestamp(bar["time"] / 1000, timezone.utc).isoformat()
        trade = {**position, "time_utc": timestamp, "exit": exit_price,
                 "reason": reason, "gross_pct": round(gross_pct, 4),
                 "net_pct": round(net_pct, 4),
                 "margin_usdt": position.get("margin_usdt", MARGIN_USDT)}
        save_trade(trade)
        closed = state["closed"]
        win_rate = state["wins"] / closed * 100
        message = (
            "BTC SMC PAPER - TRADE CLOSED\n"
            f"Side: {position['side']}\n"
            f"Mode: {position.get('trade_mode', 'LEGACY RR LEG')}\n"
            f"Result: {reason}\n"
            f"Entry: {entry:.2f}\n"
            f"Exit: {exit_price:.2f}\n"
            f"Margin: ${float(position.get('margin_usdt', MARGIN_USDT)):.2f}\n"
            f"Net Return: {net_pct:+.3f}%\n\n"
            f"Closed Records: {closed}\n"
            f"Wins: {state['wins']}\n"
            f"Losses: {state['losses']}\n"
            f"Win Rate: {win_rate:.2f}%\n"
            f"Sum Net Returns: {state['net_pct']:+.3f}%\n"
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

    highs, lows = find_swings(previous)

    bar = df.iloc[i]

    if side == "LONG" and lows:

        swing_low = lows[-1][1]

        if (
            bar["low"] < swing_low
            and bar["close"] > swing_low
        ):

            return {
                "side": "LONG",
                "stage": "SWEEP",
                "sweep_time": int(bar["time"]),
                "extreme": float(bar["low"]),
                "break_level": (
                    highs[-1][1] if highs else None
                )
            }

    if side == "SHORT" and highs:

        swing_high = highs[-1][1]

        if (
            bar["high"] > swing_high
            and bar["close"] < swing_high
        ):

            return {
                "side": "SHORT",
                "stage": "SWEEP",
                "sweep_time": int(bar["time"]),
                "extreme": float(bar["high"]),
                "break_level": (
                    lows[-1][1] if lows else None
                )
            }

    return None


# =========================
# 10. SMC SETUP
# =========================

def process_smc(df, i, state, bias):

    bar = df.iloc[i]
    setup = state.get("setup")

    if setup:

        expired = (
            bar["time"] - setup["sweep_time"]
            > SETUP_EXPIRY * CANDLE_5M_MS
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
                    bar["close"] > level
                    if is_long
                    else bar["close"] < level
                )

                if choch:

                    setup["stage"] = "CHOCH"
                    setup["choch_time"] = int(
                        bar["time"]
                    )

                    print(
                        "SMC CHOCH:",
                        setup["side"]
                    )

        elif setup["stage"] == "CHOCH":

            if i >= 2:

                first = df.iloc[i - 2]

                bullish_fvg = (
                    bar["low"] > first["high"]
                )

                bearish_fvg = (
                    bar["high"] < first["low"]
                )

                if is_long and bullish_fvg:

                    setup.update({
                        "stage": "FVG",
                        "fvg_low": float(
                            first["high"]
                        ),
                        "fvg_high": float(
                            bar["low"]
                        ),
                        "fvg_time": int(
                            bar["time"]
                        )
                    })

                elif (
                    not is_long
                    and bearish_fvg
                ):

                    setup.update({
                        "stage": "FVG",
                        "fvg_low": float(
                            bar["high"]
                        ),
                        "fvg_high": float(
                            first["low"]
                        ),
                        "fvg_time": int(
                            bar["time"]
                        )
                    })

        elif setup["stage"] == "FVG":

            fvg_expired = (
                bar["time"] - setup["fvg_time"]
                > FVG_EXPIRY * CANDLE_5M_MS
            )

            if fvg_expired:
                setup = None

            else:

                touched = (
                    bar["low"] <= setup["fvg_high"]
                    and
                    bar["high"] >= setup["fvg_low"]
                )

                if is_long:

                    rejection = (
                        bar["close"] > bar["open"]
                        and
                        bar["close"] > setup["fvg_high"]
                    )

                else:

                    rejection = (
                        bar["close"] < bar["open"]
                        and
                        bar["close"] < setup["fvg_low"]
                    )

                if touched and rejection:

                    entry = float(bar["close"])
                    stop = float(setup["extreme"])

                    risk = abs(entry - stop)

                    risk_percent = (
                        risk / entry * 100
                    )

                    valid_direction = (
                        stop < entry
                        if is_long
                        else stop > entry
                    )

                    valid_risk = (
                        MIN_RISK_PERCENT
                        <= risk_percent
                        <= MAX_RISK_PERCENT
                    )

                    if (
                        valid_direction
                        and valid_risk
                        and not state["positions"]
                    ):

                        # One $100 margin position; NO partial exits.
                        # 2R -> move SL to 2R; 3R -> move SL to 3R;
                        # 4R -> close the entire position.
                        position = {
                            "side": setup["side"],
                            "rr": 4,
                            "entry": entry,
                            "stop": stop,
                            "initial_stop": stop,
                            "initial_risk": risk,
                            "target": entry + (4 * risk if is_long else -4 * risk),
                            "entry_time": int(bar["time"]),
                            "trade_mode": "SINGLE_TRAIL_4R",
                            "max_rr_reached": 0,
                            "margin_usdt": MARGIN_USDT
                        }
                        state["positions"].append(position)
                        target2 = entry + (2 * risk if is_long else -2 * risk)
                        target3 = entry + (3 * risk if is_long else -3 * risk)
                        target4 = position["target"]

                        timestamp = (
                            datetime.fromtimestamp(
                                bar["time"] / 1000,
                                timezone.utc
                            ).isoformat()
                        )

                        message = (
                            "BTC SMC PAPER ENTRY\n"
                            f"Time UTC: {timestamp}\n"
                            f"Pair: {PAIR}\n"
                            f"Side: {setup['side']}\n"
                            f"1H Bias: {bias}\n"
                            "Setup: Liquidity Sweep "
                            "+ CHOCH + FVG Retest\n"
                            f"Entry: {entry:.2f}\n"
                            f"Stop Loss: {stop:.2f}\n"
                            f"Target RR2: {target2:.2f}\n"
                            f"Target RR3: {target3:.2f} (move SL to RR3)\n"
                            f"Target RR4: {target4:.2f} (full exit)\n"
                            "At RR2: move SL to RR2; no partial exit\n"
                            "Margin: $100 total, Leverage: 30x\n"
                            "PAPER TRADING ONLY"
                        )

                        print(message)
                        send_telegram(message)

                    setup = None

    if (
        setup is None
        and bias in ("LONG", "SHORT")
        and not state["positions"]
    ):

        setup = detect_sweep(
            df,
            i,
            bias
        )

        if setup:
            print(
                "Liquidity Sweep:",
                setup["side"]
            )

    state["setup"] = setup


# =========================
# 11. MAIN SCANNER
# =========================

def main():

    print("=" * 55)
    print("BTC SMC PAPER - V23 SINGLE POSITION TRAIL 2R/3R/4R")
    print("COINDCX BTC USDT FUTURES")
    print("5M ENTRY + 1H MARKET STRUCTURE")
    print("PAPER TRADING ONLY")
    print("=" * 55)

    df5 = get_candles(
        resolution="5",
        days=6
    )

    df1 = get_candles(
        resolution="60",
        days=18
    )

    state = load_state()

    # First run initializes at the latest completed candle.
    # No historical paper trades are invented.

    if not state["last_bar"]:

        state["last_bar"] = int(
            df5.iloc[-1]["time"]
        )

        save_state(state)

        print(
            "INITIALIZED SUCCESSFULLY"
        )

        print(
            "Waiting for next completed 5m candle"
        )

        return

    new_indices = df5.index[
        df5["time"] > state["last_bar"]
    ].tolist()

    if not new_indices:

        print(
            "NO NEW COMPLETED 5M CANDLE"
        )

        return

    if len(new_indices) > 12:

        raise RuntimeError(
            "Too many missed 5m candles. "
            "Manual review required."
        )

    for i in new_indices:

        bar = df5.iloc[i]

        # Avoid future-data leakage:
        # only use 1H candles already closed.

        eligible_h1 = df1[
            df1["time"] + CANDLE_1H_MS
            <= bar["time"] + CANDLE_5M_MS
        ]

        bias = get_h1_bias(
            eligible_h1
        )

        check_exits(
            state,
            bar
        )

        process_smc(
            df5,
            i,
            state,
            bias
        )

        state["last_bar"] = int(
            bar["time"]
        )

        save_state(state)

        print(
            "5M:",
            datetime.fromtimestamp(
                bar["time"] / 1000,
                timezone.utc
            ).isoformat(),
            "| 1H:",
            bias,
            "| Open Legs:",
            len(state["positions"])
        )

    print("=" * 55)
    print("PAPER TRADING SUMMARY")
    print("Closed Legs:", state["closed"])
    print("Wins:", state["wins"])
    print("Losses:", state["losses"])

    print(
        "Sum Net Returns:",
        round(state["net_pct"], 4),
        "%"
    )

    print("V23 SCAN COMPLETE")
    print("=" * 55)


if __name__ == "__main__":
    main()
