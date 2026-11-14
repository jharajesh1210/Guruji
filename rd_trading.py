import os
import sys
import time as _time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, time

# ---------------- Config ----------------
PUB = "https://public.coindcx.com"
CANDLE_URL = PUB + "/market_data/candlesticks"
PRICE_URL = PUB + "/market_data/v3/current_prices/futures/rt"
MIN_24H_VOLUME_USDT = float(os.getenv("MIN_24H_VOLUME_USDT", "10000000"))  # 10 million USDT
CANDLE_BARS = int(os.getenv("CANDLE_BARS", "300"))  # 15m bars fetched per coin
MAX_TICKER_AGE_SEC = int(os.getenv("MAX_TICKER_AGE_SEC", "600"))  # ignore stale/delisted tickers
MAX_NEW_ENTRIES_PER_CYCLE = int(os.getenv("MAX_NEW_ENTRIES_PER_CYCLE", "5"))  # cap new entries per cycle
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "5"))  # cap on trades open at the same time
BARS_PER_24H = 96  # 15m candles in 24 hours
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "RD-TRADING-SCANNER/1.0"})

def _px(x):
    """Round a price to 6 significant digits (2 decimals hid prices like 0.0204 as 0.02)."""
    return float(f"{float(x):.6g}")


def assign_session(timestamp):
    """
    Assigns trading session based on IST time:
    - Asian Session: 05:30 to 13:30
    - London Session: 13:30 to 18:30
    - NY Session: 18:30 to 23:30 (or close)
    """
    t = pd.to_datetime(timestamp).time()
    if time(5, 30) <= t < time(13, 30):
        return 'Asian'
    elif time(13, 30) <= t < time(18, 30):
        return 'London'
    elif time(18, 30) <= t or t < time(5, 30):
        return 'NY'
    return 'Other'

def is_high_impact_news_time(timestamp, news_events, buffer_minutes=30):
    """
    Checks if timestamp is within buffer_minutes before or after high-impact news (FOMC, CPI).
    """
    if not news_events:
        return False
    ts = pd.to_datetime(timestamp)
    for event_time in news_events:
        ev_ts = pd.to_datetime(event_time)
        if abs((ts - ev_ts).total_seconds()) / 60.0 <= buffer_minutes:
            return True
    return False

