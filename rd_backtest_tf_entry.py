"""
Entry timeframe test: PDH/PDL break confirmed on a 1m / 2m / 5m / 10m / 15m candle CLOSE.

Same everything else (v2 rules), so only the entry candle size changes:
  signal  : high > PDH and close > PDH (SELL mirror) on that timeframe's closed candle,
            close at most 0.5% beyond the level, volume > 1.5 x avg of the last 20 candles of that timeframe,
            wick against < 40%, SL = that candle's low/high (min 0.75%, max 2%)
  fill    : open of the next 1m candle
  manage  : on 1m candles - loss cap 1.5R / 3% (stop), SL + 15m MACD confirm at 15m closes,
            lock 1:2/1:3/1:4/1:5 the minute price touches it (stop there), full exit 1:6, max hold 3 days
  costs   : 0.118% round trip, 25 USDT per trade, one trade per coin at a time (no 5-trade portfolio cap)
Data: CoinDCX 1m candles, last BT_DAYS days, all coins with 24h volume >= 10M USDT.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import time as dtime

import numpy as np
import pandas as pd
import requests

import rd_trading as rd

DAYS = int(os.getenv("BT_DAYS", "30"))
OUT = os.getenv("BT_OUT", "backtest_out")
WORKERS = int(os.getenv("BT_WORKERS", "8"))
TFS = [1, 2, 5, 10, 15]
FEE, SIZE = 0.00118, 25.0
MAX_DIST, MIN_SL, MAX_SL = 0.005, 0.0075, 0.02
CAP_R, CAP_PCT = 1.5, 0.03
LOCKS, TARGET = (2.0, 3.0, 4.0, 5.0), 6.0
MAX_HOLD_MIN = 3 * 24 * 60
MINSLS = [0.0075, 0.003]   # current 0.75%, and 0.3% (small candles have small SL)


def fetch_1m(pair):
    now = int(time.time())
    beg0 = now - (DAYS + 2) * 86400
    frames, end = [], now
    while end > beg0:
        beg = max(beg0, end - 12 * 3600)
        for attempt in range(3):
            try:
                r = requests.get(rd.CANDLE_URL, params={"pair": pair, "from": beg, "to": end, "resolution": "1",
                                                        "pcode": "f"}, headers={"User-Agent": "RD-BT/1.0"}, timeout=25)
                r.raise_for_status()
                j = r.json()
                raw = (j.get("data", j.get("candles", [])) if isinstance(j, dict) else j) or []
                if raw:
                    frames.append(pd.DataFrame(raw))
                break
            except Exception:
                time.sleep(1 + attempt)
        end = beg
    if not frames:
        return pair, None
    d = pd.concat(frames, ignore_index=True)
    need = ["time", "open", "high", "low", "close", "volume"]
    if not set(need).issubset(d.columns):
        return pair, None
    for c in need:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=need).drop_duplicates("time").sort_values("time")
    d["ts"] = pd.to_datetime(d["time"], unit="ms", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    d = d.set_index("ts")[["open", "high", "low", "close", "volume"]]
    return pair, d if len(d) > 1440 * 5 else None


def trading_day(idx):
    t = pd.Series(idx)
    late = t.dt.time < dtime(5, 30)
    return (t - pd.to_timedelta(late.astype(int), unit="D")).dt.date.values


def simulate_pair(m1, MIN_SL=MIN_SL):
    # PDH/PDL per trading day from 1m (need >= 1300 minutes in the previous day)
    day = trading_day(m1.index)
    g = pd.DataFrame({"d": day, "h": m1["high"].values, "l": m1["low"].values}).groupby("d").agg(H=("h", "max"), L=("l", "min"), N=("h", "size"))
    prev = {}
    days = list(g.index)
    for k in range(1, len(days)):
        if (days[k] - days[k - 1]).days == 1 and g.iloc[k - 1]["N"] >= 1300:
            prev[days[k]] = (g.iloc[k - 1]["H"], g.iloc[k - 1]["L"])
    # 15m MACD for the loss exit
    c15 = m1["close"].resample("15min").last().dropna()
    e1, e2 = c15.ewm(span=12, adjust=False).mean(), c15.ewm(span=26, adjust=False).mean()
    macd = e1 - e2
    sig = macd.ewm(span=9, adjust=False).mean()
    lo15 = m1["low"].resample("15min").min()
    hi15 = m1["high"].resample("15min").max()
    mt = m1.index.values
    q_idx = m1.index.floor("15min")
    MACDm, SIGm = macd.reindex(q_idx).values, sig.reindex(q_idx).values
    LO15m, HI15m = lo15.reindex(q_idx).values, hi15.reindex(q_idx).values
    END15 = (m1.index.minute.values % 15) == 14
    O, H, L, C = (m1[c].values for c in ("open", "high", "low", "close"))
    pos = {t: i for i, t in enumerate(mt)}
    res = {}
    for tf in TFS:
        b = m1.resample(f"{tf}min").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
        b["vma"] = b["volume"].shift(1).rolling(20, min_periods=10).mean()
        bd = trading_day(b.index)
        pdh = np.array([prev.get(d, (np.nan, np.nan))[0] for d in bd])
        pdl = np.array([prev.get(d, (np.nan, np.nan))[1] for d in bd])
        bo, bh, bl, bc, bv, bm = (b[c].values for c in ("open", "high", "low", "close", "volume", "vma"))
        rng = np.where(bh > bl, bh - bl, np.nan)
        upw = (bh - np.maximum(bo, bc)) / rng
        dnw = (np.minimum(bo, bc) - bl) / rng
        hv = bv > 1.5 * bm
        buy = (bh > pdh) & (bc > pdh) & hv & (upw < 0.4) & ((bc - pdh) <= bc * MAX_DIST)
        sell = (bl < pdl) & (bc < pdl) & hv & (dnw < 0.4) & ((pdl - bc) <= bc * MAX_DIST)
        trades = []
        free_from = np.datetime64("1970-01-01")
        bt = b.index.values
        for k in np.where(buy | sell)[0]:
            close_t = bt[k] + np.timedelta64(tf, "m")
            if close_t < free_from:
                continue
            j = pos.get(close_t)
            if j is None:
                continue
            d = 1 if buy[k] else -1
            E = O[j]
            sl = bl[k] if d == 1 else bh[k]
            R = d * (E - sl)
            if R <= 0 or R < E * MIN_SL:
                continue
            if R > E * MAX_SL:
                R = E * MAX_SL
                sl = E - d * R
            cap = E - d * min(CAP_R * R, CAP_PCT * E)
            locked, stop = 0.0, None
            ex = why = None
            i = j
            while i < len(mt) and i - j < MAX_HOLD_MIN:
                if stop is not None and ((L[i] <= stop) if d == 1 else (H[i] >= stop)):
                    ex, why = (min(stop, O[i]) if d == 1 else max(stop, O[i])), "lock"
                    break
                if locked == 0 and ((L[i] <= cap) if d == 1 else (H[i] >= cap)):
                    ex, why = (min(cap, O[i]) if d == 1 else max(cap, O[i])), "loss_cap"
                    break
                best = d * ((H[i] if d == 1 else L[i]) - E) / R
                if best >= TARGET:
                    ex, why = E + d * TARGET * R, "target"
                    break
                lv = max([x for x in LOCKS if best >= x], default=0.0)
                if lv > locked:
                    locked, stop = lv, E + d * lv * R
                    if (C[i] <= stop) if d == 1 else (C[i] >= stop):
                        ex, why = C[i], "lock"
                        break
                if locked == 0 and END15[i] and MACDm[i] == MACDm[i]:
                    touched = (LO15m[i] <= sl) if d == 1 else (HI15m[i] >= sl)
                    ok = (MACDm[i] < SIGm[i]) if d == 1 else (MACDm[i] > SIGm[i])
                    if touched and ok:
                        ex, why = (min(sl, C[i]) if d == 1 else max(sl, C[i])), "macd_loss"
                        break
                i += 1
            if ex is None:
                ex, why = C[min(i, len(mt) - 1)], "max_hold"
            gross = d * (ex - E)
            trades.append({"t": pd.Timestamp(mt[j]), "R": gross / R - FEE * E / R,
                           "usdt": SIZE * gross / E - SIZE * FEE, "exit": why})
            free_from = mt[min(i, len(mt) - 1)] + np.timedelta64(1, "m")
        res[tf] = trades
    return res


def main():
    os.makedirs(OUT, exist_ok=True)
    pairs = [p["pair"] for p in rd.fetch_liquid_pairs()]
    print(f"coins: {len(pairs)}", flush=True)
    allres = {(tf, ms): [] for tf in TFS for ms in MINSLS}
    used, span = 0, None
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for k, (pair, m1) in enumerate(ex.map(fetch_1m, pairs), 1):
            if m1 is not None:
                try:
                    for ms in MINSLS:
                        r = simulate_pair(m1, ms)
                        for tf in TFS:
                            allres[(tf, ms)] += r[tf]
                    used += 1
                    span = span or f"{m1.index[0]} -> {m1.index[-1]}"
                except Exception as e:
                    print(pair, e, flush=True)
            if k % 10 == 0:
                print(f"  {k}/{len(pairs)} coins done", flush=True)
    lines = [f"Entry timeframe test (v2 rules), {used} coins, 1m data {span}, 25 USDT/trade, fees 0.118%, one trade per coin.", "",
             "| Entry candle | Min SL | Trades | Win % | Net R | Net USDT @25 | PF | Max DD | 1st half | 2nd half | exits |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for ms in MINSLS:
      for tf in TFS:
        x = pd.DataFrame(allres[(tf, ms)])
        if x.empty:
            lines.append(f"| {tf}m | {ms*100:.2f}% | 0 | - | - | - | - | - | - | - | - |")
            continue
        x = x.sort_values("t")
        w, l = x.usdt[x.usdt > 0], x.usdt[x.usdt < 0]
        cum = x.usdt.cumsum()
        h = len(x) // 2
        ex = ", ".join(f"{k}: {v}" for k, v in x["exit"].value_counts().items())
        lines.append(f"| {tf}m{' (now)' if (tf == 15 and ms == 0.0075) else ''} | {ms*100:.2f}% | {len(x)} | {100 * len(w) / len(x):.1f} | {x.R.sum():.1f} | "
                     f"{x.usdt.sum():.2f} | {w.sum() / -l.sum() if len(l) else 99:.2f} | {(cum - cum.cummax()).min():.2f} | "
                     f"{x.usdt.iloc[:h].sum():.2f} | {x.usdt.iloc[h:].sum():.2f} | {ex} |")
    text = "\n".join(lines) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print("\n=== RESULT ===\n" + text, flush=True)


if __name__ == "__main__":
    main()
