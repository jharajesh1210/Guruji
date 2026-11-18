# MOTHER STRATEGY v1 — R&D PDH/PDL Breakout (frozen 18 Nov 2026)

Ye folder **sirf record ke liye** hai. Isme koi change NAHI karna.
Chalne wala code `rd_trading.py` (paper) aur `rd_live.py` (live) hai — unme change ho sakta hai,
lekin original hamesha yahan aur git tag `mother-strategy-v1` mein milega.

Files:
- `rd_trading_MOTHER_v1.py` — paper / strategy logic (source of truth)
- `rd_live_MOTHER_v1.py` — CoinDCX live runner (paper ki copy)
- `SHA256.txt` — fingerprint; agar file badli to hash match nahi karega

Wapas laane ke liye (server par):
```
cd ~/Guruji && git fetch --tags && git checkout mother-strategy-v1 -- rd_trading.py rd_live.py
```

## Strategy ke rules

**Coin filter**
1. CoinDCX USDT futures, 24h volume > 10 million USDT (ticker + pichhli 96 candles)
2. Ticker 10 minute se purana na ho

**Entry (15m candle close par)**
3. PDH/PDL = pichhle trading din (05:30–05:30 IST) ka high/low, us din kam se kam 90 candles
4. BUY: high > PDH aur close > PDH | SELL: low < PDL aur close < PDL
5. Volume > 1.5 × pichhli 20 candles ka average
6. Wick filter: BUY mein upar ki wick < 40% candle, SELL mein neeche ki wick < 40%
7. Entry = candle close; SL = breakout candle ka low (BUY) / high (SELL)
8. SL distance 0.3% se kam -> skip; 2% se zyada -> 2% par cap
9. Us coin par koi trade khula na ho; max 5 khule trade, ek scan mein max 5 nayi entry
10. Profit exit ke baad usi session (Asian / London / NY) mein wahi coin usi direction mein dobara nahi

**Sessions (IST):** Asian 05:30–13:30, London 13:30–18:30, NY 18:30–05:30

**Profit lock / target**
11. 1:2 chhua -> SL 1:2 par; 1:3 -> 1:3; 1:4 -> 1:4; 1:5 -> full exit
12. Lock agli candle se lagta hai; agar lock wali candle usi lock ke neeche (SELL: upar) band ho to usi lock par exit

**Loss exit (koi lock nahi laga)**
13. SL chhua + MACD(12,26,9) confirm (BUY: MACD < signal, SELL: MACD > signal) -> exit
14. MACD confirm nahi -> wait, agli candle par phir check; exit price = SL aur close mein jo kharab ho
15. Paper mein max loss 100 USDT per trade

**Agle din**
16. Carry-over trade: BUY mein naya PDH (entry ke upar) chhua -> exit (max 1:5); SELL mein naya PDL

**Paper size:** 100 USDT × 30x = 3000 USDT per trade

## Live settings (18 Nov 2026)
- Live sirf wahi trade leta hai jo paper leta hai (same coin, candle, direction), dono server par ek saath
- Margin 2 USDT × 10x, MAX_LIVE_OPEN = 5, emergency stop 7% (exchange par)
- Late entry limit 0.75R; lock har 1 minute check, lock stop CoinDCX par
