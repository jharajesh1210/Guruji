# -*- coding: utf-8 -*-
"""
Smart Money Concepts (SMC) + Volume Profile Intraday Strategy -- v3 (Fully Fixed with yfinance)

Bug Fixes & Refinements in v3:
  1. Fixed Swing & Structure Break Search Logic: Corrected reverse iteration bug.
  2. Safe Volume Profile Bin Allocation: Clamped index bounds to prevent IndexError/KeyError on edge values.
  3. Strict Timestamp Alignment: Integrated robust timestamp-sync for Stock & Sensex data during backtesting.
  4. Symmetric Bearish Logic: Replaced mirror dataframe trick with dedicated bearish setup logic for accurate indicator scaling.
  5. Enhanced Position Sizing & Frictional Cost Accounting.
  6. Integrated yfinance live/historical data fetcher for seamless execution.
"""

from dataclasses import dataclass
from datetime import time as dtime
import numpy as np
import pandas as pd
try:
    import yfinance as yf
except ImportError:
    yf = None


# ----------------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------------
@dataclass
class Config:
    # Indicators
    rsi_len: int = 14
    bb_len: int = 20
    bb_std: float = 2.0
    atr_len: int = 14
    bb_width_window: int = 50
    min_width_quantile: float = 0.20      # BB width quantile threshold for low vol squeeze

    # Structure / Order Block
    swing_n: int = 2                      # Fractal swing: n candles left & right
    break_lookback: int = 15              # Structure break must occur within this lookback
    allow_bos: bool = True                # False = only authentic CHoCH allowed
    min_session_bars: int = 12

    # Volume Profile / Confluence
    vp_bins: int = 24
    poc_tol_atr: float = 0.5              # POC max ATR distance to OB zone
    retest_touch_atr: float = 0.25

    # Stop Loss & Risk
    sl_atr_mult: float = 0.5
    min_risk_pct: float = 0.15            # Minimum risk % of entry price
    max_risk_pct: float = 1.00            # Skip trade if risk % exceeds this limit

    # RSI & Momentum Filters
    rsi_long: tuple = (52, 75)
    rsi_short: tuple = (25, 48)
    use_vwap_filter: bool = True
    use_bb_filter: bool = True

    # Market Filter
    neutral_band_pct: float = 0.10        # Sensex movement within +-0.10% is NEUTRAL
    top_n_sectors: int = 2

    # Trailing SL (R Multiples)
    be_trigger_r: float = 1.0             # Move to Breakeven at 1R
    be_buffer_r: float = 0.10             # Breakeven + cost buffer
    lock_offset_r: float = 1.0            # Offset for locking SL (2R -> Lock 1R)
    final_target_r: float = 5.0
    use_bb_trail: bool = True             # Trail SL to BB Middle Band after 2R

    # Time Filters (IST)
    no_trade_before: dtime = dtime(9, 30)
    last_entry_time: dtime = dtime(15, 0)
    square_off_time: dtime = dtime(15, 15)

    # Risk Management
    capital: float = 100000.0
    risk_per_trade_pct: float = 0.5
    max_trades_per_day: int = 3
    max_daily_loss_pct: float = 2.0
    leverage: float = 5.0

    # Frictional Costs
    cost_pct_per_side: float = 0.03
    slippage_pct: float = 0.02

    # Backtest Lookback
    history_window: int = 160


# ----------------------------------------------------------------------------
# UTILITY & INDICATOR CALCULATIONS
# ----------------------------------------------------------------------------
def _ist_time(ts):
    if not isinstance(ts, pd.Timestamp):
        return None
    if ts.tzinfo is not None:
        ts = ts.tz_convert("Asia/Kolkata")
    return ts.time()


def _session(df):
    """Filters data for the current active day session."""
    if isinstance(df.index, pd.DatetimeIndex):
        day = df.index.normalize()
        return df[day == day[-1]]
    return df


def fetch_yfinance_data(ticker="^BSESN", period="5d", interval="5m"):
    """
    yfinance utility to fetch intraday 5-minute data with proper formatting and IST alignment.
    """
    if yf is None:
        raise ImportError("yfinance is not installed. Run 'pip install yfinance'")
    
    df = yf.download(ticker, period=period, interval=interval, progress=False)
    if df.empty:
        return None

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)

    df = df.rename(columns={
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume"
    })

    df = df[["open", "high", "low", "close", "volume"]].dropna()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    if df.index.tz is None:
        df.index = pd.to_datetime(df.index).tz_localize("UTC").tz_convert("Asia/Kolkata")
    else:
        df.index = df.index.tz_convert("Asia/Kolkata")

    return df


