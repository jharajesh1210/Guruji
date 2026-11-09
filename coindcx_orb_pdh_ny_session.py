"""
CoinDCX Futures ORB + PDH/PDL (Long & Short), NY session only
-------------------------------------------------------------
Everything is computed inside the New York session window (UTC, set below):

- Session        : SESSION_START .. SESSION_END (UTC). Default 13:30 - 21:00 UTC (19:00 IST onwards).
- ORB            : first 3 five-minute candles of the session (13:30, 13:35, 13:40 UTC) -> ORB high / low.
                   Entries are only allowed after the ORB is complete (13:45 UTC onwards).
- PDH / PDL      : high / low of the PREVIOUS NY session (same window), not the 24h UTC day.
- Volume spike   : candle volume >= 2x average of the previous 20 candles (current candle excluded).
- LONG  (A)      : close > max(ORB high, PDH), volume spike, green candle, body >= 40% of range.
                   SL = ORB low.
- SHORT (B)      : PDH touched in session, close < ORB low, session high < PDH * 1.01, volume spike,
                   red candle, body >= 40% of range.   SL = session high so far * 1.001.
- Stop ladder    : 2R reached -> SL to entry | 3R reached -> SL to +1.5R | 4R -> target exit.
                   Each candle is checked against the SL that was in force at the START of that candle
                   (SL is checked before target); the ladder only moves for the NEXT candle.
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
SESSION_START = "13:30"      # UTC (19:00 IST)
SESSION_END = "21:00"        # UTC, exclusive
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


S0, S1 = _hm(SESSION_START), _hm(SESSION_END)


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


def build_sessions(d: pd.DataFrame) -> dict:
    """UTC date -> candles of that day's NY session. Incomplete sessions are dropped."""
    mins = d.index.hour * 60 + d.index.minute
    sess = d[(mins >= S0) & (mins < S1)]
    expected = (S1 - S0) // CANDLE_MIN
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
            exit_price = sl if hit_sl else tp          # SL first if both are touched
            if not hit_sl:
                reason = f"TARGET_{TARGET_R:g}R"
            elif sl == entry:
                reason = "BREAKEVEN"
            elif (sl > entry) if long else (sl < entry):
                reason = "LOCKED_PROFIT"
            else:
                reason = "SL"
            trades.append(_close_trade(side, entry, risk, exit_price, pos["t0"], ts, reason))
            pos = None
            continue

        # ladder moves only for the NEXT candle, based on this candle's best price
        r_now = ((h - entry) if long else (entry - l)) / risk
        new_sl = sl
        if r_now >= 3.0:
            new_sl = entry + 1.5 * risk if long else entry - 1.5 * risk
        elif r_now >= 2.0:
            new_sl = entry
        pos["sl"] = max(sl, new_sl) if long else min(sl, new_sl)

    if pos is not None:                                 # session over: flatten
        last = g.iloc[-1]
        trades.append(_close_trade(pos["side"], pos["entry"], pos["risk"], last["close"],
                                   pos["t0"], g.index[-1], "SESSION_END"))
    return trades


def run_strategy(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["vol_avg"] = d["volume"].rolling(VOL_LOOKBACK, min_periods=VOL_LOOKBACK).mean().shift(1)

    sessions = build_sessions(d)
    days = sorted(sessions)
    all_trades = []
    for k in range(1, len(days)):
        day, prev = days[k], days[k - 1]
        if (day - prev).days != 1:                      # need the immediately preceding session
            continue
        pdh = sessions[prev]["high"].max()
        for t in simulate_session(sessions[day], pdh):
            t["session"] = day
            t["pdh"] = pdh
            t["pdl"] = sessions[prev]["low"].min()
            all_trades.append(t)
    return pd.DataFrame(all_trades)


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
    print("\nBy exit reason:")
    print(t.groupby("reason")["r_net"].agg(["count", "sum", "mean"]).round(3))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="CoinDCX Top Futures ORB + PDH/PDL, NY session only")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--csv", default="ny_session_trades.csv")
    ap.add_argument("--universe-csv", default="eligible_futures_universe.csv")
    args = ap.parse_args()

    try:
        universe = fetch_eligible_futures_pairs()
        if not universe:
            raise RuntimeError("No active USDT futures meet price > 1 USDT and 24h volume > 10,000,000 USDT.")
        universe_df = pd.DataFrame(universe)
        universe_df.to_csv(args.universe_csv, index=False)
        print(f"Eligible futures coins: {len(universe)} (price > {MIN_COIN_PRICE_USDT} USDT; 24h volume > {MIN_24H_VOLUME_USDT:,.0f} USDT)")
        print(universe_df.to_string(index=False))

        all_results = []
        for number, coin in enumerate(universe, start=1):
            pair = coin["pair"]
            print(f"\\n[{number}/{len(universe)}] Fetching {pair} 5m candles for {args.days} days...")
            try:
                data = fetch_coindcx_candles(pair=pair, interval="5", limit_days=args.days)
                result = run_strategy(data)
                if not result.empty:
                    result.insert(0, "pair", pair)
                    all_results.append(result)
                print(f"{pair}: sessions={len(build_sessions(data))}, trades={len(result)}")
            except Exception as exc:
                print(f"[DATA ERROR] {pair}: {exc}")
            time.sleep(0.25)

        combined = pd.concat(all_results, ignore_index=True) if all_results else pd.DataFrame()
        if not combined.empty:
            combined = combined.sort_values(["entry_time", "pair"]).reset_index(drop=True)
        summarize(combined, sum(1 for _ in universe))
        combined.to_csv(args.csv, index=False)
        print(f"\\nSaved combined trades: {args.csv}")
        print(f"Saved eligible coin universe: {args.universe_csv}")
    except Exception as e:
        print(f"Execution Error: {e}")
        raise
