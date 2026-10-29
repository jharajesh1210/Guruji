#!/usr/bin/env python3
"""
CRYPTO TOP-100 MULTI-STRATEGY SCANNER + PAPER TRADER
----------------------------------------------------
Aapki 10 infographics ki strategies ko ek score-engine me combine kiya gaya hai.
Data: Binance public API (no API key). Orders: SIRF PAPER (koi real order nahi).

Strategies (har ek +1 bullish / -1 bearish / 0 neutral):
  S1  Supertrend(10,3) + VWAP + CVD   (PCR crypto me nahi hota -> taker-buy delta use kiya)
  S2  VWAP + MACD crossover
  S3  ADX + DI crossover + EMA50
  S4  VWAP + ADX + EMA20 pullback
  S5  Hull MA(55) + RSI + Volume
  S6  Ichimoku cloud + RSI + Volume
  S7  Supertrend + EMA200 + MACD
  S8  VWAP + EMA9/21 + Volume spike
  S9  Cumulative Delta + Volume Profile (VAH/POC/VAL) + Price action
  S10 ADX breakout

Usage:
  pip install requests pandas numpy
  python New_crypto_top100_scanner.py scan                  # ek baar scan
  python New_crypto_top100_scanner.py scan --tf 1h --min-score 5
  python New_crypto_top100_scanner.py paper --tf 15m        # paper trading loop
  python New_crypto_top100_scanner.py selftest              # offline indicator test

Telegram (optional): export TG_TOKEN=... TG_CHAT_ID=...
DISCLAIMER: Educational/paper tool hai. Financial advice nahi. Crypto bahut volatile hai.
"""
import os
import sys
import json
import time
import argparse
import logging
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests

# ============================== CONFIG ==============================
BASES = [
    "https://api.binance.com",
    "https://data-api.binance.vision",  # fallback (public market data)
]
TOP_N = 100
TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}
HTF_MAP = {"1m": "15m", "5m": "1h", "15m": "1h", "30m": "4h", "1h": "4h", "4h": "1d", "1d": "1d"}

STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1", "EUR", "EURI",
           "AEUR", "PAXG", "XAUT", "WBTC", "WBETH", "BFUSD", "USDT"}
BAD_SUFFIX = ("UP", "DOWN", "BULL", "BEAR")

# Paper-trading params
START_EQUITY = 10_000.0
RISK_PER_TRADE = 0.01      # 1% equity risk per trade
ATR_SL_MULT = 1.5
ATR_TP_MULT = 3.0          # RR = 1:2
MAX_OPEN = 5
MAX_NOTIONAL_FRAC = 0.20   # ek position max 20% equity
FEE = 0.001                # 0.1% per side
ALLOW_SHORTS = True        # paper me futures-style short simulate
STATE_FILE = "paper_state.json"
TRADES_CSV = "paper_trades.csv"

log = logging.getLogger("scanner")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "crypto-scanner/1.0"})


# ============================== HTTP ==============================
def api_get(path, params=None, retries=3):
    last = None
    for attempt in range(retries):
        for base in BASES:
            try:
                r = SESSION.get(base + path, params=params, timeout=15)
                if r.status_code == 200:
                    return r.json()
                last = f"{base} -> HTTP {r.status_code}"
                if r.status_code in (418, 429):
                    time.sleep(2 + attempt * 2)
            except requests.RequestException as e:
                last = str(e)
        time.sleep(1 + attempt)
    raise RuntimeError(f"API failed: {path} ({last})")


def get_top_symbols(n=TOP_N):
    """Binance USDT spot pairs ko 24h quote-volume se rank karke top N."""
    data = api_get("/api/v3/ticker/24hr")
    rows = []
    for t in data:
        s = t["symbol"]
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if base in STABLES or base.endswith(BAD_SUFFIX):
            continue
        rows.append((s, float(t["quoteVolume"])))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:n]]


