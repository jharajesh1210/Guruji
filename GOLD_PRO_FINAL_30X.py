#!/usr/bin/env python3
"""
GOLD_SMC_FINAL_PAPER.py  -  4H Trend + SMC Liquidity Sweep + 5M Confirmation (PAPER ONLY)

  XAUUSDT  : 4H trend (both sides)  -> longs in 4H bull, shorts in 4H bear
  PAXGUSDT : 4H BEARISH trend only  -> shorts only

Logic
  1) 4H filter : EMA50 / EMA200 + close vs EMA50 (closed 4H candles only)
  2) SMC sweep : a 5M candle wicks beyond the latest swing high/low (liquidity grab)
                 and closes back inside the level
  3) 5M confirm: within SWEEP_LOOKBACK candles, a displacement candle closes beyond the
                 minor structure (CHoCH) in the trade direction
  4) Entry     : close of confirmation candle (paper)
     Stop      : beyond sweep extreme + 0.2 ATR
     TP1       : +1R  -> close 50%, stop to breakeven
     TP2       : +2.5R-> close rest
     Time stop : MAX_HOLD_BARS 5M candles

No broker orders are placed. Data source: CoinDCX public futures candles (no API key).
Only Python standard library is used.

Usage
  python GOLD_PRO_FINAL.py --backtest 365     # backtest last N days (CoinDCX data, needs internet)
  python GOLD_PRO_FINAL.py --check            # verify CoinDCX pair names first
  python GOLD_PRO_FINAL.py --live             # live paper trading loop
Optional Telegram alerts: set env TG_TOKEN and TG_CHAT_ID
"""
import argparse, csv, json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

# ----------------------------------------------------------------- CONFIG
SYMBOLS = {
    "XAUUSDT":  {"long": True,  "short": True},    # 4H trend, both sides
    "PAXGUSDT": {"long": False, "short": True},    # 4H bearish only
}
START_EQUITY   = float(os.getenv("START_EQUITY_USDT", "1000"))
MARGIN_PER_TRADE = float(os.getenv("CAPITAL_PER_TRADE_USDT", "100"))
LEVERAGE = float(os.getenv("LEVERAGE", "30"))
NOTIONAL_PER_TRADE = MARGIN_PER_TRADE * LEVERAGE
RISK_PCT       = 0.01        # retained as reference; sizing is fixed-notional below
FEE_RATE       = float(os.getenv("FEE_RATE_PER_SIDE", "0.0005"))
SLIPPAGE_RATE  = float(os.getenv("SLIPPAGE_PER_SIDE", "0.0002"))
EMA_FAST, EMA_SLOW = 50, 200
ATR_LEN        = 14
SWING_N        = 3           # pivot strength (bars each side)
PIVOT_SPAN     = 60          # how far back to look for the pivot
SWEEP_LOOKBACK = 12          # sweep must be within last N 5M candles
DISPLACE_ATR   = 0.8         # confirmation candle body >= 0.8 ATR
SL_BUFFER_ATR  = 0.2
MIN_RISK_ATR, MAX_RISK_ATR = 0.4, 3.0
TP1_R, TP1_FRAC, TP2_R = 1.0, 0.5, 2.5
MAX_HOLD_BARS  = 144         # 12 hours
COOLDOWN_BARS  = 6
STATE_FILE     = "paper_state.json"
TRADES_FILE    = "paper_trades.csv"
IV_MS = {"5m": 300_000, "1h": 3_600_000, "4h": 14_400_000}

# CoinDCX futures pair codes (run  --check  to list the exact names on your account/region)
PAIRS = {"XAUUSDT": "B-XAU_USDT", "PAXGUSDT": "B-PAXG_USDT"}
CDX_CANDLES = "https://public.coindcx.com/market_data/candlesticks"
CDX_INSTRUMENTS = "https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments"
CDX_RES = {"5m": "5", "1h": "60"}      # CoinDCX has no 4h -> built from 1h candles
CDX_CHUNK = 800                         # candles per request (API limit ~1000)

