"""
VWAP PULLBACK SHORT  --  Reliance (NSE) 5-minute intraday strategy
=================================================================
Standalone file: sirf pandas + numpy chahiye. Koi password / API key isme nahi hai.

IDEA
----
Jis din Reliance kamzor ho (VWAP aur EMA20 ke neeche), aur price thoda upar uchhal ke
ek chhota "swing high" banaye, us uchhal pe SELL (short) karo.

RULES (5-minute candles)
------------------------
Indicators:
  * ATR(14)  -- Wilder (RMA) average true range
  * VWAP     -- har din 9:15 se reset: sum(typical price x volume) / sum(volume)
  * EMA(20)  -- close ka
  * ADX(14)  -- Wilder
  * Nifty 15-min return = Nifty close ka 3 candle (15 min) ka % change

SELL setup -- "swing candle" i pe sab sahi hona chahiye:
  1. Close(i) < VWAP(i)                          (aaj Reliance kamzor)
  2. Close(i) < EMA20(i)                         (short-term trend neeche)
  3. High(i) = 5 candles (i-2 .. i+2) ka sabse bada high   (swing high, same din)
  4. Nifty 15-min return(i) > +0.10%   YA   ADX(i) > 30
Entry:
  * Swing candle ke 2 candle baad swing confirm hota hai -> uske AGLI candle (i+3) ke OPEN pe SELL
  * Entry sirf 9:25 se 14:30 ke beech
  * Risk = SL - entry; agar risk < 0.3 x ATR ya > 5 x ATR to trade skip
Exit:
  * Stop loss = High(i) + 1 x ATR(i)
  * Target    = entry - 2 x risk          (2R)
  * Same candle me SL aur target dono touch hon to SL maana jaata hai (conservative)
  * 15:15 wali candle ke close pe jo bhi bacha, band
  * Din me max 2 trade, ek waqt pe ek hi position
Size: floor(capital / entry) shares (default Rs 1,00,000, bina leverage)
Charges: 0.06% round trip (brokerage + STT + slippage, approx)

BACKTEST RESULTS (Rs 1 lakh, sirf ye strategy akeli, charges 0.06% kaat ke,
Angel One 5-min data, 12 Oct 2023 - 26 Oct 2026). Ye file khud yahi numbers deti hai.

  Period                         Trades  Win%   PF    Net P&L   Max DD   Max loss streak
  Oct 2023 - Oct 2024 (unseen)     116   38.8  0.94   -1,612    -3,496         5
  Oct 2024 - Oct 2025 (unseen)     132   45.5  0.95   -1,333    -8,772        10
  Oct 2025 - Oct 2026 (built on)   124   53.2  2.13  +21,318    -1,755         4
  3 SAAL TOTAL                     372   46.0  1.25  +18,374    -8,772        10
  Avg win Rs +544, avg loss Rs -371, profit months 18/37.

  !! Rules Oct 2025 - Oct 2026 ke data se chune gaye the (us saal Reliance gira tha).
     Jo 2 saal strategy banate waqt nahi dekhe, unme ye lagbhag BARABAR / thoda loss me rahi.
     Matlab edge pakka nahi hai. Asli paisa lagane se pehle paper trade karo.
     Ye financial advice nahi hai -- sirf research / education ke liye.

USAGE
-----
  # 1) CSV se backtest (columns: datetime,open,high,low,close,volume ; 5-min, IST)
  python vwap_pullback_short.py --reliance RELIANCE_5m.csv --nifty NIFTY_5m.csv

  # 2) Angel One SmartAPI se data laake backtest (apne env vars set karo):
  #    ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PASSWORD (MPIN), ANGEL_TOTP_SECRET
  #    pip install smartapi-python pyotp
  python vwap_pullback_short.py --angel --days 365

  # 3) Aaj ka live signal check (Angel One se):
  python vwap_pullback_short.py --angel --live
"""
import argparse
import math
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# ------------------------------------------------------------------ settings
CAPITAL = 100_000
COST = 0.0006              # 0.06% round trip
ATR_N, EMA_N, ADX_N = 14, 20, 14
NIFTY_RET_MIN = 0.10       # % in 15 min
ADX_MIN = 30.0
SL_ATR = 1.0               # SL = swing high + 1 x ATR
RR = 2.0                   # target = 2R
ENTRY_START, ENTRY_END, EXIT_TIME = (9, 25), (14, 30), (15, 15)
MAX_TRADES_PER_DAY = 2


# ---------------------------------------------------------------- indicators
def _rma(s, n):
    return s.ewm(alpha=1 / n, adjust=False).mean()


