#!/usr/bin/env python3
"""
PART-07 MASTER — CoinDCX Futures PAPER ONLY
Combined-confirmation mode (Option B):
A trade is opened ONLY when:
  1) ORB + PDH/PDL session engine confirms direction on a closed 5m candle, AND
  2) PRO QUANT hybrid engine confirms the SAME direction on a closed 15m candle
     using 1H trend, Donchian, volume, RSI, approximate POC, sweep and FVG.

The two source strategies are confirmation filters, NOT independent trade engines.
Existing strategy files are not modified. No live order endpoints are used.
"""

import os, json, time, logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "PART-07_state.json"
TRADES_FILE = ROOT / "PART-07_trades.csv"
SIGNALS_FILE = ROOT / "PART-07_signals.csv"
UNIVERSE_FILE = ROOT / "PART-07_universe.csv"

PUB = "https://public.coindcx.com"
BASE = "https://api.coindcx.com"
CG = "https://api.coingecko.com/api/v3"
CANDLE_URL = PUB + "/market_data/candlesticks"
ACTIVE_URL = BASE + "/exchange/v1/derivatives/futures/data/active_instruments"
PRICE_URL = PUB + "/market_data/v3/current_prices/futures/rt"

# Capital/risk configuration selected for PART-07
MARGIN_USDT = float(os.getenv("PAPER_MARGIN_USDT", "100"))
LEVERAGE = float(os.getenv("PAPER_LEVERAGE", "30"))
MAX_NOTIONAL = MARGIN_USDT * LEVERAGE
RISK_FRACTION = float(os.getenv("RISK_FRACTION", "0.01"))
RISK_BUDGET = MARGIN_USDT * RISK_FRACTION
MIN_24H_VOLUME_USDT = float(os.getenv("MIN_24H_VOLUME_USDT", "10000000"))
TOP_N = int(os.getenv("TOP_N", "100"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "6"))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "3"))

# ORB engine — source thresholds retained
ORB_INTERVAL_MIN = 5
ORB_CANDLES = 3
ORB_VOL_MULT = 2.0
ORB_VOL_LOOKBACK = 20
ORB_BODY_MIN = 0.60
ORB_CLOSE_EXTREME = 0.25
ORB_TARGET_R = 4.0
ORB_ENTRY_CUTOFF_MIN = 90
ORB_TIME_STOP_BARS = 24
ORB_TIME_STOP_MIN_R = 1.0
ORB_PROTECT_MIN = 45
ORB_PROTECT_MIN_R = 0.5
ORB_PROTECT_LOCK_R = 0.0
SHORT_SL_BUFFER = 0.001
MIN_SESSION_FILL = 0.95

# PRO QUANT engine — source thresholds retained
ATR_MULT = 1.5
QUANT_TARGET_R = 4.0
QUANT_TIME_STOP_MINUTES = 120  # elapsed wall-clock time; scanner evaluates on closed 5m bars
QUANT_MIN_TREND_BARS = 205
QUANT_MIN_15M_BARS = 110
QUANT_MIN_1H_BARS = 60

# Fee model: taker 0.05% per side + 18% GST on fee
TAKER_FEE = 0.0005
GST_ON_FEE = 0.18

SESSION_CONFIG = {
    "NY": {"tz": ZoneInfo("America/New_York"), "start": "09:30", "end": "17:00"},
    "LONDON": {"tz": ZoneInfo("Europe/London"), "start": "08:00", "end": "15:30"},
}

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("PART-07")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "PART-07-PAPER/1.0"})


def http_json(url, params=None):
    r = HTTP.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()


def telegram(message):
    token = os.getenv("TG_TOKEN") or os.getenv("BOT_TOKEN")
    chat = os.getenv("TG_CHAT_ID") or os.getenv("CHAT_ID")
    if not token or not chat:
        log.info("Telegram not configured; add TG_TOKEN and TG_CHAT_ID secrets.")
        return
    try:
        r = HTTP.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": message}, timeout=20)
        r.raise_for_status()
    except Exception:
        log.exception("Telegram send failed")


