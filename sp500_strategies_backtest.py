#!/usr/bin/env python3
"""
S&P 500 (SPY) backtest: two separate long-only strategies, compared head to head.

  A) RSI(2) mean reversion (Larry Connors style)
     Signal on day t-1 close: close > SMA200 and RSI(2) < ENTRY level.
     Entry: day t OPEN (conservative, no look-ahead).
     Exit : first close where close > SMA5 (or RSI(2) > 65), or after MAX_HOLD days.

  B) Gap-down fill
     Day t opens below previous close by GAP_MIN..GAP_MAX (and previous close > SMA200 if trend filter on).
     Entry: day t OPEN. Target: previous close (gap fill). Stop: entry - ATR_K * ATR14(prev day).
     Else exit at day t close. If stop and target are both reachable on the same daily bar the STOP is
     assumed first (daily bars cannot show the order) - this is deliberately pessimistic.

Method
  * Each strategy trades 100% of equity, one position at a time, compounding. Costs: COST per round trip.
  * Parameters are picked on the TRAIN period (entry date < SPLIT) only, then reported on the untouched
    TEST period (entry date >= SPLIT). The winner is decided on TEST results.
  * Daily SPY bars (unadjusted prices, no dividends). Results are for research, not trading advice.

Usage:
  python sp500_strategies_backtest.py                 # downloads SPY (yfinance, Stooq fallback)
  python sp500_strategies_backtest.py --synthetic     # offline smoke test with random data
"""
import argparse
import json
import os
from itertools import product

import numpy as np
import pandas as pd

TICKER = "SPY"
START = "1995-01-01"
SPLIT = "2018-01-01"        # train < SPLIT <= test
COST = 0.0002               # 0.02% per round trip (commission + slippage estimate)
MIN_TRAIN_TRADES = 100
OUT_DIR = "sp500_results"


# ----------------------------------------------------------------------------- data
def fetch_data(ticker=TICKER, start=START):
    err = None
    try:
        import yfinance as yf
        df = yf.download(ticker, start=start, auto_adjust=False, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[["Open", "High", "Low", "Close"]].dropna()
        df.columns = ["open", "high", "low", "close"]
        if len(df) > 1000:
            return _clean(df)
        err = f"yfinance returned only {len(df)} rows"
    except Exception as exc:  # noqa: BLE001
        err = f"yfinance failed: {exc}"
    print(f"[data] {err}; trying Stooq fallback")
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    df = pd.read_csv(url, parse_dates=["Date"], index_col="Date")
    df.columns = [c.lower() for c in df.columns]
    df = df[["open", "high", "low", "close"]].dropna()
    df = df[df.index >= start]
    if len(df) < 1000:
        raise RuntimeError("Could not download enough price history")
    return _clean(df)


def synthetic_data(seed=7):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("1995-01-02", "2025-12-31")
    n = len(idx)
    on = rng.normal(0.0002, 0.004, n)           # overnight gap
    intr = rng.normal(0.0002, 0.009, n)         # open -> close
    close = np.empty(n)
    open_ = np.empty(n)
    prev = 100.0
    for i in range(n):
        open_[i] = prev * (1 + on[i])
        close[i] = open_[i] * (1 + intr[i])
        prev = close[i]
    hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.003, n)))
    lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.003, n)))
    return _clean(pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close}, index=idx))


def _clean(df):
    df = df.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df[~df.index.duplicated()].sort_index()
    return df[(df[["open", "high", "low", "close"]] > 0).all(axis=1)]


