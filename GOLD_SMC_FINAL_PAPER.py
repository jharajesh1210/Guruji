#!/usr/bin/env python3
"""GOLD SMC paper bot: 4H EMA trend + 5M liquidity sweep/CHoCH confirmation.
CoinDCX public futures candles only. No broker orders are ever submitted.
"""
import argparse, csv, json, os, time, urllib.parse, urllib.request
from datetime import datetime, timezone

SYMBOLS = {"XAUUSDT": {"long": True, "short": True},
           "PAXGUSDT": {"long": False, "short": True}}
START_EQUITY = 1000.0
RISK_PCT = 0.005
FEE_RATE = 0.0005
EMA_FAST, EMA_SLOW = 50, 200
ATR_LEN, SWING_N, PIVOT_SPAN = 14, 3, 60
SWEEP_LOOKBACK, DISPLACE_ATR, SL_BUFFER_ATR = 12, 0.8, 0.2
MIN_RISK_ATR, MAX_RISK_ATR = 0.4, 3.0
TP1_R, TP1_FRAC, TP2_R = 1.0, 0.5, 2.5
MAX_HOLD_BARS, COOLDOWN_BARS = 144, 6
STATE_FILE, TRADES_FILE = "paper_state.json", "paper_trades.csv"
IV_MS = {"5m": 300000, "1h": 3600000, "4h": 14400000}
PAIRS = {"XAUUSDT": "B-XAU_USDT", "PAXGUSDT": "B-PAXG_USDT"}
CDX_CANDLES = "https://public.coindcx.com/market_data/candlesticks"
CDX_INSTRUMENTS = "https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments"
CDX_RES, CDX_CHUNK = {"5m": "5", "1h": "60"}, 800

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())

