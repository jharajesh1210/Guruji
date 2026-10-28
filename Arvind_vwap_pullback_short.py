
"""
ARVIND VWAP PULLBACK SHORT
RELIANCE NSE - 5 MINUTE

Original entry conditions retained.

New exit:
2R hit -> SL lock at 2R
3R hit -> SL lock at 3R
4R hit -> Final profit booking

Paper trading / backtesting only.
"""

import argparse
import math
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd


# ================= SETTINGS =================

CAPITAL = 100_000
COST = 0.0006

ATR_N = 14
EMA_N = 20
ADX_N = 14

NIFTY_RET_MIN = 0.10
ADX_MIN = 30.0

SL_ATR = 1.0
RR = 4.0

ENTRY_START = (9, 25)
ENTRY_END = (14, 30)
EXIT_TIME = (15, 15)

MAX_TRADES_PER_DAY = 2


# ================= INDICATORS =================

def _rma(s, n):
    return s.ewm(
        alpha=1 / n,
        adjust=False
    ).mean()


def prepare(rel, nif):

    d = rel.between_time(
        "09:15", "15:29"
    ).copy().sort_index()

    d["day"] = d.index.normalize()

    d["tod"] = (
        d.index.hour * 60 + d.index.minute
    )

    h, l, c, v = (
        d.high, d.low, d.close, d.volume
    )

    pc = c.shift()

    tr = pd.concat(
        [
            h - l,
            (h - pc).abs(),
            (l - pc).abs()
        ],
        axis=1
    ).max(axis=1)

    d["atr"] = _rma(tr, ATR_N)

    tp = (h + l + c) / 3

    d["vwap"] = (
        (tp * v).groupby(d.day).cumsum()
        /
        v.groupby(d.day).cumsum().replace(
            0, np.nan
        )
    ).ffill()

    d["ema20"] = c.ewm(
        span=EMA_N,
        adjust=False
    ).mean()

    up = h.diff()
    dn = -l.diff()

    pdm = up.where(
        (up > dn) & (up > 0), 0.0
    )

    ndm = dn.where(
        (dn > up) & (dn > 0), 0.0
    )

    pdi = (
        100 * _rma(pdm, ADX_N) / d.atr
    )

    ndi = (
        100 * _rma(ndm, ADX_N) / d.atr
    )

    d["adx"] = _rma(
        100 * (pdi - ndi).abs()
        / (pdi + ndi),
        ADX_N
    )

    n = nif.between_time(
        "09:15", "15:29"
    ).sort_index()

    d["nifty_ret15"] = (
        n.close.pct_change(3) * 100
    ).reindex(d.index)

    same_day = (
        (d.day.shift(-2) == d.day)
        &
        (d.day.shift(2) == d.day)
    )

    d["swing_high"] = (
        (h == h.rolling(
            5, center=True
        ).max())
        &
        same_day
    )

    d["setup"] = (
        d.swing_high
        &
        (c < d.vwap)
        &
        (c < d.ema20)
        &
        (
            (d.nifty_ret15 > NIFTY_RET_MIN)
            |
            (d.adx > ADX_MIN)
        )
    )

    return d


# ================= TRADE ENGINE =================

