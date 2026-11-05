# -*- coding: utf-8 -*-
"""
Smart Money Concepts (SMC) + Volume Profile Intraday Strategy  -- v2 (improved)

Kya naya hai (v1 se):
  1. Swing-based structure break (CHoCH / BOS) -- sirf "pichle 5 highs" nahi
  2. Proper Order Block: displacement se theek pehle ki last opposite candle
  3. Volume-weighted POC (sirf aaj ke session par, candle ke high-low range mein volume distribute)
  4. Retest entry (candle close par confirm), breakout ke top par entry nahi
  5. RSI + Bollinger Band + VWAP momentum/volatility filter
  6. ATR-based stop loss + min/max risk filter
  7. Step-lock trailing: 1R breakeven -> 2R/3R/4R lock -> 5R exit (candle high/low par check)
  8. Market bias: Sensex (change% + EMA20) with NEUTRAL zone, sector relative strength (top-N)
  9. Risk management: position sizing, daily max loss, max trades, time filter, 3:15 square-off
 10. Built-in backtest loop: win rate, profit factor, avg R, max drawdown (costs + slippage ke saath)

Data format: DataFrame with columns open, high, low, close, volume.
Index DatetimeIndex (IST ya tz-aware) ho to time filters / session logic chalte hain.
"""

from dataclasses import dataclass
from datetime import time as dtime

import numpy as np
import pandas as pd


# ----------------------------------------------------------------------------
# CONFIG -- saare parameters ek jagah (backtest karke yahin tune karo)
# ----------------------------------------------------------------------------
@dataclass
class Config:
    # Indicators
    rsi_len: int = 14
    bb_len: int = 20
    bb_std: float = 2.0
    atr_len: int = 14
    bb_width_window: int = 50
    min_width_quantile: float = 0.20      # isse kam BB width = dead market, entry nahi

    # Structure / Order Block
    swing_n: int = 2                      # fractal swing: n candles left & right
    break_lookback: int = 15              # structure break pichle itne candles ke andar hona chahiye
    allow_bos: bool = True                # False = sirf asli CHoCH (reversal) allow
    min_session_bars: int = 12

    # Volume profile / confluence
    vp_bins: int = 24
    poc_tol_atr: float = 0.5              # POC, OB zone se itne ATR door tak chalega
    retest_touch_atr: float = 0.25

    # Stop loss
    sl_atr_mult: float = 0.5
    min_risk_pct: float = 0.15            # risk entry price ka kam se kam itna %
    max_risk_pct: float = 1.00            # isse zyada risk = trade skip

    # RSI zones (momentum filter)
    rsi_long: tuple = (52, 75)
    rsi_short: tuple = (25, 48)
    use_vwap_filter: bool = True
    use_bb_filter: bool = True

    # Market filter
    neutral_band_pct: float = 0.10        # Sensex +-0.10% ke andar = NEUTRAL (no trade)
    top_n_sectors: int = 2

    # Trailing (R multiples)
    be_trigger_r: float = 1.0             # 1R par breakeven
    be_buffer_r: float = 0.10             # breakeven + thoda buffer (cost cover)
    lock_offset_r: float = 1.0            # 1.0 = 2R par SL 1R; 0.0 = original v1 (2R par SL 2R)
    final_target_r: float = 5.0
    use_bb_trail: bool = True             # 2R ke baad SL ko BB middle band tak tighten

    # Time filter (IST)
    no_trade_before: dtime = dtime(9, 30)
    last_entry_time: dtime = dtime(15, 0)
    square_off_time: dtime = dtime(15, 15)

    # Risk management
    capital: float = 100000.0
    risk_per_trade_pct: float = 0.5
    max_trades_per_day: int = 3
    max_daily_loss_pct: float = 2.0
    leverage: float = 5.0

    # Costs (per side)
    cost_pct_per_side: float = 0.03
    slippage_pct: float = 0.02

    # Backtest
    history_window: int = 160


# ----------------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------------
def _ist_time(ts):
    if not isinstance(ts, pd.Timestamp):
        return None
    if ts.tzinfo is not None:
        ts = ts.tz_convert("Asia/Kolkata")
    return ts.time()