# ----------------------------------------------------------------------------- indicators
def add_indicators(df):
    d = df.copy()
    c = d["close"]
    delta = c.diff()
    up = delta.clip(lower=0.0)
    dn = -delta.clip(upper=0.0)
    ru = up.ewm(alpha=1 / 2, adjust=False, min_periods=2).mean()
    rd = dn.ewm(alpha=1 / 2, adjust=False, min_periods=2).mean()
    rs = ru / rd.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    rsi = rsi.where(rd != 0, np.where(ru > 0, 100.0, 50.0))
    d["rsi2"] = rsi
    d["sma5"] = c.rolling(5).mean()
    d["sma200"] = c.rolling(200).mean()
    pc = c.shift(1)
    tr = pd.concat([d["high"] - d["low"], (d["high"] - pc).abs(), (d["low"] - pc).abs()], axis=1).max(axis=1)
    d["atr14"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    return d.dropna(subset=["rsi2", "sma5", "sma200", "atr14"])


# ----------------------------------------------------------------------------- strategies
def run_rsi2(d, entry_lvl=10.0, exit_mode="sma5", exit_rsi=65.0, max_hold=10, trend=True, cost=COST):
    o, c = d["open"].values, d["close"].values
    rsi, s200, s5 = d["rsi2"].values, d["sma200"].values, d["sma5"].values
    n = len(d)
    rows = []
    i = 1
    while i < n:
        if rsi[i - 1] < entry_lvl and (not trend or c[i - 1] > s200[i - 1]):
            entry, j = o[i], i
            while True:
                held = j - i + 1
                hit = (c[j] > s5[j]) if exit_mode == "sma5" else (rsi[j] > exit_rsi)
                if hit or held >= max_hold or j == n - 1:
                    break
                j += 1
            rows.append((d.index[i], d.index[j], entry, c[j], c[j] / entry - 1 - cost, j - i + 1))
            i = j + 1
        else:
            i += 1
    return _trades(rows)


def run_gap(d, gap_min=0.003, gap_max=0.015, atr_k=1.0, trend=True, cost=COST):
    o, h, l, c = d["open"].values, d["high"].values, d["low"].values, d["close"].values
    s200, atr = d["sma200"].values, d["atr14"].values
    rows = []
    for i in range(1, len(d)):
        pc = c[i - 1]
        gap = o[i] / pc - 1
        if not (-gap_max <= gap <= -gap_min):
            continue
        if trend and not pc > s200[i - 1]:
            continue
        entry, target = o[i], pc
        stop = entry - atr_k * atr[i - 1] if atr_k else None
        if stop is not None and l[i] <= stop:
            exitp = stop                      # stop assumed first (pessimistic)
        elif h[i] >= target:
            exitp = target
        else:
            exitp = c[i]
        rows.append((d.index[i], d.index[i], entry, exitp, exitp / entry - 1 - cost, 1))
    return _trades(rows)


def _trades(rows):
    return pd.DataFrame(rows, columns=["entry_date", "exit_date", "entry", "exit", "ret", "hold_days"])


# ----------------------------------------------------------------------------- statistics
def stats(tr, start, end, n_days):
    years = max((end - start).days / 365.25, 1e-9)
    if tr.empty:
        return dict(trades=0, win_rate=np.nan, avg_ret_pct=np.nan, avg_win_pct=np.nan, avg_loss_pct=np.nan,
                    profit_factor=np.nan, total_return_pct=0.0, cagr_pct=0.0, max_dd_pct=0.0,
                    time_in_market_pct=0.0, trades_per_year=0.0)
    r = tr["ret"].values
    eq = np.cumprod(1 + r)
    peak = np.maximum.accumulate(np.concatenate([[1.0], eq]))[1:]
    wins, losses = r[r > 0], r[r <= 0]
    gl = abs(losses.sum())
    return dict(
        trades=int(len(r)),
        win_rate=100 * len(wins) / len(r),
        avg_ret_pct=100 * r.mean(),
        avg_win_pct=100 * wins.mean() if len(wins) else 0.0,
        avg_loss_pct=100 * losses.mean() if len(losses) else 0.0,
        profit_factor=(wins.sum() / gl) if gl > 0 else np.inf,
        total_return_pct=100 * (eq[-1] - 1),
        cagr_pct=100 * (eq[-1] ** (1 / years) - 1),
        max_dd_pct=100 * ((eq / peak) - 1).min(),
        time_in_market_pct=100 * tr["hold_days"].sum() / max(n_days, 1),
        trades_per_year=len(r) / years,
    )


def split_stats(tr, d):
    """Return (train_stats, test_stats, full_stats)."""
    cut = pd.Timestamp(SPLIT)
    parts = {
        "train": (tr[tr["entry_date"] < cut], d.index[0], cut, int((d.index < cut).sum())),
        "test": (tr[tr["entry_date"] >= cut], cut, d.index[-1], int((d.index >= cut).sum())),
        "full": (tr, d.index[0], d.index[-1], len(d)),
    }
    return {k: stats(*v) for k, v in parts.items()}


def buy_hold(d):
    cut = pd.Timestamp(SPLIT)
    out = {}
    for name, seg in (("train", d[d.index < cut]), ("test", d[d.index >= cut]), ("full", d)):
        years = max((seg.index[-1] - seg.index[0]).days / 365.25, 1e-9)
        eq = seg["close"] / seg["close"].iloc[0]
        out[name] = dict(total_return_pct=100 * (eq.iloc[-1] - 1), cagr_pct=100 * (eq.iloc[-1] ** (1 / years) - 1),
                         max_dd_pct=100 * (eq / eq.cummax() - 1).min())
    return out


# ----------------------------------------------------------------------------- grid + report
RSI_GRID = [dict(entry_lvl=e, exit_mode=m, trend=t) for e, m, t in product([5, 10, 15], ["sma5", "rsi65"], [True, False])]
GAP_GRID = [dict(gap_min=g, atr_k=k, trend=t) for g, k, t in
            product([0.002, 0.003, 0.005], [0.75, 1.0, 1.5, None], [True, False])]


def grid_search(name, fn, grid, d):
    rows = []
    for p in grid:
        tr = fn(d, **p)
        s = split_stats(tr, d)
        rows.append({"strategy": name, "params": json.dumps(p), "_p": p, "_tr": tr,
                     **{f"train_{k}": v for k, v in s["train"].items()},
                     **{f"test_{k}": v for k, v in s["test"].items()}})
    return rows


def pick_best(rows):
    ok = [r for r in rows if r["train_trades"] >= MIN_TRAIN_TRADES and np.isfinite(r["train_cagr_pct"])]
    if not ok:
        ok = rows
    return max(ok, key=lambda r: r["train_cagr_pct"])


def md_table(rows, cols):
    head = "| " + " | ".join(cols) + " |\n|" + "|".join(["---"] * len(cols)) + "|\n"
    body = ""
    for r in rows:
        body += "| " + " | ".join(f"{r[c]:.2f}" if isinstance(r[c], (float, np.floating)) else str(r[c]) for c in cols) + " |\n"
    return head + body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true", help="offline smoke test on random data")
    ap.add_argument("--ticker", default=TICKER)
    args = ap.parse_args()

    raw = synthetic_data() if args.synthetic else fetch_data(args.ticker)
    d = add_indicators(raw)
    print(f"[data] {'SYNTHETIC' if args.synthetic else args.ticker}: {d.index[0].date()} -> {d.index[-1].date()} | {len(d)} days")
    os.makedirs(OUT_DIR, exist_ok=True)

    rsi_rows = grid_search("RSI2 mean reversion", run_rsi2, RSI_GRID, d)
    gap_rows = grid_search("Gap-down fill", run_gap, GAP_GRID, d)
    best = {"RSI2 mean reversion": pick_best(rsi_rows), "Gap-down fill": pick_best(gap_rows)}
    bh = buy_hold(d)

    cols = ["trades", "win_rate", "avg_ret_pct", "avg_win_pct", "avg_loss_pct", "profit_factor",
            "total_return_pct", "cagr_pct", "max_dd_pct", "time_in_market_pct", "trades_per_year"]
    lines = [f"# S&P 500 ({'synthetic' if args.synthetic else args.ticker}) strategy comparison",
             f"Data {d.index[0].date()} -> {d.index[-1].date()} | train < {SPLIT} <= test | cost {COST*100:.2f}% per round trip "
             f"| 100% equity per trade, one position at a time\n"]
    summary = {}
    for name, b in best.items():
        full = split_stats(b["_tr"], d)
        summary[name] = {"params": b["_p"], **full}
        lines.append(f"## {name}  (params chosen on TRAIN only: `{b['params']}`)\n")
        lines.append(md_table([{"period": k, **full[k]} for k in ("train", "test", "full")], ["period"] + cols))
        b["_tr"].to_csv(os.path.join(OUT_DIR, f"trades_{name.split()[0].lower()}.csv"), index=False)
    lines.append("## Buy & hold SPY (price only)\n")
    lines.append(md_table([{"period": k, **v} for k, v in bh.items()], ["period", "total_return_pct", "cagr_pct", "max_dd_pct"]))

    # winner on TEST (out-of-sample) CAGR
    t = {n: summary[n]["test"] for n in summary}
    winner = max(t, key=lambda n: t[n]["cagr_pct"])
    other = [n for n in t if n != winner][0]
    lines.append("## Verdict (out-of-sample TEST period)\n")
    lines.append(f"**Winner by test CAGR: {winner}** "
                 f"(CAGR {t[winner]['cagr_pct']:.2f}% vs {t[other]['cagr_pct']:.2f}% for {other}; "
                 f"max drawdown {t[winner]['max_dd_pct']:.1f}% vs {t[other]['max_dd_pct']:.1f}%; "
                 f"test trades {t[winner]['trades']} vs {t[other]['trades']}).\n")
    lines.append("Buy & hold test CAGR for reference: %.2f%%.\n" % bh["test"]["cagr_pct"])

    # full grid for transparency
    allrows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rsi_rows + gap_rows]
    pd.DataFrame(allrows).to_csv(os.path.join(OUT_DIR, "grid_results.csv"), index=False)
    lines.append("## Parameter grid (train vs test CAGR %, trades)\n")
    g = [{"strategy": r["strategy"], "params": r["params"], "train_cagr": r["train_cagr_pct"], "train_n": r["train_trades"],
          "test_cagr": r["test_cagr_pct"], "test_n": r["test_trades"]} for r in rsi_rows + gap_rows]
    lines.append(md_table(g, ["strategy", "params", "train_cagr", "train_n", "test_cagr", "test_n"]))
    lines.append("\n_Limits: daily bars, SPY ETF (no futures roll/margin), stop-first assumption on gap trades, "
                 "no dividends, costs approximate. Past results do not guarantee future results; not financial advice._\n")

    text = "\n".join(lines)
    with open(os.path.join(OUT_DIR, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    with open(os.path.join(OUT_DIR, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump({"winner": winner, "summary": summary, "buy_hold": bh}, fh, indent=2, default=float)
    print(text)
    step = os.getenv("GITHUB_STEP_SUMMARY")
    if step:
        with open(step, "a", encoding="utf-8") as fh:
            fh.write(text)


if __name__ == "__main__":
    main()
