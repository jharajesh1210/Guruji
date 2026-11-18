"""
Entry on 5-minute candles vs 15-minute candles (now) - same PDH/PDL breakout rules.

Both timeframes are built from the SAME 5m data (15m = 3 x 5m), all liquid coins, last BT_DAYS days.
Rules like the mother strategy: high & close beyond PDH/PDL, volume > 1.5 x 20-bar average, wick < 40%,
SL = breakout candle low/high (min 0.3%, max 2%), loss exit only after MACD(12,26,9) confirm on that
timeframe, lock 1:2/1:3/1:4, 1:5 full exit, profit exit -> no same-direction re-entry that session.
Execution like live: fill at the NEXT candle's open, fees 0.118% round trip, SL checked before profit
inside a candle, 20 USDT notional per trade, one trade per coin at a time, max hold 3 days.
"""
import os
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
import time

import rd_trading as rd
import rd_backtest_stops as bs
import rd_backtest_gold2 as g

DAYS = int(os.getenv("BT_DAYS", "30"))
WORKERS = int(os.getenv("BT_WORKERS", "6"))
OUT = os.getenv("BT_OUT", "backtest_out")
g.MIN_RISK = rd.MIN_RISK_PCT / 100          # 0.3% like mother
g.MAX_RISK = 0.02


def chunk5(pair, a, b):
    for attempt in range(3):
        try:
            r = requests.get(rd.CANDLE_URL, params={"pair": pair, "from": a, "to": b, "resolution": "5", "pcode": "f"},
                             headers={"User-Agent": "RD-BACKTEST/1.0"}, timeout=25)
            r.raise_for_status()
            j = r.json()
            return (j.get("data", j.get("candles", [])) if isinstance(j, dict) else j) or []
        except Exception:
            time.sleep(1 + attempt)
    return []


def load5(pair):
    now = int(pd.Timestamp.now("UTC").timestamp())
    frames, end, empty = [], now, 0
    while end > now - DAYS * 86400:
        beg = max(now - DAYS * 86400, end - int(1.5 * 86400))
        raw = chunk5(pair, beg, end)
        if raw:
            frames.append(pd.DataFrame(raw))
            empty = 0
        else:
            empty += 1
            if empty >= 2:
                break
        end = beg
    d = bs.to_df(frames)
    return pair, d


def to_15(d5):
    x = d5.set_index("Timestamp").resample("15min")
    h = x.agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
    return h.reset_index()


def main():
    os.makedirs(OUT, exist_ok=True)
    pairs = [p["pair"] for p in rd.fetch_liquid_pairs()]
    print(f"Liquid pairs: {len(pairs)}", flush=True)
    res = {"5m": [], "15m": []}
    used, span = 0, None
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for k, (pair, d5) in enumerate(ex.map(load5, pairs), 1):
            if d5.empty or len(d5) < 288 * 4:
                continue
            used += 1
            if span is None:
                span = f"{d5['Timestamp'].iloc[0]} -> {d5['Timestamp'].iloc[-1]}"
            for tf, df in (("5m", d5), ("15m", to_15(d5))):
                p = g.prep(df)
                for t in g.simulate(p, "candle", "macd", "ladder5", True):
                    t["pair"] = pair
                    res[tf].append(t)
            if k % 20 == 0:
                print(f"  {k}/{len(pairs)} done", flush=True)
    lines = [f"Entry timeframe backtest, {used} coins, data {span}, fees 0.118%, 20 USDT notional/trade.", "",
             "| Entry candle | Trades | Win % | Net R | Net USDT @20 | PF | Max DD USDT | 1st half USDT | 2nd half USDT |",
             "|---|---|---|---|---|---|---|---|---|"]
    detail = []
    for tf in ("15m", "5m"):
        s = g.summ(res[tf])
        lines.append(f"| {tf}{' (now)' if tf == '15m' else ''} | {s['n']} | {s['win']} | {s['R']} | {s['usdt']} | {s['pf']} | {s['dd']} | {s['h1']} | {s['h2']} |")
        x = pd.DataFrame(res[tf])
        if len(x):
            x["typ"] = x["exit"].str.replace(r"_\d.*", "", regex=True)
            detail += ["", f"Exit types {tf}:", "| Exit | Trades | Net USDT |", "|---|---|---|"]
            for k2, gg in x.groupby("typ"):
                detail.append(f"| {k2} | {len(gg)} | {gg['usdt'].sum():.2f} |")
            pd.DataFrame(res[tf]).to_csv(os.path.join(OUT, f"trades_{tf}.csv"), index=False)
    text = "\n".join(lines + detail) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print("\n=== RESULT ===\n" + text, flush=True)


if __name__ == "__main__":
    main()