def run_day(t, capital=CAPITAL):

    o, h, l, c, a = (
        t[k].values
        for k in (
            "open", "high", "low",
            "close", "atr"
        )
    )

    tod = t.tod.values
    setup = t.setup.values
    idx = t.index

    t0 = ENTRY_START[0] * 60 + ENTRY_START[1]
    t1 = ENTRY_END[0] * 60 + ENTRY_END[1]
    tx = EXIT_TIME[0] * 60 + EXIT_TIME[1]

    trades = []
    pos = None
    last_exit = -1
    count = 0

    def close_trade(p, price, reason, j):

        qty = p["qty"]

        pnl = (
            (p["entry"] - price) * qty
            -
            COST * p["entry"] * qty
        )

        p.update(
            exit=round(float(price), 2),
            exit_time=idx[j],
            reason=reason,
            pnl_rs=round(float(pnl), 2)
        )

        trades.append(p)

    for j in range(len(t)):

        # -------- EXISTING POSITION --------

        if pos is not None:

            # Conservative:
            # Existing stop checked before new target.
            if h[j] >= pos["sl"]:

                reason = (
                    "Locked SL 3R"
                    if pos["stage"] == 3
                    else "Locked SL 2R"
                    if pos["stage"] == 2
                    else "Stop loss"
                )

                close_trade(
                    pos, pos["sl"], reason, j
                )

                pos = None
                last_exit = j
                continue

            # Final 4R
            if l[j] <= pos["tp"]:

                close_trade(
                    pos,
                    pos["tp"],
                    "Final target 4R",
                    j
                )

                pos = None
                last_exit = j
                continue

            # Original time exit
            if tod[j] >= tx:

                close_trade(
                    pos,
                    c[j],
                    "15:15 exit",
                    j
                )

                pos = None
                last_exit = j
                continue

            # 3R lock
            if (
                pos["stage"] < 3
                and l[j] <= pos["entry"] - 3 * pos["risk"]
            ):

                pos["stage"] = 3

                pos["sl"] = round(
                    pos["entry"] - 3 * pos["risk"],
                    2
                )

            # 2R lock
            elif (
                pos["stage"] < 2
                and l[j] <= pos["entry"] - 2 * pos["risk"]
            ):

                pos["stage"] = 2

                pos["sl"] = round(
                    pos["entry"] - 2 * pos["risk"],
                    2
                )

            continue

        # -------- ORIGINAL ENTRY RULES --------

        i = j - 3

        if (
            i < 0
            or j <= last_exit
            or count >= MAX_TRADES_PER_DAY
            or not setup[i]
            or not (t0 <= tod[j] <= t1)
        ):
            continue

        e = float(o[j])

        sl = float(
            h[i] + SL_ATR * a[i]
        )

        risk = sl - e

        if not (
            0.3 * a[i] < risk <= 5 * a[i]
        ):
            continue

        pos = dict(
            signal_time=idx[i],
            entry_time=idx[j],
            side="SELL",
            entry=round(e, 2),
            sl=round(sl, 2),
            initial_sl=round(sl, 2),
            risk=risk,
            tp=round(e - RR * risk, 2),
            qty=max(
                1,
                math.floor(capital / e)
            ),
            stage=0
        )

        count += 1

        # Original same-entry-candle
        # stop/target checking.
        if h[j] >= pos["sl"]:

            close_trade(
                pos, pos["sl"], "Stop loss", j
            )

            pos = None
            last_exit = j

        elif l[j] <= pos["tp"]:

            close_trade(
                pos,
                pos["tp"],
                "Final target 4R",
                j
            )

            pos = None
            last_exit = j

        elif l[j] <= pos["entry"] - 3 * risk:

            pos["stage"] = 3
            pos["sl"] = round(e - 3 * risk, 2)

        elif l[j] <= pos["entry"] - 2 * risk:

            pos["stage"] = 2
            pos["sl"] = round(e - 2 * risk, 2)

    if pos is not None:

        pos.update(
            exit=None,
            exit_time=None,
            reason="OPEN",
            pnl_rs=None
        )

        trades.append(pos)

    return trades


# ================= BACKTEST =================

def backtest(d):

    rows = []

    for _, t in d.groupby("day"):

        rows += [
            x for x in run_day(t)
            if x["reason"] != "OPEN"
        ]

    return pd.DataFrame(rows)


# ================= STATISTICS =================

def stats(t):

    if t.empty:
        return {"Trades": 0}

    p = t.pnl_rs
    eq = p.cumsum()

    m = t.groupby(
        pd.to_datetime(
            t.entry_time
        ).dt.to_period("M")
    ).pnl_rs.sum()

    streak = 0
    cur = 0

    for v in p:

        cur = cur + 1 if v < 0 else 0
        streak = max(streak, cur)

    return {
        "Trades": len(t),

        "Win %": round(
            (p > 0).mean() * 100, 1
        ),

        "Profit Factor": round(
            p[p > 0].sum() / -p[p < 0].sum(),
            2
        ) if (p < 0).any() else float("inf"),

        "Net P&L Rs": round(p.sum()),

        "Avg win Rs": round(
            p[p > 0].mean()
        ) if (p > 0).any() else 0,

        "Avg loss Rs": round(
            p[p < 0].mean()
        ) if (p < 0).any() else 0,

        "Max DD Rs": round(
            min(0, (eq - eq.cummax()).min())
        ),

        "Max loss streak": streak,

        "Profit months": (
            f"{(m > 0).sum()}/{len(m)}"
        )
    }


# ================= CSV LOADER =================

def load_csv(path):

    d = pd.read_csv(path)

    d.columns = [
        x.strip().lower()
        for x in d.columns
    ]

    d["datetime"] = pd.to_datetime(
        d["datetime"]
    )

    if d["datetime"].dt.tz is not None:

        d["datetime"] = (
            d["datetime"]
            .dt.tz_convert("Asia/Kolkata")
            .dt.tz_localize(None)
        )

    return d.set_index("datetime")[
        ["open", "high", "low", "close", "volume"]
    ].sort_index()


