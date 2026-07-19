# AI Professional Trading Team — Technical Specification

Gold (XAUUSD) & Forex Automated Trading System

Status: Design spec, pre-implementation. Source plan: `requirement-analyst-forex-eager-sketch.md`.

---

## 1. Purpose & Concept

A personal automated trading system that replaces a human trading team with 5 cooperating roles, running 24/7:

1. **The Brain** (AI Council & Strategy Lab) — 3 personas (bull, bear, risk-manager) score every candidate setup. A trade only proceeds on a net directional signal past threshold, with the risk-manager persona acting as a veto gate. Fed by CSM (Currency Strength Meter), SMC (Smart Money Concept) structure, and news/economic-calendar data.
2. **The Shield** (Portfolio Orchestrator) — filters approved ideas against portfolio-level risk: risk-reward ratio (RRR) minimum, correlated/duplicate exposure, directional-bias consistency.
3. **The CFO** (Money Manager & Risk Manager) — computes position size from account equity, per-trade risk %, and current volatility (ATR-based); enforces the daily-loss circuit breaker.
4. **The Watchman** (Active Manager) — manages open positions: trailing stop behind confirmed market structure, early exit on invalidation. Operates alongside a mandatory broker-side hard stop-loss, never as the sole protection.
5. **The Auditor** (Fund Manager & Trade Autopsy) — daily trade-autopsy reporting and the gatekeeper for the strategy promotion pipeline: **backtest → sandbox/paper-trading → live**, with no stage skippable.

Design principle: the same decision code runs unmodified in backtest, sandbox, and live — only the data/execution adapter differs. This is the core safety property the whole system depends on.

---

## 2. Architecture

**Style:** modular monolith (single Python process), not microservices. Justification: this is a solo-built, single-box system where the critical property is that `council/ → shield/ → risk/ → watchman/` code is byte-identical across backtest, sandbox, and live. A single `BrokerAdapter` interface with three implementations (backtest / MT5-demo / MT5-live) makes that structural rather than a matter of discipline.

### 2.1 Modules

| Role | Module | Responsibility |
|---|---|---|
| (foundation) | `feed/` | MT5 price polling + news/economic-calendar polling → shared `MarketSnapshot` |
| (foundation) | `features/csm.py`, `features/smc.py` | Currency Strength Meter, Smart Money Concept structure |
| The Brain | `council/` | 3 scoring personas + vote aggregator + LLM escalation for borderline/high-stakes cases |
| The Shield | `shield/` | RRR check, correlated-exposure check, directional-bias check |
| The CFO | `risk/` | Equity+volatility-based lot sizing, daily-loss circuit breaker |
| (execution) | `execution/` | `BrokerAdapter` interface; MT5 / backtest / sandbox implementations |
| The Watchman | `watchman/` | Own async loop: trailing SL, early-exit triggers |
| The Auditor | `auditor/` | Daily trade-autopsy report + promotion-pipeline gatekeeper |
| (orchestration) | `orchestrator/` | asyncio main loop wiring feed→features→council→shield→risk→execution, plus the Watchman task |
| (persistence) | `store/` | SQLite (WAL mode, via SQLAlchemy) — trade journal, config versions, audit logs |
| (validation) | `backtest/`, `sandbox/` | Replay the *same* pipeline code over historical / live-demo data |

### 2.2 Data flow (one cycle)

```
new bar closes
  → feed/ updates MarketSnapshot (price + news)
  → features/ computes CSM + SMC onto the snapshot
  → council/ scores via 3 personas, nets bull-bear score against threshold,
    risk-manager persona can veto; borderline/high-stakes cases escalate to Claude
  → shield/ filters on RRR / correlated exposure / directional bias
  → risk/ sizes the position (equity, volatility, per-symbol contract specs)
    and checks the circuit breaker
  → execution/ sends the order via BrokerAdapter, with a mandatory
    broker-side hard stop-loss attached at placement time
  → position open
  → watchman/ (separate faster loop) trails the stop / triggers early exit
  → on close, auditor/ logs full rationale and produces the daily autopsy
  → any resulting strategy change must clear backtest → sandbox
    before being flagged live-eligible
```

### 2.3 Architectural invariants

