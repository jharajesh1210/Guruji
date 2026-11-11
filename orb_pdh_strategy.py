"""
ORB + PDH/PDL Intraday Strategy (Nifty 50 stocks)
-------------------------------------------------
Modes:
  Data: Angel One SmartAPI (env vars: ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PIN, ANGEL_TOTP_SECRET)
  python orb_pdh_strategy.py backtest            # last ~55 days of 5m candles (--days 100 also works)
  python orb_pdh_strategy.py backtest --days 30 --rr 2.0
  python orb_pdh_strategy.py scan                # live paper scan, prints signals on latest closed 5m bar

Rules implemented
  Opening range : first 15 min (three 5m candles, 9:15-9:30) -> ORB high / low
  Gap filter    : |open - prev close| > 1.5% -> no LONG breakout that day
  LONG  (A)     : 5m close > max(ORB high, PDH) AND volume >= 2x avg of previous 20 candles
                  AND Nifty index above its VWAP and above day open
                  SL = ORB low (or VWAP if SL_MODE = "vwap", whichever is nearer/lower for long)
  SHORT (B)     : price touched PDH earlier today, then a 5m candle closes below ORB low
                  AND Nifty index below its VWAP and below day open
                  SL = max(day high so far, PDH) + small buffer
  Target        : fixed 4R final exit; at 2R lock +2R, at 3R lock +3R.
  Skips         : news days listed in NEWS_SKIP_DATES, entries outside 9:30-14:30, risk > MAX_RISK_PCT
  Exit          : 2R/3R profit-lock stop, 4R final target (SL assumed first if both hit), else square-off at 15:15.

NOT automated (check manually): "major S/R level close by -> skip", sector index direction,
Gift Nifty / US sentiment, intraday news windows (only whole-day skip dates are supported).

This is a research / paper-trading tool, not financial advice. No broker orders are placed.
"""

import argparse
import datetime as dt
import sys

import json
import os
import time

import numpy as np
import pandas as pd
import pyotp
import requests
from SmartApi import SmartConnect  # pip install smartapi-python pyotp requests

# ----------------------------- CONFIG -----------------------------
INDEX_SYMBOL = "Nifty 50"            # Angel One index (token 99926000)
SYMBOLS = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO",
    "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL", "CIPLA", "COALINDIA", "DRREDDY",
    "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO",
    "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK", "INFY", "ITC", "JSWSTEEL",
    "KOTAKBANK", "LT", "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC", "POWERGRID",
    "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM", "TATAMOTORS",
    "TATASTEEL", "TCS", "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO", "JIOFIN",
]  # verify against the current Nifty 50 constituents; edit freely

GAP_LIMIT_PCT = 1.5
VOL_MULT = 2.0
VOL_LOOKBACK = 20
ORB_CANDLES = 3                      # 3 x 5m = 15 min
ENTRY_START = dt.time(9, 30)
ENTRY_END = dt.time(14, 30)
SQUARE_OFF = dt.time(15, 15)
RR = 4.0                             # fixed final target; profit locks at +2R and +3R
MAX_RISK_PCT = 1.5                   # skip trade if SL distance > this % of entry
SL_MODE = "orb"                      # "orb" or "vwap" (for LONG)
SL_BUFFER_PCT = 0.05                 # small buffer beyond SL level
COST_PCT_ROUND_TRIP = 0.05           # brokerage + slippage estimate, % of entry
NEWS_SKIP_DATES = {
    # "2026-11-12",  # example: CPI day
}
TZ = "Asia/Kolkata"
DEFAULT_CAPITAL = 50000.0
RISK_PER_TRADE_PCT = 1.0
PAPER_STATE_FILE = "ORB_PDH_PAPER_STATE.json"
PAPER_TRADES_FILE = "ORB_PDH_PAPER_TRADES.csv"
PAPER_EQUITY_FILE = "ORB_PDH_PAPER_EQUITY.csv"
# -------------------------------------------------------------------


# ------------------------- ANGEL ONE DATA -------------------------
SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
SCRIP_MASTER_FILE = "angel_scrip_master.json"
INDEX_TOKENS = {"Nifty 50": "99926000"}
REQUEST_GAP_SEC = 4.0                # Conservative spacing between Angel historical candle requests
RATE_LIMIT_WAITS = (20, 40, 60)      # seconds to pause after an Angel "Too many requests" reply, then retry
RATE_LIMIT_BUDGET_SEC = 150          # max total seconds ONE cycle may spend waiting on rate limits
CYCLE_DEADLINE_SEC = 420             # stop starting new symbols after this; the next cycle continues the scan
DAILY_CACHE_DIR = "angel_daily_cache"
DAILY_CACHE_MAX_AGE_SEC = 86400

