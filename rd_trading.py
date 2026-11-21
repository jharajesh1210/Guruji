import os
import sys
import json
import time as _time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, time, timedelta

# ---------------- Config ----------------
PUB = "https://public.coindcx.com"
CANDLE_URL = PUB + "/market_data/candlesticks"
PRICE_URL = PUB + "/market_data/v3/current_prices/futures/rt"
MIN_24H_VOLUME_USDT = float(os.getenv("MIN_24H_VOLUME_USDT", "10000000"))  # 10 million USDT
CANDLE_BARS = int(os.getenv("CANDLE_BARS", "500"))
MAX_TICKER_AGE_SEC = int(os.getenv("MAX_TICKER_AGE_SEC", "600"))
MAX_NEW_ENTRIES_PER_CYCLE = int(os.getenv("MAX_NEW_ENTRIES_PER_CYCLE", "5"))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "5"))
BARS_PER_24H = 96  
MIN_DAY_BARS = int(os.getenv("MIN_DAY_BARS", "90"))

# ---- Trade / risk config ----
CAPITAL_USDT = float(os.getenv("CAPITAL_USDT", "100"))
LEVERAGE = float(os.getenv("LEVERAGE", "30"))
POSITION_USDT = CAPITAL_USDT * LEVERAGE                      
MAX_LOSS_USDT = float(os.getenv("MAX_LOSS_USDT", "100"))     
MAX_RISK_PCT = float(os.getenv("MAX_RISK_PCT", "2.0"))       
MIN_RISK_PCT = float(os.getenv("MIN_RISK_PCT", "0.75"))  # 0.15 -> 0.3 -> 0.75 (20 Nov, 90-day live-style backtest)
# 20 Nov 2026 (90-day live-style backtest, rd_backtest_cap.md: -79 -> -18 USDT, max DD -119 -> -42):
MAX_ENTRY_DIST_PCT = float(os.getenv("MAX_ENTRY_DIST_PCT", "0.5"))  # close at most 0.5% beyond PDH/PDL, else skip
LOSS_CAP_R = float(os.getenv("LOSS_CAP_R", "1.5"))       # no profit locked: exit at once at SL + 0.5R (=1.5R) ...
LOSS_CAP_PCT = float(os.getenv("LOSS_CAP_PCT", "3.0"))   # ... or 3% of price, whichever is nearer
LOCK_LEVELS = (2.0, 3.0, 4.0, 5.0)                       # lock ladder 1:2 / 1:3 / 1:4 / 1:5
TARGET_RR = float(os.getenv("TARGET_RR", "6.0"))         # full exit at 1:6 (was 1:5)
# 21 Nov 2026 (90-day live-style backtest rd_backtest_bb.md: -7.6 -> +0.6 USDT, max DD -16.7 -> -8.4):
# take the PDH/PDL breakout only after a Bollinger squeeze = BB(20,2) width on the candle BEFORE the breakout
# is below the median width of the previous 100 candles (fresh breakout, not an already-stretched move).
BB_SQUEEZE_FILTER = os.getenv("BB_SQUEEZE_FILTER", "YES").strip().upper() == "YES"      
SL_MODE = os.getenv("SL_MODE", "candle").lower()             
RISK_CAP_PCT = min(MAX_RISK_PCT, MAX_LOSS_USDT / POSITION_USDT * 100)

MAX_CATCHUP_BARS = int(os.getenv("MAX_CATCHUP_BARS", "8"))   
FIRST_RUN_BARS = 2                                           

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "RD-TRADING-SCANNER/1.0"})


def _px(x):
    return float(f"{float(x):.6g}")


def _pnl(entry, price_move):
    return POSITION_USDT / entry * price_move


def assign_session(timestamp):
    t = pd.to_datetime(timestamp).time()
    if time(5, 30) <= t < time(13, 30):
        return 'Asian'
    elif time(13, 30) <= t < time(18, 30):
        return 'London'
    elif time(18, 30) <= t or t < time(5, 30):
        return 'NY'
    return 'Other'


