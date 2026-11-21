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
* ENTRY GUARD     : skips the entry if live price is already > ENTRY_MAX_SLIP_R (0.75R) past the paper entry.
* PROFIT LOCK     : on every paper lock (1:2/1:3/1:4) the exchange stop is moved to the locked SL.
* PROFIT BLOCK    : after a profit exit (incl. locked stop / manual close in profit) the same coin is not
                    entered again in the same direction until that session (Asian/London/NY) ends.
* FAST LOCK       : --watch (every minute between scans) applies 1:2/1:3/1:4 locks the moment price touches
                    them and closes at 1:5, instead of waiting for the 15m candle to close.
* PAPER MIRROR    : live enters ONLY trades the paper scanner took (same pair / candle / direction),
                    read from rd_trading_state.json on GitHub each scan (MIRROR_PAPER=YES).
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
# Entry chase guard: paper enters at the breakout candle close. If the live price has already run more than
# ENTRY_MAX_SLIP_R (0.75, backtested 30 days: better than 0.3) x risk past that close (against us), skip - a late, worse fill makes the 1:2 lock unreachable.
ENTRY_MAX_SLIP_R = float(os.getenv("ENTRY_MAX_SLIP_R", "999"))   # 21 Nov: OFF (live = exactly rd_trading.py); was 0.75
# Profit lock on the exchange: when paper locks 1:2 / 1:3 / 1:4, move the exchange stop to that locked level
# (instead of only the far emergency stop) so the locked profit is protected between bot cycles.
LOCK_ON_EXCHANGE = os.getenv("LOCK_ON_EXCHANGE", "YES").strip().upper() == "YES"
# 21 Nov: minute-by-minute fast lock OFF -> locks / 1:6 exit happen exactly when rd_trading.py decides them
# (15m candle close). Set FAST_LOCK=YES in /etc/rd_live.env to turn the 1-minute watcher back on.
FAST_LOCK = os.getenv("FAST_LOCK", "NO").strip().upper() == "YES"
# Live = copy of paper: a live entry is taken only if the paper scanner (GitHub) took the same trade
# (same pair, same entry candle, same direction). Paper state is read from GitHub every scan.
MIRROR_PAPER = os.getenv("MIRROR_PAPER", "YES").strip().upper() == "YES"
PAPER_STATE_URLS = (
    "https://api.github.com/repos/jharajesh1210/Guruji/contents/rd_trading_state.json?ref=main",
    "https://raw.githubusercontent.com/jharajesh1210/Guruji/main/rd_trading_state.json",
)
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
    kw["ts"] = pd.Timestamp.now("UTC").strftime("%Y-%m-%d %H:%M:%S")
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
    st.setdefault("profit_block", {})
    st.setdefault("mirrored", {})     # pair -> paper entry_ts already copied (never copy the same paper trade twice)
    return st


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)


def fresh_ltp(pair):
    """Latest futures price for one pair (public ticker), None if unavailable."""
    try:
        q = (rd.http_json(rd.PRICE_URL).get("prices") or {}).get(pair) or {}
        px = float(q.get("ls") or 0)
        return px if px > 0 else None
    except Exception:
        return None


def entry_slip_r(side, paper_entry, price, risk):
    """How far (in R) the live price is past the paper entry, against us (+ = worse)."""
    if not risk:
        return 0.0
    move = (price - paper_entry) if side == "buy" else (paper_entry - price)
    return move / risk


def move_stop_to_lock(cli, pair, t, lock_sl):
    """Put the locked-profit SL on the exchange. If price is already through it, close now.
    Returns a short status text."""
    side = t["side"]
    inst = cli.instrument(pair)
    stop = round_near(lock_sl, inst.get("price_increment") or 0.0001)
    ltp = fresh_ltp(pair)
    through = ltp is not None and ((side == "buy" and ltp <= stop) or (side == "sell" and ltp >= stop))
    if through:
        cli.exit_position(t["pos_id"])
        return f"price {ltp} already past lock {stop} -> closed at market"
    cli.stop_loss(t["pos_id"], stop)
    t["emergency_stop"] = stop
    t["lock_on_exchange"] = stop
    t.pop("lock_stop_retry", None)
    return f"exchange stop moved to lock {stop}"