def load_state():
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            state.setdefault("open", {})
            state.setdefault("last_5m", {})
            state.setdefault("last_15m", {})
            state.setdefault("daily", {})
            state.setdefault("trades", [])
            return state
        except Exception:
            log.exception("State file unreadable; starting a fresh PART-07 state.")
    return {"open": {}, "last_5m": {}, "last_15m": {}, "daily": {}, "trades": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    pd.DataFrame(state.get("trades", [])).to_csv(TRADES_FILE, index=False)


def append_rows(path, rows):
    if rows:
        pd.DataFrame(rows).to_csv(path, mode="a", header=not path.exists(), index=False)


def fetch_universe():
    """CoinGecko Top-N by market cap, 24h spot volume filter, intersect CoinDCX futures."""
    markets = http_json(CG + "/coins/markets", {
        "vs_currency": "usd", "order": "market_cap_desc",
        "per_page": TOP_N, "page": 1, "sparkline": "false"
    })
    excluded = {"USDT", "USDC", "DAI", "TUSD", "FDUSD", "USDE", "USDS"}
    by_symbol = {}
    for c in markets:
        sym = str(c.get("symbol", "")).upper()
        vol = float(c.get("total_volume") or 0)
        rank = int(c.get("market_cap_rank") or 999999)
        if sym and sym not in excluded and rank <= TOP_N and vol >= MIN_24H_VOLUME_USDT:
            by_symbol[sym] = {"rank": rank, "global_spot_volume_24h_usd": vol}

    instruments = http_json(ACTIVE_URL, {"margin_currency_short_name[]": "USDT"})
    if isinstance(instruments, dict):
        instruments = instruments.get("data", instruments.get("instruments", []))
    prices_payload = http_json(PRICE_URL)
    prices = prices_payload.get("prices", {}) if isinstance(prices_payload, dict) else {}

    out = []
    for pair in instruments if isinstance(instruments, list) else []:
        if not (isinstance(pair, str) and pair.startswith("B-") and pair.endswith("_USDT")):
            continue
        symbol = pair[2:-5]
        if symbol not in by_symbol:
            continue
        q = prices.get(pair, {})
        try:
            ltp = float(q.get("ls", 0))
            quote_vol = ltp * float(q.get("v", 0))
        except (TypeError, ValueError):
            continue
        if ltp <= 1.0 or quote_vol < MIN_24H_VOLUME_USDT:
            continue
        out.append({"pair": pair, "symbol": symbol, "ltp": ltp,
                    "coindcx_est_24h_quote_volume": quote_vol, **by_symbol[symbol]})
    out.sort(key=lambda x: x["coindcx_est_24h_quote_volume"], reverse=True)
    return out


def fetch_candles(pair, resolution, bars):
    seconds = 300 if resolution == "5" else 900 if resolution == "15" else 3600
    now = int(time.time())
    payload = http_json(CANDLE_URL, {
        "pair": pair, "from": now - seconds * (bars + 5),
        "to": now, "resolution": resolution, "pcode": "f"
    })
    raw = payload.get("data", payload.get("candles", [])) if isinstance(payload, dict) else payload
    if not raw:
        return pd.DataFrame()
    d = pd.DataFrame(raw)
    required = ["time", "open", "high", "low", "close", "volume"]
    if not set(required).issubset(d.columns):
        return pd.DataFrame()
    for col in required:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna().drop_duplicates("time").sort_values("time")
    d["time"] = pd.to_datetime(d["time"], unit="ms", utc=True)
    return d.set_index("time")[["open", "high", "low", "close", "volume"]]


def resample_15m(d5):
    if d5.empty:
        return d5
    d = d5.resample("15min", label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum"
    }).dropna()
    now = pd.Timestamp.now(tz="UTC")
    if len(d) and d.index[-1] + pd.Timedelta(minutes=15) > now:
        d = d.iloc[:-1]
    return d


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14):
    delta = s.diff()
    up = delta.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100 / (1 + up / down.replace(0, np.nan))


