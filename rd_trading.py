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
# Pichle poore din ka PDH/PDL nikalne ke liye kam se kam ~2 din ka data chahiye (96 x 15m = 1 din)
CANDLE_BARS = int(os.getenv("CANDLE_BARS", "500"))
MAX_TICKER_AGE_SEC = int(os.getenv("MAX_TICKER_AGE_SEC", "600"))  # ignore stale/delisted tickers
MAX_NEW_ENTRIES_PER_CYCLE = int(os.getenv("MAX_NEW_ENTRIES_PER_CYCLE", "5"))  # cap new entries per cycle
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "5"))  # cap on trades open at the same time
BARS_PER_24H = 96  # 15m candles in 24 hours
MIN_DAY_BARS = int(os.getenv("MIN_DAY_BARS", "90"))  # itne candles se kam wala din "poora" nahi maana jaata

# ---- Trade / risk config ----
CAPITAL_USDT = float(os.getenv("CAPITAL_USDT", "100"))
LEVERAGE = float(os.getenv("LEVERAGE", "30"))
POSITION_USDT = CAPITAL_USDT * LEVERAGE                      # 100 x 30 = 3000 USDT
MAX_LOSS_USDT = float(os.getenv("MAX_LOSS_USDT", "100"))     # ek trade ka max loss
MAX_RISK_PCT = float(os.getenv("MAX_RISK_PCT", "2.0"))       # SL entry se max itne % door
MIN_RISK_PCT = float(os.getenv("MIN_RISK_PCT", "0.15"))      # isse tight SL wali entry skip
SL_MODE = os.getenv("SL_MODE", "candle").lower()             # "candle" = breakout candle low/high, "mid" = range midpoint
# Effective cap: MAX_RISK_PCT aur (MAX_LOSS_USDT / position) mein se jo chhota ho
RISK_CAP_PCT = min(MAX_RISK_PCT, MAX_LOSS_USDT / POSITION_USDT * 100)

# ---- Live (alert) mode config ----
MAX_CATCHUP_BARS = int(os.getenv("MAX_CATCHUP_BARS", "8"))   # bina open trade wale coin par itni purani candles tak hi signal dekhna
FIRST_RUN_BARS = 2                                           # coin pehli baar dikhe to last itni candles dekhna

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "RD-TRADING-SCANNER/1.0"})


def _px(x):
    """Round a price to 6 significant digits."""
    return float(f"{float(x):.6g}")


def _pnl(entry, price_move):
    """Price move (price units) ka USDT PnL: position 100 USDT x 30X = 3000 USDT."""
    return POSITION_USDT / entry * price_move


def assign_session(timestamp):
    """
    Assigns trading session based on IST time:
    - Asian Session: 05:30 to 13:30
    - London Session: 13:30 to 18:30
    - NY Session: 18:30 to 05:30 (agle din subah tak)
    """
    t = pd.to_datetime(timestamp).time()
    if time(5, 30) <= t < time(13, 30):
        return 'Asian'
    elif time(13, 30) <= t < time(18, 30):
        return 'London'
    elif time(18, 30) <= t or t < time(5, 30):
        return 'NY'
    return 'Other'


# ======================= DATA PREP: sessions + PREVIOUS DAY PDH/PDL =======================
def prepare(df):
    """
    - Session / Date column banata hai. Trading day = 05:30 IST se agle din 05:30 IST tak.
    - Har poore din ka High/Low nikalta hai (daily table), jisse "pichle din ka PDH/PDL" milta hai.
    """
    df = df.copy()
    df['Timestamp'] = pd.to_datetime(df['Timestamp'])
    df = df.sort_values('Timestamp').reset_index(drop=True)
    df['Session'] = df['Timestamp'].apply(assign_session)
    after_midnight_ny = (df['Session'] == 'NY') & (df['Timestamp'].dt.time < time(5, 30))
    df['Date'] = (df['Timestamp'] - pd.to_timedelta(after_midnight_ny.astype(int), unit='D')).dt.date
    df['VolMA20'] = df['Volume'].shift(1).rolling(window=20, min_periods=10).mean()

    daily = df.groupby('Date').agg(DH=('High', 'max'), DL=('Low', 'min'), N=('High', 'size'))
    daily = daily[daily['N'] >= MIN_DAY_BARS]  # adhoora din (data ki shuruaat ka) PDH/PDL nahi banega
    return df, daily


