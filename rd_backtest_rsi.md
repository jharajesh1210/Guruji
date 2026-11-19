RSI filter on LONG entries. 110 coins, 2026-08-19 08:45:00 -> 2026-11-19 08:15:00. Real rd_trading logic + caps.

### Stage 1 - 90 days (15m), live estimate 20 USDT/trade after fees

| Case | Trades | BUY skipped | Paper win % | Live-est net R | Live-est USDT | PF | Max DD USDT | 1st half | 2nd half |
|---|---|---|---|---|---|---|---|---|---|
| No RSI filter (now) | 3617 | 0 | 43.5 | 362.99 | 175.14 | 1.26 | -21.3 | 56.73 | 118.41 |
|   BUY trades only: 2090 trades, 114.5 USDT | | | | | | | | | |
|   by month: 2026-08: 19.1, 2026-09: 47.4, 2026-10: 26.4, 2026-11: 82.2 | | | | | | | | | |
| Skip BUY if RSI > 80 | 3637 | 432 | 42.4 | 202.07 | 123.16 | 1.18 | -24.34 | 21.45 | 101.71 |
|   BUY trades only: 2064 trades, 67.8 USDT | | | | | | | | | |
|   by month: 2026-08: 10.7, 2026-09: 21.2, 2026-10: 22.8, 2026-11: 68.4 | | | | | | | | | |
| Skip BUY if RSI > 75 | 3643 | 1109 | 42.1 | 94.59 | 101.02 | 1.15 | -49.92 | -6.67 | 107.7 |
|   BUY trades only: 1993 trades, 42.8 USDT | | | | | | | | | |
|   by month: 2026-08: 16.3, 2026-09: -8.4, 2026-10: 19.9, 2026-11: 73.2 | | | | | | | | | |
| Skip BUY if RSI > 70 | 3519 | 2685 | 40.8 | 7.33 | 63.49 | 1.1 | -35.4 | -6.73 | 70.22 |
|   BUY trades only: 1797 trades, 2.4 USDT | | | | | | | | | |
|   by month: 2026-08: -1.9, 2026-09: 3.0, 2026-10: 13.0, 2026-11: 49.5 | | | | | | | | | |

### Stage 2 - last 30 days, live simulation on 1m (fast lock, 0.75R, 15% emergency, fees)

| Case | Live trades | Win % | Net R | Net USDT @20 | PF | Max DD R |
|---|---|---|---|---|---|---|
| No RSI filter (now) | 1251 | 48.4 | 48.11 | 29.77 | 1.12 | -97.62 |
| Skip BUY if RSI > 80 | 1256 | 46.9 | 22.93 | 25.46 | 1.11 | -116.52 |
| Skip BUY if RSI > 75 | 1258 | 48.5 | 55.43 | 40.71 | 1.19 | -121.56 |
| Skip BUY if RSI > 70 | 1233 | 46.1 | -27.67 | 6.55 | 1.03 | -129.84 |
