PDH/PDL vs rolling N-candle levels [96, 110, 200]. 110 coins, data 2026-08-19 08:30:00 -> 2026-11-19 08:00:00. Real rd_trading logic + caps (5 open). Live estimate = next-open fill + 0.118% fees, 20 USDT/trade.

### Stage 1 - 90 days (15m)

| Level | Trades | Paper win % | Paper net R | Live-est net R | Live-est USDT @20 | PF (USDT) | Max DD USDT | 1st half USDT | 2nd half USDT |
|---|---|---|---|---|---|---|---|---|---|
| PDH/PDL (now) | 3615 | 43.3 | 861.77 | 341.72 | 168.93 | 1.25 | -21.3 | 50.33 | 118.6 |
|  by month: 2026-08: 19.1, 2026-09: 41.2, 2026-10: 26.4, 2026-11: 82.2 | | | | | | | | | |
| Rolling last 96 candles | 2753 | 45.7 | 796.92 | 457.82 | 186.16 | 1.35 | -25.16 | 54.41 | 131.75 |
|  by month: 2026-08: 38.4, 2026-09: 22.4, 2026-10: 33.8, 2026-11: 91.6 | | | | | | | | | |
| Rolling last 110 candles | 2673 | 44.8 | 714.01 | 389.3 | 172.27 | 1.32 | -24.65 | 48.27 | 124.0 |
|  by month: 2026-08: 34.0, 2026-09: 17.3, 2026-10: 28.8, 2026-11: 92.2 | | | | | | | | | |
| Rolling last 200 candles | 2328 | 45.2 | 678.92 | 411.77 | 179.16 | 1.38 | -20.4 | 64.73 | 114.44 |
|  by month: 2026-08: 29.7, 2026-09: 37.8, 2026-10: 34.2, 2026-11: 77.5 | | | | | | | | | |

### Stage 2 - last 30 days, live simulation on 1m (fast lock, 0.75R guard, fees)

| Level | Live trades | Skipped late | Win % | Net R | Net USDT @20 | PF | Max DD R |
|---|---|---|---|---|---|---|---|
| PDH/PDL (now) | 1251 | 17 | 47.6 | 3.33 | 12.67 | 1.05 | -114.62 |
| Rolling last 96 candles | 940 | 12 | 48.6 | 31.09 | 16.1 | 1.08 | -61.23 |
| Rolling last 110 candles | 888 | 11 | 49.0 | 36.95 | 22.13 | 1.12 | -60.29 |
| Rolling last 200 candles | 760 | 9 | 49.2 | 28.81 | 13.62 | 1.08 | -63.01 |
