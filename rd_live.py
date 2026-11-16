"""
rd_live.py - CoinDCX futures LIVE runner for the rd_trading.py strategy.

* rd_trading.py is NOT modified and keeps running as the paper scanner.
* Same signals / SL / profit lock / target / MACD loss-exit logic: it imports and calls
  rd_trading.step_candle() (that is the single source of truth for the strategy).
* Per trade size  : LIVE_MARGIN_USDT (default 10 USDT margin) x leverage (default 30,
                    capped by the pair's max leverage).
* MACD stop loss  : handled by this bot (no normal SL order on the exchange).
* EMERGENCY STOP  : a far stop_market order placed on the exchange right after entry
                    (entry -/+ min(1.5R, 2.5% of price), never nearer than the SL).
                    If it cannot be placed, the position is closed immediately.
* SAFE BY DEFAULT : unless LIVE_ENABLED=YES this is a DRY RUN (no orders, only alerts).

Usage:
    python rd_live.py --probe   # read-only check of keys / signing / wallet / positions
    python rd_live.py --alert   # one scan cycle (workflow calls this every 5 minutes)
"""
import csv
import hashlib
import hmac
import json
import math
import os
import sys
import time as _time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

import pandas as pd
import requests

import rd_trading as rd

API = "https://api.coindcx.com"
SCRIPT = "rd_live.py"
ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(ROOT, "rd_live_state.json")
LOG_FILE = os.path.join(ROOT, "rd_live_log.csv")

LIVE = os.getenv("LIVE_ENABLED", "NO").strip().upper() == "YES"
MARGIN_USDT = float(os.getenv("LIVE_MARGIN_USDT", "10"))
LIVE_LEVERAGE = float(os.getenv("LIVE_LEVERAGE", str(rd.LEVERAGE)))
MAX_LIVE_OPEN = int(os.getenv("MAX_LIVE_OPEN", "2"))
EMERGENCY_R = float(os.getenv("EMERGENCY_R", "1.5"))
EMERGENCY_MAX_PCT = float(os.getenv("EMERGENCY_MAX_PCT", "2.5"))
ENTRY_MAX_AGE_BARS = int(os.getenv("ENTRY_MAX_AGE_BARS", "2"))   # only act on entries from the newest N candles
LIQ_SAFETY = 0.8   # emergency stop must be inside 80% of the (1/leverage) liquidation distance

API_KEY = os.getenv("COINDCX_API_KEY", "")
API_SECRET = os.getenv("COINDCX_API_SECRET", "")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "RD-LIVE/1.0"})


class ApiError(RuntimeError):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