def levels_for(date, daily):
    """Din `date` ke liye pichle din ka (PDH, PDL). Pichla poora din na ho to (None, None)."""
    prev = date - timedelta(days=1)
    if prev in daily.index:
        return float(daily.at[prev, 'DH']), float(daily.at[prev, 'DL'])
    return None, None


# ======================= STOP LOSS (tight + capped) =======================
def build_stop(direction, row):
    """
    SL breakout candle se:
      SL_MODE=candle -> BUY: candle ka Low, SELL: candle ka High
      SL_MODE=mid    -> candle range ka midpoint
    Phir SL ko entry se max RISK_CAP_PCT tak door rakhte hain
    (isse 100 USDT max loss bhi cap hota hai).
    Return: (sl, risk_per_unit). Galat side ka SL ho to risk 0 aata hai (entry skip hogi).
    """
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
    """Ek candle par fake-breakout filter ke saath entry check. Return: (status, new_trade ya None)."""
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
        trade = {
            'direction': direction,
            'entry_price': entry,
            'sl': float(sl),
            'risk': float(risk),
            'locked_rr': 0.0,        # abhi tak jo lock SL par lag chuka hai
            'pending_sl': None,      # naya lock jo AGLI candle se lagega
            'pending_rr': 0.0,
            'carried_over': False,
        }
        return ("Valid_Bullish_Breakout_BUY" if direction == "BUY" else "Valid_Bearish_Breakout_SELL"), trade

    if high_break:
        return "Fake_Bullish_Breakout_Wick_Trap_Filtered", None
    if low_break:
        return "Fake_Bearish_Breakout_Wick_Trap_Filtered", None
    return "No_Trade", None


# ======================= TRADE MANAGEMENT =======================
def manage_open_trade(trade, candle, day_pdh, day_pdl):
    """
    Open trade ko ek candle par manage karta hai (order important hai):
    1. Pichli candle mein bana profit-lock ab SL par lagta hai (lock hamesha agli candle se).
    2. SL hit check (locked SL ho to locked profit, nahi to -1R).
    3. Trade agle din tak chala gaya ho to naye din ka PDH/PDL break hone par exit.
    4. Naya lock (1:2, 1:3, 1:4) ya 1:5 par full exit. Naya lock `pending` mein jaata hai.
    Loss kabhi MAX_LOSS_USDT se zyada nahi dikhaya jaata.
    """
    if not trade:
        return "No_Active_Trade", 0.0, None

    is_buy = trade['direction'] == "BUY"
    entry = trade['entry_price']
    risk = trade['risk']
    high = float(candle['High'])
    low = float(candle['Low'])

    # 1) Pichli candle ka lock ab apply
    if trade.get('pending_sl') is not None:
        trade['sl'] = trade['pending_sl']
        trade['locked_rr'] = trade['pending_rr']
        trade['pending_sl'] = None
        trade['pending_rr'] = 0.0

    # 2) Stop loss
    sl = trade['sl']
    if (is_buy and low <= sl) or ((not is_buy) and high >= sl):
        locked = trade['locked_rr']
        rr = locked if locked > 0 else -1.0
        pnl = max(_pnl(entry, rr * risk), -MAX_LOSS_USDT)
        status = f"SL_Hit_Exit_Locked_1:{locked:g}" if locked > 0 else "Stop_Loss_Hit"
        return status, pnl, None

    # 3) Agle din ka PDH/PDL break (sirf carried-over trade ke liye)
    if trade.get('carried_over'):
        if is_buy and high >= day_pdh and day_pdh > entry:
            reward = min(day_pdh - entry, 5.0 * risk)
            return f"Next_Day_PDH_Break_Exit_1:{reward / risk:.2f}", _pnl(entry, reward), None
        if (not is_buy) and low <= day_pdl and day_pdl < entry:
            reward = min(entry - day_pdl, 5.0 * risk)
            return f"Next_Day_PDL_Break_Exit_1:{reward / risk:.2f}", _pnl(entry, reward), None

    # 4) Profit lock / target
    reward = (high - entry) if is_buy else (entry - low)
    rr = reward / risk if risk > 0 else 0.0

    if rr >= 5.0:
        return "Target_1:5_Reached_Full_Exit", _pnl(entry, 5.0 * risk), None

    new_lock = 4.0 if rr >= 4.0 else (3.0 if rr >= 3.0 else (2.0 if rr >= 2.0 else 0.0))
    if new_lock > trade['locked_rr']:
        trade['pending_rr'] = new_lock
        trade['pending_sl'] = entry + new_lock * risk if is_buy else entry - new_lock * risk
        status = f"Profit_Locked_at_1:{new_lock:g}"
        shown_rr = new_lock
    elif trade['locked_rr'] > 0:
        status = f"Running_Locked_at_1:{trade['locked_rr']:g}"
        shown_rr = trade['locked_rr']
    else:
        status = "Running"
        shown_rr = 0.0

    return status, _pnl(entry, shown_rr * risk), trade