# ----------------------------------------------------------------- DATA (CoinDCX)
def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())

def _cdx_range(pair, interval, t_from_ms, t_to_ms):
    q = urllib.parse.urlencode({"pair": pair, "from": t_from_ms // 1000, "to": t_to_ms // 1000,
                                "resolution": CDX_RES[interval], "pcode": "f"})
    j = _get(CDX_CANDLES + "?" + q)
    rows = j.get("data", []) if isinstance(j, dict) else j
    out = []
    for x in rows:
        t = int(x["time"]); t = t * 1000 if t < 10**11 else t       # seconds -> ms if needed
        out.append(dict(t=t, o=float(x["open"]), h=float(x["high"]),
                        l=float(x["low"]), c=float(x["close"])))
    out.sort(key=lambda z: z["t"])
    return out

def _agg_4h(c1h):
    buckets = {}
    for x in c1h:
        b = x["t"] // IV_MS["4h"] * IV_MS["4h"]
        buckets.setdefault(b, []).append(x)
    out = []
    for b in sorted(buckets):
        g = buckets[b]
        out.append(dict(t=b, o=g[0]["o"], h=max(z["h"] for z in g),
                        l=min(z["l"] for z in g), c=g[-1]["c"]))
    return out

def klines(symbol, interval, limit=500, end=None):
    """last `limit` candles ending at `end` (ms). interval: '5m' or '4h'."""
    pair = PAIRS[symbol]
    end = end or int(time.time() * 1000)
    if interval == "4h":
        c1h = klines(symbol, "1h", limit * 4 + 8, end)
        return _agg_4h(c1h)[-limit:]
    step, out, to = IV_MS[interval], [], end
    try:
        while len(out) < limit:
            n = min(CDX_CHUNK, limit - len(out))
            part = _cdx_range(pair, interval, to - n * step, to)
            if not part: break
            out = part + out
            to = part[0]["t"] - 1
            if n < CDX_CHUNK: break
            time.sleep(0.1)
    except Exception as e:
        raise RuntimeError(f"CoinDCX klines failed for {pair}: {e}")
    return out[-limit:]

def klines_history(symbol, interval, count):
    out, end = [], None
    while len(out) < count:
        part = klines(symbol, interval, min(CDX_CHUNK, count - len(out)), end)
        if not part: break
        out = part + out
        end = part[0]["t"] - 1
        time.sleep(0.1)
    return out[-count:]

def check_pairs():
    ins = _get(CDX_INSTRUMENTS)
    hits = [i for i in ins if any(k in str(i).upper() for k in ("XAU", "PAXG", "GOLD"))]
    print("CoinDCX futures gold-like instruments:", hits or "none found")
    print("Current PAIRS mapping:", PAIRS)

# ----------------------------------------------------------------- INDICATORS
def ema_series(vals, n):
    k, e, out = 2 / (n + 1), None, []
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out

def atr(c, n=ATR_LEN):
    seg = c[-(n + 1):]
    trs = [max(seg[i]["h"] - seg[i]["l"],
               abs(seg[i]["h"] - seg[i - 1]["c"]),
               abs(seg[i]["l"] - seg[i - 1]["c"])) for i in range(1, len(seg))]
    return sum(trs) / len(trs)

def trend_series(c4):
    """list of (close_time_ms, trend) ; trend: +1 bull, -1 bear, 0 neutral"""
    cl = [x["c"] for x in c4]
    ef, es = ema_series(cl, EMA_FAST), ema_series(cl, EMA_SLOW)
    out = []
    for i, x in enumerate(c4):
        if i < EMA_SLOW + 5: tr = 0
        elif ef[i] > es[i] and x["c"] > ef[i]: tr = 1
        elif ef[i] < es[i] and x["c"] < ef[i]: tr = -1
        else: tr = 0
        out.append((x["t"] + IV_MS["4h"], tr))
    return out

