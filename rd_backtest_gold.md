## Gold only (B-XAU_USDT,B-XAUT_USDT,B-PAXG_USDT) - with 24h volume > 10M USDT filter (same as now)

Paper trades 2026-08-24 03:30:00 -> 2026-11-18 07:15:00, 184 trades with 1m data (of 184), pairs 2
Live: fill = mid of first 1m candle after the entry candle, fees 0.118% round trip, 7% emergency stop, size 20 USDT notional

| Scenario | Trades | Skipped (late) | Wins | Win% | Net R | Net USDT @20 | PF | Max DD (R) |
|---|---|---|---|---|---|---|---|---|
| PAPER (reference, no fees) | 184 | 0 | 80 | 43.5 | 17.48 | 1.2 | 1.13 | -50.98 |
| LIVE FAST lock, guard 0.3R | 182 | 2 | 75 | 41.2 | -72.74 | -3.43 | 0.59 | -101.05 |
| LIVE FAST lock, guard 0.5R | 183 | 1 | 76 | 41.5 | -71.69 | -3.38 | 0.6 | -100.01 |
| LIVE FAST lock, guard 0.75R | 184 | 0 | 77 | 41.8 | -70.84 | -3.34 | 0.6 | -99.16 |
| LIVE FAST lock, guard 1.0R | 184 | 0 | 77 | 41.8 | -70.84 | -3.34 | 0.6 | -99.16 |
| LIVE FAST lock, guard noneR | 184 | 0 | 77 | 41.8 | -70.84 | -3.34 | 0.6 | -99.16 |
| LIVE CANDLE lock, guard 0.3R | 182 | 2 | 73 | 40.1 | -75.17 | -3.67 | 0.58 | -106.22 |
| LIVE CANDLE lock, guard 0.5R | 183 | 1 | 74 | 40.4 | -74.31 | -3.62 | 0.58 | -105.36 |
| LIVE CANDLE lock, guard 0.75R | 184 | 0 | 75 | 40.8 | -73.47 | -3.59 | 0.59 | -104.52 |
| LIVE CANDLE lock, guard 1.0R | 184 | 0 | 75 | 40.8 | -73.47 | -3.59 | 0.59 | -104.52 |
| LIVE CANDLE lock, guard noneR | 184 | 0 | 75 | 40.8 | -73.47 | -3.59 | 0.59 | -104.52 |

| Step (no guard) | Trades | Net R | Net USDT @20 |
|---|---|---|---|
| Paper | 184 | 17.48 | 1.2 |
| FAST: fill at paper price, no fees | 184 | 10.99 | 1.03 |
| FAST: real fill (1st minute), no fees | 184 | 9.82 | 1.01 |
| FAST: real fill + fees | 184 | -70.84 | -3.34 |
| CANDLE: fill at paper price, no fees | 184 | 8.36 | 0.78 |
| CANDLE: real fill (1st minute), no fees | 184 | 7.2 | 0.76 |
| CANDLE: real fill + fees | 184 | -73.47 | -3.59 |

Avg entry slip: 0.006R (median 0.007R); avg fee 0.438R (median 0.421R)

| Exit type (FAST, no guard) | Trades | Net R | Net USDT @20 |
|---|---|---|---|
| lock_fast_market_1:2 | 27 | 38.75 | 2.65 |
| lock_fast_market_1:3 | 1 | 1.65 | 0.07 |
| lock_fast_market_1:4 | 3 | 5.98 | 0.22 |
| lock_stop_1:2 | 23 | 34.64 | 2.32 |
| lock_stop_1:3 | 3 | 7.24 | 0.31 |
| lock_stop_1:4 | 1 | 3.32 | 0.13 |
| macd_loss | 104 | -177.28 | -10.56 |
| next_day_pdh | 8 | 2.03 | 0.17 |
| next_day_pdl | 13 | 7.46 | 1.16 |
| target_1:5 | 1 | 5.37 | 0.19 |

| Paper exit type | Trades | Net R | Net USDT @20 |
|---|---|---|---|
| Next_Day_PDH_Break_Exit | 9 | 6.48 | 0.52 |
| Next_Day_PDL_Break_Exit | 13 | 12.10 | 1.42 |
| SL_Hit_Exit_Locked | 55 | 122.00 | 7.37 |
| Stop_Loss_Hit_MACD_Confirmed | 104 | -138.11 | -8.63 |
| Target | 3 | 15.00 | 0.52 |


