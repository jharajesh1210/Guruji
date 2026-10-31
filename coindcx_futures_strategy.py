"""CoinDCX BTC Futures EMA20 pullback + EMA50 + ADX paper trader.
PAPER ONLY. No authenticated trading endpoints or live orders.
"""
import csv
import json
import os
import time
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

PAIR = "B-BTC_USDT"
STATE = Path("coindcx_futures_paper_state.json")
TRADES = Path("coindcx_futures_paper_trades.csv")
MARGIN_USDT = 50.0
LEVERAGE = 30.0
NOTIONAL = MARGIN_USDT * LEVERAGE
FEE_PER_SIDE = 0.0005
SLIPPAGE_PER_SIDE = 0.0002
ATR_MULT = 1.5
MIN_STOP_PCT = 0.003
FINAL_R = 4
LOCKS = {2: 2.0, 3: 3.0}
SESSION = requests.Session()


def telegram(message):
    token = os.getenv("BOT_TOKEN", "")
    chat = os.getenv("CHAT_ID", "")
    if not token or not chat:
        print("TELEGRAM DELIVERY: NOT CONFIGURED (BOT_TOKEN/CHAT_ID missing)")
        return False
    try:
        response = SESSION.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": message[:4000]}, timeout=20)
        response.raise_for_status()
        if not response.json().get("ok"):
            raise RuntimeError("Telegram API returned ok=false")
        print("TELEGRAM DELIVERY: API ACCEPTED")
        return True
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        print(f"TELEGRAM DELIVERY: FAILED: {type(exc).__name__}: {exc}")
        return False


def candles(resolution, days=7):
    now = int(time.time())
    response = SESSION.get(
        "https://public.coindcx.com/market_data/candlesticks",
        params={"pair": PAIR, "pcode": "f", "resolution": str(resolution),
                "from": now - days * 86400, "to": now}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("CoinDCX candles response invalid")
    df = pd.DataFrame(payload["data"])
    required = ["time", "open", "high", "low", "close", "volume"]
    if df.empty or any(col not in df for col in required):
        raise RuntimeError("CoinDCX candles empty or missing columns")
    for col in required:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=required)
    unit = "ms" if df["time"].median() > 1e11 else "s"
    df["time"] = pd.to_datetime(df["time"], unit=unit, utc=True)
    df = df.drop_duplicates("time").set_index("time").sort_index()
    df = df[(df["close"] > 0) & (df["high"] >= df["low"])]
    return df[df.index + pd.Timedelta(minutes=resolution) <= pd.Timestamp.now(tz="UTC")].copy()


def indicators(df):
    df = df.copy()
    df["ema20"] = df["close"].ewm(span=20, adjust=False, min_periods=20).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False, min_periods=50).mean()
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    prev = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev).abs(),
                    (df["low"] - prev).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df["adx"] = dx.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    df["plus_di"] = plus_di
    df["minus_di"] = minus_di
    df["atr"] = atr
    return df


def signal(df):
    if len(df) < 60:
        return "HOLD"
    a, b = df.iloc[-2], df.iloc[-1]
    if not all(pd.notna(b[k]) for k in ["adx", "plus_di", "minus_di", "ema20", "ema50", "atr"]):
        return "HOLD"
    if b["adx"] > 20 and b["plus_di"] > b["minus_di"] and b["close"] > b["ema50"]:
        if a["close"] <= a["ema20"] and b["close"] > b["ema20"]:
            return "BUY"
    if b["adx"] > 20 and b["minus_di"] > b["plus_di"] and b["close"] < b["ema50"]:
        if a["close"] >= a["ema20"] and b["close"] < b["ema20"]:
            return "SELL"
    return "HOLD"


def read_state():
    if not STATE.exists():
        return {"last_signal_bar": None, "position": None, "closed": 0, "realized_usdt": 0.0}
    data = json.loads(STATE.read_text(encoding="utf-8"))
    for k, v in {"last_signal_bar": None, "position": None, "closed": 0, "realized_usdt": 0.0}.items():
        data.setdefault(k, v)
    return data


def save_state(data):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(STATE)


def append_trade(row):
    fields = ["pair", "side", "entry_time", "exit_time", "entry", "exit", "initial_stop",
              "reason", "net_usdt", "realized_total_usdt"]
    exists = TRADES.exists() and TRADES.stat().st_size > 0
    with TRADES.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in fields})