_api = None
_tokens = None
_last_candle_request = 0.0
_rate_limit_waited = 0.0


class AngelRateLimitError(RuntimeError):
    """Raised when Angel One rejects candle requests due to rate limiting."""


def _get_candles_with_retry(api, params, symbol):
    """Throttle every historical request; retry transient failures and PAUSE+RETRY on rate-limit replies.

    A rate-limit reply no longer aborts the cycle at once: we wait RATE_LIMIT_WAITS seconds and try again, up to
    RATE_LIMIT_BUDGET_SEC of total waiting per cycle. Only then AngelRateLimitError is raised.
    """
    global _last_candle_request, _rate_limit_waited
    last_error = None
    attempt = 0          # transient (non rate-limit) failures
    rl_tries = 0         # rate-limit pauses used for this request
    while attempt < 3:
        wait = REQUEST_GAP_SEC - (time.monotonic() - _last_candle_request)
        if wait > 0:
            time.sleep(wait)
        _last_candle_request = time.monotonic()
        try:
            response = api.getCandleData(params)
            if response and response.get("status") and response.get("data") is not None:
                return response
            last_error = RuntimeError(f"Angel candle API returned: {response}")
            message = str(response).lower()
        except Exception as exc:  # SmartAPI may raise JSONDecodeError on plain-text rate-limit replies
            last_error = exc
            message = str(exc).lower()

        rate_limited = ("exceeding access rate" in message or "access denied" in message
                        or "rate limit" in message or "too many request" in message)
        if rate_limited:
            if rl_tries >= len(RATE_LIMIT_WAITS) or _rate_limit_waited + RATE_LIMIT_WAITS[rl_tries] > RATE_LIMIT_BUDGET_SEC:
                raise AngelRateLimitError(
                    f"Angel One rate limit reached for {symbol}; remaining symbols continue in the next cycle."
                ) from last_error
            pause = RATE_LIMIT_WAITS[rl_tries]
            rl_tries += 1
            print(f"[warn] {symbol}: Angel One rate limit; pausing {pause}s then retrying ({rl_tries}/{len(RATE_LIMIT_WAITS)})")
            time.sleep(pause)
            _rate_limit_waited += pause
            continue
        delay = (3, 6, 12)[attempt]
        print(f"[warn] {symbol}: candle API attempt {attempt + 1}/3 failed; retrying in {delay}s: {last_error}")
        time.sleep(delay)
        attempt += 1
    raise RuntimeError(f"Angel One candle request failed for {symbol}: {last_error}") from last_error


def _login():
    """Login once per run. Credentials are read from environment variables (never hardcode them)."""
    global _api
    if _api is not None:
        return _api
    keys = ["ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET"]
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        sys.exit("Set these environment variables first: " + ", ".join(missing))
    api = SmartConnect(api_key=os.environ["ANGEL_API_KEY"])
    totp = pyotp.TOTP(os.environ["ANGEL_TOTP_SECRET"]).now()
    res = api.generateSession(os.environ["ANGEL_CLIENT_CODE"], os.environ["ANGEL_PIN"], totp)
    if not res or not res.get("status"):
        sys.exit(f"Angel One login failed: {res}")
    _api = api
    return api


def _token_map() -> dict:
    """NSE cash symbol ('RELIANCE-EQ') -> Angel symbol token, from the public scrip master (cached 24h)."""
    global _tokens
    if _tokens is None:
        stale = (not os.path.exists(SCRIP_MASTER_FILE)
                 or time.time() - os.path.getmtime(SCRIP_MASTER_FILE) > 86400)
        if stale:
            r = requests.get(SCRIP_MASTER_URL, timeout=60)
            r.raise_for_status()
            with open(SCRIP_MASTER_FILE, "w", encoding="utf-8") as f:
                f.write(r.text)
        with open(SCRIP_MASTER_FILE, encoding="utf-8") as f:
            data = json.load(f)
        _tokens = {x["symbol"]: x["token"] for x in data if x.get("exch_seg") == "NSE"}
    return _tokens


