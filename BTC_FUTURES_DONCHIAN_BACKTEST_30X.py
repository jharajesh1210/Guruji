"""BTC 5m dual-regime pullback research backtest. PAPER ONLY; no live orders.

LONG reversal setup:
- Prior 12-bar RSI minimum below 25.
- RSI recovers above 35 and is rising.
- Close reclaims EMA50, MACD histogram improves, volume ratio > 0.8.
- Close is no more than 1% above EMA200 (avoid chasing extended rallies).
- Stop 3.0 ATR; target 3R.

SHORT continuation setup:
- EMA20 < EMA50 < EMA200.
- Prior 6-bar RSI maximum above 65.
- Close crosses below EMA20 and RSI is falling.
- Stop 2.5 ATR; target 2R.

Signals use completed candles; entries occur at next candle open.
Fees, slippage, assumed 8-hour funding, and an approximate liquidation-distance
guard are included. Historical funding is not supplied by this model.
This is a research backtest only; no exchange orders are sent.
"""
import argparse
import json
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

URL = "https://public.coindcx.com/market_data/candlesticks"
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
                response = session.get(
                    URL,
                    params={"pair": "B-BTC_USDT", "from": a, "to": b,
                            "resolution": "5", "pcode": "f"},
                    timeout=25,
                )
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict):
                    payload = payload.get("data", payload.get("candles", []))
                if not isinstance(payload, list):
                    raise ValueError(f"Unexpected candle payload: {str(payload)[:200]}")
                rows.extend(payload)
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
        time.sleep(0.12)

    frame = pd.DataFrame(rows)
    if frame.empty:
        raise RuntimeError("No CoinDCX candles received")
    required = {"time", "open", "high", "low", "close", "volume"}
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(f"CoinDCX response missing columns: {sorted(missing)}")
    frame["time"] = pd.to_numeric(frame["time"], errors="coerce")
    frame["time"] = (
        pd.to_datetime(frame["time"], unit="ms", utc=True)
        if frame["time"].median() > 1e11
        else pd.to_datetime(frame["time"], unit="s", utc=True)
    )
    for col in ("open", "high", "low", "close", "volume"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = (
        frame.dropna(subset=["time", "open", "high", "low", "close", "volume"])
        .drop_duplicates("time")
        .sort_values("time")
        .set_index("time")
    )
    frame = frame[frame.index < pd.Timestamp.now(tz="UTC").floor("5min")]
    if len(frame) < 1000:
        raise RuntimeError(f"Insufficient 5m candles: {len(frame)}")
    return frame


def build_signals(frame):
    d = frame.copy()
    d["ema20"] = d.close.ewm(span=20, adjust=False, min_periods=20).mean()
    d["ema50"] = d.close.ewm(span=50, adjust=False, min_periods=50).mean()
    d["ema200"] = d.close.ewm(span=200, adjust=False, min_periods=200).mean()

    delta = d.close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    d["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    macd = (
        d.close.ewm(span=12, adjust=False, min_periods=26).mean()
        - d.close.ewm(span=26, adjust=False, min_periods=26).mean()
    )
    d["macdh"] = macd - macd.ewm(span=9, adjust=False, min_periods=9).mean()

    previous_close = d.close.shift(1)
    tr = pd.concat(
        [(d.high - d.low), (d.high - previous_close).abs(),
         (d.low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    d["vol_ratio"] = d.volume / d.volume.rolling(20).mean()

    # LONG: oversold reversal / recovery; all rolling extrema exclude current candle.
    prior_rsi_low = d.rsi.rolling(12).min().shift(1)
    long_signal = (
        (prior_rsi_low < 25)
        & (d.rsi > 35)
        & (d.rsi > d.rsi.shift(1))
        & (d.close > d.ema50)
        & (d.macdh > d.macdh.shift(1))
        & (d.vol_ratio > 0.8)
        & (d.close < d.ema200 * 1.01)
    )

    # SHORT: bearish trend continuation after an overbought pullback.
    prior_rsi_high = d.rsi.rolling(6).max().shift(1)
    short_signal = (
        (d.ema20 < d.ema50)
        & (d.ema50 < d.ema200)
        & (prior_rsi_high > 65)
        & (d.close < d.ema20)
        & (d.close.shift(1) >= d.ema20.shift(1))
        & (d.rsi < d.rsi.shift(1))
    )
    return d, long_signal.fillna(False).to_numpy(), short_signal.fillna(False).to_numpy()


def simulate(d, long_signal, short_signal, capital, margin, leverage, fee,
             slippage, funding_rate, maintenance_margin_rate, start=250, end=None):
    if end is None:
        end = len(d)
    if start < 0 or end > len(d) or start >= end:
        raise ValueError("Invalid simulation range")
    if maintenance_margin_rate < 0 or maintenance_margin_rate >= 1 / leverage:
        raise ValueError("Maintenance margin rate must be >=0 and < 1/leverage")

    opens = d.open.to_numpy()
    highs = d.high.to_numpy()
    lows = d.low.to_numpy()
    closes = d.close.to_numpy()
    atrs = d.atr.to_numpy()
    times = d.index

    cash = float(capital)
    position = None
    trades = []
    equity_rows = []
    i = max(start, 250)
    liquidation_distance = (1 / leverage) - maintenance_margin_rate

    while i < end - 1:
        if position is None:
            if cash < margin:
                break
            side = 1 if long_signal[i] else (-1 if short_signal[i] else 0)
            if side == 0 or not np.isfinite(atrs[i]) or atrs[i] <= 0:
                equity_rows.append({"time": times[i], "equity_usdt": cash})
                i += 1
                continue

            stop_multiple, reward_multiple = (3.0, 3.0) if side == 1 else (2.5, 2.0)
            entry = opens[i + 1] * (1 + slippage * side)
            stop_distance = stop_multiple * atrs[i]
            # Conservative buffer before approximate isolated liquidation distance.
            if stop_distance / entry >= liquidation_distance * 0.75:
                equity_rows.append({"time": times[i], "equity_usdt": cash})
                i += 1
                continue

            entry_time = times[i + 1]
            position = {
                "side": side,
                "entry": entry,
                "stop": entry - side * stop_distance,
                "target": entry + side * stop_distance * reward_multiple,
                "qty": margin * leverage / entry,
                "margin": margin,
                "notional": margin * leverage,
                "entry_time": entry_time,
                "next_funding": entry_time.floor("8h") + pd.Timedelta(hours=8),
                "funding_paid": 0.0,
                "stop_multiple": stop_multiple,
                "reward_multiple": reward_multiple,
            }
            equity_rows.append({"time": times[i], "equity_usdt": cash})
            i += 1
            continue

        side = position["side"]
        now = times[i]
        while now >= position["next_funding"]:
            # Positive assumed funding: longs pay, shorts receive.
            position["funding_paid"] += position["notional"] * funding_rate * side
            position["next_funding"] += pd.Timedelta(hours=8)

        stop_hit = lows[i] <= position["stop"] if side == 1 else highs[i] >= position["stop"]
        target_hit = highs[i] >= position["target"] if side == 1 else lows[i] <= position["target"]

        if stop_hit or target_hit:
            # If both levels trade in one 5m candle, conservatively assume stop first.
            raw_exit = position["stop"] if stop_hit else position["target"]
            exit_price = raw_exit * (1 - slippage * side)
            gross = (exit_price - position["entry"]) * position["qty"] * side
            fees = (position["entry"] + exit_price) * position["qty"] * fee
            net = gross - fees - position["funding_paid"]
            cash += net
            trades.append({
                "entry_time": position["entry_time"],
                "exit_time": now,
                "side": "LONG" if side == 1 else "SHORT",
                "entry": position["entry"],
                "exit": exit_price,
                "margin_usdt": position["margin"],
                "notional_usdt": position["notional"],
                "leverage": leverage,
                "stop_atr": position["stop_multiple"],
                "target_R": position["reward_multiple"],
                "funding_paid_usdt": position["funding_paid"],
                "net_pnl_usdt": net,
                "reason": "STOP_LOSS" if stop_hit else "TAKE_PROFIT",
            })
            position = None
            equity_rows.append({"time": now, "equity_usdt": cash})
        else:
            marked = cash + (
                (closes[i] - position["entry"]) * position["qty"] * side
                - (position["entry"] + closes[i]) * position["qty"] * fee
                - position["funding_paid"]
            )
            equity_rows.append({"time": now, "equity_usdt": marked})
        i += 1

    if position is not None:
        last_i = end - 1
        now = times[last_i]
        while now >= position["next_funding"]:
            position["funding_paid"] += position["notional"] * funding_rate * position["side"]
            position["next_funding"] += pd.Timedelta(hours=8)
        side = position["side"]
        exit_price = closes[last_i] * (1 - slippage * side)
        gross = (exit_price - position["entry"]) * position["qty"] * side
        fees = (position["entry"] + exit_price) * position["qty"] * fee
        net = gross - fees - position["funding_paid"]
        cash += net
        trades.append({
            "entry_time": position["entry_time"], "exit_time": now,
            "side": "LONG" if side == 1 else "SHORT",
            "entry": position["entry"], "exit": exit_price,
            "margin_usdt": position["margin"], "notional_usdt": position["notional"],
            "leverage": leverage, "stop_atr": position["stop_multiple"],
            "target_R": position["reward_multiple"],
            "funding_paid_usdt": position["funding_paid"],
            "net_pnl_usdt": net, "reason": "END_OF_TEST_FORCED_CLOSE",
        })

    trade_frame = pd.DataFrame(trades)
    equity_frame = pd.DataFrame(equity_rows)
    pnl = trade_frame.net_pnl_usdt if not trade_frame.empty else pd.Series(dtype=float)
    gross_profit = pnl[pnl > 0].sum()
    gross_loss = -pnl[pnl < 0].sum()
    if not equity_frame.empty:
        peak = equity_frame.equity_usdt.cummax()
        drawdown = ((equity_frame.equity_usdt / peak.replace(0, np.nan)) - 1).min() * 100
        drawdown = 0.0 if pd.isna(drawdown) else float(drawdown)
    else:
        drawdown = 0.0

    summary = {
        "trades": len(trade_frame),
        "long_trades": int((trade_frame.side == "LONG").sum()) if len(trade_frame) else 0,
        "short_trades": int((trade_frame.side == "SHORT").sum()) if len(trade_frame) else 0,
        "win_rate_pct": round((pnl > 0).mean() * 100, 2) if len(pnl) else 0.0,
        "net_profit_usdt": round(cash - capital, 2),
        "ending_equity_usdt": round(cash, 2),
        "return_pct": round((cash / capital - 1) * 100, 2),
        "max_drawdown_pct": round(drawdown, 2),
        "profit_factor": round(float(gross_profit / gross_loss), 3) if gross_loss else ("inf" if gross_profit else 0),
        "funding_paid_usdt": round(float(trade_frame.funding_paid_usdt.sum()), 2) if len(trade_frame) else 0.0,
        "long_net_usdt": round(float(trade_frame.loc[trade_frame.side == "LONG", "net_pnl_usdt"].sum()), 2) if len(trade_frame) else 0.0,
        "short_net_usdt": round(float(trade_frame.loc[trade_frame.side == "SHORT", "net_pnl_usdt"].sum()), 2) if len(trade_frame) else 0.0,
    }
    return trade_frame, equity_frame, summary



def send_paper_telegram(message):
    token = os.getenv("TG_TOKEN", "").strip()
    chat_id = os.getenv("TG_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram skipped: set TG_TOKEN and TG_CHAT_ID environment variables.")
        return
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API rejected message: {payload}")
        print("Telegram alert sent.")
    except Exception as exc:
        print(f"Telegram alert failed: {exc}")


def append_csv_row(path, row):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([row]).to_csv(
        target, mode="a", header=not target.exists(), index=False
    )


def run_paper_cycle(args):
    state_path = Path(args.paper_state)
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    else:
        state = {
            "cash": float(args.capital),
            "position": None,
            "last_processed_candle": None,
        }

    candles = get_data(3)
    data, long_signal, short_signal = build_signals(candles)
    if data.empty:
        raise RuntimeError("No completed candles available.")
    candle_time = data.index[-1]
    now = pd.Timestamp.now(tz="UTC")
    age_minutes = (now - candle_time).total_seconds() / 60
    if age_minutes > 12:
        raise RuntimeError(
            f"Latest completed candle is stale ({age_minutes:.1f} minutes old); no state changed."
        )
    candle_key = candle_time.isoformat()
    if state.get("last_processed_candle") == candle_key:
        print(f"No new closed 5m candle: {candle_key}")
        return
    if len(data) < 250:
        raise RuntimeError(f"Insufficient candles for indicators: {len(data)}")

    candle = data.iloc[-1]
    cash = float(state["cash"])
    position = state.get("position")
    had_position_at_start = position is not None
    fee = float(args.fee)
    slippage = float(args.slippage)
    funding_rate = float(args.funding_rate)
    margin = float(args.margin)
    leverage = float(args.leverage)
    notional = margin * leverage
    event_message = None

    if position is not None:
        side = int(position["side"])
        next_funding = pd.Timestamp(position["next_funding"])
        if next_funding.tzinfo is None:
            next_funding = next_funding.tz_localize("UTC")
        while candle_time >= next_funding:
            position["funding_paid"] = float(position["funding_paid"]) + notional * funding_rate * side
            next_funding += pd.Timedelta(hours=8)
        position["next_funding"] = next_funding.isoformat()

        stop_hit = candle.low <= float(position["stop"]) if side == 1 else candle.high >= float(position["stop"])
        target_hit = candle.high >= float(position["target"]) if side == 1 else candle.low <= float(position["target"])
        if stop_hit or target_hit:
            raw_exit = float(position["stop"]) if stop_hit else float(position["target"])
            exit_price = raw_exit * (1 - slippage * side)
            qty = float(position["qty"])
            gross = (exit_price - float(position["entry"])) * qty * side
            fees = (float(position["entry"]) + exit_price) * qty * fee
            funding_paid = float(position["funding_paid"])
            net = gross - fees - funding_paid
            cash += net
            trade = {
                "entry_time": position["entry_time"],
                "exit_time": candle_time.isoformat(),
                "side": "LONG" if side == 1 else "SHORT",
                "entry": position["entry"],
                "exit": exit_price,
                "margin_usdt": margin,
                "notional_usdt": notional,
                "leverage": leverage,
                "stop_atr": position["stop_atr"],
                "target_R": position["target_R"],
                "funding_paid_usdt": funding_paid,
                "net_pnl_usdt": net,
                "reason": "STOP_LOSS" if stop_hit else "TAKE_PROFIT",
                "mode": "PAPER_ONLY",
            }
            append_csv_row(args.paper_trades, trade)
            event_message = (
                f"BTC 5M PAPER EXIT {'LONG' if side == 1 else 'SHORT'}\n"
                f"Exit: {exit_price:.2f}\n"
                f"Net P&L: {net:+.2f} USDT\n"
                f"Balance: {cash:.2f} USDT\n"
                f"Reason: {trade['reason']}"
            )
            position = None
        else:
            state["position"] = position

    if position is None and not had_position_at_start and cash >= margin:
        side = 1 if bool(long_signal[-1]) else (-1 if bool(short_signal[-1]) else 0)
        atr = float(candle.atr)
        if side and np.isfinite(atr) and atr > 0:
            stop_multiple, reward_multiple = (3.0, 3.0) if side == 1 else (2.5, 2.0)
            reference = float(candle.close)
            entry = reference * (1 + slippage * side)
            stop_distance = stop_multiple * atr
            liquidation_distance = (1 / leverage) - args.maintenance_margin_rate
            if stop_distance / entry < liquidation_distance * 0.75:
                qty = notional / entry
                position = {
                    "side": side,
                    "entry": entry,
                    "stop": entry - side * stop_distance,
                    "target": entry + side * stop_distance * reward_multiple,
                    "qty": qty,
                    "entry_time": candle_time.isoformat(),
                    "next_funding": (candle_time.floor("8h") + pd.Timedelta(hours=8)).isoformat(),
                    "funding_paid": 0.0,
                    "stop_atr": stop_multiple,
                    "target_R": reward_multiple,
                }
                event_message = (
                    f"BTC 5M PAPER {'BUY / LONG' if side == 1 else 'SELL / SHORT'}\n"
                    f"Entry: {entry:.2f}\n"
                    f"Stop Loss: {position['stop']:.2f}\n"
                    f"Target: {position['target']:.2f}\n"
                    f"Margin: {margin:.2f} USDT | Leverage: {leverage:g}x\n"
                    f"Notional: {notional:.2f} USDT\n"
                    f"Signal candle: {candle_time.isoformat()}\n"
                    "PAPER ONLY — no exchange order sent."
                )
            else:
                print("Signal skipped: stop is too close to approximate liquidation distance.")

    unrealized = 0.0
    if position is not None:
        side = int(position["side"])
        mark = float(candle.close)
        unrealized = (
            (mark - float(position["entry"])) * float(position["qty"]) * side
            - (float(position["entry"]) + mark) * float(position["qty"]) * fee
            - float(position["funding_paid"])
        )

    state["cash"] = cash
    state["position"] = position
    state["last_processed_candle"] = candle_key
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temp_path.replace(state_path)

    append_csv_row(args.paper_equity, {
        "time": candle_key,
        "realized_balance_usdt": cash,
        "unrealized_pnl_usdt": unrealized,
        "equity_usdt": cash + unrealized,
        "position": ("LONG" if position["side"] == 1 else "SHORT") if position else "FLAT",
    })
    print(
        f"BTC PAPER | {candle_key} | balance={cash:.2f} | "
        f"unrealized={unrealized:+.2f} | equity={cash + unrealized:.2f} | "
        f"position={('LONG' if position['side'] == 1 else 'SHORT') if position else 'FLAT'}"
    )
    if event_message:
        send_paper_telegram(event_message)


def run_paper(args):
    print("BTC 5M DUAL PULLBACK PAPER MODE — NO LIVE ORDERS")
    print("State and CSV files are stored locally. Press Ctrl+C to stop.")
    while True:
        try:
            run_paper_cycle(args)
        except KeyboardInterrupt:
            print("Paper scanner stopped by user.")
            break
        except Exception as exc:
            print(f"Paper cycle error: {exc}")
        if not args.loop:
            break
        wait_seconds = max(2, 300 - (time.time() % 300) + 2)
        time.sleep(wait_seconds)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("backtest", "paper"), default="backtest")
    parser.add_argument("--test-telegram", action="store_true", help="send a Telegram connectivity test and exit")
    parser.add_argument("--loop", action="store_true", help="keep paper scanner running every 5 minutes")
    parser.add_argument("--paper-state", default="BTC_DUAL_PULLBACK_PAPER_STATE.json")
    parser.add_argument("--paper-trades", default="BTC_DUAL_PULLBACK_PAPER_TRADES.csv")
    parser.add_argument("--paper-equity", default="BTC_DUAL_PULLBACK_PAPER_EQUITY.csv")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--capital", type=float, default=1000)
    parser.add_argument("--margin", type=float, default=100)
    parser.add_argument("--leverage", type=float, default=30)
    parser.add_argument("--fee", type=float, default=0.0005, help="fee per side")
    parser.add_argument("--slippage", type=float, default=0.0002, help="slippage per side")
    parser.add_argument("--funding-rate", type=float, default=0.0001, help="ASSUMED rate per 8h; positive means longs pay")
    parser.add_argument("--maintenance-margin-rate", type=float, default=DEFAULT_MAINTENANCE_MARGIN_RATE)
    parser.add_argument("--out", default="BTC_DUAL_PULLBACK_RESULTS")
    args = parser.parse_args()

    if args.test_telegram:
        token = os.getenv("TG_TOKEN", "").strip()
        chat_id = os.getenv("TG_CHAT_ID", "").strip()
        if not token or not chat_id:
            parser.error("--test-telegram requires TG_TOKEN and TG_CHAT_ID environment variables")
        send_paper_telegram(
            "✅ BTC 5M PAPER SCANNER — TELEGRAM TEST SUCCESSFUL\n"
            "Telegram connection is working.\n"
            "Mode: PAPER ONLY — no exchange order sent."
        )
        return

    if (args.days < 3 or args.capital <= 0 or args.margin <= 0 or args.leverage <= 0
            or args.fee < 0 or args.slippage < 0 or args.funding_rate < 0):
        parser.error("Invalid days/capital/margin/leverage/cost parameters")

    if args.mode == "paper":
        run_paper(args)
        return

    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    candles = get_data(args.days)
    candles.to_csv(output / "BTC_5M_candles.csv")
    data, long_signal, short_signal = build_signals(candles)

    trades, equity, summary = simulate(
        data, long_signal, short_signal, args.capital, args.margin, args.leverage,
        args.fee, args.slippage, args.funding_rate, args.maintenance_margin_rate,
    )
    trades.to_csv(output / "BTC_DUAL_PULLBACK_trades.csv", index=False)
    equity.to_csv(output / "BTC_DUAL_PULLBACK_equity.csv", index=False)

    # Chronological 70/30 split: indicators are calculated causally on full history;
    # test-period trades are independently initialized at the requested starting capital.
    split = int(len(data) * 0.70)
    _, _, train_summary = simulate(
        data, long_signal, short_signal, args.capital, args.margin, args.leverage,
        args.fee, args.slippage, args.funding_rate, args.maintenance_margin_rate,
        start=250, end=split,
    )
    _, _, test_summary = simulate(
        data, long_signal, short_signal, args.capital, args.margin, args.leverage,
        args.fee, args.slippage, args.funding_rate, args.maintenance_margin_rate,
        start=split, end=len(data),
    )
    comparison = pd.DataFrame([
        {"period": "FULL_365D", **summary},
        {"period": "TRAIN_FIRST_70_PERCENT", **train_summary},
        {"period": "OUT_OF_SAMPLE_LAST_30_PERCENT", **test_summary},
    ])
    comparison.to_csv(output / "comparison.csv", index=False)
    print("BTC DUAL PULLBACK STRATEGY — PAPER BACKTEST ONLY")
    print(comparison.to_string(index=False))
    print(f"Candles: {len(candles)} | From: {candles.index.min()} | To: {candles.index.max()}")
    print(f"Margin/trade: {args.margin} USDT | Leverage: {args.leverage}x | Exposure: {args.margin * args.leverage} USDT")
    print(f"Fee/side: {args.fee:.4%} | Slippage/side: {args.slippage:.4%} | Assumed funding/8h: {args.funding_rate:.4%}")
    print("LONG: RSI12 low <25, RSI recovers >35, close > EMA50, MACD histogram rising, volume ratio >0.8, close <= EMA200*1.01; SL 3 ATR / TP 3R.")
    print("SHORT: EMA20 < EMA50 < EMA200, prior RSI6 high >65, close crosses below EMA20, RSI falling; SL 2.5 ATR / TP 2R.")
    print("Funding is an assumption, not historical CoinDCX funding. Liquidation is guarded approximately, not exchange-tier exact.")
    print("NO LIVE ORDERS ARE PLACED.")


if __name__ == "__main__":
    main()
