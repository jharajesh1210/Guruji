"""CoinDCX GOLD paper engine V2 (patched). NEVER sends real orders.

Changes vs GOLD_FINAL_PAPER.py
- Candles: CoinDCX public futures candlestick endpoint first, old endpoint as fallback.
- News: missing/expired GOLD_NEWS_WINDOWS.json no longer blocks forever; falls back to the
  approximate IST news slots (GOLD_NEWS_MODE=strict restores the old fail-closed behaviour).
- Market filters (funding/basis): GOLD_MARKET_MODE=off (default, paper) lets entries through and
  tags them UNVERIFIED; GOLD_MARKET_MODE=strict needs GOLD_MARKET_FILTERS.json like before.
- Cost filter: cost must be <= GOLD_MAX_COST_R (default 1.0 R) instead of 0.15 R, which could
  never pass with 0.05% fee + 0.02% slippage per side on a 5m gold stop.
- Setup A proxy allowed in paper mode (GOLD_ALLOW_PROXY_A=1, default). Set 0 to disable.
- Every run prints a gate / rejection summary so you can see exactly why there is no trade.
"""
import os, json, sys, math, csv, datetime as dt
from zoneinfo import ZoneInfo
import requests, pandas as pd, numpy as np

IST = ZoneInfo("Asia/Kolkata"); UTC = dt.timezone.utc
BASE = "https://api.coindcx.com/exchange/v1/derivatives/futures/data"
PUBLIC = "https://public.coindcx.com/market_data/candlesticks"
STATE = "GOLD_FINAL_PAPER_STATE.json"; HISTORY = "GOLD_FINAL_PAPER_TRADES.csv"

def envf(k, d): return float(os.getenv(k, str(d)))
CAPITAL = envf("GOLD_CAPITAL", 1000)
POSITION_CAP = envf("GOLD_POSITION_USDT", 500)
REQUESTED_LEVERAGE = envf("GOLD_REQUESTED_LEVERAGE", 30)
MAX_STRATEGY_LEVERAGE = 5.0
FEE = envf("GOLD_FEE_RATE", 0.0005)
SLIPPAGE = envf("GOLD_SLIPPAGE_RATE", 0.0002)
MAX_COST_R = envf("GOLD_MAX_COST_R", 1.0)
NEWS_MODE = os.getenv("GOLD_NEWS_MODE", "approx").lower()      # approx | strict
MARKET_MODE = os.getenv("GOLD_MARKET_MODE", "off").lower()     # off | strict
ALLOW_PROXY_A = os.getenv("GOLD_ALLOW_PROXY_A", "1") == "1"
NEWS_WINDOW_MIN = envf("GOLD_NEWS_WINDOW_MIN", 15)

REJ = {}
def rej(reason):
    REJ[reason] = REJ.get(reason, 0) + 1
    return None

def get(url, params=None):
    r = requests.get(url, params=params, timeout=25); r.raise_for_status(); return r.json()

def notify(s):
    print(s, flush=True)
    if os.getenv("TG_TOKEN") and os.getenv("TG_CHAT_ID"):
        try:
            r = requests.post("https://api.telegram.org/bot" + os.environ["TG_TOKEN"] + "/sendMessage",
                              data={"chat_id": os.environ["TG_CHAT_ID"], "text": s}, timeout=15)
            r.raise_for_status()
        except Exception as e:
            print("TELEGRAM ERROR", repr(e), flush=True)

def load():
    if os.path.exists(STATE):
        with open(STATE) as f: return json.load(f)
    return {"equity": CAPITAL, "peak": CAPITAL, "position": None, "trades_today": 0, "losses_today": 0,
            "date": "", "week": "", "week_equity": CAPITAL, "day_equity": CAPITAL,
            "last_exit_ts": None, "last_bar": None, "last_entry_bar": None}

def save(s):
    with open(STATE, "w") as f: json.dump(s, f, indent=2)

def logtrade(t):
    exists = os.path.exists(HISTORY)
    with open(HISTORY, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["time", "pair", "setup", "side", "entry", "exit", "qty", "reason", "pnl", "r"])
        if not exists: w.writeheader()
        w.writerow(t)