def _fetch(symbol: str, interval: str, days: int, chunk_days: int) -> pd.DataFrame:
    api = _login()
    token = INDEX_TOKENS.get(symbol) or _token_map().get(f"{symbol}-EQ")
    if token is None:
        print(f"[skip] {symbol}: not found in Angel scrip master")
        return pd.DataFrame()
    end = pd.Timestamp.now(tz=TZ).tz_localize(None).to_pydatetime()
    cur = end - dt.timedelta(days=days)
    rows, res = [], None
    while cur < end:
        nxt = min(cur + dt.timedelta(days=chunk_days), end)
        params = {"exchange": "NSE", "symboltoken": token, "interval": interval,
                  "fromdate": cur.strftime("%Y-%m-%d 09:15"),
                  "todate": nxt.strftime("%Y-%m-%d 15:30")}
        res = _get_candles_with_retry(api, params, symbol)
        rows.extend(res["data"])
        cur = nxt
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    idx = pd.DatetimeIndex(pd.to_datetime(df.pop("ts")))
    idx = idx.tz_localize(TZ) if idx.tz is None else idx.tz_convert(TZ)
    df.index = idx
    df = df.astype(float)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.dropna(subset=["close"])


def load_intraday(symbol: str, days: int) -> pd.DataFrame:
    """5-minute candles. `symbol` is a plain NSE name like 'RELIANCE', or 'Nifty 50'."""
    return _fetch(symbol, "FIVE_MINUTE", days, chunk_days=90)


