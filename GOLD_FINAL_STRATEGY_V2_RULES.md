# 🥇 GOLD (XAUUSDT / PAXGUSDT) — FINAL INTRADAY FUTURES STRATEGY (v2)

**Entry TF:** 5-Min | **Confirmation:** 15-Min | **Trend filter:** 1-Hour
**Kab trade nahi:** Asian session, weekend, news window, ya koi bhi filter against.

---

## 1. ⏱️ Trading Timings (IST)

| Session | Time | Action |
|---|---|---|
| New York (BEST) | 6:30 PM – 10:30 PM | Full position size |
| London (Medium) | 1:30 PM – 4:30 PM | Half position size |
| Asian | 5:30 AM – 12:00 PM | **NO TRADE** |

- **Weekend (Sat/Sun): NO TRADE.** Asli gold market band hota hai, liquidity kam aur fake spikes zyada.
- Asian session ki High/Low note karo; London/NY mein uska breakout extra confirmation hai.
- **Time exit:** saari positions **10:30 PM IST tak band** (London trade ho to 4:30 PM tak). Overnight hold nahi, funding fee bhi isse bachti hai.

---

## 2. 🧭 Trend Filters (Pehle yeh check karo)

1. **EMA 200 (5m):** price upar → sirf BUY, neeche → sirf SELL.
2. **15-Min confirmation:** 15m price bhi EMA 200 ke sahi side par ho **aur** last closed 15m candle trade ki direction mein ho.
3. **1H Trend:** BUY ke liye EMA 50 > EMA 200, SELL ke liye EMA 50 < EMA 200.
4. **VWAP (session):** price upar → BUY, neeche → SELL.
5. **ADX (14) > 20** (5m par). Isse neeche market sideways, trade nahi.

**Ek bhi filter against → trade skip.**

---

## 3. 📊 Setup A — Volume Profile Breakout (POC / VAH / VAL)

- **Session Volume Profile reset time: 5:30 AM IST (00:00 UTC)** — roz isi time se naya profile.
- POC par seedha trade nahi.
- **LONG:** price POC zone se nikal kar **VAH ke upar candle close** kare.
- **SHORT:** price **VAL ke neeche candle close** kare.
- Breakout candle: volume > 20-candle average ka **1.5×**, body ≥ **60%**.
- Entry: agli candle breakout candle ka High (long) / Low (short) tode.

---

## 4. 💥 Setup B — Bollinger Squeeze Breakout

**Squeeze (measurable):** BB(20,2) Bandwidth pichhli 100 candles ke bottom 20% mein, **ya** BB, Keltner Channel (20, 1.5×ATR) ke andar.

**LONG:**
1. Squeeze ke baad strong green candle **Upper Band ke bahar close**.
2. Body ≥ 60%, volume > 1.5× average.
3. Agli candle uska **High tode** → LONG entry.

**SHORT (mirror):**
1. Squeeze ke baad strong red candle **Lower Band ke neeche close**.
2. Body ≥ 60%, volume > 1.5× average.
3. Agli candle uska **Low tode** → SHORT entry.

### Setup C (Optional) — Rejection Short (alag, counter-trend)
- Candle Upper Band se reject ho, lambi upper wick, agli candle red close.
- Ye counter-trend hai, isliye EMA 200/VWAP filter yahan lagu nahi hota. Iske liye alag rules: **half size, SL wick ke upar, target sirf 2R, aur sirf tab jab 1H trend neutral/sideways ho.**
- Backtest mein A/B se alag test karo. Acha result na aaye to hata do.

---

## 5. 🛑 Stop-Loss & R-Ladder

- **ATR(14)** use karo.
- **SL:** entry se 1.5×ATR (ya breakout candle ke opposite end par, jo paas ho, lekin 1.5×ATR se zyada nahi).
- **Minimum SL distance:** itna ki fees + spread total 10–15% R se zyada na khayein. Kam ho to trade skip.
- **1R = entry se SL ka distance.**

### 🔒 Step-Lock Trailing

**BUY:**

| Price pahunchi | SL kahan |
|---|---|
| Entry | Original SL (−1R) |
| +1R | Entry (Breakeven) |
| +2R | +1R par lock |
| +3R | +2R par lock |
| +4R | +3R par lock |
| **+5R** | **FULL EXIT** |

**SELL:** bilkul ulta (price neeche, wahi ladder).

**Rules:**
- SL sirf aage badhta hai, peeche nahi.
- Backtest mein level **candle close** par maanna.
- Lock-SL hit ho to us lock ka profit le kar exit.
- **Stall exit:** 20–30 candles (5m) mein +1R nahi mila to trade band.
- Time exit (Section 1) ladder se upar hai.

---

## 6. 💰 Risk Management

- **Per trade risk:** max 1% capital.
- **Position size = min(** (Capital × 1%) ÷ SL distance , **max 5x leverage wala size )**. Dono mein jo chhota ho.
- Isolated margin.
- **Ek time par ek hi position.** Setup A aur B ek saath trigger hon to ek hi trade.
- **Cooldown:** exit ke baad 3–5 candle ruko.
- **Daily loss limit:** 2–3% → us din trading band.
- **Weekly loss limit:** 5% → hafte ke baaki din band.
- **Drawdown 10%** → strategy rok kar review.
- **Max 3 trades/din; 2 consecutive losses** → us din band.
- Revenge trade bilkul nahi.

---

## 7. 📰 News Filter

Ye events ke **30 min pehle aur baad** koi naya trade nahi:
FOMC / Fed Chair speech, NFP, US CPI / PPI, bade Jobless Claims.

**IST time (naye din ke hisaab se; US DST 1 Nov 2026 ko khatam hua):**
- NFP / CPI / PPI / Claims: lagbhag **7:00 PM IST**
- US stock market open: lagbhag **8:00 PM IST**
- FOMC: lagbhag **12:30 AM IST**

Har saal DST badalne par ye time ek ghanta shift hote hain, isliye har hafte economic calendar dekh lo.
Khule trade ho to SL breakeven par ya band karo.

---

## 8. 🔀 PAXG vs XAU Filter

- PAXG aur XAU ka **basis gap bahut zyada** ho to skip.
- XAUUSDT perpetual ka **funding rate extreme** ho (bahut positive/negative) → us direction mein entry se bacho.

---

## 9. 🧮 Backtest / Paper Trading Rules

- Backtest mein **fees, spread aur slippage** zaroor lo (warna result jhootha).
- Metrics: win rate, profit factor, **expectancy (R mein)**, max drawdown, trades/din.
- Kam se kam 1 saal data, phir **2–4 hafte paper trading**, tab live.
- Setup A, B (aur C) ko alag-alag test karo, phir combined.

---

## 10. ✅ Final Checklist (har trade se pehle)

- [ ] Weekday hai, session NY/London (Asian nahi)
- [ ] Koi news 30 min ke andar nahi
- [ ] EMA 200 + 15m + 1H + VWAP + ADX>20 — sab align
- [ ] Setup A ya B ka trigger complete
- [ ] Volume > 1.5×, body ≥ 60%
- [ ] SL (1.5×ATR) aur R-Ladder set; fees ke baad SL distance theek
- [ ] Position size = min(1% risk, 5x leverage)
- [ ] Koi aur position khuli nahi, cooldown khatam
- [ ] Aaj ke trades ≤ 3, daily/weekly loss limit hit nahi
- [ ] Time exit yaad hai (10:30 PM IST)

**Ek bhi tick missing ho → trade mat lo.**

⚠️ Ye strategy backtest ke bina hai. Koi bhi strategy profit ki guarantee nahi deti.