RES = {"5m": "5", "15m": "15", "1h": "60"}
def candles(pair, interval, days):
    end = dt.datetime.now(UTC); start = end - dt.timedelta(days=days)
    rows = None; errs = []
    try:
        x = get(PUBLIC, {"pair": pair, "from": int(start.timestamp()), "to": int(end.timestamp()),
                         "resolution": RES[interval], "pcode": "f"})
        rows = x.get("data") if isinstance(x, dict) else x
    except Exception as e:
        errs.append("public:" + repr(e))
    if not rows:
        try:
            x = get(BASE + "/candles", {"pair": pair, "interval": interval,
                                        "startTime": int(start.timestamp() * 1000), "endTime": int(end.timestamp() * 1000)})
            rows = x.get("data", []) if isinstance(x, dict) else x
        except Exception as e:
            errs.append("old:" + repr(e))
    if not isinstance(rows, list) or not rows:
        raise ValueError("No candles " + pair + " " + interval + " " + " | ".join(errs))
    d = pd.DataFrame(rows)
    for c in ["open", "high", "low", "close", "volume"]: d[c] = pd.to_numeric(d[c])
    t = "time" if "time" in d else "timestamp"
    nums = pd.to_numeric(d[t])
    d["ts"] = pd.to_datetime(nums, unit="ms" if nums.median() > 1e11 else "s", utc=True)
    d = d.sort_values("ts").drop_duplicates("ts")
    mins = {"5m": 5, "15m": 15, "1h": 60}[interval]
    d = d[d.ts + pd.Timedelta(minutes=mins) <= pd.Timestamp.now(tz="UTC")].reset_index(drop=True)
    if len(d) < 205: raise ValueError("Not enough closed " + interval + " candles: " + str(len(d)))
    return d

def ema(s, n): return s.ewm(span=n, adjust=False).mean()

