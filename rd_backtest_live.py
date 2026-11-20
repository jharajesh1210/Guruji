"""
rd_backtest_live.py - backtest of the two LIVE settings, using 1-minute data:

  (a) late-entry guard: skip the live entry if price is already > X R past the paper entry
      X = 0.3 (now) / 0.5 / 0.75 / 1.0 / no guard
  (b) profit lock timing on live:
      FAST   = every minute: as soon as price touches 1:2/1:3/1:4 the stop moves there (now)
      CANDLE = lock decided on the 15m candle close (like paper), stop then put on the exchange

How:
- Paper trades come from rd_trading.py's own logic (step_candle, caps 5 open / 5 per cycle,
  profit-exit same-session block) on 15m data, all coins in time order.
- For every paper trade the 1m candles are fetched; live is simulated on them:
  fill = mid of the first 1m candle after the entry candle closes, loss exit (MACD rule) at the
  15m close, lock stop fills at the lock level (or the 1m open if price gapped through it),
  1:5 target exits at the 1m close when touched, fees 0.118% of notional round trip.
- Result in R (risk of the paper trade) and in USDT for the live size (2 USDT x 10x = 20 USDT).
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests

import rd_trading as rd
import rd_backtest_stops as bs

DAYS = int(os.getenv("BT_DAYS", "30"))
bs.DAYS = DAYS
WORKERS = int(os.getenv("BT_WORKERS", "6"))
OUT = os.getenv("BT_OUT", "backtest_out")
LIVE_NOTIONAL = float(os.getenv("BT_LIVE_NOTIONAL", "20"))
FEE_RT = float(os.getenv("BT_FEE_RT_PCT", "0.118")) / 100      # 0.05% x 2 sides + 18% GST
EMERG = 0.07
GUARDS = [0.3, 0.5, 0.75, 1.0, None]
MODES = ["FAST", "CANDLE"]


# ---------------------------- paper trades (15m) ----------------------------
def paper_trades(data, order):
    ev = []
    for pi, pair in enumerate(order):
        for ri, r in enumerate(data[pair][0]):
            ev.append((r["Timestamp"], pi, ri))
    ev.sort()
    open_tr, out, blocks = {}, [], {p: {} for p in order}
    i, n = 0, len(ev)
    while i < n:
        ts = ev[i][0]
        j = i
        while j < n and ev[j][0] == ts:
            j += 1
        group = sorted(ev[i:j], key=lambda e: (0 if order[e[1]] in open_tr else 1, e[1]))
        i = j
        new_entries = 0
        for _, pi, ri in group:
            pair = order[pi]
            recs, daily = data[pair]
            row = recs[ri]
            cur = open_tr.get(pair)
            trade = cur["t"] if cur else None

            def allow():
                qv = row.get("QV24")
                return (qv == qv and qv >= rd.MIN_24H_VOLUME_USDT and len(open_tr) < rd.MAX_OPEN_TRADES
                        and new_entries < rd.MAX_NEW_ENTRIES_PER_CYCLE)

            status, profit, trade2, pdh, pdl = rd.step_candle(row, trade, daily, allow, blocks[pair])
            if cur is not None:
                if trade2 is None:
                    cur.update(exit_ts=ts, exit_ri=ri, status=status, pnl=float(profit))
                    out.append(cur)
                    del open_tr[pair]
                else:
                    cur["t"] = trade2
                continue
            if trade2 is not None:
                open_tr[pair] = {"pair": pair, "entry_ts": ts, "entry_ri": ri, "dir": trade2["direction"],
                                 "E": trade2["entry_price"], "SL": trade2["sl"], "R": trade2["risk"],
                                 "date": row["Date"], "t": trade2}
                new_entries += 1
    return out


# ---------------------------- 1m data ----------------------------
def fetch_1m(pair, t0, t1):
    frames, beg = [], t0
    while beg < t1:
        end = min(t1, beg + 12 * 3600)
        raw = []
        for attempt in range(3):
            try:
                r = requests.get(rd.CANDLE_URL, params={"pair": pair, "from": beg, "to": end,
                                                        "resolution": "1", "pcode": "f"},
                                 headers={"User-Agent": "RD-BACKTEST/1.0"}, timeout=25)
                r.raise_for_status()
                j = r.json()
                raw = (j.get("data", j.get("candles", [])) if isinstance(j, dict) else j) or []
                break
            except Exception:
                time.sleep(1 + attempt)
        if raw:
            frames.append(pd.DataFrame(raw))
        beg = end
    if not frames:
        return None
    d = pd.concat(frames, ignore_index=True)
    if not set(bs.NEED).issubset(d.columns):
        return None
    for c in bs.NEED:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=bs.NEED).drop_duplicates("time").sort_values("time")
    d["ts"] = pd.to_datetime(d["time"], unit="ms", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    return d[["ts", "open", "high", "low", "close"]].reset_index(drop=True)


def load_1m(tr):
    t0 = int((pd.Timestamp(tr["entry_ts"]).tz_localize("Asia/Kolkata") + pd.Timedelta(minutes=15)).timestamp())
    end_ts = tr.get("exit_ts") or tr["entry_ts"]
    t1 = int((pd.Timestamp(end_ts).tz_localize("Asia/Kolkata") + pd.Timedelta(minutes=16)).timestamp())
    t1 = min(t1, t0 + 3 * 86400)
    m = fetch_1m(tr["pair"], t0, t1)
    return m


# ---------------------------- live simulation of one trade ----------------------------
from collections import namedtuple
Min = namedtuple("Min", "ts open high low close")

def sim_live(tr, recs, daily, m1, mode, guard, ideal_fill=False, fees=True, lock_lag=0.0, be=False,
             cap_r=None, cap_pct=None):
    """Returns dict(result) or None if skipped by the guard."""
    buy = tr["dir"] == "BUY"
    E, R, SL = tr["E"], tr["R"], tr["SL"]
    sgn = 1 if buy else -1
    first = m1.iloc[0]
    fill = E if ideal_fill else (first["open"] + first["close"]) / 2
    slip = sgn * (fill - E) / R
    past_sl = (buy and fill <= SL) or ((not buy) and fill >= SL)
    if past_sl or (guard is not None and slip > guard):
        return None
    locked, stop = 0, None
    emerg = fill * (1 - EMERG) if buy else fill * (1 + EMERG)
    exit_px, why = None, None
    entry_date = tr["date"]
    mins = m1.iloc[1:]                     # minutes after the fill minute
    mins_by_bar = {}
    for row in mins.itertuples(index=False):
        bar = row.ts.floor("15min")
        mins_by_bar.setdefault(bar, []).append(row)
    ri = tr["entry_ri"] + 1
    first_bar = True
    while ri < len(recs) and exit_px is None:
        c = recs[ri]
        bar_mins = mins_by_bar.get(c["Timestamp"])
        if not bar_mins:   # no 1m data for this bar -> use the 15m candle as one step
            if first_bar:
                bar_mins = []
            else:
                bar_mins = [Min(c["Timestamp"], float(c["Open"]), float(c["High"]), float(c["Low"]), float(c["Close"]))]
        first_bar = False
        # ---- minute loop
        for mm in bar_mins:
            if stop is not None and ((buy and mm.low <= stop) or ((not buy) and mm.high >= stop)):
                exit_px = min(stop, mm.open) if buy else max(stop, mm.open)
                why = f"lock_stop_1:{locked:g}"
                break
            if locked == 0 and (cap_r is not None or cap_pct is not None):
                dist = min(cap_r * R if cap_r is not None else 1e18, cap_pct * E if cap_pct is not None else 1e18)
                lvl = E - sgn * dist
                if (buy and mm.low <= lvl) or ((not buy) and mm.high >= lvl):
                    exit_px = min(lvl, mm.open) if buy else max(lvl, mm.open)
                    why = "hard_cap"
                    break
            if locked == 0 and ((buy and mm.low <= emerg) or ((not buy) and mm.high >= emerg)):
                exit_px, why = emerg, "emergency_7pct"
                break
            best = sgn * ((mm.high if buy else mm.low) - E) / R
            if best >= getattr(rd, "TARGET_RR", 5.0):      # watcher sees target -> market exit at about the minute close
                exit_px, why = mm.close, "target"
                break
            if mode == "FAST":
                L = max([l for l in getattr(rd, "LOCK_LEVELS", (2.0, 3.0, 4.0)) if best >= l], default=0.0)
                if L > locked:
                    lvl = 0.0 if (be and L == 2.0) else L - lock_lag
                    locked, stop = L, E + sgn * lvl * R
                    if (buy and mm.close <= stop) or ((not buy) and mm.close >= stop):
                        exit_px, why = mm.close, f"lock_fast_market_1:{L:g}"
                        break
        if exit_px is not None:
            break
        # ---- 15m candle close
        pdh, pdl = rd.levels_for(c["Date"], daily)
        hi, lo, cl = float(c["High"]), float(c["Low"]), float(c["Close"])
        if locked == 0:
            sl_hit = (buy and lo <= SL) or ((not buy) and hi >= SL)
            if sl_hit:
                macd, sig = float(c.get("MACD", 0)), float(c.get("MACD_Signal", 0))
                if (buy and macd < sig) or ((not buy) and macd > sig):
                    exit_px, why = cl, "macd_loss"
                    break
        if str(c["Date"]) != str(entry_date) and pdh is not None and pdh == pdh:
            if buy and hi >= pdh and pdh > E:
                exit_px, why = min(pdh, E + getattr(rd, "TARGET_RR", 5.0) * R), "next_day_pdh"
                break
            if (not buy) and lo <= pdl and pdl < E:
                exit_px, why = max(pdl, E - getattr(rd, "TARGET_RR", 5.0) * R), "next_day_pdl"
                break
        if mode == "CANDLE":
            best = sgn * ((hi if buy else lo) - E) / R
            L = max([l for l in getattr(rd, "LOCK_LEVELS", (2.0, 3.0, 4.0)) if best >= l], default=0.0)
            if L > locked:
                lvl = 0.0 if (be and L == 2.0) else L - lock_lag
                locked, stop = L, E + sgn * lvl * R
                if (buy and cl <= stop) or ((not buy) and cl >= stop):
                    exit_px, why = cl, f"lock_candle_market_1:{L:g}"
                    break
        ri += 1
        if ri - tr["entry_ri"] > 5 * 96:
            break
    if exit_px is None:
        exit_px, why = float(recs[min(ri, len(recs) - 1)]["Close"]), "open_at_end"
    gross_r = sgn * (exit_px - fill) / R
    fee_r = FEE_RT * fill / R if fees else 0.0
    usdt = LIVE_NOTIONAL * sgn * (exit_px - fill) / fill - (LIVE_NOTIONAL * FEE_RT if fees else 0.0)
    return {"pair": tr["pair"], "entry_ts": tr["entry_ts"], "dir": tr["dir"], "slip_r": round(slip, 3),
            "exit": why, "R": gross_r - fee_r, "usdt": usdt, "fee_r": fee_r}


def stats(rows, key):
    if not rows:
        return dict(n=0, wins=0, win=0, net=0, pf=0, dd=0)
    s = pd.Series([r[key] for r in rows])
    w, l = s[s > 0], s[s < 0]
    cum = s.cumsum()
    return dict(n=len(s), wins=len(w), win=round(100 * len(w) / len(s), 1), net=round(float(s.sum()), 2),
                pf=round(float(w.sum() / -l.sum()), 2) if len(l) else float("inf"),
                dd=round(float((cum - cum.cummax()).min()), 2))


def main():
    os.makedirs(OUT, exist_ok=True)
    only = [x.strip() for x in os.getenv("BT_PAIRS", "").split(",") if x.strip()]
    if only:                                   # fixed coin list (e.g. gold only), no 24h-volume ranking
        order_all = only
    else:
        pairs = rd.fetch_liquid_pairs()
        order_all = [p["pair"] for p in pairs]
    print(f"Liquid pairs: {len(order_all)}", flush=True)
    data = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for k, (pair, res) in enumerate(ex.map(bs.load_pair, order_all), 1):
            if res is not None:
                data[pair] = res
            if k % 20 == 0:
                print(f"  15m loaded {k}/{len(order_all)}", flush=True)
    order = [p for p in order_all if p in data]
    trades = paper_trades(data, order)
    trades = [t for t in trades if "exit_ts" in t]
    print(f"Paper trades closed: {len(trades)}", flush=True)

    m1s = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for k, (idx, m) in enumerate(ex.map(lambda it: (it[0], load_1m(it[1])), enumerate(trades)), 1):
            if m is not None and len(m) >= 2:
                m1s[idx] = m
            if k % 100 == 0:
                print(f"  1m loaded {k}/{len(trades)} (with data {len(m1s)})", flush=True)
    usable = [i for i in range(len(trades)) if i in m1s]
    if not usable:
        print("NO 1m DATA")
        sys.exit(1)
    first_1m = min(m1s[i]["ts"].iloc[0] for i in usable)
    print(f"Trades with 1m data: {len(usable)} (1m data from {first_1m})", flush=True)

    # paper reference on the same trades, in R
    paper_rows = []
    for i in usable:
        t = trades[i]
        r_usdt = rd.POSITION_USDT / t["E"] * t["R"]
        paper_rows.append({"R": t["pnl"] / r_usdt, "usdt": LIVE_NOTIONAL / rd.POSITION_USDT * t["pnl"]})
    tmin = min(trades[i]["entry_ts"] for i in usable)
    tmax = max(trades[i]["exit_ts"] for i in usable)

    lines = ["| Scenario | Trades | Skipped (late) | Wins | Win% | Net R | Net USDT @20 | PF | Max DD (R) |",
             "|---|---|---|---|---|---|---|---|---|"]
    p = stats(paper_rows, "R")
    pu = stats(paper_rows, "usdt")
    lines.append(f"| PAPER (reference, no fees) | {p['n']} | 0 | {p['wins']} | {p['win']} | {p['net']} | {pu['net']} | {p['pf']} | {p['dd']} |")
    all_rows = []
    for mode in MODES:
        for g in GUARDS:
            rows, skipped = [], 0
            for i in usable:
                t = trades[i]
                recs, daily = data[t["pair"]]
                r = sim_live(t, recs, daily, m1s[i], mode, g)
                if r is None:
                    skipped += 1
                    continue
                r.update(mode=mode, guard=g)
                rows.append(r)
            all_rows += rows
            s, su = stats(rows, "R"), stats(rows, "usdt")
            name = f"LIVE {mode} lock, guard {g if g is not None else 'none'}R"
            lines.append(f"| {name} | {s['n']} | {skipped} | {s['wins']} | {s['win']} | {s['net']} | {su['net']} | {s['pf']} | {s['dd']} |")
            print(f"{name}: net {s['net']}R / {su['net']} USDT, trades {s['n']}, skipped {skipped}", flush=True)
    pd.DataFrame(all_rows).to_csv(os.path.join(OUT, "live_sim_trades.csv"), index=False)

    # ---- where does live lose vs paper? (FAST, no guard) step by step
    def run(mode, ideal, fees):
        rows = []
        for i in usable:
            t = trades[i]
            recs, daily = data[t["pair"]]
            r = sim_live(t, recs, daily, m1s[i], mode, None, ideal_fill=ideal, fees=fees)
            if r is not None:
                rows.append(r)
        return rows
    steps = ["", "| Step (no guard) | Trades | Net R | Net USDT @20 |", "|---|---|---|---|",
             f"| Paper | {p['n']} | {p['net']} | {pu['net']} |"]
    for mode in MODES:
        for label, ideal, fees in ((f"{mode}: fill at paper price, no fees", True, False),
                                   (f"{mode}: real fill (1st minute), no fees", False, False),
                                   (f"{mode}: real fill + fees", False, True)):
            rr = run(mode, ideal, fees)
            a, b = stats(rr, "R"), stats(rr, "usdt")
            steps.append(f"| {label} | {a['n']} | {a['net']} | {b['net']} |")
    xr = pd.DataFrame(run("FAST", False, True))
    steps += ["", f"Avg entry slip: {xr['slip_r'].mean():.3f}R (median {xr['slip_r'].median():.3f}R); "
                  f"avg fee {xr['fee_r'].mean():.3f}R (median {xr['fee_r'].median():.3f}R)", "",
              "| Exit type (FAST, no guard) | Trades | Net R | Net USDT @20 |", "|---|---|---|---|"]
    for k, g in xr.groupby("exit"):
        steps.append(f"| {k} | {len(g)} | {g['R'].sum():.2f} | {g['usdt'].sum():.2f} |")
    pt = pd.DataFrame([{"st": trades[i]["status"].split("_1:")[0], "R": paper_rows[k]["R"], "usdt": paper_rows[k]["usdt"]}
                       for k, i in enumerate(usable)])
    steps += ["", "| Paper exit type | Trades | Net R | Net USDT @20 |", "|---|---|---|---|"]
    for k, g in pt.groupby("st"):
        steps.append(f"| {k} | {len(g)} | {g['R'].sum():.2f} | {g['usdt'].sum():.2f} |")
    lines += steps
    summary = (f"Paper trades {tmin} -> {tmax}, {len(usable)} trades with 1m data (of {len(trades)}), pairs {len(order)}\n"
               f"Live: fill = mid of first 1m candle after the entry candle, fees {FEE_RT*100:.3f}% round trip, "
               f"7% emergency stop, size {LIVE_NOTIONAL:g} USDT notional\n\n" + "\n".join(lines))
    print("\n=== RESULT ===\n" + summary, flush=True)
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(summary + "\n")


if __name__ == "__main__":
    main()
