#!/usr/bin/env python3
"""
NIFTY 50 FUTURES COMBO STRATEGY  (Angel One SmartAPI data + local PAPER trading)
================================================================================
GOLD_COMBO_ALL_IN_ONE.py ka Nifty-futures version. Koi real order nahi bhejta.

GATES (sab zaroori)
  1. 1H Trend      : EMA50 vs EMA200 (+ price EMA200 ke sahi taraf)
  2. Liquidity Sweep: 15m swing low/high  YA  Previous-Day / Opening-Range (9:15-9:45) low/high
                      ka sweep + reclaim (SL-hunt reversal)  -- sweep usi din tak valid
  3. 5m Trigger    : close VWAP + EMA20 ke sahi taraf, RSI 50 ke sahi taraf, Volume > avg
  4. Time window   : 9:30-14:45 IST me hi entry, 15:15 par forced square-off (no overnight)
  5. News blackout : NEWS_EVENTS / NO_TRADE_DAYS (RBI, Budget, ...) ; optional expiry-day skip

BONUS SCORE (min_score): +1 BB squeeze breakout, +1 price POC ke sahi taraf, +1 BankNifty bias (optional --bnf csv)

RISK: SL = sweep level +- 0.2*ATR (0.5-2.5 ATR), TP1 = 2R, TP2 = 3R, 1% equity risk, LOTS me sizing.
      1 lot ho to partial exit nahi hota: TP1 par sirf SL -> breakeven, poori lot TP2/BE/EOD par.
      Indian costs (brokerage, STT, exchange, GST, stamp) + slippage points me.

SETUP (Angel One)
  pip install smartapi-python pyotp requests pandas numpy
  env: ANGEL_API_KEY  ANGEL_CLIENT_CODE  ANGEL_PIN  ANGEL_TOTP_SECRET   (+ optional BOT_TOKEN, CHAT_ID for Telegram)

USAGE
  python NIFTY_FUT_COMBO.py demo                              # nakli data, sirf code-check
  python NIFTY_FUT_COMBO.py backtest --csv nifty_fut_5m.csv   # time col: IST (naive) ya tz-aware / epoch
  python NIFTY_FUT_COMBO.py backtest --fetch --days 60
  python NIFTY_FUT_COMBO.py compare  --fetch --days 60
  python NIFTY_FUT_COMBO.py scan     --fetch
  python NIFTY_FUT_COMBO.py paper                             # ek cycle (cron har 5 min, market hours me)
  python NIFTY_FUT_COMBO.py paper-loop                        # khud har 5 min chalta rahega (9:15-15:35 IST)

DISCLAIMER: Research / paper-test tool. Edge Nifty par saabit nahi hai. 100+ trades ke backtest aur
3-4 hafte paper trading ke baad hi kuch socho. Financial advice nahi.
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

IST = "Asia/Kolkata"

# News blackout (IST). Format: ("YYYY-MM-DD HH:MM", minutes_before, minutes_after)
NEWS_EVENTS: list[tuple[str, int, int]] = [
    # ("2026-12-05 10:00", 30, 90),   # RBI policy example
]
NO_TRADE_DAYS: list[str] = [
    # "2027-02-01",   # Budget day example
]


# --------------------------------------------------------------------------- #
# CONFIG
# --------------------------------------------------------------------------- #
@dataclass
class Cfg:
    symbol: str = "NIFTY"
    # trend (1H)
    ema_fast: int = 50
    ema_slow: int = 200
    # 5m trigger
    ema_trig: int = 20
    rsi_len: int = 14
    vol_len: int = 20
    vol_mult: float = 1.0
    # sweep
    swing_lb_15m: int = 20
    sweep_valid_bars_5m: int = 24            # sweep ke baad 2h tak (aur sirf usi din)
    or_window: tuple = (9.25, 9.75)          # opening range 9:15-9:45 IST
    # time (IST hours)
    trade_window: tuple = (9.5, 14.75)       # entry window 9:30-14:45
    squareoff: float = 15.25                 # 15:15 forced exit
    skip_expiry_day: bool = False
    expiry_weekday: int = 1                  # Tuesday (exchange calendar verify karo)
    # scoring
    min_score: int = 1
    one_per_sweep: bool = True
    invalidate_sweep: bool = True
    max_cost_r: float = 0.35                 # round-trip cost > 0.35R to skip (0 = off)
    # bollinger / volume profile
    bb_len: int = 20
    bb_sq_lookback: int = 100
    poc_window: int = 75                     # 1 trading din = 75 bars
    poc_bins: int = 40
    poc_step: int = 6
    # risk
    atr_len: int = 14
    sl_buffer_atr: float = 0.2
    max_sl_atr: float = 2.5
    min_sl_atr: float = 0.5
    tp1_r: float = 2.0
    tp2_r: float = 3.0
    tp1_frac: float = 0.5
    risk_pct: float = 1.0
    start_equity: float = 300000.0           # INR
    lot_size: int = 65                       # --fetch par scrip master se auto-update (verify karo)
    lot_locked: bool = False
    margin_per_lot: float = 180000.0         # approx; broker margin calculator se check karo
    max_lots: int = 5
    min_lot_risk_mult: float = 1.5           # 1 lot ka risk budget ke 1.5x tak chalega
    max_bars_in_trade: int = 75
    # costs (Angel One F&O approx; apne contract note se verify karo)
    slip_pts: float = 1.0                    # per side, index points
    brokerage: float = 20.0                  # INR per order
    stt_sell_pct: float = 0.02               # futures, sell side
    txn_pct: float = 0.00183                 # NSE futures, both sides
    sebi_pct: float = 0.0001
    stamp_buy_pct: float = 0.002
    gst_pct: float = 18.0


def fill_cost(cfg: Cfg, price: float, qty: float, is_sell: bool) -> float:
    """Ek order ka INR cost (slippage alag se points me)."""
    turn = price * qty
    txn, sebi = turn * cfg.txn_pct / 100, turn * cfg.sebi_pct / 100
    gst = (cfg.brokerage + txn + sebi) * cfg.gst_pct / 100
    stt = turn * cfg.stt_sell_pct / 100 if is_sell else 0.0
    stamp = 0.0 if is_sell else turn * cfg.stamp_buy_pct / 100
    return cfg.brokerage + txn + sebi + gst + stt + stamp


# --------------------------------------------------------------------------- #
# DATA
# --------------------------------------------------------------------------- #
def _to_ist_index(idx) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(idx)
    return idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.lower() for c in df.columns]
    tcol = "time" if "time" in df.columns else df.columns[0]
    t = df[tcol]
    if np.issubdtype(t.dtype, np.number):
        unit = "ms" if t.iloc[0] > 1e11 else "s"
        idx = _to_ist_index(pd.to_datetime(t, unit=unit, utc=True))
    else:
        idx = _to_ist_index(pd.to_datetime(t))      # naive = IST maana jayega
    df.index = idx
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    return df[~df.index.duplicated()].sort_index()


SCRIP_URL = "https://margincalculator.angelbroking.com/OpenAPI_Files/files/OpenAPIScripMaster.json"
_SESSION: dict = {"obj": None}


def angel_login(force: bool = False):
    if _SESSION["obj"] is not None and not force:
        return _SESSION["obj"]
    from SmartApi import SmartConnect
    import pyotp
    key, client = os.getenv("ANGEL_API_KEY"), os.getenv("ANGEL_CLIENT_CODE")
    pin, secret = os.getenv("ANGEL_PIN"), os.getenv("ANGEL_TOTP_SECRET")
    if not all([key, client, pin, secret]):
        raise RuntimeError("ANGEL_API_KEY / ANGEL_CLIENT_CODE / ANGEL_PIN / ANGEL_TOTP_SECRET env set karo")
    obj = SmartConnect(api_key=key)
    res = obj.generateSession(client, pin, pyotp.TOTP(secret).now())
    if not res or not res.get("status"):
        raise RuntimeError(f"Angel login failed: {res.get('message') if isinstance(res, dict) else res}")
    _SESSION["obj"] = obj
    return obj


def current_future(cfg: Cfg, roll_days: int = 2) -> dict:
    """Scrip master se near-month NIFTY future (expiry se roll_days pehle next month par roll)."""
    import requests
    cache = Path("angel_scrip_master.json")
    if not cache.exists() or time.time() - cache.stat().st_mtime > 12 * 3600:
        r = requests.get(SCRIP_URL, timeout=90)
        r.raise_for_status()
        cache.write_bytes(r.content)
    data = json.loads(cache.read_text(encoding="utf-8"))
    today = pd.Timestamp.now(tz=IST).tz_localize(None).normalize()
    rows = []
    for x in data:
        if x.get("exch_seg") == "NFO" and x.get("instrumenttype") == "FUTIDX" and x.get("name") == cfg.symbol:
            exp = datetime.strptime(x["expiry"].title(), "%d%b%Y")
            if pd.Timestamp(exp) >= today + pd.Timedelta(days=roll_days):
                rows.append((exp, x))
    if not rows:
        raise RuntimeError(f"{cfg.symbol} futures scrip master me nahi mile")
    exp, x = min(rows, key=lambda t: t[0])
    return {"token": x["token"], "tsym": x["symbol"], "expiry": exp, "lot": int(float(x["lotsize"]))}


def fetch_angel(cfg: Cfg, days: int = 60, interval: str = "5m") -> pd.DataFrame:
    ival = {"5m": "FIVE_MINUTE", "15m": "FIFTEEN_MINUTE", "1h": "ONE_HOUR"}[interval]
    mins = {"5m": 5, "15m": 15, "1h": 60}[interval]
    inst = current_future(cfg)
    if not cfg.lot_locked:
        cfg.lot_size = inst["lot"]
    end = pd.Timestamp.now(tz=IST).tz_localize(None)
    start = end - pd.Timedelta(days=days)
    rows, cur = [], start
    while cur < end:
        nxt = min(cur + pd.Timedelta(days=25), end)
        params = {"exchange": "NFO", "symboltoken": inst["token"], "interval": ival,
                  "fromdate": cur.strftime("%Y-%m-%d %H:%M"), "todate": nxt.strftime("%Y-%m-%d %H:%M")}
        for attempt in range(3):
            try:
                res = angel_login(force=attempt > 0).getCandleData(params)
                if not isinstance(res, dict) or not res.get("status"):
                    raise RuntimeError(f"candle response: {str(res)[:200]}")
                rows.extend(res.get("data") or [])
                break
            except Exception as exc:
                if attempt == 2:
                    raise RuntimeError(f"Angel candle fetch failed ({inst['tsym']}): {exc}") from exc
                time.sleep(2 * (attempt + 1))
        cur = nxt
        time.sleep(0.5)                                    # rate limit
    if not rows:
        raise RuntimeError(f"{inst['tsym']} ke liye candles nahi mile")
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["time"], utc=True).dt.tz_convert(IST)
    df = df[["open", "high", "low", "close", "volume"]].apply(pd.to_numeric, errors="coerce").dropna()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[(df["high"] >= df["low"]) & (df["close"] > 0)]
    hrs = df.index.hour + df.index.minute / 60
    df = df[(hrs >= 9.25) & (hrs < 15.5)]
    df = df[df.index + pd.Timedelta(minutes=mins) <= pd.Timestamp.now(tz=IST)]   # incomplete bar hatao
    if len(df) < 200:
        raise RuntimeError(f"Kam candles ({len(df)}) for {inst['tsym']}")
    print(f"Fetched {len(df)} {interval} candles | {inst['tsym']} | lot {cfg.lot_size} | expiry {inst['expiry']:%d-%b-%Y}", flush=True)
    return df


def synthetic(days: int = 90, seed: int = 7) -> pd.DataFrame:
    """Sirf code test ke liye nakli data. Result ka koi matlab nahi."""
    rng = np.random.default_rng(seed)
    bd = pd.bdate_range("2026-06-01", periods=days, tz=IST)
    idx = pd.DatetimeIndex([d + pd.Timedelta(hours=9, minutes=15) + pd.Timedelta(minutes=5 * k)
                            for d in bd for k in range(75)])
    n = len(idx)
    k = np.tile(np.arange(75), days)
    u = 1.0 + 1.2 * np.exp(-k / 8.0) + 0.8 * np.exp(-(74 - k) / 8.0)       # U-shape volume/vol
    drift = 0.00003 * np.sin(np.arange(n) / 900)
    close = 24000 * np.exp(np.cumsum(rng.normal(drift, 0.0004 * u / 1.5)))
    open_ = np.r_[close[0], close[:-1]]
    rg = np.abs(rng.normal(0, 0.0003, n)) * close
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + rg,
                         "low": np.minimum(open_, close) - rg,
                         "volume": rng.gamma(2.0, 400, n) * u, "close": close}, index=idx)[
        ["open", "high", "low", "close", "volume"]]


# --------------------------------------------------------------------------- #
# INDICATORS
# --------------------------------------------------------------------------- #
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(df, n=14):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def session_vwap(df):
    """Daily (IST) reset VWAP."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    day = df.index.floor("D")
    pv = (tp * df["volume"]).groupby(day).cumsum()
    v = df["volume"].groupby(day).cumsum().replace(0, np.nan)
    return pv / v