# ----------------------------------------------------------------- exchange client
class Client:
    def __init__(self, key, secret):
        self.key, self.secret = key, secret

    def _signed(self, method, path, body=None):
        if not self.key or not self.secret:
            raise ApiError("COINDCX_API_KEY / COINDCX_API_SECRET not set")
        body = dict(body or {})
        body["timestamp"] = int(_time.time() * 1000)
        payload = json.dumps(body, separators=(",", ":"))
        sig = hmac.new(self.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-AUTH-APIKEY": self.key, "X-AUTH-SIGNATURE": sig}
        r = HTTP.request(method, API + path, data=payload, headers=headers, timeout=25)
        if r.status_code >= 400:
            raise ApiError(f"{path} HTTP {r.status_code}: {r.text[:300]}", r.status_code)
        try:
            return r.json()
        except ValueError:
            raise ApiError(f"{path} non-JSON reply: {r.text[:200]}", r.status_code)

    # --- public
    def instrument(self, pair):
        r = HTTP.get(API + "/exchange/v1/derivatives/futures/data/instrument",
                     params={"pair": pair, "margin_currency_short_name": "USDT"}, timeout=25)
        r.raise_for_status()
        j = r.json()
        return j.get("instrument", j) if isinstance(j, dict) else {}

    # --- private
    def wallets(self):
        return self._signed("GET", "/exchange/v1/derivatives/futures/wallets")

    def positions(self, pair=None):
        body = {"page": "1", "size": "100", "margin_currency_short_name": ["USDT"]}
        if pair:
            body["pairs"] = pair
        j = self._signed("POST", "/exchange/v1/derivatives/futures/positions", body)
        if isinstance(j, dict):
            j = j.get("positions", j.get("data", []))
        return j or []

    def market_order(self, pair, side, qty, leverage):
        base = {"side": side, "pair": pair, "total_quantity": qty, "leverage": leverage,
                "notification": "no_notification", "margin_currency_short_name": "USDT",
                "position_margin_type": "isolated"}
        last = None
        # The docs are inconsistent about the order_type name and the {"order": {...}} wrapper:
        # try the likely forms; a 4xx means the exchange rejected it (nothing was created).
        for ot in ("market_order", "market"):
            for wrap in (True, False):
                o = dict(base, order_type=ot)
                try:
                    return self._signed("POST", "/exchange/v1/derivatives/futures/orders/create",
                                        {"order": o} if wrap else o)
                except ApiError as e:
                    last = e
                    if e.status is None or e.status >= 500:
                        raise          # unknown outcome: never retry blindly
        raise last

    def stop_loss(self, position_id, stop_price):
        return self._signed("POST", "/exchange/v1/derivatives/futures/positions/create_tpsl",
                            {"id": position_id,
                             "stop_loss": {"stop_price": stop_price, "order_type": "stop_market"}})

    def exit_position(self, position_id):
        return self._signed("POST", "/exchange/v1/derivatives/futures/positions/exit", {"id": position_id})


# ----------------------------------------------------------------- helpers
def _dec(x):
    return Decimal(str(x))


def round_down(x, inc):
    inc = _dec(inc)
    return float((_dec(x) / inc).to_integral_value(rounding=ROUND_DOWN) * inc)


def round_near(x, inc):
    inc = _dec(inc)
    return float((_dec(x) / inc).to_integral_value(rounding=ROUND_HALF_UP) * inc)


def tg(msg, dry=None):
    dry = (not LIVE) if dry is None else dry
    text = f"[{SCRIPT}]{' [DRY-RUN]' if dry else ' [LIVE]'} {msg}"
    return rd.send_alert(text)


def log_row(**kw):
    kw["ts"] = pd.Timestamp.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    kw["mode"] = "LIVE" if LIVE else "DRY"
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ts", "mode", "pair", "action", "side", "qty", "price", "note"])
        if new:
            w.writeheader()
        w.writerow({k: kw.get(k, "") for k in w.fieldnames})


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("trades", {})
    st.setdefault("last_ts", {})
    return st


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)


def emergency_dist(price, risk, sl):
    """Emergency-stop distance from the fill: 1.5R (max 2.5% of price, min R) and never nearer than the bot SL."""
    base = max(min(EMERGENCY_R * risk, price * EMERGENCY_MAX_PCT / 100), risk)
    return max(base, abs(price - sl) * 1.02) if sl else base


def plan_order(side, price, risk, inst, lev_wanted, sl=None):
    """Quantity, leverage and emergency-stop price. Returns (plan | None, reason)."""
    max_lev = float(inst.get("max_leverage_long" if side == "buy" else "max_leverage_short", lev_wanted) or lev_wanted)
    lev = min(lev_wanted, max_lev)
    qty_inc = inst.get("quantity_increment") or inst.get("min_quantity") or 0.001
    px_inc = inst.get("price_increment") or 0.0001
    min_qty = float(inst.get("min_quantity") or 0)
    min_notional = float(inst.get("min_notional") or 0)
    qty = round_down(MARGIN_USDT * lev / price, qty_inc)
    if qty < min_qty or qty <= 0:
        return None, f"quantity {qty} below min_quantity {min_qty}"
    if qty * price < min_notional:
        return None, f"notional {qty * price:.2f} below min_notional {min_notional}"
    dist = emergency_dist(price, risk, sl)
    if dist / price * 100 > LIQ_SAFETY * 100.0 / lev:
        return None, f"emergency stop {dist / price * 100:.2f}% too close to liquidation at {lev:g}x"
    stop = price - dist if side == "buy" else price + dist
    stop = round_near(stop, px_inc)
    return {"qty": qty, "lev": lev, "stop": stop, "dist_pct": dist / price * 100}, "ok"


