# Automated Gold & Forex Trading System
### Decision Brief for Non-Technical Stakeholders

## What This Is

This project builds a 24/7 automated trading system for Gold and Forex that replicates how a professional trading team operates — five specialists working together on every single trade decision before any money is committed to a trade.

Instead of one "black-box" algorithm making isolated decisions, the system enforces debate and checks at every step:

### The Five-Person Team

1. **The Brain (AI Council)** — Three analytical voices score each trading opportunity: a bull (looks for reasons to buy), a bear (looks for reasons to sell), and a risk-management voice (watches for danger signs like too-wide stop-losses, approaching economic news, or market turmoil). A trade only proceeds if it passes a majority vote and passes the risk-manager's veto gate. When a decision is borderline or high-stakes, the system escalates to Claude AI for a human-level sanity check before acting.

2. **The Shield (Portfolio Checkpoint)** — Before the Council's idea becomes an order, this checkpoint asks: is this trade good enough on its own (win/loss ratio)? Would it duplicate risk we already have? Does it fit the current portfolio direction? If not, it blocks the trade with a logged reason.

3. **The CFO (Money Manager)** — Given an approved trade idea, this role calculates the right position size: How much of the account should we risk? Adjust for current market volatility. Size the position so even a losing trade doesn't blow up our capital. It also enforces a daily loss limit — if we've had a bad day, no more new trades until tomorrow.

4. **The Watchman (Position Monitor)** — Once a trade is open, this role actively manages it: trails the stop-loss to lock in gains, exits early if the market structure that justified the trade breaks, protects profits once they form. It works alongside a broker-side stop-loss (a hard safety net independent of our system).

5. **The Auditor (Performance Reviewer)** — Each day, reviews what happened: which trades worked, which didn't, why, what we learned. Most importantly: decides which strategies have proven themselves enough to risk real money. Nothing moves from testing to live trading without the Auditor's sign-off.

### Core Safety Philosophy

Nothing goes live with real capital until it has proven itself three times over:
- Passes historical simulation (backtesting) on data it wasn't trained on
- Runs weeks in real-time paper trading (live market conditions, zero real money at risk)
- Starts with tiny real trades and ramps up gradually — never a sudden jump to full position size

## How It Gets Built

This is not built all at once. Instead, it's built in five plain-language stages, each proven to work before the next begins:

### Stage 1: Foundation & Data (Phases 0–1)
Set up the basic plumbing: get Python and the trading platform talking, log into real-time market data, download historical price data for backtesting.

**Proof point:** The system can see and record live market data.

### Stage 2: Proof of Concept — Tiny Test Trades (Phases 2–3)
Implement one very simple trading rule and run it in shadow-mode on the broker's demo account (no real money, no risk). The full pipeline — idea generation, risk checks, position sizing, order placement — runs end-to-end, but trades are throttled and logged for manual verification.

**Proof point:** The system can place trades, broker infrastructure works, nothing crashes.

### Stage 3: Add Intelligence & Optimization (Phases 4–8)
Build the backtest engine so we can test strategies on years of historical data. Implement the full five-person team: Council (multi-voice debate), Shield (portfolio-level checks), CFO (intelligent sizing), Watchman (trailing stops). Each piece is tested independently and together.

**Proof point:** Strategies demonstrably profit in historical simulation without overfitting.

### Stage 4: Extended Real-Time Testing with No Real Money (Phase 9)
Run the full system in paper-trading mode for weeks. Live market data, real conditions, but every trade is simulated — no capital at risk. The Auditor produces daily reports. We validate that the strategy holds up in reality.

**Proof point:** The system performs as expected when facing real market conditions, surprises are understood.

### Stage 5: Careful Rollout to Real Trading (Phases 10–11)
Deploy to a VPS (a small cloud server running 24/7). First trades are tiny (0.25% of account risk per trade). Gradually increase to full size only after live performance confirms the strategy works with real money.

**Proof point:** Real trading works, monitoring/alerts work, strategy survives first contact with the live market.

**Why this approach works:** Each stage catches errors before they become expensive. If the backtest looks good but paper trading reveals a flaw, we fix it without touching real capital. Most problems surface in stages 2–4, not after months of build time.

## Cost Breakdown

All costs are for infrastructure — the tools and cloud services the system runs on. This is separate from trading capital (the money being traded with) and trading losses (the risk of trading itself).