## Gold only (B-XAU_USDT,B-XAUT_USDT,B-PAXG_USDT) - WITHOUT volume filter

Paper trades 2026-08-24 03:30:00 -> 2026-11-18 07:15:00, 185 trades with 1m data (of 185), pairs 2
Live: fill = mid of first 1m candle after the entry candle, fees 0.118% round trip, 7% emergency stop, size 20 USDT notional

| Scenario | Trades | Skipped (late) | Wins | Win% | Net R | Net USDT @20 | PF | Max DD (R) |
|---|---|---|---|---|---|---|---|---|
| PAPER (reference, no fees) | 185 | 0 | 80 | 43.2 | 15.95 | 1.08 | 1.11 | -50.98 |
| LIVE FAST lock, guard 0.3R | 183 | 2 | 75 | 41.0 | -74.63 | -3.59 | 0.59 | -101.05 |
| LIVE FAST lock, guard 0.5R | 184 | 1 | 76 | 41.3 | -73.59 | -3.53 | 0.59 | -100.01 |
| LIVE FAST lock, guard 0.75R | 185 | 0 | 77 | 41.6 | -72.74 | -3.49 | 0.6 | -99.16 |
| LIVE FAST lock, guard 1.0R | 185 | 0 | 77 | 41.6 | -72.74 | -3.49 | 0.6 | -99.16 |
| LIVE FAST lock, guard noneR | 185 | 0 | 77 | 41.6 | -72.74 | -3.49 | 0.6 | -99.16 |
| LIVE CANDLE lock, guard 0.3R | 183 | 2 | 73 | 39.9 | -77.07 | -3.83 | 0.57 | -106.22 |
| LIVE CANDLE lock, guard 0.5R | 184 | 1 | 74 | 40.2 | -76.21 | -3.78 | 0.58 | -105.36 |
| LIVE CANDLE lock, guard 0.75R | 185 | 0 | 75 | 40.5 | -75.36 | -3.74 | 0.58 | -104.52 |
| LIVE CANDLE lock, guard 1.0R | 185 | 0 | 75 | 40.5 | -75.36 | -3.74 | 0.58 | -104.52 |
| LIVE CANDLE lock, guard noneR | 185 | 0 | 75 | 40.5 | -75.36 | -3.74 | 0.58 | -104.52 |

| Step (no guard) | Trades | Net R | Net USDT @20 |
|---|---|---|---|
| Paper | 185 | 15.95 | 1.08 |
| FAST: fill at paper price, no fees | 185 | 9.46 | 0.9 |
| FAST: real fill (1st minute), no fees | 185 | 8.21 | 0.87 |
| FAST: real fill + fees | 185 | -72.74 | -3.49 |
| CANDLE: fill at paper price, no fees | 185 | 6.83 | 0.65 |
| CANDLE: real fill (1st minute), no fees | 185 | 5.58 | 0.62 |
| CANDLE: real fill + fees | 185 | -75.36 | -3.74 |

Avg entry slip: 0.007R (median 0.008R); avg fee 0.438R (median 0.421R)

| Exit type (FAST, no guard) | Trades | Net R | Net USDT @20 |
|---|---|---|---|
| lock_fast_market_1:2 | 27 | 38.75 | 2.65 |
| lock_fast_market_1:3 | 1 | 1.65 | 0.07 |
| lock_fast_market_1:4 | 3 | 5.98 | 0.22 |
| lock_stop_1:2 | 23 | 34.64 | 2.32 |
| lock_stop_1:3 | 3 | 7.24 | 0.31 |
| lock_stop_1:4 | 1 | 3.32 | 0.13 |
| macd_loss | 105 | -179.17 | -10.71 |
| next_day_pdh | 8 | 2.03 | 0.17 |
| next_day_pdl | 13 | 7.46 | 1.16 |
| target_1:5 | 1 | 5.37 | 0.19 |

| Paper exit type | Trades | Net R | Net USDT @20 |
|---|---|---|---|
| Next_Day_PDH_Break_Exit | 9 | 6.48 | 0.52 |
| Next_Day_PDL_Break_Exit | 13 | 12.10 | 1.42 |
| SL_Hit_Exit_Locked | 55 | 122.00 | 7.37 |
| Stop_Loss_Hit_MACD_Confirmed | 105 | -139.64 | -8.75 |
| Target | 3 | 15.00 | 0.52 |
