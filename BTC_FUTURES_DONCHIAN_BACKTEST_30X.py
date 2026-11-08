"""CoinDCX BTC perpetual 5m Donchian/EMA/ATR paper backtest. No live orders.

Entry rules are unchanged:
- LONG: close breaks above prior Donchian high and is above EMA200.
- SHORT: close breaks below prior Donchian low and is below EMA200.

Risk/exit corrections:
- Initial and ATR-trailing stop.
- Opposite 10-bar Donchian channel exit.
- Approximate isolated-margin liquidation guard (not exchange-exact).
- Configurable funding assumption charged every 8 hours.
- Every open trade is closed at the test boundary and fully recorded.
"""
import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

URL = "https://public.coindcx.com/market_data/candlesticks"
EXIT_LOOKBACK = 10
DEFAULT_MAINTENANCE_MARGIN_RATE = 0.005


def get_data(days):
    end = int(time.time()) // 300 * 300
    start = end - days * 86400
    rows = []
    session = requests.Session()
    for a in range(start, end, 86400):
        b = min(a + 86400, end)
        for attempt in range(4):
            try:
                r = session.get(
                    URL,
                    params={"pair": "B-BTC_USDT", "from": a, "to": b,
                            "resolution": "5", "pcode": "f"},
                    timeout=25,
                )
                r.raise_for_status()
                payload = r.json()
                if isinstance(payload, dict):
                    payload = payload.get("data", payload.get("candles", []))
                if not isinstance(payload, list):
                    raise ValueError(str(payload)[:200])
                rows.extend(payload)
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
        time.sleep(0.12)

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("No CoinDCX candles received")
    required = {"time", "open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"CoinDCX candle response missing columns: {sorted(missing)}")
    df["time"] = pd.to_numeric(df["time"], errors="coerce")
    df["time"] = (
        pd.to_datetime(df["time"], unit="ms", utc=True)
        if df["time"].median() > 1e11
        else pd.to_datetime(df["time"], unit="s", utc=True)
    )
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = (
        df.dropna(subset=["time", "open", "high", "low", "close"])
        .drop_duplicates("time")
        .sort_values("time")
        .set_index("time")
    )
    df = df[df.index < pd.Timestamp.now(tz="UTC").floor("5min")]
    if len(df) < 1000:
        raise RuntimeError(f"Insufficient 5m candles received: {len(df)}")
    return df


def indicators(df, lookback):
    d = df.copy()
    d["ema200"] = d.close.ewm(span=200, adjust=False, min_periods=200).mean()
    # Entry channels use only completed candles; no look-ahead.
    d["upper"] = d.high.shift(1).rolling(lookback).max()
    d["lower"] = d.low.shift(1).rolling(lookback).min()
    # Faster opposite channel is used only for exits, not entries.
    d["exit_upper"] = d.high.shift(1).rolling(EXIT_LOOKBACK).max()
    d["exit_lower"] = d.low.shift(1).rolling(EXIT_LOOKBACK).min()
    prev = d.close.shift(1)
    tr = pd.concat(
        [(d.high - d.low), (d.high - prev).abs(), (d.low - prev).abs()],
        axis=1,
    ).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    return d


def _funding_due(pos, timestamp, funding_rate):
    """Accrue assumed funding every 8h after entry; positive rate means longs pay."""
    while timestamp >= pos["next_funding"]:
        pos["funding_paid"] += pos["notional"] * funding_rate * pos["side"]
        pos["next_funding"] += pd.Timedelta(hours=8)


def _close_trade(pos, raw_exit, exit_time, reason, fee, slip):
    side = pos["side"]
    exit_price = raw_exit * (1 - slip * side)
    gross = (exit_price - pos["entry"]) * pos["qty"] * side
    fees = (pos["entry"] + exit_price) * pos["qty"] * fee
    pnl = gross - fees - pos["funding_paid"]
    return {
        "entry_time": pos["entry_time"],
        "exit_time": exit_time,
        "side": "LONG" if side == 1 else "SHORT",
        "entry": pos["entry"],
        "exit": exit_price,
        "net_pnl_usdt": pnl,
        "margin_usdt": pos["margin"],
        "notional_usdt": pos["notional"],
        "leverage": pos["leverage"],
        "funding_paid_usdt": pos["funding_paid"],
        "reason": reason,
    }


def simulate(d, capital, margin, leverage, fee, slip, atrmult,
             funding_rate, maintenance_margin_rate):
    cash = float(capital)
    pos = None
    trades = []
    equity = []

    if maintenance_margin_rate < 0 or maintenance_margin_rate >= 1 / leverage:
        raise ValueError("Maintenance margin rate must be >=0 and less than 1/leverage")

    for i in range(201, len(d) - 1):
        bar = d.iloc[i]
        nxt = d.iloc[i + 1]
        needed = [
            bar.ema200, bar.upper, bar.lower, bar.exit_upper,
            bar.exit_lower, bar.atr,
        ]
        if not np.isfinite(needed).all() or bar.atr <= 0:
            continue

        closed_this_bar = False

        if pos is not None:
            _funding_due(pos, d.index[i], funding_rate)
            side = pos["side"]
            # Approximate liquidation price for linear isolated margin:
            # adverse move ~= 1/leverage - maintenance margin rate.
            liq_price = pos["liq_price"]
            trail = pos["stop"]
            protective_stop = max(trail, liq_price) if side == 1 else min(trail, liq_price)

            stop_hit = bar.low <= protective_stop if side == 1 else bar.high >= protective_stop
            channel_exit = (
                bar.close < bar.exit_lower if side == 1
                else bar.close > bar.exit_upper
            )

            if stop_hit:
                raw_exit = min(bar.open, protective_stop) if side == 1 else max(bar.open, protective_stop)
                liq_is_tighter = liq_price >= trail if side == 1 else liq_price <= trail
                reason = "LIQUIDATION_GUARD" if liq_is_tighter else "ATR_TRAILING_STOP"
                trade = _close_trade(pos, raw_exit, d.index[i], reason, fee, slip)
                cash += trade["net_pnl_usdt"]
                trades.append(trade)
                pos = None
                closed_this_bar = True
            elif channel_exit:
                trade = _close_trade(pos, bar.close, d.index[i], "DONCHIAN_EXIT_10", fee, slip)
                cash += trade["net_pnl_usdt"]
                trades.append(trade)
                pos = None
                closed_this_bar = True
            else:
                # Trail from this completed candle for use from the next candle.
                candidate = bar.close - side * atrmult * bar.atr
                pos["stop"] = max(pos["stop"], candidate) if side == 1 else min(pos["stop"], candidate)

        if pos is None and not closed_this_bar and cash > 0:
            # Original Donchian/EMA entry conditions are preserved exactly.
            side = (
                1 if bar.close > bar.upper and bar.close > bar.ema200
                else (-1 if bar.close < bar.lower and bar.close < bar.ema200 else 0)
            )
            if side:
                entry = nxt.open * (1 + side * slip)
                used_margin = min(float(margin), cash)
                notional = used_margin * leverage
                qty = notional / entry
                entry_time = d.index[i + 1]
                liq_distance = (1 / leverage) - maintenance_margin_rate
                liq_price = entry * (1 - side * liq_distance)
                initial_stop = entry - side * atrmult * bar.atr
                pos = {
                    "side": side,
                    "entry": entry,
                    "stop": initial_stop,
                    "qty": qty,
                    "entry_time": entry_time,
                    "margin": used_margin,
                    "notional": notional,
                    "leverage": leverage,
                    "liq_price": liq_price,
                    "funding_paid": 0.0,
                    "next_funding": entry_time.floor("8h") + pd.Timedelta(hours=8),
                }

        marked = cash
        if pos is not None:
            side = pos["side"]
            _funding_due(pos, d.index[i], funding_rate)
            marked += (
                (bar.close - pos["entry"]) * pos["qty"] * side
                - (pos["entry"] + bar.close) * pos["qty"] * fee
                - pos["funding_paid"]
            )
        equity.append({"time": d.index[i], "equity_usdt": marked})

    # Close any remaining position at the last available completed candle.
    if pos is not None:
        last_time = d.index[-1]
        _funding_due(pos, last_time, funding_rate)
        last_close = float(d.iloc[-1].close)
        trade = _close_trade(pos, last_close, last_time, "END_OF_TEST_FORCED_CLOSE", fee, slip)
        cash += trade["net_pnl_usdt"]
        trades.append(trade)
        equity.append({"time": last_time, "equity_usdt": cash})

    t = pd.DataFrame(trades)
    e = pd.DataFrame(equity)
    if not e.empty:
        peak = e.equity_usdt.cummax()
        dd = ((e.equity_usdt / peak.replace(0, np.nan)) - 1).min() * 100
        dd = 0.0 if pd.isna(dd) else float(dd)
    else:
        dd = 0.0

    profits = t.net_pnl_usdt if not t.empty else pd.Series(dtype=float)
    wins = profits[profits > 0].sum()
    losses = -profits[profits < 0].sum()
    summary = {
        "trades": len(t),
        "win_rate_pct": round((profits > 0).mean() * 100, 2) if len(t) else 0,
        "net_profit_usdt": round(cash - capital, 2),
        "ending_equity_usdt": round(cash, 2),
        "return_pct": round((cash / capital - 1) * 100, 2),
        "max_drawdown_pct": round(dd, 2),
        "profit_factor": round(float(wins / losses), 3) if losses else ("inf" if wins else "N/A"),
        "funding_paid_usdt": round(float(t.funding_paid_usdt.sum()), 2) if len(t) else 0.0,
        "forced_end_closes": int((t.reason == "END_OF_TEST_FORCED_CLOSE").sum()) if len(t) else 0,
        "liquidation_guard_exits": int((t.reason == "LIQUIDATION_GUARD").sum()) if len(t) else 0,
    }
    return t, e, summary


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--capital", type=float, default=1000)
    p.add_argument("--margin", type=float, default=100, help="USDT isolated margin per trade")
    p.add_argument("--leverage", type=float, default=30)
    p.add_argument("--fee", type=float, default=0.0005, help="fee per side")
    p.add_argument("--slippage", type=float, default=0.0002, help="slippage per side")
    p.add_argument("--funding-rate", type=float, default=0.0001,
                   help="ASSUMED funding rate per 8h interval; positive means longs pay")
    p.add_argument("--maintenance-margin-rate", type=float,
                   default=DEFAULT_MAINTENANCE_MARGIN_RATE,
                   help="Approximate maintenance margin rate; exchange tiers may differ")
    p.add_argument("--out", default="btc_donchian_results")
    args = p.parse_args()

    if (args.days < 3 or args.capital <= 0 or args.margin <= 0 or args.leverage <= 0
            or args.fee < 0 or args.slippage < 0 or args.funding_rate < 0):
        p.error("Invalid days/capital/margin/leverage/cost parameters")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    df = get_data(args.days)
    df.to_csv(out / "BTC_5M_candles.csv")

    results = []
    for lookback in (20, 55):
        d = indicators(df, lookback)
        for mult in (2.0, 3.0):
            trades, eq, summary = simulate(
                d, args.capital, args.margin, args.leverage, args.fee,
                args.slippage, mult, args.funding_rate,
                args.maintenance_margin_rate,
            )
            tag = f"DONCHIAN_{lookback}_ATR_{int(mult)}"
            trades.to_csv(out / f"{tag}_trades.csv", index=False)
            eq.to_csv(out / f"{tag}_equity.csv", index=False)
            results.append({
                "variant": tag,
                "candles": len(df),
                "first_candle": str(df.index.min()),
                "last_candle": str(df.index.max()),
                **summary,
            })

    comparison = pd.DataFrame(results)
    comparison.to_csv(out / "comparison.csv", index=False)
    print(comparison.to_string(index=False))
    print(f"Files saved: {out.resolve()}")
    print(
        f"Leverage={args.leverage}x | margin={args.margin} USDT | "
        f"exposure={args.margin * args.leverage} USDT"
    )
    print(
        f"Funding assumption={args.funding_rate:.5%} per 8h | "
        f"maintenance margin assumption={args.maintenance_margin_rate:.3%}"
    )
    print("NOTE: Liquidation is an approximation, not CoinDCX's live tier calculation.")
    print("NOTE: Funding is an assumption, not historical CoinDCX funding-rate data.")
    print("No live orders are placed.")


if __name__ == "__main__":
    main()