| Item | Cost | Notes |
|---|---|---|
| Trading platform (MT5) | Free | Provided by broker |
| Python & development | Free | Open-source |
| Cloud server (VPS) | $10–30/month | Entry to mid-tier; premium servers ($60–190/month) aren't needed for our decision frequency |
| AI (Claude API) | $0 *(see update below)* | Starting with rule-based decision-making only; AI escalation deferred pending real usage data (see Borderline Tracking, Appendix A §5.4) |
| Market data & news | Free | Free sources initially; premium feeds optional later |
| Monitoring alerts | Free | Standard integrations (email, Telegram, Discord) |
| **Total monthly infrastructure** | **~$10–30/month** | Once fully live (Phase 5) — revised down from original ~$15–60/month estimate |

**Important distinction:**
- Infrastructure cost (~$10–30/month) is the operational overhead, separate from trading costs.
- Trading capital is the money being traded with — that's a separate decision and risk.
- Trading losses are the money lost from bad trades — that's the risk of the strategy, managed by daily loss limits, forced ramp, and the Auditor's gating.

## AI Provider Options (Instead of Claude)

**Decision update:** The system now starts as fully rule-based (see Appendix A). No AI subscription is used for Stages 1–4. All borderline Council decisions are logged and replayed to determine, with real data after ~3 months, whether AI escalation adds measurable value before spending on any API.

The "AI (Claude API)" line is only used for one narrow job: double-checking borderline trade decisions and writing the daily performance summary. It is not required for the system to work — the five-person team's rule-based decision-making runs on its own without any AI API. Because this piece is isolated to one small part of the system, switching providers later is easy — it doesn't affect anything else that's been built.

| Option | Est. Cost/Month | Trade-off |
|---|---|---|
| No AI at all — team runs on rules only | $0 | **Current plan.** The team's math-and-rules-based decisions are fully functional without any AI double-check. Add AI later only if there's a proven need. |
| Budget AI options (Groq, or similar low-cost providers) | ~$1–5 | Cheapest paid option. Lower judgment quality — worth testing before trusting it on real trades. |
| Google's AI (Gemini) | ~$2–10 | Often has a free usage tier that may cover this system's light, occasional use entirely. |
| OpenAI (makers of ChatGPT) | ~$2–15 | Comparable quality range to Claude, similar cost. |
| Claude (Anthropic) | ~$5–30 | Favors a stronger AI for "should we risk real money on this" decisions over marginal savings. |
| Run an AI model ourselves (no per-use fee) | $0 extra fee, but a bigger/pricier server | Avoids AI subscription costs but needs a more powerful (and more expensive) cloud server, plus more setup work. Not recommended for a first build. |

## What's Still Undecided (Non-Blockers)

A handful of choices will be made as each build stage approaches. These are not holding up the project:

- **Which broker & account** — details on trading fees, contract sizes, specific platform rules. Chosen before Phase 3.
- **Which VPS provider** — cloud hosting choice (general-purpose vs. forex-specialized). Chosen before Phase 11.
- **News data source** — free economic calendar or paid real-time news feed. Decided as Phase 2 starts.
- **AI budget ceiling** — determines whether we use cost-optimized Claude or premium tier for edge cases, if AI is adopted at all. Set before Phase 10.
- **Technical thresholds** — specific win-rate requirements for "proven" strategy, daily loss limit amount, circuit-breaker behavior. These are written into configuration and can be adjusted (see Appendix A for current defaults).

None of these are architectural problems or surprises. They're choices that depend on broker selection or budget preferences.

## Risk Acknowledgment

Automated trading carries real risks:
- Strategies can fail on unseen market conditions despite backtesting.
- Execution errors, broker platform issues, or connectivity gaps can cause losses.
- Market events (flash crashes, gaps) can exceed even carefully-sized stop-losses.

How this system mitigates those risks:
- The five-person-team approach catches logical errors that a solo algorithm would miss.
- No decision is trusted without proof: backtest, paper trading, then live ramp.
- Hard broker-side stop-losses (independent of our monitoring) protect every position.
- Daily loss limits prevent "death spiral" days from compounding.
- Full audit trails make every decision reviewable — when something goes wrong, we know why.
- The Auditor controls which strategies touch real money, using objective historical criteria.

This is not "fire and forget." It's continuous automated trading with multiple layers of human-like reasoning and mechanical safety checks.