def manage_open_trade(active_trade, current_candle, current_session_pdh, current_session_pdl, news_restricted):
    """
    Manages active/open trades:
    1. Holds safely during High-Impact News.
    2. Exits when next session's independent PDH/PDL is hit/broken (for trades carried over across sessions).
    3. Manages 1:2 to 1:5 Dynamic Profit Locking with 100 USDT capital @ 30X leverage.
    """
    if not active_trade:
        return "No_Active_Trade", 0.0, None

    direction = active_trade['direction']
    entry_price = active_trade['entry_price']
    sl = active_trade['sl']
    # BUG FIX: risk must stay the ORIGINAL entry->SL distance. Recomputing it from the moved
    # (locked) SL doubled the risk after a lock, which doubled PnL and delayed 1:3/1:4/1:5 locks.
    risk_per_unit = active_trade.get('risk') or abs(entry_price - sl)
    
    close_price = current_candle['Close']
    high_price = current_candle['High']
    low_price = current_candle['Low']
    
    if news_restricted:
        return "News_Restricted_Hold", 0.0, active_trade

    # Check if Stop Loss is hit first
    if direction == "BUY" and low_price <= sl:
        # Check if we had already locked a higher RR tier
        locked_rr = active_trade.get('locked_rr', 0.0)
        result_rr = locked_rr if locked_rr > 0 else -1.0  # SL without lock = full -1R loss
        position_size_usdt = 100 * 30
        pnl = (position_size_usdt / entry_price) * (result_rr * risk_per_unit)
        status = f"SL_Hit_Exit_Locked_1:{locked_rr}" if locked_rr > 0 else "Stop_Loss_Hit"
        return status, pnl, None
    elif direction == "SELL" and high_price >= sl:
        locked_rr = active_trade.get('locked_rr', 0.0)
        result_rr = locked_rr if locked_rr > 0 else -1.0  # SL without lock = full -1R loss
        position_size_usdt = 100 * 30
        pnl = (position_size_usdt / entry_price) * (result_rr * risk_per_unit)
        status = f"SL_Hit_Exit_Locked_1:{locked_rr}" if locked_rr > 0 else "Stop_Loss_Hit"
        return status, pnl, None

    # Cross-session PDH/PDL break check (only if carried over to a new session)
    if active_trade.get('carried_over', False):
        broke_new_pdh = high_price >= current_session_pdh
        broke_new_pdl = low_price <= current_session_pdl
        
        # BUG FIX: old code forced a minimum +1R profit (max(1.0, ...)) even when the new
        # session's PDH/PDL was on the wrong side of entry, booking fake profit.
        # Now the exit counts only if the level is beyond entry, and the real reward is used.
        if direction == "BUY" and broke_new_pdh and current_session_pdh > entry_price:
            current_reward = current_session_pdh - entry_price
            rr = current_reward / risk_per_unit if risk_per_unit > 0 else 0.0
            position_size_usdt = 100 * 30
            profit_usdt = (position_size_usdt / entry_price) * current_reward
            return f"Next_Session_PDH_Break_Exit_1:{round(rr, 2)}", profit_usdt, None
            
        elif direction == "SELL" and broke_new_pdl and current_session_pdl < entry_price:
            current_reward = entry_price - current_session_pdl
            rr = current_reward / risk_per_unit if risk_per_unit > 0 else 0.0
            position_size_usdt = 100 * 30
            profit_usdt = (position_size_usdt / entry_price) * current_reward
            return f"Next_Session_PDL_Break_Exit_1:{round(rr, 2)}", profit_usdt, None

    # Dynamic Profit Locking Evaluation (1:2, 1:3, 1:4, 1:5)
    current_reward = (high_price - entry_price) if direction == "BUY" else (entry_price - low_price)
    rr = current_reward / risk_per_unit if risk_per_unit > 0 else 0
    
    current_locked = active_trade.get('locked_rr', 0.0)
    status = "Running"
    
    if rr >= 5.0:
        position_size_usdt = 100 * 30
        profit_usdt = (position_size_usdt / entry_price) * (5.0 * risk_per_unit)
        return "Target_1:5_Reached_Full_Exit", profit_usdt, None
    elif rr >= 4.0 and current_locked < 4.0:
        active_trade['locked_rr'] = 4.0
        active_trade['sl'] = entry_price + (4.0 * risk_per_unit) if direction == "BUY" else entry_price - (4.0 * risk_per_unit)
        status = "Profit_Locked_at_1:4"
    elif rr >= 3.0 and current_locked < 3.0:
        active_trade['locked_rr'] = 3.0
        active_trade['sl'] = entry_price + (3.0 * risk_per_unit) if direction == "BUY" else entry_price - (3.0 * risk_per_unit)
        status = "Profit_Locked_at_1:3"
    elif rr >= 2.0 and current_locked < 2.0:
        active_trade['locked_rr'] = 2.0
        active_trade['sl'] = entry_price + (2.0 * risk_per_unit) if direction == "BUY" else entry_price - (2.0 * risk_per_unit)
        status = "Profit_Locked_at_1:2"
    elif current_locked > 0:
        status = f"Running_Locked_at_1:{current_locked}"
            
    position_size_usdt = 100 * 30 # 100 USDT Capital @ 30X Leverage = 3000 USDT exposure
    floating_locked_profit = (position_size_usdt / entry_price) * (active_trade.get('locked_rr', 0.0) * risk_per_unit)
    
    return status, floating_locked_profit, active_trade