# ----------------------------------------------------------------- SIGNAL
def last_pivot(c, end_idx, kind):
    """most recent confirmed swing high/low strictly before end_idx"""
    lo = max(SWING_N, end_idx - PIVOT_SPAN)
    for j in range(end_idx - SWING_N - 1, lo - 1, -1):
        win = c[j - SWING_N:j + SWING_N + 1]
        if kind == "h" and c[j]["h"] == max(x["h"] for x in win): return c[j]["h"]
        if kind == "l" and c[j]["l"] == min(x["l"] for x in win): return c[j]["l"]
    return None

def find_signal(c, trend, perm):
    n = len(c)
    if n < 100 or trend == 0: return None
    a = atr(c)
    last = c[-1]
    if a <= 0 or abs(last["c"] - last["o"]) < DISPLACE_ATR * a: return None
    want_short = trend == -1 and perm["short"] and last["c"] < last["o"]
    want_long  = trend == 1 and perm["long"] and last["c"] > last["o"]
    if not (want_short or want_long): return None

    for s in range(n - 2, max(n - 2 - SWEEP_LOOKBACK, 10), -1):
        if want_short:
            lvl = last_pivot(c, s, "h")
            if lvl is None or not (c[s]["h"] > lvl and c[s]["c"] < lvl): continue
            struct = min(x["l"] for x in c[s - 5:s + 1])
            if not last["c"] < struct: continue
            if any(x["c"] < struct for x in c[s + 1:n - 1]): continue   # already broke earlier
            sl = max(x["h"] for x in c[s:]) + SL_BUFFER_ATR * a
            risk = sl - last["c"]
            side = "SHORT"
        else:
            lvl = last_pivot(c, s, "l")
            if lvl is None or not (c[s]["l"] < lvl and c[s]["c"] > lvl): continue
            struct = max(x["h"] for x in c[s - 5:s + 1])
            if not last["c"] > struct: continue
            if any(x["c"] > struct for x in c[s + 1:n - 1]): continue
            sl = min(x["l"] for x in c[s:]) - SL_BUFFER_ATR * a
            risk = last["c"] - sl
            side = "LONG"
        if not (MIN_RISK_ATR * a <= risk <= MAX_RISK_ATR * a): return None
        d = 1 if side == "LONG" else -1
        e = last["c"]
        return dict(side=side, d=d, entry=e, sl=sl, risk=risk,
                    tp1=e + d * TP1_R * risk, tp2=e + d * TP2_R * risk,
                    t=last["t"], sweep_level=lvl)
    return None

