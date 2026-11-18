"""Compare main paper (PDH/PDL, rd_trading_signals_v2.csv) with rd_paper96 (rolling 96 candles)
from the moment paper96 started. Usage: python compare_paper96.py"""
import pandas as pd

a = pd.read_csv("rd_trading_signals_v2.csv")
b = pd.read_csv("rd_paper96_signals.csv")
for d in (a, b):
    d["Timestamp"] = pd.to_datetime(d["Timestamp"], format="mixed")
start = b["Timestamp"].min()
print(f"From {start} (paper96 start) to {max(a.Timestamp.max(), b.Timestamp.max())}\n")
print("| Paper | Entries | Exits | Wins | Win % | PnL (3000 USDT size) | Live size 20 USDT |")
print("|---|---|---|---|---|---|---|")
for name, d in (("PDH/PDL (main)", a), ("Rolling 96 candles", b)):
    d = d[d["Timestamp"] >= start]
    x = d[d["Kind"] == "EXIT"]
    w = (x["Locked_PnL_USDT"] > 0).sum()
    pnl = x["Locked_PnL_USDT"].sum()
    print(f"| {name} | {(d.Kind == 'ENTRY').sum()} | {len(x)} | {w} | {100 * w / max(len(x), 1):.1f} | "
          f"{pnl:+.1f} | {pnl / 150:+.2f} |")