**Realistic expectation (see Appendix B.6):** the goal for year one is that the system survives with drawdown inside its defined bounds and proves (or disproves) its edge with real data — not a profit target. Treating survival-and-proof as the success metric guards against pressure to ramp faster than the plan allows.

## Next Steps

**To greenlight:**
- Confirm budget for infrastructure (~$10–30/month).
- Confirm willingness to invest development time through Stage 2 (proof the system can place trades without crashing).
- Identify broker preference (needed by Phase 3 to finalize platform integration details).
- Confirm trading capital in the range recommended in Appendix B.3 (~$2,500–3,000) so position-sizing rules can actually place trades.

**To proceed, this document should be reviewed alongside:**
- The technical specification (spec.md) for architecture details if needed.
- **Appendix A** — the full Rule-Based Decision Spec (Council, Shield, CFO, Watchman, Auditor rules and default thresholds).
- **Appendix B** — additional recommendations (cost, timeline, minimum capital, operational runbook, disaster recovery, and expectation-setting).
- The verification summary (final section of spec) listing concrete proof-points before any live capital is committed.

---
*Document Date: 2026-07-18 | Project: Automated Gold & Forex Trading System | Status: Design Phase*
*Revision: includes Appendix A (rule-based spec) and Appendix B (recommendations), added 2026-07-19*

---

# ภาคผนวก A — Rule-Based Decision Spec (ฉบับเต็ม)

เอกสารนี้กำหนดกฎ (rules) และค่าเริ่มต้น (default thresholds) ของทั้ง 5 roles
สำหรับใช้คู่กับ spec.md — ทุกค่าที่มี `[adjustable]` ให้เก็บใน config file ปรับได้โดยไม่แก้โค้ด

หลักการใหญ่: **ก้ำกึ่ง = ไม่เทรด (no-trade is the default)**

---

## 0. Global Definitions (ใช้ร่วมกันทุก role — ห้ามตีความต่าง)

- **Timeframe หลัก = H1** ทุก indicator (EMA, RSI, MACD, ATR) คำนวณจาก H1 **closed bars เท่านั้น**
  ไม่ใช้แท่งที่ยังไม่ปิด — signal ประเมินหนึ่งครั้งต่อหนึ่ง H1 bar close ไม่ประเมินซ้ำระหว่างแท่ง
- **Swing point (นิยามเชิงคำนวณ)** — ใช้ pivot แบบ N-bar:
  swing high = แท่งที่ high สูงกว่า high ของ 3 แท่งก่อนหน้าและ 3 แท่งถัดไป (fractal 3-3) `[adjustable]`
  swing low สมมาตรกัน — ทุกที่ที่เอกสารนี้พูดถึง swing/higher low/lower high ใช้นิยามนี้เท่านั้น
  (หมายเหตุ: swing ยืนยันได้หลังปิดแท่งที่ 3 ฝั่งขวา — โค้ดห้าม lookahead ใช้ swing ที่ยังไม่ยืนยัน)
- **เวลาและ "วัน" = MT5 server time ทั้งระบบ** — daily loss limit, daily report, cooldown, news window
  ใช้ server time หมด; economic calendar ต้องถูกแปลงเป็น server time ตอน ingest ทันที
  ห้ามมี local time (เวลาไทย) ปนในตรรกะเทรด ใช้แสดงผลใน report เท่านั้น
- **Cost model** — ทุกการคำนวณ expectancy/backtest ต้องรวม spread + commission + slippage เสมอ
  (รายละเอียดบังคับใน 5.2)

---

## 1. The Brain — AI Council (Rule-Based Version)

แต่ละ voice ให้คะแนน 0–100 จากสูตร ไม่ใช่ดุลยพินิจ

### 1.1 Bull Voice (หาเหตุผลซื้อ)

| Component | Rule | Score |
|---|---|---|
| Trend alignment | EMA20 > EMA50 > EMA200 บน H1 | +30 |
| | EMA20 > EMA50 เท่านั้น | +15 |
| Momentum | RSI(14) อยู่ระหว่าง 50–70 (มีแรงแต่ยังไม่ overbought) | +20 |
| | MACD histogram เป็นบวกและกำลังขยาย | +15 |
| Market structure | ราคาทำ higher low ล่าสุด และยืนเหนือ swing low ก่อนหน้า | +20 |
| Confluence | ราคาอยู่ใกล้ (≤0.5×ATR) แนวรับสำคัญ (daily pivot, round number) | +15 |

