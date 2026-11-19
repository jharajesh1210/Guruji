Paper vs live gap. 111 coins, 1230 paper trades with 1m data, 2026-10-22 06:15:00 -> 2026-11-19 19:45:00. Size 25 USDT per trade.

### Waterfall (paper -> live, one effect at a time)

| Step | Trades | Win % | Net R | Net USDT @25 | PF | Max DD USDT | 1st half | 2nd half |
|---|---|---|---|---|---|---|---|---|
| P1 paper as booked (loss cap 100) | 1230 | 46.8 | 498.88 | 175.63 | 1.67 | -9.0 | 84.94 | 90.69 |
| P2 paper, NO loss cap | 1230 | 46.8 | 342.07 | 108.99 | 1.33 | -18.96 | 62.88 | 46.12 |
| L1 live engine ideal (paper fill, no fees, no emergency) | 1230 | 48.3 | 210.26 | 54.87 | 1.18 | -23.29 | 37.14 | 17.73 |
| L2 + 15% emergency stop | 1230 | 48.3 | 217.63 | 58.56 | 1.2 | -21.28 | 40.05 | 18.50 |
| L3 + real fill + 0.75R guard | 1216 | 48.2 | 210.44 | 56.54 | 1.2 | -20.8 | 39.55 | 16.99 |
| L4 + fees = TODAY'S LIVE | 1216 | 47.8 | 39.7 | 20.67 | 1.07 | -24.77 | 21.62 | -0.95 |

Exit types in today's live (L4):

| Exit | Trades | Net USDT @25 |
|---|---|---|
| emergency_7pct | 6 | -22.68 |
| lock_fast_market | 338 | 183.91 |
| lock_stop | 189 | 106.37 |
| macd_loss | 633 | -274.99 |
| next_day_pdh | 14 | 3.92 |
| next_day_pdl | 26 | 8.40 |
| target | 10 | 15.72 |

Exit types in paper (P1):

| Exit | Trades | Net USDT @25 |
|---|---|---|
| Next_Day_PDH_Break_Exit | 15 | 4.64 |
| Next_Day_PDL_Break_Exit | 37 | 15.03 |
| SL_Hit_Exit_Locked | 483 | 338.26 |
| Stop_Loss_Hit_MACD_Confirmed | 654 | -261.13 |
| Target | 41 | 78.83 |

### Fixes (each on top of today's live L4)

| Step | Trades | Win % | Net R | Net USDT @25 | PF | Max DD USDT | 1st half | 2nd half |
|---|---|---|---|---|---|---|---|---|
| L4 today's live | 1216 | 47.8 | 39.7 | 20.67 | 1.07 | -24.77 | 21.62 | -0.95 |
| Lock one step behind (1:2->SL 1:1, 1:3->1:2, 1:4->1:3) | 1216 | 47.3 | 32.33 | 23.32 | 1.08 | -23.68 | 14.36 | 8.97 |
| Lock half step behind (1:2->SL 1:1.5 ...) | 1216 | 47.7 | 40.7 | 19.98 | 1.07 | -26.3 | 17.34 | 2.64 |
| 1:2 -> breakeven, then one step behind | 1216 | 36.3 | 66.3 | 35.06 | 1.11 | -26.27 | 24.85 | 10.21 |
| Lock on 15m candle close (like paper) | 1216 | 45.9 | 17.74 | 17.78 | 1.06 | -28.15 | 19.97 | -2.18 |
| Min SL 0.5% (skip tighter trades) | 1000 | 45.9 | -1.49 | 16.63 | 1.06 | -27.22 | 16.89 | -0.27 |
| Min SL 0.75% | 814 | 47.4 | 53.23 | 25.1 | 1.1 | -25.21 | 26.30 | -1.21 |
| Lock half step behind + min SL 0.5% | 1000 | 45.9 | 10.04 | 16.86 | 1.06 | -27.83 | 14.07 | 2.79 |
| Lock one step behind + min SL 0.5% | 1000 | 45.6 | 12.19 | 21.32 | 1.07 | -24.41 | 13.28 | 8.04 |
