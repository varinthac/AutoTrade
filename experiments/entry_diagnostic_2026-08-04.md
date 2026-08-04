# Entry-side diagnostic — 2026-08-04

> **EXPLORATORY. NOT AN EXPERIMENT. MULTIPLE-COMPARISONS-UNSAFE.**
>
> This document is a *mining* pass over existing evidence and existing code. Nothing in it was
> pre-registered. Dozens of splits/buckets were computed and only the interesting ones are
> reported, which is exactly the condition under which a "finding" is most likely to be noise.
> **No lever suggested here may be adopted on the strength of this document.** Every item in the
> ranked menu (§7) must be re-stated as its own pre-registered experiment — hypothesis, range,
> deciding metric, acceptance bar, multiple-testing count — and cleared on the standing protocol
> (plateau, per-year, top-5, rule 6 sample floor, rule 7 edge inflation) before it can change a
> single value in `config/base.yaml`.
>
> **Scope: Train (y1/y2/y3) + Validation (y4) ONLY.** The Test year `2025-07-22 → 2026-07-21`
> received its one authorised measurement touch earlier today (log § *MEASUREMENT 2026-08-04 —
> HONEST TEST BASELINE*) and was **not** read, sliced, decomposed, or re-run here in any form.
> The diagnostic harness refuses any window outside `{y1, y2, y3, y4}` by construction.
>
> **Rule 8 restated:** no promotion/demotion gate, Auditor threshold or circuit-breaker limit is
> touched here, and none is proposed for change as a way to make any number look better. The
> honest Test baseline (PF 1.0845 vs the 1.30 floor) is treated as a fact about the strategy, not
> a problem with the gate.

---

## 1. What was run, and the fidelity that makes it trustworthy

Production path only. `backtest.engine.run_backtest` (which now models Risk Voice, Watchman exits,
Shield cooldown, the min-lot fallback, swap, and — in the A arm — news protection from the real
calendar), `council.decision_matrix.evaluate_council` for scoring, and `backtest.forward_walk.
simulate_order_forward` (the Phase-8b borderline-replay machinery) for the near-miss study.
EXP-022's validated fast-path memoisation shim was installed for speed. Nothing under `src/`,
`config/` or `experiments/experiments_log.md` was modified.

Context identical to EXP-022/023/024/025/026 and to today's Test measurement: $3,000 per-year
anchored equity, `min_lot_risk_cap_pct` 1.5, `risk_per_trade_pct` 1.0, all-24h session, be/trail
OFF, tp 2.0, pivot 3, complete cost model (slippage = min-1-spread, swap modelled, commission
$0.00 IC Markets Standard).

**Fidelity — checked before any number below was read.** The engine reproduces this log's own
recorded rows exactly:

| window | mode C (news OFF) | recorded | mode A@real (news ON) | recorded |
|---|---|---|---|---|
| y1 2021-22 | 266 | 266 (EXP-026 §4) | 391 | 391 |
| y2 2022-23 | 254 | 254 | 399 | 399 |
| y3 2023-24 | 233 | 233 | 358 | 358 |
| y4 VAL | 254 (PF 1.0961, +$352.60, DD 9.99%) | 254 / 1.0961 / +$352.60 / 9.9895% | 350 | 350 (PF 1.0667) |

Two arms are reported throughout because they answer different questions. **Mode C** isolates the
*entry's own* quality (exits are SL/TP/Watchman only) and is the right lens for attribution.
**Mode A@real** is what live actually runs. Note that mode A's per-record `avgR` is inflated and
not comparable to mode C's, because the engine emits a genuine partial close as its own
`ClosedTrade` record; mode A is used here for **signs and rankings**, never for levels.

### 1.1 Caveats that apply to every table below

1. **The near-miss / unconditional tables replay overlapping price paths.** Consecutive bars in the
   same signal episode produce highly correlated forward-walks, so the printed `n` overstates the
   effective sample by roughly 5× (≈2,600 clean bars/yr but only ≈540 distinct signal episodes/yr).
   Treat the printed SEs as optimistic.
2. **Forward-walk uses Appendix A §5.4's cost convention** (spread + commission, *no* slippage
   term, no Watchman, no Shield, no news protection) because a borderline case was never filled.
   Absolute levels therefore run hotter than the engine's. Only *cross-band comparisons inside the
   same convention* are meaningful.
3. **Everything is one instrument in one macro regime.** Gold rose +14.2% / +22.3% / +40.9% in
   y2/y3/y4 and fell −4.5% in y1. Any directional finding must be read against that (§6).

---

## 2. (A) Score-band, negation-margin and direction decomposition of executed trades

### 2.1 Leading score band — mode C (executed trades)