**Bull signal ผ่านเมื่อ score ≥ 70** `[adjustable]`

### 1.2 Bear Voice (หาเหตุผลขาย)

สมมาตรกับ Bull ทุกข้อ (EMA stack กลับด้าน, RSI 30–50, lower high, แนวต้าน)

**Bear signal ผ่านเมื่อ score ≥ 70** `[adjustable]`

### 1.3 Decision Matrix

| เงื่อนไข | ผลลัพธ์ |
|---|---|
| Bull ≥ 70 และ Bear < 40 | เสนอ BUY ไป Shield |
| Bear ≥ 70 และ Bull < 40 | เสนอ SELL ไป Shield |
| ทั้งคู่ ≥ 55 (ขัดแย้งกัน) | NO TRADE — log ว่า "conflicting signals" |
| ไม่มีฝั่งไหนถึง 70 | NO TRADE — log ว่า "no conviction" |

> จุดที่เดิมออกแบบให้ "escalate to Claude" (คะแนน 60–70 หรือขัดแย้ง)
> ใน rule-based ให้ตีเป็น NO TRADE เสมอ และ log แยก tag `borderline`
> **โดยต้อง log เป็น hypothetical order เต็มรูปแบบ**: ทิศทาง, entry, SL, TP,
> spread ณ ขณะนั้น, และคะแนนทั้งสาม voice — เพื่อให้ Auditor simulate ผลได้จริงใน 5.4
> (log แค่คะแนนอย่างเดียวจะ replay ไม่ได้)

### 1.4 Order Construction — Entry / SL / TP (คำนวณทันทีเมื่อ signal ผ่าน 1.3)

- **Entry** = market order ที่ราคาปัจจุบันหลัง H1 bar close ที่เกิด signal
- **Stop-loss (BUY)** = ใต้ swing low ยืนยันล่าสุด − buffer 0.2×ATR(14) `[adjustable]`
  แต่ไม่แคบกว่า 0.8×ATR (กันโดน noise stop) และไม่กว้างกว่า 2.5×ATR (เพดานของ Risk voice)
  — SELL สมมาตรกัน (เหนือ swing high + buffer)
- **Take-profit** = entry ± 2.0 × stop_distance (คือ TP ตายตัวที่ 2R) `[adjustable]`
  เหตุผล: เรียบง่าย backtest ได้ตรง และทำให้ R:R ก่อนเข้า = 2.0 ≥ เกณฑ์ Shield (1.5) เสมอ
  ทางเลือกอนาคต: TP อิง structure (swing ถัดไป) — ถ้าเปลี่ยน ต้อง re-backtest ใหม่ทั้งชุด
- ทั้งสามค่านี้ fix ตั้งแต่ก่อนส่งเข้า Risk voice → Shield → CFO
  (Watchman ปรับ SL ได้ภายหลังตามกฎ trail แต่ห้ามขยับ SL ให้กว้างขึ้นเด็ดขาด)

### 1.5 Risk Voice (มีสิทธิ์ veto — เช็คก่อนส่งต่อทุกครั้ง)

Veto ทันทีถ้าข้อใดข้อหนึ่งจริง:

- Spread ปัจจุบัน > 1.5× spread เฉลี่ย 20 วัน หรือ > 35 points (XAUUSD) `[adjustable]`
- ข่าว high-impact ของสกุลเงินที่เกี่ยวข้อง ภายใน −45 ถึง +30 นาที `[adjustable]`
  (ดึงจาก economic calendar อัปเดตทุกเช้า; ถ้าดึง calendar ไม่ได้ = ถือว่ามีข่าว → veto)
- Stop-loss ที่คำนวณได้ > 2.5×ATR(14) `[adjustable]`
- อยู่นอก session ที่กำหนด: อนุญาตเฉพาะ London + New York overlap ก่อน แล้วขยายทีหลัง `[adjustable]`
- วันศุกร์หลัง 20:00 (server time) — ไม่เปิดไม้ใหม่ก่อนปิดตลาด
- ATR(14) ปัจจุบัน > 3× ค่าเฉลี่ย ATR 20 วัน (ตลาดผิดปกติ/panic)

> **Re-check ตอนยิง order:** เงื่อนไข spread และ news window ต้องเช็คซ้ำอีกครั้ง
> ณ วินาทีก่อนส่ง order จริง (ไม่ใช่แค่ตอนประเมิน signal) — ถ้าไม่ผ่าน = ยกเลิกไม้นั้น log `stale_signal`