def atr(d):
    p = d.close.shift()
    tr = pd.concat([d.high - d.low, (d.high - p).abs(), (d.low - p).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / 14, adjust=False).mean()

def adx(d):
    up = d.high.diff(); down = -d.low.diff(); a = atr(d).replace(0, np.nan)
    plus = 100 * up.where((up > down) & (up > 0), 0).ewm(alpha=1 / 14, adjust=False).mean() / a
    minus = 100 * down.where((down > up) & (down > 0), 0).ewm(alpha=1 / 14, adjust=False).mean() / a
    return (100 * (plus - minus).abs() / (plus + minus).replace(0, np.nan)).ewm(alpha=1 / 14, adjust=False).mean()

def vwap(d):
    grp = d.ts.dt.floor("D"); tp = (d.high + d.low + d.close) / 3   # 05:30 IST reset == 00:00 UTC
    return (tp * d.volume).groupby(grp).cumsum() / d.volume.groupby(grp).cumsum().replace(0, np.nan)

def session(now):
    if now.weekday() > 4: return None
    m = now.hour * 60 + now.minute
    if 13 * 60 + 30 <= m < 16 * 60 + 30: return "LONDON"
    if 18 * 60 + 30 <= m < 22 * 60 + 30: return "NEW_YORK"
    return None

# ---------------------------------------------------------------- news
def news_verified(now):
    """True = clear, False = blackout, None = calendar file missing/expired/unusable."""
    try:
        x = json.load(open("GOLD_NEWS_WINDOWS.json"))
        until = dt.datetime.fromisoformat(x["verified_until"].replace("Z", "+00:00"))
        n = now.astimezone(UTC)
        if until < n: return None
        for e in x["events"]:
            a = dt.datetime.fromisoformat(e["start"].replace("Z", "+00:00")) - dt.timedelta(minutes=30)
            b = dt.datetime.fromisoformat(e["end"].replace("Z", "+00:00")) + dt.timedelta(minutes=30)
            if a <= n <= b: return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return None

def source_news_window(now):
    """Approximate IST slots, NOT a real calendar. True = inside a blocked window."""
    local = now.astimezone(IST); minute = local.hour * 60 + local.minute
    for slot in (18 * 60 + 45, 19 * 60 + 15, 23 * 60 + 45, 30):
        if min(abs(minute - slot), 1440 - abs(minute - slot)) <= NEWS_WINDOW_MIN: return True
    return False

def news_ok(now):
    v = news_verified(now)
    if v is not None: return v
    if NEWS_MODE == "strict": return False
    return not source_news_window(now)

# ---------------------------------------------------------------- setups
def common_filters(d, b, h, x, direction, tag):
    if direction * (x.close - x.open) <= 0: return rej(tag + ":candle_dir")
    if direction * (x.close - ema(d.close, 200).iloc[-2]) <= 0: return rej(tag + ":5m_ema200")
    if direction * (b.close.iloc[-1] - ema(b.close, 200).iloc[-1]) <= 0: return rej(tag + ":15m_ema200")
    if direction * (b.close.iloc[-1] - b.open.iloc[-1]) <= 0: return rej(tag + ":15m_candle")
    if direction * (ema(h.close, 50).iloc[-1] - ema(h.close, 200).iloc[-1]) <= 0: return rej(tag + ":1h_trend")
    if direction * (x.close - vwap(d).iloc[-2]) <= 0: return rej(tag + ":vwap")
    if not adx(d).iloc[-2] > 20: return rej(tag + ":adx")
    return True

def build_signal(d, x, confirm, direction, atrval, setup, tag):
    entry = float(x.high if direction > 0 else x.low) * (1 + direction * SLIPPAGE)
    stop_candle = float(x.low if direction > 0 else x.high)
    stop_atr = entry - direction * 1.5 * atrval
    sl = max(stop_candle, stop_atr) if direction > 0 else min(stop_candle, stop_atr)
    risk = abs(entry - sl)
    if risk <= 0: return rej(tag + ":risk_zero")
    if risk > 1.5 * atrval: return rej(tag + ":risk_gt_1.5atr")
    cost_r = (2 * FEE + 2 * SLIPPAGE) * entry / risk
    if cost_r > MAX_COST_R: return rej(tag + ":cost_gt_%.2fR" % MAX_COST_R)
    return {"setup": setup, "side": "LONG" if direction > 0 else "SHORT", "direction": direction, "entry": entry,
            "stop": sl, "risk": risk, "bar": str(confirm.ts), "start": str(confirm.ts), "bars": 0, "level": 0,
            "cost_r": round(cost_r, 3)}

def setup_b(d, b, h):
    if len(d) < 205: return rej("B:not_enough_data")
    x = d.iloc[-2]; confirm = d.iloc[-1]
    mid = d.close.rolling(20).mean(); sd = d.close.rolling(20).std(ddof=0)
    upper = mid + 2 * sd; lower = mid - 2 * sd
    width = (upper - lower) / mid.replace(0, np.nan)
    a = atr(d); atrval = float(a.iloc[-2])
    squeeze = float(width.iloc[-3]) <= float(width.iloc[-103:-3].quantile(.2))
    kup = ema(d.close, 20) + 1.5 * a; klo = ema(d.close, 20) - 1.5 * a
    squeeze = squeeze or bool(upper.iloc[-3] < kup.iloc[-3] and lower.iloc[-3] > klo.iloc[-3])
    if not squeeze: return rej("B:no_squeeze")
    if abs(x.close - x.open) / max(x.high - x.low, 1e-12) < .6: return rej("B:body<60%")
    if x.volume <= 1.5 * float(d.volume.iloc[-22:-2].mean()): return rej("B:volume<1.5x")
    direction = 1 if x.close > upper.iloc[-2] and confirm.high > x.high else \
                -1 if x.close < lower.iloc[-2] and confirm.low < x.low else 0
    if not direction: return rej("B:no_breakout_confirm")
    if common_filters(d, b, h, x, direction, "B") is None: return None
    return build_signal(d, x, confirm, direction, atrval, "B", "B")

def setup_a_source_proxy(d, b, h):
    if len(d) < 290: return rej("A:not_enough_data")
    x = d.iloc[-2]; confirm = d.iloc[-1]
    if abs(x.close - x.open) / max(x.high - x.low, 1e-12) < .6: return rej("A:body<60%")
    if x.volume <= 1.5 * float(d.volume.iloc[-22:-2].mean()): return rej("A:volume<1.5x")
    vah = float(d.high.rolling(288).quantile(.7).iloc[-2]); val = float(d.low.rolling(288).quantile(.3).iloc[-2])
    direction = 1 if x.close > vah and confirm.high > x.high else -1 if x.close < val and confirm.low < x.low else 0
    if not direction: return rej("A:no_breakout_confirm")
    if common_filters(d, b, h, x, direction, "A") is None: return None
    return build_signal(d, x, confirm, direction, float(atr(d).iloc[-2]), "A_SOURCE_PROXY", "A")

def validated_market_filters(symbol, now):
    if MARKET_MODE != "strict": return True
    try:
        with open("GOLD_MARKET_FILTERS.json") as f: x = json.load(f)
        checked = dt.datetime.fromisoformat(x["checked_at"].replace("Z", "+00:00"))
        if checked.tzinfo is None or abs((now - checked).total_seconds()) > 300: return False
        if x.get("basis_verified") is not True or x.get("funding_verified") is not True: return False
        if x.get("basis_ok") is not True: return False
        return x.get("funding_ok", {}).get(symbol) is True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False

# ---------------------------------------------------------------- trade management
def paper_size(entry, risk, equity, sess):
    if min(entry, risk, equity) <= 0: return 0
    size = min(POSITION_CAP / entry, .01 * equity / risk, equity * MAX_STRATEGY_LEVERAGE / entry)
    return max(0, size * (.5 if sess == "LONDON" else 1))

def finish(s, p, price, reason, now):
    direction = p["direction"]; qty = p["qty"]; entry = p["entry"]
    exit_price = price * (1 - direction * SLIPPAGE)
    pnl = direction * (exit_price - entry) * qty - (exit_price + entry) * qty * FEE
    r = pnl / (p["risk"] * qty) if p["risk"] * qty else 0
    s["equity"] += pnl; s["peak"] = max(s["peak"], s["equity"])
    if pnl < 0: s["losses_today"] += 1
    s["last_exit_ts"] = now.isoformat()
    logtrade({"time": now.isoformat(), "pair": p["pair"], "setup": p["setup"], "side": p["side"], "entry": entry,
              "exit": exit_price, "qty": qty, "reason": reason, "pnl": round(pnl, 4), "r": round(r, 3)})
    s["position"] = None
    notify("GOLD PAPER EXIT " + p["pair"] + " " + p["side"] + " " + reason + " | P&L " + str(round(pnl, 2)) +
           " USDT | Equity " + str(round(s["equity"], 2)))

def manage(s, d, now):
    p = s.get("position")
    if not p: return
    local = now.astimezone(IST); m = local.hour * 60 + local.minute
    end = 16 * 60 + 30 if p["session"] == "LONDON" else 22 * 60 + 30
    price = float(d.close.iloc[-1]); direction = p["direction"]; r = p["risk"]
    if m >= end or local.weekday() > 4: return finish(s, p, price, "SESSION_EXIT", now)
    if not news_ok(now): return finish(s, p, price, "NEWS_SAFETY_EXIT", now)
    multiple = direction * (price - p["entry"]) / r
    if multiple >= 5: return finish(s, p, price, "5R_FINAL", now)
    level = min(4, max(0, math.floor(multiple + 1e-9)))
    if level > p["level"]:
        p["level"] = level
        p["stop"] = p["entry"] + direction * max(0, level - 1) * r
        notify("GOLD PAPER SL LOCK " + p["pair"] + " " + str(level) + "R | SL " + str(round(p["stop"], 4)))
    if direction * (price - p["stop"]) <= 0: return finish(s, p, price, "STOP_LOCK", now)
    p["bars"] += 1
    if p["bars"] >= 30 and p["level"] == 0: finish(s, p, price, "STALL_EXIT", now)

# ---------------------------------------------------------------- main
def main():
    now = dt.datetime.now(UTC); local = now.astimezone(IST)
    s = load()
    print(f"PAPER CONFIG: cap {POSITION_CAP:g} USDT, requested {REQUESTED_LEVERAGE:g}x, strategy max 5x | "
          f"news={NEWS_MODE} market={MARKET_MODE} maxCostR={MAX_COST_R:g} proxyA={int(ALLOW_PROXY_A)} | no real orders")
    day = local.date().isoformat(); week = str(local.isocalendar().year) + "-" + str(local.isocalendar().week)
    if s["date"] != day: s["date"] = day; s["trades_today"] = 0; s["losses_today"] = 0; s["day_equity"] = s["equity"]
    if s["week"] != week: s["week"] = week; s["week_equity"] = s["equity"]

    active = get(BASE + "/active_instruments", {"margin_currency_short_name[]": "USDT"})
    if isinstance(active, dict): active = active.get("data", [])
    pairs = set(active)
    target = {}
    for sym in ("XAU", "PAXG"):
        for p in pairs:
            if isinstance(p, str) and p.endswith("-" + sym + "_USDT"): target[sym] = p; break
    print("INSTRUMENTS:", target)

    if s.get("position"):
        p = s["position"]; d = candles(p["pair"], "5m", 5)
        if str(d.ts.iloc[-1]) != s.get("last_bar"):
            s["last_bar"] = str(d.ts.iloc[-1]); manage(s, d, now)
        save(s); return

    sess = session(local)
    if not sess: print("NO ENTRY: session closed (London 13:30-16:30 IST, NY 18:30-22:30 IST, Mon-Fri)"); save(s); return
    if not news_ok(now): print("NO ENTRY: news blackout / calendar unusable (mode=" + NEWS_MODE + ")"); save(s); return
    if (s["trades_today"] >= 3 or s["losses_today"] >= 2 or s["equity"] <= s.get("day_equity", CAPITAL) * .97
            or s["equity"] <= s["week_equity"] * .95 or s["equity"] <= s["peak"] * .9):
        print("NO ENTRY: risk guard"); save(s); return
    if s.get("last_exit_ts") and (now - dt.datetime.fromisoformat(s["last_exit_ts"])).total_seconds() < 25 * 60:
        print("NO ENTRY: cooldown"); save(s); return
    if not target:
        print("NO ENTRY: neither XAU nor PAXG USDT futures found in active_instruments"); save(s); return

    for symbol, pair in target.items():
        try:
            d = candles(pair, "5m", 5); b = candles(pair, "15m", 12); h = candles(pair, "1h", 40)
            bar = str(d.ts.iloc[-1])
            if s.get("last_entry_bar") == bar: continue
            before = dict(REJ)
            sig = setup_b(d, b, h)
            if not sig and ALLOW_PROXY_A: sig = setup_a_source_proxy(d, b, h)
            if not sig:
                top = sorted(((k, v - before.get(k, 0)) for k, v in REJ.items() if v - before.get(k, 0) > 0), key=lambda kv: -kv[1])
                print("NO SIGNAL", symbol, "| last close", float(d.close.iloc[-1]), "| rejected by:", top[:6]); continue
            if not validated_market_filters(symbol, now):
                print("CANDIDATE", symbol, sig["setup"], "BLOCKED: verified basis/funding snapshot missing"); continue
            qty = paper_size(sig["entry"], sig["risk"], s["equity"], sess)
            if qty <= 0: print("NO ENTRY", symbol, "qty=0"); continue
            sig.update({"pair": pair, "qty": qty, "session": sess})
            s["position"] = sig; s["trades_today"] += 1; s["last_entry_bar"] = bar
            tag = "" if MARKET_MODE == "strict" else " | funding/basis UNVERIFIED"
            notify("GOLD PAPER ENTRY " + symbol + " " + sig["side"] + " SETUP " + sig["setup"] + " | Entry " +
                   str(round(sig["entry"], 4)) + " | SL " + str(round(sig["stop"], 4)) + " | Notional " +
                   str(round(qty * sig["entry"], 2)) + " USDT | cost " + str(sig["cost_r"]) + "R" + tag + " | PAPER ONLY")
            break
        except Exception as e:
            print("NO ENTRY", symbol, repr(e))
    save(s)

if __name__ == "__main__":
    try: main()
    except Exception as e:
        print("GOLD PAPER ERROR:", repr(e), file=sys.stderr); sys.exit(1)
