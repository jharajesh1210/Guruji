"""
CoinDCX Futures ORB + PDH/PDL (Long & Short), London + NY sessions
-------------------------------------------------------------
Everything is computed inside each session window (UTC, see SESSIONS). London and NY run
separately with identical rules; London ends exactly when NY starts, so trades never overlap:

- Sessions       : LONDON 08:00 - 13:30 UTC (13:30 IST) and NY 13:30 - 21:00 UTC (19:00 IST).
- ORB            : first 3 five-minute candles of the session (NY: 13:30, 13:35, 13:40 UTC) -> ORB high / low.
                   Entries are only allowed after the ORB is complete (NY: 13:45 UTC onwards).
- PDH / PDL      : high / low of the PREVIOUS session of the SAME name (London uses yesterday's London
                   session, NY uses yesterday's NY session), not the 24h UTC day.
- Volume spike   : candle volume >= 2x average of the previous 20 candles (current candle excluded).
- LONG  (A)      : close > max(ORB high, PDH), volume spike, green candle, body >= 40% of range.
                   SL = ORB low.
- SHORT (B)      : PDH touched in session, close < ORB low, session high < PDH * 1.01, volume spike,
                   red candle, body >= 40% of range.   SL = session high so far * 1.001.
- Stop ladder    : price reaches 2R -> SL locked at +2R | reaches 3R -> SL locked at +3R | reaches 4R -> exit at 4R.
                   So a reversal after 2R exits at +2R, after 3R exits at +3R; before 2R the original SL applies.
                   Each candle is checked against the SL that was in force at the START of that candle
                   (SL is checked before target); the ladder only moves for the NEXT candle. If a candle
                   opens beyond the SL (gap), the exit is filled at that open, not at the SL price.
- Session end    : any open trade is closed at the last session candle's close.
- Fees           : FEE_PCT_PER_SIDE is charged on entry and exit and shown in R.

Sessions with missing candles (incomplete) are skipped, and so is a session whose previous
session is missing or incomplete (no valid PDH/PDL).

Research / paper-trading tool only. It places no orders.
"""

import argparse
import time

import numpy as np
import pandas as pd
import requests

URL = "https://public.coindcx.com/market_data/candlesticks"

# ----------------------------- CONFIG -----------------------------
SESSIONS = {                 # name -> (start, end) in UTC, end exclusive
    "LONDON": ("08:00", "13:30"),   # 13:30 - 19:00 IST (London summer time opens at 07:00 UTC)
    "NY": ("13:30", "21:00"),       # 19:00 - 02:30 IST
}
CANDLE_MIN = 5
ORB_CANDLES = 3              # 3 x 5m = 15 min
VOL_MULT = 2.0
VOL_LOOKBACK = 20
BODY_MIN = 0.4
TARGET_R = 4.0
SHORT_SL_BUFFER = 0.001
FEE_PCT_PER_SIDE = 0.05      # taker fee estimate, % of notional per side
MIN_SESSION_FILL = 0.95      # share of expected candles a session needs to count as complete
# -------------------------------------------------------------------


def _hm(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 60 + int(m)


SESSION_MINS = {name: (_hm(a), _hm(b)) for name, (a, b) in SESSIONS.items()}


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


def build_sessions(d: pd.DataFrame, name: str = "NY") -> dict:
    """UTC date -> candles of that day's session `name`. Incomplete sessions are dropped."""
    s0, s1 = SESSION_MINS[name]
    mins = d.index.hour * 60 + d.index.minute
    sess = d[(mins >= s0) & (mins < s1)]
    expected = (s1 - s0) // CANDLE_MIN
    out = {}
    for day, g in sess.groupby(sess.index.date):
        if len(g) >= int(expected * MIN_SESSION_FILL):
            out[day] = g
    return out


def _close_trade(side, entry, risk, exit_price, entry_time, exit_time, reason):
    gross_r = ((exit_price - entry) if side == "LONG" else (entry - exit_price)) / risk
    fee_r = (2 * FEE_PCT_PER_SIDE / 100 * entry) / risk
    return {
        "entry_time": entry_time, "exit_time": exit_time, "side": side,
        "entry": entry, "exit": exit_price, "risk": risk,
        "r_gross": gross_r, "r_net": gross_r - fee_r, "reason": reason,
    }


def simulate_session(g: pd.DataFrame, pdh: float):
    orb = g.iloc[:ORB_CANDLES]
    orb_high, orb_low = orb["high"].max(), orb["low"].min()
    run_high = g["high"].cummax().values

    trades, pos = [], None
    for i in range(ORB_CANDLES, len(g)):
        bar, ts = g.iloc[i], g.index[i]
        o, h, l, c, v = bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]

        if pos is None:
            rng = h - l
            strong = rng > 0 and abs(c - o) / rng >= BODY_MIN
            va = bar["vol_avg"]
            spike = (not np.isnan(va)) and v >= VOL_MULT * va
            if not (strong and spike):
                continue

            long_ok = c > max(orb_high, pdh) and c > o
            short_ok = run_high[i] >= pdh * 0.999 and c < orb_low and run_high[i] < pdh * 1.01 and c < o

            if long_ok:
                sl = orb_low
                risk = c - sl
                if risk > 0:
                    pos = dict(side="LONG", entry=c, sl=sl, risk=risk, tp=c + TARGET_R * risk, t0=ts)
            elif short_ok:
                sl = run_high[i] * (1 + SHORT_SL_BUFFER)
                risk = sl - c
                if risk > 0:
                    pos = dict(side="SHORT", entry=c, sl=sl, risk=risk, tp=c - TARGET_R * risk, t0=ts)
            continue

        side, entry, risk, sl, tp = pos["side"], pos["entry"], pos["risk"], pos["sl"], pos["tp"]
        long = side == "LONG"

        hit_sl = (l <= sl) if long else (h >= sl)
        hit_tp = (h >= tp) if long else (l <= tp)
        if hit_sl or hit_tp:
            if hit_sl:                                  # SL first if both are touched
                exit_price = min(sl, o) if long else max(sl, o)   # gap through the stop fills at the open
                locked = abs(sl - entry) / risk if ((sl > entry) if long else (sl < entry)) else 0
                reason = f"LOCK_{locked:.0f}R" if locked else "SL"
            else:
                exit_price = tp
                reason = f"TARGET_{TARGET_R:g}R"
            trades.append(_close_trade(side, entry, risk, exit_price, pos["t0"], ts, reason))
            pos = None
            continue

        # ladder moves only for the NEXT candle, based on this candle's best price
        r_now = ((h - entry) if long else (entry - l)) / risk
        new_sl = sl
        if r_now >= 3.0:
            new_sl = entry + 3.0 * risk if long else entry - 3.0 * risk
        elif r_now >= 2.0:
            new_sl = entry + 2.0 * risk if long else entry - 2.0 * risk
        pos["sl"] = max(sl, new_sl) if long else min(sl, new_sl)

    if pos is not None:                                 # session over: flatten
        last = g.iloc[-1]
        trades.append(_close_trade(pos["side"], pos["entry"], pos["risk"], last["close"],
                                   pos["t0"], g.index[-1], "SESSION_END"))
    return trades