def open_position(cli, pair, side, qty, lev, stop_ref_price, risk, sl=None):
    """Place the market order, find the position, attach the emergency stop.
    Returns (position_id, fill_price, stop_price). Never leaves a position without the emergency stop."""
    cli.market_order(pair, side, qty, lev)
    pos = None
    for _ in range(8):
        _time.sleep(2)
        for p in cli.positions(pair):
            if p.get("pair") == pair and abs(float(p.get("active_pos") or 0)) > 0:
                pos = p
                break
        if pos:
            break
    if not pos:
        raise ApiError("order sent but no open position found - CHECK THE EXCHANGE MANUALLY")
    fill = float(pos.get("avg_price") or stop_ref_price)
    inst = cli.instrument(pair)
    dist = min(emergency_dist(fill, risk, sl), fill * LIQ_SAFETY / lev)
    stop = fill - dist if side == "buy" else fill + dist
    stop = round_near(stop, inst.get("price_increment") or 0.0001)
    try:
        cli.stop_loss(pos["id"], stop)
    except Exception as e:
        closed = True
        try:
            cli.exit_position(pos["id"])
        except Exception as e2:
            closed = False
            e = f"{e}; AND closing it failed: {e2}"
        raise ApiError(f"emergency stop failed ({e}); " +
                       ("position was closed for safety" if closed else "POSITION IS STILL OPEN - CLOSE IT MANUALLY"))
    return pos["id"], fill, stop