def settle(state, df):
    pos = state["position"]
    if not pos:
        return
    sign = 1 if pos["side"] == "BUY" else -1
    risk = abs(pos["entry"] - pos["initial_stop"])
    since = pd.Timestamp(pos["next_bar"])
    for t, bar in df[df.index >= since].iterrows():
        stop = pos["stop"]
        target = pos["entry"] + sign * FINAL_R * risk
        hit_stop = bar["low"] <= stop if sign == 1 else bar["high"] >= stop
        hit_target = bar["high"] >= target if sign == 1 else bar["low"] <= target
        if hit_stop or hit_target:
            raw_exit = stop if hit_stop else target
            exit_price = raw_exit * (1 - sign * SLIPPAGE_PER_SIDE)
            net_pct = sign * (exit_price / pos["entry"] - 1) - 2 * FEE_PER_SIDE
            pnl = NOTIONAL * net_pct
            state["realized_usdt"] += pnl
            state["closed"] += 1
            reason = ("STOP_OR_TRAIL" if hit_stop else "TP_1_4")
            append_trade({"pair": PAIR, "side": pos["side"], "entry_time": pos["entry_time"],
                          "exit_time": t.isoformat(), "entry": pos["entry"], "exit": exit_price,
                          "initial_stop": pos["initial_stop"], "reason": reason,
                          "net_usdt": round(pnl, 4),
                          "realized_total_usdt": round(state["realized_usdt"], 4)})
            telegram(f"COINDCX BTC FUTURES PAPER EXIT {pos['side']} | {reason}\n"
                     f"PnL {pnl:+.2f} USDT | Total {state['realized_usdt']:+.2f} USDT\nNo real order.")
            print(f"PAPER EXIT {reason} net={pnl:+.2f} USDT")
            state["position"] = None
            return
        best = bar["high"] if sign == 1 else bar["low"]
        reached = sign * (best - pos["entry"]) / risk
        for level in sorted(LOCKS):
            if reached >= level and level > pos["locked_r"]:
                pos["locked_r"] = level
                pos["stop"] = pos["entry"] + sign * LOCKS[level] * risk
                telegram(f"COINDCX BTC FUTURES PAPER LOCK {pos['side']}\n"
                         f"1:{level} reached; SL locked at {pos['stop']:.4f}\n"
                         "Effective next 15m candle. No real order.")
        pos["next_bar"] = (t + pd.Timedelta(minutes=15)).isoformat()


def main():
    state = read_state()
    try:
        df = indicators(candles(15))
        settle(state, df)
        bar = df.index[-1].isoformat()
        last = df.iloc[-1]
        action = signal(df)
        print(f"Current BTC Price: {last['close']:.4f}")
        print(f"Generated Signal: {action}")
        print(f"ADX Value: {last['adx']:.2f}")
        print(f"Closed bar: {bar}")
        if state["last_signal_bar"] != bar:
            state["last_signal_bar"] = bar
            if action != "HOLD" and state["position"] is None:
                sign = 1 if action == "BUY" else -1
                # Paper fill at last closed candle close with modeled slippage.
                entry = float(last["close"]) * (1 + sign * SLIPPAGE_PER_SIDE)
                risk = max(float(last["atr"]) * ATR_MULT, entry * MIN_STOP_PCT)
                stop = entry - sign * risk
                state["position"] = {"side": action, "entry": entry, "initial_stop": stop,
                                     "stop": stop, "locked_r": 0, "entry_time": bar,
                                     "next_bar": (df.index[-1] + pd.Timedelta(minutes=15)).isoformat()}
                telegram(f"COINDCX BTC FUTURES PAPER ENTRY {action}\n"
                         f"Entry {entry:.4f} | SL {stop:.4f} | TP 1:4 {entry + sign*4*risk:.4f}\n"
                         f"Margin 50 USDT x 30 | Locks 1:2, 1:3 | No real order.")
                print(f"PAPER ENTRY {action} at {entry:.4f}")
        save_state(state)
        status = state["position"]["side"] if state["position"] else "NONE"
        summary = (f"COINDCX BTC FUTURES BOT CHECK\nBTC 15m | Signal: {action}\n"
                   f"Price: {last['close']:.4f} | ADX: {last['adx']:.2f}\n"
                   f"Paper position: {status} | Closed: {state['closed']}\n"
                   f"Realized: {state['realized_usdt']:+.2f} USDT\n"
                   "Paper only; no real orders.")
        telegram(summary)
        print(f"PAPER SUMMARY open={status} closed={state['closed']} "
              f"realized={state['realized_usdt']:+.2f} USDT")
    except Exception as exc:
        save_state(state)
        telegram(f"COINDCX BTC FUTURES BOT ERROR: {type(exc).__name__}: {str(exc)[:300]}")
        raise


if __name__ == "__main__":
    main()