| split | band | n | win% | avgR | PF |
|---|---|---|---|---|---|
| TRAIN | 70–74 | 217 | 38.2 | +0.072 | 1.124 |
| TRAIN | 75–84 | 252 | 39.7 | +0.062 | 1.113 |
| TRAIN | 85–100 | 284 | 34.2 | −0.011 | **0.981** |
| VAL | 70–74 | 58 | 41.4 | +0.058 | 1.102 |
| VAL | 75–84 | 79 | 34.2 | −0.033 | 0.940 |
| VAL | 85–100 | 117 | 37.6 | +0.103 | **1.184** |

**The band that is worst on Train is best on Validation.** On the executed set the Council score is
not a quality gradient in any stable direction. This reproduces the 2026-07-23 scoring NOTE's §3
"hump-shaped / high conviction is weakest" observation on Train — and shows it *reverses* out of
sample, which the NOTE could not see.

### 2.2 Negation margin (the trailing voice's score, hard-capped at <40)

| split | trailing band | n | avgR | PF |
|---|---|---|---|---|
| TRAIN | 15–29 | 544 | +0.014 | 1.024 |
| TRAIN | 30–39 | 209 | +0.098 | 1.169 |
| VAL | 15–29 | 185 | +0.051 | 1.093 |
| VAL | 30–39 | 69 | +0.050 | 1.085 |

Trades where the *opposing* voice scored 30–39 (i.e. barely negated) are the better half on Train
and indistinguishable on Val. There is no evidence that a *tighter* negation requirement would help.
The looser direction is tested properly in §3.

### 2.3 Direction — the most eye-catching number in the whole diagnostic

Mode C, executed trades:

| window | BUY n / PF | SELL n / PF |
|---|---|---|
| y1 2021-22 | 137 / 0.899 | 129 / **1.186** |
| y2 2022-23 | 135 / 0.931 | 119 / 0.990 |
| y3 2023-24 | 127 / **1.485** | 106 / 1.003 |
| y4 VAL | 159 / **1.346** | 95 / **0.725** |

Mode A@real agrees on the sign in y4 (BUY 1.645 / SELL 0.811) and disagrees in y1 (BUY 1.265 /
SELL 1.608). **On the executed set, direction flips which side is better between y1 and y4.**
§6 resolves this properly — and the resolution is a warning, not a lever.

### 2.4 Exit mix (mode C, for orientation)

TRAIN: 402 stop_loss (−1.021R avg) · 230 take_profit (+1.971R) · 100 time_stop (+0.003R) ·
20 structure_invalidation (−0.766R). VAL: 132 / 77 / 36 / 8. The time-stop bucket is a coin flip
that costs nothing; structure-invalidation is a small, uniformly negative bucket (28 trades over
four years, avgR −0.69) — too small to be a lever, recorded for completeness.

---

## 3. (B) Near-miss analysis — is the gate too tight or too loose?

Every bar of Train+Val was scored, classified into its decision-matrix band, and — for every band
that produces a constructible hypothetical order — forward-walked under one identical cost
convention (Appendix A §5.4) with `time_stop_bars = 48`. This is the machinery Phase 8b built for
exactly this question, applied at scale for the first time.

### 3.1 Band census (bars) and unconditional outcome

| split | band | bars | n replayed | win% | avgR | PF |
|---|---|---|---|---|---|---|
| TRAIN | clean (admitted) | 7,879 | 7,868 | 36.9 | **+0.026** | **1.042** |
| TRAIN | near-threshold (60–69) | 3,775 | 3,771 | 35.5 | −0.014 | 0.978 |
| TRAIN | strong-but-not-negated | 1,250 | 1,250 | 33.7 | −0.041 | 0.936 |
| TRAIN | conflicting (both ≥55) | 397 | 397 | 33.8 | −0.004 | 0.994 |
| VAL | clean (admitted) | 2,745 | 2,740 | 38.4 | **+0.064** | **1.105** |
| VAL | near-threshold (60–69) | 1,180 | 1,176 | 36.1 | −0.008 | 0.987 |
| VAL | strong-but-not-negated | 392 | 392 | 40.6 | +0.141 | 1.250 |
| VAL | conflicting (both ≥55) | 149 | 149 | 37.6 | +0.099 | 1.158 |

**The admitted band is the only one that is positive on both splits.** The gate is doing real work.

### 3.2 Per-year, the two "loosen it" candidates

| window | near-threshold PF | strong-but-not-negated PF |
|---|---|---|
| y1 2021-22 | 0.891 | 0.937 |
| y2 2022-23 | 0.900 | 0.971 |
| y3 2023-24 | 1.152 | 0.902 |
| y4 VAL | 0.987 | **1.250** |

