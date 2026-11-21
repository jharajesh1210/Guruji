Win-rate study, current live rules (v2 + BB squeeze), 145 coins, 90 days, live-style 1m, 10 USDT/trade, fees.

### Part A - what separates winners from losers (base trades)

| Group | Trades | Win % | USDT |
|---|---|---|---|
| ALL | 846 | 38.8 | 0.30 |
| session Asian | 356 | 37.4 | 0.91 |
| session London | 186 | 31.7 | -5.15 |
| session NY | 304 | 44.7 | 4.53 |
| BUY | 394 | 36.0 | -5.42 |
| SELL | 452 | 41.2 | 5.71 |
| 1h trend WITH trade | 764 | 38.7 | -0.79 |
| 1h trend AGAINST trade | 82 | 39.0 | 1.08 |
| volume 1.5-2x | 224 | 37.5 | -0.38 |
| volume 2-3x | 271 | 37.6 | -1.14 |
| volume >= 3x | 351 | 40.5 | 1.82 |
| SL 0.75-1% | 269 | 39.0 | -2.07 |
| SL 1-1.5% | 326 | 36.8 | -1.32 |
| SL 1.5-2% | 251 | 41.0 | 3.69 |
| distance 0-0.25% | 436 | 38.8 | -0.60 |
| distance 0.25-0.5% | 410 | 38.8 | 0.90 |

Exit types (base):

| Exit | Trades | USDT |
|---|---|---|
| hard_cap | 173 | -33.32 |
| lock_candle_market | 199 | 44.22 |
| lock_stop | 82 | 23.17 |
| macd_loss | 344 | -44.49 |
| next_day_pdh | 17 | 2.16 |
| next_day_pdl | 25 | 4.93 |
| target | 6 | 3.63 |

### Part B - ideas tested

| Variant | Trades | Win % | Net R | Net USDT @10 | PF | Max DD | 1st half | 2nd half |
|---|---|---|---|---|---|---|---|---|
| NOW (base) | 846 | **38.8** | -18.58 | 0.3 | 1.0 | -8.4 | -6.5 | 6.8 |
| 1. Breakeven at 1R | 846 | **31.9** | -18.5 | -0.38 | 0.99 | -6.95 | -5.5 | 5.1 |
| 2. Partial 50% at 1R + rest BE | 846 | **56.7** | -20.76 | -0.94 | 0.98 | -6.12 | -3.9 | 3.0 |
| 2b. Partial 50% at 1.5R + rest BE | 846 | **45.7** | -26.49 | -1.19 | 0.98 | -6.67 | -4.9 | 3.7 |
| 3. 1h trend filter | 792 | **39.0** | -17.55 | -0.83 | 0.99 | -8.09 | -5.9 | 5.1 |
| 4. Volume >= 2x | 645 | **38.6** | -18.71 | -1.18 | 0.98 | -8.42 | -7.1 | 5.9 |
| 5. Skip London | 676 | **40.5** | 21.72 | 4.57 | 1.08 | -5.34 | -4.5 | 9.0 |
| 6. 1h trend + partial 1R | 792 | **56.7** | -18.41 | -1.45 | 0.97 | -6.4 | -3.8 | 2.3 |
| 7. 1h trend + vol 2x + partial 1R | 591 | **56.5** | -20.86 | -2.18 | 0.95 | -6.69 | -5.5 | 3.3 |
| 8. Skip London + partial 1R | 676 | **57.4** | -5.4 | 1.23 | 1.03 | -3.96 | -3.1 | 4.3 |
| 9. 1h trend + breakeven 1R | 792 | **32.6** | -17.11 | -1.37 | 0.98 | -6.92 | -4.8 | 3.4 |