# ======================= DATA PREP: sessions + MACD + PDH/PDL =======================
def prepare(df):
    df = df.copy()
    df['Timestamp'] = pd.to_datetime(df['Timestamp'])
    df = df.sort_values('Timestamp').reset_index(drop=True)
    df['Session'] = df['Timestamp'].apply(assign_session)
    after_midnight_ny = (df['Session'] == 'NY') & (df['Timestamp'].dt.time < time(5, 30))
    df['Date'] = (df['Timestamp'] - pd.to_timedelta(after_midnight_ny.astype(int), unit='D')).dt.date
    df['VolMA20'] = df['Volume'].shift(1).rolling(window=20, min_periods=10).mean()

    # --- MACD Calculation Added ---
    exp1 = df['Close'].ewm(span=12, adjust=False).mean()
    exp2 = df['Close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = exp1 - exp2
    df['MACD_Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
    df['Prev_MACD'] = df['MACD'].shift(1)
    df['Prev_Signal'] = df['MACD_Signal'].shift(1)
    # ------------------------------
    # --- Bollinger squeeze (20, 2) ---
    bb_mid = df['Close'].rolling(20).mean()
    bb_sd = df['Close'].rolling(20).std(ddof=0)
    bb_width = (4 * bb_sd) / bb_mid
    prev_w = bb_width.shift(1)
    df['BB_Squeeze'] = prev_w < prev_w.rolling(100, min_periods=50).median()

    daily = df.groupby('Date').agg(DH=('High', 'max'), DL=('Low', 'min'), N=('High', 'size'))
    daily = daily[daily['N'] >= MIN_DAY_BARS]  
    return df, daily


def levels_for(date, daily):
    prev = date - timedelta(days=1)
    if prev in daily.index:
        return float(daily.at[prev, 'DH']), float(daily.at[prev, 'DL'])
    return None, None


# ======================= STOP LOSS =======================
def build_stop(direction, row):
    entry = float(row['Close'])
    if SL_MODE == "mid":
        sl = (float(row['High']) + float(row['Low'])) / 2
    else:
        sl = float(row['Low']) if direction == "BUY" else float(row['High'])

    if (direction == "BUY" and sl >= entry) or (direction == "SELL" and sl <= entry):
        return sl, 0.0

    risk = abs(entry - sl)
    max_risk = entry * RISK_CAP_PCT / 100
    if risk > max_risk:
        risk = max_risk
        sl = entry - risk if direction == "BUY" else entry + risk
    return sl, risk


# ======================= SIGNAL DETECTION =======================
def detect_signal(row, pdh, pdl):
    high_break = row['High'] > pdh
    close_break = row['Close'] > pdh
    low_break = row['Low'] < pdl
    close_low_break = row['Close'] < pdl

    vol_ma = row['VolMA20']
    high_vol = bool(row['Volume'] > (vol_ma * 1.5)) if not pd.isna(vol_ma) else False

    candle_range = row['High'] - row['Low']
    if candle_range > 0:
        upper_wick_ratio = (row['High'] - max(row['Open'], row['Close'])) / candle_range
        lower_wick_ratio = (min(row['Open'], row['Close']) - row['Low']) / candle_range
    else:
        upper_wick_ratio = 0.0
        lower_wick_ratio = 0.0

    is_clean_bullish = bool(high_break and close_break and high_vol and (upper_wick_ratio < 0.40))
    is_clean_bearish = bool(low_break and close_low_break and high_vol and (lower_wick_ratio < 0.40))

    direction = "BUY" if is_clean_bullish else ("SELL" if is_clean_bearish else None)
    if direction:
        entry = float(row['Close'])
        sl, risk = build_stop(direction, row)
        if risk < entry * MIN_RISK_PCT / 100:
            return "Skipped_SL_Too_Tight", None
        beyond = (entry - pdh) if direction == "BUY" else (pdl - entry)
        if MAX_ENTRY_DIST_PCT > 0 and beyond > entry * MAX_ENTRY_DIST_PCT / 100:
            return "Skipped_Late_Entry_Too_Far_From_Level", None
        if BB_SQUEEZE_FILTER and not bool(row.get('BB_Squeeze', False)):
            return "Skipped_No_BB_Squeeze", None
        trade = {
            'direction': direction,
            'entry_price': entry,
            'sl': float(sl),
            'risk': float(risk),
            'locked_rr': 0.0,        
            'pending_sl': None,      
            'pending_rr': 0.0,
            'carried_over': False,
        }
        return ("Valid_Bullish_Breakout_BUY" if direction == "BUY" else "Valid_Bearish_Breakout_SELL"), trade

    if high_break:
        return "Fake_Bullish_Breakout_Wick_Trap_Filtered", None
    if low_break:
        return "Fake_Bearish_Breakout_Wick_Trap_Filtered", None
    return "No_Trade", None


# ======================= TRADE MANAGEMENT (WITH MACD LOSS-EXIT FILTER) =======================
def manage_open_trade(trade, candle, day_pdh, day_pdl):
    if not trade:
        return "No_Active_Trade", 0.0, None

    is_buy = trade['direction'] == "BUY"
    entry = trade['entry_price']
    risk = trade['risk']
    high = float(candle['High'])
    low = float(candle['Low'])

    # 1) Pichli candle ka lock apply karna
    if trade.get('pending_sl') is not None:
        trade['sl'] = trade['pending_sl']
        trade['locked_rr'] = trade['pending_rr']
        trade['pending_sl'] = None
        trade['pending_rr'] = 0.0

    # 1b) Loss cap: no profit locked -> exit at once at 1.5R or 3% (whichever is nearer), no MACD wait
    if trade['locked_rr'] <= 0 and (LOSS_CAP_R > 0 or LOSS_CAP_PCT > 0):
        cap_dist = min(LOSS_CAP_R * risk if LOSS_CAP_R > 0 else 1e18, entry * LOSS_CAP_PCT / 100 if LOSS_CAP_PCT > 0 else 1e18)
        cap_px = entry - cap_dist if is_buy else entry + cap_dist
        if (is_buy and low <= cap_px) or ((not is_buy) and high >= cap_px):
            op = float(candle['Open'])
            exit_px = min(cap_px, op) if is_buy else max(cap_px, op)
            move = (exit_px - entry) if is_buy else (entry - exit_px)
            return f"Stop_Loss_Hit_Loss_Cap_{cap_dist / risk:.2f}R", max(_pnl(entry, move), -MAX_LOSS_USDT), None

    # 2) Stop loss check
    sl = trade['sl']
    sl_hit = (is_buy and low <= sl) or ((not is_buy) and high >= sl)

    if sl_hit:
        locked = trade['locked_rr']
        is_loss_exit = (locked <= 0) # Agar profit locked nahi hai, matlab loss exit hai

        if is_loss_exit:
            # MACD Crossover Check
            macd_val = float(candle.get('MACD', 0))
            signal_val = float(candle.get('MACD_Signal', 0))
            prev_macd = float(candle.get('Prev_MACD', 0))
            prev_signal = float(candle.get('Prev_Signal', 0))

            # MACD rule sirf SL touch ke time check hota hai (entry/tracking se koi lena-dena nahi):
            # BUY: MACD signal ke neeche (cross ho chuka) -> exit valid
            # SELL: MACD signal ke upar (cross ho chuka) -> exit valid
            bearish_cross = macd_val < signal_val
            bullish_cross = macd_val > signal_val

            if is_buy and not bearish_cross:
                # BUY mein loss exit par MACD ne cross nahi kiya -> WAIT KARO (Exit skip)
                pass 
            elif (not is_buy) and not bullish_cross:
                # SELL mein loss exit par MACD ne cross nahi kiya -> WAIT KARO (Exit skip)
                pass
            else:
                # MACD ne confirm kar diya -> Exit
                # Realistic exit price: SL aur candle close mein se jo kharab ho (BUY: kam, SELL: zyada)
                close_px = float(candle['Close'])
                exit_px = min(sl, close_px) if is_buy else max(sl, close_px)
                move = (exit_px - entry) if is_buy else (entry - exit_px)
                pnl = max(_pnl(entry, move), -MAX_LOSS_USDT)
                return "Stop_Loss_Hit_MACD_Confirmed", pnl, None
        else:
            # Locked profit par SL hit hua hai, normal exit
            rr = locked
            pnl = max(_pnl(entry, rr * risk), -MAX_LOSS_USDT)
            return f"SL_Hit_Exit_Locked_1:{locked:g}", pnl, None

    # 3) Agle din ka PDH/PDL break
    if trade.get('carried_over'):
        if is_buy and high >= day_pdh and day_pdh > entry:
            reward = min(day_pdh - entry, TARGET_RR * risk)
            return f"Next_Day_PDH_Break_Exit_1:{reward / risk:.2f}", _pnl(entry, reward), None
        if (not is_buy) and low <= day_pdl and day_pdl < entry:
            reward = min(entry - day_pdl, TARGET_RR * risk)
            return f"Next_Day_PDL_Break_Exit_1:{reward / risk:.2f}", _pnl(entry, reward), None

    # 4) Profit lock / target
    reward = (high - entry) if is_buy else (entry - low)
    rr = reward / risk if risk > 0 else 0.0

    if rr >= TARGET_RR:
        return f"Target_1:{TARGET_RR:g}_Reached_Full_Exit", _pnl(entry, TARGET_RR * risk), None

    new_lock = max([l for l in LOCK_LEVELS if rr >= l], default=0.0)
    if new_lock > trade['locked_rr']:
        lock_px = entry + new_lock * risk if is_buy else entry - new_lock * risk
        close_px = float(candle['Close'])
        # Lock turant (same candle): price lock level tak gaya aur candle usi ke neeche (SELL: upar) band hui
        # -> reversal ne lock level cross kiya -> wahi locked level par exit.
        if (is_buy and close_px <= lock_px) or ((not is_buy) and close_px >= lock_px):
            return f"SL_Hit_Exit_Locked_1:{new_lock:g}", _pnl(entry, new_lock * risk), None
        trade['pending_rr'] = new_lock
        trade['pending_sl'] = entry + new_lock * risk if is_buy else entry - new_lock * risk
        status = f"Profit_Locked_at_1:{new_lock:g}"
    elif trade['locked_rr'] > 0:
        status = f"Running_Locked_at_1:{trade['locked_rr']:g}"
    else:
        status = "Running_Waiting_MACD_Cross" if sl_hit else "Running"

    return status, _pnl(entry, trade.get('locked_rr', 0.0) * risk), trade


# ======================= SINGLE CANDLE STEP =======================
def session_key(date, session):
    """One trading session instance, e.g. '2026-11-17|NY' (NY after midnight belongs to the previous Date)."""
    return f"{date}|{session}"


def session_key_now():
    now = pd.Timestamp.now("UTC").tz_convert("Asia/Kolkata").tz_localize(None)
    sess = assign_session(now)
    day = (now - timedelta(days=1)).date() if (sess == 'NY' and now.time() < time(5, 30)) else now.date()
    return session_key(day, sess)


def step_candle(row, trade, daily, allow_entry=None, blocks=None):
    """blocks (optional, per pair): {"BUY": session_key, "SELL": session_key}.
    Rule: after a PROFIT exit, the same coin is not entered again in the same direction
    until that session (Asian / London / NY) ends."""
    pdh, pdl = levels_for(row['Date'], daily)
    have_levels = pdh is not None
    if not have_levels:
        pdh = pdl = float('nan')

    if trade is not None and trade.get('entry_date') != str(row['Date']):
        trade['carried_over'] = True

    if trade is not None:
        direction = trade['direction']
        status, profit, trade = manage_open_trade(trade, row, pdh, pdl)
        if trade is None and profit > 0 and blocks is not None:
            blocks[direction] = session_key(row['Date'], row['Session'])
        return status, profit, trade, pdh, pdl

    if not have_levels:
        return "No_Prev_Day_Data", 0.0, None, pdh, pdl

    status, new_trade = detect_signal(row, pdh, pdl)
    if new_trade is not None:
        if blocks is not None and blocks.get(new_trade['direction']) == session_key(row['Date'], row['Session']):
            return "Entry_Skipped_Profit_Exit_Same_Session", 0.0, None, pdh, pdl
        if allow_entry is not None and not allow_entry():
            return "Entry_Skipped_Cap", 0.0, None, pdh, pdl
        new_trade['entry_ts'] = str(row['Timestamp'])
        new_trade['entry_date'] = str(row['Date'])
        new_trade['entry_session'] = row['Session']
        trade = new_trade
    return status, 0.0, trade, pdh, pdl


def _record(row, status, profit, trade, pdh, pdl):
    entry = sl = float('nan')
    if trade is not None:
        entry = trade['entry_price']
        sl = trade['pending_sl'] if trade.get('pending_sl') is not None else trade['sl']
    return {
        'Timestamp': row['Timestamp'],
        'Session': row['Session'],
        'Close': _px(row['Close']),
        'PDH': _px(pdh),
        'PDL': _px(pdl),
        'Entry': _px(entry),
        'SL': _px(sl),
        'Status': status,
        'Locked_PnL_USDT': round(profit, 2),
    }


def run_rd_trading_system(df):
    df, daily = prepare(df)
    results = []
    trade = None
    blocks = {}
    for _, row in df.iterrows():
        status, profit, trade, pdh, pdl = step_candle(row, trade, daily, blocks=blocks)
        if status == "No_Prev_Day_Data":
            continue
        results.append(_record(row, status, profit, trade, pdh, pdl))
    return pd.DataFrame(results)


# ======================= SCANNER (24h volume filter) =======================
def http_json(url, params=None):
    r = HTTP.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()


def fetch_liquid_pairs(min_volume_usdt=MIN_24H_VOLUME_USDT):
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
        if ltp <= 0 or ts <= 0 or (now_ms - ts) > MAX_TICKER_AGE_SEC * 1000:
            continue
        vol_usdt = ltp * v if volume_is_base else v
        if vol_usdt >= min_volume_usdt:
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
    ts = pd.to_datetime(d["time"], unit="ms", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    out = pd.DataFrame({"Timestamp": ts, "Open": d["open"], "High": d["high"],
                        "Low": d["low"], "Close": d["close"], "Volume": d["volume"]})
    return out.iloc[:-1].reset_index(drop=True)   


def candle_volume_ok(df):
    if len(df) < BARS_PER_24H:
        return False, 0.0
    last = df.iloc[-BARS_PER_24H:]
    vol = float((last["Volume"] * last["Close"]).sum())
    return vol >= MIN_24H_VOLUME_USDT, vol


def scan_market():
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
            res = run_rd_trading_system(df)
            if res.empty:
                continue
            last = res.iloc[-1].to_dict()
            last.update({"Pair": p["pair"], "Volume_24h_USDT": round(p["volume_24h_usdt"])})
            rows.append(last)
        except Exception as e:
            print(f"skip {p['pair']}: {e}")
        _time.sleep(0.2)
    return pd.DataFrame(rows)


# ======================= TELEGRAM ALERTS =======================
SCRIPT_NAME = os.path.basename(__file__)
ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.getenv("RD_STATE_FILE") or os.path.join(ROOT, "rd_trading_state.json")
SIGNALS_FILE = os.getenv("RD_SIGNALS_FILE") or os.path.join(ROOT, "rd_trading_signals_v2.csv")
STATE_SEED_FILE = os.path.join(ROOT, "rd_trading_state.json")   # first run of a new state file starts from this
PAPER_TELEGRAM = os.getenv("PAPER_TELEGRAM", "YES").strip().upper() != "NO"

ENTRY_STATUSES = {"Valid_Bullish_Breakout_BUY", "Valid_Bearish_Breakout_SELL"}
EXIT_PREFIXES = ("Stop_Loss_Hit", "SL_Hit_Exit_Locked", "Target_1:", "Next_Day_PDH", "Next_Day_PDL")
LOCK_PREFIXES = ("Profit_Locked_at",)


def _tg_configured():
    return bool((os.getenv("TG_TOKEN") or os.getenv("BOT_TOKEN")) and (os.getenv("TG_CHAT_ID") or os.getenv("CHAT_ID")))


def telegram(message):
    token = os.getenv("TG_TOKEN") or os.getenv("BOT_TOKEN")
    chat = os.getenv("TG_CHAT_ID") or os.getenv("CHAT_ID")
    if not token or not chat:
        print("Telegram not configured.")
        return False
    try:
        r = HTTP.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": message}, timeout=20)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"Telegram send failed: {e}")
        return False


def send_alert(message):
    if not PAPER_TELEGRAM:
        print("PAPER_TELEGRAM=NO; alert not sent:\n" + message)
        return True
    if not _tg_configured():
        print("Telegram not configured; alert logged only:\n" + message)
        return True
    return telegram(message)


def _load_state():
    path = STATE_FILE if os.path.exists(STATE_FILE) else STATE_SEED_FILE
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("sent", [])
    st.setdefault("trades", {})     
    st.setdefault("last_ts", {})    
    st.setdefault("profit_block", {})
    st.pop("open", None)            
    return st


def _save_state(st):
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
    icons = {"ENTRY": "🟢" if "BUY" in status else "🔴", "LOCK": "🔒", "EXIT": "❌"}
    lines = [
        f"{icons[kind]} [{SCRIPT_NAME}] {kind}",
        f"Coin: {pair}",
        f"Session: {row['Session']} | Time (IST): {row['Timestamp']}",
        f"Signal: {status}",
        f"Price: {row['Close']} | Prev Day High: {row['PDH']} | Prev Day Low: {row['PDL']}",
    ]
    if vol24 > 0:
        lines.append(f"24h Vol: {vol24:,.0f} USDT")
    if kind == "ENTRY":
        risk_pct = abs(row['Entry'] - row['SL']) / row['Entry'] * 100
        lines.append(f"Entry: {row['Entry']} | SL: {row['SL']} (risk {risk_pct:.2f}%)")
    elif kind == "LOCK":
        lines.append(f"New SL: {row['SL']}")
        lines.append(f"Locked PnL: {row['Locked_PnL_USDT']} USDT")
    else:
        lines.append(f"PnL: {row['Locked_PnL_USDT']} USDT")
    lines.append("PAPER ONLY - no real orders")
    return "\n".join(lines)


def live_events(df, trade, last_ts, allow_entry, blocks=None):
    if df.empty:
        return [], trade, last_ts
    df, daily = prepare(df)
    if last_ts:
        new = df[df['Timestamp'] > pd.Timestamp(last_ts)]
        if trade is None and len(new) > MAX_CATCHUP_BARS:
            new = new.tail(MAX_CATCHUP_BARS)   
    else:
        new = df.tail(FIRST_RUN_BARS)

    events = []
    for _, row in new.iterrows():
        status, profit, trade, pdh, pdl = step_candle(row, trade, daily, allow_entry, blocks)
        kind = _event_kind(status)
        if kind:
            events.append((kind, _record(row, status, profit, trade, pdh, pdl)))
    return events, trade, str(df['Timestamp'].iloc[-1])


def scan_and_alert(after_pair=None):
    """after_pair(pair, open_trades): optional hook called right after each coin is processed
    (rd_live.py uses it to copy a paper entry to live within seconds)."""
    st = _load_state()
    sent = set(st["sent"])
    trades = st["trades"]
    last_ts = st["last_ts"]

    liquid = fetch_liquid_pairs()
    liquid_map = {p["pair"]: p["volume_24h_usdt"] for p in liquid}
    work = [p["pair"] for p in liquid] + [pr for pr in trades if pr not in liquid_map]

    counters = {"entries": 0}
    new_rows = []
    candle_rejected = 0

    for pair in work:
        has_trade = pair in trades
        try:
            df = fetch_candles_15m(pair)
            vol24 = liquid_map.get(pair, 0.0)
            if not has_trade:
                ok, cvol = candle_volume_ok(df)
                if not ok:
                    candle_rejected += 1
                    continue
                vol24 = min(vol24, cvol)
            entry_allowed = pair in liquid_map

            def allow_entry():
                return (entry_allowed and counters["entries"] < MAX_NEW_ENTRIES_PER_CYCLE
                        and len(trades) < MAX_OPEN_TRADES)

            trade = dict(trades[pair]) if has_trade else None
            events, new_trade, new_last = live_events(df, trade, last_ts.get(pair), allow_entry,
                                                      st["profit_block"].setdefault(pair, {}))

            delivered = True
            entries_now = 0
            rows_now = []
            for kind, rec in events:
                key = f"{pair}|{rec['Timestamp']}|{rec['Status']}"
                if key not in sent:
                    if not send_alert(_format_alert(kind, pair, rec, vol24)):
                        delivered = False   
                        break
                    sent.add(key)
                    st["sent"].append(key)
                if kind == "ENTRY":
                    entries_now += 1
                rows_now.append({"Script": SCRIPT_NAME, "Pair": pair, "Kind": kind, **rec,
                                 "Volume_24h_USDT": round(vol24)})
            if not delivered:
                continue

            if new_trade is None:
                trades.pop(pair, None)
            else:
                trades[pair] = new_trade
            if new_last:
                last_ts[pair] = new_last
            counters["entries"] += entries_now
            new_rows.extend(rows_now)
        except Exception as e:
            print(f"skip {pair}: {e}")
        finally:
            if after_pair is not None:
                try:
                    after_pair(pair, trades)
                except Exception as e:
                    print(f"after_pair {pair}: {e}")
        _time.sleep(0.2)

    if new_rows:
        pd.DataFrame(new_rows).to_csv(SIGNALS_FILE, mode="a", header=not os.path.exists(SIGNALS_FILE), index=False)
    _save_state(st)


if __name__ == "__main__":
    if "--alert" in sys.argv:
        scan_and_alert()
        sys.exit(0)
    if "--scan" in sys.argv:
        out = scan_market()
        print(out.to_string(index=False) if not out.empty else "No data")
        sys.exit(0)

    n = 480
    dates = pd.date_range(start="2026-11-09 20:15", periods=n, freq="15min")
    np.random.seed(7)
    prices = 1000 + np.cumsum(np.random.randn(n) * 2.5)
    vol = np.random.randint(10000, 40000, n).astype(float)
    spikes = np.random.rand(n) < 0.06
    vol[spikes] *= 4

    test_df = pd.DataFrame({
        'Timestamp': dates,
        'Open': prices,
        'High': prices + np.random.uniform(1, 4, n),
        'Low': prices - np.random.uniform(1, 4, n),
        'Close': prices + np.random.uniform(-1.5, 1.5, n),
        'Volume': vol
    })

    res = run_rd_trading_system(test_df)
    print("=== R&D PY Execution Log (MACD Loss Exit Filter Added) ===")
    shown = res[~res['Status'].isin(['No_Trade', 'Running', 'Running_Waiting_MACD_Cross'])]
    print(shown.head(30).to_string(index=False) if not shown.empty else "No signals in demo data")
