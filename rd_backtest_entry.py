"""
Late-entry check: today the BUY rule is only "high > PDH and close > PDH (+volume, wick)".
It does NOT require that price just crossed PDH, so after price is already far above PDH, any later
green candle with volume triggers a BUY (example 19 Nov 1000LUNC: entry 0.04908 with PDH 0.04339 = +13%).

Entry filters tested (same for SELL, mirrored), everything else = current logic, caps, min SL 0.3%:
  NONE      : today
  FRESH     : previous candle closed at/below PDH (BUY) / at/above PDL (SELL) -> only the first cross
  NEAR_1ATR : close is at most 1 x ATR(14, 15m) beyond PDH/PDL
  NEAR_0.5% : close at most 0.5% beyond PDH/PDL
  RUN<=2    : at most 2 same-colour candles in a row (incl. the signal candle)
  FRESH+RUN : both
Stage A: 90 days, live-style on 1m (real fill, 0.75R guard, FAST lock, 15% emergency, fees), 25 USDT/trade,
         paper without loss cap shown too.
Also prints how far beyond PDH/PDL today's entries are (distribution) and the 1000LUNC example.
"""
import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

import rd_trading as rd
import rd_backtest_stops as bs
import rd_backtest_live as bl

OUT = os.getenv("BT_OUT", "backtest_out")
DAYS = int(os.getenv("BT_DAYS", "90"))
bs.DAYS = DAYS + 2
SIZE = 25.0
bl.LIVE_NOTIONAL = SIZE
bl.EMERG = 0.15
rd.MAX_LOSS_USDT = 1e9          # honest paper (no loss cap)

F = {"mode": "NONE"}
_step = rd.step_candle


def blocked(row, d, pdh, pdl):
    m = F["mode"]
    if m == "NONE":
        return False
    pc = row.get("PrevClose")
    fresh = (pc == pc) and ((pc <= pdh) if d == "BUY" else (pc >= pdl))
    run = row.get("GreenRun", 0) if d == "BUY" else row.get("RedRun", 0)
    beyond = (row["Close"] - pdh) if d == "BUY" else (pdl - row["Close"])
    atr = row.get("ATR15", np.nan)
    if m == "FRESH":
        return not fresh
    if m == "NEAR_1ATR":
        return not (atr == atr and beyond <= atr)
    if m == "NEAR_0.5%":
        return beyond > row["Close"] * 0.005
    if m == "RUN<=2":
        return run > 2
    if m == "FRESH+RUN":
        return (not fresh) or run > 2
    return False


def step_f(row, trade, *a, **k):
    out = _step(row, trade, *a, **k)
    status, profit, trade2, pdh, pdl = out
    if trade is None and trade2 is not None and blocked(row, trade2["direction"], pdh, pdl):
        return "Entry_Skipped_Late", 0.0, None, pdh, pdl
    return out


rd.step_candle = step_f