def add_indicators(df, cfg=None):
    """Calculates RSI, Bollinger Bands, ATR, and Session VWAP."""
    cfg = cfg or Config()
    df = df.copy()
    close = df["close"]

    # Wilder's RSI
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / cfg.rsi_len, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / cfg.rsi_len, adjust=False).mean()
    rsi = np.where(loss == 0, 100.0, 100 - 100 / (1 + gain / loss.replace(0, np.nan)))
    df["rsi"] = pd.Series(rsi, index=df.index).where(gain.notna(), np.nan).fillna(50.0)

    # Bollinger Bands
    df["bb_mid"] = close.rolling(cfg.bb_len).mean()
    sd = close.rolling(cfg.bb_len).std()
    df["bb_up"] = df["bb_mid"] + cfg.bb_std * sd
    df["bb_lo"] = df["bb_mid"] - cfg.bb_std * sd
    df["bb_width"] = (df["bb_up"] - df["bb_lo"]) / df["bb_mid"].replace(0, np.nan)

    # ATR
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - close.shift()).abs(), (df["low"] - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / cfg.atr_len, adjust=False).mean()

    # Session VWAP
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = tp * df["volume"]
    if isinstance(df.index, pd.DatetimeIndex):
        day = df.index.normalize()
        cum_pv = pv.groupby(day).cumsum()
        cum_v = df["volume"].groupby(day).cumsum()
    else:
        cum_pv, cum_v = pv.cumsum(), df["volume"].cumsum()
    df["vwap"] = cum_pv / cum_v.replace(0, np.nan)
    return df


def volume_profile(session_df, bins=24):
    """Robust Volume-Weighted Profile calculation with safe index clamping."""
    lo = float(session_df["low"].min())
    hi = float(session_df["high"].max())
    if hi <= lo:
        return hi, hi, lo

    edges = np.linspace(lo, hi, bins + 1)
    prof = np.zeros(bins)

    highs = session_df["high"].values
    lows = session_df["low"].values
    volumes = session_df["volume"].values

    for h, l, v in zip(highs, lows, volumes):
        a = int(np.clip(np.searchsorted(edges, l, side="right") - 1, 0, bins - 1))
        b = int(np.clip(np.searchsorted(edges, h, side="left") - 1, 0, bins - 1))
        if b < a:
            b = a
        prof[a : b + 1] += v / (b - a + 1)

    pi = int(np.argmax(prof))
    poc = (edges[pi] + edges[pi + 1]) / 2.0

    total, acc = prof.sum(), prof[pi]
    lo_i = hi_i = pi
    while acc < 0.7 * total and (lo_i > 0 or hi_i < bins - 1):
        up = prof[hi_i + 1] if hi_i < bins - 1 else -1
        dn = prof[lo_i - 1] if lo_i > 0 else -1
        if up >= dn:
            hi_i += 1
            acc += up
        else:
            lo_i -= 1
            acc += dn
    return poc, edges[hi_i + 1], edges[lo_i]


