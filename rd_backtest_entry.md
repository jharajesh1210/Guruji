Entry-timing filters, 112 coins, last 90 days. Live-style 1m, 25 USDT/trade, fees, 15% emergency.

### How late are today's entries? (paper trades, NONE)

- Entries: 3770; fresh cross (prev candle still inside): 38%
- Distance beyond PDH/PDL at entry: median 1.33%, 75% 3.34%, 90% 7.28%
- Same-colour run (incl. signal candle) >= 3: 37% of entries

| Group | Entries | Paper PnL (no cap, 3000 size) | avg per trade |
|---|---|---|---|
| fresh cross | 1449 | 6293 | 4.3 |
| NOT fresh (already beyond) | 2321 | 14141 | 6.1 |
| beyond <= 0.5% | 972 | 3308 | 3.4 |
| beyond 0.5-2% | 1360 | -1060 | -0.8 |
| beyond > 2% | 1438 | 18186 | 12.6 |
| run <= 2 | 2361 | 9768 | 4.1 |
| run >= 3 | 1409 | 10666 | 7.6 |

1000LUNC entries:

| time | dir | beyond PDH/PDL % | fresh | run | paper PnL |
|---|---|---|---|---|---|
| 2026-08-23 08:45:00 | SELL | 0.83 | True | 3 | -38 |
| 2026-09-21 04:45:00 | BUY | 3.40 | False | 3 | -35 |
| 2026-11-17 15:00:00 | BUY | 0.72 | False | 0 | 50 |
| 2026-11-18 02:00:00 | BUY | 10.69 | False | 2 | -48 |
| 2026-11-19 09:30:00 | BUY | 5.82 | False | 1 | 119 |

### Filters (90 days, live-style)

| Entry rule | Paper trades | Paper USDT@25 (no cap) | LIVE trades | LIVE win % | LIVE net R | LIVE USDT @25 | PF | Max DD | by month |
|---|---|---|---|---|---|---|---|---|---|
| NONE | 3770 | 170.3 | 3743 | 44.1 | -402.83 | -82.53 | 0.91 | -132.27 | 2026-08: -5, 2026-09: -68, 2026-10: -19, 2026-11: 9 |
| FRESH | 2462 | 97.5 | 2441 | 41.8 | -317.5 | -92.76 | 0.87 | -115.2 | 2026-08: 8, 2026-09: -66, 2026-10: 1, 2026-11: -36 |
| NEAR_1ATR | 2864 | 64.9 | 2845 | 43.3 | -383.55 | -102.35 | 0.86 | -130.71 | 2026-08: -23, 2026-09: -67, 2026-10: 3, 2026-11: -15 |
| NEAR_0.5% | 2227 | 77.3 | 2224 | 42.8 | -284.79 | -37.79 | 0.91 | -52.63 | 2026-08: -6, 2026-09: -23, 2026-10: -3, 2026-11: -6 |
| RUN<=2 | 3630 | 137.7 | 3593 | 44.3 | -388.4 | -81.91 | 0.91 | -108.38 | 2026-08: -8, 2026-09: -75, 2026-10: 13, 2026-11: -12 |
| FRESH+RUN | 1889 | 21.2 | 1876 | 40.5 | -262.11 | -88.78 | 0.83 | -100.87 | 2026-08: -1, 2026-09: -76, 2026-10: 9, 2026-11: -21 |