def run_strategy(df: pd.DataFrame, session_names=("LONDON", "NY")) -> pd.DataFrame:
    d = df.copy()
    d["vol_avg"] = d["volume"].rolling(VOL_LOOKBACK, min_periods=VOL_LOOKBACK).mean().shift(1)

    all_trades = []
    for name in session_names:
        sessions = build_sessions(d, name)
        days = sorted(sessions)
        for k in range(1, len(days)):
            day, prev = days[k], days[k - 1]
            if (day - prev).days != 1:                  # need yesterday's same session
                continue
            pdh = sessions[prev]["high"].max()
            pdl = sessions[prev]["low"].min()
            for t in simulate_session(sessions[day], pdh):
                t.update(session=name, date=day, pdh=pdh, pdl=pdl)
                all_trades.append(t)
    if not all_trades:
        return pd.DataFrame()
    return pd.DataFrame(all_trades).sort_values("entry_time").reset_index(drop=True)


def summarize(t: pd.DataFrame, n_sessions: int):
    print(f"\nSessions tested : {n_sessions}")
    if t.empty:
        print("No trades generated.")
        return
    wins, losses = t[t["r_net"] > 0], t[t["r_net"] <= 0]
    loss_sum = abs(losses["r_net"].sum())
    pf = wins["r_net"].sum() / loss_sum if loss_sum else float("inf")
    equity = t["r_net"].cumsum()
    print(f"Trades          : {len(t)}  (LONG {int((t.side == 'LONG').sum())}, SHORT {int((t.side == 'SHORT').sum())})")
    print(f"Win rate        : {len(wins) / len(t) * 100:.1f}%")
    print(f"Net R (after fee): {t['r_net'].sum():.2f}   avg {t['r_net'].mean():.3f} R/trade")
    print(f"Profit factor   : {pf:.2f}")
    print(f"Max drawdown    : {(equity.cummax() - equity).max():.2f} R")
    print("\nBy side:")
    print(t.groupby("side")["r_net"].agg(["count", "sum", "mean"]).round(3))
    print("\nBy session:")
    print(t.groupby("session")["r_net"].agg(["count", "sum", "mean"]).round(3))
    print("\nBy exit reason:")
    print(t.groupby("reason")["r_net"].agg(["count", "sum", "mean"]).round(3))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="CoinDCX ORB + PDH/PDL, NY session only")
    ap.add_argument("--pair", default="B-BTC_USDT")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--csv", default="session_trades.csv")
    ap.add_argument("--sessions", default="LONDON,NY", help="comma list, e.g. NY or LONDON,NY")
    args = ap.parse_args()
    args.sessions = [s.strip().upper() for s in args.sessions.split(",") if s.strip()]
    bad = [s for s in args.sessions if s not in SESSION_MINS]
    if bad:
        ap.error(f"unknown session(s): {bad}; choose from {list(SESSION_MINS)}")

    print(f"Fetching CoinDCX {args.pair} 5m candles for {args.days} days...")
    try:
        data = fetch_coindcx_candles(pair=args.pair, interval="5", limit_days=args.days)
        res = run_strategy(data, args.sessions)
        summarize(res, sum(len(build_sessions(data, s)) for s in args.sessions))
        if not res.empty:
            res.to_csv(args.csv, index=False)
            print(f"\nSaved: {args.csv}")
    except Exception as e:
        print(f"Execution Error: {e}")