# ----------------------------------------------------------------------------
# STRATEGY ENGINE
# ----------------------------------------------------------------------------
class SMCSectorialStrategy:
    def __init__(self, config=None):
        self.cfg = config or Config()
        self.last_reject = ""

    def analyze_market_filters(self, sensex_df, sectoral_data=None):
        cfg = self.cfg
        sess = _session(sensex_df)
        if sess.empty:
            return {"market_bias": "NEUTRAL", "sensex_change_pct": 0.0, "sector_data_available": False}

        s_open = float(sess["open"].iloc[0])
        s_cur = float(sess["close"].iloc[-1])
        change_pct = (s_cur / s_open - 1) * 100 if s_open > 0 else 0.0

        bias = "NEUTRAL"
        ema_ok_long = ema_ok_short = True
        if len(sess) >= 20:
            ema20 = float(sess["close"].ewm(span=20, adjust=False).mean().iloc[-1])
            ema_ok_long, ema_ok_short = s_cur > ema20, s_cur < ema20

        if change_pct > cfg.neutral_band_pct and ema_ok_long:
            bias = "BULLISH"
        elif change_pct < -cfg.neutral_band_pct and ema_ok_short:
            bias = "BEARISH"

        out = {
            "market_bias": bias,
            "sensex_change_pct": change_pct,
            "sector_data_available": bool(sectoral_data),
            "top_gaining_sectors": [],
            "top_losing_sectors": [],
        }

        if sectoral_data:
            rs = {k: v - change_pct for k, v in sectoral_data.items()}
            gainers = [k for k, v in sorted(rs.items(), key=lambda x: -x[1]) if v > 0][: cfg.top_n_sectors]
            losers = [k for k, v in sorted(rs.items(), key=lambda x: x[1]) if v < 0][: cfg.top_n_sectors]
            out.update(top_gaining_sectors=gainers, top_losing_sectors=losers)

        return out

    def _swings(self, h, l):
        n = self.cfg.swing_n
        highs, lows = [], []
        for i in range(n, len(h) - n):
            if h[i] > h[i - n : i].max() and h[i] >= h[i + 1 : i + n + 1].max():
                highs.append((i, h[i]))
            if l[i] < l[i - n : i].min() and l[i] <= l[i + 1 : i + n + 1].min():
                lows.append((i, l[i]))
        return highs, lows

    def _find_setup(self, df, atr, poc, side="LONG"):
        cfg = self.cfg
        n = cfg.swing_n
        o, h, l, c = (df[k].values for k in ("open", "high", "low", "close"))
        N = len(df)
        highs, lows = self._swings(h, l)

        if side == "LONG":
            if not highs:
                self.last_reject = "no swing high"
                return None

            found = None
            for brk in range(N - 2, max(N - 1 - cfg.break_lookback, n) - 1, -1):
                valid_swings = [s for s in highs if s[0] + n < brk]
                for si, sp in reversed(valid_swings):
                    if si + 1 < brk and c[si + 1 : brk].max() > sp:
                        continue
                    if c[brk] > sp:
                        found = (brk, si, sp)
                        break
                if found:
                    break

            if not found:
                self.last_reject = "no structure break"
                return None

            brk, si, level = found
            prior_lows = [x for x in lows if x[0] < si]
            kind = "CHoCH" if len(prior_lows) >= 2 and prior_lows[-1][1] < prior_lows[-2][1] else "BOS"
            if kind == "BOS" and not cfg.allow_bos:
                self.last_reject = "BOS skipped"
                return None

            leg_low_idx = si + int(np.argmin(l[si : brk + 1]))
            ob = None
            for j in range(leg_low_idx, brk):
                if c[j] < o[j] and c[j + 1] > h[j]:
                    ob = j
                    break
            if ob is None:
                for j in range(leg_low_idx, max(leg_low_idx - 4, -1), -1):
                    if c[j] < o[j]:
                        ob = j
                        break
            if ob is None:
                self.last_reject = "no order block"
                return None

            ob_low, ob_high = l[ob], h[ob]
            ob_mid = (ob_low + ob_high) / 2.0

            if c[brk:N].min() <= ob_low:
                self.last_reject = "OB invalidated"
                return None

            tol = cfg.poc_tol_atr * atr
            if not (ob_low - tol <= poc <= ob_high + tol):
                self.last_reject = "POC not near OB"
                return None

            start = max(brk + 1, N - 3)
            if l[start:].min() > ob_high + cfg.retest_touch_atr * atr:
                self.last_reject = "no retest of OB yet"
                return None

            if not (c[-1] > ob_mid and c[-1] > o[-1]):
                self.last_reject = "no rejection candle"
                return None

            return {"kind": kind, "ob_low": ob_low, "ob_high": ob_high}

        else:
            if not lows:
                self.last_reject = "no swing low"
                return None

            found = None
            for brk in range(N - 2, max(N - 1 - cfg.break_lookback, n) - 1, -1):
                valid_swings = [s for s in lows if s[0] + n < brk]
                for si, sp in reversed(valid_swings):
                    if si + 1 < brk and c[si + 1 : brk].min() < sp:
                        continue
                    if c[brk] < sp:
                        found = (brk, si, sp)
                        break
                if found:
                    break

            if not found:
                self.last_reject = "no structure break"
                return None

            brk, si, level = found
            prior_highs = [x for x in highs if x[0] < si]
            kind = "CHoCH" if len(prior_highs) >= 2 and prior_highs[-1][1] > prior_highs[-2][1] else "BOS"
            if kind == "BOS" and not cfg.allow_bos:
                self.last_reject = "BOS skipped"
                return None

            leg_high_idx = si + int(np.argmax(h[si : brk + 1]))
            ob = None
            for j in range(leg_high_idx, brk):
                if c[j] > o[j] and c[j + 1] < l[j]:
                    ob = j
                    break
            if ob is None:
                for j in range(leg_high_idx, max(leg_high_idx - 4, -1), -1):
                    if c[j] > o[j]:
                        ob = j
                        break
            if ob is None:
                self.last_reject = "no order block"
                return None

            ob_low, ob_high = l[ob], h[ob]
            ob_mid = (ob_low + ob_high) / 2.0

            if c[brk:N].max() >= ob_high:
                self.last_reject = "OB invalidated"
                return None

            tol = cfg.poc_tol_atr * atr
            if not (ob_low - tol <= poc <= ob_high + tol):
                self.last_reject = "POC not near OB"
                return None

            start = max(brk + 1, N - 3)
            if h[start:].max() < ob_low - cfg.retest_touch_atr * atr:
                self.last_reject = "no retest of OB yet"
                return None

            if not (c[-1] < ob_mid and c[-1] < o[-1]):
                self.last_reject = "no rejection candle"
                return None

            return {"kind": kind, "ob_low": ob_low, "ob_high": ob_high}

    def _momentum_filter(self, df, side):
        cfg = self.cfg
        r, pr = df["rsi"].iloc[-1], df["rsi"].iloc[-2]
        c, mid, up, lo = (df[k].iloc[-1] for k in ("close", "bb_mid", "bb_up", "bb_lo"))
        vwap = df["vwap"].iloc[-1]

        if cfg.use_bb_filter:
            q = df["bb_width"].rolling(cfg.bb_width_window, min_periods=20).quantile(cfg.min_width_quantile).iloc[-1]
            if not np.isnan(q) and df["bb_width"].iloc[-1] < q:
                self.last_reject = "BB squeeze/dead market"
                return False

        if side == "LONG":
            if not (cfg.rsi_long[0] <= r <= cfg.rsi_long[1] and r > pr):
                self.last_reject = f"RSI {r:.1f} filter fail"
                return False
            if cfg.use_bb_filter and not (mid < c <= up):
                self.last_reject = "Price not between BB Mid & Upper"
                return False
            if cfg.use_vwap_filter and not np.isnan(vwap) and c < vwap:
                self.last_reject = "Price below VWAP"
                return False
        else:
            if not (cfg.rsi_short[0] <= r <= cfg.rsi_short[1] and r < pr):
                self.last_reject = f"RSI {r:.1f} filter fail"
                return False
            if cfg.use_bb_filter and not (lo <= c < mid):
                self.last_reject = "Price not between BB Lower & Mid"
                return False
            if cfg.use_vwap_filter and not np.isnan(vwap) and c > vwap:
                self.last_reject = "Price above VWAP"
                return False
        return True

    def check_entry_signal_5m(self, df_5m, market_filters, stock_sector=None):
        cfg = self.cfg
        self.last_reject = ""
        df = df_5m if "atr" in df_5m.columns else add_indicators(df_5m, cfg)

        if len(df) < 3:
            self.last_reject = "Insufficient data length"
            return None

        t = _ist_time(df.index[-1])
        if t is not None and not (cfg.no_trade_before <= t <= cfg.last_entry_time):
            self.last_reject = "Outside trading window"
            return None

        bias = market_filters["market_bias"]
        if bias == "NEUTRAL":
            self.last_reject = "Market NEUTRAL"
            return None

        side = "LONG" if bias == "BULLISH" else "SHORT"

        if stock_sector is not None and market_filters.get("sector_data_available"):
            pool = market_filters["top_gaining_sectors"] if side == "LONG" else market_filters["top_losing_sectors"]
            if stock_sector not in pool:
                self.last_reject = "Sector not in top relative strength list"
                return None

        sess = _session(df)
        if len(sess) < cfg.min_session_bars:
            self.last_reject = "Session too young"
            return None

        last = df.iloc[-1]
        if last[["rsi", "bb_mid", "atr"]].isna().any():
            self.last_reject = "Indicators warming up"
            return None

        atr = float(last["atr"])
        poc, vah, val = volume_profile(sess, cfg.vp_bins)

        setup = self._find_setup(sess, atr, poc, side=side)
        if setup is None:
            return None

        if not self._momentum_filter(df, side):
            return None

        entry = float(last["close"])
        if side == "LONG":
            sl = setup["ob_low"] - cfg.sl_atr_mult * atr
        else:
            sl = setup["ob_high"] + cfg.sl_atr_mult * atr

        risk = abs(entry - sl)
        risk_pct = (risk / entry) * 100

        if risk_pct > cfg.max_risk_pct:
            self.last_reject = f"Risk {risk_pct:.2f}% exceeds maximum limit"
            return None
        if risk_pct < cfg.min_risk_pct:
            risk = entry * (cfg.min_risk_pct / 100)
            sl = entry - risk if side == "LONG" else entry + risk

        return {
            "signal": side,
            "entry_price": entry,
            "initial_sl": sl,
            "poc": poc,
            "ob_low": setup["ob_low"],
            "ob_high": setup["ob_high"],
            "structure": setup["kind"],
            "rsi": float(last["rsi"]),
            "atr": atr,
            "risk_pct": (risk / entry) * 100,
        }

    def position_size(self, entry, sl):
        cfg = self.cfg
        risk_per_share = abs(entry - sl)
        if risk_per_share <= 0:
            return 0
        qty_by_risk = (cfg.capital * (cfg.risk_per_trade_pct / 100)) / risk_per_share
        qty_by_margin = (cfg.capital * cfg.leverage) / entry
        return int(min(qty_by_risk, qty_by_margin))

    def process_step_lock_trailing_sl(self, candle, position, bb_mid=None, exhausted=False):
        cfg = self.cfg
        if not isinstance(candle, dict):
            p = float(candle)
            candle = {"open": p, "high": p, "low": p, "close": p}

        pos_type = position["signal"]
        sign = 1 if pos_type == "LONG" else -1
        entry = position["entry_price"]
        init_sl = position["initial_sl"]
        cur_sl = position.get("current_sl", init_sl)
        risk = abs(entry - init_sl)

        adverse = candle["low"] if sign == 1 else candle["high"]
        favorable = candle["high"] if sign == 1 else candle["low"]

        if sign * (adverse - cur_sl) <= 0:
            px = candle["open"] if sign * (candle["open"] - cur_sl) <= 0 else cur_sl
            locked = sign * (cur_sl - entry) >= 0
            return {"action": "EXIT", "execution_price": px, "reason": "Trailing SL Hit" if locked else "Stop Loss Hit"}

        target = entry + sign * cfg.final_target_r * risk
        if sign * (favorable - target) >= 0:
            return {"action": "EXIT", "execution_price": target, "reason": f"Target 1:{cfg.final_target_r:g} Hit"}

        r_reached = sign * (favorable - entry) / risk
        lock_r = None
        if cfg.be_trigger_r is not None and r_reached >= cfg.be_trigger_r:
            lock_r = cfg.be_buffer_r
        for trig in (2, 3, 4):
            if r_reached >= trig:
                lock_r = trig - cfg.lock_offset_r

        new_sl, reason = cur_sl, None
        if lock_r is not None:
            cand = entry + sign * lock_r * risk
            if sign * (cand - cur_sl) > 0:
                new_sl = cand
                reason = "SL Breakeven" if lock_r <= cfg.be_buffer_r else f"SL Stepped & Locked at {lock_r:g}R"

        if cfg.use_bb_trail and bb_mid is not None and not np.isnan(bb_mid) and r_reached >= 2:
            if sign * (bb_mid - new_sl) > 0 and sign * (candle["close"] - bb_mid) > 0:
                new_sl, reason = bb_mid, "SL Trailed to BB Mid"

        if new_sl != cur_sl:
            return {"action": "TRAIL_LOCK", "new_sl": new_sl, "reason": reason}
        return {"action": "HOLD", "current_sl": cur_sl}

    def backtest(self, df_5m, sensex_df, stock_sector=None, sector_provider=None):
        cfg = self.cfg
        df = add_indicators(df_5m, cfg)
        sensex_clean = add_indicators(sensex_df, cfg) if "atr" not in sensex_df.columns else sensex_df

        trades, pos = [], None
        cur_day, n_today, pnl_today = None, 0, 0.0

        for i in range(len(df)):
            ts = df.index[i]
            row = df.iloc[i]

            if ts.date() != cur_day:
                cur_day, n_today, pnl_today = ts.date(), 0, 0.0
            t = _ist_time(ts)

            if pos is not None:
                candle = {k: float(row[k]) for k in ("open", "high", "low", "close")}
                closed = None
                if t >= cfg.square_off_time:
                    closed = (candle["close"], "Square-off 3:15")
                else:
                    ex = (pos["signal"] == "LONG" and row["close"] >= row["bb_up"] and row["rsi"] >= 80) or \
                         (pos["signal"] == "SHORT" and row["close"] <= row["bb_lo"] and row["rsi"] <= 20)
                    res = self.process_step_lock_trailing_sl(candle, pos, bb_mid=row["bb_mid"], exhausted=bool(ex))
                    if res["action"] == "EXIT":
                        closed = (res["execution_price"], res["reason"])
                    elif res["action"] == "TRAIL_LOCK":
                        pos["current_sl"] = res["new_sl"]

                if closed:
                    sign = 1 if pos["signal"] == "LONG" else -1
                    exit_fill = closed[0] * (1 - sign * cfg.slippage_pct / 100)
                    qty = pos["qty"]
                    gross = sign * (exit_fill - pos["entry_price"]) * qty
                    costs = (cfg.cost_pct_per_side / 100) * (pos["entry_price"] + exit_fill) * qty
                    pnl = gross - costs
                    risk_amt = abs(pos["entry_price"] - pos["initial_sl"]) * qty

                    trades.append({
                        "entry_time": pos["entry_time"],
                        "exit_time": ts,
                        "side": pos["signal"],
                        "structure": pos["structure"],
                        "entry": pos["entry_price"],
                        "exit": exit_fill,
                        "qty": qty,
                        "pnl": pnl,
                        "r": pnl / risk_amt if risk_amt > 0 else 0.0,
                        "reason": closed[1],
                    })
                    pnl_today += pnl
                    pos = None
                continue

            if n_today >= cfg.max_trades_per_day or pnl_today <= -cfg.capital * (cfg.max_daily_loss_pct / 100):
                continue
            if t is not None and t >= cfg.last_entry_time:
                continue

            sx = sensex_clean.loc[:ts]
            if sx.empty:
                continue

            window = df.iloc[max(0, i - cfg.history_window + 1) : i + 1]
            sectors = sector_provider(ts) if sector_provider else None
            filters = self.analyze_market_filters(sx.iloc[-cfg.history_window:], sectors)

            sig = self.check_entry_signal_5m(window, filters, stock_sector)
            if not sig:
                continue

            sign = 1 if sig["signal"] == "LONG" else -1
            fill = sig["entry_price"] * (1 + sign * cfg.slippage_pct / 100)
            qty = self.position_size(fill, sig["initial_sl"])

            if qty < 1:
                continue

            pos = {
                "signal": sig["signal"],
                "entry_price": fill,
                "initial_sl": sig["initial_sl"],
                "current_sl": sig["initial_sl"],
                "qty": qty,
                "entry_time": ts,
                "structure": sig["structure"],
            }
            n_today += 1

        tdf = pd.DataFrame(trades)
        return tdf, self.summarize(tdf)

    def summarize(self, tdf):
        if tdf is None or len(tdf) == 0:
            return {"trades": 0}
        wins, losses = tdf[tdf["pnl"] > 0], tdf[tdf["pnl"] <= 0]
        loss_sum = abs(losses["pnl"].sum())
        eq = np.concatenate([[self.cfg.capital], self.cfg.capital + tdf["pnl"].cumsum().values])
        peak = np.maximum.accumulate(eq)
        return {
            "trades": int(len(tdf)),
            "win_rate_%": round(len(wins) / len(tdf) * 100, 1),
            "avg_R": round(float(tdf["r"].mean()), 2),
            "profit_factor": round(float(wins["pnl"].sum() / loss_sum), 2) if loss_sum > 0 else float("inf"),
            "net_pnl": round(float(tdf["pnl"].sum()), 2),
            "max_drawdown": round(float((eq - peak).min()), 2),
            "exit_reasons": tdf["reason"].value_counts().to_dict(),
        }




# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# ANGEL ONE SMARTAPI NIFTY 50 MULTI-STOCK PAPER-TRADING ADAPTER (NO LIVE ORDERS)
# ----------------------------------------------------------------------------
import os
import json
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
STATE_FILE = "SMC_SECTORIAL_PAPER_STATE.json"
TRADES_FILE = "SMC_SECTORIAL_PAPER_TRADES.csv"
SCRIP_MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"

# NIFTY 50 cash-equity watchlist. Keep symbols in Angel One NSE trading-symbol format.
# Review this snapshot after official NSE index rebalances.
NIFTY50_SYMBOLS = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL",
    "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDALCO",
    "HINDUNILVR", "ICICIBANK", "INDUSINDBK", "INFY", "ITC",
    "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M",
    "MARUTI", "NESTLEIND", "NTPC", "ONGC", "POWERGRID",
    "RELIANCE", "SBIN", "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM",
    "TATASTEEL", "TCS", "TECHM", "TITAN", "TRENT",
    "ULTRACEMCO", "WIPRO", "BAJAJHLDNG", "TMPV", "ZYDUSLIFE"
]
INDEX_SYMBOL = "Nifty 50"
INDEX_TOKEN_FALLBACK = "99926000"
MAX_OPEN_POSITIONS = 5
API_CALL_PAUSE_SECONDS = 0.35


