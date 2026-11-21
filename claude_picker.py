"""
claude_picker.py - "Abhi kis coin mein entry leni chahiye?"  (decision helper, NO orders)

How the decision is made (based on our backtests, 16-21 Nov 2026):

STEP 1 - HARD RULES (all must pass, else the coin is not a trade):
  1. 24h volume >= 10M USDT (liquid, fees/slippage small)
  2. Last CLOSED 15m candle broke the previous day's level:
       BUY  : high > PDH and close > PDH      SELL : low < PDL and close < PDL
  3. Not late: close at most 0.5% beyond PDH/PDL      (late entries cost the most in live tests)
  4. Volume of that candle >= 1.5 x average of the last 20 candles
  5. Wick against the trade < 40% of the candle     (fake breakouts have long wicks)
  6. SL (breakout candle low/high) between 0.75% and 2% away   (tighter SL -> fees eat the trade)

STEP 2 - SCORE 0-100 (which of the valid coins is BEST right now):
  volume strength   25  (1.5x -> 0 ... 4x+ -> 25)
  closeness         20  (0% beyond level -> 20 ... 0.5% -> 0)
  candle quality    15  (big body, small wick against the trade)
  trend             15  (EMA20 vs EMA50 and price vs EMA20 in trade direction)
  room to run       10  (no 3-day high/low within 2R in the trade direction)
  session           10  (NY 10, Asian 5, London 0 - London was weakest in the 90-day test)
  fee efficiency     5  (SL >= 1%)
  DECISION: score >= 60 -> ENTRY,  40-59 -> WEAK (skip or small),  < 40 -> SKIP

STEP 3 - WATCHLIST (no entry yet): coins whose last close is within 0.3% BELOW PDH (or above PDL)
  with volume >= 1.2x -> "ready to break", check again after the next candle.

Exits to use (current v2 rules): SL at candle low/high, loss cap 1.5R or 3%, lock 1:2/1:3/1:4/1:5, exit 1:6.

Usage:  python claude_picker.py            -> prints the decision table
        python claude_picker.py --tg       -> also sends the top picks to Telegram (TG_TOKEN / TG_CHAT_ID)
"""
import os
import sys
import time
from datetime import time as dtime

import numpy as np
import pandas as pd

import rd_trading as rd

MAX_DIST = 0.5
MIN_SL, MAX_SL = 0.75, 2.0
VOL_X = 1.5
TOP_N = int(os.getenv("PICKER_TOP", "8"))


def session(ts):
    t = ts.time()
    if dtime(5, 30) <= t < dtime(13, 30):
        return "Asian"
    if dtime(13, 30) <= t < dtime(18, 30):
        return "London"
    return "NY"