# ======================= SINGLE CANDLE STEP (backtest + live dono yahi use karte hain) =======================
def step_candle(row, trade, daily, allow_entry=None):
    """
    Ek candle process karo. Return: (status, profit, trade, pdh, pdl)
    allow_entry: optional function; False return kare to entry nahi li jaati (cap ke liye).
    """
    pdh, pdl = levels_for(row['Date'], daily)
    have_levels = pdh is not None
    if not have_levels:
        pdh = pdl = float('nan')

    # Trade kisi purane din ka hai to "carried over" mark karo
    if trade is not None and trade.get('entry_date') != str(row['Date']):
        trade['carried_over'] = True

    if trade is not None:
        status, profit, trade = manage_open_trade(trade, row, pdh, pdl)
        return status, profit, trade, pdh, pdl

    if not have_levels:
        return "No_Prev_Day_Data", 0.0, None, pdh, pdl

    status, new_trade = detect_signal(row, pdh, pdl)
    if new_trade is not None:
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
        # naya lock pending ho to wahi SL dikhao jo agli candle se lagega
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
    """Poore data par replay (backtest / --scan ke liye). Live alerts neeche scan_and_alert se aate hain."""
    df, daily = prepare(df)
    results = []
    trade = None
    for _, row in df.iterrows():
        status, profit, trade, pdh, pdl = step_candle(row, trade, daily)
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
    return out.iloc[:-1].reset_index(drop=True)   # chalu (adhoori) candle hata do


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


# ======================= TELEGRAM ALERTS + AUTO RUN =======================
SCRIPT_NAME = os.path.basename(__file__)
ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(ROOT, "rd_trading_state.json")
# Columns badal gaye hain (PDH/PDL/Entry/SL), isliye naya CSV naam taaki purani file ke saath header mix na ho
SIGNALS_FILE = os.path.join(ROOT, "rd_trading_signals_v2.csv")

ENTRY_STATUSES = {"Valid_Bullish_Breakout_BUY", "Valid_Bearish_Breakout_SELL"}
EXIT_PREFIXES = ("Stop_Loss_Hit", "SL_Hit_Exit_Locked", "Target_1:5", "Next_Day_PDH", "Next_Day_PDL")
LOCK_PREFIXES = ("Profit_Locked_at",)


def _tg_configured():
    return bool((os.getenv("TG_TOKEN") or os.getenv("BOT_TOKEN")) and (os.getenv("TG_CHAT_ID") or os.getenv("CHAT_ID")))


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


def send_alert(message):
    """True = bhej diya (ya Telegram configure nahi, sirf log). False = bhejna fail hua (agli cycle mein retry)."""
    if not _tg_configured():
        print("Telegram not configured; alert logged only:\n" + message)
        return True
    return telegram(message)