def cdx_range(pair, interval, start_ms, end_ms):
    q = urllib.parse.urlencode({"pair": pair, "from": start_ms // 1000,
        "to": end_ms // 1000, "resolution": CDX_RES[interval], "pcode": "f"})
    data = get_json(CDX_CANDLES + "?" + q)
    rows = data.get("data", []) if isinstance(data, dict) else data
    out = []
    for x in rows:
        t = int(x["time"])
        if t < 10**11: t *= 1000
        out.append({"t": t, "o": float(x["open"]), "h": float(x["high"]),
                    "l": float(x["low"]), "c": float(x["close"])})
    return sorted(out, key=lambda x: x["t"])

def aggregate_4h(c1h):
    buckets = {}
    for x in c1h:
        b = x["t"] // IV_MS["4h"] * IV_MS["4h"]
        buckets.setdefault(b, []).append(x)
    out = []
    for b, g in sorted(buckets.items()):
        out.append({"t": b, "o": g[0]["o"], "h": max(x["h"] for x in g),
                    "l": min(x["l"] for x in g), "c": g[-1]["c"]})
    return out

def klines(symbol, interval, limit=500, end=None):
    end = end or int(time.time() * 1000)
    if interval == "4h":
        return aggregate_4h(klines(symbol, "1h", limit * 4 + 8, end))[-limit:]
    pair, step, out, to = PAIRS[symbol], IV_MS[interval], [], end
    while len(out) < limit:
        n = min(CDX_CHUNK, limit - len(out))
        part = cdx_range(pair, interval, to - n * step, to)
        if not part: break
        out = part + out
        to = part[0]["t"] - 1
        if n < CDX_CHUNK: break
        time.sleep(0.1)
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
    data = get_json(CDX_INSTRUMENTS)
    hits = [x for x in data if any(k in str(x).upper() for k in ("XAU", "PAXG", "GOLD"))]
    print("CoinDCX gold instruments:", hits or "none found")
    print("Configured pair codes:", PAIRS)

def ema_series(values, n):
    k, e, out = 2 / (n + 1), None, []
    for v in values:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out

def atr(c, n=ATR_LEN):
    seg = c[-(n + 1):]
    trs = [max(seg[i]["h"] - seg[i]["l"],
        abs(seg[i]["h"] - seg[i-1]["c"]), abs(seg[i]["l"] - seg[i-1]["c"]))
        for i in range(1, len(seg))]
    return sum(trs) / len(trs)

def trend_series(c4):
    closes = [x["c"] for x in c4]
    fast, slow = ema_series(closes, EMA_FAST), ema_series(closes, EMA_SLOW)
    out = []
    for i, x in enumerate(c4):
        tr = 0
        if i >= EMA_SLOW + 5:
            if fast[i] > slow[i] and x["c"] > fast[i]: tr = 1
            elif fast[i] < slow[i] and x["c"] < fast[i]: tr = -1
        out.append((x["t"] + IV_MS["4h"], tr))
    return out

def last_pivot(c, end_idx, kind):
    lo = max(SWING_N, end_idx - PIVOT_SPAN)
    for j in range(end_idx - SWING_N - 1, lo - 1, -1):
        win = c[j-SWING_N:j+SWING_N+1]
        if kind == "h" and c[j]["h"] == max(x["h"] for x in win): return c[j]["h"]
        if kind == "l" and c[j]["l"] == min(x["l"] for x in win): return c[j]["l"]
    return None

def find_signal(c, trend, perm):
    n = len(c)
    if n < 100 or trend == 0: return None
    a, last = atr(c), c[-1]
    if a <= 0 or abs(last["c"] - last["o"]) < DISPLACE_ATR * a: return None
    short = trend == -1 and perm["short"] and last["c"] < last["o"]
    long = trend == 1 and perm["long"] and last["c"] > last["o"]
    if not (short or long): return None
    for s in range(n - 2, max(n - 2 - SWEEP_LOOKBACK, 10), -1):
        if short:
            lvl = last_pivot(c, s, "h")
            if lvl is None or not (c[s]["h"] > lvl and c[s]["c"] < lvl): continue
            struct = min(x["l"] for x in c[s-5:s+1])
            if last["c"] >= struct or any(x["c"] < struct for x in c[s+1:n-1]): continue
            sl, side = max(x["h"] for x in c[s:]) + SL_BUFFER_ATR*a, "SHORT"
            risk = sl - last["c"]
        else:
            lvl = last_pivot(c, s, "l")
            if lvl is None or not (c[s]["l"] < lvl and c[s]["c"] > lvl): continue
            struct = max(x["h"] for x in c[s-5:s+1])
            if last["c"] <= struct or any(x["c"] > struct for x in c[s+1:n-1]): continue
            sl, side = min(x["l"] for x in c[s:]) - SL_BUFFER_ATR*a, "LONG"
            risk = last["c"] - sl
        if not MIN_RISK_ATR*a <= risk <= MAX_RISK_ATR*a: return None
        d, entry = (1 if side == "LONG" else -1), last["c"]
        return {"side": side, "d": d, "entry": entry, "sl": sl, "risk": risk,
                "tp1": entry + d*TP1_R*risk, "tp2": entry + d*TP2_R*risk,
                "t": last["t"], "sweep_level": lvl}
    return None

class Paper:
    def __init__(self, symbol, equity=START_EQUITY):
        self.symbol, self.equity = symbol, equity
        self.pos, self.trades, self.cool_until = None, [], 0
    def open(self, sig, bar_idx):
        self.pos = dict(sig, risk_usd=self.equity*RISK_PCT, tp1_hit=False,
            realized_R=0.0, bars=0, start_idx=bar_idx, cur_sl=sig["sl"])
        return self.pos
    def close(self, bar, exit_price, rem_R, reason):
        p = self.pos
        fee_R = (p["entry"]*2*FEE_RATE)/p["risk"]
        total_R = p["realized_R"] + rem_R - fee_R
        pnl = total_R*p["risk_usd"]
        self.equity += pnl
        tr = {"symbol": self.symbol, "side": p["side"],
            "open_time": datetime.fromtimestamp(p["t"]/1000, timezone.utc).isoformat(timespec="minutes"),
            "close_time": datetime.fromtimestamp(bar["t"]/1000, timezone.utc).isoformat(timespec="minutes"),
            "entry": round(p["entry"],3), "sl": round(p["sl"],3),
            "tp1": round(p["tp1"],3), "tp2": round(p["tp2"],3),
            "exit": round(exit_price,3), "reason": reason, "R": round(total_R,3),
            "pnl": round(pnl,2), "equity": round(self.equity,2)}
        self.trades.append(tr); self.pos = None
        return tr
    def on_bar(self, bar, idx):
        p = self.pos
        if not p: return None
        p["bars"] += 1; d = p["d"]
        hit_sl = bar["l"] <= p["cur_sl"] if d == 1 else bar["h"] >= p["cur_sl"]
        hit_tp1 = bar["h"] >= p["tp1"] if d == 1 else bar["l"] <= p["tp1"]
        hit_tp2 = bar["h"] >= p["tp2"] if d == 1 else bar["l"] <= p["tp2"]
        rem = 1.0 if not p["tp1_hit"] else 1.0 - TP1_FRAC
        if hit_sl:
            r = (p["cur_sl"]-p["entry"])*d/p["risk"]
            self.cool_until = idx + COOLDOWN_BARS
            return self.close(bar, p["cur_sl"], rem*r, "SL" if not p["tp1_hit"] else "BE")
        if not p["tp1_hit"] and hit_tp1:
            p["tp1_hit"] = True; p["realized_R"] += TP1_FRAC*TP1_R
            p["cur_sl"] = p["entry"]
            return "TP1 hit: 50% booked, SL -> breakeven"
        if p["tp1_hit"] and hit_tp2:
            self.cool_until = idx + COOLDOWN_BARS
            return self.close(bar, p["tp2"], (1-TP1_FRAC)*TP2_R, "TP2")
        if p["bars"] >= MAX_HOLD_BARS:
            r = (bar["c"]-p["entry"])*d/p["risk"]
            self.cool_until = idx + COOLDOWN_BARS
            return self.close(bar, bar["c"], rem*r, "TIME")
        return None

def backtest(symbol, days):
    c5 = klines_history(symbol, "5m", days*288+300)
    c4 = klines_history(symbol, "4h", days*6+260)
    ts, eng, ti = trend_series(c4), Paper(symbol), -1
    for i in range(300, len(c5)):
        bar, close_t = c5[i], c5[i]["t"] + IV_MS["5m"]
        while ti+1 < len(ts) and ts[ti+1][0] <= close_t: ti += 1
        trend = ts[ti][1] if ti >= 0 else 0
        if eng.pos: eng.on_bar(bar, i)
        elif i >= eng.cool_until:
            sig = find_signal(c5[i-299:i+1], trend, SYMBOLS[symbol])
            if sig: eng.open(sig, i)
    return eng

def stats(eng):
    t = eng.trades
    if not t: return "no trades"
    wins = [x for x in t if x["R"] > 0]
    gp, gl = sum(x["R"] for x in wins), -sum(x["R"] for x in t if x["R"] <= 0)
    peak = dd = cum = 0.0
    for x in t:
        cum += x["R"]; peak = max(peak, cum); dd = max(dd, peak-cum)
    pf = gp/gl if gl else float("inf")
    return (f"trades={len(t)} win%={100*len(wins)/len(t):.1f} totalR={sum(x['R'] for x in t):.2f} "
            f"PF={pf:.2f} maxDD(R)={dd:.2f} equity {START_EQUITY:.0f} -> {eng.equity:.2f}")

def save_trades(trades):
    if not trades: return
    new = not os.path.exists(TRADES_FILE)
    with open(TRADES_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
        if new: w.writeheader()
        w.writerows(trades)

def tg(msg):
    token, chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT_ID")
    print(msg)
    if not (token and chat): return
    try:
        get_json("https://api.telegram.org/bot"+token+"/sendMessage?"+
                 urllib.parse.urlencode({"chat_id": chat, "text": msg}))
    except Exception as e: print("telegram error:", e)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f: return json.load(f)
    return {s: {"equity": START_EQUITY, "pos": None, "last_t": 0, "cool_until_t": 0} for s in SYMBOLS}

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=1)

def live():
    state = load_state()
    tg("GOLD SMC PAPER bot started: " + ", ".join(SYMBOLS))
    while True:
        try:
            now = int(time.time()*1000)
            for sym, perm in SYMBOLS.items():
                s = state.setdefault(sym, {"equity": START_EQUITY, "pos": None, "last_t": 0, "cool_until_t": 0})
                c5 = [x for x in klines(sym, "5m", 300) if x["t"]+IV_MS["5m"] <= now]
                if len(c5) < 120: continue
                newest = c5[-1]
                if newest["t"] <= s["last_t"]: continue
                eng = Paper(sym, s["equity"]); eng.pos = s["pos"]
                new_bars = [x for x in c5 if x["t"] > s["last_t"]] if s["last_t"] else []
                for b in new_bars:
                    if eng.pos and b["t"] > eng.pos["t"]:
                        res = eng.on_bar(b, 10**9)
                        if isinstance(res, dict):
                            save_trades([res]); s["cool_until_t"] = b["t"]+COOLDOWN_BARS*IV_MS["5m"]
                            tg(f"{sym} CLOSED {res['side']} {res['reason']} R={res['R']} equity={res['equity']}")
                        elif res: tg(f"{sym} {res}")
                if not eng.pos and newest["t"] >= s["cool_until_t"]:
                    c4 = [x for x in klines(sym, "4h", 400) if x["t"]+IV_MS["4h"] <= now]
                    trend = trend_series(c4)[-1][1]
                    sig = find_signal(c5, trend, perm)
                    if sig:
                        eng.open(sig, 0)
                        action = "BUY" if sig["side"] == "LONG" else "SELL"
                        tg(f"{sym} PAPER {action} entry={sig['entry']:.2f} SL={sig['sl']:.2f} "
                           f"TP1={sig['tp1']:.2f} TP2={sig['tp2']:.2f} (4H trend={trend})")
                s.update(equity=eng.equity, pos=eng.pos, last_t=newest["t"])
            save_state(state)
        except Exception as e:
            print("loop error:", e)
        time.sleep(20)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", type=int, metavar="DAYS")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.check: check_pairs()
    elif args.backtest:
        for sym in SYMBOLS:
            e = backtest(sym, args.backtest)
            print(f"\n{sym}: {stats(e)}")
            for trade in e.trades[-5:]: print("  ", trade)
            save_trades(e.trades)
    elif args.live: live()
    else: ap.print_help()
