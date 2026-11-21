"""
gold_bpro2_paper.py - PAPER ONLY (no orders), Telegram alerts ON.  Gold strategy "B-Pro 2" on CoinDCX B-XAU_USDT.

Rules (same as the 1-year backtest rd_gold_bpro2.py, variant K5):
  1. Asian range = high / low of 05:30-13:30 IST (15m candles).
  2. Trend = previous day's close vs 20-day EMA of daily closes -> above: only BUY, below: only SELL.
  3. Entry window 13:30-20:30 IST: stop-order entry when price reaches Asian high + 0.05% (BUY) /
     Asian low - 0.05% (SELL). One trade per day.
  4. SL = other side of the Asian range, min 0.4%, max 1.2% of price.
  5. Stop moves to entry at +1.5R. Target 2.5R. Otherwise closed at 05:30 IST (end of the trading day).
  Fees 0.118% round trip; P&L shown for a 10 USDT position.
Every run replays the last ~45 days with exactly the backtest rules and sends Telegram for new ENTRY / BE / EXIT events.
Run:  python gold_bpro2_paper.py   |   python gold_bpro2_paper.py --report
"""
import csv
import json
import os
import sys
import time

import numpy as np
import pandas as pd

import rd_trading as rd
import rd_backtest_stops as bs

PAIR = os.getenv("GOLD_PAIR", "B-XAU_USDT")
STATE_FILE = "gold_bpro2_state.json"
LOG_FILE = "gold_bpro2_trades.csv"
START_DAY = os.getenv("GOLD_START_DAY", "2026-11-22")     # paper starts from this trading day
TAG = "[gold_bpro2_paper.py] PAPER"
FEE = 0.00118
NOTIONAL = 10.0
CFG = dict(ws=810, we=1230, buf=0.0005, slmin=0.004, slmax=0.012, tp=2.5, be=1.5)
bs.DAYS = 45


def load():
    _, res = bs.load_pair(PAIR)
    if res is None:
        return None, None
    df = pd.DataFrame(res[0])
    df["Timestamp"] = pd.to_datetime(df["Timestamp"])
    df["m"] = df["Timestamp"].dt.hour * 60 + df["Timestamp"].dt.minute
    df["D"] = df["Date"].astype(str)
    d = df.groupby("D").Close.last()
    trend = np.sign(d - d.ewm(span=20, adjust=False).mean()).shift(1).to_dict()
    return df.reset_index(drop=True), trend


def day_trade(day, t, now_ist, day_end):
    """Replay one trading day. Returns dict with the trade events or None."""
    a = day[(day.m >= 330) & (day.m < 810)]
    if len(a) < 28 or not t or t != t:
        return None
    ah, al = float(a.High.max()), float(a.Low.min())
    sgn = int(t)
    lvl, opp = (ah, al) if sgn == 1 else (al, ah)
    trig = lvl * (1 + sgn * CFG["buf"])
    rows = list(day[day.Timestamp > a.Timestamp.max()].itertuples(index=False))
    for i, r in enumerate(rows):
        if not (CFG["ws"] <= r.m < CFG["we"]):
            continue
        if not ((sgn == 1 and r.High >= trig) or (sgn == -1 and r.Low <= trig)):
            continue
        e = max(trig, r.Open) if sgn == 1 else min(trig, r.Open)
        R = min(max(abs(e - opp), CFG["slmin"] * e), CFG["slmax"] * e)
        sl = e - sgn * R
        tgt = e + sgn * CFG["tp"] * R
        out = {"dir": "BUY" if sgn == 1 else "SELL", "asian_high": ah, "asian_low": al, "entry_ts": str(r.Timestamp),
               "entry": e, "sl": sl, "target": tgt, "R": R, "be_ts": None, "exit_ts": None, "exit": None, "why": None}
        stop = sl
        for j in range(i, len(rows)):
            q = rows[j]
            if (sgn == 1 and q.Low <= stop) or (sgn == -1 and q.High >= stop):
                out.update(exit_ts=str(q.Timestamp), exit=stop, why="SL" if stop == sl else "BREAKEVEN")
                break
            if j == i:
                continue
            best = sgn * ((q.High if sgn == 1 else q.Low) - e) / R
            if best >= CFG["tp"]:
                out.update(exit_ts=str(q.Timestamp), exit=tgt, why="TARGET_2.5R")
                break
            if best >= CFG["be"] and stop == sl:
                stop = e
                out["be_ts"] = str(q.Timestamp)
        if out["exit"] is None and now_ist >= day_end:
            out.update(exit_ts=str(rows[-1].Timestamp), exit=float(rows[-1].Close), why="TIME_EXIT_05:30")
        if out["exit"] is not None:
            pct = sgn * (out["exit"] - e) / e - FEE
            out["pct"] = pct
            out["usdt10"] = NOTIONAL * pct
            out["r_result"] = pct * e / R
        return out
    return None