def _env_first(*names):
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def send_telegram(message):
    token = _env_first("TG_TOKEN", "TELEGRAM_BOT_TOKEN", "BOT_TOKEN")
    chat_id = _env_first("TG_CHAT_ID", "TELEGRAM_CHAT_ID", "CHAT_ID")
    if not token or not chat_id:
        print("Telegram skipped: TG_TOKEN/TG_CHAT_ID secrets are not configured.")
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            print(f"Telegram API rejected message: {payload}")
            return False
        return True
    except Exception as exc:
        print(f"Telegram delivery failed: {exc}")
        return False


def load_paper_state():
    empty = {
        "positions": {},
        "last_signal_candles": {},
        "last_managed_candles": {},
        "trade_date": "",
        "trades_today": 0,
        "daily_pnl": 0.0,
    }
    if not os.path.exists(STATE_FILE):
        return empty
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        # Migrate the previous single-RELIANCE position format without losing it.
        old_position = state.get("position")
        positions = state.get("positions") or {}
        if old_position and not positions:
            positions["RELIANCE"] = old_position
        state["positions"] = positions
        state.setdefault("last_signal_candles", {})
        state.setdefault("last_managed_candles", {})
        # Carry forward old single-symbol candle markers if present.
        if state.get("last_signal_candle"):
            state["last_signal_candles"].setdefault("RELIANCE", state["last_signal_candle"])
        if state.get("last_managed_candle"):
            state["last_managed_candles"].setdefault("RELIANCE", state["last_managed_candle"])
        state.setdefault("trade_date", "")
        state.setdefault("trades_today", 0)
        state.setdefault("daily_pnl", 0.0)
        return state
    except Exception as exc:
        raise RuntimeError(f"Paper state file is unreadable; refusing new entries: {exc}")