# ================= ANGEL ONE OPTION =================

def load_angel(days):

    import time
    import pyotp
    from SmartApi import SmartConnect

    api = SmartConnect(
        api_key=os.environ["ANGEL_API_KEY"]
    )

    api.generateSession(
        os.environ["ANGEL_CLIENT_ID"],
        os.environ["ANGEL_PASSWORD"],
        pyotp.TOTP(
            os.environ["ANGEL_TOTP_SECRET"]
        ).now()
    )

    out = {}
    end = datetime.now()

    for name, token in (
        ("RELIANCE", "2885"),
        ("NIFTY", "99926000")
    ):

        rows = []
        cur = end - timedelta(days=days)

        while cur < end:

            nxt = min(
                cur + timedelta(days=30),
                end
            )

            for attempt in range(6):

                r = api.getCandleData({
                    "exchange": "NSE",
                    "symboltoken": token,
                    "interval": "FIVE_MINUTE",
                    "fromdate": cur.strftime(
                        "%Y-%m-%d 09:15"
                    ),
                    "todate": nxt.strftime(
                        "%Y-%m-%d %H:%M"
                    )
                })

                if r and r.get("status") is not False:
                    rows += r.get("data") or []
                    break

                time.sleep(3 * (attempt + 1))

            cur = nxt + timedelta(days=1)
            time.sleep(0.6)

        df = pd.DataFrame(
            rows,
            columns=[
                "datetime", "open", "high",
                "low", "close", "volume"
            ]
        )

        df["datetime"] = (
            pd.to_datetime(df["datetime"])
            .dt.tz_localize(None)
        )

        out[name] = (
            df.drop_duplicates("datetime")
            .set_index("datetime")
            .sort_index()
        )

    return out["RELIANCE"], out["NIFTY"]


# ================= MAIN =================

def main():

    ap = argparse.ArgumentParser(
        description="Arvind VWAP SHORT RR4"
    )

    ap.add_argument("--reliance")
    ap.add_argument("--nifty")
    ap.add_argument(
        "--angel", action="store_true"
    )
    ap.add_argument(
        "--days", type=int, default=365
    )
    ap.add_argument(
        "--live", action="store_true"
    )
    ap.add_argument(
        "--out",
        default="vwap_pullback_trades.csv"
    )

    args = ap.parse_args()

    if args.angel:

        rel, nif = load_angel(
            7 if args.live else args.days
        )

    elif args.reliance and args.nifty:

        rel = load_csv(args.reliance)
        nif = load_csv(args.nifty)

    else:

        ap.error(
            "RELIANCE_5m.csv and NIFTY_5m.csv required"
        )

    if args.live:

        now = (
            datetime.utcnow()
            + timedelta(hours=5, minutes=30)
        )

        rel = rel[
            rel.index + pd.Timedelta(minutes=5)
            <= now
        ]

        nif = nif[
            nif.index + pd.Timedelta(minutes=5)
            <= now
        ]

        d = prepare(rel, nif)

        if d.empty:
            print("No completed candles.")
            return

        t = d[d.day == d.day.iloc[-1]]

        last = t.iloc[-1]

        print(
            f"{t.index[-1]} "
            f"Close {last.close:.2f} "
            f"VWAP {last.vwap:.2f} "
            f"EMA20 {last.ema20:.2f} "
            f"ADX {last.adx:.1f} "
            f"Nifty15m {last.nifty_ret15:+.2f}%"
        )

        trades = run_day(t)

        print(
            "Aaj ke trades:"
            if trades else
            "Aaj abhi tak koi trade nahi."
        )

        for x in trades:
            print(x)

        if len(t) >= 3 and t.setup.values[-3]:
            print(
                "Setup confirmed: "
                "next candle OPEN pe SELL"
            )

        return

    d = prepare(rel, nif)

    if d.empty:
        raise RuntimeError("No market data available")

    t = backtest(d)

    print("ARVIND VWAP SHORT - RR4")

    print(
        f"Data: {d.index[0]} to {d.index[-1]}"
    )

    print("TOTAL:", stats(t))

    if not t.empty:

        te = pd.to_datetime(t.entry_time)

        for y, g in t.groupby(
            te.dt.to_period("Y")
        ):
            print(y, stats(g))

    t.to_csv(args.out, index=False)

    print("Trades saved:", args.out)


if __name__ == "__main__":
    main()