def get_klines(symbol, interval, limit=500):
    raw = api_get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    df = pd.DataFrame(raw, columns=["open_time", "open", "high", "low", "close", "volume",
                                    "close_time", "qav", "trades", "tbb", "tbq", "ignore"])
    df = df[["open_time", "open", "high", "low", "close", "volume", "tbb"]].copy()
    for c in ["open", "high", "low", "close", "volume", "tbb"]:
        df[c] = df[c].astype(float)
    df["time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.rename(columns={"tbb": "taker_buy"}).drop(columns=["open_time"])
    return df.iloc[:-1].reset_index(drop=True)  # sirf CLOSED candles


# ============================== INDICATORS ==============================
def ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rma(s, n):
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(close, n=14):
    d = close.diff()
    up = rma(d.clip(lower=0), n)
    dn = rma((-d).clip(lower=0), n)
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0)


def macd(close, f=12, s=26, sig=9):
    m = ema(close, f) - ema(close, s)
    sg = m.ewm(span=sig, adjust=False, min_periods=sig).mean()
    return m, sg, m - sg


def true_range(df):
    pc = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)


def atr(df, n=14):
    return rma(true_range(df), n)


def adx_di(df, n=14):
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = rma(true_range(df), n)
    pdi = 100 * rma(plus, n) / tr
    mdi = 100 * rma(minus, n) / tr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return rma(dx.fillna(0), n), pdi, mdi


def supertrend(df, n=10, mult=3.0):
    a = atr(df, n).values
    hl2 = ((df["high"] + df["low"]) / 2).values
    c = df["close"].values
    ub, lb = hl2 + mult * a, hl2 - mult * a
    fub, flb = ub.copy(), lb.copy()
    d = np.ones(len(df))
    st = np.full(len(df), np.nan)
    for i in range(1, len(df)):
        if np.isnan(a[i]):
            continue
        if np.isnan(fub[i - 1]) or np.isnan(a[i - 1]):
            fub[i], flb[i] = ub[i], lb[i]
        else:
            fub[i] = ub[i] if (ub[i] < fub[i - 1] or c[i - 1] > fub[i - 1]) else fub[i - 1]
            flb[i] = lb[i] if (lb[i] > flb[i - 1] or c[i - 1] < flb[i - 1]) else flb[i - 1]
        if d[i - 1] == 1 and c[i] < flb[i]:
            d[i] = -1
        elif d[i - 1] == -1 and c[i] > fub[i]:
            d[i] = 1
        else:
            d[i] = d[i - 1]
        st[i] = flb[i] if d[i] == 1 else fub[i]
    return pd.Series(st, index=df.index), pd.Series(d, index=df.index)


def wma(s, n):
    w = np.arange(1, n + 1)
    return s.rolling(n).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)