* **Lowering `bull_threshold`/`bear_threshold` below 70 is refuted.** The 60–69 population is
  net-negative on Train *and* on Validation and in 3 of 4 years. Sub-splitting it makes it worse,
  not better: 60–64 is Train PF 0.814 / Val 1.565 (n=211/80 — a sign flip on tiny samples).
* **Relaxing the hard-coded `<40` negation ceiling is refuted, and is a textbook trap.** The
  "strong-but-not-negated" band (leading ≥70, trailing 40–54) is negative in y1, y2 **and** y3 and
  positive only in y4 — the very window a candidate would be validated on. Sub-split by trailing
  score: 45–49 Train 0.995 / Val 1.205; 50–54 Train **0.881** / Val 1.296. A sweep run on
  Train→Val would have "discovered" that the *worst* Train slice is the *best* Val slice.

### 3.3 Clean signals, unconditional, by leading band — and a correction to the record

| window | 70–74 | 75–84 | 85–100 |
|---|---|---|---|
| y1 | 0.990 | 0.860 | 0.901 |
| y2 | 0.960 | 0.677 | 1.116 |
| y3 | **0.535** | 1.647 | 1.387 |
| y4 VAL | 0.955 | 0.966 | 1.192 |

Pooled, the unconditional response is monotone (Train 0.847 / 1.019 / 1.121; Val 0.955 / 0.966 /
1.192) — the *opposite* of the hump-shape the 2026-07-23 NOTE found on the executed set. **The
NOTE's hump is a selection artifact of the single-position engine, not a property of the signal.**
That is a genuine correction to the record. But it does **not** license raising the threshold: the
70–74 slice is only decisively negative in y3 (−0.333 ± 0.054); in y1/y2/y4 it is within ~0.5 SE
of zero (−0.007 / −0.025 / −0.028), and 75–84 is all over the map (0.860 / 0.677 / 1.647 / 0.966).

### 3.4 The structural finding: the gate is not what limits trade count

| window | clean signal bars | distinct signal episodes | executed trades | executed / episode |
|---|---|---|---|---|
| y1 | 2,762 | 560 | 266 | 47.5% |
| y2 | 2,610 | 530 | 254 | 47.9% |
| y3 | 2,507 | 538 | 233 | 43.3% |
| y4 VAL | 2,745 | 543 | 254 | 46.8% |

**Roughly half of all distinct Council signal episodes never become trades**, because
`max_positions_per_symbol: 1` has the slot busy or Shield's 4h duplicate cooldown is active. This
is the mechanical reason every portfolio delta in this log is reshuffling-dominated (EXP-017, 020,
021, 024 §3, 026 §4) and the reason EXP-001 found widening the session window did *not* raise trade
count. It also means **loosening the Council gate is not a route to the 200-trade floor** — the
supply is already 2× what the slot can absorb.

---

## 4. (C) Regime overlay — what separates the losing regime from the winning one

Mode C executed trades, features measured at the **signal** bar (causal; the ATR/spread percentile
ranks use a trailing 500-bar window, never forward data).

| feature | Train | Val | verdict |
|---|---|---|---|
| ATR%-of-price rank, terciles | lo 0.935 / mid 1.076 / **hi 1.277** | lo 1.102 / mid 1.239 / **hi 0.942** | **SIGN FLIP** |
| EMA200 slope (24 bars), terciles | T1 0.900 / T2 1.193 / T3 1.170 | T1 1.098 / T2 1.137 / T3 1.040 | Train effect vanishes on Val |
| price vs EMA200 (ATR units), terciles | T1 0.964 / T2 1.064 / T3 1.197 | T1 **0.610** / T2 1.234 / T3 1.284 | same direction, but see below |
| entry **agrees** with EMA200 side (signed) | True 1.088 / False 0.949 | True 1.085 / False **1.121** | **SIGN FLIP** |
| spread percentile rank | tight 1.075 / mid 1.759 / wide 1.315 | tight 0.911 / mid 1.980 / wide 3.880 | n=43 off-tight over 4 yrs — unusable |

Per-year for the ATR tercile: hi-vol PF 1.126 / 1.042 / **1.799** / 0.942. The Train "high
volatility is better" signal is a y3 artifact.

The **signed** trend-agreement row is the causal form of EXP-020's trend-regime filter, and it
reproduces EXP-020's out-of-sample failure exactly (helps on Train, flips on Val). Nothing here
reopens that question.

The one row with the same sign on both splits is *unsigned* price-vs-EMA200: when gold trades far
**below** its 200-EMA the whole book does worse (Train 0.964, Val 0.610). But Val T1 is n=56 —
below rule 6's floor — and this is not the same thing as EXP-020's directional gate. It is recorded
as an observation, not a lever.