def send(msg):
    msg = f"{TAG}\n{msg}"
    print(msg)
    if os.getenv("GOLD_TELEGRAM", "YES").upper() == "YES":
        rd.telegram(msg)


def log(row):
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["Day", "Kind", "Dir", "Entry_Time_IST", "Entry", "SL", "Target", "Asian_High", "Asian_Low",
                        "Exit_Time_IST", "Exit", "Reason", "Pct_after_fees", "R", "PnL_USDT_10"])
        w.writerow(row)


def fmt(x):
    return f"{x:.2f}"


def run():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("sent", [])
    df, trend = load()
    if df is None:
        print("no data")
        return
    now_ist = pd.Timestamp.now("UTC").tz_convert("Asia/Kolkata").tz_localize(None)
    sent = set(st["sent"])
    open_now = None
    for d, day in df.groupby("D", sort=True):
        if d < START_DAY:
            continue
        day_end = pd.Timestamp(d) + pd.Timedelta(days=1, hours=5, minutes=30)
        tr = day_trade(day, trend.get(d), now_ist, day_end)
        if tr is None:
            continue
        k_e, k_b, k_x = f"{d}|ENTRY", f"{d}|BE", f"{d}|EXIT"
        if k_e not in sent:
            send(f"{'🟢' if tr['dir'] == 'BUY' else '🔴'} GOLD {tr['dir']} ENTRY ({PAIR})\n"
                 f"Day: {d} | Time (IST): {tr['entry_ts']}\n"
                 f"Asian range: {fmt(tr['asian_low'])} - {fmt(tr['asian_high'])}\n"
                 f"Entry: {fmt(tr['entry'])} | SL: {fmt(tr['sl'])} ({100 * tr['R'] / tr['entry']:.2f}%) | "
                 f"Target 2.5R: {fmt(tr['target'])}\nBreakeven at 1.5R, time exit 05:30 IST")
            log([d, "ENTRY", tr["dir"], tr["entry_ts"], round(tr["entry"], 2), round(tr["sl"], 2), round(tr["target"], 2),
                 round(tr["asian_high"], 2), round(tr["asian_low"], 2), "", "", "", "", "", ""])
            sent.add(k_e); st["sent"].append(k_e)
        if tr["be_ts"] and k_b not in sent and (tr["exit"] is None or tr["exit_ts"] >= tr["be_ts"]):
            send(f"🔒 GOLD {tr['dir']} at +1.5R -> SL moved to entry {fmt(tr['entry'])}\nTime (IST): {tr['be_ts']}")
            sent.add(k_b); st["sent"].append(k_b)
        if tr["exit"] is not None and k_x not in sent:
            send(f"{'✅' if tr['pct'] > 0 else '❌'} GOLD {tr['dir']} EXIT - {tr['why']}\n"
                 f"Day: {d} | Exit time (IST): {tr['exit_ts']}\nEntry {fmt(tr['entry'])} -> Exit {fmt(tr['exit'])}\n"
                 f"Result: {100 * tr['pct']:.2f}% after fees ({tr['r_result']:.2f}R) = {tr['usdt10']:.3f} USDT on 10 USDT")
            log([d, "EXIT", tr["dir"], tr["entry_ts"], round(tr["entry"], 2), round(tr["sl"], 2), round(tr["target"], 2),
                 round(tr["asian_high"], 2), round(tr["asian_low"], 2), tr["exit_ts"], round(tr["exit"], 2), tr["why"],
                 round(tr["pct"], 5), round(tr["r_result"], 3), round(tr["usdt10"], 4)])
            sent.add(k_x); st["sent"].append(k_x)
        if tr["exit"] is None:
            open_now = {"day": d, **{k: tr[k] for k in ("dir", "entry_ts", "entry", "sl", "target", "be_ts")}}
    st["open"] = open_now
    st["last_run_ist"] = str(now_ist)
    st["sent"] = st["sent"][-500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, default=str)
    print(f"done {now_ist} | open: {open_now}")


def report():
    if not os.path.exists(LOG_FILE):
        print("No trades yet.")
        return
    d = pd.read_csv(LOG_FILE)
    x = d[d.Kind == "EXIT"]
    print(f"Entries: {(d.Kind == 'ENTRY').sum()} | Closed: {len(x)}")
    if len(x):
        print(f"Win %: {100 * (x.PnL_USDT_10 > 0).mean():.1f} | Net: {x.PnL_USDT_10.sum():.3f} USDT on 10 USDT/trade "
              f"| Net R: {x.R.sum():.2f}")
        print(x.groupby("Reason").PnL_USDT_10.agg(["count", "sum"]).to_string())


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    else:
        run()