---

## 2. The Shield — Portfolio Checkpoint

รับ trade idea ที่ผ่าน Council แล้ว เช็คระดับพอร์ต — block พร้อม log เหตุผลถ้าข้อใดไม่ผ่าน:

1. **R:R ขั้นต่ำ** — reward:risk ≥ 1.5 จาก TP/SL ที่คำนวณจริง `[adjustable]`
2. **Correlation guard** — ไม่เปิดไม้ใหม่ถ้ามี position ทิศเดียวกันใน symbol ที่ correlation > 0.7
   (ตาราง correlation คำนวณ rolling 60 วัน อัปเดตรายวัน เช่น XAUUSD ↔ XAGUSD, EURUSD ↔ GBPUSD)
3. **Max exposure ต่อ symbol** — สูงสุด 1 position ต่อ symbol `[adjustable: เพิ่มเป็น 2 ได้หลัง live 3 เดือน]`
4. **Max exposure รวม** — สูงสุด 3 positions พร้อมกันทั้งพอร์ต `[adjustable]`
5. **Total risk ceiling** — ผลรวม risk ของทุก position ที่เปิดอยู่ + ไม้ใหม่ ≤ 3% ของ equity
6. **Duplicate signal cooldown** — symbol เดิม ทิศเดิม ต้องห่างจากไม้ก่อนหน้า ≥ 4 ชั่วโมง
   หรือมี swing point ใหม่เกิดขึ้นแล้ว `[adjustable]`

---

## 3. The CFO — Money Manager

คำนวณ position size สำหรับไม้ที่ผ่าน Shield:

### 3.1 Position Sizing

```
risk_amount   = equity × risk_per_trade
stop_distance = entry − stop_loss (in points)
lot_size      = risk_amount / (stop_distance × point_value)
```

- `risk_per_trade` = **0.5%** ช่วง live ramp เริ่มต้น → **1.0%** เมื่อผ่านเกณฑ์ Auditor `[adjustable]`
- ปัดเศษ lot **ลง** เสมอ (ไม่ปัดขึ้น)
- ถ้า lot ที่ได้ < min lot ของโบรก = ไม่เทรด (อย่าฝืนเสี่ยงเกินแผน)

### 3.2 Volatility Adjustment

- ถ้า ATR(14) วันนี้ > 1.5× ATR เฉลี่ย 20 วัน → ลด risk_per_trade ลงครึ่งหนึ่งอัตโนมัติ
- หมายเหตุ (ตั้งใจ): sizing ใน 3.1 ลดขนาด lot ตาม volatility อยู่แล้วผ่าน stop ที่กว้างขึ้น
  ข้อนี้เป็น dampening ชั้นที่สอง **โดยเจตนา** สำหรับช่วงตลาดร้อนแรง — ไม่ใช่ bug ห้าม "แก้ให้ถูก"

### 3.3 Circuit Breakers (เรียงจากเบาไปหนัก)

| Trigger | Action |
|---|---|
| ขาดทุนสะสมวันนี้ ≥ 2% ของ equity `[adjustable]` | หยุดเปิดไม้ใหม่จนถึงวันถัดไป (ไม้เดิมให้ Watchman จัดการต่อ) |
| ("ขาดทุนวันนี้" = realized P&L ของไม้ที่ปิดใน server day นี้ + floating loss ปัจจุบัน; "วันถัดไป" = server day ใหม่ตาม Section 0) | |
| แพ้ติดกัน 3 ไม้ | หยุด 24 ชั่วโมง |
| Drawdown จาก equity peak ≥ 8% `[adjustable]` | หยุดระบบ + แจ้งเตือน ต้อง manual restart เท่านั้น |
| Equity ต่ำกว่าจุดเริ่ม live ≥ 15% | ระบบ downgrade ตัวเองกลับ paper trading อัตโนมัติ |

---

## 4. The Watchman — Position Monitor

จัดการไม้ที่เปิดอยู่ ทุกๆ tick/bar close:

1. **Hard stop ฝั่งโบรก** — ทุกไม้ต้องมี SL/TP ตั้งที่โบรกตั้งแต่เปิดไม้ (ไม่พึ่งระบบเราอย่างเดียว)
2. **Break-even move** — เมื่อกำไรถึง 1×R ย้าย SL ไป entry + spread `[adjustable]`
3. **Trailing stop** — เมื่อกำไรเกิน 1.5×R เริ่ม trail ด้วยระยะ 1×ATR(14) จาก high/low ล่าสุด `[adjustable]`
4. **Structure invalidation exit** — ปิดไม้ทันทีถ้าเงื่อนไขที่ใช้เข้าพังก่อนโดน SL:
   - ไม้ BUY: ราคาปิด H1 ต่ำกว่า swing low ที่ใช้อ้างอิงตอนเข้า
   - ไม้ SELL: ราคาปิด H1 สูงกว่า swing high ที่ใช้อ้างอิง
5. **News protection** — ถ้ามีข่าว high-impact จะมาถึงใน 30 นาที และไม้กำไรอยู่ ≥ 0.5×R → ปิดครึ่งหนึ่ง + ย้าย SL เป็น break-even `[adjustable: หรือปิดทั้งไม้]`
6. **Time stop** — ไม้ที่เปิดเกิน 48 ชั่วโมงและกำไร/ขาดทุนอยู่ระหว่าง ±0.3R → ปิดทิ้ง (dead trade) `[adjustable]`
7. **Connectivity watchdog** — ถ้าระบบขาดการเชื่อมต่อ MT5 เกิน 5 นาที → แจ้งเตือนทันที (ไม้ยังปลอดภัยเพราะมี SL ฝั่งโบรก)
8. **Execution error handling** (ใช้ทั้งตอนเปิดไม้และ modify):
   - Order ถูก reject (requote, off-quotes) → retry สูงสุด 2 ครั้ง ห่างกัน 3 วินาที `[adjustable]`
     ถ้ายัง fail = ยกเลิกไม้ log `execution_failed` — **ห้าม** chase ราคาด้วย market order ซ้ำๆ
   - Modify SL ไม่ผ่านเพราะติด broker stop level (SL ใกล้ราคาเกินไป) → ตั้ง SL ที่ระยะ
     ใกล้สุดที่โบรกยอมรับ (`SYMBOL_TRADE_STOPS_LEVEL`) แล้ว log ว่าเบี่ยงจากแผนเท่าไหร่
   - Partial fill → ยอมรับส่วนที่ fill แล้ว recalculate risk จริงของไม้ ไม่ยิงเพิ่มให้ครบ lot เดิม
   - Slippage ตอนเข้าเกิน 0.3×ATR จาก entry ที่ตั้งใจ → log `abnormal_slippage`
     และถ้า R:R หลัง slippage ต่ำกว่า 1.3 → ปิดไม้ทิ้งทันที `[adjustable]`

---

## 5. The Auditor — Performance Reviewer

### 5.1 Daily Report (อัตโนมัติทุกสิ้นวัน)

- จำนวนไม้ / win / loss / net P&L / ค่าเฉลี่ย R ที่ได้จริง
- จำนวน signal ที่ถูก block แยกตามเหตุผล (Risk veto, Shield block, borderline no-trade)
- Slippage จริง vs ที่คาด, spread เฉลี่ยตอนเข้าไม้
- เหตุการณ์ผิดปกติ: reconnect, order reject, circuit breaker triggers

### 5.2 เกณฑ์เลื่อนขั้น (Promotion Gates)

| จาก → ไป | เกณฑ์ (ต้องผ่านทุกข้อ) |
|---|---|
| Backtest → Paper | Out-of-sample: profit factor ≥ 1.3, max DD ≤ 15%, ≥ 200 trades ใน backtest, ผลไม่พึ่งไม้ top-5 (ตัด 5 ไม้ที่กำไรสูงสุดออกแล้วยังกำไร) **และ backtest ต้องรวม cost ครบ: spread เฉลี่ยจริงของโบรก + commission + slippage สมมติขั้นต่ำ 1 spread — backtest ที่ไม่มี cost model = ไม่นับ** |
| Paper → Live ramp | ≥ 100 trades **หรือ** ≥ 16 สัปดาห์ paper (แล้วแต่อะไรถึงก่อน แต่ขั้นต่ำ 8 สัปดาห์เสมอ), profit factor ≥ 1.2, max DD ≤ 12%, ผล paper ไม่ต่างจาก backtest เกิน 30% (win rate & avg R) — ระบบ conservative แบบนี้เทรดไม่บ่อย ถ้าได้ <100 ไม้ใน 16 สัปดาห์ ให้ยอมรับ sample เล็กแต่เพิ่มความเข้มช่วง live ramp แทน (คง risk 0.25% นานขึ้น) |
| Live ramp → Full size | ≥ 3 เดือน live ที่ 0.25–0.5% risk, profit factor ≥ 1.2, ไม่มี circuit breaker ระดับหนักถูก trigger, slippage จริงไม่ทำให้ expectancy ติดลบ |