**Spread is not analysable on this data.** The spread column over Train+Val is effectively a
constant: mean 4.96 points, 25th = median = 75th percentile = 5. Consequently:

| existing Risk-Voice entry gate | current value | bars it would fire on (23,650 Train+Val bars) |
|---|---|---|
| `max_spread_points_xauusd` | 35 | **4 bars (0.017%)** |
| `max_spread_multiple` | 1.5 | 515 bars (2.18%) |
| `max_atr_panic_multiple` | 3.0 | **21 bars (0.089%)** |
| `friday_close_hour` | 20 | 798 bars |

Two of the six Risk-Voice conditions are, on this data, effectively inert. That is a finding about
where *not* to spend effort.

---

## 5. (D) News-adjacent entries — and an unmodelled entry-side gate

### 5.1 The mechanical finding (this is the important one, and it is not a tuning result)

`backtest/engine.py` passes `backtest.news_stub.NoHistoricalNewsDataProvider` to Risk Voice, which
**always returns `[]`**. Risk Voice condition 2 — the entry blackout, `news_blackout_before_min: 45`
/ `news_blackout_after_min: 30` — therefore **never fires in any backtest this project has ever
run**, while live enforces it on every bar. `backtest/report.py`'s envelope flags this
(`risk_voice_modeled` is true but the module docstring is explicit that the news condition is the
one not genuinely modelled), and the live journal proves the gate is live and firing:
`blocked_signal_records` holds 3 blocks for *"1 high-impact USD news event(s) within -45/+30 min of
now"* plus 17 calendar-unavailable fail-safe vetoes in 13 days of paper trading.

**This is structurally the same class of error as EXP-018 (swap not charged) and EXP-023/024 (news
protection not modelled) — a real gate the backtest simply does not apply — except it sits on the
ENTRY side and it has never been measured.** The blocker that prevented measuring it no longer
exists: `data/historical/news_calendar_backtest.csv` was built this session.

### 5.2 What the unmodelled entries are worth

Unconditional clean signals, split by live's own blackout halves:

| split | bucket | n | avgR ± SE | PF |
|---|---|---|---|---|
| TRAIN | pre-event ≤45 min | 393 | −0.087 ± 0.067 | **0.867** |
| TRAIN | post-event ≤30 min | 207 | +0.008 ± 0.094 | 1.012 |
| TRAIN | outside | 7,268 | +0.033 ± 0.016 | 1.053 |
| VAL | pre-event ≤45 min | 118 | −0.116 ± 0.122 | **0.828** |
| VAL | post-event ≤30 min | 71 | −0.053 ± 0.160 | 0.918 |
| VAL | outside | 2,551 | +0.075 ± 0.027 | 1.125 |

Per-year, pre-event: 0.465 (y1) / 0.888 (y2) / **1.388** (y3) / 0.828 (y4). Executed trades agree
in direction: mode C pre-event Train PF 0.826 (n=58) / Val 0.736 (n=17); mode A@real pre-event
Train 0.819 (n=115) / Val 0.853 (n=51) against outside 1.509 / 1.363.

**Honest reading.** The sign is negative on both splits and in 3 of 4 years, and the mechanism is
not a fitted one — it is a gate live already applies. But neither pooled estimate is close to
significant (Train t ≈ −1.3, Val t ≈ −0.95), y3 flips positive, and the affected executed subsets
are **58 / 17 (mode C) and 115 / 51 (mode A) — all below rule 6's 100-trade floor except pooled
Train mode A.** Scaled to the whole book, removing pre-event entries is worth roughly **+0.008R
per trade on Train and +0.010R on Val**, at a cost of ~10–13% fewer trades. That is a modest,
honest number, not a rescue for a PF-1.08 strategy.

### 5.3 The asymmetry — and the sub-band that must NOT be chased

The harm is on the **pre-event** side. The **post-event ≤30 min** half is neutral on Train (1.012)
and mildly negative on Val (0.918) as raw signals — and under mode A@real it is the *best* bucket
in the book (Train PF 1.902, Val 1.818, n=93/24). `news_blackout_after_min: 30` may be blocking a
population the strategy handles fine.

Finer pre-event bands: 0–15m Train 0.866 / Val 0.764; 15–30m 0.958 / 1.278; **30–45m 0.558 / 0.031
(n=50 / 16)**. The 30–45m cell is the worst on both splits and it is exactly the kind of cell this
document exists to warn about: seven bands × two splits, samples of 16–50, and the "winner" is a
band with no mechanism distinguishing it from its neighbours. **Do not widen the blackout to chase
it.** It is reported so that nobody rediscovers it and treats it as a signal.

### 5.4 Interaction that any future experiment must respect

