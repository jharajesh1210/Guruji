"""
Backtest: minimum SL distance (MIN_RISK_PCT) 0.15% (now) vs 0.3 / 0.5 / 0.75 / 1.0 %.
Trades whose SL is closer than this are skipped (fees eat too much of a tight trade).
Live-style on 1m data (FAST lock, 0.75R late-entry guard, fees 0.118%), two universes:
  A) all liquid coins, 30 days     B) gold only (XAU, PAXG), 90 days
"""
import os
import re

import rd_trading as rd
import rd_backtest_stops as bs
import rd_backtest_live as bl

OUT = os.getenv("BT_OUT", "backtest_out")
VALUES = [0.15, 0.3, 0.5, 0.75, 1.0]
bl.GUARDS = [0.75]
bl.MODES = ["FAST"]

_c15, _c1 = {}, {}
_load_pair, _fetch_1m = bs.load_pair, bl.fetch_1m


def load_pair_cached(pair):
    k = (pair, bs.DAYS)
    if k not in _c15:
        _c15[k] = _load_pair(pair)
    return _c15[k]


def fetch_1m_cached(pair, t0, t1):
    k = (pair, t0, t1)
    if k not in _c1:
        _c1[k] = _fetch_1m(pair, t0, t1)
    return _c1[k]


bs.load_pair = load_pair_cached
bl.fetch_1m = fetch_1m_cached


def grab(md, prefix):
    for line in md.splitlines():
        if line.startswith(prefix):
            return [c.strip() for c in line.strip("|").split("|")]
    return None


def run_universe(title, pairs, days):
    if pairs:
        os.environ["BT_PAIRS"] = pairs
    else:
        os.environ.pop("BT_PAIRS", None)
    bs.DAYS = days
    rows = ["| Min SL % | Paper trades | Paper net R | Paper USDT@20 | LIVE trades | LIVE win% | LIVE net R | LIVE USDT@20 (after fees) | LIVE PF | LIVE max DD R |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for v in VALUES:
        rd.MIN_RISK_PCT = v
        bl.OUT = os.path.join(OUT, re.sub(r"\W", "_", title), f"min{v}")
        print(f"\n######## {title} | MIN_RISK_PCT={v} ########", flush=True)
        try:
            bl.main()
            md = open(os.path.join(bl.OUT, "backtest_results.md"), encoding="utf-8").read()
            p = grab(md, "| PAPER")
            l = grab(md, "| LIVE FAST")
            rows.append(f"| {v} | {p[1]} | {p[5]} | {p[6]} | {l[1]} | {l[4]} | {l[5]} | {l[6]} | {l[7]} | {l[8]} |")
        except SystemExit:
            rows.append(f"| {v} | 0 | - | - | 0 | - | - | - | - | - |")
    return f"## {title}\n\n" + "\n".join(rows)


parts = [run_universe("All liquid coins, 30 days", "", int(os.getenv("BT_DAYS", "30"))),
         run_universe("Gold only (XAU, PAXG), 90 days", "B-XAU_USDT,B-PAXG_USDT", 90)]
text = ("Min SL distance backtest. LIVE = FAST lock, 0.75R late-entry guard, real 1m fill, fees 0.118% round trip, "
        "7% emergency stop, 20 USDT notional per trade.\n\n" + "\n\n".join(parts))
os.makedirs(OUT, exist_ok=True)
with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
    f.write(text + "\n")
print("\n=== FINAL ===\n" + text, flush=True)