# ----------------------------------------------------------------- one scan cycle
def scan_and_trade(cli=None):
    cli = cli or Client(API_KEY, API_SECRET)
    st = load_state()
    trades, last_ts = st["trades"], st["last_ts"]

    liquid = rd.fetch_liquid_pairs()
    liquid_map = {p["pair"]: p for p in liquid}
    work = [p for p in trades] + [p["pair"] for p in liquid if p["pair"] not in trades]

    exch_open = {}
    if LIVE:
        for p in cli.positions():
            if abs(float(p.get("active_pos") or 0)) > 0:
                exch_open[p.get("pair")] = p

    # 1) reconcile: trade in state but no position on the exchange -> closed there (emergency stop / manual)
    if LIVE:
        for pair in list(trades):
            if trades[pair].get("pos_id") and pair not in exch_open and not trades[pair].get("pending_exit"):
                tg(f"CLOSED ON EXCHANGE (emergency stop or manual): {pair}. Removed from bot state.")
                log_row(pair=pair, action="closed_on_exchange", note="emergency stop / manual")
                trades.pop(pair)
        for pair in exch_open:
            if pair not in trades:
                tg(f"WARNING: open position {pair} on the exchange is not tracked by the bot. Not touching it.")

    # 2) retry exits that failed earlier
    for pair, t in list(trades.items()):
        if t.get("pending_exit"):
            try:
                if LIVE:
                    cli.exit_position(t["pos_id"])
                tg(f"EXIT retried OK: {pair} ({t['pending_exit']})")
                log_row(pair=pair, action="exit_retry", note=t["pending_exit"])
                trades.pop(pair)
            except Exception as e:
                tg(f"EXIT RETRY FAILED {pair}: {e}  -> CLOSE IT MANUALLY IF IT KEEPS FAILING")

    counters = {"opened": 0}
    for pair in work:
        t = trades.get(pair)
        if t and t.get("pending_exit"):
            continue
        try:
            df = rd.fetch_candles_15m(pair)
            if df.empty:
                continue
            df, daily = rd.prepare(df)
            newest = df["Timestamp"].iloc[-1]
            if last_ts.get(pair):
                new = df[df["Timestamp"] > pd.Timestamp(last_ts[pair])]
                if t is None and len(new) > rd.MAX_CATCHUP_BARS:
                    new = new.tail(rd.MAX_CATCHUP_BARS)
            else:
                new = df.tail(rd.FIRST_RUN_BARS)

            if t is None:
                ok, _ = rd.candle_volume_ok(df)
                if not ok or pair not in liquid_map:
                    last_ts[pair] = str(newest)
                    continue

            ctx = {"fresh": False}

            def allow_entry():
                return (ctx["fresh"] and pair in liquid_map
                        and len(trades) < MAX_LIVE_OPEN and counters["opened"] < MAX_LIVE_OPEN)

            rdtrade = dict(t["rd"]) if t else None
            for _, row in new.iterrows():
                ctx["fresh"] = row["Timestamp"] >= newest - pd.Timedelta(minutes=15 * (ENTRY_MAX_AGE_BARS - 1))
                status, profit, rdtrade_next, pdh, pdl = rd.step_candle(row, rdtrade, daily, allow_entry)
                kind = rd._event_kind(status)

                if t is None and kind == "ENTRY" and rdtrade_next is not None:
                    side = "buy" if rdtrade_next["direction"] == "BUY" else "sell"
                    price = liquid_map[pair]["ltp"]
                    risk = rdtrade_next["risk"]
                    inst = cli.instrument(pair)
                    plan, why = plan_order(side, price, risk, inst, LIVE_LEVERAGE, rdtrade_next["sl"])
                    if not plan:
                        tg(f"ENTRY SKIPPED {pair} {side.upper()}: {why}")
                        log_row(pair=pair, action="entry_skipped", side=side, note=why)
                        rdtrade = None
                        continue
                    if LIVE:
                        try:
                            pos_id, fill, stop = open_position(cli, pair, side, plan["qty"], plan["lev"], price, risk, rdtrade_next["sl"])
                        except Exception as e:
                            tg(f"ENTRY FAILED {pair} {side.upper()}: {e}")
                            log_row(pair=pair, action="entry_failed", side=side, note=str(e)[:200])
                            rdtrade = None
                            continue
                    else:
                        pos_id, fill, stop = f"DRY-{pair}", price, plan["stop"]
                    t = {"rd": rdtrade_next, "pos_id": pos_id, "qty": plan["qty"], "lev": plan["lev"],
                         "side": side, "fill": fill, "emergency_stop": stop, "entry_ts": str(row["Timestamp"])}
                    trades[pair] = t
                    rdtrade = rdtrade_next
                    counters["opened"] += 1
                    save_state(st)
                    tg(f"ENTRY {side.upper()} {pair} | Session: {rdtrade_next.get('entry_session', row['Session'])}\nQty {plan['qty']} @ ~{fill} | {plan['lev']:g}x | margin {MARGIN_USDT} USDT\n"
                       f"Bot SL {rdtrade_next['sl']:.6g} (MACD rule) | Emergency stop {stop} ({plan['dist_pct']:.2f}% away)")
                    log_row(pair=pair, action="entry", side=side, qty=plan["qty"], price=fill, note=f"emergency {stop}")
                    continue

                if t is not None:
                    t["rd"] = rdtrade_next if rdtrade_next is not None else t["rd"]
                    if kind == "LOCK":
                        tg(f"LOCK {pair} ({t['rd'].get('entry_session', '?')} session): {status} -> new bot SL {rdtrade_next['pending_sl']:.6g}")
                        log_row(pair=pair, action="lock", note=status)
                    if kind == "EXIT":
                        try:
                            if LIVE:
                                cli.exit_position(t["pos_id"])
                            tg(f"EXIT {pair} {t['side'].upper()} | Session: {t['rd'].get('entry_session', '?')}\n{status}\n(paper-equivalent PnL {profit:.2f} USDT at 3000 USDT size)")
                            log_row(pair=pair, action="exit", side=t["side"], note=status)
                            trades.pop(pair, None)
                        except Exception as e:
                            t["pending_exit"] = status
                            tg(f"EXIT FAILED {pair}: {e}. Will retry next cycle.")
                            log_row(pair=pair, action="exit_failed", note=str(e)[:200])
                        t = trades.get(pair)
                        rdtrade = None if t is None else t["rd"]
                        if t is None:
                            break
                    else:
                        rdtrade = t["rd"]
                else:
                    rdtrade = None

            last_ts[pair] = str(newest)
        except Exception as e:
            print(f"skip {pair}: {e}")
        _time.sleep(0.2)

    save_state(st)


def probe():
    print(f"LIVE_ENABLED={LIVE} (orders {'WILL' if LIVE else 'will NOT'} be placed)")
    print(f"key length {len(API_KEY)} | secret length {len(API_SECRET)} | key == secret: {API_KEY == API_SECRET}")
    cli = Client(API_KEY, API_SECRET)
    inst = cli.instrument("B-BTC_USDT")
    print("instrument B-BTC_USDT:", {k: inst.get(k) for k in ("min_quantity", "quantity_increment", "price_increment",
                                                              "min_notional", "max_leverage_long", "max_leverage_short")})
    tests = [
        ("spot balances   (POST, any valid key)", lambda: cli._signed("POST", "/exchange/v1/users/balances")),
        ("futures wallets (GET)", cli.wallets),
        ("futures positions (POST)", cli.positions),
    ]
    for name, fn in tests:
        try:
            r = fn()
            size = len(r) if hasattr(r, "__len__") else "-"
            print(f"OK    {name}: reply received ({type(r).__name__}, size {size})")
        except Exception as e:
            print(f"FAIL  {name}: {str(e)[:160]}")


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
    elif "--alert" in sys.argv:
        scan_and_trade()
    else:
        print(__doc__)