The entries blocked by the blackout are disproportionately the same trades that later trigger
Watchman's news *protection* (both are defined against the same event list, 30–45 min apart). The
two mechanisms cannot be measured one at a time: an entry-blackout experiment must run with news
protection modelled (mode A@real) in **both** arms, or it will price one control against a book
that is missing the other.

---

## 6. (E) Council scoring internals — free of the selection confound for the first time

The 2026-07-23 scoring NOTE measured component present-vs-absent **among trades that fired**, and
explicitly flagged the confound; EXP-015 and EXP-016 then proved that confounded reads do not
survive as real trade-set changes. The census here scores **every bar** and forward-walks it, so
the reads below carry no fired-set selection. Winning voice's own component only, clean signals.

| component / value | Train PF | Val PF | y1 | y2 | y3 | y4 |
|---|---|---|---|---|---|---|
| `trend_alignment` = 30 (full) | 1.127 | 1.172 | 0.880 | 1.053 | 1.499 | 1.172 |
| `trend_alignment` = 15 (**partial**) | **0.783** | **0.888** | **0.925** | **0.775** | **0.587** | **0.888** |
| `trend_alignment` = 0 | 1.283 | 1.320 | 1.902 | 2.025 | 0.419 | 1.320 |
| `momentum_macd` = 15 | 1.118 | 1.197 | 1.030 | 0.983 | 1.372 | 1.197 |
| `momentum_macd` = 0 | 1.000 | 1.055 | 0.849 | 1.017 | 1.161 | 1.055 |
| `momentum_rsi` = 20 | 1.049 | 1.115 | 0.921 | 1.040 | 1.215 | 1.115 |
| `momentum_rsi` = 0 | 0.941 | 0.988 | 0.829 | 0.488 | 1.520 | 0.988 |
| `market_structure` = 20 | 1.039 | **1.120** | 0.918 | 1.031 | 1.191 | 1.120 |
| `market_structure` = 0 | **1.076** | 0.946 | 0.884 | 0.787 | 1.758 | 0.946 |

* **The partial trend tier is the single most per-year-consistent negative in this diagnostic:
  PF < 1.0 in all four years, Train avgR −0.148 ± 0.030 (t = −4.9), Val −0.071 ± 0.051 (t = −1.4),
  same sign.** It is a much stronger read than the NOTE's confounded −0.022R. It is also, on the
  record, a lever that has already been tried and rejected — see §7 item 4 for why the two are not
  in contradiction, and why the untried variant is still a *cautious* candidate rather than an
  obvious one.
* **`momentum_macd` and `momentum_rsi` earn their points**, unconditionally and on both splits.
  Up-weighting them is already closed (EXP-015 rejected `macd`→30 and `trend_full`→45).
* **`market_structure` is noise, and the executed-set read of it is a trap.** On executed trades the
  `struct = 0` bucket looks dramatically better (Train PF 1.423 vs 0.993). Unconditionally the
  effect sign-flips (Train 1.076 vs Val 0.946) and the y3 cell (1.758, n=237) carries the whole
  Train result. Nothing to do here — recorded because the executed-set number is tempting.

### 6.1 The direction asymmetry, resolved — and it is a warning

Unconditional clean signals by direction:

| window | BUY avgR ± SE / PF | SELL avgR ± SE / PF |
|---|---|---|
| y1 2021-22 | −0.058 ± 0.036 / 0.911 | −0.051 ± 0.035 / 0.919 |
| y2 2022-23 | +0.010 ± 0.037 / 1.015 | −0.004 ± 0.038 / 0.994 |
| y3 2023-24 | +0.390 ± 0.039 / **1.768** | −0.194 ± 0.038 / **0.716** |
| y4 VAL | +0.246 ± 0.034 / **1.450** | −0.267 ± 0.040 / **0.629** |

SELL is below 1.0 in all four years, on samples of 974–1,401, with the two most recent years at
5–7 SE. On its face this is the largest and most statistically robust effect in the entire
diagnostic, and `council.bear_threshold` is a live `[adjustable]` knob that could suppress it.

**Then apply a within-sample regime control.** Label every calendar quarter by gold's own realised
return and re-cut (this control is deliberately **non-causal** — it uses information not available
at entry — precisely because its job is to diagnose, not to trade):

| regime | BUY n / avgR / PF | SELL n / avgR / PF |
|---|---|---|
| gold **DOWN** quarters | 1,547 / +0.063 / 1.100 | 2,160 / **+0.085 / 1.143** |
| gold **UP** quarters | 4,360 / +0.190 / 1.336 | 2,541 / **−0.287 / 0.601** |
| TRAIN, gold DOWN | 1,120 / −0.047 / 0.929 | 1,837 / **+0.122 / 1.211** |
| TRAIN, gold UP | 3,021 / +0.180 / 1.315 | 1,890 / −0.270 / 0.620 |