def analyse(pair, df):
    """Return a decision dict for this coin's last closed candle, or None."""
    d, daily = rd.prepare(df)
    if len(d) < 60:
        return None
    d = d.copy()
    d["EMA20"] = d["Close"].ewm(span=20, adjust=False).mean()
    d["EMA50"] = d["Close"].ewm(span=50, adjust=False).mean()
    r = d.iloc[-1]
    pdh, pdl = rd.levels_for(r["Date"], daily)
    if pdh is None or pdl is None:
        return None
    o, h, l, c, v = (float(r[k]) for k in ("Open", "High", "Low", "Close", "Volume"))
    vma = float(d["Volume"].iloc[-21:-1].mean())
    if vma <= 0:
        return None
    vx = v / vma
    rng = h - l if h > l else 1e-12
    body = abs(c - o) / rng
    ses = session(r["Timestamp"])
    out = {"pair": pair, "time": str(r["Timestamp"]), "session": ses, "close": c, "PDH": pdh, "PDL": pdl, "volx": round(vx, 2)}

    for side in ("BUY", "SELL"):
        if side == "BUY":
            broke = h > pdh and c > pdh
            dist = (c - pdh) / c * 100
            wick = (h - max(o, c)) / rng
            sl = l
            trend = (r["EMA20"] > r["EMA50"]) + (c > r["EMA20"])
            hi3 = float(d["High"].iloc[-289:-1].max())
            room = hi3 <= c or (hi3 - c) >= 2 * (c - sl)
        else:
            broke = l < pdl and c < pdl
            dist = (pdl - c) / c * 100
            wick = (min(o, c) - l) / rng
            sl = h
            trend = (r["EMA20"] < r["EMA50"]) + (c < r["EMA20"])
            lo3 = float(d["Low"].iloc[-289:-1].min())
            room = lo3 >= c or (c - lo3) >= 2 * (sl - c)
        if not broke:
            continue
        sl_pct = abs(c - sl) / c * 100
        fails = []
        if dist > MAX_DIST:
            fails.append(f"late {dist:.2f}% beyond level (max {MAX_DIST}%)")
        if vx < VOL_X:
            fails.append(f"volume only {vx:.1f}x (need {VOL_X}x)")
        if wick >= 0.4:
            fails.append(f"wick {wick*100:.0f}% (fake-breakout risk)")
        if sl_pct < MIN_SL:
            fails.append(f"SL only {sl_pct:.2f}% (fees eat it)")
        sl_used = sl
        if sl_pct > MAX_SL:
            sl_used = c * (1 - MAX_SL / 100) if side == "BUY" else c * (1 + MAX_SL / 100)
            sl_pct = MAX_SL
        risk = abs(c - sl_used)
        score = 0.0
        score += 25 * min(max((vx - 1.5) / 2.5, 0), 1)
        score += 20 * max(0.0, 1 - dist / MAX_DIST)
        score += 15 * (0.6 * min(body / 0.7, 1) + 0.4 * max(0.0, 1 - wick / 0.4))
        score += 7.5 * trend
        score += 10 if room else 0
        score += {"NY": 10, "Asian": 5, "London": 0}[ses]
        score += 5 if sl_pct >= 1.0 else 0
        reasons = [f"{'PDH' if side == 'BUY' else 'PDL'} break +{dist:.2f}%", f"vol {vx:.1f}x",
                   f"body {body*100:.0f}%", f"trend {int(trend)}/2", "room OK" if room else "level near",
                   f"{ses}", f"SL {sl_pct:.2f}%"]
        decision = "SKIP" if fails else ("ENTRY" if score >= 60 else ("WEAK" if score >= 40 else "SKIP"))
        sgn = 1 if side == "BUY" else -1
        out.update(side=side, score=round(score), decision=decision, fails="; ".join(fails), reasons=", ".join(reasons),
                   entry=c, sl=sl_used, cap=c - sgn * min(1.5 * risk, 0.03 * c), t2=c + sgn * 2 * risk, t6=c + sgn * 6 * risk)
        return out

    # not broken: watchlist?
    near_buy = 0 <= (pdh - c) / c * 100 <= 0.3 and vx >= 1.2
    near_sell = 0 <= (c - pdl) / c * 100 <= 0.3 and vx >= 1.2
    if near_buy or near_sell:
        side = "BUY" if near_buy else "SELL"
        gap = (pdh - c) / c * 100 if near_buy else (c - pdl) / c * 100
        out.update(side=side, score=0, decision="WATCH", fails="", entry=c, sl=np.nan, cap=np.nan, t2=np.nan, t6=np.nan,
                   reasons=f"{gap:.2f}% from {'PDH' if near_buy else 'PDL'}, vol {vx:.1f}x - ready to break")
        return out
    return None


def main():
    liquid = rd.fetch_liquid_pairs()
    rows = []
    for p in liquid:
        try:
            df = rd.fetch_candles_15m(p["pair"])
            ok, _ = rd.candle_volume_ok(df)
            if not ok:
                continue
            a = analyse(p["pair"], df)
            if a:
                rows.append(a)
        except Exception as e:
            print(f"{p['pair']}: {e}")
        time.sleep(0.15)
    now = pd.Timestamp.now("Asia/Kolkata").strftime("%d %b %H:%M IST")
    print(f"\n=== CLAUDE PICKER  {now}  ({len(liquid)} liquid coins scanned) ===\n")
    if not rows:
        print("Abhi koi coin breakout / near-breakout par nahi hai. ENTRY: none. Next 15m candle ke baad phir chalao.")
        return
    x = pd.DataFrame(rows)
    order = {"ENTRY": 0, "WEAK": 1, "WATCH": 2, "SKIP": 3}
    x = x.sort_values(by=["decision", "score"], key=lambda s: s.map(order) if s.name == "decision" else -s)
    lines = []
    for r in x.head(TOP_N * 3).itertuples():
        if r.decision in ("ENTRY", "WEAK"):
            lines.append(f"[{r.decision} {r.score}] {r.side} {r.pair} @ {r.entry:.6g} | SL {r.sl:.6g} | cap {r.cap:.6g} | "
                         f"1:2 {r.t2:.6g} | 1:6 {r.t6:.6g}\n    why: {r.reasons}")
        elif r.decision == "WATCH":
            lines.append(f"[WATCH] {r.side} {r.pair} @ {r.entry:.6g} - {r.reasons}")
        else:
            lines.append(f"[SKIP {r.score}] {r.side} {r.pair} - {r.fails}")
    text = "\n".join(lines)
    print(text)
    n_entry = int((x["decision"] == "ENTRY").sum())
    print(f"\nENTRY: {n_entry} | WEAK: {int((x['decision'] == 'WEAK').sum())} | WATCH: {int((x['decision'] == 'WATCH').sum())} | "
          f"SKIP: {int((x['decision'] == 'SKIP').sum())}")
    if "--tg" in sys.argv and n_entry:
        top = x[x["decision"] == "ENTRY"].head(TOP_N)
        msg = "[claude_picker.py] ABHI ENTRY (no orders, decision only)\n" + "\n".join(
            f"{r.side} {r.pair} score {r.score} @ {r.entry:.6g} SL {r.sl:.6g} | {r.reasons}" for r in top.itertuples())
        rd.send_alert(msg)


if __name__ == "__main__":
    main()