def prepare(rel: pd.DataFrame, nif: pd.DataFrame) -> pd.DataFrame:
    """rel / nif: 5-min OHLCV, DatetimeIndex (IST, timezone ke bina). Return: signals ke saath df."""
    d = rel.between_time("09:15", "15:29").copy().sort_index()
    d["day"] = d.index.normalize()
    d["tod"] = d.index.hour * 60 + d.index.minute
    h, l, c, v = d.high, d.low, d.close, d.volume
    pc = c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    d["atr"] = _rma(tr, ATR_N)
    tp = (h + l + c) / 3
    d["vwap"] = ((tp * v).groupby(d.day).cumsum() / v.groupby(d.day).cumsum().replace(0, np.nan)).ffill()
    d["ema20"] = c.ewm(span=EMA_N, adjust=False).mean()
    up, dn = h.diff(), -l.diff()
    pdm = up.where((up > dn) & (up > 0), 0.0)
    ndm = dn.where((dn > up) & (dn > 0), 0.0)
    pdi, ndi = 100 * _rma(pdm, ADX_N) / d.atr, 100 * _rma(ndm, ADX_N) / d.atr
    d["adx"] = _rma(100 * (pdi - ndi).abs() / (pdi + ndi), ADX_N)
    n = nif.between_time("09:15", "15:29").sort_index()
    d["nifty_ret15"] = (n.close.pct_change(3) * 100).reindex(d.index)
    same_day = (d.day.shift(-2) == d.day) & (d.day.shift(2) == d.day)
    d["swing_high"] = (h == h.rolling(5, center=True).max()) & same_day
    d["setup"] = (d.swing_high & (c < d.vwap) & (c < d.ema20)
                  & ((d.nifty_ret15 > NIFTY_RET_MIN) | (d.adx > ADX_MIN)))
    return d


# ------------------------------------------------------------------- engine
def run_day(t: pd.DataFrame, capital=CAPITAL):
    """Ek din ke candles pe strategy. Return: list of trades (dict)."""
    o, h, l, c, a = (t[k].values for k in ("open", "high", "low", "close", "atr"))
    tod, setup, idx = t.tod.values, t.setup.values, t.index
    t0, t1, tx = (ENTRY_START[0] * 60 + ENTRY_START[1], ENTRY_END[0] * 60 + ENTRY_END[1],
                  EXIT_TIME[0] * 60 + EXIT_TIME[1])
    trades, pos, last_exit, count = [], None, -1, 0
    for j in range(len(t)):
        if pos is not None:
            ex = why = None
            if h[j] >= pos["sl"]:
                ex, why = pos["sl"], "Stop loss"
            elif l[j] <= pos["tp"]:
                ex, why = pos["tp"], "Target 2R"
            elif tod[j] >= tx:
                ex, why = c[j], "15:15 exit"
            if ex is not None:
                q = pos["qty"]
                pnl = (pos["entry"] - ex) * q - COST * pos["entry"] * q
                pos.update(exit=round(float(ex), 2), exit_time=idx[j], reason=why, pnl_rs=round(pnl, 2))
                trades.append(pos)
                pos, last_exit = None, j
            continue
        i = j - 3
        if i < 0 or j <= last_exit or count >= MAX_TRADES_PER_DAY or not setup[i] or not (t0 <= tod[j] <= t1):
            continue
        e = float(o[j])
        sl = float(h[i] + SL_ATR * a[i])
        risk = sl - e
        if not (0.3 * a[i] < risk <= 5 * a[i]):
            continue
        pos = dict(signal_time=idx[i], entry_time=idx[j], side="SELL", entry=round(e, 2),
                   sl=round(sl, 2), tp=round(e - RR * risk, 2), qty=max(1, math.floor(capital / e)))
        count += 1
        # entry wali candle me hi SL / target
        if h[j] >= pos["sl"] or l[j] <= pos["tp"]:
            ex, why = (pos["sl"], "Stop loss") if h[j] >= pos["sl"] else (pos["tp"], "Target 2R")
            q = pos["qty"]
            pos.update(exit=ex, exit_time=idx[j], reason=why,
                       pnl_rs=round((pos["entry"] - ex) * q - COST * pos["entry"] * q, 2))
            trades.append(pos)
            pos, last_exit = None, j
    if pos is not None:            # data khatam (live din) -> open trade
        pos.update(exit=None, exit_time=None, reason="OPEN", pnl_rs=None)
        trades.append(pos)
    return trades


def backtest(d: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, t in d.groupby("day"):
        rows += [x for x in run_day(t) if x["reason"] != "OPEN"]
    return pd.DataFrame(rows)


def stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"Trades": 0}
    p = t.pnl_rs
    eq = p.cumsum()
    m = t.groupby(pd.to_datetime(t.entry_time).dt.to_period("M")).pnl_rs.sum()
    streak = cur = 0
    for v in p:
        cur = cur + 1 if v < 0 else 0
        streak = max(streak, cur)
    return {"Trades": len(t), "Win %": round((p > 0).mean() * 100, 1),
            "Profit Factor": round(p[p > 0].sum() / -p[p < 0].sum(), 2) if (p < 0).any() else float("inf"),
            "Net P&L Rs": round(p.sum()), "Avg win Rs": round(p[p > 0].mean()), "Avg loss Rs": round(p[p < 0].mean()),
            "Max DD Rs": round((eq - eq.cummax()).min()), "Max loss streak": streak,
            "Profit months": f"{(m > 0).sum()}/{len(m)}"}