def load_daily(symbol: str) -> pd.DataFrame:
    """Load previous-day levels from a persistent 24h cache to avoid repeated daily API calls."""
    os.makedirs(DAILY_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(DAILY_CACHE_DIR, f"{symbol.replace('/', '_')}.csv")
    if os.path.exists(cache_path) and time.time() - os.path.getmtime(cache_path) < DAILY_CACHE_MAX_AGE_SEC:
        try:
            cached = pd.read_csv(cache_path, index_col=0, parse_dates=True)
            if not cached.empty:
                cached.index = pd.DatetimeIndex(cached.index)
                cached.index = cached.index.tz_localize(TZ) if cached.index.tz is None else cached.index.tz_convert(TZ)
                return cached.astype(float)
        except Exception as exc:
            print(f"[warn] {symbol}: daily cache unreadable; refreshing: {exc}")
    daily = _fetch(symbol, "ONE_DAY", 200, chunk_days=400)
    if not daily.empty:
        daily.to_csv(cache_path)
    return daily


def add_vwap(d: pd.DataFrame) -> pd.DataFrame:
    """Per-day VWAP. Falls back to expanding mean of typical price if volume is zero (index)."""
    d = d.copy()
    tp = (d["high"] + d["low"] + d["close"]) / 3
    if d["volume"].sum() > 0:
        d["vwap"] = (tp * d["volume"]).cumsum() / d["volume"].cumsum().replace(0, np.nan)
        d["vwap"] = d["vwap"].ffill()
    else:
        d["vwap"] = tp.expanding().mean()
    return d


def index_state(idx_day: pd.DataFrame, ts) -> str:
    """'bull', 'bear' or 'neutral' for the index at candle time ts."""
    if idx_day is None or ts not in idx_day.index:
        return "neutral"
    row = idx_day.loc[ts]
    day_open = idx_day["open"].iloc[0]
    if row["close"] > row["vwap"] and row["close"] > day_open:
        return "bull"
    if row["close"] < row["vwap"] and row["close"] < day_open:
        return "bear"
    return "neutral"


def check_signal(d: pd.DataFrame, i: int, pdh: float, pdl: float, prev_close: float, idx_day):
    """Evaluate original PDH setups plus their mirrored PDL setups."""
    if i < ORB_CANDLES:
        return None
    ts = d.index[i]
    t = ts.time()
    if not (ENTRY_START <= t <= ENTRY_END):
        return None

    orb = d.iloc[:ORB_CANDLES]
    orb_h, orb_l = orb["high"].max(), orb["low"].min()
    bar = d.iloc[i]
    close = bar["close"]
    day_high = d["high"].iloc[: i + 1].max()
    day_low = d["low"].iloc[: i + 1].min()
    gap_pct = (d["open"].iloc[0] - prev_close) / prev_close * 100
    st = index_state(idx_day, ts)
    prior_vol = d["volume"].iloc[max(0, i - VOL_LOOKBACK): i]
    volume_ok = (
        len(prior_vol) >= 5
        and prior_vol.mean() > 0
        and bar["volume"] >= VOL_MULT * prior_vol.mean()
    )

    # ---- Scenario A (original): LONG breakout above ORB high + PDH ----
    if abs(gap_pct) <= GAP_LIMIT_PCT and close > max(orb_h, pdh) and st == "bull" and volume_ok:
        sl = orb_l if SL_MODE == "orb" else bar["vwap"]
        sl *= 1 - SL_BUFFER_PCT / 100
        risk = close - sl
        if 0 < risk and risk / close * 100 <= MAX_RISK_PCT:
            return dict(side="LONG", entry=close, sl=sl, target=close + 4.0 * risk, risk=risk)

    # ---- Scenario B (original): SHORT trap after PDH touch, then ORB-low breakdown ----
    touched_pdh = d["high"].iloc[: i + 1].max() >= pdh * 0.999
    if touched_pdh and close < orb_l and day_high < pdh * 1.01 and st == "bear":
        sl = max(day_high, pdh) * (1 + SL_BUFFER_PCT / 100)
        risk = sl - close
        if 0 < risk and risk / close * 100 <= MAX_RISK_PCT:
            return dict(side="SHORT", entry=close, sl=sl, target=close - 4.0 * risk, risk=risk)

    # ---- Scenario C (new mirror): SHORT breakdown below ORB low + PDL ----
    if abs(gap_pct) <= GAP_LIMIT_PCT and close < min(orb_l, pdl) and st == "bear" and volume_ok:
        sl = orb_h if SL_MODE == "orb" else bar["vwap"]
        sl *= 1 + SL_BUFFER_PCT / 100
        risk = sl - close
        if 0 < risk and risk / close * 100 <= MAX_RISK_PCT:
            return dict(side="SHORT", entry=close, sl=sl, target=close - RR * risk, risk=risk)

    # ---- Scenario D (new mirror): LONG false-breakdown trap after PDL touch ----
    touched_pdl = day_low <= pdl * 1.001
    if touched_pdl and close > orb_h and day_low > pdl * 0.99 and st == "bull":
        sl = min(day_low, pdl) * (1 - SL_BUFFER_PCT / 100)
        risk = close - sl
        if 0 < risk and risk / close * 100 <= MAX_RISK_PCT:
            return dict(side="LONG", entry=close, sl=sl, target=close + RR * risk, risk=risk)

    return None


def simulate_exit(d: pd.DataFrame, i: int, sig: dict) -> dict:
    side, entry, sl, tgt, risk = sig["side"], sig["entry"], sig["sl"], sig["target"], sig["risk"]
    exit_price, reason, exit_ts = None, "EOD", d.index[-1]
    lock_stage = 0
    for j in range(i + 1, len(d)):
        b = d.iloc[j]
        if b.name.time() >= SQUARE_OFF:
            exit_price, reason, exit_ts = b["open"], "EOD", b.name
            break
        if side == "LONG":
            if b["low"] <= sl:
                exit_price, reason, exit_ts = sl, ("LOCK_3R" if lock_stage == 2 else "LOCK_2R" if lock_stage == 1 else "SL"), b.name
                break
            if b["high"] >= tgt:
                exit_price, reason, exit_ts = tgt, "TARGET_4R", b.name
                break
            favorable_r = (float(b["high"]) - entry) / risk
            if favorable_r >= 3.0 and lock_stage < 2:
                sl, lock_stage = entry + 3.0 * risk, 2
            elif favorable_r >= 2.0 and lock_stage < 1:
                sl, lock_stage = entry + 2.0 * risk, 1
        else:
            if b["high"] >= sl:
                exit_price, reason, exit_ts = sl, ("LOCK_3R" if lock_stage == 2 else "LOCK_2R" if lock_stage == 1 else "SL"), b.name
                break
            if b["low"] <= tgt:
                exit_price, reason, exit_ts = tgt, "TARGET_4R", b.name
                break
            favorable_r = (entry - float(b["low"])) / risk
            if favorable_r >= 3.0 and lock_stage < 2:
                sl, lock_stage = entry - 3.0 * risk, 2
            elif favorable_r >= 2.0 and lock_stage < 1:
                sl, lock_stage = entry - 2.0 * risk, 1
    if exit_price is None:
        exit_price = d["close"].iloc[-1]
    pnl = (exit_price - entry) if side == "LONG" else (entry - exit_price)
    cost = entry * COST_PCT_ROUND_TRIP / 100
    return dict(exit=exit_price, exit_time=exit_ts, reason=reason,
                r_multiple=(pnl - cost) / risk, pnl_pct=(pnl - cost) / entry * 100)


def prev_levels(daily: pd.DataFrame) -> dict:
    """date -> (PDH, PDL, prev close) using the previous trading day."""
    out = {}
    dd = daily.copy()
    dd["date"] = dd.index.date
    rows = dd.reset_index(drop=True)
    for k in range(1, len(rows)):
        p = rows.iloc[k - 1]
        out[rows.iloc[k]["date"]] = (p["high"], p["low"], p["close"])
    # Angel's daily series may not contain today's partial candle yet: today's "previous day" is then the last row
    if len(rows):
        last = rows.iloc[-1]
        today_ist = pd.Timestamp.now(tz=TZ).date()
        if last["date"] < today_ist:
            out.setdefault(today_ist, (last["high"], last["low"], last["close"]))
    return out


def split_days(df: pd.DataFrame) -> dict:
    return {day: add_vwap(g) for day, g in df.groupby(df.index.date) if len(g) >= ORB_CANDLES + 2}


def backtest(days: int):
    idx_df = load_intraday(INDEX_SYMBOL, days)
    if idx_df.empty:
        sys.exit("Could not download index data (^NSEI). Check internet connection.")
    idx_days = split_days(idx_df)
    trades = []

    for sym in SYMBOLS:
        try:
            intr = load_intraday(sym, days)
            daily = load_daily(sym)
        except Exception as e:  # noqa: BLE001
            print(f"[skip] {sym}: {e}")
            continue
        if intr.empty or daily.empty:
            print(f"[skip] {sym}: no data")
            continue
        levels = prev_levels(daily)
        for day, d in split_days(intr).items():
            if day.isoformat() in NEWS_SKIP_DATES or day not in levels or day not in idx_days:
                continue
            pdh, pdl, pc = levels[day]
            for i in range(ORB_CANDLES, len(d)):
                sig = check_signal(d, i, pdh, pdl, pc, idx_days[day])
                if sig:
                    res = simulate_exit(d, i, sig)
                    trades.append(dict(symbol=sym, date=day, entry_time=d.index[i], **sig, **res))
                    break  # one trade per stock per day
        print(f"[done] {sym}")

    if not trades:
        print("\nNo trades generated for this period.")
        return
    tdf = pd.DataFrame(trades)
    tdf.to_csv("orb_pdh_trades.csv", index=False)

    wins = tdf[tdf["r_multiple"] > 0]
    losses = tdf[tdf["r_multiple"] <= 0]
    pf = wins["r_multiple"].sum() / abs(losses["r_multiple"].sum()) if len(losses) and losses["r_multiple"].sum() != 0 else float("inf")
    print("\n========== SUMMARY ==========")
    print(f"Trades        : {len(tdf)}  (LONG {sum(tdf.side=='LONG')}, SHORT {sum(tdf.side=='SHORT')})")
    print(f"Win rate      : {len(wins) / len(tdf) * 100:.1f}%")
    print(f"Net R         : {tdf['r_multiple'].sum():.2f}   (avg {tdf['r_multiple'].mean():.3f} R/trade)")
    print(f"Profit factor : {pf:.2f}")
    print(tdf.groupby("side")["r_multiple"].agg(["count", "sum", "mean"]).round(3))
    print(tdf.groupby("reason")["r_multiple"].agg(["count", "mean"]).round(3))
    print("Saved: orb_pdh_trades.csv")


def scan():
    today = dt.datetime.now(pd.Timestamp.now(tz=TZ).tz).date()
    if today.isoformat() in NEWS_SKIP_DATES:
        print("News skip date -> no trades today.")
        return
    idx_df = load_intraday(INDEX_SYMBOL, 5)
    idx_days = split_days(idx_df) if not idx_df.empty else {}
    idx_day = idx_days.get(today)
    if idx_day is None:
        print("Index data for today not available yet (market closed or before 9:30).")
        return
    print(f"Index state now: {index_state(idx_day, idx_day.index[-1])}")

    found = False
    for sym in SYMBOLS:
        try:
            intr = load_intraday(sym, 1)
            daily = load_daily(sym)
        except Exception:  # noqa: BLE001
            continue
        if intr.empty or daily.empty:
            continue
        levels = prev_levels(daily)
        d = split_days(intr).get(today)
        if d is None or today not in levels:
            continue
        pdh, pdl, pc = levels[today]
        i = len(d) - 1
        if i == 0:
            continue
        # last row may be a still-forming candle; use the previous completed one
        i = len(d) - 2 if len(d) > ORB_CANDLES + 1 else i
        sig = check_signal(d, i, pdh, pdl, pc, idx_day)
        if sig:
            found = True
            print(f"{d.index[i].strftime('%H:%M')} {sym:<12} {sig['side']:<5} entry {sig['entry']:.2f} "
                  f"SL {sig['sl']:.2f} TGT {sig['target']:.2f}")
    if not found:
        print("No signals on the latest closed 5m candle.")


def send_telegram(message):
    token, chat_id = os.getenv("BOT_TOKEN", "").strip(), os.getenv("CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Missing BOT_TOKEN or CHAT_ID GitHub secret")
    response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                             data={"chat_id": chat_id, "text": message}, timeout=20)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")
    print("Telegram alert sent.")