def load_paper_trades():
    """Open trades of the paper scanner (rd_trading_state.json on GitHub main). None if unavailable."""
    for url in PAPER_STATE_URLS:
        try:
            r = HTTP.get(url, headers={"Accept": "application/vnd.github.raw", "Cache-Control": "no-cache"},
                         params={"t": int(_time.time())} if "raw.githubusercontent" in url else None, timeout=20)
            if r.status_code == 200:
                return (r.json() or {}).get("trades", {})
        except Exception:
            pass
    return None


def emergency_dist(price, risk, sl):
    """Emergency (backstop) stop distance from the fill: EMERGENCY_MAX_PCT of price (2.5%), and never nearer than the bot SL.
    It is a far disaster stop, so the MACD loss-exit rule keeps running the trade like the paper strategy."""
    far = price * EMERGENCY_MAX_PCT / 100
    return max(far, abs(price - sl) * 1.02) if sl else far


def plan_order(side, price, risk, inst, lev_wanted, sl=None):
    """Quantity, leverage and emergency-stop price. Returns (plan | None, reason)."""
    ml = inst.get("max_leverage_long" if side == "buy" else "max_leverage_short")
    if not ml:   # some pairs (e.g. B-BLESS_USDT) give null here and only list tiers in dynamic_position_leverage_details
        tiers = inst.get("dynamic_position_leverage_details") or {}
        ml = max((float(k) for k in tiers), default=lev_wanted)
    max_lev = float(ml)
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
    # loss cap (same rule as paper): exchange stop at 1.5R or 3% from the paper entry, whichever is nearer
    if sl and risk and (rd.LOSS_CAP_R > 0 or rd.LOSS_CAP_PCT > 0):
        e_paper = sl + risk if side == "buy" else sl - risk
        cap = min(rd.LOSS_CAP_R * risk if rd.LOSS_CAP_R > 0 else 1e18,
                  e_paper * rd.LOSS_CAP_PCT / 100 if rd.LOSS_CAP_PCT > 0 else 1e18)
        cap_px = e_paper - cap if side == "buy" else e_paper + cap
        cap_from_fill = (fill - cap_px) if side == "buy" else (cap_px - fill)
        if cap_from_fill > 0:
            dist = min(dist, cap_from_fill)
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
def live_cycle(cli=None, paper_local=None):
    """Prepare one live scan. Returns (do_pair, finish). do_pair(pair, paper_trades) handles one coin;
    finish() handles the coins not visited yet and saves the state."""
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
                tr = trades[pair]
                why = (f"locked-profit stop {tr['lock_on_exchange']}" if tr.get("lock_on_exchange")
                       else "loss-cap stop (1.5R/3%) or manual")
                # profit exit (locked, or closed by hand while in profit) -> same coin, same direction blocked this session
                rdt = tr.get("rd") or {}
                ltp = fresh_ltp(pair)
                in_profit = rdt.get("locked_rr", 0) > 0 or rdt.get("pending_rr", 0) > 0 or (
                    ltp is not None and rdt.get("entry_price") and
                    ((ltp > rdt["entry_price"]) if rdt.get("direction") == "BUY" else (ltp < rdt["entry_price"])))
                if in_profit and rdt.get("direction"):
                    st["profit_block"].setdefault(pair, {})[rdt["direction"]] = rd.session_key_now()
                    why += f"; in profit -> no new {rdt['direction']} on {pair} until this session ends"
                tg(f"CLOSED ON EXCHANGE ({why}): {pair}. Removed from bot state.")
                log_row(pair=pair, action="closed_on_exchange", note=why)
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

    # 3) retry exchange lock stops that failed earlier
    if LIVE and LOCK_ON_EXCHANGE:
        for pair, t in list(trades.items()):
            if t.get("lock_stop_retry") and not t.get("pending_exit"):
                try:
                    note = move_stop_to_lock(cli, pair, t, t["lock_stop_retry"])
                    tg(f"LOCK STOP retry {pair}: {note}")
                    log_row(pair=pair, action="lock_retry", note=note)
                    if "closed at market" in note:
                        st["profit_block"].setdefault(pair, {})[t["rd"]["direction"]] = rd.session_key_now()
                        trades.pop(pair)
                except Exception as e:
                    tg(f"LOCK STOP RETRY FAILED {pair}: {str(e)[:150]}")

    paper = dict(paper_local) if paper_local is not None else (load_paper_trades() if MIRROR_PAPER else {})
    if MIRROR_PAPER and paper is None:
        print("paper state not readable from GitHub -> no new live entries this cycle")
        paper = {}

    counters = {"opened": 0}
    done = set()

    def do_pair(pair, paper_now=None):
        if paper_now is not None:
            paper.clear()
            paper.update(paper_now)
        if pair in done:
            return
        done.add(pair)
        t = trades.get(pair)
        if t is None and pair not in liquid_map and pair not in work:
            return
        if t and t.get("pending_exit"):
            return
        try:
            df = rd.fetch_candles_15m(pair)
            if df.empty:
                return
            df, daily = rd.prepare(df)
            newest = df["Timestamp"].iloc[-1]
            if last_ts.get(pair):
                new = df[df["Timestamp"] > pd.Timestamp(last_ts[pair])]
                if t is None and len(new) > rd.MAX_CATCHUP_BARS:
                    new = new.tail(rd.MAX_CATCHUP_BARS)
            else:
                new = df.tail(rd.FIRST_RUN_BARS)

            # paper took a trade here that live has not copied yet -> look at its entry candle again
            pt = paper.get(pair) if MIRROR_PAPER else None
            if t is None and pt and st["mirrored"].get(pair) != str(pt.get("entry_ts")):
                ets = pd.Timestamp(pt["entry_ts"])
                if ets >= newest - pd.Timedelta(minutes=15 * (ENTRY_MAX_AGE_BARS - 1)):
                    new = df[df["Timestamp"] >= ets]

            if t is None:
                ok, _ = rd.candle_volume_ok(df)
                if not ok or pair not in liquid_map:
                    last_ts[pair] = str(newest)
                    return

            ctx = {"fresh": False, "ts": None}

            def in_paper():
                if not MIRROR_PAPER:
                    return True
                pt = paper.get(pair)
                return (bool(pt) and str(pt.get("entry_ts")) == str(ctx["ts"])
                        and st["mirrored"].get(pair) != str(pt.get("entry_ts")))

            def allow_entry():
                return (ctx["fresh"] and pair in liquid_map and in_paper()
                        and len(trades) < MAX_LIVE_OPEN and counters["opened"] < MAX_LIVE_OPEN)

            rdtrade = dict(t["rd"]) if t else None
            for _, row in new.iterrows():
                ctx["fresh"] = row["Timestamp"] >= newest - pd.Timedelta(minutes=15 * (ENTRY_MAX_AGE_BARS - 1))
                ctx["ts"] = row["Timestamp"]
                status, profit, rdtrade_next, pdh, pdl = rd.step_candle(row, rdtrade, daily, allow_entry,
                                                                           st["profit_block"].setdefault(pair, {}))
                kind = rd._event_kind(status)

                if t is None and kind == "ENTRY" and rdtrade_next is not None:
                    if MIRROR_PAPER and paper.get(pair, {}).get("direction") != rdtrade_next["direction"]:
                        rdtrade = None
                        continue
                    st["mirrored"][pair] = str(row["Timestamp"])
                    side = "buy" if rdtrade_next["direction"] == "BUY" else "sell"
                    price = fresh_ltp(pair) or liquid_map[pair]["ltp"]
                    risk = rdtrade_next["risk"]
                    slip = entry_slip_r(side, rdtrade_next["entry_price"], price, risk)
                    sl_px = rdtrade_next["sl"]
                    past_sl = (side == "buy" and price <= sl_px) or (side == "sell" and price >= sl_px)
                    if slip > ENTRY_MAX_SLIP_R or past_sl:
                        why = (f"price {price} already past SL {sl_px:.6g}" if past_sl else
                               f"late entry: price {price} is {slip:.2f}R past paper entry {rdtrade_next['entry_price']} (max {ENTRY_MAX_SLIP_R}R)")
                        tg(f"ENTRY SKIPPED {pair} {side.upper()}: {why}")
                        log_row(pair=pair, action="entry_skipped", side=side, price=price, note=why)
                        rdtrade = None
                        continue
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
                            tg(f"ENTRY FAILED {pair} {side.upper()}: {e}\nsent qty={plan['qty']} lev={plan['lev']:g} price~{price} | instrument {inst}")
                            log_row(pair=pair, action="entry_failed", side=side, note=str(e)[:200])
                            rdtrade = None
                            continue
                    else:
                        pos_id, fill, stop = f"DRY-{pair}", price, plan["stop"]
                    t = {"rd": rdtrade_next, "pos_id": pos_id, "qty": plan["qty"], "lev": plan["lev"],
                         "side": side, "fill": fill, "emergency_stop": stop, "entry_ts": str(row["Timestamp"]),
                         "paper_entry": rdtrade_next["entry_price"]}
                    trades[pair] = t
                    rdtrade = rdtrade_next
                    counters["opened"] += 1
                    save_state(st)
                    tg(f"ENTRY {side.upper()} {pair} | Session: {rdtrade_next.get('entry_session', row['Session'])}\nQty {plan['qty']} @ ~{fill} (paper entry {rdtrade_next['entry_price']:.6g}, slip {entry_slip_r(side, rdtrade_next['entry_price'], fill, risk):+.2f}R) | {plan['lev']:g}x | margin {MARGIN_USDT} USDT\n"
                       f"Bot SL {rdtrade_next['sl']:.6g} (MACD rule) | Emergency stop {stop} ({plan['dist_pct']:.2f}% away)")
                    log_row(pair=pair, action="entry", side=side, qty=plan["qty"], price=fill, note=f"emergency {stop}")
                    continue

                if t is not None:
                    t["rd"] = rdtrade_next if rdtrade_next is not None else t["rd"]
                    if kind == "LOCK":
                        lock_sl = rdtrade_next["pending_sl"]
                        note = "bot SL only"
                        if LIVE and LOCK_ON_EXCHANGE:
                            try:
                                note = move_stop_to_lock(cli, pair, t, lock_sl)
                                if "closed at market" in note:
                                    tg(f"LOCK {pair}: {status} -> {note}")
                                    log_row(pair=pair, action="lock_exit", note=f"{status}; {note}")
                                    st["profit_block"].setdefault(pair, {})[t["rd"]["direction"]] = rd.session_key(row["Date"], row["Session"])
                                    trades.pop(pair, None)
                                    t = None
                                    break
                            except Exception as e:
                                t["lock_stop_retry"] = lock_sl
                                note = f"EXCHANGE LOCK STOP FAILED ({str(e)[:150]}) - will retry next cycle; bot SL still active"
                        tg(f"LOCK {pair} ({t['rd'].get('entry_session', '?')} session): {status} -> new SL {lock_sl:.6g} | {note}")
                        log_row(pair=pair, action="lock", note=f"{status}; {note}")
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


    def finish():
        for pair in work:
            do_pair(pair)
        save_state(st)

    return do_pair, finish