def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("sent", [])
    st.setdefault("trades", {})     # pair -> open trade (entry, SL, risk, lock, ...)
    st.setdefault("last_ts", {})    # pair -> last processed candle timestamp
    st.pop("open", None)            # purana format (sirf pair list) - ab trades se kaam hota hai
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
    icons = {"ENTRY": "🟢" if "BUY" in status else "🔴", "LOCK": "🔒", "EXIT": "🚪"}
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
        lines.append(f"New SL (agli candle se): {row['SL']}")
        lines.append(f"Locked PnL (100 USDT @ 30X): {row['Locked_PnL_USDT']} USDT")
    else:
        lines.append(f"PnL (100 USDT @ 30X): {row['Locked_PnL_USDT']} USDT")
    lines.append("PAPER ONLY - no real orders")
    return "\n".join(lines)


def live_events(df, trade, last_ts, allow_entry):
    """
    Sirf NAYI closed candles process karta hai (last_ts ke baad ki) aur open trade state se aage badhata hai.
    Return: (events, trade, new_last_ts); events = [(kind, record), ...]
    """
    if df.empty:
        return [], trade, last_ts
    df, daily = prepare(df)
    if last_ts:
        new = df[df['Timestamp'] > pd.Timestamp(last_ts)]
        if trade is None and len(new) > MAX_CATCHUP_BARS:
            new = new.tail(MAX_CATCHUP_BARS)   # bahut purane signals par alert nahi
    else:
        new = df.tail(FIRST_RUN_BARS)

    events = []
    for _, row in new.iterrows():
        status, profit, trade, pdh, pdl = step_candle(row, trade, daily, allow_entry)
        kind = _event_kind(status)
        if kind:
            events.append((kind, _record(row, status, profit, trade, pdh, pdl)))
    return events, trade, str(df['Timestamp'].iloc[-1])


def scan_and_alert():
    st = _load_state()
    sent = set(st["sent"])
    trades = st["trades"]
    last_ts = st["last_ts"]

    liquid = fetch_liquid_pairs()
    liquid_map = {p["pair"]: p["volume_24h_usdt"] for p in liquid}
    # Jinke trade khule hain unhe volume filter se bahar hone par bhi manage karte rahenge
    work = [p["pair"] for p in liquid] + [pr for pr in trades if pr not in liquid_map]
    print(f"[{SCRIPT_NAME}] Ticker volume filter >= {MIN_24H_VOLUME_USDT:,.0f} USDT: {len(liquid)} coins pass, "
          f"{len(trades)} open trade(s) in state")

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
            events, new_trade, new_last = live_events(df, trade, last_ts.get(pair), allow_entry)

            delivered = True
            entries_now = 0
            rows_now = []
            for kind, rec in events:
                key = f"{pair}|{rec['Timestamp']}|{rec['Status']}"
                if key not in sent:
                    if not send_alert(_format_alert(kind, pair, rec, vol24)):
                        delivered = False   # Telegram fail: is coin ka state commit nahi, agli cycle mein retry
                        break
                    sent.add(key)
                    st["sent"].append(key)
                if kind == "ENTRY":
                    entries_now += 1
                rows_now.append({"Script": SCRIPT_NAME, "Pair": pair, "Kind": kind, **rec,
                                 "Volume_24h_USDT": round(vol24)})
            if not delivered:
                continue

            # State commit: open trade (entry/SL/lock) + last processed candle
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
        _time.sleep(0.2)

    if new_rows:
        pd.DataFrame(new_rows).to_csv(SIGNALS_FILE, mode="a", header=not os.path.exists(SIGNALS_FILE), index=False)
    _save_state(st)
    print(f"[{SCRIPT_NAME}] cycle done: {len(new_rows)} new event(s), {len(trades)} open trade(s), "
          f"{candle_rejected} coin(s) rejected by 24h candle-volume check")


if __name__ == "__main__":
    if "--alert" in sys.argv:
        scan_and_alert()
        sys.exit(0)
    if "--scan" in sys.argv:
        out = scan_market()
        print(out.to_string(index=False) if not out.empty else "No data")
        sys.exit(0)

    # Demo: 5 din ka synthetic 15m data (pichle din ka PDH/PDL ke liye kam se kam 2 din chahiye)
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
    print("=== R&D PY (rd_trading.py) Execution Log (Prev-Day PDH/PDL + capped SL) ===")
    shown = res[~res['Status'].isin(['No_Trade', 'Running'])]
    print(shown.head(30).to_string(index=False) if not shown.empty else "No signals in demo data")