def save_paper_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


def angel_one_login():
    api_key = _env_first("ANGEL_API_KEY")
    client_code = _env_first("ANGEL_CLIENT_CODE")
    pin = _env_first("ANGEL_PIN")
    totp_secret = _env_first("ANGEL_TOTP_SECRET")
    missing = [name for name, value in (
        ("ANGEL_API_KEY", api_key), ("ANGEL_CLIENT_CODE", client_code),
        ("ANGEL_PIN", pin), ("ANGEL_TOTP_SECRET", totp_secret)
    ) if not value]
    if missing:
        raise RuntimeError("Missing GitHub Actions secrets/environment: " + ", ".join(missing))
    try:
        import pyotp
        from SmartApi import SmartConnect
    except ImportError as exc:
        raise RuntimeError("Install smartapi-python and pyotp before running.") from exc

    smart = SmartConnect(api_key=api_key)
    try:
        session = smart.generateSession(client_code, pin, pyotp.TOTP(totp_secret).now())
    except Exception as exc:
        raise RuntimeError(f"Angel One login request failed: {exc}") from exc
    if not session or not session.get("status") or not session.get("data"):
        message = (session or {}).get("message", "No valid session returned")
        raise RuntimeError(f"Angel One login failed: {message}")
    smart.getfeedToken()
    return smart


def load_angel_scrip_master():
    response = requests.get(SCRIP_MASTER_URL, timeout=45)
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Angel One scrip master returned no instrument rows.")
    return rows


def build_nse_token_map(rows):
    token_map = {}
    index_token = None
    for row in rows:
        if str(row.get("exch_seg", "")).upper() != "NSE":
            continue
        symbol = str(row.get("symbol", "")).upper()
        name = str(row.get("name", "")).upper()
        token = str(row.get("token", "")).strip()
        if not token:
            continue
        if symbol == "NIFTY 50" or name == "NIFTY 50":
            index_token = token
        if symbol.endswith("-EQ"):
            base = symbol[:-3]
            if base in NIFTY50_SYMBOLS:
                token_map[base] = token
    missing = [s for s in NIFTY50_SYMBOLS if s not in token_map]
    if missing:
        raise RuntimeError("Angel One scrip master is missing NIFTY 50 symbols: " + ", ".join(missing))
    return token_map, (index_token or INDEX_TOKEN_FALLBACK)