**On Train, in falling-gold quarters, SELL is the profitable side and BUY is the losing one** — the
exact mirror of the headline. Gold rose in 11 of 17 quarters and by +14% / +22% / +41% in y2/y3/y4.
"Shorts don't work" is therefore not a mechanism; it is a restatement of "gold went up during our
entire sample". Suppressing SELL would convert a symmetric trend-follower into a levered long-gold
proxy whose backtest edge is indistinguishable from beta. (Val's down-quarters do not reproduce the
Train symmetry — but Val has few and shallow ones inside a +41% year, which is the same point.)

This is ranked **last** in §7, with a recommendation against, specifically because it is the number
most likely to be proposed by someone reading §2.3 alone.

---

## 7. Ranked menu of pre-registerable entry-side experiments

Ranked by **mechanism plausibility × evidence strength × sample size**, not by effect size.

### #1 — Model Risk Voice's news ENTRY blackout in the backtest (parity/honesty first, tuning second)

* **Mechanism.** Live vetoes every entry within `[−45 min, +30 min]` of a high-impact USD event
  (`risk_voice.py` condition 2). The backtest models that condition as *never firing*
  (`NoHistoricalNewsDataProvider` returns `[]`). Every number in this log — including today's
  honest Test baseline — is measured on a trade population containing entries live would refuse.
* **Evidence.** §5.2: pre-event entries PF 0.867 (Train) / 0.828 (Val) against 1.053 / 1.125
  outside; negative in 3 of 4 years; executed-set and mode-A@real reads agree in sign. Live journal
  confirms the gate fires. The calendar to do this now exists.
* **Knobs.** Step 1 is an **engine change**, not a tune: `BacktestConfig` needs a real historical
  provider on Risk Voice's news condition and a `risk_voice_news_modeled` envelope flag. Step 2, if
  and only if step 1 lands: `risk_voice.news_blackout_before_min` (45) — `[adjustable]` per
  Appendix A §1.5.
* **Sample.** Blocked entries: mode A@real 36 / 42 / 37 / 51 per window (pooled Train 115, Val 51);
  mode C 16 / 20 / 22 / 17. **Every window is below rule 6's floor**; only pooled-Train mode A
  clears it. Unconditional signal-bar samples are 393 (Train) / 118 (Val) but overlap heavily.
* **Acceptance bar.** Step 1 is a *measurement*, exactly like EXP-024: fidelity gate (blackout OFF
  must reproduce the recorded rows byte-for-byte), then publish trigger rate + paired
  sequence-fixed per-trade delta with SE, in the pre-registered words "MEASUREMENT WITH WIDE ERROR
  BARS" if pooled Train < 100. Nothing is selected, so no Test touch and no multiple-testing
  inflation. Step 2 would need > 1.7 SE on Train **and** Val plus per-year sign consistency — which
  today's samples cannot deliver, and the pre-registration should say so up front.
* **Main overfitting risk.** Low for step 1 (nothing is chosen). For step 2: the 30–45m sub-band
  (§5.3) is a seductive small-n artifact; any pre-registration must forbid sub-band shopping and
  fix the grid to symmetric, mechanism-motivated widths.
* **Cost.** Trade count falls ~10–13%; the Test-year A arm's 292 records would land near ~254,
  still clear of the 200 floor.

### #2 — `news_blackout_after_min` (30 → 0/15): stop blocking the post-event half

* **Mechanism.** The measured harm is entirely pre-event. Post-event entries — where the release is
  already printed and the direction is revealed — are neutral as raw signals (Train 1.012) and the
  best bucket in the book under live's own exit rules (mode A Train PF 1.902 / Val 1.818, n=93/24).
  A blackout that stays on for 30 minutes *after* the print may be paying for protection it no
  longer needs.
* **Knob.** `risk_voice.news_blackout_after_min`, `[adjustable]`, one parameter, grid {0, 15, 30}.
* **Sample.** 207 (Train) / 71 (Val) unconditional signals; 93 / 24 mode-A records. **Below floor
  on Val.**
* **Acceptance bar.** Strictly downstream of #1 — it is unmeasurable until the blackout is modelled
  at all. Then: paired sequence-fixed effect positive on Train and Val, per-year sign consistency,
  and an explicit risk statement, because this is a **control-weakening** direction (same posture
  EXP-026 was required to take): loosening it re-exposes entries to release-spike slippage that the
  H1 backtest cannot see. That unmodellable tail alone may be reason enough to keep 30.