- **Server-side SL is mandatory and independent of the Watchman.** Every position gets a hard, broker-server-side stop-loss at order-placement time — it survives process death, disconnects, or a VPS reboot. The Watchman only ratchets that stop tighter; it is never the sole line of defense.
- **No direct OS clock reads in decision code.** A `Clock` abstraction (real in live/sandbox, simulated in backtest) is the only source of "now" for `council/`, `shield/`, `risk/`, `watchman/`, `features/` — otherwise backtest/live parity breaks silently.
- **MT5 access is single-threaded.** The `MetaTrader5` package holds one global terminal connection and is not thread-safe. All MT5 calls route through one dedicated executor owned by `execution/`, never ad-hoc `asyncio.to_thread` calls from multiple loops.
- **Dependency direction is enforced, not just documented.** `council/`, `shield/`, `risk/`, `watchman/` only ever consume/produce plain dataclasses (`MarketSnapshot`, `TradeIdea`, `Order`) — never import anything MT5-specific. Enforced via a CI import-check (e.g. import-linter), so `backtest/`/`sandbox/` reuse them unmodified.

---

## 3. Domain Logic

The plumbing (feed, orchestrator, store) is comparatively low-risk. The actual trading edge — and the place look-ahead bias hides — is in these four pieces.

### 3.1 CSM (`features/csm.py`)