def atr(d, n=14):
    prev = d.close.shift()
    tr = pd.concat([d.high-d.low, (d.high-prev).abs(), (d.low-prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


def approximate_poc(d):
    # Candle-OHLCV approximation only; exchange tick-level volume-at-price is not available here.
    q = d.tail(96)
    if len(q) < 20 or q.volume.sum() <= 0:
        return float(q.close.iloc[-1])
    typical = (q.high + q.low + q.close) / 3
    hist, edges = np.histogram(typical, bins=24, weights=q.volume)
    i = int(np.argmax(hist))
    return float((edges[i] + edges[i+1]) / 2)


def quant_confirmation(d15, h1):
    """Return BUY/SELL only when original PRO QUANT gates and score >= MIN_SCORE pass."""
    if len(d15) < QUANT_MIN_15M_BARS or len(h1) < QUANT_MIN_1H_BARS:
        return None
    x, z = d15.copy(), h1.copy()
    x["rsi"] = rsi(x.close)
    x["atr"] = atr(x)
    x["vma"] = x.volume.rolling(20).mean()
    z["e20"], z["e50"], z["e200"] = ema(z.close, 20), ema(z.close, 50), ema(z.close, 200)
    c, hz = x.iloc[-1], z.iloc[-1]
    prev, recent = x.iloc[-21:-1], x.iloc[-11:-1]
    if len(prev) < 20 or pd.isna(c.atr) or c.atr <= 0 or pd.isna(c.rsi):
        return None
    bull = hz.e20 > hz.e50 and hz.close > hz.e20
    bear = hz.e20 < hz.e50 and hz.close < hz.e20
    if len(z) >= QUANT_MIN_TREND_BARS:
        bull = bull and hz.close > hz.e200
        bear = bear and hz.close < hz.e200
    hi, lo = float(prev.high.max()), float(prev.low.min())
    vr = float(c.volume / c.vma) if c.vma and c.vma > 0 else 0.0
    rng = max(float(c.high-c.low), 1e-12)
    body = abs(float(c.close-c.open)) / rng
    bull_sweep = c.low < recent.low.min() and c.close > recent.low.min()
    bear_sweep = c.high > recent.high.max() and c.close < recent.high.max()
    bull_fvg = c.low > x.iloc[-3].high
    bear_fvg = c.high < x.iloc[-3].low
    p = approximate_poc(x)
    long_score = (2*int(bull) + 2*int(c.close > hi) +
                  int(vr >= 1.5 and c.close > c.open) +
                  int(50 <= c.rsi <= 70) + int(c.close > p) +
                  int(bull_sweep) + int(bull_fvg) +
                  int(body >= .4 and c.close > c.open))
    short_score = (2*int(bear) + 2*int(c.close < lo) +
                   int(vr >= 1.5 and c.close < c.open) +
                   int(30 <= c.rsi <= 50) + int(c.close < p) +
                   int(bear_sweep) + int(bear_fvg) +
                   int(body >= .4 and c.close < c.open))
    side = "LONG" if long_score >= short_score else "SHORT"
    score = max(long_score, short_score)
    if score < MIN_SCORE:
        return None
    if side == "LONG" and not (bull and vr >= 1.2 and (c.close > hi or bull_sweep)):
        return None
    if side == "SHORT" and not (bear and vr >= 1.2 and (c.close < lo or bear_sweep)):
        return None
    dist = float(c.atr * ATR_MULT)
    entry = float(c.close)
    return {"side": side, "score": int(score), "entry": entry, "dist": dist,
            "stop": entry-dist if side == "LONG" else entry+dist,
            "target": entry+QUANT_TARGET_R*dist if side == "LONG" else entry-QUANT_TARGET_R*dist,
            "time": c.name.isoformat(), "rsi": float(c.rsi), "volume_ratio": vr, "poc": p}


def hm(value):
    h, m = value.split(":")
    return int(h)*60 + int(m)


def session_frame(d5, session):
    cfg = SESSION_CONFIG[session]
    local = d5.index.tz_convert(cfg["tz"])
    mins = local.hour*60 + local.minute
    mask = (mins >= hm(cfg["start"])) & (mins < hm(cfg["end"]))
    return d5.loc[mask].copy()


def orb_confirmation(d5, session):
    """Latest closed 5m ORB setup. Returns a directional signal only after 3 ORB bars."""
    g = session_frame(d5, session)
    if len(g) < ORB_CANDLES + 1:
        return None
    cfg = SESSION_CONFIG[session]
    local_now = datetime.now(timezone.utc).astimezone(cfg["tz"])
    today = local_now.date()
    g = g[g.index.tz_convert(cfg["tz"]).date == today]
    if len(g) < ORB_CANDLES + 1:
        return None
    # Previous complete session day provides PDH. A day is accepted at >=95% of expected bars.
    prior = session_frame(d5, session)
    prior_local = prior.index.tz_convert(cfg["tz"])
    expected = (hm(cfg["end"])-hm(cfg["start"])) // ORB_INTERVAL_MIN
    groups = {}
    for day, grp in prior.groupby(prior_local.date):
        if day < today and len(grp) >= int(expected*MIN_SESSION_FILL):
            groups[day] = grp
    if not groups:
        return None
    prev = groups[sorted(groups)[-1]]
    pdh = float(prev.high.max())
    pdl = float(prev.low.min())
    orb = g.iloc[:ORB_CANDLES]
    orb_high, orb_low = float(orb.high.max()), float(orb.low.min())
    bar = g.iloc[-1]
    ts = g.index[-1]
    left = hm(cfg["end"]) - (ts.tz_convert(cfg["tz"]).hour*60 + ts.tz_convert(cfg["tz"]).minute)
    if left <= ORB_ENTRY_CUTOFF_MIN:
        return None
    hist = d5.loc[:ts].iloc[:-1]
    if len(hist) < ORB_VOL_LOOKBACK:
        return None
    vol_avg = float(hist.volume.tail(ORB_VOL_LOOKBACK).mean())
    o,h,l,c,v = map(float, (bar.open,bar.high,bar.low,bar.close,bar.volume))
    rng = h-l
    if rng <= 0 or abs(c-o)/rng < ORB_BODY_MIN or v < ORB_VOL_MULT*vol_avg:
        return None
    run_high = float(g.loc[:ts].high.max())
    long_ok = (c > max(orb_high,pdh) and c > o and
               (c-l)/rng >= 1-ORB_CLOSE_EXTREME)
    # Short setup must clear BOTH the ORB low and previous-session low (PDL).
    short_ok = (c < min(orb_low,pdl) and c < o and
                (h-c)/rng >= 1-ORB_CLOSE_EXTREME)
    if long_ok and c > orb_low:
        return {"side":"LONG","entry":c,"stop":orb_low,"time":ts.isoformat(),"session":session}
    if short_ok and run_high* (1+SHORT_SL_BUFFER) > c:
        return {"side":"SHORT","entry":c,"stop":run_high*(1+SHORT_SL_BUFFER),"time":ts.isoformat(),"session":session}
    return None


def fee_cost(notional):
    return notional * TAKER_FEE * 2 * (1 + GST_ON_FEE)


def close_trade(pair, pos, price, timestamp, reason):
    direction = 1 if pos["side"] == "LONG" else -1
    gross = (price-pos["entry"]) * pos["qty"] * direction
    fees = fee_cost(pos["notional"])
    net = gross-fees
    return {
        "strategy":"PART-07_CONFLUENCE", "pair":pair, "side":pos["side"],
        "session":pos["session"], "entry_time":pos["entry_time"],
        "exit_time":timestamp, "entry":pos["entry"], "exit":price,
        "stop_initial":pos["initial_stop"], "stop_final":pos["stop"],
        "target":pos["target"], "quantity":pos["qty"], "notional_usdt":pos["notional"],
        "margin_est_usdt":pos["notional"]/LEVERAGE, "leverage":LEVERAGE,
        "gross_pnl_usdt":gross, "fees_gst_usdt":fees, "net_pnl_usdt":net,
        "net_r":net/max(pos["risk_budget"],1e-12), "exit_reason":reason,
        "quant_score":pos["quant_score"], "orb_time":pos["orb_time"],
        "quant_time":pos["quant_time"]
    }


def process_open(pair, pos, bar, ts, state, closed_rows):
    side = pos["side"]
    long_side = side == "LONG"
    cfg = SESSION_CONFIG.get(pos.get("session"), SESSION_CONFIG["NY"])
    local_ts = pd.Timestamp(ts).tz_convert(cfg["tz"])
    entry_local_ts = pd.Timestamp(pos["entry_time"]).tz_convert(cfg["tz"])
    local_minute = local_ts.hour * 60 + local_ts.minute
    session_end = hm(cfg["end"])

    # Flatten after the configured session closes; also recover stale positions after a missed run.
    if local_ts.date() > entry_local_ts.date() or local_minute >= session_end:
        price = float(bar.close)
        row = close_trade(pair, pos, price, ts.isoformat(), "SESSION_END")
        closed_rows.append(row)
        del state["open"][pair]
        telegram(f"PART-07 PAPER EXIT {side}\n{pair}\nReason: SESSION_END\nNet P&L: {row['net_pnl_usdt']:.4f} USDT")
        return

    # In the final 45 minutes, protect a trade once it has reached +0.5R.
    fav = (float(bar.high)-pos["entry"]) if long_side else (pos["entry"]-float(bar.low))
    r_now = fav / max(float(pos["risk_dist"]), 1e-12)
    if local_minute >= session_end - ORB_PROTECT_MIN and r_now >= ORB_PROTECT_MIN_R:
        be_stop = float(pos["entry"]) + (ORB_PROTECT_LOCK_R if long_side else -ORB_PROTECT_LOCK_R) * float(pos["risk_dist"])
        pos["stop"] = max(float(pos["stop"]), be_stop) if long_side else min(float(pos["stop"]), be_stop)

    old_stop = float(pos["stop"])
    hit_sl = bar.low <= old_stop if long_side else bar.high >= old_stop
    hit_tp = bar.high >= pos["target"] if long_side else bar.low <= pos["target"]
    # Conservative same-candle assumption: stop is checked before target.
    if hit_sl or hit_tp:
        price = old_stop if hit_sl else pos["target"]
        reason = "TARGET_4R" if hit_tp and not hit_sl else (
            "LOCKED_PROFIT" if ((old_stop > pos["entry"]) if long_side else (old_stop < pos["entry"])) else "SL")
        closed_rows.append(close_trade(pair,pos,float(price),ts.isoformat(),reason))
        del state["open"][pair]
        telegram(f"PART-07 PAPER EXIT {side}\n{pair}\nExit: {price:.8g}\nReason: {reason}\nNet P&L: {closed_rows[-1]['net_pnl_usdt']:.4f} USDT")
        return

    if r_now >= 3 and pos.get("lock",0) < 3:
        pos["stop"] = pos["entry"] + (3 if long_side else -3)*pos["risk_dist"]
        pos["lock"] = 3
    elif r_now >= 2 and pos.get("lock",0) < 2:
        pos["stop"] = pos["entry"] + (2 if long_side else -2)*pos["risk_dist"]
        pos["lock"] = 2

    entry_ts = pd.Timestamp(pos["entry_time"])
    bar_close_ts = pd.Timestamp(ts) + pd.Timedelta(minutes=5)
    if bar_close_ts - entry_ts >= pd.Timedelta(minutes=QUANT_TIME_STOP_MINUTES):
        row = close_trade(pair,pos,float(bar.close),ts.isoformat(),"TIME_STOP_2H")
        closed_rows.append(row)
        del state["open"][pair]
        telegram(f"PART-07 PAPER EXIT {side}\n{pair}\nReason: TIME_STOP_2H\nNet P&L: {row['net_pnl_usdt']:.4f} USDT")
    else:
        state["open"][pair] = pos


def run():
    state = load_state()
    universe = fetch_universe()
    if not universe:
        raise RuntimeError("No eligible CoinDCX USDT futures pairs found.")
    pd.DataFrame(universe).to_csv(UNIVERSE_FILE,index=False)
    today = datetime.now(timezone.utc).date().isoformat()
    state.setdefault("daily",{}).setdefault(today,[])
    new_rows, closed_rows = [], []
    log.info("PART-07 combined confirmation | eligible=%d | margin=%.2f | leverage=%.1fx | risk=%.2f",
             len(universe),MARGIN_USDT,LEVERAGE,RISK_BUDGET)

    for n,item in enumerate(universe,1):
        pair = item["pair"]
        try:
            d5 = fetch_candles(pair,"5",1500)
            h1 = fetch_candles(pair,"60",260)
            if d5.empty or h1.empty:
                continue
            # Drop the currently forming 5m candle.
            now = pd.Timestamp.now(tz="UTC")
            d5 = d5[d5.index + pd.Timedelta(minutes=5) <= now]
            d15 = resample_15m(d5)
            if len(d15) < QUANT_MIN_15M_BARS or len(h1) < QUANT_MIN_1H_BARS:
                continue
            latest5 = d5.index[-1]
            latest15 = d15.index[-1]
            if pair in state["open"]:
                pos = state["open"][pair]
                if state["last_5m"].get(pair) != latest5.isoformat():
                    process_open(pair,pos,d5.iloc[-1],latest5,state,closed_rows)
                    state["last_5m"][pair] = latest5.isoformat()
                continue
            if len(state["open"]) >= MAX_OPEN_TRADES or pair in state["daily"][today]:
                continue
            # Quant is evaluated only on a newly closed 15m candle.
            if state["last_15m"].get(pair) == latest15.isoformat():
                continue
            qsig = quant_confirmation(d15,h1)
            state["last_15m"][pair] = latest15.isoformat()
            if not qsig:
                continue
            # ORB is checked independently for both session definitions.
            for session in ("NY","LONDON"):
                osig = orb_confirmation(d5,session)
                if not osig or osig["side"] != qsig["side"]:
                    continue
                # Require ORB trigger to be recent: same 15m quant confirmation window.
                qtime = pd.Timestamp(qsig["time"])
                otime = pd.Timestamp(osig["time"])
                if otime < qtime or otime >= qtime + pd.Timedelta(minutes=15):
                    continue
                entry = float(d5.loc[otime].close)
                # Use the more conservative of the two independently calculated stops.
                qstop = float(qsig["stop"])
                ostop = float(osig["stop"])
                stop = min(qstop,ostop) if osig["side"]=="LONG" else max(qstop,ostop)
                dist = entry-stop if osig["side"]=="LONG" else stop-entry
                if dist <= 0:
                    continue
                stop_pct = dist/entry
                notional = min(MAX_NOTIONAL,RISK_BUDGET/stop_pct)
                if notional < 10:
                    continue
                target = entry + 4*dist if osig["side"]=="LONG" else entry - 4*dist
                pos = {
                    "side":osig["side"],"session":session,"entry":entry,
                    "stop":stop,"initial_stop":stop,"target":target,
                    "risk_dist":dist,"notional":notional,"qty":notional/entry,
                    "risk_budget":RISK_BUDGET,"quant_score":qsig["score"],
                    "orb_time":osig["time"],"quant_time":qsig["time"],
                    "entry_time":otime.isoformat(),"bars":0,"lock":0
                }
                state["open"][pair]=pos
                state["daily"][today].append(pair)
                row={"timestamp":otime.isoformat(),"pair":pair,"symbol":item["symbol"],
                     "session":session,"side":pos["side"],"quant_score":qsig["score"],
                     "entry":entry,"stop":stop,"target_4r":target,
                     "margin_est_usdt":notional/LEVERAGE,"notional_usdt":notional,
                     "leverage":LEVERAGE,"risk_budget_usdt":RISK_BUDGET,
                     "market_cap_rank":item["rank"],
                     "global_spot_volume_24h_usd":item["global_spot_volume_24h_usd"],
                     "coindcx_est_24h_quote_volume":item["coindcx_est_24h_quote_volume"],
                     "orb_time":osig["time"],"quant_time":qsig["time"],
                     "mode":"PAPER_ONLY","confirmation":"ORB_AND_PRO_QUANT_SAME_DIRECTION"}
                new_rows.append(row)
                telegram(f"PART-07 PAPER {pos['side']} CONFIRMED\n{pair} | {session}\nQuant score: {qsig['score']}\nEntry: {entry:.8g}\nSL: {stop:.8g}\nTP 4R: {target:.8g}\nMargin est.: {notional/LEVERAGE:.2f} USDT | Notional: {notional:.2f} USDT")
                log.info("PAPER ENTRY %s %s %s score=%d",pair,session,pos["side"],qsig["score"])
                break
        except Exception as exc:
            log.warning("[%d/%d] %s failed: %s",n,len(universe),pair,exc)
        time.sleep(0.15)

    append_rows(SIGNALS_FILE,new_rows)
    append_rows(TRADES_FILE,closed_rows)
    state.setdefault("trades",[]).extend(closed_rows)
    save_state(state)
    log.info("PART-07 finished | new=%d closed=%d open=%d",
             len(new_rows),len(closed_rows),len(state["open"]))


if __name__ == "__main__":
    run()