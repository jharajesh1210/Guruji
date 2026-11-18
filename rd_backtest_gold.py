"""
Same rd_trading strategy, ONLY on gold coins (XAU / XAUT / PAXG), live-style backtest on 1m data.
Runs twice: (1) with the normal 24h-volume > 10M USDT filter, (2) without the volume filter.
Writes backtest_out/backtest_results.md with both tables.
"""
import os

GOLD = os.getenv("BT_GOLD_PAIRS", "B-XAU_USDT,B-XAUT_USDT,B-PAXG_USDT")
os.environ["BT_PAIRS"] = GOLD
os.environ.setdefault("BT_DAYS", "90")

import rd_trading as rd          # noqa: E402
import rd_backtest_live as bl     # noqa: E402

OUT = os.getenv("BT_OUT", "backtest_out")
parts = []
for label, vol in (("with 24h volume > 10M USDT filter (same as now)", rd.MIN_24H_VOLUME_USDT),
                   ("WITHOUT volume filter", 0.0)):
    rd.MIN_24H_VOLUME_USDT = vol
    bl.OUT = os.path.join(OUT, "vol" if vol else "novol")
    print(f"\n######## GOLD ONLY - {label} ########", flush=True)
    try:
        bl.main()
        with open(os.path.join(bl.OUT, "backtest_results.md"), encoding="utf-8") as f:
            parts.append(f"## Gold only ({GOLD}) - {label}\n\n" + f.read())
    except SystemExit:
        parts.append(f"## Gold only - {label}\n\nNo trades / no data.\n")
os.makedirs(OUT, exist_ok=True)
with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
    f.write("\n\n".join(parts))
print("\n\n".join(parts))