def scan_and_trade(cli=None):
    do_pair, finish = live_cycle(cli)
    finish()


def paper_then_live(cli=None):
    """Paper scanner and live runner in ONE pass on this server: each coin is scanned by paper first and,
    if paper enters, live copies it a few seconds later. Paper state lives in RD_STATE_FILE (server copy)."""
    rd.STATE_FILE = os.getenv("RD_STATE_FILE") or os.path.join(ROOT, "rd_trading_state_server.json")
    rd.SIGNALS_FILE = os.getenv("RD_SIGNALS_FILE") or os.path.join(ROOT, "rd_trading_signals_server.csv")
    do_pair, finish = live_cycle(cli, paper_local={})
    rd.scan_and_alert(after_pair=lambda pair, paper_trades: do_pair(pair, paper_trades))
    finish()


# ----------------------------------------------------------------- fast lock watcher (every minute)
def recent_extremes(pair, seconds=180):
    """High / low of the last few 1-minute candles (including the one still forming)."""
    now = int(_time.time())
    try:
        payload = rd.http_json(rd.CANDLE_URL, {"pair": pair, "from": now - seconds, "to": now,
                                               "resolution": "1", "pcode": "f"})
        raw = payload.get("data", payload.get("candles", [])) if isinstance(payload, dict) else payload
        hi = max(float(c["high"]) for c in raw)
        lo = min(float(c["low"]) for c in raw)
        return hi, lo
    except Exception:
        return None, None


