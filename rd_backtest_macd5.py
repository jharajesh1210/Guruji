"""
Loss exit: MACD confirm on 15m candles (now) vs on 5m candles.

Entry stays on 15m (same PDH/PDL breakout rules). The open trade is managed on 5m candles in BOTH
variants (same lock / target handling), only the loss exit differs:
  A) 15m: at every 15m close -> if that 15m candle touched SL and 15m MACD(12,26,9) confirms -> exit
  B) 5m : at every 5m close  -> if that 5m candle touched SL and 5m MACD(12,26,9) confirms  -> exit
Exit price = worse of SL and the candle close (like mother). Fill at next 15m open, fees 0.118%,
20 USDT notional, SL 0.3-2%, lock 1:2/1:3/1:4, 1:5 exit, 7% emergency stop ignored (pure rule test).
"""
import os
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

import rd_trading as rd
import rd_backtest_gold2 as g
import rd_backtest_5m as b5

OUT = os.getenv("BT_OUT", "backtest_out")
FEE, NOTIONAL = 0.00118, 20.0
MIN_RISK, MAX_RISK = rd.MIN_RISK_PCT / 100, 0.02
LOCKS, T = (2.0, 3.0, 4.0), 5.0


def run_pair(d5):
    p5 = g.prep(d5)
    p15 = g.prep(b5.to_15(d5))
    i15 = {ts: k for k, ts in enumerate(p15["Timestamp"])}
    i5 = {ts: k for k, ts in enumerate(p5["Timestamp"])}
    O5, H5, L5, C5 = (p5[c].values for c in ("Open", "High", "Low", "Close"))
    M5, S5 = p5["MACD"].values, p5["Sig"].values
    T5 = p5["Timestamp"]
    out = {"15m": [], "5m": []}
    for var in ("15m", "5m"):
        i, n15, block = 1, len(p15), {}
        while i < n15 - 1:
            r = p15.iloc[i]
            pdh, pdl = r["PDH"], r["PDL"]
            if pdh != pdh or r["VolMA"] != r["VolMA"]:
                i += 1
                continue
            rng = r["High"] - r["Low"]
            up_w = (r["High"] - max(r["Open"], r["Close"])) / rng if rng > 0 else 0
            dn_w = (min(r["Open"], r["Close"]) - r["Low"]) / rng if rng > 0 else 0
            hv = r["Volume"] > 1.5 * r["VolMA"]
            d = 1 if (r["High"] > pdh and r["Close"] > pdh and hv and up_w < 0.4) else (
                -1 if (r["Low"] < pdl and r["Close"] < pdl and hv and dn_w < 0.4) else None)
            skey = f"{r['Date']}|{r['Session']}"
            if d is None or block.get(d) == skey:
                i += 1
                continue
            t_in = p15["Timestamp"].iloc[i + 1]
            j = i5.get(t_in)
            if j is None:
                i += 1
                continue
            E = O5[j]
            sl = r["Low"] if d == 1 else r["High"]
            risk = d * (E - sl)
            if risk <= 0 or risk < E * MIN_RISK:
                i += 1
                continue
            if risk > E * MAX_RISK:
                risk = E * MAX_RISK
                sl = E - d * risk
            locked = pend = 0.0
            exit_px = why = None
            n5 = len(p5)
            while j < n5:
                if pend > locked:
                    locked = pend
                stop = E + d * locked * risk if locked > 0 else sl
                hit = (L5[j] <= stop) if d == 1 else (H5[j] >= stop)
                if hit and locked > 0:
                    gap = (O5[j] < stop) if d == 1 else (O5[j] > stop)
                    exit_px, why = (O5[j] if gap else stop), "lock"
                    break
                if locked == 0:
                    if var == "5m":
                        if hit and ((M5[j] < S5[j]) if d == 1 else (M5[j] > S5[j])):
                            exit_px = min(sl, C5[j]) if d == 1 else max(sl, C5[j])
                            why = "macd_loss"
                            break
                    elif (T5.iloc[j].minute % 15) == 10:          # last 5m bar of a 15m candle
                        k = i15.get(T5.iloc[j].floor("15min"))
                        if k is not None:
                            c = p15.iloc[k]
                            hit15 = (c["Low"] <= sl) if d == 1 else (c["High"] >= sl)
                            if hit15 and ((c["MACD"] < c["Sig"]) if d == 1 else (c["MACD"] > c["Sig"])):
                                exit_px = min(sl, c["Close"]) if d == 1 else max(sl, c["Close"])
                                why = "macd_loss"
                                break
                best = d * ((H5[j] if d == 1 else L5[j]) - E) / risk
                if best >= T:
                    exit_px, why = E + d * T * risk, "target"
                    break
                new = max([l for l in LOCKS if best >= l], default=0.0)
                if new > max(locked, pend):
                    lock_px = E + d * new * risk
                    if (C5[j] <= lock_px) if d == 1 else (C5[j] >= lock_px):
                        exit_px, why = lock_px, "lock"
                        break
                    pend = new
                if T5.iloc[j] - t_in >= g.MAX_HOLD:
                    exit_px, why = C5[j], "max_hold"
                    break
                j += 1
            if exit_px is None:
                break
            gross = d * (exit_px - E)
            usd = NOTIONAL * gross / E - NOTIONAL * FEE
            out[var].append({"entry": t_in, "R": gross / risk - FEE * E / risk, "usdt": usd, "exit": why,
                             "loss_R": gross / risk if why == "macd_loss" else None})
            if usd > 0:
                block[d] = f"{p5['Date'].iloc[j]}|{p5['Session'].iloc[j]}"
            ts_exit = T5.iloc[j].floor("15min")
            i = max(i + 1, i15.get(ts_exit, i) + 1)
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    pairs = [p["pair"] for p in rd.fetch_liquid_pairs()]
    print(f"Liquid pairs: {len(pairs)}", flush=True)
    res, used, span = {"15m": [], "5m": []}, 0, None
    with ThreadPoolExecutor(max_workers=b5.WORKERS) as ex:
        for k, (pair, d5) in enumerate(ex.map(b5.load5, pairs), 1):
            if d5.empty or len(d5) < 288 * 4:
                continue
            used += 1
            span = span or f"{d5['Timestamp'].iloc[0]} -> {d5['Timestamp'].iloc[-1]}"
            o = run_pair(d5)
            for v in o:
                res[v] += o[v]
            if k % 20 == 0:
                print(f"  {k}/{len(pairs)}", flush=True)
    lines = [f"Loss-exit MACD timeframe, {used} coins, {span}, entry on 15m, managed on 5m, fees 0.118%, 20 USDT/trade.", "",
             "| MACD loss check on | Trades | Win % | Net R | Net USDT @20 | PF | Max DD USDT | 1st half | 2nd half | MACD loss exits | avg loss per MACD exit (R) |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for v in ("15m", "5m"):
        s = g.summ(res[v])
        x = pd.DataFrame(res[v])
        ml = x[x["exit"] == "macd_loss"] if len(x) else x
        lines.append(f"| {v}{' (now)' if v == '15m' else ''} | {s['n']} | {s['win']} | {s['R']} | {s['usdt']} | {s['pf']} | {s['dd']} | "
                     f"{s['h1']} | {s['h2']} | {len(ml)} | {ml['loss_R'].mean():.2f} |")
    for v in ("15m", "5m"):
        x = pd.DataFrame(res[v])
        lines += ["", f"Exit types ({v}):", "| Exit | Trades | Net USDT |", "|---|---|---|"]
        for k2, gg in x.groupby("exit"):
            lines.append(f"| {k2} | {len(gg)} | {gg['usdt'].sum():.2f} |")
    text = "\n".join(lines) + "\n"
    with open(os.path.join(OUT, "backtest_results.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print("\n=== RESULT ===\n" + text, flush=True)


if __name__ == "__main__":
    main()
