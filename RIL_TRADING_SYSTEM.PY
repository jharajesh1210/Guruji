"""
RIL_TRADING_SYSTEM.PY - Cash Intraday Trading System & Backtester.
Includes Dynamic Trailing Stop Loss (1:2, 1:3, 1:4 RR Exit Strategy) for both Buy & Sell (Short) in Cash Segment.
"""

from __future__ import annotations

import argparse
import logging
from datetime import time
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

# -------------------------------------------------------------------------
# 1. CONFIGURATION (CASH INTRADAY MODE)
# -------------------------------------------------------------------------
SYMBOL = "RELIANCE.NS"
EXCHANGE = "NSE"
TIMEZONE = "Asia/Kolkata"
INTERVAL = "5m"
INTERVAL_MINUTES = 5
TICK_SIZE = 0.05

CAPITAL = 100_000.0
INTRADAY_LEVERAGE = 1.0      # Cash intraday leverage (1x standard)

RISK_PER_TRADE_PCT = 1.0
MAX_DAILY_LOSS_PCT = 2.5
MAX_TRADES_PER_DAY = 3

# Risk-Reward Milestones (1:2, 1:3, 1:4)
RR_TARGET_1 = 2.0      # 1:2 RR milestone
RR_TARGET_2 = 3.0      # 1:3 RR milestone
RR_TARGET_FINAL = 4.0  # 1:4 Full Exit Target

MAX_STOP_PCT = 1.0
MIN_STOP_PCT = 0.15

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
ENTRY_START = time(9, 30)
ENTRY_END = time(14, 30)
SQUARE_OFF = time(15, 15)
AVOID_MIDDAY = True
MIDDAY_START = time(12, 0)
MIDDAY_END = time(13, 30)

def t_min(t: time) -> int:
    return t.hour * 60 + t.minute

OPEN_MIN = t_min(MARKET_OPEN)
CLOSE_MIN = t_min(MARKET_CLOSE)
ENTRY_START_MIN = t_min(ENTRY_START)
ENTRY_END_MIN = t_min(ENTRY_END)
SQUARE_OFF_MIN = t_min(SQUARE_OFF)
MIDDAY_START_MIN = t_min(MIDDAY_START)
MIDDAY_END_MIN = t_min(MIDDAY_END)

DEFAULT_STRATEGY = "orb"
ORB_MINUTES = 15
ORB_STOP_MODE = "opposite"

EMA_FAST = 9
EMA_SLOW = 21
ATR_PERIOD = 14
ATR_STOP_MULT = 1.5

USE_VOLUME_FILTER = True
VOLUME_MA = 20
VOLUME_MULT = 1.0

# -------------------------------------------------------------------------
# Cash Intraday Transaction Costs (Indian Brokerage Standards)
# -------------------------------------------------------------------------
BROKERAGE_PCT = 0.03         # Per order %, max BROKERAGE_CAP
BROKERAGE_CAP = 20.0         # Rs. per order max
STT_SELL_PCT = 0.025         # STT only on Sell side (Intraday Cash)
EXCHANGE_TXN_PCT = 0.00297   # NSE transaction charge (Buy + Sell)
SEBI_PCT = 0.0001            # SEBI turnover fees
STAMP_BUY_PCT = 0.003        # Stamp duty only on Buy side
GST_PCT = 18.0               # GST on Brokerage + Exchange + SEBI
SLIPPAGE_PCT = 0.02          # Approx slippage on market orders

HOLIDAYS: set = set()
OHLCV = ["open", "high", "low", "close", "volume"]
log = logging.getLogger("ril_trading_system")


# -------------------------------------------------------------------------
# 2. RISK & CASH TRANSACTION COSTS MANAGEMENT
# -------------------------------------------------------------------------
def round_tick(price: float) -> float:
    return round(round(price / TICK_SIZE) * TICK_SIZE, 2)

def stop_distance_ok(entry: float, stop: float, side: int):
    if side == 1 and not stop < entry:
        return False, "stop entry ke neeche nahi hai"
    if side == -1 and not stop > entry:
        return False, "stop entry ke upar nahi hai"
    pct = abs(entry - stop) / entry * 100
    if pct > MAX_STOP_PCT:
        return False, f"stop bahut door ({pct:.2f}%)"
    if pct < MIN_STOP_PCT:
        return False, f"stop bahut paas ({pct:.2f}%)"
    return True, ""