* **Main overfitting risk.** Moderate-to-high. n=24 on Val, and the mode-A "1.8 PF" reading is a
  per-record figure inflated by partial closes (§1) — it must be re-derived per position, not per
  record, before it is quoted in a pre-registration.

### #3 — Shield slot allocation: `duplicate_signal_cooldown_hours` (and, separately, `max_positions_per_symbol`)

* **Mechanism.** §3.4: only ~47% of distinct signal episodes ever trade. The single slot, not the
  Council, decides which entries the book gets — which is why every filter this project has tested
  produced reshuffling noise rather than a clean effect. If the entry side has a real constraint, it
  is here.
* **Knobs.** `shield.duplicate_signal_cooldown_hours` (4.0, never tested, one parameter, grid
  {2, 4, 6, 8}) and `shield.max_positions_per_symbol` (1).
* **Evidence, stated against the candidate.** The 2026-07-23 martingale NOTE already measured
  variant B ("unlock a second independent slot") as **neutral-to-slightly-worse** (PF 1.092 vs
  baseline 1.102) with lower drawdown. So the second-slot direction has weak negative evidence
  already. The cooldown itself is genuinely untested.
* **Sample.** Full book, 753 (Train) / 254 (Val) trades — the only item on this menu with no rule-6
  problem at all.
* **Acceptance bar.** Standard: Val PF ≥ baseline, plateau across the grid, per-year consistency,
  PF-excl-top-5 > 1.0. **And an explicit anti-reshuffling clause**: because this knob's whole
  mechanism *is* re-sequencing, the usual "portfolio deltas are reshuffling noise" veto cannot be
  applied — which means the plateau and per-year bars must carry the entire weight, and should be
  set harder than usual.
* **Main overfitting risk.** High in a subtle way: this parameter changes *which* trades exist, so
  its response surface is the noisiest kind this log has encountered. `max_positions_per_symbol` is
  additionally gated by the spec's own "raise to 2 only after 3 months live" and by
  `total_risk_ceiling_pct` — it is not a free parameter and should not be swept before that
  condition is met.

### #4 — `trend_alignment` partial tier as a direct entry VETO (not a score change)

* **Mechanism.** Partial EMA alignment (EMA20>EMA50 but EMA200 not yet crossed) is the "trend is
  turning but not established" state. §6: PF < 1.0 in **all four years**, Train avgR −0.148 ± 0.030.
* **Why this is not simply EXP-016 re-run.** EXP-016 changed the *weight* (15 → 0 / 7). That only
  removes partial bars scoring 70–84; a partial bar that also has RSI + MACD + structure + the inert
  confluence scores 85 and still fires at 70 after the cut. A direct veto at the accept step removes
  **all** partial-trend entries (~24% of clean supply) — a materially different treatment set that
  has never been measured. EXP-016's rejection therefore constrains this candidate but does not
  close it.
* **Why it is still ranked below #1–#3.** EXP-016's failure mode was that removing these trades made
  Train *worse* (y1 flipped negative, y3 halved) through re-sequencing — and a broader veto removes
  more, so the same mechanism could bite harder. The unconditional read cannot see that.
* **Knob.** None. This is a `council/` code change, so it needs a spec conversation before it needs
  an experiment (rule 10). Pre-registration must say so.
* **Sample.** 1,871 (Train) / 647 (Val) unconditional signals; roughly 180 / 60 executed trades
  would be removed — Val below floor.
* **Acceptance bar.** Must beat baseline on **Train first, per-year-consistent** (EXP-015/016's own
  bar) before earning a Validation look, then plateau + per-year + top-5 on Val.
* **Main overfitting risk.** Moderate. The read is per-year consistent and confound-free, which is
  rare here; the risk is entirely that removal ≠ improvement once sequencing is applied — the exact
  lesson EXP-015 and EXP-016 already taught on this same component.

### #5 — `bull_threshold` / `bear_threshold` symmetric raise (70 → 75) — WEAK, listed for completeness

* **Mechanism.** §3.3: unconditionally, the 70–74 slice is below 1.0 in all four years and the
  response is monotone in the score on both splits, contradicting the executed-set hump.
* **Why it is weak.** Only y3 is decisively negative (−0.333 ± 0.054); y1/y2/y4 are within ~0.5 SE
  of zero. The 75–84 band is wildly unstable (0.860 / 0.677 / 1.647 / 0.966). And the *executed*-set
  read (§2.1) shows no such gradient — the change would have to work through sequencing, which is
  precisely where this project's candidates die. Raising it also cuts trade count against a
  200-trade floor the strategy is already only just clearing.
* **Knobs.** `council.bull_threshold`, `council.bear_threshold` — a legitimate coupled pair.
* **Acceptance bar.** Standard, but per the project's own priority ranking these are the
  **last** knobs to touch (highest interaction/overfit risk), and the evidence above does not
  justify jumping the queue.