# ----------------------------------------------------------------- PAPER ENGINE
class Paper:
    def __init__(self, symbol, equity=START_EQUITY):
        self.symbol, self.equity = symbol, equity
        self.pos, self.trades, self.cool_until = None, [], 0

    def open(self, sig, bar_idx):
        # Apply adverse entry slippage and size the position to 100 USDT notional.
        d = int(sig["d"])
        entry = float(sig["entry"]) * (1 + d * SLIPPAGE_RATE)
        risk = abs(entry - float(sig["sl"]))
        if risk <= 0 or entry <= 0:
            return None
        margin_used = min(MARGIN_PER_TRADE, self.equity)
        notional = margin_used * LEVERAGE
        qty = notional / entry
        risk_usd = qty * risk
        adjusted = dict(sig)
        adjusted.update(entry=entry, risk=risk,
                        tp1=entry + d * TP1_R * risk,
                        tp2=entry + d * TP2_R * risk,
                        qty=qty, notional=notional, margin_used=margin_used, leverage=LEVERAGE)
        self.pos = dict(adjusted, risk_usd=risk_usd, tp1_hit=False, realized_R=0.0,
                        qty_remaining=qty, realized_pnl_usd=0.0,
                        fees_paid=entry * qty * FEE_RATE,
                        bars=0, start_idx=bar_idx, cur_sl=sig["sl"])
        return self.pos

    def _close(self, bar, exit_price, rem_R, reason):
        p = self.pos
        # Adverse exit slippage; fee charged on entry and exit notional.
        exit_price = float(exit_price) * (1 - p["d"] * SLIPPAGE_RATE)
        exit_qty = float(p.get("qty_remaining", p["qty"]))
        gross_pnl = float(p.get("realized_pnl_usd", 0.0)) + (exit_price - p["entry"]) * p["d"] * exit_qty
        fee_usd = float(p.get("fees_paid", 0.0)) + exit_price * exit_qty * FEE_RATE
        pnl = gross_pnl - fee_usd
        total_R = pnl / p["risk_usd"] if p["risk_usd"] else 0.0
        self.equity += pnl
        tr = dict(symbol=self.symbol, side=p["side"],
                  open_time=datetime.fromtimestamp(p["t"] / 1000, timezone.utc).isoformat(timespec="minutes"),
                  close_time=datetime.fromtimestamp(bar["t"] / 1000, timezone.utc).isoformat(timespec="minutes"),
                  entry=round(p["entry"], 3), sl=round(p["sl"], 3), tp1=round(p["tp1"], 3),
                  tp2=round(p["tp2"], 3), exit=round(exit_price, 3), reason=reason,
                  notional_usdt=round(p["notional"], 2), margin_usdt=round(p["margin_used"], 2), leverage=p["leverage"], quantity=round(p["qty"], 10),
                  fee_rate_per_side=FEE_RATE, slippage_per_side=SLIPPAGE_RATE,
                  R=round(total_R, 3), pnl=round(pnl, 4), equity=round(self.equity, 2))
        self.trades.append(tr)
        self.pos = None
        return tr

    def on_bar(self, bar, bar_idx):
        """manage open position on a closed 5M bar. returns list of event strings / closed trade"""
        p = self.pos
        if not p: return None
        p["bars"] += 1
        d = p["d"]
        hit_sl = bar["l"] <= p["cur_sl"] if d == 1 else bar["h"] >= p["cur_sl"]
        hit_tp1 = bar["h"] >= p["tp1"] if d == 1 else bar["l"] <= p["tp1"]
        hit_tp2 = bar["h"] >= p["tp2"] if d == 1 else bar["l"] <= p["tp2"]
        rem = 1.0 if not p["tp1_hit"] else 1.0 - TP1_FRAC
        if hit_sl:                                   # conservative: SL before TP in same bar
            r_each = (p["cur_sl"] - p["entry"]) * d / p["risk"]
            self.cool_until = bar_idx + COOLDOWN_BARS
            return self._close(bar, p["cur_sl"], rem * r_each,
                               "SL" if not p["tp1_hit"] else "BE")
        if not p["tp1_hit"] and hit_tp1:
            p["tp1_hit"] = True
            partial_qty = min(p["qty"] * TP1_FRAC, p["qty_remaining"])
            partial_exit = float(p["tp1"]) * (1 - d * SLIPPAGE_RATE)
            p["realized_pnl_usd"] += (partial_exit - p["entry"]) * d * partial_qty
            p["fees_paid"] += partial_exit * partial_qty * FEE_RATE
            p["qty_remaining"] -= partial_qty
            p["realized_R"] = p["realized_pnl_usd"] / p["risk_usd"] if p["risk_usd"] else 0.0
            p["cur_sl"] = p["entry"]
            return "TP1 hit: 50% booked, SL -> breakeven"
        if p["tp1_hit"] and hit_tp2:
            self.cool_until = bar_idx + COOLDOWN_BARS
            return self._close(bar, p["tp2"], (1 - TP1_FRAC) * TP2_R, "TP2")
        if p["bars"] >= MAX_HOLD_BARS:
            r_each = (bar["c"] - p["entry"]) * d / p["risk"]
            self.cool_until = bar_idx + COOLDOWN_BARS
            return self._close(bar, bar["c"], rem * r_each, "TIME")
        return None

