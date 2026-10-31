#!/usr/bin/env python3
"""
GOLD_BOOK.py  --  XAUUSDT + PAXGUSDT ek hi file me, har symbol ki alag strategy.

Ye dono ko ek "gold book" ki tarah chalata hai:
  * ek time par sirf 1 position (dono symbol milake)  -> double risk nahi
  * ek din me 2 SL lagte hi us din trading band
  * entry sirf allowed UTC window me (default 16:00-20:00 UTC = 21:30-01:30 IST)
  * BLACKOUT: 11:30-15:30 UTC (17:00-21:00 IST) me koi entry nahi (US data/open whipsaw)
  * exit: 2R -> SL +1R, 3R -> SL +2R, 4R -> full exit; 24h TIME exit

Per-symbol strategy (neeche CFG_XAU / CFG_PAXG):
  XAUUSDT : sirf SHORT (SHORT-ONLY mode; --allow-long se LONG wapas, LONG sirf daily trend upar ho)
  PAXGUSDT: sirf SHORT (data me long 17% hi jeete the)

NOTE: asli bot ka entry-signal code mere paas nahi tha. Yahan ka signal
(breakdown/breakout + RSI + 1h EMA50 trend) meri likhi hui baseline hai.
Results purani CSV se match nahi karenge. Pehle backtest + paper trade karo.

Chalane ke tareeke:
  python GOLD_BOOK.py                     # 365 din backtest, CSV save
  python GOLD_BOOK.py --days 180          # chhota backtest
  python GOLD_BOOK.py --scan              # abhi ka latest signal dekho
  python GOLD_BOOK.py --synthetic         # bina internet ke test (nakli data)
  python GOLD_BOOK.py --skip-dates 2026-11-05,2026-11-13   # NFP/CPI/FOMC din
Requirements: pip install pandas numpy requests
"""
import argparse
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

BINANCE_FUTURES = "https://fapi.binance.com"
TP1_R, TP2_R, TP3_R = 2.0, 3.0, 4.0           # TP1 = 2R, TP2 = 3R (CSV ke tp1/tp2 jaisa)
HOLD_HOURS = 24                   # TIME exit
MAX_SL_PER_DAY = 2                # 2 SL ke baad din band
BLACKOUTS = [(11 * 60 + 30, 15 * 60 + 30)]   # UTC minutes [start, end): is time par ENTRY nahi
COST_PCT = 0.0008                 # round-trip fee+slippage (0.08%) -> R me convert hota hai


# --------------------------------------------------------------------------
# Strategy config  (yahin se har symbol ki strategy alag hoti hai)
# --------------------------------------------------------------------------
@dataclass
class Cfg:
    symbol: str
    allow_long: bool
    allow_short: bool
    windows: list = field(default_factory=lambda: [(16, 20)])  # UTC hours [start, end)
    lookback: int = 12            # breakout/breakdown 5m bars
    rsi_short: float = 50.0       # short ke liye RSI < ye
    rsi_long: float = 50.0        # long ke liye RSI > ye
    atr_mult: float = 3.0         # SL = ATR(14, 5m) * mult
    min_sl_pct: float = 0.0035    # SL clamp (entry ka %)
    max_sl_pct: float = 0.0060
    long_needs_daily_trend: bool = True
    priority: int = 1             # chhota number = pehle check hota hai


CFG_XAU = Cfg("XAUUSDT", allow_long=False, allow_short=True, lookback=12,
              rsi_short=50, rsi_long=52, priority=1)
CFG_PAXG = Cfg("PAXGUSDT", allow_long=False, allow_short=True, lookback=18,
               rsi_short=45, rsi_long=55, priority=2)