def run_rd_trading_system(df, news_events=None, news_buffer_minutes=30):
    df = df.copy()
    df['Timestamp'] = pd.to_datetime(df['Timestamp'])
    df = df.sort_values('Timestamp').reset_index(drop=True)
    df['Session'] = df['Timestamp'].apply(assign_session)
    # NY runs 18:30 -> 05:30 IST: candles after midnight belong to the previous day's NY session
    after_midnight_ny = (df['Session'] == 'NY') & (df['Timestamp'].dt.time < time(5, 30))
    df['Date'] = (df['Timestamp'] - pd.to_timedelta(after_midnight_ny.astype(int), unit='D')).dt.date
    # Volume average over the previous 20 candles of the whole series (not just this session),
    # so the volume check also works in the first candles of a session.
    df['VolMA20'] = df['Volume'].shift(1).rolling(window=20, min_periods=10).mean()
    
    results = []
    active_trade = None
    
    for date, date_group in df.groupby('Date'):
        for sess_name, sess_group in date_group.groupby('Session'):
            if len(sess_group) < 3:
                continue
            
            sess_group = sess_group.sort_values('Timestamp').reset_index(drop=True)
            
            # Mark carried_over flag if a trade entered from a previous session
            if active_trade is not None and active_trade.get('entry_session') != (date, sess_name):
                active_trade['carried_over'] = True
            
            # Opening Range (First 3 candles of 15-min = 45 mins)
            orb_candles = sess_group.iloc[:3]
            orb_high = orb_candles['High'].max()
            orb_low = orb_candles['Low'].min()
            
            # Independent PDH and PDL for the current session
            
            for i in range(len(sess_group)):
                row = sess_group.iloc[i]
                current_ts = row['Timestamp']
                # running session high/low from PRIOR candles only (no look-ahead)
                if i > 0:
                    curr_pdh = sess_group['High'].iloc[:i].max()
                    curr_pdl = sess_group['Low'].iloc[:i].min()
                else:
                    curr_pdh, curr_pdl = float('inf'), float('-inf')
                
                news_restricted = is_high_impact_news_time(current_ts, news_events, news_buffer_minutes)
                
                trade_status = "No_Trade"
                profit = 0.0
                
                if active_trade is not None:
                    trade_status, profit, active_trade = manage_open_trade(
                        active_trade, row, curr_pdh, curr_pdl, news_restricted
                    )
                else:
                    # Look for new entries after first 3 candles (45 mins)
                    if i >= 3:
                        if news_restricted:
                            trade_status = "Skipped_Due_To_News_Filter"
                        else:
                            high_break = row['High'] > orb_high
                            close_break = row['Close'] > orb_high
                            
                            low_break = row['Low'] < orb_low
                            close_low_break = row['Close'] < orb_low
                            
                            # BUG FIX: earlier, vol_ma was NaN for the first ~10 candles of a session and
                            # NaN meant "volume OK" -> every breakout passed at session start.
                            # Now: no volume average available = NOT confirmed.
                            vol_ma = row['VolMA20']
                            high_vol = bool(row['Volume'] > (vol_ma * 1.5)) if not pd.isna(vol_ma) else False
                            
                            if high_break:
                                if close_break and high_vol:
                                    active_trade = {
                                        'direction': 'BUY',
                                        'entry_price': row['Close'],
                                        'sl': orb_low,
                                        'risk': abs(row['Close'] - orb_low),
                                        'locked_rr': 0.0,
                                        'entry_session': (date, sess_name),
                                        'carried_over': False
                                    }
                                    trade_status = "Valid_Bullish_Breakout_BUY"
                                else:
                                    trade_status = "Fake_Bullish_Breakout_Wick_Trap"
                            elif low_break:
                                if close_low_break and high_vol:
                                    active_trade = {
                                        'direction': 'SELL',
                                        'entry_price': row['Close'],
                                        'sl': orb_high,
                                        'risk': abs(orb_high - row['Close']),
                                        'locked_rr': 0.0,
                                        'entry_session': (date, sess_name),
                                        'carried_over': False
                                    }
                                    trade_status = "Valid_Bearish_Breakout_SELL"
                                else:
                                    trade_status = "Fake_Bearish_Breakout_Wick_Trap"
                
                results.append({
                    'Timestamp': current_ts,
                    'Session': sess_name,
                    'Close': _px(row['Close']),
                    'ORH': _px(orb_high),
                    'ORL': _px(orb_low),
                    'Session_PDH': _px(curr_pdh) if np.isfinite(curr_pdh) else np.nan,
                    'Session_PDL': _px(curr_pdl) if np.isfinite(curr_pdl) else np.nan,
                    'News_Filter': news_restricted,
                    'Status': trade_status,
                    'Locked_PnL_USDT': round(profit, 2)
                })
                
    return pd.DataFrame(results)


