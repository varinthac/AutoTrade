# Automated Gold & Forex Trading System
## Decision Brief for Non-Technical Stakeholders

---

## What This Is

This project builds a **24/7 automated trading system for Gold and Forex** that replicates how a professional trading team operates — five specialists working together on every single trade decision before any money is committed to a trade.

Instead of one "black-box" algorithm making isolated decisions, the system enforces debate and checks at every step:

**The Five-Person Team:**

1. **The Brain (AI Council)** — Three analytical voices score each trading opportunity: a bull (looks for reasons to buy), a bear (looks for reasons to sell), and a risk-management voice (watches for danger signs like too-wide stop-losses, approaching economic news, or market turmoil). A trade only proceeds if it passes a majority vote and passes the risk-manager's veto gate. When a decision is borderline or high-stakes, the system escalates to Claude AI for a human-level sanity check before acting.

2. **The Shield (Portfolio Checkpoint)** — Before the Council's idea becomes an order, this checkpoint asks: is this trade good enough on its own (win/loss ratio)? Would it duplicate risk we already have? Does it fit the current portfolio direction? If not, it blocks the trade with a logged reason.

3. **The CFO (Money Manager)** — Given an approved trade idea, this role calculates the right position size: How much of the account should we risk? Adjust for current market volatility. Size the position so even a losing trade doesn't blow up our capital. It also enforces a daily loss limit — if we've had a bad day, no more new trades until tomorrow.

4. **The Watchman (Position Monitor)** — Once a trade is open, this role actively manages it: trails the stop-loss to lock in gains, exits early if the market structure that justified the trade breaks, protects profits once they form. It works alongside a broker-side stop-loss (a hard safety net independent of our system).

5. **The Auditor (Performance Reviewer)** — Each day, reviews what happened: which trades worked, which didn't, why, what we learned. Most importantly: decides which strategies have proven themselves enough to risk real money. Nothing moves from testing to live trading without the Auditor's sign-off.

**Core Safety Philosophy:**  
Nothing goes live with real capital until it has *proven itself three times over*:
- Passes historical simulation (backtesting) on data it wasn't trained on
- Runs weeks in real-time paper trading (live market conditions, zero real money at risk)
- Starts with tiny real trades and ramps up gradually — never a sudden jump to full position size

---

## How It Gets Built

This is not built all at once. Instead, it's built in **five plain-language stages**, each proven to work before the next begins:

### Stage 1: Foundation & Data (Phases 0–1)
Set up the basic plumbing: get Python and the trading platform talking, log into real-time market data, download historical price data for backtesting.  
*Proof point: The system can see and record live market data.*

### Stage 2: Proof of Concept — Tiny Test Trades (Phases 2–3)
Implement one very simple trading rule and run it in shadow-mode on the broker's demo account (no real money, no risk). The full pipeline — idea generation, risk checks, position sizing, order placement — runs end-to-end, but trades are throttled and logged for manual verification.  
*Proof point: The system can place trades, broker infrastructure works, nothing crashes.*

### Stage 3: Add Intelligence & Optimization (Phases 4–8)
Build the backtest engine so we can test strategies on years of historical data. Implement the full five-person team: Council (multi-voice debate), Shield (portfolio-level checks), CFO (intelligent sizing), Watchman (trailing stops). Each piece is tested independently and together.  
*Proof point: Strategies demonstrably profit in historical simulation without overfitting.*

### Stage 4: Extended Real-Time Testing with No Real Money (Phase 9)
Run the full system in paper-trading mode for weeks. Live market data, real conditions, but every trade is simulated — no capital at risk. The Auditor produces daily reports. We validate that the strategy holds up in reality.  
*Proof point: The system performs as expected when facing real market conditions, surprises are understood.*

### Stage 5: Careful Rollout to Real Trading (Phases 10–11)
Deploy to a VPS (a small cloud server running 24/7). First trades are tiny (0.25% of account risk per trade). Gradually increase to full size only after live performance confirms the strategy works with real money.  
*Proof point: Real trading works, monitoring/alerts work, strategy survives first contact with the live market.*

**Why this approach works:**  
Each stage catches errors before they become expensive. If the backtest looks good but paper trading reveals a flaw, we fix it without touching real capital. Most problems surface in stages 2–4, not after months of build time.

---

## Cost Breakdown

All costs are for infrastructure — the tools and cloud services the system runs on. This is *separate* from trading capital (the money being traded with) and trading losses (the risk of trading itself).