def watch_locks(cli=None):
    """Between 15m candles: as soon as price touches 1:2 / 1:3 / 1:4 the lock is applied at once
    (bot SL + exchange stop); at 1:5 the trade is closed. Does not open any trade."""
    if not FAST_LOCK:
        return
    cli = cli or Client(API_KEY, API_SECRET)
    st = load_state()
    trades = st["trades"]
    changed = False
    for pair, t in list(trades.items()):
        if t.get("pending_exit") or not t.get("pos_id"):
            continue
        r = t["rd"]
        risk, entry, is_buy = r.get("risk") or 0, r["entry_price"], r["direction"] == "BUY"
        if risk <= 0:
            continue
        hi, lo = recent_extremes(pair)
        if hi is None:
            continue
        best = ((hi - entry) if is_buy else (entry - lo)) / risk
        have = max(r.get("locked_rr", 0), r.get("pending_rr", 0))
        try:
            if best >= rd.TARGET_RR:
                if LIVE:
                    cli.exit_position(t["pos_id"])
                st["profit_block"].setdefault(pair, {})[r["direction"]] = rd.session_key_now()
                tg(f"TARGET 1:{rd.TARGET_RR:g} {pair} (fast check) -> full exit at market")
                log_row(pair=pair, action="exit", side=t["side"], note=f"Target_1:{rd.TARGET_RR:g} fast")
                trades.pop(pair)
                changed = True
                continue
            new_lock = max([l for l in rd.LOCK_LEVELS if best >= l], default=0.0)
            if new_lock <= have:
                continue
            lock_sl = entry + new_lock * risk if is_buy else entry - new_lock * risk
            r["sl"], r["locked_rr"], r["pending_sl"], r["pending_rr"] = lock_sl, new_lock, None, 0.0
            changed = True
            note = "bot SL only (dry run)"
            if LIVE:
                note = move_stop_to_lock(cli, pair, t, lock_sl)
            tg(f"LOCK 1:{new_lock:g} {pair} (fast, same candle) -> SL {lock_sl:.6g} | {note}")
            log_row(pair=pair, action="lock_fast", note=f"1:{new_lock:g}; {note}")
            if "closed at market" in note:
                st["profit_block"].setdefault(pair, {})[r["direction"]] = rd.session_key_now()
                trades.pop(pair)
        except Exception as e:
            t["lock_stop_retry"] = r.get("sl")
            changed = True
            tg(f"FAST LOCK FAILED {pair}: {str(e)[:150]} - will retry; bot SL still active")
    if changed:
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
    elif "--combined" in sys.argv:
        paper_then_live()
    elif "--watch" in sys.argv:
        watch_locks()
    else:
        print(__doc__)
