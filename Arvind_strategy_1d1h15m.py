
"""
1D Trend > 1H Zone > 15M Entry
BTC / PAXG - CoinDCX Futures Historical Backtest

BACKTEST ONLY - EDUCATIONAL PURPOSE ONLY

Results are GROSS (no fees/slippage).

Usage:
python Arvind_strategy_1d1h15m.py BTC BTCUSDT_15m.csv
python Arvind_strategy_1d1h15m.py PAXG PAXGUSDT_15m.csv

CSV columns:
time (milliseconds epoch), open, high, low, close
"""

import sys
import numpy as np
import pandas as pd


# ==========================================
# ORIGINAL STRATEGY SETTINGS - UNCHANGED
# ==========================================

OHLC = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last"
}

RR = 2.0
HOLD_H = 12
TRIG_H = 4
ZONE_DAYS = 5

MIN_DIST = 0.0015
MAX_DIST = 0.04

MAX_ZONE_AGE_H = 48

FILTERS = {
    "BTC": {
        "min_stop": 0.46,
        "min_trend": 1.15
    },
    "PAXG": {
        "min_stop": 0.23,
        "min_trend": 0.57
    }
}

NOTIONAL_INR = 3000 * 30


# ==========================================
# LOAD MARKET DATA
# ==========================================

def load(path):

    d = pd.read_csv(path)

    required = [
        "time", "open", "high", "low", "close"
    ]

    missing = [
        col for col in required
        if col not in d.columns
    ]

    if missing:
        raise ValueError(
            f"Missing CSV columns: {missing}"
        )

    d["time"] = pd.to_datetime(
        d["time"],
        unit="ms"
    )

    d = (
        d.sort_values("time")
        .drop_duplicates("time")
        .set_index("time")
    )

    print(
        f"Loaded {len(d)} candles from {path}"
    )

    return d


# ==========================================
# ATR
# ==========================================

def atr(df, n=14):

    pc = df.close.shift(1)

    tr = pd.concat(
        [
            df.high - df.low,
            (df.high - pc).abs(),
            (df.low - pc).abs()
        ],
        axis=1
    ).max(axis=1)

    return tr.ewm(
        alpha=1 / n,
        adjust=False
    ).mean()


# ==========================================
# BACKTEST
# ==========================================

def backtest(raw, rr=RR):

    base = raw

    bar_min = 15
    bpd = 1440 // bar_min

    o = base.open.values
    h = base.high.values
    l = base.low.values
    c = base.close.values
    ts = base.index.values

    z = (
        base.resample("1h")
        .agg(OHLC)
        .dropna()
    )

    atz = atr(z).values

    zh = z.high.values
    zl = z.low.values
    zi = z.index

    zdelta = pd.Timedelta("1h")

    # Daily trend - original logic

    dc = (
        raw.close.resample("1D")
        .last()
        .dropna()
    )

    e = dc.ewm(
        span=20,
        adjust=False
    ).mean()

    s = (
        np.sign(dc - e)
        .shift(1)
        .fillna(0)
    )

    dstr = (
        ((dc - e) / e * 100)
        .shift(1)
        .to_dict()
    )

    # Supply / Demand Zones

    zones = []

    for i in range(2, len(z) - 2):

        avail = zi[i + 2] + zdelta

        if (
            zl[i] < min(zl[i - 2:i])
            and
            zl[i] < min(zl[i + 1:i + 3])
        ):

            zones.append(
                (
                    avail,
                    1,
                    zl[i],
                    zl[i] + 0.5 * atz[i + 2]
                )
            )

        if (
            zh[i] > max(zh[i - 2:i])
            and
            zh[i] > max(zh[i + 1:i + 3])
        ):

            zones.append(
                (
                    avail,
                    -1,
                    zh[i] - 0.5 * atz[i + 2],
                    zh[i]
                )
            )

    zones.sort(key=lambda q: q[0])

    out = []
    busy = -1

    hold = int(
        HOLD_H * 60 / bar_min
    )

    trig_bars = int(
        TRIG_H * 60 / bar_min
    )

    for avail, side, zlo, zhi in zones:

        j0 = int(
            np.searchsorted(
                ts,
                np.datetime64(avail)
            )
        )

        if j0 >= len(ts) - 5:
            continue

        if (
            s.get(
                pd.Timestamp(avail).normalize(),
                0
            ) != side
        ):
            continue

        exp = min(
            len(ts) - 3,
            j0 + ZONE_DAYS * bpd
        )

        touched = None

        for j in range(
            max(j0, busy + 1),
            exp
        ):

            if (
                (side == 1 and c[j] < zlo)
                or
                (side == -1 and c[j] > zhi)
            ):
                break

            if (
                (side == 1 and l[j] <= zhi)
                or
                (side == -1 and h[j] >= zlo)
            ):
                touched = j
                break

        if touched is None:
            continue

        trig = None

        for k in range(
            touched,
            min(
                touched + trig_bars,
                len(ts) - 3
            )
        ):

            if (
                (side == 1 and c[k] < zlo)
                or
                (side == -1 and c[k] > zhi)
            ):
                break

            if k < 3:
                continue

            if (
                side == 1
                and c[k] > o[k]
                and c[k] > h[k - 3:k].max()
            ):
                trig = k
                break

            if (
                side == -1
                and c[k] < o[k]
                and c[k] < l[k - 3:k].min()
            ):
                trig = k
                break

        if trig is None or trig <= busy:
            continue

        i0 = trig + 1

        entry = o[i0]

        stop = (
            zlo if side == 1 else zhi
        )

        dist = side * (entry - stop)

        if (
            not dist > 0
            or dist / entry < MIN_DIST
            or dist / entry > MAX_DIST
        ):
            continue

        end = min(
            i0 + hold,
            len(ts) - 1
        )

        tp = entry + side * rr * dist

        res = None
        kind = "TIME"

        for k in range(i0, end + 1):

            if (
                (side == 1 and l[k] <= stop)
                or
                (side == -1 and h[k] >= stop)
            ):

                res = (
                    side * (stop - entry)
                    / entry * 100
                )

                kind = "SL"
                break

            if (
                (side == 1 and h[k] >= tp)
                or
                (side == -1 and l[k] <= tp)
            ):

                res = (
                    side * (tp - entry)
                    / entry * 100
                )

                kind = "TP"
                break

        if res is None:

            res = (
                side * (c[end] - entry)
                / entry * 100
            )

        out.append(
            dict(
                t=pd.Timestamp(ts[i0]),
                side=side,
                g=res,
                dist=dist / entry * 100,
                kind=kind,
                trend_str=(
                    dstr.get(
                        pd.Timestamp(
                            avail
                        ).normalize(),
                        np.nan
                    ) * side
                ),
                zone_age_h=(
                    ts[i0] - np.datetime64(avail)
                ) / np.timedelta64(1, "h")
            )
        )

        busy = end

    # ======================================
    # FIX 1: HANDLE ZERO TRADES
    # ======================================

    columns = [
        "t",
        "side",
        "g",
        "dist",
        "kind",
        "trend_str",
        "zone_age_h"
    ]

    if not out:

        print(
            "INFO: No trades found under "
            "the original strategy conditions."
        )

        return pd.DataFrame(
            columns=columns
        )

    return (
        pd.DataFrame(out)
        .sort_values("t")
        .reset_index(drop=True)
    )