def position_size(entry: float, stop: float, capital: float = CAPITAL) -> int:
    """Cash intraday position sizing based on risk per trade and capital."""
    per_share_risk = abs(entry - stop)
    if per_share_risk <= 0:
        return 0
    
    by_risk = math.floor(capital * RISK_PER_TRADE_PCT / 100 / per_share_risk)
    by_margin = math.floor(capital * INTRADAY_LEVERAGE / entry)
    return max(0, min(by_risk, by_margin))

def trade_costs(entry: float, exit_: float, qty: int, side: int) -> float:
    """Cash intraday charges: Brokerage, STT (sell), Exchange, SEBI, Stamp (buy), GST."""
    buy_px, sell_px = (entry, exit_) if side == 1 else (exit_, entry)
    buy_val, sell_val = buy_px * qty, sell_px * qty
    turnover = buy_val + sell_val

    brokerage = (min(buy_val * BROKERAGE_PCT / 100, BROKERAGE_CAP)
                 + min(sell_val * BROKERAGE_PCT / 100, BROKERAGE_CAP))
    stt = sell_val * STT_SELL_PCT / 100
    txn = turnover * EXCHANGE_TXN_PCT / 100
    sebi = turnover * SEBI_PCT / 100
    stamp = buy_val * STAMP_BUY_PCT / 100
    gst = (brokerage + txn + sebi) * GST_PCT / 100
    return brokerage + stt + txn + sebi + stamp + gst

@dataclass
class DayRiskState:
    date: str
    trades: int = 0
    pnl: float = 0.0

    def loss_limit_hit(self) -> bool:
        return self.pnl <= -(CAPITAL * MAX_DAILY_LOSS_PCT / 100)

    def can_open(self):
        if self.trades >= MAX_TRADES_PER_DAY:
            return False, "aaj ke max trades pure ho gaye"
        if self.loss_limit_hit():
            return False, "daily loss limit hit, aaj trading band"
        return True, ""

    def register_entry(self) -> None:
        self.trades += 1

    def register_exit(self, net_pnl: float) -> None:
        self.pnl += net_pnl


# -------------------------------------------------------------------------
# 3. INDICATORS
# -------------------------------------------------------------------------
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def vwap(df: pd.DataFrame) -> pd.Series:
    day = df.index.normalize()
    typical = (df["high"] + df["low"] + df["close"]) / 3
    cum_pv = (typical * df["volume"]).groupby(day).cumsum()
    cum_v = df["volume"].groupby(day).cumsum()
    return cum_pv / cum_v.replace(0, np.nan)

def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()

def opening_range(df: pd.DataFrame, minutes: int = 15):
    day = df.index.normalize()
    bar_min = df.index.hour * 60 + df.index.minute
    in_range = (bar_min >= OPEN_MIN) & (bar_min < OPEN_MIN + minutes)
    or_high = df["high"].where(in_range).groupby(day).transform("max")
    or_low = df["low"].where(in_range).groupby(day).transform("min")
    ready = bar_min >= OPEN_MIN + minutes
    return or_high.where(ready), or_low.where(ready)

