"""
rd_trading.py ka backtest: exchange emergency-stop width (none / 2.5% / 4% / 5% / 7% from entry), original repeat rule.
(old doc: repeat-entry rules (original / fresh breakout / one trade per coin per session /
block coin for the session after a loss / fresh + block).

- rd_trading.py ka asli logic (step_candle: entry, SL, MACD loss exit, profit lock, target) use hota hai.
- Itna purana 15m data jitna CoinDCX API de (BT_DAYS tak, default 120).
- Saare coins ek saath, time ke order mein chalte hain taaki cap sahi lage.
- Aaj ke liquid coins hi liye jaate hain (delisted coins nahi) -> thoda survivorship bias.
- Alerts/Telegram/state file kuch nahi chhedta.
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests

import rd_trading as rd

DAYS = int(os.getenv("BT_DAYS", "60"))
CHUNK_DAYS = 5
WORKERS = int(os.getenv("BT_WORKERS", "6"))
OUT = os.getenv("BT_OUT", "backtest_out")
SCENARIOS = [("paper_no_stop", None), ("stop_2.5pct", 2.5), ("stop_4pct", 4.0), ("stop_5pct", 5.0), ("stop_7pct", 7.0)]
NEED = ["time", "open", "high", "low", "close", "volume"]


# ---------------------------- data ----------------------------
def fetch_chunk(pair, t_from, t_to):
    for attempt in range(3):
        try:
            r = requests.get(rd.CANDLE_URL,
                             params={"pair": pair, "from": t_from, "to": t_to,
                                     "resolution": "15", "pcode": "f"},
                             headers={"User-Agent": "RD-BACKTEST/1.0"}, timeout=25)
            r.raise_for_status()
            j = r.json()
            raw = j.get("data", j.get("candles", [])) if isinstance(j, dict) else j
            return raw or []
        except Exception:
            time.sleep(1 + attempt)
    return []


def to_df(frames):
    if not frames:
        return pd.DataFrame()
    d = pd.concat(frames, ignore_index=True)
    if not set(NEED).issubset(d.columns):
        return pd.DataFrame()
    for c in NEED:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=NEED).drop_duplicates("time").sort_values("time")
    ts = pd.to_datetime(d["time"], unit="ms", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    out = pd.DataFrame({"Timestamp": ts.values, "Open": d["open"].values, "High": d["high"].values,
                        "Low": d["low"].values, "Close": d["close"].values, "Volume": d["volume"].values})
    return out.iloc[:-1].reset_index(drop=True)  # aakhri (adhoori) candle hatao


def load_pair(pair):
    now = int(time.time())
    start_limit = now - DAYS * 86400
    end = now
    frames, empty = [], 0
    while end > start_limit:
        beg = max(start_limit, end - CHUNK_DAYS * 86400)
        raw = fetch_chunk(pair, beg, end)
        if raw:
            frames.append(pd.DataFrame(raw))
            empty = 0
        else:
            empty += 1
            if empty >= 2:      # isse purana data API ke paas nahi
                break
        end = beg
        time.sleep(0.1)
    df = to_df(frames)
    if len(df) < 3 * rd.BARS_PER_24H:
        return pair, None
    df, daily = rd.prepare(df)
    df["PrevClose"] = df["Close"].shift(1)
    df["QV24"] = (df["Volume"] * df["Close"]).rolling(rd.BARS_PER_24H, min_periods=rd.BARS_PER_24H).sum()
    return pair, (df.to_dict("records"), daily)


# ---------------------------- simulation ----------------------------
def simulate(data, order, emerg):
    rule = "original"
    ev = []
    for pi, pair in enumerate(order):
        for ri, r in enumerate(data[pair][0]):
            ev.append((r["Timestamp"], pi, ri))
    ev.sort()

    trades, exits = {}, []
    entries = skipped_cap = 0
    done, blocked = set(), set()      # per-session memory: "pair|date|session"
    use_fresh = rule in ("fresh_breakout", "fresh_plus_block")
    use_session = rule == "one_per_session"
    use_block = rule in ("block_after_loss", "fresh_plus_block")
    i, n = 0, len(ev)
    while i < n:
        ts = ev[i][0]
        j = i
        while j < n and ev[j][0] == ts:
            j += 1
        group = ev[i:j]
        i = j
        # pehle open trades wale coins (exit slot khali kare), phir volume rank ke hisaab se
        group.sort(key=lambda e: (0 if order[e[1]] in trades else 1, e[1]))
        new_entries = 0
        for _, pi, ri in group:
            pair = order[pi]
            recs, daily = data[pair]
            row = recs[ri]
            trade = trades.get(pair)
            prev_dir = trade["direction"] if trade else None
            entry_ts = trade.get("entry_ts") if trade else None
            if trade is not None and emerg and trade.get("locked_rr", 0) <= 0 and trade.get("pending_sl") is None:
                e0, p0 = trade["entry_price"], emerg / 100.0
                hit = (prev_dir == "BUY" and row["Low"] <= e0 * (1 - p0)) or (prev_dir == "SELL" and row["High"] >= e0 * (1 + p0))
                if hit:      # exchange stop fires first (conservative): loss = notional * p
                    exits.append({"pair": pair, "dir": prev_dir, "entry_ts": entry_ts, "exit_ts": ts,
                                  "status": "EMERGENCY_STOP", "pnl": -rd.POSITION_USDT * p0})
                    del trades[pair]
                    continue
            status, profit, trade2, pdh, pdl = rd.step_candle(row, trade, daily)
            if trade is not None:
                if trade2 is None:
                    exits.append({"pair": pair, "dir": prev_dir, "entry_ts": entry_ts,
                                  "exit_ts": ts, "status": status, "pnl": float(profit)})
                    del trades[pair]
                    if use_block and profit < 0:
                        blocked.add(f"{pair}|{row['Date']}|{row['Session']}")
                else:
                    trades[pair] = trade2
                continue
            if trade2 is None:
                continue
            d = trade2["direction"]
            qv = row.get("QV24")
            ok = (qv == qv and qv >= rd.MIN_24H_VOLUME_USDT
                  and len(trades) < rd.MAX_OPEN_TRADES
                  and new_entries < rd.MAX_NEW_ENTRIES_PER_CYCLE)
            if not ok:
                continue
            skey = f"{pair}|{row['Date']}|{row['Session']}"
            pc = row.get("PrevClose")
            if use_fresh and pc == pc and ((d == "BUY" and pc > pdh) or (d == "SELL" and pc < pdl)):
                skipped_cap += 1       # breakout already in progress (previous candle already beyond PDH/PDL)
                continue
            if use_session and skey in done:
                skipped_cap += 1
                continue
            if use_block and skey in blocked:
                skipped_cap += 1
                continue
            done.add(skey)
            trades[pair] = trade2
            new_entries += 1
            entries += 1
    return exits, entries, skipped_cap, len(trades)


def metrics(exits):
    x = pd.DataFrame(exits)
    if x.empty:
        return {"trades": 0, "wins": 0, "win_pct": 0, "net": 0, "avg_win": 0, "avg_loss": 0,
                "pf": 0, "max_dd": 0, "quarters": [0, 0, 0, 0]}, x
    x = x.sort_values("exit_ts").reset_index(drop=True)
    pnl = x["pnl"]
    cum = pnl.cumsum()
    wins, loss = pnl[pnl > 0], pnl[pnl < 0]
    q = pd.cut(x["exit_ts"].astype("int64"), 4, labels=False)
    quarters = [round(float(pnl[q == k].sum()), 2) for k in range(4)]
    return {
        "trades": len(x), "wins": len(wins), "win_pct": round(100 * len(wins) / len(x), 1),
        "net": round(float(pnl.sum()), 2),
        "avg_win": round(float(wins.mean()), 2) if len(wins) else 0,
        "avg_loss": round(float(loss.mean()), 2) if len(loss) else 0,
        "pf": round(float(wins.sum() / -loss.sum()), 2) if len(loss) else float("inf"),
        "max_dd": round(float((cum - cum.cummax()).min()), 2),
        "quarters": quarters,
    }, x


def main():
    os.makedirs(OUT, exist_ok=True)
    pairs = rd.fetch_liquid_pairs()
    order_all = [p["pair"] for p in pairs]
    print(f"Liquid pairs (24h vol >= {rd.MIN_24H_VOLUME_USDT:,.0f}): {len(order_all)}", flush=True)

    data = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for k, (pair, res) in enumerate(ex.map(load_pair, order_all), 1):
            if res is not None:
                data[pair] = res
            if k % 20 == 0:
                print(f"  loaded {k}/{len(order_all)} (usable {len(data)})", flush=True)
    order = [p for p in order_all if p in data]
    if not order:
        print("NO DATA")
        sys.exit(1)

    tmin = min(data[p][0][0]["Timestamp"] for p in order)
    tmax = max(data[p][0][-1]["Timestamp"] for p in order)
    days = (tmax - tmin).total_seconds() / 86400
    print(f"Usable pairs: {len(order)} | data {tmin} -> {tmax} (~{days:.1f} days, requested {DAYS})", flush=True)

    lines = ["| Scenario | Trades | Wins | Win% | Net USDT | Avg win | Avg loss | PF | Max DD | Skipped by rule | Q1 | Q2 | Q3 | Q4 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, emerg in SCENARIOS:
        t0 = time.time()
        exits, entries, skipped, still_open = simulate(data, order, emerg)
        m, x = metrics(exits)
        x.to_csv(os.path.join(OUT, f"trades_{name}.csv"), index=False)
        q = m["quarters"]
        lines.append(f"| {name} | {m['trades']} | {m['wins']} | {m['win_pct']} | {m['net']} | {m['avg_win']} | "
                     f"{m['avg_loss']} | {m['pf']} | {m['max_dd']} | {skipped} | {q[0]} | {q[1]} | {q[2]} | {q[3]} |")
        print(f"{name}: net {m['net']} trades {m['trades']} (open at end {still_open}, {time.time()-t0:.0f}s)", flush=True)

    summary = (f"Data: {tmin} -> {tmax} (~{days:.1f} days), pairs used: {len(order)}\n"
               "Q1-Q4 = net PnL in four equal time slices of the period (consistency check)\n\n" + "\n".join(lines))
    print("\n=== RESULT ===\n" + summary, flush=True)
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(summary + "\n")


if __name__ == "__main__":
    main()