# ----------------------------------------------------------------- BACKTEST
def backtest(symbol, days, outdir=None):
    perm = SYMBOLS[symbol]
    c5 = klines_history(symbol, "5m", days * 288 + 300)
    c4 = klines_history(symbol, "4h", days * 6 + 260)
    if outdir:
        os.makedirs(outdir, exist_ok=True)
        pd = __import__("pandas")
        pd.DataFrame(c5).to_csv(os.path.join(outdir, f"{symbol}_5M_CANDLES.csv"), index=False)
        pd.DataFrame(c4).to_csv(os.path.join(outdir, f"{symbol}_4H_CANDLES.csv"), index=False)
    ts = trend_series(c4)
    eng, ti = Paper(symbol), -1
    for i in range(300, len(c5)):
        bar = c5[i]
        bar_close = bar["t"] + IV_MS["5m"]
        while ti + 1 < len(ts) and ts[ti + 1][0] <= bar_close: ti += 1
        trend = ts[ti][1] if ti >= 0 else 0
        if eng.pos:
            eng.on_bar(bar, i)
        elif i >= eng.cool_until:
            sig = find_signal(c5[i - 299:i + 1], trend, perm)
            if sig: eng.open(sig, i)
    return eng

def stats(eng):
    t = eng.trades
    if not t: return "no trades"
    wins = [x for x in t if x["R"] > 0]
    gp = sum(x["R"] for x in wins)
    gl = -sum(x["R"] for x in t if x["R"] <= 0)
    peak = dd = cum = 0.0
    for x in t:
        cum += x["R"]; peak = max(peak, cum); dd = max(dd, peak - cum)
    peak_eq = START_EQUITY
    max_dd_pct = 0.0
    for x in t:
        peak_eq = max(peak_eq, float(x["equity"]))
        if peak_eq > 0:
            max_dd_pct = max(max_dd_pct, (peak_eq - float(x["equity"])) / peak_eq * 100)
    net = eng.equity - START_EQUITY
    return (f"trades={len(t)}  win%={100*len(wins)/len(t):.1f}  net={net:+.2f} USDT  "
            f"totalR={sum(x['R'] for x in t):.2f}  PF={(gp/gl if gl else float('inf')):.2f}  "
            f"maxDD={max_dd_pct:.2f}%  equity {START_EQUITY:.0f} -> {eng.equity:.2f}")

