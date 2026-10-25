#!/usr/bin/env python3
"""
GOLD COMBO STRATEGY  -  XAU/USDT + PAXG/USDT  (CoinDCX Futures paper-trading)
==============================================================================
gold_logic.xlsx ke saare logic ek "confluence" system me:

  GATES (sab zaroori - inke bina trade nahi)
    1. 1H Trend      : EMA50 vs EMA200 (+ price EMA200 ke sahi taraf)
    2. Liquidity Sweep: 15m swing low/high  YA  Previous-Day / Asian-session low/high
                        ka sweep + reclaim (SL-hunt reversal)
    3. 5m Trigger    : close VWAP + EMA20 ke sahi taraf, RSI 50 ke sahi taraf, Volume > avg
    4. Session       : XAU -> London/NY zaroori ; PAXG -> sirf bonus (24x7 asset)
    5. News blackout : CPI/NFP/FOMC ke aas-paas trade nahi

  BONUS SCORE (min_score tak pahunchna zaroori)
    +1 Session (London/NY)          +1 Bollinger squeeze breakout
    +1 Price POC ke sahi taraf       +1 DXY weak(long)/strong(short)  [optional data]
    +1 PAXG: BTC 1H weak (long)      +1 PAXG: premium vs XAU high     [optional data]

  RISK: SL = sweep level +- 0.2*ATR (max 2.5 ATR), TP1 = 2R (50% close, SL->breakeven),
        TP2 = 3R, risk 1% equity/trade, fees + slippage included.

USAGE
  python gold_combo_strategy.py backtest --symbol PAXG --csv paxg_5m.csv
  python gold_combo_strategy.py backtest --symbol XAU  --fetch --days 30
  python gold_combo_strategy.py scan --symbol XAU --fetch
  python gold_combo_strategy.py demo                # synthetic data pe sirf code-check

CSV columns: time(UTC, datetime/epoch-ms), open, high, low, close, volume  (5-minute candles)
Optional extra data (--dxy dxy.csv --btc btc.csv --xau xau.csv): same CSV format (any timeframe).

DISCLAIMER: Ye research/paper-test tool hai, financial advice nahi. Win-rate guaranteed nahi.
Live paise lagane se pehle backtest + kam se kam 2-4 hafte paper trading karo.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# CONFIG
# --------------------------------------------------------------------------- #

# CoinDCX futures pair codes - apne account/API docs se verify kar lena.
PAIRS = {"XAU": "B-XAU_USDT", "PAXG": "B-PAXG_USDT"}

# News blackout (UTC). Har mahine CPI / NFP / FOMC ki date yaha add karo.
# Format: ("YYYY-MM-DD HH:MM", minutes_before, minutes_after)
NEWS_EVENTS: list[tuple[str, int, int]] = [
    # ("2026-11-06 13:30", 60, 90),   # NFP example
    # ("2026-11-12 13:30", 60, 90),   # CPI example
    # ("2026-12-09 19:00", 60, 120),  # FOMC example
]


@dataclass
class Cfg:
    symbol: str = "XAU"
    # trend (1H)
    ema_fast: int = 50
    ema_slow: int = 200
    # 5m trigger
    ema_trig: int = 20
    rsi_len: int = 14
    vol_len: int = 20
    vol_mult: float = 1.0
    # sweep
    swing_lb_15m: int = 20          # 15m bars (=5h) ka swing
    sweep_valid_bars_5m: int = 24   # sweep ke baad 2h tak entry allowed
    # session (UTC)  London 13:30 IST=08:00 UTC ; NY 18:00 IST=12:30 UTC
    london: tuple = (8.0, 12.5)
    newyork: tuple = (12.5, 17.5)
    asian: tuple = (0.0, 8.0)
    session_required: bool = True   # XAU me True, PAXG me False (auto set)
    # scoring
    min_score: int = 1              # XAU: session gate me hai, to score me alag se nahi gina jata
    # v2 fixes (False/0 karne par purane rules wapas)
    one_per_sweep: bool = True      # ek sweep par sirf pehla signal (SL ke turant baad re-entry nahi)
    invalidate_sweep: bool = True   # sweep level ke paar close ho gaya -> setup fail, signal band
    max_cost_r: float = 0.30        # round-trip fee+slip > 0.30R ho to trade skip (0 = off)
    score_excl_gate_session: bool = True
    # bollinger squeeze
    bb_len: int = 20
    bb_sq_lookback: int = 100
    # volume profile
    poc_window: int = 288           # 1 din (5m)
    poc_bins: int = 40
    # risk
    atr_len: int = 14
    sl_buffer_atr: float = 0.2
    max_sl_atr: float = 2.5
    min_sl_atr: float = 0.5
    tp1_r: float = 2.0
    tp2_r: float = 3.0
    tp1_frac: float = 0.5
    risk_pct: float = 1.0
    max_leverage: float = 10.0
    start_equity: float = 1000.0
    fee_pct: float = 0.05           # per side (taker) %
    slip_pct: float = 0.01          # per side %
    max_bars_in_trade: int = 288    # time stop = 1 din

    def __post_init__(self):
        if self.symbol.upper().startswith("PAXG"):
            self.session_required = False   # 24x7 token
            self.vol_mult = 1.2             # kam liquidity -> zyada strict volume
            self.min_score = 2


# --------------------------------------------------------------------------- #
# DATA
# --------------------------------------------------------------------------- #

def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.lower() for c in df.columns]
    tcol = "time" if "time" in df.columns else df.columns[0]
    t = df[tcol]
    if np.issubdtype(t.dtype, np.number):
        unit = "ms" if t.iloc[0] > 1e11 else "s"
        idx = pd.to_datetime(t, unit=unit, utc=True)
    else:
        idx = pd.to_datetime(t, utc=True)
    df.index = idx
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    return df[~df.index.duplicated()].sort_index()


def fetch_coindcx(symbol: str, days: int = 30, interval: str = "5m") -> pd.DataFrame:
    """Fetch CoinDCX futures candles using the documented candlesticks API."""
    import requests
    import time

    symbol = symbol.upper().replace("USDT", "").strip()
    if symbol not in PAIRS:
        raise ValueError(f"Unsupported symbol: {symbol}")
    pair = PAIRS[symbol]
    resolution = {"5m": "5", "15m": "15", "1h": "60"}.get(interval)
    if resolution is None:
        raise ValueError(f"Unsupported interval: {interval}")

    url = "https://public.coindcx.com/market_data/candlesticks"
    end = int(time.time())
    start = end - int(days * 86400)
    rows = []

    # One-day batches keep each 5-minute request within typical candle limits.
    with requests.Session() as session:
        for batch_start in range(start, end, 86400):
            batch_end = min(batch_start + 86400, end)
            params = {
                "pair": pair,
                "pcode": "f",
                "resolution": resolution,
                "from": batch_start,
                "to": batch_end,
            }
            for attempt in range(3):
                try:
                    response = session.get(url, params=params, timeout=30)
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                        raise ValueError(f"Unexpected CoinDCX candle response for {pair}: {str(payload)[:200]}")
                    rows.extend(payload["data"])
                    break
                except (requests.RequestException, ValueError) as exc:
                    if attempt == 2:
                        raise RuntimeError(f"CoinDCX candle request failed for {pair}: {exc}") from exc
                    time.sleep(2 * (attempt + 1))

    if not rows:
        raise RuntimeError(
            f"No candles returned for {pair}. Verify this exact futures pair "
            "in CoinDCX active instruments."
        )

    df = pd.DataFrame(rows)
    required = ["time", "open", "high", "low", "close", "volume"]
    if any(col not in df.columns for col in required):
        raise RuntimeError(f"Incomplete candle fields for {pair}: {list(df.columns)}")

    numeric_time = pd.to_numeric(df["time"], errors="coerce")
    unit = "ms" if numeric_time.dropna().median() > 1e11 else "s"
    df["time"] = pd.to_datetime(numeric_time, unit=unit, utc=True, errors="coerce")
    df = df.dropna(subset=["time"]).set_index("time")
    df = df[["open", "high", "low", "close", "volume"]].apply(pd.to_numeric, errors="coerce")
    df = df.dropna().sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df = df[(df["high"] >= df["low"]) & (df["close"] > 0) & (df["volume"] >= 0)]
    if df.empty:
        raise RuntimeError(f"No valid candles for {pair}")
    # Drop the current incomplete 5-minute bar before indicator calculations.
    minutes = int(resolution)
    now = pd.Timestamp.now(tz="UTC")
    df = df[df.index + pd.Timedelta(minutes=minutes) <= now]
    if len(df) < 100:
        raise RuntimeError(f"Insufficient completed candles for {pair}: {len(df)}")
    print(f"Fetched {len(df)} completed {interval} candles for {pair}", flush=True)
    return df


def synthetic(n: int = 288 * 60, seed: int = 7) -> pd.DataFrame:
    """Sirf code test karne ke liye nakli data. Is par result ka koi matlab nahi."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-08-01", periods=n, freq="5min", tz="UTC")
    hour = idx.hour + idx.minute / 60
    vol_scale = np.where((hour >= 8) & (hour < 17.5), 1.6, 0.7)
    drift = 0.00002 * np.sin(np.arange(n) / 2000)
    ret = rng.normal(drift, 0.0006 * vol_scale)
    close = 4000 * np.exp(np.cumsum(ret))
    open_ = np.r_[close[0], close[:-1]]
    rng_ = np.abs(rng.normal(0, 0.0004, n)) * close
    high = np.maximum(open_, close) + rng_
    low = np.minimum(open_, close) - rng_
    volume = rng.gamma(2.0, 50, n) * vol_scale
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


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
    """Daily (UTC) reset VWAP."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    day = df.index.floor("D")
    pv = (tp * df["volume"]).groupby(day).cumsum()
    v = df["volume"].groupby(day).cumsum().replace(0, np.nan)
    return pv / v


def rolling_poc(df, window, bins, step=12):
    """Rolling Volume-Profile Point of Control (har `step` bar par update)."""
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
    """Resample to higher TF with correct bin edges (e.g. 1H = 09:00-09:55 candles).
    Index = open-time of the LAST base candle inside the bar, i.e. the row whose close
    coincides with the HTF close -> available exactly at that moment, no look-ahead.
    (Purana code label=right/closed=right se bins ek candle shift kar deta tha.)"""
    base = pd.Series(df.index).diff().median()
    o = df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    o.index = o.index + pd.Timedelta(rule) - base
    return o


def to_5m(series_htf: pd.Series, idx5: pd.DatetimeIndex, limit=None):
    """HTF series (indexed at bar close) -> 5m index, forward-fill only."""
    return series_htf.reindex(idx5.union(series_htf.index)).ffill(limit=limit).reindex(idx5)


# --------------------------------------------------------------------------- #
# SIGNAL ENGINE
# --------------------------------------------------------------------------- #

def in_window(hours: pd.Series, win) -> pd.Series:
    return (hours >= win[0]) & (hours < win[1])


def bias_from_external(ext: pd.DataFrame | None, idx5, ema_len=50, rule="1h"):
    """+1 = asset weak (gold-bullish for DXY/BTC), -1 = asset strong, 0 = unknown."""
    if ext is None or ext.empty:
        return pd.Series(0, index=idx5)
    h = htf(ext, rule)["close"]
    sig = np.sign(ema(h, ema_len) - h)          # price < EMA  -> +1 (weak)
    return to_5m(sig, idx5).fillna(0)


def build_signals(df: pd.DataFrame, cfg: Cfg, dxy=None, btc=None, xau=None) -> pd.DataFrame:
    d = df.copy()
    idx = d.index
    hrs = idx.hour + idx.minute / 60

    # ---- 5m indicators
    d["ema20"] = ema(d["close"], cfg.ema_trig)
    d["rsi"] = rsi(d["close"], cfg.rsi_len)
    d["atr"] = atr(d, cfg.atr_len)
    d["vwap"] = session_vwap(d)
    d["vol_ok"] = d["volume"] > cfg.vol_mult * d["volume"].rolling(cfg.vol_len).mean()
    d["poc"] = rolling_poc(d, cfg.poc_window, cfg.poc_bins)

    # ---- Bollinger squeeze + breakout (5m)
    ma = d["close"].rolling(cfg.bb_len).mean()
    sd = d["close"].rolling(cfg.bb_len).std()
    bw = (4 * sd) / ma
    squeeze = bw <= bw.rolling(cfg.bb_sq_lookback).quantile(0.2)
    recent_sq = squeeze.rolling(12).max().fillna(0).astype(bool)
    d["bb_up"], d["bb_dn"] = ma + 2 * sd, ma - 2 * sd

    # ---- 1H trend
    h1 = htf(d[["open", "high", "low", "close", "volume"]], "1h")
    e_f, e_s = ema(h1["close"], cfg.ema_fast), ema(h1["close"], cfg.ema_slow)
    up = ((e_f > e_s) & (h1["close"] > e_s)).astype(float)
    dn = ((e_f < e_s) & (h1["close"] < e_s)).astype(float)
    d["trend_up"] = to_5m(up, idx).fillna(0).astype(bool)
    d["trend_dn"] = to_5m(dn, idx).fillna(0).astype(bool)

    # ---- 15m liquidity sweep (swing low/high)
    m15 = htf(d[["open", "high", "low", "close", "volume"]], "15min")
    sw_lo = m15["low"].rolling(cfg.swing_lb_15m).min().shift(1)
    sw_hi = m15["high"].rolling(cfg.swing_lb_15m).max().shift(1)
    sweep_l = (m15["low"] < sw_lo) & (m15["close"] > sw_lo)       # wick neeche, close wapas upar
    sweep_s = (m15["high"] > sw_hi) & (m15["close"] < sw_hi)
    lvl_l = m15["low"].where(sweep_l)                              # SL anchor
    lvl_s = m15["high"].where(sweep_s)
    L = cfg.sweep_valid_bars_5m
    sweep_low_15 = to_5m(lvl_l, idx, limit=L)
    sweep_high_15 = to_5m(lvl_s, idx, limit=L)

    # ---- Previous-Day / Asian-session level sweep (5m)
    day = idx.floor("D")
    daily = d.groupby(day).agg(h=("high", "max"), l=("low", "min"))
    pdh = pd.Series(day, index=idx).map(daily["h"].shift(1))
    pdl = pd.Series(day, index=idx).map(daily["l"].shift(1))
    asian = in_window(pd.Series(hrs, index=idx), cfg.asian)
    a_hi = d["high"].where(asian).groupby(day).cummax().groupby(day).ffill()
    a_lo = d["low"].where(asian).groupby(day).cummin().groupby(day).ffill()
    after_asian = ~asian
    lvl_lo = pd.concat([pdl, a_lo.where(after_asian)], axis=1).min(axis=1)
    lvl_hi = pd.concat([pdh, a_hi.where(after_asian)], axis=1).max(axis=1)
    ev_l = (d["low"] < lvl_lo) & (d["close"] > lvl_lo)
    ev_s = (d["high"] > lvl_hi) & (d["close"] < lvl_hi)
    lvl5_l = d["low"].where(ev_l)
    lvl5_s = d["high"].where(ev_s)
    sweep_low_5 = lvl5_l.ffill(limit=L)
    sweep_high_5 = lvl5_s.ffill(limit=L)

    # combine: jo sweep level ZYADA aggressive (SL ke liye wide) ho wo lo
    sw_low = pd.concat([sweep_low_15, sweep_low_5], axis=1).min(axis=1)
    sw_high = pd.concat([sweep_high_15, sweep_high_5], axis=1).max(axis=1)
    sid_l = (sw_low != sw_low.shift()).cumsum()       # har naya sweep = naya id
    sid_s = (sw_high != sw_high.shift()).cumsum()
    if cfg.invalidate_sweep:
        # sweep extreme ke neeche (long) / upar (short) close ho gaya -> sweep fail, id ke baaki bars band
        failed_l = (d["close"] < sw_low).astype(int).groupby(sid_l).cummax().astype(bool)
        failed_s = (d["close"] > sw_high).astype(int).groupby(sid_s).cummax().astype(bool)
        sw_low, sw_high = sw_low.where(~failed_l), sw_high.where(~failed_s)
    d["sweep_low"], d["sweep_high"] = sw_low, sw_high

    # ---- Session
    h_s = pd.Series(hrs, index=idx)
    d["session"] = in_window(h_s, cfg.london) | in_window(h_s, cfg.newyork)

    # ---- News blackout
    blk = pd.Series(False, index=idx)
    for ts, bef, aft in NEWS_EVENTS:
        t = pd.Timestamp(ts, tz="UTC")
        blk |= (idx >= t - pd.Timedelta(minutes=bef)) & (idx <= t + pd.Timedelta(minutes=aft))
    d["news"] = blk

    # ---- optional external bias
    d["dxy_bias"] = bias_from_external(dxy, idx)          # +1 = DXY weak (gold long friendly)
    d["btc_bias"] = bias_from_external(btc, idx)          # +1 = BTC weak (PAXG long friendly)
    if xau is not None and cfg.symbol.upper().startswith("PAXG"):
        x = xau["close"].reindex(idx, method="ffill")
        prem = d["close"] - x
        z = (prem - prem.rolling(288).mean()) / prem.rolling(288).std()
        d["prem_z"] = z.fillna(0)
    else:
        d["prem_z"] = 0.0
    is_paxg = cfg.symbol.upper().startswith("PAXG")

    # ---- GATES
    sess_ok = d["session"] if cfg.session_required else pd.Series(True, index=idx)
    base_ok = sess_ok & ~d["news"]

    trig_l = (d["close"] > d["vwap"]) & (d["close"] > d["ema20"]) & (d["rsi"] > 50) & d["vol_ok"]
    trig_s = (d["close"] < d["vwap"]) & (d["close"] < d["ema20"]) & (d["rsi"] < 50) & d["vol_ok"]
    gate_l = base_ok & d["trend_up"] & d["sweep_low"].notna() & trig_l
    gate_s = base_ok & d["trend_dn"] & d["sweep_high"].notna() & trig_s

    # ---- SCORE
    sess_pt = d["session"].astype(int)
    if cfg.score_excl_gate_session and cfg.session_required:
        sess_pt = sess_pt * 0          # session pehle se gate hai; score me dobara gin'na score ko bekaar bana deta tha
    sc_l = sess_pt + ((d["close"] > d["bb_up"].shift()) & recent_sq).astype(int) \
        + (d["close"] > d["poc"]).astype(int) + (d["dxy_bias"] > 0).astype(int)
    sc_s = sess_pt + ((d["close"] < d["bb_dn"].shift()) & recent_sq).astype(int) \
        + (d["close"] < d["poc"]).astype(int) + (d["dxy_bias"] < 0).astype(int)
    if is_paxg:
        sc_l += (d["btc_bias"] > 0).astype(int) + (d["prem_z"] > 1).astype(int)
        sc_s += (d["btc_bias"] < 0).astype(int) + (d["prem_z"] < -1).astype(int)
    d["score_l"], d["score_s"] = sc_l, sc_s

    d["long"] = gate_l & (sc_l >= cfg.min_score)
    d["short"] = gate_s & (sc_s >= cfg.min_score)
    if cfg.one_per_sweep:   # ek sweep par sirf pehla valid bar; SL hit ke turant baad wahi setup dobara nahi
        d["long"] = d["long"] & d["long"].astype(int).groupby(sid_l).cumsum().eq(1)
        d["short"] = d["short"] & d["short"].astype(int).groupby(sid_s).cumsum().eq(1)
    return d


# --------------------------------------------------------------------------- #
# BACKTEST
# --------------------------------------------------------------------------- #

@dataclass
class Trade:
    side: int
    entry_t: pd.Timestamp
    entry: float
    sl: float
    tp1: float
    tp2: float
    qty: float
    risk: float
    score: int
    exit_t: pd.Timestamp | None = None
    pnl: float = 0.0
    reason: str = ""
    tp1_hit: bool = False
    qty_left: float = field(default=0.0)
    fees: float = 0.0
    tp1_i: int = -1


def backtest(sig: pd.DataFrame, cfg: Cfg):
    eq = cfg.start_equity
    trades: list[Trade] = []
    curve = []
    pos: Trade | None = None
    cost = (cfg.fee_pct + cfg.slip_pct) / 100

    o, h, l, c = (sig[k].to_numpy() for k in ("open", "high", "low", "close"))
    a = sig["atr"].to_numpy()
    lg, sh = sig["long"].to_numpy(), sig["short"].to_numpy()
    sl_low, sl_high = sig["sweep_low"].to_numpy(), sig["sweep_high"].to_numpy()
    scl, scs = sig["score_l"].to_numpy(), sig["score_s"].to_numpy()
    idx = sig.index
    bars_in = 0

    def close_part(t: Trade, qty, price, ts):
        nonlocal eq
        gross = (price - t.entry) * t.side * qty
        fees = (t.entry + price) * qty * cost
        net = gross - fees
        eq += net
        t.pnl += net
        t.fees += fees

    for i in range(1, len(sig)):
        # ---- manage open position on bar i
        if pos is not None:
            bars_in += 1
            s = pos.side
            sl_hit = (l[i] <= pos.sl) if s == 1 else (h[i] >= pos.sl)
            tp1_hit = (h[i] >= pos.tp1) if s == 1 else (l[i] <= pos.tp1)
            tp2_hit = (h[i] >= pos.tp2) if s == 1 else (l[i] <= pos.tp2)
            done = False
            if sl_hit:                                   # conservative: SL pehle
                gap_px = min(o[i], pos.sl) if s == 1 else max(o[i], pos.sl)   # gap par open par fill
                close_part(pos, pos.qty_left, gap_px, idx[i])
                pos.reason = "BE/SL" if pos.tp1_hit else "SL"
                done = True
            else:
                if not pos.tp1_hit and tp1_hit:
                    q = pos.qty * cfg.tp1_frac
                    close_part(pos, q, pos.tp1, idx[i])
                    pos.qty_left -= q
                    pos.tp1_hit = True
                    pos.tp1_i = i
                    pos.sl = pos.entry                    # breakeven
                if pos.tp1_hit and pos.tp1_i < i and tp2_hit:   # TP2 sirf TP1 ke agle bar se (same-bar dono nahi)
                    close_part(pos, pos.qty_left, pos.tp2, idx[i])
                    pos.reason, done = "TP2", True
                elif bars_in >= cfg.max_bars_in_trade:
                    close_part(pos, pos.qty_left, c[i], idx[i])
                    pos.reason, done = "TIME", True
            if done:
                pos.exit_t = idx[i]
                trades.append(pos)
                pos, bars_in = None, 0

        # ---- new entry: signal bar i-1 close -> entry at open[i]
        if pos is None and not np.isnan(a[i - 1]) and (lg[i - 1] or sh[i - 1]):
            side = 1 if lg[i - 1] else -1
            entry = o[i]                       # slippage cost (fee+slip) me pehle se hai; double count nahi
            anchor = sl_low[i - 1] if side == 1 else sl_high[i - 1]
            plan = plan_trade(entry, anchor, a[i - 1], side, cfg)
            qty = 0.0
            if plan is not None:
                dist = plan[0]
                sl = entry - side * dist
                qty = (eq * cfg.risk_pct / 100) / dist
                qty = min(qty, eq * cfg.max_leverage / entry)
            if qty > 0 and eq > 0:
                pos = Trade(side, idx[i], entry, sl, entry + side * cfg.tp1_r * dist,
                            entry + side * cfg.tp2_r * dist, qty, eq * cfg.risk_pct / 100,
                            int(scl[i - 1] if side == 1 else scs[i - 1]), qty_left=qty)
                bars_in = 0
        curve.append(eq)

    return trades, pd.Series(curve, index=idx[1:])


def plan_trade(entry: float, anchor: float, atr_v: float, side: int, cfg: Cfg):
    """SL distance + cost-in-R. None = trade skip (fee/slip risk ke muqable bahut bada).
    round-trip cost (R me) = 2 * entry * cost / SL_distance."""
    if not (entry > 0 and atr_v > 0) or np.isnan(anchor):
        return None
    sl = anchor - side * cfg.sl_buffer_atr * atr_v
    dist = min(max(abs(entry - sl), cfg.min_sl_atr * atr_v), cfg.max_sl_atr * atr_v)
    cost_r = 2 * entry * (cfg.fee_pct + cfg.slip_pct) / 100 / dist
    if cfg.max_cost_r and cost_r > cfg.max_cost_r:
        return None
    return dist, cost_r


def stats_row(trades: list[Trade], curve: pd.Series, cfg: Cfg) -> dict:
    if not trades:
        return dict(trades=0, win_pct=np.nan, pf=np.nan, netR=np.nan, grossR=np.nan, net_pct=0.0, maxDD_pct=0.0)
    pnl = np.array([t.pnl for t in trades])
    risk = np.array([t.risk for t in trades])
    fees = np.array([t.fees for t in trades])
    gp, gl = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    return dict(trades=len(trades), win_pct=(pnl > 0).mean() * 100, pf=(gp / gl if gl > 0 else np.inf),
                netR=(pnl / risk).mean(), grossR=((pnl + fees) / risk).mean(),
                net_pct=pnl.sum() / cfg.start_equity * 100, maxDD_pct=(curve / curve.cummax() - 1).min() * 100)


def compare(df, cfg, dxy=None, btc=None, xau=None, oos_frac=0.4):
    """Same data par purane rules vs v2 variants; ALL + last-40% (OOS) alag. grossR = fees se pehle."""
    import dataclasses
    variants = {
        "old-rules": dict(one_per_sweep=False, invalidate_sweep=False, max_cost_r=0.0,
                          score_excl_gate_session=False, min_score=2),
        "v2 no-cost-filter": dict(max_cost_r=0.0),
        "v2 default": dict(),
        "v2 wide-SL(min1ATR)": dict(min_sl_atr=1.0),
        "v2 TP1=1.5R": dict(tp1_r=1.5),
    }
    split = int(len(df) * (1 - oos_frac))
    rows = []
    for name, kw in variants.items():
        c = dataclasses.replace(cfg, **kw)
        sig = build_signals(df, c, dxy=dxy, btc=btc, xau=xau)
        for part, s in (("ALL", sig), ("OOS", sig.iloc[split:])):
            tr, cv = backtest(s, c)
            rows.append({"variant": name, "part": part, **stats_row(tr, cv, c)})
    print(f"\n===== {cfg.symbol} COMPARE  (OOS = last {int(oos_frac * 100)}% of data) =====")
    print(pd.DataFrame(rows).round(2).to_string(index=False))
    print("\ngrossR > 0 lekin netR < 0  => edge hai par fees kha rahi hain (stop bahut chhota / entry-exit taker).")
    print("grossR <= 0                 => signal logic me hi edge nahi.  100+ trades se kam par bharosa mat karo.")


def report(trades: list[Trade], curve: pd.Series, cfg: Cfg):
    print(f"\n===== {cfg.symbol} BACKTEST =====")
    if not trades:
        print("Koi trade nahi bana. Filters bahut strict hain ya data kam hai.")
        return
    pnl = np.array([t.pnl for t in trades])
    r = np.array([t.pnl / t.risk for t in trades])
    wins = pnl > 0
    dd = (curve / curve.cummax() - 1).min() * 100
    gp, gl = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    print(f"Trades        : {len(trades)}  (Long {sum(t.side == 1 for t in trades)} / Short {sum(t.side == -1 for t in trades)})")
    print(f"Win rate      : {wins.mean() * 100:.1f}%")
    print(f"Profit factor : {gp / gl:.2f}" if gl > 0 else "Profit factor : inf")
    rg = np.array([(t.pnl + t.fees) / t.risk for t in trades])
    print(f"Avg R / trade : {r.mean():.2f}R net   |   {rg.mean():.2f}R gross (fees se pehle)   |   fee+slip {rg.mean() - r.mean():.2f}R/trade")
    print(f"Net PnL       : {pnl.sum():.2f} USDT  ({pnl.sum() / cfg.start_equity * 100:.1f}%)")
    print(f"Max drawdown  : {dd:.1f}%")
    print(f"Final equity  : {curve.iloc[-1]:.2f}")
    print("Exit reasons  :", pd.Series([t.reason for t in trades]).value_counts().to_dict())
    print("Score vs avgR :", pd.Series(r).groupby([t.score for t in trades]).mean().round(2).to_dict())
    print("\nNOTE: Pehle 100+ trades ka sample chahiye. Chhote sample pe result bharosemand nahi.")


def to_dataframe(trades):
    return pd.DataFrame([{
        "entry_time": t.entry_t, "exit_time": t.exit_t, "side": "LONG" if t.side == 1 else "SHORT",
        "entry": t.entry, "sl": t.sl, "tp1": t.tp1, "tp2": t.tp2, "score": t.score,
        "pnl": t.pnl, "R": t.pnl / t.risk, "reason": t.reason} for t in trades])


# --------------------------------------------------------------------------- #
# LIVE / PAPER SCANNER
# --------------------------------------------------------------------------- #

def scan(sig: pd.DataFrame, cfg: Cfg):
    last = sig.iloc[-1]                       # last CLOSED candle (fetch excludes incomplete)
    t = sig.index[-1]
    print(f"\n[{cfg.symbol}] last closed 5m candle: {t}  close={last['close']:.2f}")
    print(f" 1H trend  : {'UP' if last['trend_up'] else 'DOWN' if last['trend_dn'] else 'NEUTRAL'}")
    print(f" Sweep low : {last['sweep_low']}   Sweep high: {last['sweep_high']}")
    print(f" Session   : {bool(last['session'])}   News blackout: {bool(last['news'])}")
    print(f" RSI {last['rsi']:.1f} | VWAP {last['vwap']:.2f} | EMA20 {last['ema20']:.2f} | vol_ok {bool(last['vol_ok'])}")
    print(f" Score L/S : {int(last['score_l'])}/{int(last['score_s'])}  (min {cfg.min_score})")
    if last["long"] or last["short"]:
        side = 1 if last["long"] else -1
        anchor = last["sweep_low"] if side == 1 else last["sweep_high"]
        entry = last["close"]
        plan = plan_trade(entry, anchor, last["atr"], side, cfg)
        if plan is None:
            print("\n--- Signal bana par SKIP: fee+slip stop distance ke muqable bahut bada (max_cost_r). ---")
            return
        dist, cost_r = plan
        sl = entry - side * dist
        print(f"\n>>> SIGNAL: {'LONG' if side == 1 else 'SHORT'}  entry~{entry:.2f}  SL {sl:.2f}  "
              f"TP1 {entry + side * cfg.tp1_r * dist:.2f}  TP2 {entry + side * cfg.tp2_r * dist:.2f}  (cost {cost_r:.2f}R)")
    else:
        print("\n--- Abhi koi setup nahi. Wait karo. ---")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def get_df(args):
    if args.csv:
        return load_csv(args.csv)
    if args.fetch:
        return fetch_coindcx(args.symbol, args.days)
    sys.exit("--csv FILE ya --fetch do (ya `demo` command chalao)")


def main():
    p = argparse.ArgumentParser(description="Gold combo strategy (XAU/PAXG)")
    p.add_argument("mode", choices=["backtest", "scan", "demo", "compare"])
    p.add_argument("--symbol", default="XAU", choices=["XAU", "PAXG"])
    p.add_argument("--csv")
    p.add_argument("--fetch", action="store_true")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--dxy"), p.add_argument("--btc"), p.add_argument("--xau")
    p.add_argument("--min-score", type=int)
    p.add_argument("--risk", type=float)
    p.add_argument("--fee", type=float, help="fee %% per side (default 0.05 taker)")
    p.add_argument("--slip", type=float, help="slippage %% per side (default 0.01)")
    p.add_argument("--max-cost-r", type=float, help="round-trip cost > X R to trade skip (0 = off)")
    p.add_argument("--save-trades")
    a = p.parse_args()

    cfg = Cfg(symbol=a.symbol)
    if a.min_score is not None:
        cfg.min_score = a.min_score
    if a.risk is not None:
        cfg.risk_pct = a.risk
    if a.fee is not None:
        cfg.fee_pct = a.fee
    if a.slip is not None:
        cfg.slip_pct = a.slip
    if a.max_cost_r is not None:
        cfg.max_cost_r = a.max_cost_r

    if a.mode == "demo":
        df = synthetic()
        print("DEMO: nakli data - sirf ye check karne ke liye ki code chal raha hai.")
    else:
        df = get_df(a)

    ext = lambda f: load_csv(f) if f else None
    if a.mode == "compare":
        compare(df, cfg, dxy=ext(a.dxy), btc=ext(a.btc), xau=ext(a.xau))
        return
    sig = build_signals(df, cfg, dxy=ext(a.dxy), btc=ext(a.btc), xau=ext(a.xau))

    if a.mode == "scan":
        scan(sig, cfg)
        return
    trades, curve = backtest(sig, cfg)
    report(trades, curve, cfg)
    if a.save_trades and trades:
        to_dataframe(trades).to_csv(a.save_trades, index=False)
        print("Trades saved ->", a.save_trades)


if __name__ == "__main__":
    main()