### #6 — Direction asymmetry via `bear_threshold` — **RECOMMENDED AGAINST** (listed to pre-empt it)

* §6.1. The evidence looks overwhelming (SELL PF < 1.0 in 4/4 years, n ≈ 4,700) and the knob exists.
  The regime control demolishes it: in falling-gold quarters on Train, SELL is the *profitable* side
  (PF 1.211) and BUY is the losing one (0.929). The asymmetry is gold's 2021–2026 bull market, not a
  property of the Bear Voice.
* If anyone still wants it, the only honest path is the one EXP-020 already prescribed for the
  regime hypothesis: test it on genuinely out-of-regime data (the cross-project 2009–2019 series,
  which contains the 2013–2015 gold bear market) **before** it is allowed anywhere near this
  project's Validation or Test years.

---

## 8. Checked and found NOT worth pursuing (negative results are results)

1. **`council.conflict_threshold: 55` cannot affect expectancy at all.** Read
   `decision_matrix.evaluate_council`: the clean BUY/SELL rows depend only on
   `bull_threshold`/`bear_threshold` and the module constant `NO_CLEAN_SIGNAL_CEILING = 40`. The
   conflict branch is reached only *after* both clean rows have failed, so it can never add or
   remove a trade — it only labels borderline cases for logging. The project's own priority list
   describes the Council thresholds as "70/40/55"; **55 is inert and 40 is not configurable at all.**
   Any future plan to "tune the Council thresholds" should be written as "tune 70, and decide
   whether 40 is worth a code change".
2. **Lowering the 70 threshold.** The 60–69 population is net-negative on Train *and* Val (§3.1).
3. **Relaxing the `<40` negation ceiling.** Negative in y1/y2/y3, positive only in y4 (§3.2).
4. **Loosening the gate to reach the 200-trade floor.** Supply is ~2× what the single slot can
   absorb (§3.4); the gate is not the constraint, and the marginal supply is negative anyway.
5. **ATR / volatility-regime entry filter.** Train hi-vol 1.277 vs Val hi-vol 0.942 — sign flip,
   and the Train result is a y3 artifact (§4).
6. **Signed EMA200 trend agreement.** Train 1.088 / 0.949, Val 1.085 / 1.121 — flips. Reproduces
   EXP-020's out-of-sample failure; does not reopen it (§4).
7. **Spread-regime filtering, and the `max_spread_*` / `max_atr_panic_multiple` ceilings.** The
   spread series is a near-constant 5 points; `max_spread_points_xauusd: 35` would fire on 4 bars in
   four years and `max_atr_panic_multiple: 3.0` on 21. Inert knobs (§4).
8. **`market_structure` component.** The executed-set read (struct=0 PF 1.423 vs 1.039) is a
   selection artifact; unconditionally it sign-flips across splits (§6).
9. **The 30–45-minutes-before-news sub-band.** Worst cell on both splits (PF 0.558 / 0.031) on
   n = 50 / 16 across seven bands — recorded explicitly as a *warning*, not a lead (§5.3).
10. **Anything already closed by the log** was not re-proposed: session window (EXP-001/003/004),
    `tp_r_multiple` (EXP-002/009), pivot bars (EXP-009), lower-TF entries (EXP-005/007/010/011/014),
    cross-TF confluence filters (EXP-012/013), be/trail (EXP-006/008), scoring reweight (EXP-015),
    partial-tier weight change (EXP-016), deep-loss timeout (EXP-017), trend-regime gate (EXP-020),
    weekend close (EXP-021), min-lot cap (EXP-022), news-protection action/level/fail-direction
    (EXP-023/025/026).

---

## 9. Corrections to the record produced by this pass

* The 2026-07-23 scoring NOTE §3's "score is hump-shaped; the 85+ bucket is the weakest" is a
  **selection artifact of the single-position engine**. Unconditionally the response is monotone in
  the score on both splits (§3.3). The NOTE's *conclusion* (do not move the threshold) survives, for
  different and better reasons; its *stated reason* does not.
* The same NOTE's component present-vs-absent reads can now be replaced with confound-free
  unconditional ones (§6). The partial-trend tier's negative sign is much stronger than the NOTE's
  −0.022R suggested; `market_structure`'s apparent effect is entirely a selection artifact.

## 10. Artifacts

* Diagnostic harness and analysis scripts: session scratchpad (not committed) —
  `entry_diag.py` (modes `trades` / `census`, refuses any window outside y1–y4), `analyze.py`,
  `analyze2.py`.
* Nothing under `config/`, `src/`, or `experiments/experiments_log.md` was modified by this pass.
* Test year: **NOT touched.**
