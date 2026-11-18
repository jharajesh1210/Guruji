Paper trades 2026-10-21 05:30:00 -> 2026-11-18 07:00:00, 1356 trades with 1m data (of 1356), pairs 116
Live: fill = mid of first 1m candle after the entry candle, fees 0.118% round trip, 7% emergency stop, size 20 USDT notional

| Scenario | Trades | Skipped (late) | Wins | Win% | Net R | Net USDT @20 | PF | Max DD (R) |
|---|---|---|---|---|---|---|---|---|
| PAPER (reference, no fees) | 1356 | 0 | 634 | 46.8 | 528.14 | 159.73 | 1.51 | -37.91 |
| LIVE FAST lock, guard 0.3R | 1286 | 70 | 601 | 46.7 | -73.99 | 6.41 | 0.93 | -143.17 |
| LIVE FAST lock, guard 0.5R | 1319 | 37 | 623 | 47.2 | -71.2 | 9.96 | 0.94 | -137.42 |
| LIVE FAST lock, guard 0.75R | 1332 | 24 | 634 | 47.6 | -52.84 | 16.01 | 0.95 | -132.11 |
| LIVE FAST lock, guard 1.0R | 1336 | 20 | 637 | 47.7 | -45.89 | 16.67 | 0.96 | -127.82 |
| LIVE FAST lock, guard noneR | 1345 | 11 | 643 | 47.8 | -37.74 | 21.44 | 0.97 | -129.62 |
| LIVE CANDLE lock, guard 0.3R | 1286 | 70 | 574 | 44.6 | -96.34 | 1.88 | 0.92 | -157.34 |
| LIVE CANDLE lock, guard 0.5R | 1319 | 37 | 592 | 44.9 | -86.38 | 6.81 | 0.93 | -145.67 |
| LIVE CANDLE lock, guard 0.75R | 1332 | 24 | 600 | 45.0 | -74.06 | 14.01 | 0.94 | -133.12 |
| LIVE CANDLE lock, guard 1.0R | 1336 | 20 | 603 | 45.1 | -64.1 | 15.06 | 0.95 | -125.08 |
| LIVE CANDLE lock, guard noneR | 1345 | 11 | 606 | 45.1 | -78.82 | 16.3 | 0.94 | -149.76 |

| Step (no guard) | Trades | Net R | Net USDT @20 |
|---|---|---|---|
| Paper | 1356 | 528.14 | 159.73 |
| FAST: fill at paper price, no fees | 1356 | 198.15 | 45.81 |
| FAST: real fill (1st minute), no fees | 1345 | 200.47 | 53.18 |
| FAST: real fill + fees | 1345 | -37.74 | 21.44 |
| CANDLE: fill at paper price, no fees | 1356 | 168.27 | 44.56 |
| CANDLE: real fill (1st minute), no fees | 1345 | 159.39 | 48.04 |
| CANDLE: real fill + fees | 1345 | -78.82 | 16.3 |

Avg entry slip: 0.011R (median 0.000R); avg fee 0.177R (median 0.117R)

| Exit type (FAST, no guard) | Trades | Net R | Net USDT @20 |
|---|---|---|---|
| emergency_7pct | 46 | -176.36 | -65.49 |
| lock_fast_market_1:2 | 323 | 504.24 | 123.65 |
| lock_fast_market_1:3 | 51 | 113.21 | 27.91 |
| lock_fast_market_1:4 | 14 | 36.42 | 8.37 |
| lock_stop_1:2 | 169 | 301.70 | 67.91 |
| lock_stop_1:3 | 18 | 47.82 | 10.40 |
| lock_stop_1:4 | 2 | 7.11 | 1.70 |
| macd_loss | 663 | -974.39 | -184.46 |
| next_day_pdh | 15 | 9.12 | 2.00 |
| next_day_pdl | 26 | 21.88 | 6.90 |
| target_1:5 | 18 | 71.52 | 22.53 |

| Paper exit type | Trades | Net R | Net USDT @20 |
|---|---|---|---|
| Next_Day_PDH_Break_Exit | 16 | 14.31 | 2.68 |
| Next_Day_PDL_Break_Exit | 36 | 43.73 | 12.72 |
| SL_Hit_Exit_Locked | 529 | 1238.00 | 284.77 |
| Stop_Loss_Hit_MACD_Confirmed | 722 | -1032.90 | -211.91 |
| Target | 53 | 265.00 | 71.48 |