def _session(df):
    """Sirf last candle wale din ka data."""
    if isinstance(df.index, pd.DatetimeIndex):
        d = df.index.normalize()
        return df[d == d[-1]]
    return df


def _mirror(df):
    """Price negate karke bearish setup ko bullish logic se detect karne ke liye."""
    return pd.DataFrame(
        {"open": -df["open"], "high": -df["low"], "low": -df["high"], "close": -df["close"]},
        index=df.index,
    )


def add_indicators(df, cfg=None):
    """RSI (Wilder), Bollinger Bands, ATR, session VWAP."""
    cfg = cfg or Config()
    df = df.copy()
    close = df["close"]

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / cfg.rsi_len, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / cfg.rsi_len, adjust=False).mean()
    rsi = np.where(loss == 0, 100.0, 100 - 100 / (1 + gain / loss.replace(0, np.nan)))
    df["rsi"] = pd.Series(rsi, index=df.index).where(gain.notna(), np.nan).fillna(50.0)

    df["bb_mid"] = close.rolling(cfg.bb_len).mean()
    sd = close.rolling(cfg.bb_len).std()
    df["bb_up"] = df["bb_mid"] + cfg.bb_std * sd
    df["bb_lo"] = df["bb_mid"] - cfg.bb_std * sd
    df["bb_width"] = (df["bb_up"] - df["bb_lo"]) / df["bb_mid"]

    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - close.shift()).abs(), (df["low"] - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / cfg.atr_len, adjust=False).mean()

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
    """Volume-weighted profile: har candle ka volume uske high-low range ke bins mein baanta jaata hai.
    Returns (poc, vah, val)."""
    lo = float(session_df["low"].min())
    hi = float(session_df["high"].max())
    if hi <= lo:
        return hi, hi, lo
    edges = np.linspace(lo, hi, bins + 1)
    prof = np.zeros(bins)
    for h, l, v in zip(session_df["high"].values, session_df["low"].values, session_df["volume"].values):
        a = int(np.clip(np.searchsorted(edges, l, side="right") - 1, 0, bins - 1))
        b = int(np.clip(np.searchsorted(edges, h, side="left") - 1, 0, bins - 1))
        if b < a:
            b = a
        prof[a:b + 1] += v / (b - a + 1)

    pi = int(np.argmax(prof))
    poc = (edges[pi] + edges[pi + 1]) / 2

    # 70% value area
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
# STRATEGY
# ----------------------------------------------------------------------------
class SMCSectorialStrategy:
    def __init__(self, config=None):
        self.cfg = config or Config()
        self.active_positions = {}
        self.last_reject = ""          # debug: signal kyun nahi aaya

    # ---------------- Phase 1: Market filters ----------------
    def analyze_market_filters(self, sensex_df, sectoral_data=None):
        """Sensex bias (BULLISH / BEARISH / NEUTRAL) + sectoral relative strength (vs Sensex)."""
        cfg = self.cfg
        sess = _session(sensex_df)
        s_open = float(sess["open"].iloc[0])
        s_cur = float(sess["close"].iloc[-1])
        change_pct = (s_cur / s_open - 1) * 100

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
            "top_gaining_sector": None,
            "top_losing_sector": None,
        }
        if sectoral_data:
            rs = {k: v - change_pct for k, v in sectoral_data.items()}     # relative strength
            gainers = [k for k, v in sorted(rs.items(), key=lambda x: -x[1]) if v > 0][: cfg.top_n_sectors]
            losers = [k for k, v in sorted(rs.items(), key=lambda x: x[1]) if v < 0][: cfg.top_n_sectors]
            out.update(
                top_gaining_sectors=gainers,
                top_losing_sectors=losers,
                top_gaining_sector=max(sectoral_data, key=sectoral_data.get),
                top_losing_sector=min(sectoral_data, key=sectoral_data.get),
                sector_rs=rs,
            )
        return out

    # ---------------- Phase 2: Volume profile ----------------
    def calculate_volume_profile_poc(self, df_5m, bins=None):
        poc, _, _ = volume_profile(_session(df_5m), bins or self.cfg.vp_bins)
        return poc

    # ---------------- Structure + Order Block ----------------
    def _swings(self, h, l):
        n = self.cfg.swing_n
        highs, lows = [], []
        for i in range(n, len(h) - n):
            if h[i] > h[i - n:i].max() and h[i] >= h[i + 1:i + n + 1].max():
                highs.append((i, h[i]))
            if l[i] < l[i - n:i].min() and l[i] <= l[i + 1:i + n + 1].min():
                lows.append((i, l[i]))
        return highs, lows

    def _find_bullish_setup(self, df, atr, poc):
        """Bullish orientation mein: structure break + order block + retest. (Short ke liye mirrored df aata hai.)"""
        cfg = self.cfg
        n = cfg.swing_n
        o, h, l, c = (df[k].values for k in ("open", "high", "low", "close"))
        N = len(df)
        highs, lows = self._swings(h, l)
        if not highs:
            self.last_reject = "no swing high"
            return None

        found = None
        for brk in range(N - 2, max(N - 1 - cfg.break_lookback, n) - 1, -1):
            for si, sp in reversed(highs):
                if si + n >= brk:
                    continue
                if si + 1 < brk and c[si + 1:brk].max() > sp:
                    continue                       # ye swing high pehle hi toot chuka
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
            self.last_reject = "BOS skipped (allow_bos=False)"
            return None

        # Order block: displacement leg ke base ki last bearish candle
        leg_low = si + int(np.argmin(l[si:brk + 1]))
        ob = None
        for j in range(leg_low, brk):
            if c[j] < o[j] and c[j + 1] > h[j]:
                ob = j
                break
        if ob is None:
            for j in range(leg_low, max(leg_low - 4, -1), -1):
                if c[j] < o[j]:
                    ob = j
                    break
        if ob is None:
            self.last_reject = "no order block"
            return None
        ob_low, ob_high = l[ob], h[ob]
        ob_mid = (ob_low + ob_high) / 2

        # OB invalidate: break ke baad close OB low ke neeche gaya
        if c[brk:N].min() <= ob_low:
            self.last_reject = "OB invalidated"
            return None

        # POC confluence (tolerance ke saath)
        tol = cfg.poc_tol_atr * atr
        if not (ob_low - tol <= poc <= ob_high + tol):
            self.last_reject = "POC not near OB"
            return None

        # Retest + rejection candle (last closed candle)
        start = max(brk + 1, N - 3)
        if l[start:].min() > ob_high + cfg.retest_touch_atr * atr:
            self.last_reject = "no retest of OB yet"
            return None
        if not (c[-1] > ob_mid and c[-1] > o[-1]):
            self.last_reject = "no rejection candle"
            return None

        return {"kind": kind, "brk": brk, "level": level, "ob_low": ob_low, "ob_high": ob_high}

    # ---------------- Momentum (RSI + BB + VWAP) ----------------
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
            ok = cfg.rsi_long[0] <= r <= cfg.rsi_long[1] and r > pr
            if not ok:
                self.last_reject = f"RSI {r:.1f} not in long zone/rising"
                return False
            if cfg.use_bb_filter and not (mid < c <= up):
                self.last_reject = "price not between BB mid and upper"
                return False
            if cfg.use_vwap_filter and not np.isnan(vwap) and c < vwap:
                self.last_reject = "price below VWAP"
                return False
        else:
            ok = cfg.rsi_short[0] <= r <= cfg.rsi_short[1] and r < pr
            if not ok:
                self.last_reject = f"RSI {r:.1f} not in short zone/falling"
                return False
            if cfg.use_bb_filter and not (lo <= c < mid):
                self.last_reject = "price not between BB lower and mid"
                return False
            if cfg.use_vwap_filter and not np.isnan(vwap) and c > vwap:
                self.last_reject = "price above VWAP"
                return False
        return True

    # ---------------- Phase 2 & 3: Entry signal ----------------
    def check_entry_signal_5m(self, df_5m, market_filters, stock_sector=None):
        cfg = self.cfg
        self.last_reject = ""
        df = df_5m if "atr" in df_5m.columns else add_indicators(df_5m, cfg)
        if len(df) < 3:
            self.last_reject = "not enough data"
            return None

        # Time filter
        t = _ist_time(df.index[-1])
        if t is not None and not (cfg.no_trade_before <= t <= cfg.last_entry_time):
            self.last_reject = "outside trading window"
            return None

        bias = market_filters["market_bias"]
        if bias == "NEUTRAL":
            self.last_reject = "market NEUTRAL"
            return None
        side = "LONG" if bias == "BULLISH" else "SHORT"

        # Sector filter
        if stock_sector is not None and market_filters.get("sector_data_available"):
            pool = market_filters["top_gaining_sectors"] if side == "LONG" else market_filters["top_losing_sectors"]
            if stock_sector not in pool:
                self.last_reject = "sector not in top-N for this side"
                return None

        sess = _session(df)
        if len(sess) < cfg.min_session_bars:
            self.last_reject = "session too young"
            return None
        last = df.iloc[-1]
        if last[["rsi", "bb_mid", "atr"]].isna().any():
            self.last_reject = "indicators warming up"
            return None

        atr = float(last["atr"])
        poc, vah, val = volume_profile(sess, cfg.vp_bins)

        # Structure + OB + retest (bullish orientation; short = mirror)
        if side == "LONG":
            setup = self._find_bullish_setup(sess, atr, poc)
        else:
            setup = self._find_bullish_setup(_mirror(sess), atr, -poc)
        if setup is None:
            return None

        if not self._momentum_filter(df, side):
            return None

        entry = float(last["close"])
        if side == "LONG":
            ob_low, ob_high = setup["ob_low"], setup["ob_high"]
            sl = ob_low - cfg.sl_atr_mult * atr
        else:
            ob_low, ob_high = -setup["ob_high"], -setup["ob_low"]
            sl = ob_high + cfg.sl_atr_mult * atr

        risk = abs(entry - sl)
        risk_pct = risk / entry * 100
        if risk_pct > cfg.max_risk_pct:
            self.last_reject = f"risk {risk_pct:.2f}% too large"
            return None
        if risk_pct < cfg.min_risk_pct:
            risk = entry * cfg.min_risk_pct / 100
            sl = entry - risk if side == "LONG" else entry + risk

        return {
            "signal": side,
            "entry_price": entry,
            "initial_sl": sl,
            "poc": poc,
            "vah": vah,
            "val": val,
            "ob_low": ob_low,
            "ob_high": ob_high,
            "structure": setup["kind"],
            "rsi": float(last["rsi"]),
            "atr": atr,
            "risk_pct": risk / entry * 100,
        }

    # ---------------- Position sizing ----------------
    def position_size(self, entry, sl):
        cfg = self.cfg
        risk_per_share = abs(entry - sl)
        if risk_per_share <= 0:
            return 0
        qty_by_risk = (cfg.capital * cfg.risk_per_trade_pct / 100) / risk_per_share
        qty_by_margin = cfg.capital * cfg.leverage / entry
        return int(min(qty_by_risk, qty_by_margin))

    # ---------------- Phase 3: Step-lock trailing SL ----------------
    def process_step_lock_trailing_sl(self, candle, position, bb_mid=None, exhausted=False):
        """
        candle: dict(open, high, low, close) ya sirf price (float).
        Flow: 1R -> breakeven, 2R/3R/4R -> SL lock (lock_offset_r ke hisaab se), 5R -> full exit.
        lock_offset_r=1.0 : 2R par SL 1R, 3R par 2R, 4R par 3R   (default, noise se bachata hai)
        lock_offset_r=0.0 : original v1 -- 2R par SL seedha 2R (price ke bilkul paas)
        Stop check candle ke low/high par hota hai, candle shuru hone wale SL ke against (conservative).
        """
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

        # 1) Stop loss / trailing SL hit (gap ho to open par fill)
        if sign * (adverse - cur_sl) <= 0:
            px = candle["open"] if sign * (candle["open"] - cur_sl) <= 0 else cur_sl
            locked = sign * (cur_sl - entry) >= 0
            return {"action": "EXIT", "execution_price": px,
                    "reason": "Trailing SL Hit" if locked else "Stop Loss Hit"}

        # 2) Final target
        target = entry + sign * cfg.final_target_r * risk
        if sign * (favorable - target) >= 0:
            return {"action": "EXIT", "execution_price": target,
                    "reason": f"Target 1:{cfg.final_target_r:g} Fully Booked"}

        # 3) Step locks
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
                reason = "SL moved to breakeven" if lock_r <= cfg.be_buffer_r else f"SL Stepped & Locked at {lock_r:g}R"

        # 4) Optional BB-mid trail (sirf tighten, kabhi loosen nahi)
        if cfg.use_bb_trail and bb_mid is not None and not np.isnan(bb_mid) and r_reached >= 2:
            if sign * (bb_mid - new_sl) > 0 and sign * (candle["close"] - bb_mid) > 0:
                new_sl, reason = bb_mid, "SL trailed to BB middle band"

        # 5) Exhaustion (price band ke bahar + RSI extreme): SL candle ke extreme tak
        if exhausted and r_reached >= 1:
            cand = candle["low"] if sign == 1 else candle["high"]
            if sign * (cand - new_sl) > 0 and sign * (candle["close"] - cand) > 0:
                new_sl, reason = cand, "Exhaustion: SL tightened to candle extreme"

        if new_sl != cur_sl:
            return {"action": "TRAIL_LOCK", "new_sl": new_sl, "reason": reason}
        return {"action": "HOLD", "current_sl": cur_sl}

    # ---------------- Backtest ----------------
    def _close_trade(self, pos, exit_px, ts, reason):
        cfg = self.cfg
        sign = 1 if pos["signal"] == "LONG" else -1
        exit_fill = exit_px * (1 - sign * cfg.slippage_pct / 100)
        qty = pos["qty"]
        gross = sign * (exit_fill - pos["entry_price"]) * qty
        costs = cfg.cost_pct_per_side / 100 * (pos["entry_price"] + exit_fill) * qty
        pnl = gross - costs
        risk_amt = abs(pos["entry_price"] - pos["initial_sl"]) * qty
        return {
            "entry_time": pos["entry_time"], "exit_time": ts, "side": pos["signal"],
            "structure": pos["structure"], "entry": pos["entry_price"], "exit": exit_fill,
            "qty": qty, "pnl": pnl, "r": pnl / risk_amt if risk_amt else 0.0, "reason": reason,
        }

    def backtest(self, df_5m, sensex_df, stock_sector=None, sector_provider=None):
        """
        df_5m / sensex_df: DatetimeIndex wale 5m OHLCV (same timestamps).
        sector_provider: optional callable(ts) -> {"IT": 1.2, "BANK": -0.3, ...} (sector % change).
        Returns (trades_df, stats_dict).
        """
        cfg = self.cfg
        df = add_indicators(df_5m, cfg)
        trades, pos = [], None
        cur_day, n_today, pnl_today = None, 0, 0.0

        for i in range(len(df)):
            ts = df.index[i]
            row = df.iloc[i]
            if ts.date() != cur_day:
                cur_day, n_today, pnl_today = ts.date(), 0, 0.0
            t = _ist_time(ts)

            # ---- open position manage ----
            if pos is not None:
                candle = {k: float(row[k]) for k in ("open", "high", "low", "close")}
                closed = None
                if t >= cfg.square_off_time:
                    closed = (candle["close"], "Square-off 3:15")
                else:
                    ex = ((pos["signal"] == "LONG" and row["close"] >= row["bb_up"] and row["rsi"] >= 80) or
                          (pos["signal"] == "SHORT" and row["close"] <= row["bb_lo"] and row["rsi"] <= 20))
                    res = self.process_step_lock_trailing_sl(candle, pos, bb_mid=row["bb_mid"], exhausted=bool(ex))
                    if res["action"] == "EXIT":
                        closed = (res["execution_price"], res["reason"])
                    elif res["action"] == "TRAIL_LOCK":
                        pos["current_sl"] = res["new_sl"]
                if closed:
                    tr = self._close_trade(pos, closed[0], ts, closed[1])
                    trades.append(tr)
                    pnl_today += tr["pnl"]
                    pos = None
                continue

            # ---- new entry ----
            if n_today >= cfg.max_trades_per_day or pnl_today <= -cfg.capital * cfg.max_daily_loss_pct / 100:
                continue
            if t is not None and t >= cfg.last_entry_time:
                continue

            window = df.iloc[max(0, i - cfg.history_window + 1): i + 1]
            sx = sensex_df.loc[:ts].iloc[-cfg.history_window:]
            if sx.empty:
                continue
            sectors = sector_provider(ts) if sector_provider else None
            filters = self.analyze_market_filters(sx, sectors)
            sig = self.check_entry_signal_5m(window, filters, stock_sector)
            if not sig:
                continue

            sign = 1 if sig["signal"] == "LONG" else -1
            fill = sig["entry_price"] * (1 + sign * cfg.slippage_pct / 100)
            qty = self.position_size(fill, sig["initial_sl"])
            if qty < 1:
                continue
            pos = {"signal": sig["signal"], "entry_price": fill, "initial_sl": sig["initial_sl"],
                   "current_sl": sig["initial_sl"], "qty": qty, "entry_time": ts,
                   "structure": sig["structure"]}
            n_today += 1

        if pos is not None:
            tr = self._close_trade(pos, float(df["close"].iloc[-1]), df.index[-1], "End of data")
            trades.append(tr)

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
# DEMO (synthetic data -- sirf code chalane ke liye, asli results nahi)
# ----------------------------------------------------------------------------
def make_synthetic(days=25, seed=11):
    rng = np.random.default_rng(seed)
    idx, s_close, k_close = [], [], []
    s_px, k_px = 75000.0, 1500.0
    for d in pd.bdate_range(end="2026-11-04", periods=days):
        stamps = pd.date_range(d + pd.Timedelta(hours=9, minutes=15), periods=75, freq="5min")
        drift = rng.normal(0, 0.00025)                     # din ka trend
        ar = 0.0
        for _ in stamps:
            ar = 0.7 * ar + rng.normal(0, 0.0004)
            s_ret = drift + ar
            k_ret = 1.1 * s_ret + rng.normal(0, 0.0004)
            s_px *= 1 + s_ret
            k_px *= 1 + k_ret
            s_close.append(s_px)
            k_close.append(k_px)
        idx += list(stamps)

    def ohlc(closes, vol_base):
        c = np.array(closes)
        o = np.r_[c[0], c[:-1]]
        spread = np.abs(rng.normal(0, 0.0004, len(c))) * c
        return pd.DataFrame({
            "open": o, "high": np.maximum(o, c) + spread, "low": np.minimum(o, c) - spread,
            "close": c, "volume": rng.lognormal(np.log(vol_base), 0.5, len(c)),
        }, index=pd.DatetimeIndex(idx))

    return ohlc(k_close, 20000), ohlc(s_close, 1000)