# ==========================================
# ORIGINAL FILTERS
# ==========================================

def apply_filters(T, name):

    f = FILTERS[name]

    if T.empty:
        return T.copy()

    return T[
        (T.dist >= f["min_stop"])
        &
        (T.zone_age_h <= MAX_ZONE_AGE_H)
        &
        (T.trend_str >= f["min_trend"])
    ].copy()


# ==========================================
# BACKTEST REPORT
# ==========================================

def report(X, label, days=365):

    print()
    print("=" * 55)
    print(label)
    print("=" * 55)

    # ======================================
    # FIX 2: EMPTY REPORT HANDLING
    # ======================================

    if X.empty:

        print(f"Period       : Last {days} days")
        print("Total Trades : 0")
        print("Win Rate     : N/A")
        print("Gross P/L    : Rs.0")
        print("Max Drawdown : Rs.0")
        print("Worst Trade  : N/A")

        print(
            "STATUS: No qualifying trades "
            "found in available data."
        )

        print(
            "Original strategy filters "
            "remain unchanged."
        )

        return

    X = X[
        X.t >= (
            X.t.max()
            - pd.Timedelta(days=days)
        )
    ].copy()

    if X.empty:

        print(
            "No trades in selected period."
        )

        return

    pnl = (
        X.g / 100 * NOTIONAL_INR
    )

    eq = (
        3000 + pnl.cumsum()
    )

    dd = (
        np.maximum.accumulate(
            np.r_[3000, eq.values]
        )[1:] - eq
    ).max()

    win_rate = (
        np.mean(X.g > 0) * 100
    )

    print(
        f"Period       : Last {days} days"
    )

    print(
        f"Total Trades : {len(X)}"
    )

    print(
        f"Win Rate     : {win_rate:.0f}%"
    )

    print(
        f"Gross P/L    : Rs.{pnl.sum():+,.0f}"
    )

    print(
        f"Max Drawdown : Rs.{dd:,.0f}"
    )

    print(
        f"Worst Trade  : Rs.{pnl.min():+,.0f}"
    )

    m = X.groupby(
        X.t.dt.to_period("M")
    ).g.agg(
        n="size",
        pl=lambda s: (
            s.sum() / 100 * NOTIONAL_INR
        )
    )

    print()
    print("MONTHLY REPORT")
    print(m.round(0).to_string())

    print()
    print(
        "Note: Gross P/L, no fees. "
        "Maker+slippage ~0.11% = "
        "~Rs.99/trade at Rs.90k notional."
    )


# ==========================================
# MAIN
# ==========================================

if __name__ == "__main__":

    if len(sys.argv) < 3:

        print(
            "Usage: python "
            "Arvind_strategy_1d1h15m.py "
            "BTC BTCUSDT_15m.csv"
        )

        sys.exit(1)

    name = sys.argv[1].upper()
    path = sys.argv[2]

    if name not in FILTERS:

        print(
            f"ERROR: Unsupported symbol {name}"
        )

        sys.exit(1)

    print()
    print("=" * 55)
    print(f"{name} - COINDCX FUTURES BACKTEST")
    print("=" * 55)

    T = backtest(
        load(path)
    )

    report(
        T,
        f"{name} base (no filters)"
    )

    filtered = apply_filters(
        T,
        name
    )

    report(
        filtered,
        f"{name} with F1+F2+F3"
    )

    print()
    print(
        "EDUCATIONAL PURPOSE ONLY. "
        "Not financial advice. "
        "Backtest is not a guarantee."
    )
