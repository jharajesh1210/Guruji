# -*- coding: utf-8 -*-
"""
Smart Money Concepts (SMC) + Volume Profile Intraday Strategy -- v3 (Fully Fixed)

Bug Fixes & Refinements in v3:
  1. Fixed Swing & Structure Break Search Logic: Corrected reverse iteration bug.
  2. Safe Volume Profile Bin Allocation: Clamped index bounds to prevent IndexError/KeyError on edge values.
  3. Strict Timestamp Alignment: Integrated robust timestamp-sync for Stock & Sensex data during backtesting.
  4. Symmetric Bearish Logic: Replaced mirror dataframe trick with dedicated bearish setup logic for accurate indicator scaling.
  5. Enhanced Position Sizing & Frictional Cost Accounting.
"""

from dataclasses import dataclass
from datetime import time as dtime
import numpy as np
import pandas as pd


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

    # 70% Value Area
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