def angel_candles(smart, token, symbol, interval="FIVE_MINUTE", days=5):
    now = datetime.now(IST)
    start = now - timedelta(days=days)
    params = {
        "exchange": "NSE",
        "symboltoken": str(token),
        "interval": interval,
        "fromdate": start.strftime("%Y-%m-%d %H:%M"),
        "todate": now.strftime("%Y-%m-%d %H:%M"),
    }
    response = smart.getCandleData(params)
    rows = (response or {}).get("data")
    if not rows:
        raise RuntimeError(f"No Angel One candle data for {symbol}: {(response or {}).get('message', response)}")
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(IST)
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert(IST)
    df = df.dropna(subset=["timestamp"]).set_index("timestamp").sort_index()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    # Exclude a candle until its full five-minute interval has completed.
    cutoff = now.replace(second=0, microsecond=0)
    cutoff -= timedelta(minutes=cutoff.minute % 5)
    df = df[df.index + timedelta(minutes=5) <= cutoff]
    if df.empty:
        raise RuntimeError(f"No completed 5m candle available for {symbol}.")
    if (now - df.index[-1].to_pydatetime()) > timedelta(minutes=10):
        raise RuntimeError(f"Stale Angel One candle data for {symbol}; latest={df.index[-1]}")
    if len(df) < 60:
        raise RuntimeError(f"Insufficient completed 5m candles for {symbol}: {len(df)}")
    return df


def append_paper_trade(record):
    pd.DataFrame([record]).to_csv(
        TRADES_FILE, mode="a", header=not os.path.exists(TRADES_FILE), index=False
    )


def _close_paper_position(state, symbol, pos, candle_time, result, cfg):
    sign = 1 if pos["signal"] == "LONG" else -1
    exit_price = float(result["execution_price"])
    qty = int(pos["qty"])
    gross = sign * (exit_price - float(pos["entry_price"])) * qty
    costs = (cfg.cost_pct_per_side / 100.0) * (float(pos["entry_price"]) + exit_price) * qty
    pnl = gross - costs
    state["daily_pnl"] = float(state.get("daily_pnl", 0.0)) + pnl
    append_paper_trade({
        "entry_time": pos["entry_time"], "exit_time": candle_time,
        "symbol": symbol, "side": pos["signal"], "qty": qty,
        "entry": pos["entry_price"], "exit": exit_price,
        "pnl": round(pnl, 2), "reason": result["reason"], "mode": "PAPER_ONLY"
    })
    close_side = "SELL" if pos["signal"] == "LONG" else "BUY"
    sent = send_telegram(
        f"ANGEL ONE NIFTY50 SMC PAPER EXIT | {symbol} | {close_side}\n"
        f"Entry: {float(pos['entry_price']):.2f} | Exit: {exit_price:.2f}\n"
        f"Qty: {qty} | P&L: {pnl:+.2f} INR\n"
        f"Reason: {result['reason']}\nMode: PAPER ONLY (NO LIVE ORDER)"
    )
    print(f"{symbol}: paper exit {result['reason']} P&L={pnl:.2f} INR; telegram={sent}")
    state["positions"].pop(symbol, None)
    save_paper_state(state)


