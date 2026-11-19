"""
Search for the best CURRENT-logic settings, judged only by a 90-day LIVE-STYLE test
(1m data, real fill, 0.75R guard, FAST lock, 15% emergency, fees 0.118%, 25 USDT/trade, caps 5 open).

Grid (18 variants):
  level      : PDH/PDL (now) | rolling 96 candles | rolling 200 candles
  entry      : any (now) | close at most 0.5% beyond the level
  min SL     : 0.3% (now) | 0.5% | 0.75%
Ranked by live USDT; also shows month-by-month and both halves so a lucky month does not win.
"""
import itertools
import os
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

os.environ.setdefault("BT_LEVELS", "96,200")
import rd_trading as rd                      # noqa: E402
import rd_backtest_stops as bs               # noqa: E402
import rd_backtest_live as bl                # noqa: E402
import rd_backtest_level96 as lv             # noqa: E402  (patches rd.levels_for)
import rd_backtest_entry as en               # noqa: E402  (patches rd.step_candle)

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
                lv.add_rolling(res[0])
                en.add_feats(res[0])
                data[pair] = res
    order = [p for p in order_all if p in data]
    levels = [("pdh", "PDH/PDL")] + [(f"roll{n}", f"roll {n}") for n in lv.LEVELS]
    entries = [("NONE", "any"), ("NEAR_0.5%", "<=0.5%")]
    minsl = [0.3, 0.5, 0.75]
    variants = list(itertools.product(levels, entries, minsl))
    trs = {}
    for (lm, ln), (em, enm), ms in variants:
        en.F["mode"] = em
        rd.MIN_RISK_PCT = ms
        trs[(lm, em, ms)] = [t for t in lv.paper_trades(data, order, lm) if "exit_ts" in t]
        print(f"{ln} | {enm} | minSL {ms}: {len(trs[(lm, em, ms)])} trades", flush=True)
    en.F["mode"] = "NONE"
    rd.MIN_RISK_PCT = 0.3
    allt = {(t["pair"], t["entry_ts"], t["exit_ts"]): t for v in trs.values() for t in v}
    need = list(allt.values())
    print(f"1m loads needed: {len(need)}", flush=True)
    cache = {}
    with ThreadPoolExecutor(max_workers=max(bl.WORKERS, 8)) as ex:
        for k, (t, m1) in enumerate(zip(need, ex.map(bl.load_1m, need)), 1):
            cache[(t["pair"], t["entry_ts"], t["exit_ts"])] = m1
            if k % 500 == 0:
                print(f"  1m {k}/{len(need)}", flush=True)
    res = []
    for (lm, ln), (em, enm), ms in variants:
        rows = []
        for t in trs[(lm, em, ms)]:
            m1 = cache.get((t["pair"], t["entry_ts"], t["exit_ts"]))
            if m1 is None or len(m1) < 2:
                continue
            lv.CUR["mode"], lv.CUR["row"] = "pdh", None
            recs, daily = data[t["pair"]]
            r = bl.sim_live(t, recs, daily, m1, "FAST", 0.75)
            if r is not None:
                rows.append(r)
        s, u = bl.stats(rows, "R"), bl.stats(rows, "usdt")
        x = pd.DataFrame(rows).sort_values("entry_ts")
        x["mo"] = pd.to_datetime(x["entry_ts"]).dt.to_period("M")
        bym = x.groupby("mo")["usdt"].sum()
        h = len(x) // 2
        res.append({"level": ln, "entry": enm, "minSL%": ms, "trades": s["n"], "win%": s["win"], "R": s["net"],
                    "USDT@25": u["net"], "PF": u["pf"], "maxDD": u["dd"],
                    "half1": round(x["usdt"].iloc[:h].sum(), 1), "half2": round(x["usdt"].iloc[h:].sum(), 1),
                    "months+": int((bym > 0).sum()), "by_month": ", ".join(f"{k}: {v:.0f}" for k, v in bym.items())})
        print(res[-1], flush=True)
    df = pd.DataFrame(res).sort_values("USDT@25", ascending=False)
    cols = list(df.columns)
    text = (f"Best-settings search, {len(order)} coins, {DAYS} days, live-style 1m, 25 USDT/trade, fees, 15% emergency.\n"
            "NOW = PDH/PDL | any | 0.3\n\n| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
            + "\n".join("| " + " | ".join(str(v) for v in r) + " |" for r in df[cols].values) + "\n")
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print("\n=== RESULT ===\n" + text, flush=True)


if __name__ == "__main__":
    main()