CONFIGS = {c.symbol: c for c in (CFG_XAU, CFG_PAXG)}


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
def fetch_klines(symbol, days, interval="5m"):
    """Fetch closed 5m futures candles from CoinDCX; never substitute fake data."""
    import requests
    if interval != "5m":
        raise ValueError("GOLD_BOOK supports 5m only")
    api = "https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments"
    resp = requests.get(api, params={"margin_currency_short_name[]": "USDT"}, timeout=25)
    resp.raise_for_status()
    instruments = resp.json()
    if isinstance(instruments, dict):
        instruments = instruments.get("data", instruments.get("instruments", []))
    if not isinstance(instruments, list):
        raise RuntimeError("CoinDCX active instruments: unexpected response")
    suffix = "-" + symbol[:-4] + "_USDT"
    pairs = [p for p in instruments if isinstance(p, str) and p.upper().endswith(suffix)]
    if not pairs:
        raise RuntimeError(f"{symbol}: no active CoinDCX USDT futures pair")
    pair = sorted(pairs)[0]
    print(f"{symbol}: CoinDCX futures pair {pair}", flush=True)
    endpoint = "https://public.coindcx.com/market_data/candlesticks"
    end = int(time.time())
    cursor = end - days * 86400
    rows = []
    while cursor < end:
        stop = min(cursor + 3 * 86400, end)
        r = requests.get(endpoint, params={"pair": pair, "from": cursor,
                         "to": stop, "resolution": "5", "pcode": "f"}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict) or data.get("s") != "ok" or not isinstance(data.get("data"), list):
            raise RuntimeError(f"{symbol}: CoinDCX candle API error: {str(data)[:200]}")
        rows.extend(data["data"])
        cursor = stop
        time.sleep(0.08)
    if not rows:
        raise RuntimeError(f"{symbol}: CoinDCX returned no futures candles")
    df = pd.DataFrame(rows)
    needed = {"time", "open", "high", "low", "close", "volume"}
    if not needed.issubset(df.columns):
        raise RuntimeError(f"{symbol}: missing candle fields {needed - set(df.columns)}")
    df.index = pd.to_datetime(df["time"], unit="ms", utc=True)
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[df.index < pd.Timestamp.now(tz="UTC").floor("5min")]
    if len(df) < 100:
        raise RuntimeError(f"{symbol}: insufficient 5m candles: {len(df)}")
    return df

def synthetic_klines(symbol, days, seed):
    """Sirf code test karne ke liye nakli gold-jaisa data."""
    rng = np.random.default_rng(seed)
    n = days * 288
    idx = pd.date_range(end=pd.Timestamp.now('UTC').floor("5min"), periods=n, freq="5min")
    drift = np.sin(np.arange(n) / 4000.0) * 0.00004
    ret = rng.normal(drift, 0.0006)
    close = 4500 * np.exp(np.cumsum(ret))
    open_ = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.normal(0, 0.0004, n)) * close
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - wick
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": 1.0}, index=idx)