- Basket: USD, EUR, GBP, JPY, CHF, AUD, CAD, NZD.
- Currency strength = aggregate rate-of-change contribution across every pair containing it (inverted when it's the quote currency), normalized (z-score or fixed 0–100 scale, chosen in config).
- Computed on two frames (e.g. H1 context + M15 entry) — CSM is timeframe-dependent.
- Tradable signal = strength differential between a pair's base and quote past a configurable threshold.
- **Gold is not in the basket** — must explicitly decide whether XAU is treated as a synthetic "currency" derived from XAU pairs, or modeled from USD-strength + a gold-specific momentum term.
- Every reading tagged with its trading session (correlated currencies can double-count strength; thin Asian-session liquidity produces noisy readings).

### 3.2 SMC (`features/smc.py`)

A computable subset of Smart Money Concept, not the full discretionary canon — everything anchored to **closed, confirmed** bars:

- **Swing points** via fractal/pivot detection, confirmed only N bars after forming (an unconfirmed swing is a classic look-ahead trap).
- **BOS** (structure continuation) / **CHoCH** (first counter-break) — defined by a bar *close* breaking a confirmed swing.
- **FVG** (fair value gap) — a 3-candle gap, fully deterministic.
- **Order blocks** — the last opposite-color candle before the structure-breaking impulse.
- **Key liquidity** — prior-day/week high-low, Asian-session range.
- **Premium/discount** — position within the current dealing range (50% fib).

### 3.3 Council scoring (`council/`)

- Bull and Bear personas score the *same* setup, one for/one against, as directional conviction (0–1).
- **Net directional score = bull − bear**; trade only if `|net|` clears a configured threshold.
- **Risk-Manager persona is a hard veto/gate, not a third directional voter** — wide spread, high-impact news within X minutes, adverse volatility regime, thin session, proximity to a major level, infeasible RRR. It can veto but never create a trade.
- Each persona returns a structured vote: direction, score, the specific features it keyed on, rationale — logged in full.
- Deterministic weighted-feature scoring is primary; the weights are the strategy-version parameters the promotion pipeline hashes.
- LLM escalation trigger is a computable predicate (e.g. `|net|` inside a borderline band, feature conflict between CSM and SMC, or high-stakes size/news proximity) — the firing predicate is logged.

### 3.4 Watchman trailing (`watchman/`)

- Explicit modes in config: breakeven move at +1R, structure trail (SL just beyond the latest confirmed swing), ATR trail (SL = price − k·ATR, ratchet only), optional partial-TP + trail remainder.
- **Hard invariant: SL only ever moves in the favorable direction** — unit-tested.
- Invalidation exit defined precisely: counter-CHoCH forms, or the entry order block is violated by a close.
- Respects broker constraints (`TRADE_STOPS_LEVEL` / `FREEZE_LEVEL`) with bounded retry on requotes/rejects.
- Reacts tick-by-tick for hard protection; structure-trail only on closed-bar confirmed swings.

---

## 4. Tech Stack

- **Language/runtime:** Python 3.11+.
- **Broker integration:** official `MetaTrader5` package (Windows-only — drives the VPS OS choice).
- **Data/features:** pandas / numpy.
- **Backtest engine:** custom, lightweight, event-driven — not vectorbt/backtrader. It must replay through the *exact same* council/shield/risk/watchman functions the live system uses, which vectorized (vectorbt) or framework-owned (backtrader) models both work against. `vectorbt` may be added later purely for fast parameter sweeps, as a narrower secondary tool. Guardrails: decisions only read closed bars, fills happen at next-bar open with modeled spread, and the engine is validated once against a known-good tool on a trivial strategy to rule out look-ahead bias.
- **Cost model (from the first backtest version):** bid/ask spread per symbol (not mid-price), per-lot commission, swap/overnight financing (Gold swaps are typically strongly negative).
- **Symbol abstraction:** strategy code uses canonical symbol names; an adapter maps canonical→broker symbol names (which vary by broker, e.g. `XAUUSD` vs `XAUUSD.r` vs `GOLD`) and reads `SYMBOL_INFO` (digits, tick size/value, contract size, min/max/step volume, `TRADE_STOPS_LEVEL`, `FREEZE_LEVEL`).
- **Concurrency:** `asyncio` for the orchestrator loop; MT5 calls serialized through one dedicated executor. No APScheduler/Celery needed at this scale.
- **Persistence:** SQLite (WAL mode) via SQLAlchemy for the trade journal, config versions, and audit logs; migrate to Postgres only if a second process needs concurrent writes.
- **LLM layer (Claude API):** used only in `council/llm_escalation.py` (borderline/high-stakes cases) and the Auditor's daily narrative summary — low call volume. Default model: `claude-sonnet-5` (bounded, schema-constrained escalation with a cached persona rubric — Sonnet is very likely sufficient at ~5x lower cost than Opus); an Opus (`claude-opus-4-8`) high-stakes sub-tier is optional. Structured output (JSON schema/Pydantic) for every escalation response. Prompt caching on the static persona/rubric system prompt (1-hour TTL, given sparse call frequency).
- **Secrets:** `.env` (git-ignored) locally for the Anthropic API key and MT5 credentials; ACL'd `.env` or Windows Credential Manager on the VPS.
- **Version control:** Git is a hard dependency of the safety model — the strategy-version hash gating live eligibility covers the git commit SHA (+ dirty-flag), all strategy params, and policy thresholds. Live order placement refuses to run from a dirty working tree.

---

## 5. Repository Layout

```
D:\AutoTrade
├── pyproject.toml
├── .env.example
├── .gitignore
├── spec.md                      # this file
├── config/
│   ├── base.yaml / dev.yaml / sandbox.yaml / live.yaml
│   └── strategies/               # versioned strategy/threshold parameter sets
├── src/autotrade/
│   ├── feed/          features/     council/      shield/       risk/
│   ├── execution/      watchman/     auditor/      orchestrator/
│   ├── backtest/        sandbox/      store/         common/
├── scripts/
│   ├── run_live.py  run_sandbox.py  run_backtest.py  run_auditor.py
│   └── kill_switch.py            # standalone manual override
├── tests/
│   ├── unit/  integration/  fixtures/
└── data/
    ├── historical/                # downloaded MT5 history for backtesting
    └── db/                        # sqlite file(s), git-ignored
```

---

## 6. Phased MVP Roadmap

Each phase produces something runnable/testable before the next begins. A trivial rule flows end-to-end (Phase 3) *before* the full council is built (Phase 6), so plumbing is proven before "smart" logic is added. **Continuous demo shadow-running and the standalone kill-switch are pulled forward to Phase 3** rather than deferred to the end, so broker-reality (spreads, requotes, rejects, weekend gaps, disconnects) surfaces early.

| # | Phase | Verification |
|---|---|---|
| 0 | Scaffolding — Python/MT5 demo/`.env`/git repo | Script logs into MT5 demo, prints live XAUUSD ticks |
| 1 | MT5 connectivity + data ingestion, symbol abstraction | Poller logs new bar closes; historical dataset downloaded, gap/dedup-validated |
| 2 | CSM/SMC feature engine + golden-file tests | CSM table + annotated SMC structure, spot-checked |
| 3 | CFO sizing + one trivial rule, staged: (3a) NoOp adapter → (3b) throttled demo adapter with reconciliation → (3c) standalone kill-switch → (3d) continuous demo shadow-running begins | Real demo-account trade placed via the full pipeline, throttled, reconciled, independently killable |
| 4 | Backtest engine, cost model included from v1 | Backtest report (win rate, drawdown, RRR) including weekend gap bars |
| 5 | Shield (RRR/correlation/bias filtering) | Shield measurably filters trades with logged reasons |
| 6 | Rule-based Council (bull/bear + risk veto), out-of-sample backtest gate | Out-of-sample win-rate/RRR vs Phase 3 baseline |
| 7 | Watchman (trailing SL/early-exit) | Trailing measurably changes outcomes vs static SL/TP; SL-monotonicity tested |
| 8 | Auditor (daily trade-autopsy report) | Daily report generated after a demo trading day |
| 9 | Formal sandbox/paper-trading validation window (weeks) | N weeks of paper track record, no unexplained circuit-breaker trips |
| 10 | LLM council-escalation layer, offline-sanity-checked first | Escalation wired in sandbox, cost/latency logged |
| 11 | VPS deployment, monitoring, forced live ramp | Live trading unattended, alerting working, verified reduced-size ramp period |

---

## 7. Safety Gates

Non-negotiable, built in from Phase 3 onward:

- **Circuit breaker / kill-switch** — `risk/circuit_breaker.py` blocks new orders once daily loss hits a configured limit, reset at broker-server midnight (not local midnight). `scripts/kill_switch.py` is a standalone manual override, independent of the main loop's responsiveness.
- **Backtest gate — out-of-sample required** — thresholds (win-rate, RRR, sample size) evaluated on held-out/walk-forward data, not the tuning data.
- **Sandbox gate** — minimum calendar duration + trade count before live-eligible.
- **Promotion is not self-approval** — the promotion command recomputes the gate from the immutable trade log at flip time and refuses if thresholds aren't met; the human action is a required second factor, never a bypass.
- **Thresholds are version-hashed and change-audited** — policy thresholds live in a separately-audited config from strategy params; changing them requires the same promotion ceremony.
- **Config hash covers code, not just params** — git commit SHA (+ dirty-flag) + strategy params + policy thresholds; any change invalidates live-eligibility.
- **Live-eligible flag references its exact evidence** — the specific sandbox trade IDs that satisfied the gate are stored alongside the flag.
- **Structural enforcement** — the live adapter refuses to execute any order whose strategy-version isn't marked live-eligible in `store/`.
- **Forced live ramp** — first N live trades capped at reduced size (e.g. 0.25% risk) before full size unlocks.
- **Full audit logging** — every decision point (persona scores+rationale, Shield accept/reject+reason, CFO sizing inputs, every Watchman SL move, full LLM escalation prompt/response) written append-only.
- **Fail-safe defaults** — unhandled exceptions stop new trade entry, leave existing stops alone, alert; never silently continue, never panic-close everything.
- **Stale-data / disconnect handling** — halt new entries on stale feed or MT5 disconnect; the server-side SL (not the Watchman) protects existing positions during any connectivity gap.
- **Execution retcode handling** — explicit handling of requotes, rejects, partial fills, invalid-stops; paired with fill reconciliation.
- **Weekend gap policy** — explicit policy on new entries near Friday close / holding through the gap; the backtest includes gap bars so this risk is visible pre-live.

---

## 8. Cost Breakdown

| Item | Estimated cost | Notes |
|---|---|---|
| MT5 terminal + Python package | Free | Provided by the broker |
| Broker trading costs (spread, commission, swap) | Variable, per-trade | Cost of trading itself, not infrastructure; factored into the backtest cost model |
| Windows VPS (forex-oriented, low-latency) | ~$10–30/month entry/mid-tier (premium low-latency/HFT-grade VPS runs $60–190/month but is unnecessary for this system's per-bar decision cadence) | Only needed from Phase 11 onward |
| Claude API (council escalation + daily summary) | ~$5–30/month at MVP call volume | Sonnet 5: $2/$10 per MTok input/output (intro pricing through 2026-08-31, then $3/$15). Active from Phase 10 onward |
| Economic calendar / news data | Free tier viable to start | Paid low-latency real-time news feed is a later, separately-justified upgrade |
| Alerting (Telegram/Discord/email) | Free | Standard bot/webhook integrations |

### 8.1 LLM provider alternatives to Claude API

The council-escalation call and the Auditor's daily summary are isolated behind one module (`council/llm_escalation.py`) — swapping providers is a contained change, not an architecture change. Options, cheapest to most capable, at this system's low call volume (a handful to a few dozen escalations/day plus one daily summary):

| Option | Est. monthly cost | Notes |
|---|---|---|
| **No LLM — rule-based council only** | **$0** | Skip Phase 10 entirely; the deterministic council (§3.3) is already the primary decision-maker and fully functional on its own. Recommended default until there's a concrete reason to add LLM judgment. |
| Open-source model via a fast host (Groq, DeepInfra, Together.ai — e.g. Llama 3.3 70B, Qwen3 32B) | **~$1–5/month** | Cheapest paid option ($0.05–$0.90 per million tokens depending on model); lower reasoning quality than frontier models — worth a quick offline eval (§ Phase 10) before trusting it on real trades. |
| Google Gemini API (Flash tier) | **~$2–10/month** | ~$1.50/$9 per million tokens (input/output); Flash/Flash-Lite retain a free tier with reduced quotas, which may cover this system's low volume entirely. |
| OpenAI API (GPT-4o-mini or GPT-5-tier) | **~$2–15/month** | GPT-4o-mini is very cheap (~$0.15/$0.60 per MTok) but a lighter-weight model; a GPT-5-tier model (~$1.25/$10 per MTok) is closer to Claude Sonnet in capability at this volume. |
| Claude API (Sonnet 5, plan default) | **~$5–30/month** | See main cost table above — the default recommendation, on the reasoning that a "should I risk money on this" judgment call favors a stronger model over marginal savings at this call volume. |
| Self-hosted open-source model (e.g. Ollama on the VPS) | **$0 marginal, but raises VPS cost** | No per-token fee, but running a capable model needs more RAM/CPU (or a GPU) than the entry-tier VPS in the main cost table — likely pushes the VPS line item up, plus added latency/ops complexity. Not recommended for a first build. |

**Recommendation:** start with **no LLM** (pure rule-based council) through Phases 0–9, and only add Phase 10's LLM layer once the rule-based system has a live track record — at that point, re-evaluate cost vs. quality across these options with real escalation-frequency data rather than the estimate above.

**Rough all-in estimate once live (Phase 11): ~$15–60/month**, dominated by VPS choice — excludes trading capital and trading losses, which are a risk decision, not an infrastructure cost.

---

## 9. Open Decisions

Deferred, not blocking the design — revisit before the relevant phase:

1. **News data source/API** (Phase 2/6) — start with a free/cheap economic calendar (NFP, CPI, FOMC); a true low-latency real-time news feed is a later addition if justified.
2. **Specific broker/account details** (Phase 3) — affects CFO sizing math, Shield RRR assumptions, symbol-suffix mapping.
3. **LLM API budget ceiling** (Phase 10) — determines Sonnet-only vs an Opus high-stakes sub-tier.
4. **VPS provider** (Phase 11) — broker-affiliated vs general cloud vs boutique, ideally chosen once the broker is known.
5. **Correlation matrix source for Shield** (Phase 5) — start static/config-driven.
6. **Sandbox thresholds** (Phase 9) — specific win-rate/duration/trade-count numbers, kept as config.
7. **Circuit-breaker behavior on breach** (Phase 11) — halt-new-entries-only vs force-flatten-all-positions.
8. **Circuit-breaker reset boundary** (Phase 3) — confirm broker server timezone for the daily-loss reset point.
9. **Weekend policy** (Phase 4) — flat-before-weekend vs hold-through-gap-with-wider-stops.
10. **Out-of-sample method** (Phase 6) — walk-forward vs a fixed hold-out split.

---

## 10. Verification Summary

Before any live capital is committed:

1. Backtest report meets configured win-rate/RRR/drawdown/sample-size thresholds, on out-of-sample data (Phase 4/6).
2. Sandbox run (live demo prices, real-time, weeks-long) meets configured duration/trade-count thresholds with no unexplained circuit-breaker trips (Phase 9).
3. Kill-switch tested independently, including against a simulated hung orchestrator.
4. Manual review of a sample of Auditor daily reports and LLM escalation logs for sane rationale before flipping the live-eligible flag.