# -------------------------------------------------------------- data loaders
def load_csv(path):
    d = pd.read_csv(path)
    d.columns = [x.strip().lower() for x in d.columns]
    d["datetime"] = pd.to_datetime(d["datetime"])
    if d["datetime"].dt.tz is not None:
        d["datetime"] = d["datetime"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    return d.set_index("datetime")[["open", "high", "low", "close", "volume"]].sort_index()


def load_angel(days):
    """Angel One SmartAPI (env vars se login). RELIANCE token 2885, NIFTY 99926000."""
    import time
    import pyotp
    from SmartApi import SmartConnect
    api = SmartConnect(api_key=os.environ["ANGEL_API_KEY"])
    api.generateSession(os.environ["ANGEL_CLIENT_ID"], os.environ["ANGEL_PASSWORD"],
                        pyotp.TOTP(os.environ["ANGEL_TOTP_SECRET"]).now())
    out = {}
    end = datetime.now()
    for name, token in (("RELIANCE", "2885"), ("NIFTY", "99926000")):
        rows, cur = [], end - timedelta(days=days)
        while cur < end:
            nxt = min(cur + timedelta(days=30), end)
            for attempt in range(6):
                r = api.getCandleData({"exchange": "NSE", "symboltoken": token, "interval": "FIVE_MINUTE",
                                       "fromdate": cur.strftime("%Y-%m-%d 09:15"),
                                       "todate": nxt.strftime("%Y-%m-%d %H:%M")})
                if r and r.get("status") is not False:
                    rows += r.get("data") or []
                    break
                time.sleep(3 * (attempt + 1))
            cur = nxt + timedelta(days=1)
            time.sleep(0.6)
        df = pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "volume"])
        df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(None)
        out[name] = df.drop_duplicates("datetime").set_index("datetime").sort_index()
    return out["RELIANCE"], out["NIFTY"]


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="VWAP Pullback Short - Reliance 5m")
    ap.add_argument("--reliance", help="Reliance 5-min CSV")
    ap.add_argument("--nifty", help="Nifty 5-min CSV")
    ap.add_argument("--angel", action="store_true", help="Angel One se data lao (env vars)")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--live", action="store_true", help="aaj ka signal / open trade dikhao")
    ap.add_argument("--out", default="vwap_pullback_trades.csv")
    a = ap.parse_args()

    if a.angel:
        rel, nif = load_angel(7 if a.live else a.days)
    elif a.reliance and a.nifty:
        rel, nif = load_csv(a.reliance), load_csv(a.nifty)
    else:
        ap.error("--reliance aur --nifty CSV do, ya --angel use karo")

    if a.live:
        now = datetime.utcnow() + timedelta(hours=5, minutes=30)
        rel = rel[rel.index + pd.Timedelta(minutes=5) <= now]          # sirf poori band candles
        nif = nif[nif.index + pd.Timedelta(minutes=5) <= now]
        d = prepare(rel, nif)
        t = d[d.day == d.day.iloc[-1]]
        last = t.iloc[-1]
        print(f"{t.index[-1]}  close {last.close:.2f}  VWAP {last.vwap:.2f}  EMA20 {last.ema20:.2f}  "
              f"ADX {last.adx:.1f}  Nifty15m {last.nifty_ret15:+.2f}%")
        trades = run_day(t)
        print("Aaj ke trades:" if trades else "Aaj abhi tak koi trade nahi.")
        for x in trades:
            pnl = "" if x["pnl_rs"] is None else f"P&L Rs {x['pnl_rs']:+,.0f}"
            print(f"  SELL {x['entry']} @ {x['entry_time']:%H:%M}  SL {x['sl']}  Target {x['tp']}  "
                  f"qty {x['qty']}  -> {x['reason']} {x['exit'] or ''} {pnl}")
        if len(t) >= 3 and t.setup.values[-3]:
            print("  >> Setup confirm hua: agli candle ke open pe SELL")
        return

    d = prepare(rel, nif)
    t = backtest(d)
    print(f"Data: {d.index[0]:%d %b %Y} - {d.index[-1]:%d %b %Y} | Capital Rs {CAPITAL:,}\n")
    print("TOTAL:", stats(t))
    te = pd.to_datetime(t.entry_time)
    for y, g in t.groupby(te.dt.to_period("Y")):
        print(f"  {y}:", stats(g))
    t.to_csv(a.out, index=False)
    print(f"\nSaare trades -> {a.out}")


if __name__ == "__main__":
    main()
