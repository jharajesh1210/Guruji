"""
expert_paper.py - PAPER trading (no real orders) of expert_crypto_strategy.py on ALL CoinDCX USDT
futures coins whose 24h volume is >= 15 million USDT.

The strategy rules are imported unchanged from expert_crypto_strategy.py:
  entry  : fresh PDH/PDL break (previous close inside, this close beyond), max 0.75% beyond the level,
           volume > 1.3x 20-bar SMA, trend (close vs VWAP, EMA20 vs EMA50), RSI 52-72 / 28-48, MACD side
  SL     : candle low/high or EMA20 (0.4% - 2%), hard stop (no MACD wait)
  exits  : lock 1:2 / 1:3 / 1:4, target 1:5, exhaustion exit (RSI > 78 / < 22 + MACD flip, profit >= 1.5R)
This runner only adds: coin universe (24h volume >= EXPERT_MIN_VOL), one trade per coin, state between
runs, a CSV log with the entry reason and the exit reason, and Telegram alerts (TG_TOKEN / TG_CHAT_ID).
PnL is logged in R and in USDT at 25 USDT notional per trade (live size) after 0.118% fees.
Files: expert_paper_state.json, expert_paper_trades.csv
"""
import csv
import json
import os
import sys
import time

import pandas as pd

os.environ.setdefault("MIN_24H_VOLUME_USDT", os.getenv("EXPERT_MIN_VOL", "15000000"))
import rd_trading as rd                       # noqa: E402  (CoinDCX data helpers + Telegram)
import expert_crypto_strategy as ex           # noqa: E402  (the strategy rules, unchanged)

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "expert_paper_state.json")
TRADES_FILE = os.path.join(HERE, "expert_paper_trades.csv")
MIN_VOL = float(os.getenv("EXPERT_MIN_VOL", "15000000"))
NOTIONAL, FEE = 25.0, 0.00118
TAG = "[expert_paper.py] PAPER"
COLS = ["Time_IST", "Pair", "Kind", "Direction", "Entry", "SL", "Exit_Price", "Status", "R", "PnL_USDT_25", "Reason"]


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
        json.dump(st, f, indent=1, default=str)


def log(**kw):
    new = not os.path.exists(TRADES_FILE)
    with open(TRADES_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        if new:
            w.writeheader()
        w.writerow({c: kw.get(c, "") for c in COLS})


def alert(msg):
    try:
        rd.send_alert(f"{TAG}\n{msg}")
    except Exception as e:
        print("telegram:", e)


def candles(pair):
    d = rd.fetch_candles_15m(pair)                      # closed 15m candles, IST time
    if d.empty:
        return d
    d = d.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
    d["IST"] = d["Timestamp"]
    d["Timestamp"] = (d["Timestamp"] - pd.Timedelta(hours=5, minutes=30)).dt.tz_localize("UTC")
    return d


def run():
    st = load_state()
    trades, last_ts = st["trades"], st["last_ts"]
    liquid = [p["pair"] for p in rd.fetch_liquid_pairs(MIN_VOL)]
    work = list(trades) + [p for p in liquid if p not in trades]
    print(f"coins with 24h volume >= {MIN_VOL/1e6:.0f}M USDT: {len(liquid)}, open trades: {len(trades)}", flush=True)
    for pair in work:
        try:
            df = candles(pair)
            if len(df) < 120:
                continue
            ind = ex.add_indicators(df)
            last = last_ts.get(pair)
            idx = [i for i in range(1, len(ind)) if last is None or str(ind["IST"].iloc[i]) > last]
            if last is None:
                idx = idx[-1:]                          # first run for this coin: only the latest candle
            for i in idx:
                row, prev = ind.iloc[i], ind.iloc[i - 1]
                ist = str(row["IST"])
                t = trades.get(pair)
                if t is not None:
                    status, pnl_r, t2 = ex.manage_open_trade(row, t)
                    if t2 is None:
                        pct = pnl_r * t["risk"] / t["entry_price"]
                        usd = NOTIONAL * pct - NOTIONAL * FEE
                        r_net = pnl_r - FEE * t["entry_price"] / t["risk"]
                        exit_px = t["entry_price"] + (pnl_r * t["risk"] if t["direction"] == "BUY" else -pnl_r * t["risk"])
                        log(Time_IST=ist, Pair=pair, Kind="EXIT", Direction=t["direction"], Entry=t["entry_price"],
                            SL=t["initial_sl"], Exit_Price=round(exit_px, 8), Status=status, R=round(r_net, 2),
                            PnL_USDT_25=round(usd, 3), Reason=t.get("reason", ""))
                        alert(f"EXIT {pair} {t['direction']} | {status} | {r_net:+.2f}R | {usd:+.3f} USDT (25 USDT size)")
                        del trades[pair]
                    else:
                        if t2.get("locked_rr", 0) > t.get("_logged_lock", 0):
                            t2["_logged_lock"] = t2["locked_rr"]
                            log(Time_IST=ist, Pair=pair, Kind="LOCK", Direction=t2["direction"], Entry=t2["entry_price"],
                                SL=round(t2["sl"], 8), Status=f"Locked_1:{t2['locked_rr']:g}")
                        trades[pair] = t2
                elif pair in liquid:
                    sig = ex.check_entry_signal(row, prev)
                    if sig:
                        sig["entry_ist"] = ist
                        trades[pair] = sig
                        log(Time_IST=ist, Pair=pair, Kind="ENTRY", Direction=sig["direction"], Entry=sig["entry_price"],
                            SL=round(sig["sl"], 8), Status="ENTRY", Reason=sig["reason"])
                        alert(f"ENTRY {pair} {sig['direction']}\nEntry {sig['entry_price']:.6g} | SL {sig['sl']:.6g} | "
                              f"Target {sig['target']:.6g}\n{sig['reason']}")
            last_ts[pair] = str(ind["IST"].iloc[-1])
        except Exception as e:
            print(f"{pair}: {e}")
        time.sleep(0.15)
    save_state(st)
    print(f"done, open trades: {list(trades)}", flush=True)


def report(since=None):
    if not os.path.exists(TRADES_FILE):
        print("no trades yet")
        return
    x = pd.read_csv(TRADES_FILE)
    x["Time_IST"] = pd.to_datetime(x["Time_IST"])
    if since:
        x = x[x["Time_IST"] >= pd.Timestamp(since)]
    e = x[x["Kind"] == "EXIT"]
    print(f"Entries {int((x['Kind'] == 'ENTRY').sum())}, exits {len(e)}, wins {(e['PnL_USDT_25'] > 0).sum()}, "
          f"net {e['R'].sum():+.2f}R, {e['PnL_USDT_25'].sum():+.3f} USDT (25 USDT/trade, after fees)")
    if len(e):
        print(e.groupby("Status")["PnL_USDT_25"].agg(["count", "sum"]).round(3).to_string())


if __name__ == "__main__":
    if "--report" in sys.argv:
        report(sys.argv[sys.argv.index("--report") + 1] if len(sys.argv) > sys.argv.index("--report") + 1 else None)
    else:
        run()