def save_trades(trades):
    if not trades: return
    new = not os.path.exists(TRADES_FILE)
    with open(TRADES_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
        if new: w.writeheader()
        w.writerows(trades)

# ----------------------------------------------------------------- LIVE
def tg(msg):
    tok, chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT_ID")
    print(msg)
    if not (tok and chat): return
    try:
        _get(f"https://api.telegram.org/bot{tok}/sendMessage?" +
             urllib.parse.urlencode({"chat_id": chat, "text": msg}))
    except Exception as e:
        print("telegram error:", e)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f: return json.load(f)
    return {s: dict(equity=START_EQUITY, pos=None, last_t=0, cool_until_t=0) for s in SYMBOLS}

def save_state(st):
    with open(STATE_FILE, "w") as f: json.dump(st, f, indent=1)

def live():
    st = load_state()
    tg("Gold SMC paper bot started: " + ", ".join(SYMBOLS))
    while True:
        try:
            now = int(time.time() * 1000)
            for sym, perm in SYMBOLS.items():
                s = st.setdefault(sym, dict(equity=START_EQUITY, pos=None, last_t=0, cool_until_t=0))
                c5 = [x for x in klines(sym, "5m", 300) if x["t"] + IV_MS["5m"] <= now]
                if len(c5) < 120: continue
                newest = c5[-1]
                if newest["t"] <= s["last_t"]: continue
                eng = Paper(sym, s["equity"]); eng.pos = s["pos"]
                new_bars = [x for x in c5 if x["t"] > s["last_t"]] if s["last_t"] else []
                for b in new_bars:                      # manage open position
                    if eng.pos and b["t"] > eng.pos["t"]:
                        res = eng.on_bar(b, 10**9)
                        if isinstance(res, dict):
                            save_trades([res]); s["cool_until_t"] = b["t"] + COOLDOWN_BARS * IV_MS["5m"]
                            tg(f"{sym} CLOSED {res['side']} {res['reason']} R={res['R']} equity={res['equity']}")
                        elif res: tg(f"{sym} {res}")
                if not eng.pos and newest["t"] >= s["cool_until_t"]:
                    c4 = [x for x in klines(sym, "4h", 400) if x["t"] + IV_MS["4h"] <= now]
                    trend = trend_series(c4)[-1][1]
                    sig = find_signal(c5, trend, perm)
                    if sig:
                        eng.open(sig, 0)
                        tg(f"{sym} PAPER {sig['side']} entry={sig['entry']:.2f} SL={sig['sl']:.2f} "
                           f"TP1={sig['tp1']:.2f} TP2={sig['tp2']:.2f} (4H trend={trend})")
                s.update(equity=eng.equity, pos=eng.pos, last_t=newest["t"])
            save_state(st)
        except Exception as e:
            print("loop error:", e)
        time.sleep(20)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Gold XAUUSDT + PAXGUSDT SMC paper engine and 365d backtest")
    ap.add_argument("--backtest", type=int, metavar="DAYS")
    ap.add_argument("--live", action="store_true", help="paper-trading loop only; no live orders")
    ap.add_argument("--check", action="store_true", help="list CoinDCX gold pair names")
    ap.add_argument("--out", default="GOLD_BACKTEST_RESULTS")
    a = ap.parse_args()
    if a.check:
        check_pairs()
    elif a.backtest:
        if a.backtest < 30:
            ap.error("Use at least 30 days; recommended historical test is 365 days.")
        os.makedirs(a.out, exist_ok=True)
        summary = []
        for sym in SYMBOLS:
            e = backtest(sym, a.backtest, a.out)
            print(f"\\n{sym}: {stats(e)}")
            rows = e.trades
            if rows:
                pd = __import__("pandas")
                pd.DataFrame(rows).to_csv(os.path.join(a.out, f"{sym}_TRADES.csv"), index=False)
                pd.DataFrame([{"trade_number": i+1, "equity_usdt": tr["equity"],
                               "close_time": tr["close_time"], "net_pnl_usdt": tr["pnl"]}
                              for i, tr in enumerate(rows)]).to_csv(
                                  os.path.join(a.out, f"{sym}_EQUITY.csv"), index=False)
            wins = [x for x in rows if float(x["pnl"]) > 0]
            gross_profit = sum(float(x["pnl"]) for x in rows if float(x["pnl"]) > 0)
            gross_loss = -sum(float(x["pnl"]) for x in rows if float(x["pnl"]) < 0)
            peak = START_EQUITY
            maxdd = 0.0
            for tr in rows:
                peak = max(peak, float(tr["equity"]))
                if peak:
                    maxdd = max(maxdd, (peak-float(tr["equity"]))/peak*100)
            summary.append({
                "symbol": sym, "days_requested": a.backtest, "trades": len(rows),
                "win_rate_pct": round(100*len(wins)/len(rows), 2) if rows else 0,
                "net_profit_usdt": round(e.equity-START_EQUITY, 4),
                "ending_equity_usdt": round(e.equity, 4),
                "max_drawdown_pct": round(maxdd, 3),
                "profit_factor": round(gross_profit/gross_loss, 3) if gross_loss else ("inf" if gross_profit else 0),
                "margin_per_trade_usdt": MARGIN_PER_TRADE, "leverage": LEVERAGE,
                "notional_per_trade_usdt": NOTIONAL_PER_TRADE,
                "fee_per_side": FEE_RATE, "slippage_per_side": SLIPPAGE_RATE,
                "direction_permissions": SYMBOLS[sym]
            })
        with open(os.path.join(a.out, "GOLD_BACKTEST_SUMMARY.json"), "w") as f:
            json.dump(summary, f, indent=2)
        with open(os.path.join(a.out, "GOLD_BACKTEST_SUMMARY.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(summary[0].keys()))
            w.writeheader(); w.writerows(summary)
        print("\\nSUMMARY")
        print(json.dumps(summary, indent=2))
        save_trades([tr for sym in SYMBOLS for tr in []])
    elif a.live:
        print("SAFETY: --live starts PAPER trading only. This program contains no real-order placement.")
        live()
    else:
        ap.print_help()