| Item | Cost | Notes |
|---|---|---|
| Trading platform (MT5) | Free | Provided by broker |
| Python & development | Free | Open-source |
| Cloud server (VPS) | $10–30/month | Entry to mid-tier; premium servers ($60–190/month) aren't needed for our decision frequency |
| AI (Claude API) | $5–30/month | Used for trade debate escalation and daily performance summaries; starts Phase 10 |
| Market data & news | Free | Free sources initially; premium feeds optional later |
| Monitoring alerts | Free | Standard integrations (email, Telegram, Discord) |
| **Total monthly infrastructure** | **~$15–60/month** | Once fully live (Phase 5) |

**Important distinction:**
- Infrastructure cost (~$15–60/month) is the operational overhead, separate from trading costs.
- Trading capital is the money being traded with — that's a separate decision and risk.
- Trading losses are the money lost from bad trades — that's the risk of the strategy, managed by daily loss limits, forced ramp, and the Auditor's gating.

---

## AI Provider Options (Instead of Claude)

The "AI (Claude API)" line above is only used for one narrow job: double-checking borderline trade decisions and writing the daily performance summary. It is **not** required for the system to work — the five-person team's rule-based decision-making runs on its own without any AI API. Because this piece is isolated to one small part of the system, switching providers later is easy — it doesn't affect anything else that's been built.

| Option | Estimated Cost/Month | Trade-off |
|---|---|---|
| **No AI at all — team runs on rules only** | **$0** | Recommended starting point. The team's math-and-rules-based decisions are fully functional without any AI double-check. Add AI later only if there's a proven need. |
| Budget AI options (Groq, or similar low-cost providers) | ~$1–5 | Cheapest paid option. Lower judgment quality — worth testing before trusting it on real trades. |
| Google's AI (Gemini) | ~$2–10 | Often has a free usage tier that may cover this system's light, occasional use entirely. |
| OpenAI (makers of ChatGPT) | ~$2–15 | Comparable quality range to Claude, similar cost. |
| Claude (Anthropic) — current plan | ~$5–30 | The default recommendation — favors a stronger AI for "should we risk real money on this" decisions over marginal savings. |
| Run an AI model ourselves (no per-use fee) | $0 extra fee, but a bigger/pricier server | Avoids AI subscription costs but needs a more powerful (and more expensive) cloud server, plus more setup work. Not recommended for a first build. |

**Bottom line:** this is not a decision that needs to be made now. The recommended path is to build and prove the system with **no AI subscription at all** (Stages 1–4), and only decide on an AI provider once there's a real track record showing it's needed — at that point, pick based on actual usage, not an estimate.

---

## What's Still Undecided (Non-Blockers)

A handful of choices will be made as each build stage approaches. These are **not** holding up the project:

- **Which broker & account** — details on trading fees, contract sizes, specific platform rules. Chosen before Phase 3.
- **Which VPS provider** — cloud hosting choice (general-purpose vs. forex-specialized). Chosen before Phase 11.
- **News data source** — free economic calendar or paid real-time news feed. Decided as Phase 2 starts.
- **AI budget ceiling** — determines whether we use cost-optimized Claude or premium tier for edge cases. Set before Phase 10.
- **Technical thresholds** — specific win-rate requirements for "proven" strategy, daily loss limit amount, circuit-breaker behavior. These are written into configuration and can be adjusted.

None of these are architectural problems or surprises. They're choices that depend on broker selection or budget preferences.

---

## Risk Acknowledgment

**Automated trading carries real risks:**
- Strategies can fail on unseen market conditions despite backtesting.
- Execution errors, broker platform issues, or connectivity gaps can cause losses.
- Market events (flash crashes, gaps) can exceed even carefully-sized stop-losses.

**How this system mitigates those risks:**
- The five-person-team approach catches logical errors that a solo algorithm would miss.
- No decision is trusted without proof: backtest, paper trading, then live ramp.
- Hard broker-side stop-losses (independent of our monitoring) protect every position.
- Daily loss limits prevent "death spiral" days from compounding.
- Full audit trails make every decision reviewable — when something goes wrong, we know why.
- The Auditor controls which strategies touch real money, using objective historical criteria.

This is not "fire and forget." It's continuous automated trading with multiple layers of human-like reasoning and mechanical safety checks.

---

## Next Steps

**To greenlight:**
- Confirm budget for infrastructure (~$15–60/month).
- Confirm willingness to invest development time through Stage 2 (proof the system can place trades without crashing).
- Identify broker preference (needed by Phase 3 to finalize platform integration details).

**To proceed, this document should be reviewed alongside:**
- The technical specification (`spec.md`) for architecture details if needed.
- The verification summary (final section of spec) listing concrete proof-points before any live capital is committed.