# --------------------------------------------------------------------------
# Indicators + signals (lookahead se bachne ke liye higher-TF values shift(1) hain)
# --------------------------------------------------------------------------
def prepare(df, cfg, skip_dates=()):
    d = df.copy()
    c = d["close"]
    tr = pd.concat([d.high - d.low, (d.high - c.shift()).abs(),
                    (d.low - c.shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    delta = c.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    d["rsi"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))

    h1 = c.resample("1h").last().dropna()
    h1_ema = h1.ewm(span=50, adjust=False).mean()
    d["h1c"] = h1.shift(1).reindex(d.index, method="ffill")
    d["h1ema"] = h1_ema.shift(1).reindex(d.index, method="ffill")

    dd = c.resample("1D").last().dropna()
    dd_ema = dd.ewm(span=50, adjust=False).mean()
    d["dc"] = dd.shift(1).reindex(d.index, method="ffill")
    d["dema"] = dd_ema.shift(1).reindex(d.index, method="ffill")

    d["hh"] = d.high.rolling(cfg.lookback).max().shift(1)
    d["ll"] = d.low.rolling(cfg.lookback).min().shift(1)

    hour = d.index.hour
    in_win = np.zeros(len(d), dtype=bool)
    for a, b in cfg.windows:
        in_win |= (hour >= a) & (hour < b)
    # blackout entry-time par lagta hai: signal bar ke baad agli 5m candle ke open par entry hoti hai
    entry_min = (hour * 60 + d.index.minute + 5) % 1440
    for a, b in BLACKOUTS:
        in_win &= ~((entry_min >= a) & (entry_min < b))
    skip = {pd.Timestamp(x).date() for x in skip_dates}
    if skip:
        in_win &= ~np.isin(d.index.date, list(skip))

    short = (cfg.allow_short & in_win & (c < d.ll) & (d.rsi < cfg.rsi_short)
             & (d.h1c < d.h1ema))
    long_ = (cfg.allow_long & in_win & (c > d.hh) & (d.rsi > cfg.rsi_long)
             & (d.h1c > d.h1ema))
    if cfg.long_needs_daily_trend:
        long_ &= (d.dc > d.dema)
    d["sig"] = np.where(long_, 1, np.where(short, -1, 0))
    return d


# --------------------------------------------------------------------------
# Backtest engine  (gold book: ek position, shared risk)
# --------------------------------------------------------------------------
def run_book(frames, cfgs, risk_usd):
    idx = sorted(set().union(*[set(f.index) for f in frames.values()]))
    idx = pd.DatetimeIndex(idx)
    A = {}
    for s, f in frames.items():
        g = f.reindex(idx)
        A[s] = {k: g[k].to_numpy() for k in ("open", "high", "low", "close", "atr", "sig")}
    order = sorted(frames, key=lambda s: cfgs[s].priority)

    trades, trade = [], None
    sl_count = defaultdict(int)

    def finish(t, ts, reason, rest_r):
        r = t["real"] + rest_r - COST_PCT * t["entry"] / t["risk"]
        trades.append(dict(
            symbol=t["sym"], entry_time=t["t0"], exit_time=ts,
            side="LONG" if t["dir"] == 1 else "SHORT",
            entry=round(t["entry"], 2), sl=round(t["sl0"], 2),
            tp1=round(t["tp1"], 2), tp2=round(t["tp2"], 2), tp3=round(t["tp3"], 2),
            pnl=round(r * risk_usd, 2), R=round(r, 3), reason=reason))
        if reason == "SL":
            sl_count[ts.date()] += 1

    def manage(t, ts, hi, lo, cl):
        d = t["dir"]
        # Conservative: current SL first if SL and target both touch same bar.
        hit_sl = (lo <= t["sl"]) if d == 1 else (hi >= t["sl"])
        if hit_sl:
            rest_r = (t["sl"] - t["entry"]) * d / t["risk"]
            return finish(t, ts, "SL" if t["stage"] == 0 else "TRAIL", rest_r) or True
        if t["stage"] == 0 and ((hi >= t["tp1"]) if d == 1 else (lo <= t["tp1"])):
            t["stage"] = 1
            t["sl"] = t["entry"] + d * t["risk"]  # +1R lock
        if t["stage"] == 1 and ((hi >= t["tp2"]) if d == 1 else (lo <= t["tp2"])):
            t["stage"] = 2
            t["sl"] = t["entry"] + d * 2 * t["risk"]  # +2R lock
        if t["stage"] == 2 and ((hi >= t["tp3"]) if d == 1 else (lo <= t["tp3"])):
            return finish(t, ts, "TP3", 4.0) or True
        if ts >= t["deadline"]:
            return finish(t, ts, "TIME", (cl - t["entry"]) * d / t["risk"]) or True
        return False

    for i, ts in enumerate(idx):
        if trade is None and i > 0 and sl_count[ts.date()] < MAX_SL_PER_DAY:
            for s in order:
                a, cfg = A[s], cfgs[s]
                sg, o, atr = a["sig"][i - 1], a["open"][i], a["atr"][i - 1]
                if sg == 0 or np.isnan(o) or np.isnan(atr):
                    continue
                risk = float(np.clip(atr * cfg.atr_mult, o * cfg.min_sl_pct, o * cfg.max_sl_pct))
                d = int(sg)
                trade = dict(sym=s, dir=d, entry=o, risk=risk, sl0=o - d * risk, sl=o - d * risk,
                             tp1=o + d * risk * TP1_R, tp2=o + d * risk * TP2_R,
                              tp3=o + d * risk * TP3_R,
                             stage=0, real=0.0, t0=ts,
                             deadline=ts + pd.Timedelta(hours=HOLD_HOURS))
                break
        if trade is not None:
            a = A[trade["sym"]]
            if not np.isnan(a["close"][i]):
                if manage(trade, ts, a["high"][i], a["low"][i], a["close"][i]):
                    trade = None
    return pd.DataFrame(trades)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
def summarize(name, t):
    if t.empty:
        print(f"{name}: koi trade nahi")
        return
    w, l = t[t.R > 0], t[t.R <= 0]
    print(f"\n== {name} ==")
    print(f"trades {len(t)} | win% {len(w)/len(t)*100:.0f} | total {t.R.sum():+.2f}R | "
          f"pnl {t.pnl.sum():+.1f} | avgW {w.R.mean() if len(w) else 0:.2f}R | "
          f"avgL {l.R.mean() if len(l) else 0:.2f}R")
    print(t.groupby("side").R.agg(["count", "sum"]).round(2).to_string())
    print(t.groupby("reason").R.agg(["count", "sum"]).round(2).to_string())


def scan(frames, cfgs):
    now = pd.Timestamp.now('UTC')
    print(f"UTC abhi: {now:%Y-%m-%d %H:%M}  |  IST: {now + pd.Timedelta(hours=5, minutes=30):%H:%M}")
    for s in sorted(frames, key=lambda x: cfgs[x].priority):
        f, cfg = frames[s], cfgs[s]
        row = f.iloc[-1]
        sg = int(row["sig"])
        if sg == 0:
            print(f"{s}: koi signal nahi (last closed bar {f.index[-1]:%H:%M} UTC)")
            continue
        e = float(row["close"])
        risk = float(np.clip(row["atr"] * cfg.atr_mult, e * cfg.min_sl_pct, e * cfg.max_sl_pct))
        side = "LONG" if sg == 1 else "SHORT"
        print(f"{s}: {side} signal | entry~{e:.2f} SL {e - sg*risk:.2f} "
              f"TP1 {e + sg*risk*TP1_R:.2f} TP2 {e + sg*risk*TP2_R:.2f} TP3 {e + sg*risk*TP3_R:.2f}")
    print("Yaad rakho: dono me se sirf EK hi lena (gold book).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--risk-usd", type=float, default=10.0, help="1R = kitne $")
    ap.add_argument("--skip-dates", default="", help="comma list YYYY-MM-DD (NFP/CPI/FOMC)")
    ap.add_argument("--scan", action="store_true", help="sirf latest signal")
    ap.add_argument("--synthetic", action="store_true", help="nakli data se test")
    ap.add_argument("--windows", default="", help="entry windows UTC, e.g. 0-24 ya 16-20,0-11 (default 16-20)")
    ap.add_argument("--allow-long", action="store_true", help="XAUUSDT par LONG bhi chalu (default: sirf SHORT)")
    ap.add_argument("--no-blackout", action="store_true", help="11:30-15:30 UTC block band karo")
    ap.add_argument("--out", default=".", help="CSV folder")
    a = ap.parse_args()

    skip = [x for x in a.skip_dates.split(",") if x]
    if a.allow_long:
        CFG_XAU.allow_long = True
    if a.no_blackout:
        BLACKOUTS.clear()
    if a.windows:
        wins = [tuple(int(v) for v in w.split("-")) for w in a.windows.split(",")]
        for c in CONFIGS.values():
            c.windows = wins
    days = 130 if a.scan else a.days
    frames = {}
    for k, (s, cfg) in enumerate(CONFIGS.items()):
        raw = synthetic_klines(s, days, seed=k + 1) if a.synthetic else fetch_klines(s, days)
        frames[s] = prepare(raw, cfg, skip)
        print(f"{s}: {len(raw)} candles, signals {int((frames[s].sig != 0).sum())}")

    if a.scan:
        return scan(frames, CONFIGS)

    t = run_book(frames, CONFIGS, a.risk_usd)
    if t.empty:
        print("Koi trade nahi bana.")
        return
    cols = ["entry_time", "exit_time", "side", "entry", "sl", "tp1", "tp2", "tp3", "pnl", "R", "reason"]
    for s in CONFIGS:
        x = t[t.symbol == s]
        x[cols].to_csv(f"{a.out}/{s}_TRADES.csv", index=False)
        summarize(s, x)
    t.to_csv(f"{a.out}/GOLD_BOOK_ALL_TRADES.csv", index=False)
    summarize("GOLD BOOK (combined)", t)
    print(f"\nCSV save ho gayi: {a.out}/")


if __name__ == "__main__":
    sys.exit(main())