def hull(s, n=55):
    return wma(2 * wma(s, n // 2) - wma(s, n), int(round(np.sqrt(n))))


def session_vwap(df):
    """Daily (UTC) anchored VWAP."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    day = df["time"].dt.floor("D")
    pv = (tp * df["volume"]).groupby(day).cumsum()
    v = df["volume"].groupby(day).cumsum()
    return pv / v.replace(0, np.nan)


def ichimoku(df):
    hi, lo = df["high"], df["low"]
    tenkan = (hi.rolling(9).max() + lo.rolling(9).min()) / 2
    kijun = (hi.rolling(26).max() + lo.rolling(26).min()) / 2
    sa = ((tenkan + kijun) / 2).shift(26)
    sb = ((hi.rolling(52).max() + lo.rolling(52).min()) / 2).shift(26)
    return tenkan, kijun, sa, sb


def cvd(df):
    """Taker-buy se approximate Cumulative Volume Delta."""
    return (2 * df["taker_buy"] - df["volume"]).cumsum()


def volume_profile(df, bins=30, va_pct=0.70):
    lo, hi = df["low"].min(), df["high"].max()
    if hi <= lo:
        return None
    edges = np.linspace(lo, hi, bins + 1)
    idx = np.clip(np.digitize(df["close"].values, edges) - 1, 0, bins - 1)
    vol = np.bincount(idx, weights=df["volume"].values, minlength=bins)
    centers = (edges[:-1] + edges[1:]) / 2
    poc_i = int(vol.argmax())
    total, acc = vol.sum(), vol[poc_i]
    l = h = poc_i
    while acc < va_pct * total and (l > 0 or h < bins - 1):
        lv = vol[l - 1] if l > 0 else -1
        hv = vol[h + 1] if h < bins - 1 else -1
        if hv >= lv:
            h += 1
            acc += vol[h]
        else:
            l -= 1
            acc += vol[l]
    return {"poc": centers[poc_i], "vah": edges[h + 1], "val": edges[l]}


def crossed(a, b, n, up=True):
    """Pichhli n candles me a ne b ko cross kiya?"""
    prev = (a.shift(1) <= b.shift(1)) if up else (a.shift(1) >= b.shift(1))
    now = (a > b) if up else (a < b)
    return bool((prev & now).iloc[-n:].any())


# ============================== STRATEGY ENGINE ==============================
def analyze(df):
    """df (closed candles) -> dict(scores, price, atr). None agar data kam ho."""
    if len(df) < 260:
        return None
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    px = float(c.iloc[-1])

    vw = session_vwap(df)
    e9, e20, e21, e50, e200 = ema(c, 9), ema(c, 20), ema(c, 21), ema(c, 50), ema(c, 200)
    r = rsi(c)
    m, sg, hist = macd(c)
    adx, pdi, mdi = adx_di(df)
    st, sdir = supertrend(df)
    hm = hull(c, 55)
    ten, kij, sa, sb = ichimoku(df)
    cv = cvd(df)
    vsma = v.rolling(20).mean()
    a14 = atr(df)

    L = lambda s: float(s.iloc[-1])
    vol_ok = L(v) > 1.2 * L(vsma)
    s = {}

    # S1 Supertrend + VWAP + CVD
    cvd_up = L(cv) > float(cv.iloc[-21]) and L(cv) > float(cv.rolling(20).mean().iloc[-1])
    cvd_dn = L(cv) < float(cv.iloc[-21]) and L(cv) < float(cv.rolling(20).mean().iloc[-1])
    s["S1"] = 1 if (L(sdir) == 1 and px > L(vw) and cvd_up) else -1 if (L(sdir) == -1 and px < L(vw) and cvd_dn) else 0

    # S2 VWAP + MACD crossover (last 3 candles)
    s["S2"] = 1 if (px > L(vw) and crossed(m, sg, 3, True)) else -1 if (px < L(vw) and crossed(m, sg, 3, False)) else 0

    # S3 ADX>20 + DI crossover + EMA50
    adx_rise = L(adx) > float(adx.iloc[-3])
    s["S3"] = (1 if (L(adx) > 20 and adx_rise and L(pdi) > L(mdi) and crossed(pdi, mdi, 5, True) and px > L(e50))
               else -1 if (L(adx) > 20 and adx_rise and L(mdi) > L(pdi) and crossed(mdi, pdi, 5, True) and px < L(e50)) else 0)

    # S4 VWAP + ADX + EMA20 pullback
    touch_dn = bool((l.iloc[-3:] <= e20.iloc[-3:] * 1.002).any())
    touch_up = bool((h.iloc[-3:] >= e20.iloc[-3:] * 0.998).any())
    s["S4"] = (1 if (L(adx) > 20 and px > L(vw) and px > L(e20) and touch_dn and L(pdi) > L(mdi))
               else -1 if (L(adx) > 20 and px < L(vw) and px < L(e20) and touch_up and L(mdi) > L(pdi)) else 0)

    # S5 Hull MA + RSI + Volume
    s["S5"] = (1 if (px > L(hm) and L(hm) > float(hm.iloc[-2]) and L(r) > 50 and vol_ok)
               else -1 if (px < L(hm) and L(hm) < float(hm.iloc[-2]) and L(r) < 50 and vol_ok) else 0)

    # S6 Ichimoku + RSI + Volume
    top, bot = max(L(sa), L(sb)), min(L(sa), L(sb))
    s["S6"] = (1 if (px > top and L(ten) > L(kij) and L(r) > 50 and vol_ok)
               else -1 if (px < bot and L(ten) < L(kij) and L(r) < 50 and vol_ok) else 0)

    # S7 Supertrend + EMA200 + MACD
    s["S7"] = (1 if (px > L(e200) and L(sdir) == 1 and (crossed(m, sg, 5, True) or (L(m) > L(sg) and L(m) > 0)))
               else -1 if (px < L(e200) and L(sdir) == -1 and (crossed(m, sg, 5, False) or (L(m) < L(sg) and L(m) < 0))) else 0)

    # S8 VWAP + EMA9/21 + Volume spike (last 3 candles)
    spike = bool((v.iloc[-3:] > 2.0 * vsma.iloc[-3:]).any())
    s["S8"] = 1 if (px > L(vw) and L(e9) > L(e21) and spike) else -1 if (px < L(vw) and L(e9) < L(e21) and spike) else 0

    # S9 CVD + Volume Profile + Price action
    vp = volume_profile(df.iloc[-105:-5])
    s["S9"] = 0
    if vp:
        rec_hi, rec_lo = float(h.iloc[-5:].max()), float(l.iloc[-5:].min())
        if rec_hi > vp["vah"] and px > vp["poc"] and cvd_up:
            s["S9"] = 1
        elif rec_lo < vp["val"] and px < vp["poc"] and cvd_dn:
            s["S9"] = -1

    # S10 ADX breakout (20-candle range)
    res, sup = float(h.iloc[-21:-1].max()), float(l.iloc[-21:-1].min())
    big_vol = L(v) > 1.5 * L(vsma)
    s["S10"] = (1 if (px > res and L(adx) > 25 and L(pdi) > L(mdi) and big_vol)
                else -1 if (px < sup and L(adx) > 25 and L(mdi) > L(pdi) and big_vol) else 0)

    score = int(sum(s.values()))
    return {"scores": s, "score": score, "bull": sum(1 for x in s.values() if x > 0),
            "bear": sum(1 for x in s.values() if x < 0), "price": px, "atr": L(a14),
            "rsi": L(r), "adx": L(adx), "ema200": L(e200)}


def htf_bias(symbol, tf):
    """Higher-timeframe filter: +1 bullish, -1 bearish, 0 neutral."""
    try:
        d = get_klines(symbol, HTF_MAP[tf], 300)
        c = d["close"]
        e50, e200 = float(ema(c, 50).iloc[-1]), float(ema(c, 200).iloc[-1])
        px = float(c.iloc[-1])
        return 1 if (px > e50 > e200) else -1 if (px < e50 < e200) else 0
    except Exception:
        return 0


def decide(res, min_score, bias=None):
    """Final LONG/SHORT/None."""
    if res is None:
        return None
    sc = res["score"]
    if sc >= min_score and res["bear"] == 0 and (bias in (None, 1, 0)) and res["rsi"] < 80:
        return "LONG"
    if sc <= -min_score and res["bull"] == 0 and (bias in (None, -1, 0)) and res["rsi"] > 20:
        return "SHORT"
    return None


# ============================== TELEGRAM ==============================
def tg(msg):
    tok, chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT_ID")
    if not tok or not chat:
        return
    try:
        response = SESSION.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                                data={"chat_id": chat, "text": msg}, timeout=10)
        if not response.ok:
            log.warning("Telegram rejected message: HTTP %s %s", response.status_code, response.text[:300])
    except requests.RequestException as e:
        log.warning("Telegram fail: %s", e)


# ============================== SCAN ==============================
def scan_all(symbols, tf, workers=8):
    def work(sym):
        try:
            return sym, get_klines(sym, tf, 500)
        except Exception as e:
            log.warning("%s skip: %s", sym, e)
            return sym, None
    with ThreadPoolExecutor(max_workers=workers) as ex:
        data = dict(ex.map(work, symbols))
    out = {}
    for sym, df in data.items():
        if df is not None:
            out[sym] = (df, analyze(df))
    return out


def cmd_scan(args):
    syms = get_top_symbols(args.top)
    log.info("Scanning %d coins on %s ...", len(syms), args.tf)
    results = scan_all(syms, args.tf)
    rows = []
    for sym, (df, res) in results.items():
        if not res:
            continue
        rows.append({"symbol": sym, "price": res["price"], "score": res["score"], "bull": res["bull"],
                     "bear": res["bear"], "rsi": round(res["rsi"], 1), "adx": round(res["adx"], 1),
                     **res["scores"]})
    if not rows:
        print("Koi data nahi mila.")
        return
    tab = pd.DataFrame(rows).sort_values("score", key=abs, ascending=False)
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    print(f"\n=== TOP SETUPS ({args.tf}, min_score={args.min_score}) ===")
    shown = tab[tab["score"].abs() >= args.min_score]
    print(shown.to_string(index=False) if len(shown) else "Abhi koi strong setup nahi.")
    msgs = []
    for _, row in shown.iterrows():
        res = results[row["symbol"]][1]
        bias = htf_bias(row["symbol"], args.tf) if not args.no_htf else None
        side = decide(res, args.min_score, bias)
        if side:
            sl = res["price"] - ATR_SL_MULT * res["atr"] * (1 if side == "LONG" else -1)
            tp = res["price"] + ATR_TP_MULT * res["atr"] * (1 if side == "LONG" else -1)
            m = f"{side} {row['symbol']} @ {res['price']:.6g} | score {res['score']} | SL {sl:.6g} TP {tp:.6g} ({args.tf})"
            print(m)
            msgs.append(m)
    if msgs:
        tg("📊 Crypto scan\n" + "\n".join(msgs))
    else:
        tg(f"✅ SCAN COMPLETED | Crypto Top100 | {args.tf} | NO SIGNAL | checked={len(results)} | min_score={args.min_score}")


# ============================== PAPER TRADING ==============================
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"equity": START_EQUITY, "positions": {}, "closed": 0, "wins": 0}


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=2)


def log_trade(row):
    hdr = not os.path.exists(TRADES_CSV)
    pd.DataFrame([row]).to_csv(TRADES_CSV, mode="a", header=hdr, index=False)


def close_pos(st, sym, price, reason, when):
    p = st["positions"].pop(sym)
    d = 1 if p["side"] == "LONG" else -1
    pnl = (price - p["entry"]) * p["qty"] * d - FEE * p["qty"] * (price + p["entry"])
    st["equity"] += pnl
    st["closed"] += 1
    st["wins"] += int(pnl > 0)
    log_trade({"symbol": sym, "side": p["side"], "entry": p["entry"], "exit": price, "qty": p["qty"],
               "pnl": round(pnl, 2), "reason": reason, "entry_time": p["time"], "exit_time": str(when),
               "equity": round(st["equity"], 2)})
    m = f"{'✅' if pnl > 0 else '❌'} CLOSE {p['side']} {sym} @ {price:.6g} ({reason}) PnL {pnl:+.2f} | Equity {st['equity']:.2f}"
    print(m)
    tg(m)


def manage_positions(st, results):
    for sym in list(st["positions"]):
        if sym not in results:
            continue
        df, res = results[sym]
        p = st["positions"][sym]
        t0 = pd.Timestamp(p["time"])
        new = df[df["time"] > t0]
        for _, k in new.iterrows():
            long = p["side"] == "LONG"
            hit_sl = k["low"] <= p["sl"] if long else k["high"] >= p["sl"]
            hit_tp = k["high"] >= p["tp"] if long else k["low"] <= p["tp"]
            if hit_sl:  # conservative: SL pehle
                close_pos(st, sym, p["sl"], "SL" if not p.get("be") else "BE", k["time"])
                break
            if hit_tp:
                close_pos(st, sym, p["tp"], "TP", k["time"])
                break
            moved = (k["high"] - p["entry"]) if long else (p["entry"] - k["low"])
            if not p.get("be") and moved >= p["risk"]:  # 1R par breakeven
                p["sl"], p["be"] = p["entry"], True
        else:
            # opposite strong signal par exit
            if sym in st["positions"] and res:
                if (p["side"] == "LONG" and res["score"] <= -3) or (p["side"] == "SHORT" and res["score"] >= 3):
                    close_pos(st, sym, res["price"], "REVERSAL", df["time"].iloc[-1])


def open_new(st, results, args):
    cands = sorted(((s, r) for s, (_, r) in results.items() if r), key=lambda x: abs(x[1]["score"]), reverse=True)
    for sym, res in cands:
        if len(st["positions"]) >= MAX_OPEN:
            break
        if sym in st["positions"] or abs(res["score"]) < args.min_score:
            continue
        bias = htf_bias(sym, args.tf) if not args.no_htf else None
        side = decide(res, args.min_score, bias)
        if not side or (side == "SHORT" and not ALLOW_SHORTS):
            continue
        d = 1 if side == "LONG" else -1
        entry, risk = res["price"], ATR_SL_MULT * res["atr"]
        if risk <= 0:
            continue
        qty = (st["equity"] * RISK_PER_TRADE) / risk
        qty = min(qty, st["equity"] * MAX_NOTIONAL_FRAC / entry)
        st["positions"][sym] = {"side": side, "entry": entry, "qty": qty, "risk": risk,
                                "sl": entry - d * risk, "tp": entry + d * ATR_TP_MULT * res["atr"],
                                "time": str(results[sym][0]["time"].iloc[-1]), "score": res["score"]}
        m = (f"🟢 OPEN {side} {sym} @ {entry:.6g} qty {qty:.4g} | SL {st['positions'][sym]['sl']:.6g} "
             f"TP {st['positions'][sym]['tp']:.6g} | score {res['score']}")
        print(m)
        tg(m)


def cmd_paper(args):
    st = load_state()
    iv = TF_SECONDS[args.tf]
    print(f"PAPER MODE | tf={args.tf} | equity={st['equity']:.2f} | (real orders nahi hote)")
    while True:
        try:
            syms = get_top_symbols(args.top)
            results = scan_all(syms, args.tf)
            manage_positions(st, results)
            open_new(st, results, args)
            save_state(st)
            wr = (st["wins"] / st["closed"] * 100) if st["closed"] else 0
            print(f"[{pd.Timestamp.utcnow():%H:%M}] equity {st['equity']:.2f} | open {len(st['positions'])} "
                  f"| closed {st['closed']} | win% {wr:.0f}")
        except Exception as e:
            log.error("cycle error: %s", e)
        nxt = (int(time.time()) // iv + 1) * iv + 5  # next candle close + 5s
        time.sleep(max(5, nxt - time.time()))


# ============================== SELFTEST ==============================
def cmd_selftest(_):
    rng = np.random.default_rng(7)
    n = 600
    ret = rng.normal(0.0004, 0.01, n)
    close = 100 * np.exp(np.cumsum(ret))
    op = np.r_[close[0], close[:-1]]
    hi = np.maximum(op, close) * (1 + rng.uniform(0, 0.005, n))
    lo = np.minimum(op, close) * (1 - rng.uniform(0, 0.005, n))
    vol = rng.uniform(100, 1000, n)
    df = pd.DataFrame({"time": pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC"),
                       "open": op, "high": hi, "low": lo, "close": close, "volume": vol,
                       "taker_buy": vol * rng.uniform(0.3, 0.7, n)})
    res = analyze(df)
    assert res is not None
    print("Selftest OK ->", res["scores"], "| total score:", res["score"], "| ATR:", round(res["atr"], 4))


# ============================== MAIN ==============================
def main():
    ap = argparse.ArgumentParser(description="Crypto Top-100 multi-strategy scanner / paper trader")
    ap.add_argument("mode", choices=["scan", "paper", "selftest"])
    ap.add_argument("--tf", default="15m", choices=list(TF_SECONDS))
    ap.add_argument("--top", type=int, default=TOP_N)
    ap.add_argument("--min-score", type=int, default=4, help="kitni strategies align ho (1-10)")
    ap.add_argument("--no-htf", action="store_true", help="higher-timeframe filter band")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    {"scan": cmd_scan, "paper": cmd_paper, "selftest": cmd_selftest}[args.mode](args)


if __name__ == "__main__":
    main()