if __name__ == "__main__":
    strategy = SMCSectorialStrategy()
    print("--- SMC + Volume Profile + BB/RSI Framework v2 ---\n")

    # 1) Market filter demo
    sensex_mock = pd.DataFrame({"open": [75000], "close": [75250]})
    sectors_mock = {"IT": 1.8, "BANK": 0.5, "AUTO": -1.2}
    f = strategy.analyze_market_filters(sensex_mock, sectors_mock)
    print(f"Market bias: {f['market_bias']} (Sensex {f['sensex_change_pct']:+.2f}%)")
    print(f"Long ke liye top sectors (RS vs Sensex): {f['top_gaining_sectors']}")
    print(f"Short ke liye bottom sectors: {f['top_losing_sectors']}\n")

    # 2) Trailing demo (LONG, entry 100, SL 99 => 1R = 1.0)
    pos = {"signal": "LONG", "entry_price": 100.0, "initial_sl": 99.0, "current_sl": 99.0}
    print("Trailing demo (1R = 1.0):")
    for px in [101.1, 102.2, 103.2, 104.2, 105.1]:
        r = strategy.process_step_lock_trailing_sl(px, pos)
        print(f"  price {px}: {r['action']} -> {r.get('reason', '')}")
        if r["action"] == "TRAIL_LOCK":
            pos["current_sl"] = r["new_sl"]
            print(f"     current SL = {pos['current_sl']:.2f}")
    print()

    # 3) Backtest demo -- synthetic data par (sirf pipeline check)
    stock, sensex = make_synthetic()
    trades, stats = strategy.backtest(stock, sensex)
    print("Backtest (SYNTHETIC data, sector filter off):")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    if len(trades):
        print("\nPehle 5 trades:")
        print(trades[["entry_time", "side", "structure", "entry", "exit", "qty", "pnl", "r", "reason"]].head().to_string(index=False))
    print("\nNote: asli NSE 5m data (Sensex + stock) daal kar hi parameters tune karo.")