def rolling_poc(df, window, bins, step=6):
    c, v = df["close"].to_numpy(), df["volume"].to_numpy()
    out = np.full(len(df), np.nan)
    for i in range(window, len(df), step):
        cs, vs = c[i - window:i], v[i - window:i]
        lo, hi = cs.min(), cs.max()
        if hi <= lo:
            continue
        hist, edges = np.histogram(cs, bins=bins, range=(lo, hi), weights=vs)
        k = hist.argmax()
        out[i:i + step] = (edges[k] + edges[k + 1]) / 2
    return pd.Series(out, index=df.index).ffill()


def htf(df, rule):
    """Higher TF resample, bins NSE session (9:15) se aligned. Index = us bar ke aakhri base candle ka
    time (jab HTF close hota hai) -> no look-ahead. Din ka aakhri adhoora bar bhi sahi label hota hai."""
    tmp = df.copy()
    tmp["_t"] = df.index
    o = tmp.resample(rule, label="left", closed="left", offset="15min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "_t": "last"})
    o = o.dropna(subset=["open"])
    o.index = pd.DatetimeIndex(o["_t"])
    return o.drop(columns="_t")


def to_5m(series_htf: pd.Series, idx5: pd.DatetimeIndex, limit=None):
    return series_htf.reindex(idx5.union(series_htf.index)).ffill(limit=limit).reindex(idx5)


def carry_same_day(level: pd.Series, idx5: pd.DatetimeIndex, limit: int) -> pd.Series:
    """HTF sweep level ko 5m par aage le jao, par raat bhar nahi (sirf usi din)."""
    lv = level.dropna()
    if lv.empty:
        return pd.Series(np.nan, index=idx5)
    dno = pd.Series(lv.index.normalize().asi8.astype(float), index=lv.index)
    lvl, dd = to_5m(lv, idx5, limit), to_5m(dno, idx5, limit)
    today = pd.Series(idx5.normalize().asi8.astype(float), index=idx5)
    return lvl.where(dd == today)


def bias_from_external(ext, idx5, ema_len=50, rule="1h"):
    """+1 = external index 1H EMA ke upar (strong), -1 = neeche, 0 = data nahi."""
    if ext is None or ext.empty:
        return pd.Series(0, index=idx5)
    h = htf(ext, rule)["close"]
    return to_5m(np.sign(h - ema(h, ema_len)), idx5).fillna(0)


# --------------------------------------------------------------------------- #
# SIGNAL ENGINE
# --------------------------------------------------------------------------- #
def in_window(hours: pd.Series, win) -> pd.Series:
    return (hours >= win[0]) & (hours < win[1])


def build_signals(df: pd.DataFrame, cfg: Cfg, bnf=None) -> pd.DataFrame:
    d = df.copy()
    d.index = _to_ist_index(d.index)
    idx = d.index
    hrs = pd.Series(idx.hour + idx.minute / 60, index=idx)
    day = idx.floor("D")

    d["ema20"] = ema(d["close"], cfg.ema_trig)
    d["rsi"] = rsi(d["close"], cfg.rsi_len)
    d["atr"] = atr(d, cfg.atr_len)
    d["vwap"] = session_vwap(d)
    d["vol_ok"] = d["volume"] > cfg.vol_mult * d["volume"].rolling(cfg.vol_len).mean()
    d["poc"] = rolling_poc(d, cfg.poc_window, cfg.poc_bins, cfg.poc_step)

    ma = d["close"].rolling(cfg.bb_len).mean()
    sd = d["close"].rolling(cfg.bb_len).std()
    bw = (4 * sd) / ma
    squeeze = bw <= bw.rolling(cfg.bb_sq_lookback).quantile(0.2)
    recent_sq = squeeze.rolling(12).max().fillna(0).astype(bool)
    d["bb_up"], d["bb_dn"] = ma + 2 * sd, ma - 2 * sd

    ohlcv = d[["open", "high", "low", "close", "volume"]]
    h1 = htf(ohlcv, "1h")
    e_f, e_s = ema(h1["close"], cfg.ema_fast), ema(h1["close"], cfg.ema_slow)
    up = ((e_f > e_s) & (h1["close"] > e_s)).astype(float)
    dn = ((e_f < e_s) & (h1["close"] < e_s)).astype(float)
    d["trend_up"] = to_5m(up, idx).fillna(0).astype(bool)
    d["trend_dn"] = to_5m(dn, idx).fillna(0).astype(bool)

    # ---- 15m swing sweep
    m15 = htf(ohlcv, "15min")
    sw_lo = m15["low"].rolling(cfg.swing_lb_15m).min().shift(1)
    sw_hi = m15["high"].rolling(cfg.swing_lb_15m).max().shift(1)
    sweep_l = (m15["low"] < sw_lo) & (m15["close"] > sw_lo)
    sweep_s = (m15["high"] > sw_hi) & (m15["close"] < sw_hi)
    L = cfg.sweep_valid_bars_5m
    sweep_low_15 = carry_same_day(m15["low"].where(sweep_l), idx, L)
    sweep_high_15 = carry_same_day(m15["high"].where(sweep_s), idx, L)

    # ---- Previous-day + Opening-range sweep (5m)
    daily = d.groupby(day).agg(h=("high", "max"), l=("low", "min"))
    dser = pd.Series(day, index=idx)
    pdh, pdl = dser.map(daily["h"].shift(1)), dser.map(daily["l"].shift(1))
    in_or = in_window(hrs, cfg.or_window)
    after_or = hrs >= cfg.or_window[1]
    or_hi = d["high"].where(in_or).groupby(day).transform("max").where(after_or)
    or_lo = d["low"].where(in_or).groupby(day).transform("min").where(after_or)
    ev_l = ((d["low"] < pdl) & (d["close"] > pdl)) | ((d["low"] < or_lo) & (d["close"] > or_lo))
    ev_s = ((d["high"] > pdh) & (d["close"] < pdh)) | ((d["high"] > or_hi) & (d["close"] < or_hi))
    sweep_low_5 = d["low"].where(ev_l).groupby(day).ffill(limit=L)
    sweep_high_5 = d["high"].where(ev_s).groupby(day).ffill(limit=L)

    sw_low = pd.concat([sweep_low_15, sweep_low_5], axis=1).min(axis=1)
    sw_high = pd.concat([sweep_high_15, sweep_high_5], axis=1).max(axis=1)
    sid_l = (sw_low != sw_low.shift()).cumsum()
    sid_s = (sw_high != sw_high.shift()).cumsum()
    if cfg.invalidate_sweep:
        failed_l = (d["close"] < sw_low).astype(int).groupby(sid_l).cummax().astype(bool)
        failed_s = (d["close"] > sw_high).astype(int).groupby(sid_s).cummax().astype(bool)
        sw_low, sw_high = sw_low.where(~failed_l), sw_high.where(~failed_s)
    d["sweep_low"], d["sweep_high"] = sw_low, sw_high

    # ---- Time window + news
    d["session"] = in_window(hrs, cfg.trade_window)
    blk = pd.Series(False, index=idx)
    for ts, bef, aft in NEWS_EVENTS:
        t = pd.Timestamp(ts, tz=IST)
        blk |= (idx >= t - pd.Timedelta(minutes=bef)) & (idx <= t + pd.Timedelta(minutes=aft))
    if NO_TRADE_DAYS:
        blk |= pd.Series(idx.strftime("%Y-%m-%d"), index=idx).isin(NO_TRADE_DAYS)
    if cfg.skip_expiry_day:
        blk |= pd.Series(idx.weekday == cfg.expiry_weekday, index=idx)
    d["news"] = blk

    d["bnf_bias"] = bias_from_external(bnf, idx)

    # ---- Gates + score
    base_ok = d["session"] & ~d["news"]
    trig_l = (d["close"] > d["vwap"]) & (d["close"] > d["ema20"]) & (d["rsi"] > 50) & d["vol_ok"]
    trig_s = (d["close"] < d["vwap"]) & (d["close"] < d["ema20"]) & (d["rsi"] < 50) & d["vol_ok"]
    gate_l = base_ok & d["trend_up"] & d["sweep_low"].notna() & trig_l
    gate_s = base_ok & d["trend_dn"] & d["sweep_high"].notna() & trig_s
    d["score_l"] = (((d["close"] > d["bb_up"].shift()) & recent_sq).astype(int)
                    + (d["close"] > d["poc"]).astype(int) + (d["bnf_bias"] > 0).astype(int))
    d["score_s"] = (((d["close"] < d["bb_dn"].shift()) & recent_sq).astype(int)
                    + (d["close"] < d["poc"]).astype(int) + (d["bnf_bias"] < 0).astype(int))
    d["long"] = gate_l & (d["score_l"] >= cfg.min_score)
    d["short"] = gate_s & (d["score_s"] >= cfg.min_score)
    if cfg.one_per_sweep:
        d["long"] = d["long"] & d["long"].astype(int).groupby(sid_l).cumsum().eq(1)
        d["short"] = d["short"] & d["short"].astype(int).groupby(sid_s).cumsum().eq(1)
    return d


# --------------------------------------------------------------------------- #
# SIZING / PLAN
# --------------------------------------------------------------------------- #
def plan_trade(entry: float, anchor: float, atr_v: float, side: int, cfg: Cfg):
    """(SL distance pts, cost in R) ya None agar cost bahut zyada."""
    if not (entry > 0 and atr_v > 0) or np.isnan(anchor):
        return None
    sl = anchor - side * cfg.sl_buffer_atr * atr_v
    dist = min(max(abs(entry - sl), cfg.min_sl_atr * atr_v), cfg.max_sl_atr * atr_v)
    lot = cfg.lot_size
    rt = fill_cost(cfg, entry, lot, False) + fill_cost(cfg, entry, lot, True) + 2 * cfg.slip_pts * lot
    cost_r = rt / (dist * lot)
    if cfg.max_cost_r and cost_r > cfg.max_cost_r:
        return None
    return dist, cost_r


def size_lots(equity: float, dist: float, cfg: Cfg) -> int:
    lot_risk = dist * cfg.lot_size
    budget = equity * cfg.risk_pct / 100
    lots = int(budget // lot_risk)
    if lots < 1 and lot_risk <= budget * cfg.min_lot_risk_mult:
        lots = 1
    lots = min(lots, cfg.max_lots, int(equity * 0.9 // cfg.margin_per_lot))
    return max(lots, 0)


# --------------------------------------------------------------------------- #
# BACKTEST
# --------------------------------------------------------------------------- #
@dataclass
class Trade:
    side: int
    entry_t: pd.Timestamp
    entry: float
    fill_entry: float
    sl: float
    tp1: float
    tp2: float
    qty: float
    lots: int
    tp1_qty: float
    risk: float
    score: int
    exit_t: pd.Timestamp | None = None
    pnl: float = 0.0
    reason: str = ""
    tp1_hit: bool = False
    qty_left: float = field(default=0.0)
    fees: float = 0.0          # fees + slippage (pnl + fees = gross)
    tp1_i: int = -1


def backtest(sig: pd.DataFrame, cfg: Cfg):
    eq = cfg.start_equity
    trades: list[Trade] = []
    curve = []
    pos: Trade | None = None
    o, h, l, c = (sig[k].to_numpy() for k in ("open", "high", "low", "close"))
    a = sig["atr"].to_numpy()
    lg, sh = sig["long"].to_numpy(), sig["short"].to_numpy()
    sl_low, sl_high = sig["sweep_low"].to_numpy(), sig["sweep_high"].to_numpy()
    scl, scs = sig["score_l"].to_numpy(), sig["score_s"].to_numpy()
    idx = sig.index
    hrs = (idx.hour + idx.minute / 60).to_numpy()
    dno = idx.normalize().asi8
    bars_in = 0

    def close_part(t: Trade, qty, price):
        nonlocal eq
        fill = price - t.side * cfg.slip_pts
        fee = fill_cost(cfg, fill, qty, is_sell=(t.side == 1))
        net = (fill - t.fill_entry) * t.side * qty - fee
        eq += net
        t.pnl += net
        t.fees += fee + cfg.slip_pts * qty

    for i in range(1, len(sig)):
        if pos is not None:
            bars_in += 1
            s = pos.side
            done = False
            if hrs[i] >= cfg.squareoff or dno[i] != dno[i - 1]:
                close_part(pos, pos.qty_left, o[i])
                pos.reason, done = "EOD", True
            else:
                sl_hit = (l[i] <= pos.sl) if s == 1 else (h[i] >= pos.sl)
                tp1_hit = (h[i] >= pos.tp1) if s == 1 else (l[i] <= pos.tp1)
                tp2_hit = (h[i] >= pos.tp2) if s == 1 else (l[i] <= pos.tp2)
                if sl_hit:
                    px = min(o[i], pos.sl) if s == 1 else max(o[i], pos.sl)
                    close_part(pos, pos.qty_left, px)
                    pos.reason, done = ("BE/SL" if pos.tp1_hit else "SL"), True
                else:
                    if not pos.tp1_hit and tp1_hit:
                        if pos.tp1_qty > 0:
                            close_part(pos, pos.tp1_qty, pos.tp1)
                            pos.qty_left -= pos.tp1_qty
                        pos.tp1_hit, pos.tp1_i = True, i
                        pos.sl = pos.entry                      # breakeven
                    if pos.tp1_hit and pos.tp1_i < i and tp2_hit:
                        close_part(pos, pos.qty_left, pos.tp2)
                        pos.reason, done = "TP2", True
                    elif bars_in >= cfg.max_bars_in_trade:
                        close_part(pos, pos.qty_left, c[i])
                        pos.reason, done = "TIME", True
            if done:
                pos.exit_t = idx[i]
                trades.append(pos)
                pos, bars_in = None, 0

        # entry: signal bar i-1 close -> fill at open[i] (usi din)
        if pos is None and dno[i] == dno[i - 1] and not np.isnan(a[i - 1]) and (lg[i - 1] or sh[i - 1]):
            side = 1 if lg[i - 1] else -1
            entry = o[i]
            anchor = sl_low[i - 1] if side == 1 else sl_high[i - 1]
            plan = plan_trade(entry, anchor, a[i - 1], side, cfg)
            lots = size_lots(eq, plan[0], cfg) if (plan is not None and eq > 0) else 0
            if lots > 0:
                dist = plan[0]
                qty = lots * cfg.lot_size
                fill_e = entry + side * cfg.slip_pts
                efee = fill_cost(cfg, fill_e, qty, is_sell=(side == -1))
                eq -= efee
                pos = Trade(side, idx[i], entry, fill_e, entry - side * dist,
                            entry + side * cfg.tp1_r * dist, entry + side * cfg.tp2_r * dist,
                            qty, lots, int(lots * cfg.tp1_frac) * cfg.lot_size, qty * dist,
                            int(scl[i - 1] if side == 1 else scs[i - 1]), qty_left=qty)
                pos.pnl = -efee
                pos.fees = efee + cfg.slip_pts * qty
                bars_in = 0
        curve.append(eq)
    return trades, pd.Series(curve, index=idx[1:])


def stats_row(trades, curve, cfg):
    if not trades:
        return dict(trades=0, win_pct=np.nan, pf=np.nan, netR=np.nan, grossR=np.nan, net_pct=0.0, maxDD_pct=0.0)
    pnl = np.array([t.pnl for t in trades])
    risk = np.array([t.risk for t in trades])
    fees = np.array([t.fees for t in trades])
    gp, gl = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    return dict(trades=len(trades), win_pct=(pnl > 0).mean() * 100, pf=(gp / gl if gl > 0 else np.inf),
                netR=(pnl / risk).mean(), grossR=((pnl + fees) / risk).mean(),
                net_pct=pnl.sum() / cfg.start_equity * 100, maxDD_pct=(curve / curve.cummax() - 1).min() * 100)


def compare(df, cfg, bnf=None, oos_frac=0.4):
    variants = {
        "old-style": dict(one_per_sweep=False, invalidate_sweep=False, max_cost_r=0.0, min_score=2),
        "no-cost-filter": dict(max_cost_r=0.0),
        "default": dict(),
        "min_score=2": dict(min_score=2),
        "wide-SL(min1ATR)": dict(min_sl_atr=1.0),
        "TP1=1.5R": dict(tp1_r=1.5),
    }
    split = int(len(df) * (1 - oos_frac))
    rows = []
    for name, kw in variants.items():
        c = dataclasses.replace(cfg, **kw)
        sig = build_signals(df, c, bnf=bnf)
        for part, s in (("ALL", sig), ("OOS", sig.iloc[split:])):
            tr, cv = backtest(s, c)
            rows.append({"variant": name, "part": part, **stats_row(tr, cv, c)})
    print(f"\n===== {cfg.symbol} COMPARE (OOS = last {int(oos_frac * 100)}%) =====")
    print(pd.DataFrame(rows).round(2).to_string(index=False))
    print("\ngrossR > 0 par netR < 0  => edge hai par costs kha rahe hain.  grossR <= 0 => logic me edge nahi.")
    print("100+ trades se kam par bharosa mat karo.")


def report(trades, curve, cfg):
    print(f"\n===== {cfg.symbol} FUT BACKTEST (INR, lot {cfg.lot_size}) =====")
    if not trades:
        print("Koi trade nahi bana. Filters strict hain, data kam hai, ya cost/lot-size filter ne rok diya.")
        return
    pnl = np.array([t.pnl for t in trades])
    r = np.array([t.pnl / t.risk for t in trades])
    rg = np.array([(t.pnl + t.fees) / t.risk for t in trades])
    gp, gl = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    print(f"Trades        : {len(trades)}  (Long {sum(t.side == 1 for t in trades)} / Short {sum(t.side == -1 for t in trades)})")
    print(f"Win rate      : {(pnl > 0).mean() * 100:.1f}%")
    print(f"Profit factor : {gp / gl:.2f}" if gl > 0 else "Profit factor : inf")
    print(f"Avg R / trade : {r.mean():.2f}R net | {rg.mean():.2f}R gross | cost+slip {rg.mean() - r.mean():.2f}R/trade")
    print(f"Net PnL       : Rs {pnl.sum():,.0f}  ({pnl.sum() / cfg.start_equity * 100:.1f}%)")
    print(f"Max drawdown  : {(curve / curve.cummax() - 1).min() * 100:.1f}%")
    print(f"Final equity  : Rs {curve.iloc[-1]:,.0f}")
    print("Exit reasons  :", pd.Series([t.reason for t in trades]).value_counts().to_dict())
    print("Lots used     :", pd.Series([t.lots for t in trades]).value_counts().to_dict())
    print("\nNOTE: 100+ trades ka sample chahiye. Chhote sample pe result bharosemand nahi.")


def to_dataframe(trades):
    return pd.DataFrame([{
        "entry_time": t.entry_t, "exit_time": t.exit_t, "side": "LONG" if t.side == 1 else "SHORT",
        "lots": t.lots, "entry": t.entry, "sl": t.sl, "tp1": t.tp1, "tp2": t.tp2, "score": t.score,
        "pnl_inr": t.pnl, "R": t.pnl / t.risk, "reason": t.reason} for t in trades])


def scan(sig, cfg):
    last, t = sig.iloc[-1], sig.index[-1]
    print(f"\n[{cfg.symbol} FUT] last closed 5m candle: {t}  close={last['close']:.2f}")
    print(f" 1H trend  : {'UP' if last['trend_up'] else 'DOWN' if last['trend_dn'] else 'NEUTRAL'}")
    print(f" Sweep low : {last['sweep_low']}   Sweep high: {last['sweep_high']}")
    print(f" Window ok : {bool(last['session'])}   News/blackout: {bool(last['news'])}")
    print(f" RSI {last['rsi']:.1f} | VWAP {last['vwap']:.2f} | EMA20 {last['ema20']:.2f} | vol_ok {bool(last['vol_ok'])}")
    print(f" Score L/S : {int(last['score_l'])}/{int(last['score_s'])}  (min {cfg.min_score})")
    if last["long"] or last["short"]:
        side = 1 if last["long"] else -1
        anchor = last["sweep_low"] if side == 1 else last["sweep_high"]
        entry = last["close"]
        plan = plan_trade(entry, anchor, last["atr"], side, cfg)
        if plan is None:
            print("\n--- Signal bana par SKIP: cost stop distance ke muqable bahut bada (max_cost_r). ---")
            return
        dist, cost_r = plan
        lots = size_lots(cfg.start_equity, dist, cfg)
        print(f"\n>>> SIGNAL: {'LONG' if side == 1 else 'SHORT'} entry~{entry:.2f} SL {entry - side * dist:.2f} "
              f"TP1 {entry + side * cfg.tp1_r * dist:.2f} TP2 {entry + side * cfg.tp2_r * dist:.2f} "
              f"| lots {lots} | cost {cost_r:.2f}R")
    else:
        print("\n--- Abhi koi setup nahi. Wait karo. ---")


# --------------------------------------------------------------------------- #
# FORWARD-ONLY PAPER TRADING (no real orders)
# --------------------------------------------------------------------------- #
NAME = "NIFTY_FUT_PAPER"
STATE, TRADES, SIGNALS = Path(NAME + "_state.json"), Path(NAME + "_trades.csv"), Path(NAME + "_signals.csv")
CFG = Cfg()


def notify(message):
    import requests
    token = os.getenv("TG_TOKEN") or os.getenv("BOT_TOKEN")
    chat = os.getenv("TG_CHAT_ID") or os.getenv("CHAT_ID")
    if not token or not chat:
        raise RuntimeError("Telegram secrets missing: TG_TOKEN/TG_CHAT_ID (or BOT_TOKEN/CHAT_ID)")
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": message}, timeout=15)
        r.raise_for_status()
        if not r.json().get("ok"):
            raise ValueError("Telegram ok=false")
        print("TELEGRAM DELIVERY: API ACCEPTED", flush=True)
    except Exception as exc:
        raise RuntimeError(f"Telegram delivery failed: {exc}") from exc


def append(path, row):
    first = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(row))
        if first:
            w.writeheader()
        w.writerow(row)


def new_record():
    return {"last_bar": None, "position": None, "closed_count": 0, "wins": 0,
            "net_pnl_inr": 0.0, "net_return_sum_pct": 0.0, "equity": CFG.start_equity}


def load():
    if STATE.exists():
        st = json.loads(STATE.read_text(encoding="utf-8"))
        if not isinstance(st, dict):
            raise RuntimeError("Invalid state")
        return st
    return {CFG.symbol: new_record()}


def save(state):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(STATE)


def _part_exit(pos, sign, qty, price, reason):
    fill = price - sign * CFG.slip_pts
    net = (fill - pos["fill_entry"]) * sign * qty - fill_cost(CFG, fill, qty, is_sell=(sign == 1))
    pos["pnl"] += net
    pos["qty_left"] -= qty
    if reason != "TP1":
        pos["reason"] = reason
        return True
    return False


def settle(record, candle, stamp):
    pos = record["position"]
    if not pos:
        return
    entry_stamp = pd.Timestamp(pos["entry_bar"])
    if stamp <= entry_stamp:
        return                                         # same-bar fill/exit nahi
    sign = 1 if pos["side"] == "LONG" else -1
    o, l, h, c = (float(candle[k]) for k in ("open", "low", "high", "close"))
    closed = False
    if stamp.hour + stamp.minute / 60 >= CFG.squareoff or stamp.normalize() != entry_stamp.normalize():
        closed = _part_exit(pos, sign, pos["qty_left"], o, "EOD")
    else:
        stop = pos["sl"]
        stop_hit = l <= stop if sign == 1 else h >= stop
        tp1_hit = h >= pos["tp1"] if sign == 1 else l <= pos["tp1"]
        tp2_hit = h >= pos["tp2"] if sign == 1 else l <= pos["tp2"]
        was_tp1 = pos["tp1_hit"]
        if stop_hit:
            px = min(o, stop) if sign == 1 else max(o, stop)
            closed = _part_exit(pos, sign, pos["qty_left"], px, "BE/SL" if was_tp1 else "SL")
        else:
            if not was_tp1 and tp1_hit:
                if pos["tp1_qty"] > 0:
                    _part_exit(pos, sign, pos["tp1_qty"], pos["tp1"], "TP1")
                pos["tp1_hit"] = True
                pos["sl"] = pos["entry"]               # breakeven, agli candle se
            elif was_tp1 and tp2_hit:
                closed = _part_exit(pos, sign, pos["qty_left"], pos["tp2"], "TP2")
            pos["bars"] += 1
            if not closed and pos["bars"] >= CFG.max_bars_in_trade:
                closed = _part_exit(pos, sign, pos["qty_left"], c, "TIME")
    if closed:
        net = pos["pnl"]
        record["closed_count"] += 1
        record["wins"] += int(net > 0)
        record["net_pnl_inr"] += net
        record["equity"] += net
        pct = net / pos["equity_at_entry"] * 100
        record["net_return_sum_pct"] += pct
        append(TRADES, {"symbol": CFG.symbol, "side": pos["side"], "lots": pos["lots"],
                        "entry_time": pos["entry_time"], "exit_time": stamp.isoformat(),
                        "entry": pos["entry"], "initial_sl": pos["initial_sl"], "tp1": pos["tp1"],
                        "tp2": pos["tp2"], "result": pos["reason"], "net_pnl_inr": round(net, 2),
                        "return_pct_of_equity": round(pct, 4)})
        notify(f'NIFTY_FUT_COMBO | NIFTY FUT PAPER EXIT | {pos["side"]} x{pos["lots"]} lot\n{pos["reason"]}\n'
               f'Net P/L Rs {net:+,.0f}\nClosed {record["closed_count"]} | Wins {record["wins"]}\nNO REAL ORDER')
        record["position"] = None


def enter(record, signal, stamp):
    side = "LONG" if bool(signal["long"]) else "SHORT"
    sign = 1 if side == "LONG" else -1
    entry, atr_v = float(signal["close"]), float(signal["atr"])
    anchor = float(signal["sweep_low"] if sign == 1 else signal["sweep_high"])
    plan = plan_trade(entry, anchor, atr_v, sign, CFG)
    if plan is None:
        print(f"{side} signal skip (cost vs stop distance zyada)", flush=True)
        return
    dist = plan[0]
    lots = size_lots(record["equity"], dist, CFG) if record["equity"] > 0 else 0
    if lots <= 0:
        print(f"{side} signal skip (1 lot ka risk/margin budget se bahar)", flush=True)
        return
    qty = lots * CFG.lot_size
    fill_e = entry + sign * CFG.slip_pts
    efee = fill_cost(CFG, fill_e, qty, is_sell=(sign == -1))
    sl = entry - sign * dist
    tp1, tp2 = entry + sign * CFG.tp1_r * dist, entry + sign * CFG.tp2_r * dist
    record["position"] = {"side": side, "entry": entry, "fill_entry": fill_e, "sl": sl, "initial_sl": sl,
                          "tp1": tp1, "tp2": tp2, "qty": qty, "qty_left": qty, "lots": lots,
                          "tp1_qty": int(lots * CFG.tp1_frac) * CFG.lot_size,
                          "entry_bar": stamp.isoformat(), "entry_time": stamp.isoformat(),
                          "tp1_hit": False, "pnl": -efee, "bars": 0, "equity_at_entry": record["equity"]}
    append(SIGNALS, {"symbol": CFG.symbol, "side": side, "lots": lots, "entry_time": stamp.isoformat(),
                     "entry_proxy": entry, "sl": sl, "tp1": tp1, "tp2": tp2,
                     "score": int(signal["score_l"] if sign == 1 else signal["score_s"])})
    notify(f"NIFTY_FUT_COMBO | NIFTY FUT PAPER ENTRY | {side} x{lots} lot\nEntry approx {entry:.2f}\nSL {sl:.2f}\n"
           f"TP1 {tp1:.2f} | TP2 {tp2:.2f}\nNO REAL ORDER")


def paper_main():
    state = load()
    record = state.setdefault(CFG.symbol, new_record())
    try:
        df = fetch_angel(CFG, days=60)                  # 1H EMA200 ke liye ~45 din chahiye
        sig = build_signals(df, CFG)
        if sig.empty:
            raise RuntimeError("No completed candles")
        latest = sig.index[-1]
        if record["last_bar"] is None:
            record["last_bar"] = latest.isoformat()
            print(f"initialized at {latest}; no retroactive trades", flush=True)
            return
        new_bars = sig[sig.index > pd.Timestamp(record["last_bar"])]
        if new_bars.empty:
            print("no new closed bars", flush=True)
            return
        for stamp, candle in new_bars.iterrows():
            settle(record, candle, stamp)
            if stamp == latest and record["position"] is None and (bool(candle["long"]) or bool(candle["short"])):
                age = (pd.Timestamp.now(tz=IST) - (stamp + pd.Timedelta(minutes=5))).total_seconds()
                if 0 <= age <= 420:
                    enter(record, candle, stamp)
            record["last_bar"] = stamp.isoformat()
        print(f'open={int(record["position"] is not None)} closed={record["closed_count"]} '
              f'wins={record["wins"]} net=Rs {record["net_pnl_inr"]:,.0f} equity=Rs {record["equity"]:,.0f}', flush=True)
    finally:
        save(state)
    print("NIFTY PAPER CYCLE COMPLETE - no real orders", flush=True)


def paper_loop():
    print("paper-loop start (9:15-15:35 IST, Mon-Fri). Ctrl+C se band.", flush=True)
    while True:
        now = pd.Timestamp.now(tz=IST)
        h = now.hour + now.minute / 60
        if now.weekday() < 5 and 9.25 <= h <= 15.6:
            try:
                paper_main()
            except Exception as exc:
                print("cycle error:", exc, flush=True)
                _SESSION["obj"] = None
        nxt = (now.floor("5min") + pd.Timedelta(minutes=5, seconds=15))
        time.sleep(max(5.0, (nxt - pd.Timestamp.now(tz=IST)).total_seconds()))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def get_df(args, cfg):
    if args.csv:
        return load_csv(args.csv)
    if args.fetch:
        return fetch_angel(cfg, args.days)
    sys.exit("--csv FILE ya --fetch do (ya `demo` chalao)")


def main():
    p = argparse.ArgumentParser(description="Nifty 50 futures combo (Angel One data, paper trading)")
    p.add_argument("mode", choices=["backtest", "scan", "demo", "compare", "paper", "paper-loop", "telegram-test"])
    p.add_argument("--csv")
    p.add_argument("--fetch", action="store_true")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--bnf", help="BankNifty csv (optional bias)")
    p.add_argument("--min-score", type=int)
    p.add_argument("--risk", type=float, help="risk %% per trade")
    p.add_argument("--equity", type=float, help="starting capital INR")
    p.add_argument("--lot-size", type=int, help="lot size override")
    p.add_argument("--slip", type=float, help="slippage points per side")
    p.add_argument("--max-cost-r", type=float)
    p.add_argument("--skip-expiry", action="store_true", help="weekly expiry din trade na karo")
    p.add_argument("--save-trades")
    a = p.parse_args()

    cfg = CFG if a.mode.startswith("paper") else Cfg()
    if a.min_score is not None: cfg.min_score = a.min_score
    if a.risk is not None: cfg.risk_pct = a.risk
    if a.equity is not None: cfg.start_equity = a.equity
    if a.lot_size: cfg.lot_size, cfg.lot_locked = a.lot_size, True
    if a.slip is not None: cfg.slip_pts = a.slip
    if a.max_cost_r is not None: cfg.max_cost_r = a.max_cost_r
    if a.skip_expiry: cfg.skip_expiry_day = True

    if a.mode == "telegram-test":
        return notify("NIFTY_FUT_COMBO | Telegram connection test successful | PAPER ONLY")
    if a.mode == "paper":
        return paper_main()
    if a.mode == "paper-loop":
        return paper_loop()
    if a.mode == "demo":
        df = synthetic()
        print("DEMO: nakli data - sirf code chal raha hai ya nahi check karne ke liye.")
    else:
        df = get_df(a, cfg)
    bnf = load_csv(a.bnf) if a.bnf else None
    if a.mode == "compare":
        return compare(df, cfg, bnf=bnf)
    sig = build_signals(df, cfg, bnf=bnf)
    if a.mode == "scan":
        return scan(sig, cfg)
    trades, curve = backtest(sig, cfg)
    report(trades, curve, cfg)
    if a.save_trades and trades:
        to_dataframe(trades).to_csv(a.save_trades, index=False)
        print("Trades saved ->", a.save_trades)


if __name__ == "__main__":
    main()