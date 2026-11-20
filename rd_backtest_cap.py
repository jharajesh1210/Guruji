"""
90-day live-style test (1m data, real fill, 0.75R guard, FAST lock, 15% emergency, fees, 25 USDT/trade):
  Entry max distance beyond PDH/PDL : any (now) | 0.5% | 1.0%
  Hard exit cap (only while no profit is locked): none (now, MACD wait) |
      exit at once when the loss reaches SL + 0.5R (= 1.5R) or 3% of price, whichever comes first
  Min SL : 0.3% (now) | 0.75%
"""
import itertools
import os
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

import rd_trading as rd
import rd_backtest_stops as bs
import rd_backtest_live as bl
import rd_backtest_entry as en

OUT = os.getenv("BT_OUT", "backtest_out")
DAYS = int(os.getenv("BT_DAYS", "90"))
bs.DAYS = DAYS + 2
SIZE = 25.0
bl.LIVE_NOTIONAL = SIZE
bl.EMERG = 0.15
rd.MAX_LOSS_USDT = 1e9


def main():
    os.makedirs(OUT, exist_ok=True)
    order_all = [p["pair"] for p in rd.fetch_liquid_pairs()]
    data = {}
    with ThreadPoolExecutor(max_workers=bl.WORKERS) as ex:
        for pair, res in ex.map(bs.load_pair, order_all):
            if res is not None:
                en.add_feats(res[0])
                data[pair] = res
    order = [p for p in order_all if p in data]
    entries = [("NONE", "any (now)"), ("NEAR_0.5%", "max 0.5%"), ("NEAR_1.0%", "max 1.0%")]
    minsl = [0.3, 0.75]
    trs = {}
    for (em, _), ms in itertools.product(entries, minsl):
        en.F["mode"], rd.MIN_RISK_PCT = em, ms
        trs[(em, ms)] = [t for t in bl.paper_trades(data, order) if "exit_ts" in t]
        print(em, ms, len(trs[(em, ms)]), flush=True)
    en.F["mode"], rd.MIN_RISK_PCT = "NONE", 0.3
    allt = {(t["pair"], t["entry_ts"], t["exit_ts"]): t for v in trs.values() for t in v}
    need = list(allt.values())
    cache = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for k, (t, m1) in enumerate(zip(need, ex.map(bl.load_1m, need)), 1):
            cache[(t["pair"], t["entry_ts"], t["exit_ts"])] = m1
            if k % 500 == 0:
                print(f"  1m {k}/{len(need)}", flush=True)
    res = []
    for ((em, en_name), ms), (cap, cap_name) in itertools.product(itertools.product(entries, minsl),
                                                                 [(None, "none (MACD wait)"), (1, "1.5R or 3%")]):
        rows = []
        for t in trs[(em, ms)]:
            m1 = cache.get((t["pair"], t["entry_ts"], t["exit_ts"]))
            if m1 is None or len(m1) < 2:
                continue
            recs, daily = data[t["pair"]]
            r = bl.sim_live(t, recs, daily, m1, "FAST", 0.75,
                            cap_r=1.5 if cap else None, cap_pct=0.03 if cap else None)
            if r is not None:
                rows.append(r)
        s, u = bl.stats(rows, "R"), bl.stats(rows, "usdt")
        x = pd.DataFrame(rows).sort_values("entry_ts")
        x["mo"] = pd.to_datetime(x["entry_ts"]).dt.to_period("M")
        bym = x.groupby("mo")["usdt"].sum()
        h = len(x) // 2
        worst = x["usdt"].min()
        res.append({"entry": en_name, "minSL%": ms, "loss cap": cap_name, "trades": s["n"], "win%": s["win"],
                    "R": s["net"], "USDT@25": u["net"], "PF": u["pf"], "maxDD": u["dd"], "worst trade": round(worst, 2),
                    "half1": round(x["usdt"].iloc[:h].sum(), 1), "half2": round(x["usdt"].iloc[h:].sum(), 1),
                    "by_month": ", ".join(f"{k}: {v:.0f}" for k, v in bym.items()),
                    "cap exits": int((x["exit"] == "hard_cap").sum())})
        print(res[-1], flush=True)
    df = pd.DataFrame(res).sort_values("USDT@25", ascending=False)
    cols = list(df.columns)
    text = (f"Entry distance x hard loss cap x min SL. {len(order)} coins, {DAYS} days, live-style 1m, 25 USDT/trade, fees, "
            "15% emergency.\nNOW = any | 0.3 | none\n\n| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
            + "\n".join("| " + " | ".join(str(v) for v in r) + " |" for r in df[cols].values) + "\n")
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print("\n=== RESULT ===\n" + text, flush=True)


if __name__ == "__main__":
    main()