### 5.3 เกณฑ์ถอดถอน (Demotion Rules)

- Live strategy ขาดทุน 2 เดือนติดต่อกัน → กลับไป paper
- Profit factor rolling 60 วัน < 1.0 → กลับไป paper
- พฤติกรรมจริงเบี่ยงจาก backtest ชัดเจน (win rate ต่างเกิน 15 percentage points ที่ ≥ 50 trades) → หยุดสอบสวนก่อน

### 5.4 Borderline Tracking (สำหรับตัดสินใจเรื่อง AI ภายหลัง)

ทุกเคสที่ Council log เป็น `borderline` (ซึ่งมี hypothetical order เต็มรูปแบบตาม 1.3)
ให้ Auditor replay กับราคาจริงย้อนหลัง: ไม้สมมตินั้นโดน SL, ถึง TP, หรือโดน time stop —
คิด cost (spread ณ ตอน log + commission) ด้วยเสมอ

หลัง 3 เดือน: ถ้า borderline cases มี expectancy เป็นบวกชัดเจน (เช่น ≥ +0.2R เฉลี่ย
ที่ ≥ 30 เคส) = ค่อยพิจารณาเพิ่ม AI escalation — ตัดสินจากข้อมูลจริง ไม่ใช่ความรู้สึก

---

## 6. Config Structure (แนวทาง)

ทุก threshold รวมไว้ที่เดียว เช่น `config/rules.yaml`:

```yaml
global:
  timeframe: H1
  swing_pivot_bars: 3        # fractal N-N
  timezone: server           # MT5 server time ทั้งระบบ
council:
  bull_threshold: 70
  bear_threshold: 70
  conflict_threshold: 55
order:
  sl_buffer_atr: 0.2
  sl_min_atr: 0.8
  sl_max_atr: 2.5
  tp_r_multiple: 2.0
  max_retries: 2
  retry_delay_sec: 3
  max_entry_slippage_atr: 0.3
risk_voice:
  max_spread_points: 35
  news_blackout_before_min: 45
  news_blackout_after_min: 30
  max_stop_atr_multiple: 2.5
shield:
  min_rr: 1.5
  max_correlation: 0.7
  max_positions_total: 3
  total_risk_ceiling_pct: 3.0
cfo:
  risk_per_trade_pct: 0.5
  daily_loss_limit_pct: 2.0
  max_consecutive_losses: 3
  max_drawdown_halt_pct: 8.0
watchman:
  breakeven_at_r: 1.0
  trail_start_r: 1.5
  trail_distance_atr: 1.0
  time_stop_hours: 48
```

หมายเหตุ: ค่า default ทั้งหมดเป็นจุดเริ่มที่ conservative — ห้ามจูนหลายค่าพร้อมกันตอน optimize
(เปลี่ยนทีละตัว แล้ววัดผล out-of-sample เสมอ เพื่อกัน overfitting)

---

# ภาคผนวก B — ข้อแนะนำเพิ่มเติมต่อ Decision Brief

ข้อเสนอแนะต่อเนื้อหาในเอกสารหลัก ก่อนใช้ greenlight โครงการ:

## B.1 ปรับค่าใช้จ่าย AI ในตาราง Cost Breakdown เป็น $0 เริ่มต้น

ตามการตัดสินใจล่าสุด ระบบเริ่มด้วย rule-based ล้วน (ไม่มี AI API) และใช้กลไก
Borderline Tracking (ภาคผนวก A ข้อ 5.4) เก็บข้อมูลจริง 3 เดือนก่อนตัดสินใจ —
บรรทัด "AI (Claude API) $5–30/month" ในตารางควรแก้เป็น **$0 (rule-based, ตัดสินใจใหม่หลังมีข้อมูล)**
ทำให้ total monthly infrastructure ลดเหลือ **~$10–30/month**

## B.2 เพิ่มกรอบเวลารวมที่สมจริง (Timeline Expectation)

เอกสารหลักบอกลำดับ Stage แต่ไม่บอกระยะเวลา ผู้อนุมัติควรเห็นภาพรวม:

- Stage 1–2 (Foundation + Proof of Concept): ~1–2 เดือน
- Stage 3 (Backtest engine + 5 roles): ~2–3 เดือน
- Stage 4 (Paper trading): **8–16 สัปดาห์ขั้นต่ำ** ตาม promotion gate — ข้ามไม่ได้
- Stage 5 (Live ramp ที่ 0.25–0.5% risk): **อย่างน้อย 3 เดือน** ก่อน full size

รวมแล้ว **ประมาณ 9–14 เดือนก่อนถึง full size** — ถ้าใครคาดหวังกำไรจริงจังใน 3 เดือนแรก
เอกสารนี้ควรแก้ความคาดหวังนั้นตั้งแต่วันอนุมัติ

## B.3 ระบุเงินทุนขั้นต่ำที่ทำให้กฎ sizing ทำงานได้จริง

กฎ CFO เสี่ยง 0.5% ต่อไม้ และ "lot ต่ำกว่า min lot = ไม่เทรด" มีผลข้างเคียง:
ถ้าทุนน้อยเกินไป ระบบจะไม่มีวันเปิดไม้ได้เลย ตัวอย่าง XAUUSD (min lot 0.01 = 1 oz):
stop ทั่วไปบน H1 อยู่ราว $8–20 ต่อ 0.01 lot → ที่ risk 0.5% ต้องมี equity อย่างน้อย
**~$1,600–4,000** แนะนำระบุในเอกสารว่า **ทุนขั้นต่ำที่ใช้งานได้จริง ≈ $2,500–3,000
(ประมาณ 85,000–100,000 บาท)** และควรเป็นเงินที่ยอมรับการขาดทุนได้ทั้งจำนวน

## B.4 เพิ่มหัวข้อ "บทบาทมนุษย์ระหว่างระบบรัน" (Operational Runbook)

เอกสารเน้นว่าระบบอัตโนมัติ แต่ยังต้องมีคนรับผิดชอบ 3 เรื่อง ซึ่งควรเขียนให้ชัด:

1. **ใครรับ alert และต้องตอบสนองภายในเท่าไหร่** — เช่น connectivity ขาด, circuit breaker,
   abnormal slippage → แจ้งผ่าน Telegram/LINE ควรกำหนด response time เช่น ภายใน 4 ชั่วโมง
2. **Manual restart procedure** — circuit breaker ระดับหนัก (drawdown ≥ 8%) ตั้งใจให้
   ต้อง restart ด้วยมือ ควรมี checklist ว่าต้องตรวจอะไรก่อนกดรันใหม่ (ห้ามรีสตาร์ทเพราะ "อยากเทรดต่อ")
3. **Kill switch** — วิธีหยุดระบบ + ปิดทุก position ได้ใน 1 คำสั่ง ทดสอบจริงตั้งแต่ Stage 2

## B.5 เพิ่มแผนกู้คืนเมื่อ VPS ล่ม (Disaster Recovery)

ระบุใน brief ว่า: ทุก position มี SL/TP ฝั่งโบรกเสมอ ดังนั้น VPS ล่ม = ไม้เดิมยังปลอดภัย
แต่ควรกำหนดเพิ่ม: (1) uptime monitor ภายนอกแจ้งเตือนเมื่อ VPS เงียบเกิน 10 นาที
(2) config + state ทั้งหมด backup อัตโนมัติรายวันออกนอก VPS
(3) ขั้นตอน rebuild VPS ใหม่ต้องเสร็จได้ภายใน 1 ชั่วโมงจาก backup

## B.6 ตั้งความคาดหวังผลตอบแทนให้ตรงความจริง

หัวข้อ Risk Acknowledgment ครอบคลุมความเสี่ยงแล้ว แต่ยังไม่ได้พูดเรื่องความคาดหวัง:
ระบบเทรดรายย่อยส่วนใหญ่ไม่รอดปีแรก — ควรเขียนตรงๆ ว่า **เป้าหมายปีแรกคือ
"ระบบอยู่รอดโดย drawdown อยู่ในกรอบ และพิสูจน์ edge ได้ด้วยข้อมูลจริง"
ไม่ใช่ตัวเลขกำไร** การนิยามความสำเร็จแบบนี้ป้องกันการกดดันให้เร่ง ramp เร็วเกินแผน
ซึ่งเป็นสาเหตุพังอันดับต้นๆ ของระบบเทรดอัตโนมัติ