# ======================= SCANNER (24h volume filter) =======================
def http_json(url, params=None):
    r = HTTP.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()


def fetch_liquid_pairs(min_volume_usdt=MIN_24H_VOLUME_USDT):
    """
    CoinDCX USDT futures pairs whose 24h volume is ABOVE min_volume_usdt (default 10M USDT).
    Only these coins get scanned.
    The CoinDCX 'v' field IS the 24h quote (USDT) volume (verified against 24h candle sums:
    BTC v=13.71B vs candles 13.70B), so no ltp multiplication. VOLUME_IS_BASE=1 is kept only
    as an override. Stale tickers (old timestamp) are skipped.
    """
    payload = http_json(PRICE_URL)
    prices = payload.get("prices", {}) if isinstance(payload, dict) else {}
    volume_is_base = os.getenv("VOLUME_IS_BASE", "0") == "1"
    out = []
    now_ms = _time.time() * 1000
    for pair, q in prices.items():
        if not (pair.startswith("B-") and pair.endswith("_USDT")):
            continue
        try:
            ltp = float(q.get("ls", 0))
            v = float(q.get("v", 0))
            ts = float(q.get("ctRT") or q.get("btST") or 0)
        except (TypeError, ValueError):
            continue
        # Stale/delisted tickers keep an old volume number forever (e.g. STORJ, SCRT) - skip them.
        if ltp <= 0 or ts <= 0 or (now_ms - ts) > MAX_TICKER_AGE_SEC * 1000:
            continue
        vol_usdt = ltp * v if volume_is_base else v
        if vol_usdt >= min_volume_usdt:  # 24h volume must NOT be below 10M USDT
            out.append({"pair": pair, "ltp": ltp, "volume_24h_usdt": vol_usdt})
    out.sort(key=lambda x: x["volume_24h_usdt"], reverse=True)
    return out


