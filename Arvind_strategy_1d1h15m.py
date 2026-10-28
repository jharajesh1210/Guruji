"""
1D trend > 1H zone > 15M entry  (BTC / PAXG)  -- BACKTEST ONLY, EDUCATIONAL PURPOSE ONLY.
Results are GROSS (no fees/slippage). Not financial advice.

Usage:
    python strategy_1d1h15m.py BTC path/to/BTCUSDT_15m.csv
    python strategy_1d1h15m.py PAXG path/to/PAXGUSDT_15m.csv
CSV needs columns: time (ms epoch), open, high, low, close.

Rules
 1. Daily trend : previous completed daily close vs EMA20 -> only trade that side.
 2. 1H zone     : 1H pivot low/high (2 bars each side). Zone width = 0.5*ATR14(1H). Valid 5 days.
 3. 15M trigger : price touches zone, then within 16 bars a candle closes beyond the previous 3 bars'
                  high (BUY) / low (SELL). Entry at next candle open.
 4. Stop        : far edge of zone. Target 2R. Time exit 12h. One position at a time.
                  Skip if stop distance <0.15% or >4%.
 5. Filters     : F1 stop distance >= MIN_STOP_PCT, F2 zone age <= 48h, F3 daily trend strength >= MIN_TREND_PCT
                  (thresholds were learned on the first 60% of data).
"""
import sys
import numpy as np, pandas as pd

OHLC = {"open": "first", "high": "max", "low": "min", "close": "last"}
RR, HOLD_H, TRIG_H, ZONE_DAYS = 2.0, 12, 4, 5
MIN_DIST, MAX_DIST = 0.0015, 0.04
MAX_ZONE_AGE_H = 48
FILTERS = {"BTC": dict(min_stop=0.46, min_trend=1.15),
           "PAXG": dict(min_stop=0.23, min_trend=0.57)}   # percent
NOTIONAL_INR = 3000 * 30                                   # Rs.3000 x 30x


def load(path):
    d = pd.read_csv(path)
    d["time"] = pd.to_datetime(d["time"], unit="ms")
    return d.sort_values("time").drop_duplicates("time").set_index("time")


def atr(df, n=14):
    pc = df.close.shift(1)
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def backtest(raw, rr=RR):
    """Returns DataFrame of trades (unfiltered): t, side, g(%), dist(%), trend_str(%), zone_age_h, kind."""
    base = raw
    bar_min = 15; bpd = 1440 // bar_min
    o, h, l, c = base.open.values, base.high.values, base.low.values, base.close.values
    ts = base.index.values
    z = base.resample("1h").agg(OHLC).dropna()
    atz = atr(z).values; zh, zl = z.high.values, z.low.values; zi = z.index; zdelta = pd.Timedelta("1h")

    dc = raw.close.resample("1D").last().dropna()
    e = dc.ewm(span=20, adjust=False).mean()
    s = np.sign(dc - e).shift(1).fillna(0)                 # previous completed day only
    dstr = ((dc - e) / e * 100).shift(1).to_dict()

    zones = []
    for i in range(2, len(z) - 2):
        avail = zi[i + 2] + zdelta
        if zl[i] < min(zl[i - 2:i]) and zl[i] < min(zl[i + 1:i + 3]):
            zones.append((avail, 1, zl[i], zl[i] + 0.5 * atz[i + 2]))          # demand
        if zh[i] > max(zh[i - 2:i]) and zh[i] > max(zh[i + 1:i + 3]):
            zones.append((avail, -1, zh[i] - 0.5 * atz[i + 2], zh[i]))         # supply
    zones.sort(key=lambda q: q[0])

    out, busy = [], -1
    hold, trig_bars = int(HOLD_H * 60 / bar_min), int(TRIG_H * 60 / bar_min)
    for avail, side, zlo, zhi in zones:
        j0 = int(np.searchsorted(ts, np.datetime64(avail)))
        if j0 >= len(ts) - 5:
            continue
        if s.get(pd.Timestamp(avail).normalize(), 0) != side:   # trend must match zone side
            continue
        exp = min(len(ts) - 3, j0 + ZONE_DAYS * bpd); touched = None
        for j in range(max(j0, busy + 1), exp):
            if (side == 1 and c[j] < zlo) or (side == -1 and c[j] > zhi):
                break                                            # zone broken
            if (side == 1 and l[j] <= zhi) or (side == -1 and h[j] >= zlo):
                touched = j; break
        if touched is None:
            continue
        trig = None
        for k in range(touched, min(touched + trig_bars, len(ts) - 3)):
            if (side == 1 and c[k] < zlo) or (side == -1 and c[k] > zhi):
                break
            if k < 3:
                continue
            if side == 1 and c[k] > o[k] and c[k] > h[k - 3:k].max(): trig = k; break
            if side == -1 and c[k] < o[k] and c[k] < l[k - 3:k].min(): trig = k; break
        if trig is None or trig <= busy:
            continue
        i0 = trig + 1
        entry = o[i0]; stop = zlo if side == 1 else zhi; dist = side * (entry - stop)
        if not dist > 0 or dist / entry < MIN_DIST or dist / entry > MAX_DIST:
            continue
        end = min(i0 + hold, len(ts) - 1); tp = entry + side * rr * dist; res = None; kind = "TIME"
        for k in range(i0, end + 1):
            if (side == 1 and l[k] <= stop) or (side == -1 and h[k] >= stop):
                res = side * (stop - entry) / entry * 100; kind = "SL"; break
            if (side == 1 and h[k] >= tp) or (side == -1 and l[k] <= tp):
                res = side * (tp - entry) / entry * 100; kind = "TP"; break
        if res is None:
            res = side * (c[end] - entry) / entry * 100
        out.append(dict(t=pd.Timestamp(ts[i0]), side=side, g=res, dist=dist / entry * 100, kind=kind,
                        trend_str=dstr.get(pd.Timestamp(avail).normalize(), np.nan) * side,
                        zone_age_h=(ts[i0] - np.datetime64(avail)) / np.timedelta64(1, "h")))
        busy = end
    return pd.DataFrame(out).sort_values("t").reset_index(drop=True)


def apply_filters(T, name):
    f = FILTERS[name]
    return T[(T.dist >= f["min_stop"]) & (T.zone_age_h <= MAX_ZONE_AGE_H) & (T.trend_str >= f["min_trend"])]


def report(X, label, days=365):
    X = X[X.t >= X.t.max() - pd.Timedelta(days=days)]
    pnl = X.g / 100 * NOTIONAL_INR
    eq = 3000 + pnl.cumsum(); dd = (np.maximum.accumulate(np.r_[3000, eq.values])[1:] - eq).max()
    print(f"\n{label}: last {days}d | trades {len(X)} | win {np.mean(X.g > 0) * 100:.0f}% | "
          f"GROSS P/L Rs.{pnl.sum():+,.0f} | maxDD Rs.{dd:,.0f} | worst trade Rs.{pnl.min():+,.0f}")
    m = X.groupby(X.t.dt.to_period("M")).g.agg(n="size", pl=lambda s: s.sum() / 100 * NOTIONAL_INR)
    print(m.round(0).to_string())
    print("Note: gross, no fees. Maker+slippage ~0.11% = ~Rs.99/trade at Rs.90k notional.")


if __name__ == "__main__":
    name, path = sys.argv[1].upper(), sys.argv[2]
    T = backtest(load(path))
    report(T, f"{name} base (no filters)")
    report(apply_filters(T, name), f"{name} with F1+F2+F3")
    print("\nEDUCATIONAL PURPOSE ONLY. Not financial advice. Backtest is not a guarantee.")
