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
