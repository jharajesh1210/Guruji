"""
Session test on the CURRENT v2 rules (entry max 0.5% from PDH/PDL, min SL 0.75%, loss cap 1.5R/3%,
lock 1:2..1:5, exit 1:6). 90 days, live-style on 1m (real fill, 0.75R guard, fast lock, fees), 25 USDT/trade.
Variants: all sessions (now) | skip Asian (05:30-13:30 IST) | skip London | skip NY | only NY.
Also: result of each session inside 'all sessions'.
"""
import os
from concurrent.futures import ThreadPoolExecutor

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
rd.MAX_LOSS_USDT = 1e9
SKIP = {"set": set()}
_step = rd.step_candle


def step_s(row, trade, *a, **k):
    out = _step(row, trade, *a, **k)
    if trade is None and out[2] is not None and row.get("Session") in SKIP["set"]:
        return "Entry_Skipped_Session", 0.0, None, out[3], out[4]
    return out


rd.step_candle = step_s


def main():
    os.makedirs(OUT, exist_ok=True)
    order_all = [p["pair"] for p in rd.fetch_liquid_pairs()]
    data = {}
    with ThreadPoolExecutor(max_workers=bl.WORKERS) as ex:
        for pair, res in ex.map(bs.load_pair, order_all):
            if res is not None:
                data[pair] = res
    order = [p for p in order_all if p in data]
    variants = [("All sessions (now)", set()), ("Skip Asian", {"Asian"}), ("Skip London", {"London"}),
                ("Skip NY", {"NY"}), ("Only NY", {"Asian", "London"})]
    trs = {}
    for name, sk in variants:
        SKIP["set"] = sk
        trs[name] = [t for t in bl.paper_trades(data, order) if "exit_ts" in t]
        print(name, len(trs[name]), flush=True)
    SKIP["set"] = set()
    allt = {(t["pair"], t["entry_ts"], t["exit_ts"]): t for v in trs.values() for t in v}
    need = list(allt.values())
    cache = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for k, (t, m1) in enumerate(zip(need, ex.map(bl.load_1m, need)), 1):
            cache[(t["pair"], t["entry_ts"], t["exit_ts"])] = m1
            if k % 500 == 0:
                print(f"  1m {k}/{len(need)}", flush=True)
    lines = [f"Session test, v2 rules, {len(order)} coins, {DAYS} days, live-style 1m, 25 USDT/trade, fees.", "",
             "| Variant | Trades | Win % | Net R | Net USDT @25 | PF | Max DD | 1st half | 2nd half | by month |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    per_session = None
    for name, _ in variants:
        rows = []
        for t in trs[name]:
            m1 = cache.get((t["pair"], t["entry_ts"], t["exit_ts"]))
            if m1 is None or len(m1) < 2:
                continue
            recs, daily = data[t["pair"]]
            r = bl.sim_live(t, recs, daily, m1, "FAST", 0.75, cap_r=rd.LOSS_CAP_R, cap_pct=rd.LOSS_CAP_PCT / 100)
            if r is not None:
                r["session"] = recs[t["entry_ri"]]["Session"]
                rows.append(r)
        s, u = bl.stats(rows, "R"), bl.stats(rows, "usdt")
        if not rows:
            lines.append(f"| {name} | 0 | - | - | - | - | - | - | - | - |")
            continue
        x = pd.DataFrame(rows).sort_values("entry_ts")
        x["mo"] = pd.to_datetime(x["entry_ts"]).dt.to_period("M")
        h = len(x) // 2
        bym = ", ".join(f"{k}: {v:.0f}" for k, v in x.groupby("mo")["usdt"].sum().items())
        lines.append(f"| {name} | {s['n']} | {s['win']} | {s['net']} | {u['net']} | {u['pf']} | {u['dd']} | "
                     f"{x['usdt'].iloc[:h].sum():.1f} | {x['usdt'].iloc[h:].sum():.1f} | {bym} |")
        if per_session is None:
            per_session = x
    lines += ["", "Inside 'All sessions': result per session", "", "| Session | Trades | Win % | Net USDT @25 | worst trade |", "|---|---|---|---|---|"]
    for k, g in (per_session.groupby("session") if per_session is not None else []):
        lines.append(f"| {k} | {len(g)} | {100 * (g.usdt > 0).mean():.1f} | {g.usdt.sum():.2f} | {g.usdt.min():.2f} |")
    text = "\n".join(lines) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print("\n=== RESULT ===\n" + text, flush=True)


if __name__ == "__main__":
    main()