def fetch_candles_15m(pair, bars=CANDLE_BARS):
    now = int(_time.time())
    payload = http_json(CANDLE_URL, {"pair": pair, "from": now - 900 * (bars + 5),
                                     "to": now, "resolution": "15", "pcode": "f"})
    raw = payload.get("data", payload.get("candles", [])) if isinstance(payload, dict) else payload
    if not raw:
        return pd.DataFrame()
    d = pd.DataFrame(raw)
    need = ["time", "open", "high", "low", "close", "volume"]
    if not set(need).issubset(d.columns):
        return pd.DataFrame()
    for c in need:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna().drop_duplicates("time").sort_values("time")
    # UTC -> IST (sessions are defined in IST), tz-naive
    ts = pd.to_datetime(d["time"], unit="ms", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    out = pd.DataFrame({"Timestamp": ts, "Open": d["open"], "High": d["high"],
                        "Low": d["low"], "Close": d["close"], "Volume": d["volume"]})
    return out.iloc[:-1].reset_index(drop=True)  # drop the still-forming last candle


def candle_volume_ok(df):
    """Second check on the SAME candles that get traded: last 24h (96 x 15m) volume in USDT
    must also be >= MIN_24H_VOLUME_USDT. Returns (ok, volume_usdt)."""
    if len(df) < BARS_PER_24H:
        return False, 0.0
    last = df.iloc[-BARS_PER_24H:]
    vol = float((last["Volume"] * last["Close"]).sum())
    return vol >= MIN_24H_VOLUME_USDT, vol


def scan_market(news_events=None, news_buffer_minutes=30):
    pairs = fetch_liquid_pairs()
    print(f"Volume filter >= {MIN_24H_VOLUME_USDT:,.0f} USDT: {len(pairs)} coins pass")
    rows = []
    for p in pairs:
        try:
            df = fetch_candles_15m(p["pair"])
            ok, cvol = candle_volume_ok(df)
            if not ok:
                continue
            p["volume_24h_usdt"] = min(p["volume_24h_usdt"], cvol)
            res = run_rd_trading_system(df, news_events, news_buffer_minutes)
            if res.empty:
                continue
            last = res.iloc[-1].to_dict()
            last.update({"Pair": p["pair"], "Volume_24h_USDT": round(p["volume_24h_usdt"])})
            rows.append(last)
        except Exception as e:
            print(f"skip {p['pair']}: {e}")
        _time.sleep(0.2)
    return pd.DataFrame(rows)


# ======================= TELEGRAM ALERTS + AUTO RUN =======================
SCRIPT_NAME = os.path.basename(__file__)  # "rd_trading.py" - shown in every Telegram message
ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(ROOT, "rd_trading_state.json")
SIGNALS_FILE = os.path.join(ROOT, "rd_trading_signals.csv")

# Statuses that are worth an alert (entries, lock updates, exits)
ENTRY_STATUSES = {"Valid_Bullish_Breakout_BUY", "Valid_Bearish_Breakout_SELL"}
EXIT_PREFIXES = ("Stop_Loss_Hit", "SL_Hit_Exit_Locked", "Target_1:5", "Next_Session_PDH", "Next_Session_PDL")
LOCK_PREFIXES = ("Profit_Locked_at",)


def telegram(message):
    token = os.getenv("TG_TOKEN") or os.getenv("BOT_TOKEN")
    chat = os.getenv("TG_CHAT_ID") or os.getenv("CHAT_ID")
    if not token or not chat:
        print("Telegram not configured; add TG_TOKEN and TG_CHAT_ID secrets.")
        return False
    try:
        r = HTTP.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": message}, timeout=20)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"Telegram send failed: {e}")
        return False


def _load_state():
    try:
        import json
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
            st.setdefault("sent", [])
            return st
    except Exception:
        return {"sent": []}


def _save_state(st):
    import json
    st["sent"] = st["sent"][-3000:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)


def _event_kind(status):
    if status in ENTRY_STATUSES:
        return "ENTRY"
    if status.startswith(EXIT_PREFIXES):
        return "EXIT"
    if status.startswith(LOCK_PREFIXES):
        return "LOCK"
    return None


def _format_alert(kind, pair, row, vol24):
    status = row["Status"]
    icons = {"ENTRY": "🟢" if "BUY" in status else "🔴", "LOCK": "🔒", "EXIT": "🏁"}
    lines = [
        f"{icons[kind]} [{SCRIPT_NAME}] {kind}",
        f"Coin: {pair}",
        f"Session: {row['Session']} | Time (IST): {row['Timestamp']}",
        f"Signal: {status}",
        f"Price: {row['Close']} | ORH: {row['ORH']} | ORL: {row['ORL']}",
        f"24h Vol: {vol24:,.0f} USDT",
    ]
    if kind == "ENTRY":
        sl = row["ORL"] if "BUY" in status else row["ORH"]
        lines.append(f"Entry: {row['Close']} | SL: {sl}")
    elif kind in ("LOCK", "EXIT"):
        lines.append(f"PnL (100 USDT @ 30X): {row['Locked_PnL_USDT']} USDT")
    lines.append("PAPER ONLY - no real orders")
    return "\n".join(lines)


def scan_and_alert(news_events=None, news_buffer_minutes=30):
    """One auto-run cycle: volume filter -> scan -> Telegram for NEW events only."""
    st = _load_state()
    sent = set(st["sent"])
    if "open" not in st:
        # first run with trade tracking: rebuild open trades from alerts already sent
        rebuilt = set()
        for k in st["sent"]:
            pair, _, status = k.split("|", 2)
            if status in ENTRY_STATUSES:
                rebuilt.add(pair)
            elif status.startswith(EXIT_PREFIXES):
                rebuilt.discard(pair)
        st["open"] = sorted(rebuilt)
    open_pairs = set(st["open"])
    pairs = fetch_liquid_pairs()
    print(f"[{SCRIPT_NAME}] Ticker volume filter >= {MIN_24H_VOLUME_USDT:,.0f} USDT: {len(pairs)} coins pass")
    new_rows = []
    entries_sent = 0
    candle_rejected = 0
    for p in pairs:
        try:
            df = fetch_candles_15m(p["pair"])
            ok, cvol = candle_volume_ok(df)
            if not ok:
                candle_rejected += 1
                continue
            p["volume_24h_usdt"] = min(p["volume_24h_usdt"], cvol)  # show the lower (safer) volume
            res = run_rd_trading_system(df, news_events, news_buffer_minutes)
            # last 2 closed candles so a missed cycle does not lose a signal
            for _, row in res.tail(2).iterrows():
                kind = _event_kind(str(row["Status"]))
                if not kind:
                    continue
                key = f"{p['pair']}|{row['Timestamp']}|{row['Status']}"
                if key in sent:
                    continue
                if kind in ("LOCK", "EXIT") and p["pair"] not in open_pairs:
                    # Trade was never announced (opened before alerts started, or skipped by the
                    # entry cap): no orphan LOCK/EXIT without a matching ENTRY.
                    sent.add(key)
                    st["sent"].append(key)
                    continue
                if kind == "ENTRY":
                    # coins are processed highest 24h volume first; cap new entries per cycle
                    # cap per cycle AND on total open trades (a per-cycle cap alone lets new
                    # entries pile up every 5 minutes)
                    if entries_sent >= MAX_NEW_ENTRIES_PER_CYCLE or len(open_pairs) >= MAX_OPEN_TRADES:
                        sent.add(key)
                        st["sent"].append(key)
                        print(f"entry cap reached, skipped {p['pair']}")
                        continue
                    entries_sent += 1
                if telegram(_format_alert(kind, p["pair"], row, p["volume_24h_usdt"])) or not (
                        (os.getenv("TG_TOKEN") or os.getenv("BOT_TOKEN"))):
                    sent.add(key)
                    st["sent"].append(key)
                    if kind == "ENTRY":
                        open_pairs.add(p["pair"])
                    elif kind == "EXIT":
                        open_pairs.discard(p["pair"])
                new_rows.append({"Script": SCRIPT_NAME, "Pair": p["pair"], "Kind": kind,
                                 **row.to_dict(), "Volume_24h_USDT": round(p["volume_24h_usdt"])})
        except Exception as e:
            print(f"skip {p['pair']}: {e}")
        _time.sleep(0.2)
    if new_rows:
        pd.DataFrame(new_rows).to_csv(SIGNALS_FILE, mode="a", header=not os.path.exists(SIGNALS_FILE), index=False)
    st["open"] = sorted(open_pairs)
    _save_state(st)
    print(f"[{SCRIPT_NAME}] cycle done: {len(new_rows)} new event(s), "
          f"{candle_rejected} coin(s) rejected by 24h candle-volume check")


if __name__ == "__main__":
    if "--alert" in sys.argv:
        scan_and_alert()
        sys.exit(0)
    if "--scan" in sys.argv:
        out = scan_market()
        print(out.to_string(index=False) if not out.empty else "No data")
        sys.exit(0)
    # Sample 15-minute data across Asian, London, and NY sessions
    dates = pd.date_range(start="2026-11-14 05:30", periods=65, freq="15min")
    np.random.seed(42)
    prices = 1000 + np.cumsum(np.random.randn(65) * 2.5)
    
    test_df = pd.DataFrame({
        'Timestamp': dates,
        'Open': prices,
        'High': prices + np.random.uniform(1, 4, 65),
        'Low': prices - np.random.uniform(1, 4, 65),
        'Close': prices + np.random.uniform(-1.5, 1.5, 65),
        'Volume': np.random.randint(1000, 7000, 65)
    })
    
    high_impact_news = ["2026-11-14 14:00:00", "2026-11-14 19:00:00"]
    res = run_rd_trading_system(test_df, news_events=high_impact_news, news_buffer_minutes=30)
    print("=== R&D PY (rd_trading.py) Execution Log ===")
    print(res[res['Status'] != 'No_Trade'].head(20).to_string(index=False))