def add_feats(recs):
    df = pd.DataFrame({k: [r[k] for r in recs] for k in ("Open", "High", "Low", "Close")})
    g = (df["Close"] > df["Open"]).astype(int)
    r = (df["Close"] < df["Open"]).astype(int)
    gr = g.groupby((g != g.shift()).cumsum()).cumsum() * g
    rr = r.groupby((r != r.shift()).cumsum()).cumsum() * r
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - df["Close"].shift()).abs(),
                    (df["Low"] - df["Close"].shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    for rec, a, b, c in zip(recs, gr.values, rr.values, atr.values):
        rec["GreenRun"], rec["RedRun"], rec["ATR15"] = int(a), int(b), c


def main():
    os.makedirs(OUT, exist_ok=True)
    order_all = [p["pair"] for p in rd.fetch_liquid_pairs()]
    data = {}
    with ThreadPoolExecutor(max_workers=bl.WORKERS) as ex:
        for pair, res in ex.map(bs.load_pair, order_all):
            if res is not None:
                add_feats(res[0])
                data[pair] = res
    order = [p for p in order_all if p in data]
    modes = ["NONE", "FRESH", "NEAR_1ATR", "NEAR_0.5%", "RUN<=2", "FRESH+RUN"]
    trs = {}
    for m in modes:
        F["mode"] = m
        trs[m] = [t for t in bl.paper_trades(data, order) if "exit_ts" in t]
        print(f"{m}: {len(trs[m])} paper trades", flush=True)
    F["mode"] = "NONE"

    # how late are today's entries?
    late = []
    for t in trs["NONE"]:
        rec = data[t["pair"]][0][t["entry_ri"]]
        pdh, pdl = rd.levels_for(rec["Date"], data[t["pair"]][1])
        beyond = (rec["Close"] - pdh) / rec["Close"] if t["dir"] == "BUY" else (pdl - rec["Close"]) / rec["Close"]
        pc = rec.get("PrevClose")
        fresh = (pc <= pdh) if t["dir"] == "BUY" else (pc >= pdl)
        run = rec["GreenRun"] if t["dir"] == "BUY" else rec["RedRun"]
        late.append({"pair": t["pair"], "ts": t["entry_ts"], "dir": t["dir"], "beyond_pct": 100 * beyond,
                     "fresh": fresh, "run": run, "pnl": t["pnl"]})
    L = pd.DataFrame(late)

    cache = {}
    allt = {(t["pair"], t["entry_ts"]): t for m in modes for t in trs[m]}
    need = list(allt.values())
    with ThreadPoolExecutor(max_workers=bl.WORKERS) as ex:
        for k, (t, m1) in enumerate(zip(need, ex.map(bl.load_1m, need)), 1):
            cache[(t["pair"], t["entry_ts"])] = m1
            if k % 500 == 0:
                print(f"  1m {k}/{len(need)}", flush=True)

    lines = [f"Entry-timing filters, {len(order)} coins, last {DAYS} days. Live-style 1m, 25 USDT/trade, fees, 15% emergency.", "",
             "### How late are today's entries? (paper trades, NONE)", "",
             f"- Entries: {len(L)}; fresh cross (prev candle still inside): {L.fresh.mean()*100:.0f}%",
             f"- Distance beyond PDH/PDL at entry: median {L.beyond_pct.median():.2f}%, 75% {L.beyond_pct.quantile(.75):.2f}%, "
             f"90% {L.beyond_pct.quantile(.9):.2f}%",
             f"- Same-colour run (incl. signal candle) >= 3: {(L.run >= 3).mean()*100:.0f}% of entries", "",
             "| Group | Entries | Paper PnL (no cap, 3000 size) | avg per trade |", "|---|---|---|---|"]
    for name, g in (("fresh cross", L[L.fresh]), ("NOT fresh (already beyond)", L[~L.fresh]),
                    ("beyond <= 0.5%", L[L.beyond_pct <= 0.5]), ("beyond 0.5-2%", L[(L.beyond_pct > 0.5) & (L.beyond_pct <= 2)]),
                    ("beyond > 2%", L[L.beyond_pct > 2]), ("run <= 2", L[L.run <= 2]), ("run >= 3", L[L.run >= 3])):
        lines.append(f"| {name} | {len(g)} | {g.pnl.sum():.0f} | {g.pnl.mean():.1f} |")
    ex1 = L[L.pair.str.contains("1000LUNC")].tail(5)
    if len(ex1):
        lines += ["", "1000LUNC entries:", "", "| time | dir | beyond PDH/PDL % | fresh | run | paper PnL |", "|---|---|---|---|---|---|"]
        for r in ex1.itertuples():
            lines.append(f"| {r.ts} | {r.dir} | {r.beyond_pct:.2f} | {r.fresh} | {r.run} | {r.pnl:.0f} |")

    lines += ["", "### Filters (90 days, live-style)", "",
              "| Entry rule | Paper trades | Paper USDT@25 (no cap) | LIVE trades | LIVE win % | LIVE net R | LIVE USDT @25 | PF | Max DD | by month |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for m in modes:
        rows = []
        for t in trs[m]:
            m1 = cache.get((t["pair"], t["entry_ts"]))
            if m1 is None or len(m1) < 2:
                continue
            recs, daily = data[t["pair"]]
            r = bl.sim_live(t, recs, daily, m1, "FAST", 0.75)
            if r is not None:
                rows.append(r)
        s, u = bl.stats(rows, "R"), bl.stats(rows, "usdt")
        x = pd.DataFrame(rows)
        x["mo"] = pd.to_datetime(x["entry_ts"]).dt.to_period("M")
        bym = ", ".join(f"{k}: {v:.0f}" for k, v in x.groupby("mo")["usdt"].sum().items())
        pp = sum(SIZE / rd.POSITION_USDT * t["pnl"] for t in trs[m])
        lines.append(f"| {m} | {len(trs[m])} | {pp:.1f} | {s['n']} | {s['win']} | {s['net']} | {u['net']} | {u['pf']} | {u['dd']} | {bym} |")
        print(lines[-1], flush=True)
    text = "\n".join(lines) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print("\n=== RESULT ===\n" + text, flush=True)


if __name__ == "__main__":
    main()