def append_paper_csv(path, row):
    pd.DataFrame([row]).to_csv(path, mode="a", header=not os.path.exists(path), index=False)


def load_paper_state(capital):
    if not os.path.exists(PAPER_STATE_FILE):
        return {"starting_capital": capital, "cash": capital, "positions": {},
                "last_processed_candle": None, "trade_count": 0}
    with open(PAPER_STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
    if float(state.get("starting_capital", capital)) != float(capital):
        raise RuntimeError("Saved paper account capital differs from configured ₹50,000; refusing reset.")
    if not isinstance(state.get("positions"), dict):
        raise RuntimeError("Invalid paper positions state.")
    return state


def manage_paper_exit(pos, bar, candle_time, cash):
    side, sl, target = pos["side"], float(pos["sl"]), float(pos["target"])
    entry, risk = float(pos["entry"]), float(pos["risk"])
    lock_stage = int(pos.get("lock_stage", 0))
    reason = exit_price = None
    if side == "LONG":
        if float(bar.low) <= sl:
            reason, exit_price = ("LOCK_3R" if lock_stage == 2 else "LOCK_2R" if lock_stage == 1 else "SL"), sl
        elif float(bar.high) >= target:
            reason, exit_price = "TARGET_4R", target
    else:
        if float(bar.high) >= sl:
            reason, exit_price = ("LOCK_3R" if lock_stage == 2 else "LOCK_2R" if lock_stage == 1 else "SL"), sl
        elif float(bar.low) <= target:
            reason, exit_price = "TARGET_4R", target
    if reason is None and candle_time.time() >= SQUARE_OFF:
        reason, exit_price = "EOD", float(bar.close)
    if reason is None:
        favorable_r = ((float(bar.high)-entry) if side == "LONG" else (entry-float(bar.low))) / risk
        if favorable_r >= 3.0 and lock_stage < 2:
            pos["sl"] = entry + 3.0*risk if side == "LONG" else entry - 3.0*risk
            pos["lock_stage"] = 2
        elif favorable_r >= 2.0 and lock_stage < 1:
            pos["sl"] = entry + 2.0*risk if side == "LONG" else entry - 2.0*risk
            pos["lock_stage"] = 1
        return None, cash
    qty = float(pos["qty"])
    gross = (exit_price-entry)*qty if side == "LONG" else (entry-exit_price)*qty
    cost = (entry+exit_price)*qty*COST_PCT_ROUND_TRIP/100.0
    net = gross-cost
    cash += float(pos["notional"])+net
    return {"symbol":pos["symbol"],"side":side,"entry_time":pos["entry_time"],
            "exit_time":candle_time.isoformat(),"entry":entry,"exit":exit_price,
            "quantity":qty,"notional_inr":pos["notional"],"sl":sl,"target":target,
            "reason":reason,"gross_pnl_inr":gross,"estimated_cost_inr":cost,
            "net_pnl_inr":net,"capital_after_exit_inr":cash,"mode":"PAPER_ONLY"}, cash


def paper_cycle(capital=DEFAULT_CAPITAL):
    """One 5-minute paper-only cycle; no Angel One order placement is used."""
    now = pd.Timestamp.now(tz=TZ)
    if now.weekday() >= 5 or now.date().isoformat() in NEWS_SKIP_DATES:
        print("Market closed/weekend or configured news skip date; no scan.")
        return
    # Avoid Angel One historical API calls outside the NSE cash-market session.
    if not (dt.time(9, 15) <= now.time() <= dt.time(15, 30)):
        print(f"Outside NSE market hours (09:15-15:30 IST); no API calls. Current time: {now:%H:%M:%S}")
        return
    cycle_start = time.monotonic()
    state = load_paper_state(capital)
    try:
        idx = load_intraday(INDEX_SYMBOL, 1)
    except AngelRateLimitError as exc:
        print(f"[DATA UNAVAILABLE] Nifty index request rate-limited: {exc}")
        return
    idx_days = split_days(idx) if not idx.empty else {}
    today = now.date()
    idx_day = idx_days.get(today)
    if idx_day is None or len(idx_day) < ORB_CANDLES+2:
        print("Nifty index candles unavailable or session not ready.")
        return
    # Exclude latest still-forming candle; Angel candle timestamps are candle start times.
    completed_idx = idx_day.iloc[:-1]
    if completed_idx.empty:
        print("No completed Nifty candle.")
        return
    ts = completed_idx.index[-1]
    if state.get("last_processed_candle") == ts.isoformat():
        print(f"Duplicate closed candle {ts.isoformat()}; no action.")
        return
    # A candle counts as "processed" only after EVERY symbol was scanned. If Angel One throttles us part-way, the
    # symbols already done are remembered here and the next cycle continues with the rest of the SAME candle.
    if state.get("scan_candle") != ts.isoformat():
        state["scan_candle"], state["scan_done"] = ts.isoformat(), []
    scan_done = set(state.get("scan_done", []))
    incomplete = False

    cash = float(state["cash"])
    positions = state.setdefault("positions", {})
    events, checked = [], 0

    # Manage existing paper positions first; SL is assumed before target if both hit in one bar.
    for sym in list(positions):
        try:
            if positions[sym].get("entry_time") == ts.isoformat():
                continue   # opened at this very candle's close in an earlier partial pass of the same candle
            intr = load_intraday(sym, 5)
            day = split_days(intr).get(today) if not intr.empty else None
            if day is None or ts not in day.index:
                continue
            closed, cash = manage_paper_exit(positions[sym], day.loc[ts], ts, cash)
            if closed:
                append_paper_csv(PAPER_TRADES_FILE, closed)
                events.append(f"NIFTY50 ORB/PDH PAPER EXIT {sym} {closed['side']}\nExit ₹{closed['exit']:.2f} ({closed['reason']})\nNet P&L ₹{closed['net_pnl_inr']:+.2f}\nCash ₹{cash:.2f}\nPAPER ONLY — no broker order.")
                del positions[sym]
            checked += 1
        except AngelRateLimitError as exc:
            incomplete = True   # position not managed for this candle yet -> candle stays unprocessed, retried next cycle
            print(f"[DATA UNAVAILABLE] {exc}")
            continue
        except Exception as exc:
            print(f"[position error] {sym}: {exc}")
            raise

    if state.get("trade_date") != today.isoformat():
        state["trade_date"] = today.isoformat()
        state["traded_symbols"] = []
    traded_symbols = set(state.get("traded_symbols", []))
    used = sum(float(p["notional"]) for p in positions.values())
    risk_budget = capital * RISK_PER_TRADE_PCT/100.0

    # Signal rules are kept as supplied; sizing is 1% capital risk per trade,
    # with total open notional capped at ₹50,000.
    if ENTRY_START <= ts.time() <= ENTRY_END:
        for sym in SYMBOLS:
            if sym in positions or sym in traded_symbols or cash <= 0 or sym in scan_done:
                continue
            if time.monotonic() - cycle_start > CYCLE_DEADLINE_SEC:
                incomplete = True
                print("[DATA UNAVAILABLE] Cycle time budget used; remaining symbols continue in the next cycle.")
                break
            try:
                intr, daily = load_intraday(sym, 1), load_daily(sym)
                scan_done.add(sym)
                if intr.empty or daily.empty:
                    continue
                day = split_days(intr).get(today)
                levels = prev_levels(daily)
                if day is None or today not in levels or ts not in day.index:
                    continue
                pdh, pdl, prev_close = levels[today]
                sig = check_signal(day, day.index.get_loc(ts), pdh, pdl, prev_close, completed_idx)
                if not sig:
                    continue
                qty = min(risk_budget/float(sig["risk"]),
                          max(0.0, capital-used)/float(sig["entry"]))
                if not np.isfinite(qty) or qty <= 0:
                    continue
                notional = qty*float(sig["entry"])
                if notional < 1:
                    continue
                traded_symbols.add(sym)
                positions[sym] = {"symbol":sym,"side":sig["side"],"entry":float(sig["entry"]),
                    "sl":float(sig["sl"]),"target":float(sig["entry"] + 4.0*float(sig["risk"]) if sig["side"] == "LONG" else sig["entry"] - 4.0*float(sig["risk"])),"risk":float(sig["risk"]),"lock_stage":0,"qty":float(qty),
                    "notional":float(notional),"entry_time":ts.isoformat()}
                cash -= notional
                used += notional
                events.append(f"NIFTY50 ORB/PDH PAPER {sig['side']} — {sym}\nEntry ₹{sig['entry']:.2f}\nSL ₹{sig['sl']:.2f}\nTarget ₹{sig['target']:.2f} (4R)\nQty {qty:.3f} | Notional ₹{notional:.2f}\nRisk budget ₹{risk_budget:.2f} | Capital ₹{capital:.2f}\nCandle {ts.isoformat()}\nPAPER ONLY — no broker order.")
                print(f"[PAPER ENTRY] {sym} {sig['side']} qty={qty:.3f} entry={sig['entry']:.2f} SL={sig['sl']:.2f} target={sig['target']:.2f}")
                checked += 1
            except AngelRateLimitError as exc:
                incomplete = True
                print(f"[DATA UNAVAILABLE] {exc}")
                break
            except Exception as exc:
                scan_done.add(sym)
                print(f"[data error] {sym}: {exc}")
                continue

    state.update({"cash":cash,"positions":positions,"traded_symbols":sorted(traded_symbols),
                  "scan_candle":ts.isoformat(),"scan_done":sorted(scan_done),"updated_at":now.isoformat()})
    if not incomplete:
        state["last_processed_candle"] = ts.isoformat()
    tmp = PAPER_STATE_FILE+".tmp"
    with open(tmp,"w",encoding="utf-8") as f:
        json.dump(state,f,indent=2)
    os.replace(tmp,PAPER_STATE_FILE)
    equity = cash+used
    if not incomplete:
        append_paper_csv(PAPER_EQUITY_FILE,{"time":ts.isoformat(),"cash_inr":cash,
            "reserved_capital_inr":used,"open_positions":len(positions),
            "equity_estimate_inr":equity,"events_this_cycle":checked})
    status_tail = " | SCAN INCOMPLETE, continues next cycle" if incomplete else ""
    print(f"PAPER STATUS | capital ₹{capital:.2f} | cash ₹{cash:.2f} | reserved ₹{used:.2f} | open positions {len(positions)} | symbols scanned {len(scan_done)}/{len(SYMBOLS)} for candle {ts:%H:%M}{status_tail}")
    for message in events:
        send_telegram(message)


def test_telegram():
    send_telegram("NIFTY50 ORB + PDH/PDL PAPER BOT — TELEGRAM TEST SUCCESSFUL\nCapital ₹50,000\nPAPER ONLY — no broker order sent.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ORB + PDH/PDL NIFTY50 strategy — paper only")
    ap.add_argument("mode", choices=["backtest", "scan", "paper", "test-telegram"])
    ap.add_argument("--days", type=int, default=55)
    ap.add_argument("--rr", type=float, default=None)
    ap.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    args = ap.parse_args()
    if args.rr is not None:
        RR = args.rr
    if args.rr is not None and float(args.rr) != 4.0:
        ap.error("This strategy uses fixed 4R final target with 2R and 3R profit locks.")
    if args.capital <= 0:
        ap.error("Capital must be positive")
    if args.mode == "backtest":
        backtest(args.days)
    elif args.mode == "scan":
        scan()
    elif args.mode == "paper":
        paper_cycle(args.capital)
    else:
        test_telegram()