def add_all(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema_fast"] = ema(out["close"], EMA_FAST)
    out["ema_slow"] = ema(out["close"], EMA_SLOW)
    out["vwap"] = vwap(out)
    out["atr"] = atr(out, ATR_PERIOD)
    out["vol_ma"] = out["volume"].rolling(VOLUME_MA).mean().shift(1)
    out["or_high"], out["or_low"] = opening_range(out, ORB_MINUTES)
    out["bar_min"] = out.index.hour * 60 + out.index.minute
    return out


# -------------------------------------------------------------------------
# 4. STRATEGIES
# -------------------------------------------------------------------------
def _window_mask(df: pd.DataFrame) -> pd.Series:
    entry_min = df["bar_min"] + INTERVAL_MINUTES
    ok = (entry_min >= ENTRY_START_MIN) & (entry_min <= ENTRY_END_MIN)
    if AVOID_MIDDAY:
        ok &= ~((entry_min >= MIDDAY_START_MIN) & (entry_min < MIDDAY_END_MIN))
    return ok

def _volume_ok(df: pd.DataFrame) -> pd.Series:
    if not USE_VOLUME_FILTER:
        return pd.Series(True, index=df.index)
    return df["volume"] >= df["vol_ma"] * VOLUME_MULT

def _pack(df, long_, short_, long_stop, short_stop) -> pd.DataFrame:
    window = _window_mask(df)
    long_ = long_ & window
    short_ = short_ & window
    signal = np.where(long_, 1, np.where(short_, -1, 0))
    stop = np.where(long_, long_stop, np.where(short_, short_stop, np.nan))
    return pd.DataFrame({"signal": signal, "stop": stop}, index=df.index)

class ORBStrategy:
    name = "orb"
    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        hi, lo = df["or_high"], df["or_low"]
        mid = (hi + lo) / 2
        prev_close = df["close"].shift(1)
        vol = _volume_ok(df)

        long_ = (df["close"] > hi) & (prev_close <= hi) & (df["close"] > df["vwap"]) & vol
        short_ = (df["close"] < lo) & (prev_close >= lo) & (df["close"] < df["vwap"]) & vol

        long_stop = mid if ORB_STOP_MODE == "mid" else lo
        short_stop = mid if ORB_STOP_MODE == "mid" else hi
        return _pack(df, long_, short_, long_stop, short_stop)

class EMAVWAPStrategy:
    name = "ema_vwap"
    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        fast, slow = df["ema_fast"], df["ema_slow"]
        cross_up = (fast > slow) & (fast.shift(1) <= slow.shift(1))
        cross_dn = (fast < slow) & (fast.shift(1) >= slow.shift(1))
        vol = _volume_ok(df)

        long_ = cross_up & (df["close"] > df["vwap"]) & vol
        short_ = cross_dn & (df["close"] < df["vwap"]) & vol

        dist = ATR_STOP_MULT * df["atr"]
        return _pack(df, long_, short_, df["close"] - dist, df["close"] + dist)

STRATEGIES = {"orb": ORBStrategy, "ema_vwap": EMAVWAPStrategy}

def get_strategy(name: str):
    if name not in STRATEGIES:
        raise ValueError(f"Unknown strategy '{name}'")
    return STRATEGIES[name]()


# -------------------------------------------------------------------------
# 5. DATA LOADER & SYNTHETIC GENERATOR
# -------------------------------------------------------------------------
def _to_ist(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    if index.tz is None:
        return index.tz_localize(TIMEZONE)
    return index.tz_convert(TIMEZONE)

def clean_data(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).lower() for c in df.columns]
    df = df[OHLCV].astype(float)
    df.index = _to_ist(pd.DatetimeIndex(df.index))
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.dropna()
    df = df[(df[["open", "high", "low", "close"]] > 0).all(axis=1)]
    df = df[df["high"] >= df["low"]]
    df = df.between_time(MARKET_OPEN, time(15, 29))
    df.index.name = "datetime"
    return df

def generate_synthetic(days: int = 120, seed: int = 7, start_price: float = 1400.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    bars = (CLOSE_MIN - OPEN_MIN) // INTERVAL_MINUTES
    end = pd.Timestamp.now(tz=TIMEZONE).normalize().tz_localize(None) - pd.Timedelta(days=1)
    dates = pd.bdate_range(end=end, periods=days)
    n = len(dates)

    day_drift = rng.normal(0, 0.0003, size=(n, 1))
    rets = rng.normal(0, 0.0014, size=(n, bars)) + day_drift
    gaps = rng.normal(0, 0.004, size=n)

    opens = np.empty((n, bars))
    closes = np.empty((n, bars))
    prev_close = start_price
    for d in range(n):
        first = prev_close * (1 + gaps[d])
        path = first * np.exp(np.cumsum(rets[d]))
        closes[d] = path
        opens[d] = np.concatenate(([first], path[:-1]))
        prev_close = path[-1]

    highs = np.maximum(opens, closes) * (1 + np.abs(rng.normal(0, 0.0005, size=(n, bars))))
    lows = np.minimum(opens, closes) * (1 - np.abs(rng.normal(0, 0.0005, size=(n, bars))))

    t = np.linspace(0, 1, bars)
    shape = 1 + 2.5 * np.exp(-t * 12) + 1.5 * np.exp(-(1 - t) * 12)
    volume = rng.lognormal(0, 0.35, size=(n, bars)) * shape * 60_000

    def tick(x):
        return np.round(x / TICK_SIZE) * TICK_SIZE

    times = [pd.Timestamp(d) + pd.Timedelta(minutes=OPEN_MIN + k * INTERVAL_MINUTES)
             for d in dates for k in range(bars)]
    df = pd.DataFrame({
        "open": tick(opens).ravel(), "high": tick(highs).ravel(),
        "low": tick(lows).ravel(), "close": tick(closes).ravel(),
        "volume": np.round(volume).ravel(),
    }, index=pd.DatetimeIndex(times))
    return clean_data(df)

def fetch_yfinance(period: str = "59d"):
    try:
        import yfinance as yf
    except ImportError as e:
        raise RuntimeError("yfinance install karein: pip install yfinance") from e
    raw = yf.download(SYMBOL, period=period, interval=INTERVAL, auto_adjust=False, progress=False, threads=False)
    if raw is None or raw.empty:
        raise RuntimeError("Data fetch nahi ho saka")
    return clean_data(raw)


# -------------------------------------------------------------------------
# 6. DYNAMIC TRAILING BACKTEST ENGINE (CASH BUY & SELL EXIT LOGIC)
# -------------------------------------------------------------------------
def _clamp(px: float, lo: float, hi: float) -> float:
    return min(max(px, lo), hi)

def _check_trailing_exit(pos: dict, o: float, h: float, l: float, minute: int, slip: float):
    side = pos["side"]

    if minute >= SQUARE_OFF_MIN:
        return _clamp(o * (1 - side * slip), l, h), "square_off"

    # ------------------ LONG (BUY) CASH EXIT ------------------
    if side == 1:
        if l <= pos["stop"]:
            return _clamp(pos["stop"] * (1 - slip), l, h), pos["exit_reason"]

        if h >= pos["t_final"]:
            return pos["t_final"], "target_1:4"

        if h >= pos["t_3"]:
            pos["stop"] = max(pos["stop"], pos["t_3"])
            pos["exit_reason"] = "trail_sl_1:3"
        elif h >= pos["t_2"]:
            pos["stop"] = max(pos["stop"], pos["t_2"])
            pos["exit_reason"] = "trail_sl_1:2"

        if l <= pos["stop"]:
            return _clamp(pos["stop"] * (1 - slip), l, h), pos["exit_reason"]

    # ------------------ SHORT (SELL) CASH EXIT ------------------
    else:
        if h >= pos["stop"]:
            return _clamp(pos["stop"] * (1 + slip), l, h), pos["exit_reason"]

        if l <= pos["t_final"]:
            return pos["t_final"], "target_1:4"

        if l <= pos["t_3"]:
            pos["stop"] = min(pos["stop"], pos["t_3"])
            pos["exit_reason"] = "trail_sl_1:3"
        elif l <= pos["t_2"]:
            pos["stop"] = min(pos["stop"], pos["t_2"])
            pos["exit_reason"] = "trail_sl_1:2"

        if h >= pos["stop"]:
            return _clamp(pos["stop"] * (1 + slip), l, h), pos["exit_reason"]

    return None

def _close_trade(pos: dict, exit_px: float, exit_time, reason: str) -> dict:
    side, qty, entry = pos["side"], pos["qty"], pos["entry"]
    gross = side * (exit_px - entry) * qty
    costs = trade_costs(entry, exit_px, qty, side)
    net = gross - costs
    risk_amt = pos["risk_dist"] * qty
    return {
        "date": pos["entry_time"].date(),
        "side": "LONG" if side == 1 else "SHORT",
        "entry_time": pos["entry_time"], "exit_time": exit_time,
        "entry": round(entry, 2), "exit": round(exit_px, 2),
        "initial_stop": pos["initial_stop"], "final_stop": pos["stop"],
        "qty": qty, "gross": round(gross, 2), "costs": round(costs, 2),
        "net": round(net, 2), "r_multiple": round(net / risk_amt, 3) if risk_amt else 0.0,
        "exit_reason": reason,
    }

def run_backtest(df: pd.DataFrame, strategy_name: str) -> pd.DataFrame:
    strat = get_strategy(strategy_name)
    data = add_all(df)
    data = data.join(strat.signals(data))
    slip = SLIPPAGE_PCT / 100
    trades = []

    for day, d in data.groupby(data.index.normalize()):
        o, h, l, c = (d[k].to_numpy() for k in ("open", "high", "low", "close"))
        sig, stp, mins = d["signal"].to_numpy(), d["stop"].to_numpy(), d["bar_min"].to_numpy()
        ts = d.index
        state = DayRiskState(str(day.date()))
        pos = None

        for i in range(len(d)):
            if pos is None and i > 0 and sig[i - 1] != 0 and mins[i] < SQUARE_OFF_MIN:
                if state.can_open()[0]:
                    side, stop = int(sig[i - 1]), float(stp[i - 1])
                    entry = round_tick(o[i] * (1 + side * slip))
                    stop = round_tick(stop)
                    valid, _ = stop_distance_ok(entry, stop, side)
                    qty = position_size(entry, stop) if valid else 0

                    if qty > 0:
                        risk_dist = abs(entry - stop)
                        pos = {
                            "side": side, "entry": entry, "initial_stop": stop, "stop": stop,
                            "qty": qty, "risk_dist": risk_dist,
                            "t_2": round_tick(entry + side * risk_dist * RR_TARGET_1),
                            "t_3": round_tick(entry + side * risk_dist * RR_TARGET_2),
                            "t_final": round_tick(entry + side * risk_dist * RR_TARGET_FINAL),
                            "entry_time": ts[i], "exit_reason": "stop_loss"
                        }
                        state.register_entry()

            if pos is not None:
                res = _check_trailing_exit(pos, o[i], h[i], l[i], mins[i], slip)
                if res:
                    t = _close_trade(pos, res[0], ts[i], res[1])
                    state.register_exit(t["net"])
                    trades.append(t)
                    pos = None

        if pos is not None:
            t = _close_trade(pos, c[-1], ts[-1], "eod")
            state.register_exit(t["net"])
            trades.append(t)

    return pd.DataFrame(trades)

def summarize(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"trades": 0}
    net = trades["net"]
    wins, losses = net[net > 0], net[net <= 0]
    equity = CAPITAL + net.cumsum()
    peak = equity.cummax().clip(lower=CAPITAL)
    dd = equity - peak
    gross_loss = -losses.sum()
    return {
        "trades": len(trades),
        "net_pnl": net.sum(),
        "return_pct": net.sum() / CAPITAL * 100,
        "total_costs": trades["costs"].sum(),
        "win_rate": len(wins) / len(trades) * 100,
        "avg_win": wins.mean() if len(wins) else 0.0,
        "avg_loss": losses.mean() if len(losses) else 0.0,
        "profit_factor": wins.sum() / gross_loss if gross_loss > 0 else float("inf"),
        "avg_r": trades["r_multiple"].mean(),
        "max_dd": dd.min(),
        "max_dd_pct": (dd / peak).min() * 100,
        "trading_days": trades["date"].nunique(),
    }

def print_summary(name: str, s: dict) -> None:
    print(f"\n=== {name} (CASH INTRADAY MODE) ===")
    if s["trades"] == 0:
        print("Koi trade nahi bana.")
        return
    pf = "inf" if np.isinf(s["profit_factor"]) else f"{s['profit_factor']:.2f}"
    print(f"Trades            : {s['trades']} ({s['trading_days']} din mein)")
    print(f"Net P&L           : Rs {s['net_pnl']:,.0f}  ({s['return_pct']:+.2f}% of capital)")
    print(f"Total costs       : Rs {s['total_costs']:,.0f}")
    print(f"Win rate          : {s['win_rate']:.1f}%")
    print(f"Profit factor     : {pf}")
    print(f"Avg R per trade   : {s['avg_r']:+.2f}")
    print(f"Max drawdown      : Rs {s['max_dd']:,.0f}  ({s['max_dd_pct']:.2f}%)")


# -------------------------------------------------------------------------
# 7. MAIN EXECUTION
# -------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Cash Trailing SL Backtester")
    ap.add_argument("--strategy", default=DEFAULT_STRATEGY, choices=list(STRATEGIES) + ["all"])
    ap.add_argument("--source", default="synthetic", choices=["yfinance", "synthetic"])
    ap.add_argument("--days", type=int, default=59)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.source == "synthetic":
        df = generate_synthetic(days=args.days)
        print("Synthetic cash data load ho gaya.")
    else:
        df = fetch_yfinance()
        print("Yahoo Finance data load ho gaya.")

    print(f"Data coverage: {df.index.min()} to {df.index.max()} | {len(df)} candles")
    names = list(STRATEGIES) if args.strategy == "all" else [args.strategy]
    combined = []
    trade_columns = ["date", "side", "entry_time", "exit_time", "entry", "exit",
                     "initial_stop", "final_stop", "qty", "gross", "costs",
                     "net", "r_multiple", "exit_reason"]
    for name in names:
        trades = run_backtest(df, name)
        print_summary(name, summarize(trades))
        output = f"RELIANCE_{name.upper()}_TRADES.csv"
        trades.reindex(columns=trade_columns).to_csv(output, index=False)
        print(f"Trade report saved: {output} ({len(trades)} trades)")
        if not trades.empty:
            tagged = trades.copy()
            tagged.insert(0, "strategy", name)
            combined.append(tagged)

    combined_columns = ["strategy"] + trade_columns
    report = (pd.concat(combined, ignore_index=True) if combined
              else pd.DataFrame(columns=combined_columns))
    report.reindex(columns=combined_columns).to_csv("RELIANCE_ALL_TRADES.csv", index=False)
    print(f"Combined trade report saved: RELIANCE_ALL_TRADES.csv ({len(report)} trades)")


if __name__ == "__main__":
    main()