def run_angel_one_paper():
    # Send a Telegram confirmation only when the workflow is started manually.
    # Scheduled five-minute scans do not generate test-message spam.
    if os.getenv("GITHUB_EVENT_NAME", "").strip() == "workflow_dispatch":
        test_message = (
            "ANGEL ONE NIFTY50 SMC PAPER | TELEGRAM TEST SUCCESS\n"
            "GitHub Actions manual test reached the Telegram send step.\n"
            "Mode: PAPER ONLY (NO LIVE ORDER)"
        )
        if not send_telegram(test_message):
            raise RuntimeError(
                "Telegram test failed. Check TG_TOKEN/TELEGRAM_BOT_TOKEN/BOT_TOKEN "
                "and TG_CHAT_ID/TELEGRAM_CHAT_ID/CHAT_ID GitHub Actions secrets."
            )
        print("Telegram manual test message delivered successfully.")
    now = datetime.now(IST)
    if now.weekday() >= 5 or not (dtime(9, 15) <= now.time() <= dtime(15, 30)):
        print(f"Outside NSE session; no trade action. IST={now.isoformat()}")
        return

    smart = angel_one_login()
    print("Downloading Angel One NSE scrip master...")
    scrip_rows = load_angel_scrip_master()
    token_map, index_token = build_nse_token_map(scrip_rows)
    print(f"Validated {len(token_map)}/{len(NIFTY50_SYMBOLS)} NIFTY 50 equity tokens.")

    # NIFTY 50 index is the market-direction filter; all 50 constituents are tradable paper symbols.
    index_df = angel_candles(smart, index_token, INDEX_SYMBOL)
    index_df = add_indicators(index_df)
    strategy = SMCSectorialStrategy(Config())
    market_filters = strategy.analyze_market_filters(index_df)
    print(f"NIFTY 50 market bias: {market_filters['market_bias']}")

    state = load_paper_state()
    today = now.date().isoformat()
    if state["trade_date"] != today:
        state["trade_date"] = today
        state["trades_today"] = 0
        state["daily_pnl"] = 0.0

    # First manage existing paper positions. A failure on one symbol does not stop the other 49.
    for symbol, pos in list(state["positions"].items()):
        try:
            df = angel_candles(smart, token_map[symbol], symbol)
            df = add_indicators(df)
            candle_time = df.index[-1].isoformat()
            if state["last_managed_candles"].get(symbol) == candle_time and now.time() < strategy.cfg.square_off_time:
                print(f"{symbol}: latest candle already managed.")
                continue
            state["last_managed_candles"][symbol] = candle_time
            save_paper_state(state)
            row = df.iloc[-1]
            candle = {k: float(row[k]) for k in ("open", "high", "low", "close")}
            result = strategy.process_step_lock_trailing_sl(candle, pos, bb_mid=float(row["bb_mid"]))
            if now.time() >= strategy.cfg.square_off_time:
                result = {"action": "EXIT", "execution_price": float(row["close"]), "reason": "Intraday square-off"}
            if result["action"] == "TRAIL_LOCK":
                pos["current_sl"] = float(result["new_sl"])
                state["positions"][symbol] = pos
                save_paper_state(state)
                print(f"{symbol}: trailing SL updated to {pos['current_sl']:.2f}")
            elif result["action"] == "EXIT":
                _close_paper_position(state, symbol, pos, candle_time, result, strategy.cfg)
            else:
                print(f"{symbol}: open paper position; no HOLD Telegram sent.")
            time.sleep(API_CALL_PAUSE_SECONDS)
        except Exception as exc:
            print(f"{symbol}: position-management error: {exc}")

    if now.time() >= strategy.cfg.last_entry_time:
        print("Entry window is closed; existing paper positions were managed.")
        return

    if int(state.get("trades_today", 0)) >= strategy.cfg.max_trades_per_day:
        print("Daily paper-trade limit reached.")
        return
    if float(state.get("daily_pnl", 0.0)) <= -strategy.cfg.capital * strategy.cfg.max_daily_loss_pct / 100:
        print("Daily paper loss limit reached; no new entries.")
        return
    if len(state["positions"]) >= MAX_OPEN_POSITIONS:
        print(f"Maximum open paper positions reached: {MAX_OPEN_POSITIONS}.")
        return
    if market_filters["market_bias"] == "NEUTRAL":
        print("NIFTY 50 market bias is NEUTRAL; no entries.")
        return

    # Scan each NIFTY 50 constituent independently; strategy rules are unchanged.
    scanned = 0
    signals = 0
    errors = 0
    for symbol in NIFTY50_SYMBOLS:
        if symbol in state["positions"]:
            continue
        if int(state.get("trades_today", 0)) >= strategy.cfg.max_trades_per_day:
            break
        if len(state["positions"]) >= MAX_OPEN_POSITIONS:
            break
        try:
            df = angel_candles(smart, token_map[symbol], symbol)
            df = add_indicators(df)
            scanned += 1
            candle_time = df.index[-1].isoformat()
            if state["last_signal_candles"].get(symbol) == candle_time:
                time.sleep(API_CALL_PAUSE_SECONDS)
                continue
            state["last_signal_candles"][symbol] = candle_time
            save_paper_state(state)

            window = df.tail(strategy.cfg.history_window)
            signal = strategy.check_entry_signal_5m(window, market_filters)
            if not signal:
                print(f"{symbol}: no signal ({strategy.last_reject})")
                time.sleep(API_CALL_PAUSE_SECONDS)
                continue

            entry = float(signal["entry_price"])
            sl = float(signal["initial_sl"])
            qty = strategy.position_size(entry, sl)
            if qty < 1:
                print(f"{symbol}: signal rejected; calculated paper quantity below one.")
                time.sleep(API_CALL_PAUSE_SECONDS)
                continue

            pos = {
                "signal": signal["signal"], "entry_price": entry,
                "initial_sl": sl, "current_sl": sl, "qty": int(qty),
                "entry_time": candle_time, "structure": signal["structure"]
            }
            state["positions"][symbol] = pos
            state["trades_today"] = int(state.get("trades_today", 0)) + 1
            save_paper_state(state)
            action = "BUY" if signal["signal"] == "LONG" else "SELL"
            sent = send_telegram(
                f"ANGEL ONE NIFTY50 SMC PAPER {action} | {symbol}\n"
                f"Entry: {entry:.2f} | SL: {sl:.2f} | Qty: {qty}\n"
                f"Structure: {signal['structure']} | RSI: {signal['rsi']:.1f}\n"
                f"NIFTY 50 Bias: {market_filters['market_bias']} | POC: {signal['poc']:.2f}\n"
                f"Mode: PAPER ONLY (NO LIVE ORDER)"
            )
            signals += 1
            print(f"{symbol}: paper {action} entry={entry:.2f}, SL={sl:.2f}, qty={qty}; telegram={sent}")
            time.sleep(API_CALL_PAUSE_SECONDS)
        except Exception as exc:
            errors += 1
            print(f"{symbol}: scan error: {exc}")
            time.sleep(API_CALL_PAUSE_SECONDS)

    print(
        f"NIFTY50 scan complete: universe={len(NIFTY50_SYMBOLS)}, "
        f"scanned={scanned}, new_signals={signals}, errors={errors}, "
        f"open_positions={len(state['positions'])}, trades_today={state['trades_today']}."
    )


if __name__ == "__main__":
    run_angel_one_paper()
