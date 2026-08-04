# AutoTrade — Parameter Tuning Experiments Log

Rigor protocol: cost model always on (spread + commission + slippage, min 1 spread);
Train/Validation/Test discipline; one parameter (or one coupled pair) per experiment;
pre-registration before running; plateau-beats-peak; >=100 trades per evaluation window;
multiple-testing honesty; never tune the Auditor gate thresholds.

Symbol/data: XAUUSD H1, `data/historical/XAUUSD_H1.csv`, 2021-07-22 → 2026-07-21
(29,543 bars, ~5 yr). Cost model: commission $7.00/lot (IC Markets Raw Spread),
slippage = bar's own spread (min-1-spread convention), spread baked into fill.
Real MT5 SymbolSpec (tick_value 1.0, point_value 100) — harness validated to
reproduce `XAUUSD_20260721T060416Z.json` (199 trades, PF 1.2769) byte-for-byte
with no session gate.

---

## Experiment #1 — Session window (coupled pair `risk_voice.session_start_hour` × `session_end_hour`)

### 0. Methodology finding (must read — affects validity of the whole family)

`backtest/engine.py` explicitly does NOT wire in Risk Voice ("Known gap (Phase 6b)").
Therefore `scripts/run_backtest.py` is **ungated by session** — editing
`config/base.yaml`'s `session_start_hour`/`session_end_hour` has **zero effect** on
that CLI's output. The prior baseline (199 trades) is the *ungated / all-24h* run,
NOT the current 14-18 config as live would apply it.

To evaluate the session window faithfully I reproduce Risk Voice condition 4 exactly
(veto/drop any entry whose bar hour is outside `[start, end)` server time, half-open)
by wrapping the stock `_council_signal_fn` with a session gate, while still feeding
ALL bars to the engine so SL/TP/gap exit simulation stays correct. Harness:
`experiments/session_window_harness.py`. No production/pipeline code modified — only
the engine's documented `signal_fn` injection point is used.

Implication: the "199 trades / PF 1.28" figure from the prior context is the *all-hours*
run. The true current-config (14-18) baseline is measured fresh below.

### 1. Hypothesis / mechanism

Appendix A §1.5 allows the session gate to start at the London+NY overlap only, then
**expand later** ("อนุญาตเฉพาะ London + New York overlap ก่อน แล้วขยายทีหลัง `[adjustable]`").
The current window `[14,18)` is just the 4-hour overlap. Widening should admit more
in-session bars → more entries → higher trade count (directly relevant to the 200-trade
promotion-gate shortfall). Open question: does widening hold/raise profit factor, or
dilute the edge by admitting lower-quality London-only / NY-afternoon hours? The filter
exists to avoid Asia-session chop (≈00:00–08:00 server), so candidates stay within
London+NY hours and never reach into Asia.

### 2. Data splits (chronological, no shuffling — 60/20/20)

Calibration run showed 14-18 yields ~160 trades/yr even gated (one-position-at-a-time +
long holds mean the gate only modestly cuts count), so single-year OOS windows clear the
100-trade floor → a standard 60/20/20 split is viable; walk-forward not required.

| Split       | Range                       | ~span |
|-------------|-----------------------------|-------|
| Train       | 2021-07-22 → 2024-07-21     | 3 yr  |
| Validation  | 2024-07-21 → 2025-07-21     | 1 yr  |
| Test        | 2025-07-21 → 2026-07-21     | 1 yr  |

Tune/choose on Train. Compare candidates on Validation. Touch Test exactly once, at the
end, on the single chosen window + baseline.

### 3. Grid (coupled pair, coarse — 1 experiment)

Baseline B `[14,18)` (current). Candidates expand around the overlap, staying within
London+NY (≈08:00–22:00 server), never into Asia:

| id | window   | shape                          |
|----|----------|--------------------------------|
| B  | [14,18)  | current — overlap only (4h)    |
| C1 | [13,19)  | symmetric +1h (6h)             |
| C2 | [12,20)  | symmetric +2h (8h)             |
| C3 | [10,20)  | London-open → NY (10h)         |
| C4 | [14,22)  | extend into NY afternoon (8h)  |
| C5 | [12,22)  | wide London-late + NY (10h)    |

6 configs in this family (multiple-testing count = 6, well under the N>20 threshold).

### 4. Metric that decides + acceptance criterion (pre-registered)

Deciding metric: **profit factor on the out-of-sample window**, gated by trade-count
floor and plateau checks. ADOPT candidate W over baseline iff ALL of:
- (a) Validation: `trade_count(W) >= 100` AND `PF(W) >= PF(B) - 0.02` (not materially worse);
- (b) Plateau: W's grid-neighbors on Validation within ~15% PF of W (reject isolated peaks);
- (c) Train consistency: W is among the stronger windows on Train too (not a Val-only fluke);
- (d) Meaningful direction: higher PF at similar/higher trades, OR materially more trades
      (toward the 200 gate) with PF held within ~0.02 of baseline;
- (e) Test (touched once) confirms (a).
Else REJECT (keep [14,18)) or INSUFFICIENT DATA. Auditor gate thresholds NOT touched.

### 5. Spec bounds

§1.5 tags the session hours `[adjustable]` and explicitly sanctions expansion; no hard
numeric bounds. Candidates confined to London+NY (08:00–22:00 server) per the filter's
stated purpose (avoid Asia chop). `friday_close_hour` (separate param) untouched.

### 6. Results

#### 6.1 Train (2021-07-22 → 2024-07-21, 3 yr)

| id | window   | trades | PF    | net $ | DD %  | PF(excl top5) |
|----|----------|--------|-------|-------|-------|---------------|
| B  | [14,18)  | 512    | 1.040 | +618  | 11.0  | 1.006 |
| C1 | [13,19)  | 550    | 0.976 | −369  | 18.5  | 0.945 |
| C2 | [12,20)  | 560    | 1.028 | +461  | 14.3  | 0.996 |
| C3 | [10,20)  | 593    | 0.967 | −556  | 14.8  | 0.939 |
| C4 | [14,22)  | 499    | **1.120** | +1852 | 8.8   | 1.083 |
| C5 | [12,22)  | 541    | 1.081 | +1302 | 10.5  | 1.046 |

Signal: **extending the END hour into NY afternoon (18→22) helps** (C4 PF 1.120, lower DD
8.8%); **extending the START hour earlier into London morning (10–14) hurts** (C1/C3 go
sub-1.0). The "wider → more trades" hypothesis is only weakly/non-monotonically true
(one-position-at-a-time: a late entry blocks the next day's overlap entry), so the real
effect is edge QUALITY per hour-of-day, not raw count. Train winner: C4 [14,22).

#### 6.2 Refinement pass — pre-registered BEFORE running Validation

C4 [14,22) is the Train peak. To guard against an isolated peak (rule 5) I add two
grid-neighbors of C4 for the Validation comparison: R1 `[14,20)` (end −1 step) and
R2 `[16,22)` (start +1 step). Validation set = {B, C2, C4, C5, R1, R2}. Family size now
8 distinct windows (still < 20). Deciding metric and acceptance criterion unchanged (§4).

#### 6.3 Validation (2024-07-21 → 2025-07-21, 1 yr)

| id | window   | trades | PF        | net $ | DD %  | Train PF (ref) |
|----|----------|--------|-----------|-------|-------|----------------|
| B  | [14,18)  | 155    | 1.039     | +172  | 4.72  | 1.040 |
| C2 | [12,20)  | 187    | 1.051     | +268  | 5.40  | 1.028 |
| R1 | [14,20)  | 166    | 1.050     | +237  | 4.88  | 1.051 |
| C4 | [14,22)  | 172    | **0.985** | −73   | 5.80  | 1.120 |
| C5 | [12,22)  | 187    | 0.979     | −110  | 5.82  | 1.081 |
| R2 | [16,22)  | 136    | 0.963     | −138  | 5.23  | — |

R1 [14,20) Train run added post-hoc for the consistency check: 520 trades, PF 1.051,
DD 13.5% (worse than baseline's 11.0%).

#### 6.4 Robustness analysis

- **Overfitting trap caught:** the Train-optimal window C4 [14,22) (PF 1.120, best on Train)
  **fails out-of-sample** (Val PF 0.985, net-negative). Extending the end hour into NY
  afternoon (18→22) was a 2021-2024 artifact; on 2024-2025 those hours were net-losing
  (C4/C5/R2 all sub-1.0 on Val). Exactly the failure mode the Train/Val split exists to catch.
- **No stable edge over baseline:** the Train and Validation rankings disagree. Val leaders
  (C2 1.051, R1 1.050) were mediocre on Train (C2 was *below* baseline); the Train leader is
  near-worst on Val. The only window consistent across both splits is R1 [14,20)
  (Train 1.051 / Val 1.050), but its edge over baseline is only ~+0.011 PF on each — inside
  the noise band (its excl-top-5 PF on Val is 0.942, still fragile) — and it *worsens*
  Train drawdown (13.5% vs 11.0%).
- **Unstable optimum, not a plateau (rule 5):** the end-hour response surface flips between
  splits — on Train PF keeps *rising* toward end=22 (1.040→1.051→1.120), on Val it *falls*
  (1.039→1.050→0.985). The apparent Val peak at end=20 is the crossover of two opposite
  regime behaviors, not a structural plateau.
- **Multiple-testing (rule 7):** 8 windows evaluated in this family; best Val gap over
  baseline is +0.012 PF — the magnitude expected from luck alone across 8 tries. Edge far
  too small to demand a change.
- **Baseline is the most stable window:** [14,18) Train 1.040 ≈ Val 1.039.
- **Original count hypothesis refuted:** widening does *not* reliably raise trade count
  (one-position-at-a-time: a late entry blocks the next overlap entry); where it does
  (C2/C5 → 187/yr) PF does not rise above noise. **Widening the session window is NOT a
  viable lever to clear the 200-trade / PF 1.3 promotion gate** — a finding for the roadmap.

### 7. VERDICT — REJECT (no change)

Keep `session_start_hour: 14` / `session_end_hour: 18`. No candidate delivers a stable,
plateau-backed edge over baseline across both Train and Validation; the one window with a
large Train edge (C4 [14,22)) fails OOS, and the only cross-split-consistent window (R1
[14,20)) improves PF by ~1% (within noise) while worsening drawdown on an unstable response
surface. Per rule 5 (plateau beats peak), rule 7 (multiple-testing), and rule 9 (negative
results are results): **default is already on the plateau — no change recommended.**

**Test set (2025-07-21 → 2026-07-21) left UNTOUCHED** — it is reserved to confirm an adopted
change (rule 2); none was adopted, so it stays pristine for a future genuine candidate.
`config/base.yaml` NOT modified. Auditor gate thresholds NOT touched (rule 8).

---

## EXP-002 (in progress) 2026-07-21 — `order.tp_r_multiple`

Status: PRE-REGISTERED (running)

### 0. Methodology finding (must read)

The stock backtest engine (`backtest/engine.py`, `run_backtest`) exits a position ONLY on
SL / TP / end-of-data. **Watchman (breakeven_at_r, trail_start_r, trail_distance_atr,
time_stop_hours, dead_trade) is NOT modeled in the backtest** — grep confirms no
breakeven/trail/time_stop logic in `engine.py`. Consequences:
- Tuning any `watchman.*` param has ZERO effect on backtest output — cannot be tuned this way.
- TP is the ONLY profit exit in the backtest, so `tp_r_multiple` is the single dominant
  expectancy lever available here. It is the honest first target.
- CAVEAT for interpretation: because live adds breakeven+trailing on top, backtest expectancy
  is a conservative-in-some-ways / biased-in-others proxy for live. Any TP change adopted from
  backtest must be re-confirmed once Watchman is wired into the engine (roadmap item).

`scripts/run_backtest.py` passes NO strategy overrides and NO `risk_voice_cfg`, so both
existing baseline reports used engine DEFAULTS (sl 0.2/0.8/2.5, tp 2.0, thresholds 70/70/55,
Risk Voice OFF). Harness `experiments/param_sweep_harness.py` reproduces that faithfully
(test window: 200 trades / PF 1.302 vs report's 199 / 1.277 — the 1-trade delta is one
boundary bar from an inclusive end-date, engine logic identical).

### 1. Hypothesis / mechanism

Win rate is ~40% with a fixed 2R TP → expectancy ≈ 0.4·2 − 0.6·1 = +0.2R (matches observed
avg_r 0.14–0.19). Lowering TP raises hit-rate but shrinks reward-per-win; raising TP does the
opposite. There is a smooth interior optimum. Mechanism is transparent (no interaction with
other params beyond stop distance, which is unchanged). Question: is 2.0 already on the
plateau, or is a nearby value materially better OOS?

### 2. Data splits (reuse EXP-001's, chronological 60/20/20)

| Split      | Range                    | span |
|------------|--------------------------|------|
| Train      | 2021-07-22 → 2024-07-21  | 3 yr |
| Validation | 2024-07-21 → 2025-07-21  | 1 yr |
| Test       | 2025-07-21 → 2026-07-21  | 1 yr (touch once, only if a candidate is adopted) |

### 3. Grid (one parameter, coarse)

tp_r_multiple ∈ {1.5, 1.75, 2.0(baseline), 2.25, 2.5, 3.0}. 6 configs (family count = 6,
< 20). One refinement pass max around any plateau.

### 4. Metric that decides + acceptance criterion (pre-registered)

Deciding metric: **expectancy in R (avg_r_multiple) on Validation**, gated by PF and trades.
ADOPT candidate T over baseline (2.0) iff ALL:
- (a) Validation: trades ≥ 100 AND avg_r(T) > avg_r(2.0) AND PF(T) ≥ PF(2.0);
- (b) Plateau: T's ±1 grid-step neighbors on Validation within ~15% of T's avg_r (reject peaks);
- (c) Train consistency: T is also ≥ baseline on Train (not a Val-only fluke);
- (d) Robustness: per-year PF not owed to one year; PF-excl-top-5 stays > 1.0;
- (e) Test (touched once) confirms (a).
Else REJECT (keep 2.0) or INSUFFICIENT DATA. Auditor gate thresholds NOT touched (rule 8).
Spec bounds: `tp_r_multiple` tagged [adjustable] (Appendix A §1.4), no hard numeric bound.

### 5. Results

Harness: `experiments/param_sweep_harness.py` (stock engine + stock signal fn, cost model on:
commission $7/lot, slippage = bar's own spread; Risk Voice OFF, matching the CLI baselines).

#### 5.1 Train (2021-07-22 → 2024-07-21, 3 yr)

| tp   | trades | win%  | PF    | net $  | avgR  | DD %  | PF_ex5 |
|------|--------|-------|-------|--------|-------|-------|--------|
| 1.5  | 770    | 41.6  | 1.031 | +656   | 0.024 | 14.4  | 1.012  |
| 1.75 | 665    | 37.7  | 1.032 | +639   | 0.023 | 16.5  | 1.008  |
| 2.0* | 587    | 35.6  | 1.084 | +1544  | 0.052 | 11.1  | 1.052  |
| 2.25 | 531    | 32.4  | 1.060 | +939   | 0.038 | 13.0  | 1.022  |
| 2.5  | 528    | 31.3  | **1.124** | +2133 | 0.077 | 12.5 | 1.082 |
| 3.0  | 441    | 27.2  | 1.115 | +1763  | 0.074 | 14.3  | 1.060  |

(*baseline)

#### 5.2 Validation (2024-07-21 → 2025-07-21, 1 yr)

| tp   | trades | win%  | PF    | net $  | avgR  | DD %  | PF_ex5 |
|------|--------|-------|-------|--------|-------|-------|--------|
| 1.5  | 271    | 40.6  | 0.985 | −105   | −0.004| 6.74  | 0.930  |
| 1.75 | 256    | 36.7  | 0.964 | −246   | −0.009| 7.66  | 0.902  |
| 2.0* | 223    | 36.3  | 1.064 | +404   | 0.070 | 4.66  | 0.983  |
| 2.25 | 194    | 34.5  | **1.116** | +646 | 0.102 | 4.62 | 1.012 |
| 2.5  | 178    | 32.0  | 1.092 | +479   | 0.099 | 5.88  | 0.973  |
| 3.0  | 163    | 28.2  | 1.109 | +541   | 0.102 | 5.94  | 0.961  |

#### 5.3 Robustness read

- **Robust guardrail (both splits):** tp < 2.0 is worse — Val goes NET-NEGATIVE at 1.5/1.75
  (PF 0.985/0.964), Train PF falls to ~1.03 with DD up to 16.5%. Confirms current 2.0 is NOT
  too high; lowering TP is off the table.
- **Cross-split plateau above baseline:** tp ∈ [2.5, 3.0] beats baseline (2.0) on BOTH PF and
  avgR on BOTH splits, and forms a plateau on each (Train 1.124/1.115; Val 1.092/1.109).
- **The 2.25 wrinkle:** 2.25 is the Val PF peak but a Train dip (PF 1.060 < baseline) — it
  fails Train-consistency (criterion c) and is treated as noise, not the candidate. tp=2.5 is
  the value sitting on BOTH plateaus.
- **DD / trade-count trade-off:** raising TP lowers trade count (fewer completed round-trips)
  and lifts Train DD (2.0→11.1%, 2.5→12.5%, 3.0→14.3% — 3.0 approaches the 15% gate ceiling,
  2.5 keeps margin). Fewer trades moves AWAY from the 200-trade promotion gate on short
  windows — a real cost, flagged.
- Candidate = **tp 2.5** (better DD margin & trade count than 3.0, on both plateaus).

#### 5.4 Per-year consistency (DECISIVE) — Test year untouched

Per-year PF (Y4 = Validation, from §5.2):

| Year         | tp 2.0 (base) | tp 2.5      | tp 3.0      |
|--------------|---------------|-------------|-------------|
| Y1 2021-22   | **1.050** (+297) | 0.866 (−746) | 0.873 (−675) |
| Y2 2022-23   | 1.001 (+5)    | 1.151 (+910)| 1.182 (+901)|
| Y3 2023-24   | 1.224 (+1294) | 1.294 (+1673)| 1.218 (+1040)|
| Y4 2024-25   | 1.064 (+404)  | 1.092 (+479)| 1.109 (+541)|

**The aggregate Train/Val edge of tp 2.5/3.0 is a REGIME ARTIFACT.** In Y1 (2021-22 — a
choppier, lower-momentum gold regime) the wider target rarely fills (win rate collapses 35%→
26%/23%), PF drops to 0.866/0.873 and the year goes NET-NEGATIVE with DD blowing out to
10.4%/14.3%. tp 2.5/3.0 make money only because the 2022-2024 trending run (Y2+Y3) outweighs
the Y1 loss. **Baseline tp=2.0 is the ONLY value in [1.5, 3.0] that is PF ≥ 1.0 in every
single year** (1.050 / 1.001 / 1.224 / 1.064) — the most regime-robust setting tested.

### 6. Robustness summary

- neighborhood/plateau: ✓ on each split individually (Val plateau 2.25-3.0; Train plateau
  2.5-3.0) — BUT the plateau is a split-aggregate illusion; it dissolves per-year.
- per-year consistency: ✗ (tp 2.5/3.0 net-negative in Y1; convert a winning baseline year to
  a loss — owe their edge to one regime).
- top-5 dependency: mixed (Val PF_ex5 < 1.0 for 2.5/3.0 AND for baseline on the 1-yr window).
- walk-forward: n.a. (the per-year split above is the equivalent regime check and is decisive).
- Multiple-testing (rule 7): 6 configs this family; the best aggregate gap fails the per-year
  robustness gate, so magnitude is moot.

### 7. VERDICT — REJECT (no change)

Keep `order.tp_r_multiple: 2.0`. Two robust findings, both pointing to "leave it":
1. **Do NOT lower TP below 2.0** — Val goes net-negative (PF 0.985/0.964 at 1.5/1.75), Train
   PF falls to ~1.03 with DD to 16.5%. The current value is not too high.
2. **Do NOT raise TP to 2.5/3.0** — the apparent OOS improvement is a 2022-2024 trending-regime
   bet that would have lost money in 2021-22. tp=2.0 is the most regime-robust value tested.

Per rule 5 (plateau must survive robustness), rule 9 (negative results are results), rule 2
(Test untouched — no candidate adopted): **default tp=2.0 is already well-placed; no change.**

**Test set (2025-07-21 → 2026-07-21) left UNTOUCHED.** `config/base.yaml` NOT modified.
Auditor gate thresholds NOT touched (rule 8). Watchman exits remain UNMODELED in the engine
(see §0) — wiring them in is the highest-value roadmap item before any further exit-param
tuning, since fixed-TP tuning cannot capture the trailing behaviour live actually uses.

---

## EXP-003 2026-07-21 — Session filter ON `[14,18)` vs OFF (all-24h)

Status: ADOPT-CANDIDATE (Test confirmed) — recommend to user, config NOT auto-changed

### 0. Relation to EXP-001

EXP-001 compared `[14,18)` against OTHER windows CONFINED to London+NY (08-22), and found no
robust edge among them → kept `[14,18)`. It never tested REMOVING the filter (all-24h incl.
Asia). This experiment does exactly that — a different, 1-dimensional question (filter on/off),
prompted by the user. NOTE the live/backtest mismatch surfaced here: `scripts/run_backtest.py`
is ungated, so BOTH existing promotion-gate reports were generated ALL-24h, while live applies
`[14,18)` via Risk Voice — the promotion numbers do not reflect what live actually trades.

### 1. Hypothesis / mechanism

If the London+NY overlap were the genuinely-best-quality window, gating to `[14,18)` should
raise PF vs trading 24h. Test: does the filter add risk-adjusted value, per year?

### 2. Splits — reuse EXP-001/002 (Train Y1-Y3, Val Y4, Test Y5). Metric: per-year PF + net,
gated by trade floor. ADOPT-removal iff all-24h ≥ inside in a MAJORITY of years AND not
materially worse in any (mirrors EXP-002's per-year robustness bar), then Test confirms.

### 3. Results — all-24h (ungated, tp2.0) vs inside-[14,18)-only

Harness: `scratchpad/session_split.py` (mode inside|outside|all) + EXP-002's ungated per-year
runs (which ARE the all-24h case). Cost model on (commission $7/lot, slippage = bar spread).

| Year        | all-24h PF (net)  | inside [14,18) PF (net) | winner   |
|-------------|-------------------|--------------------------|----------|
| Y1 2021-22  | 1.050 (+297)      | **0.898 (−518)**         | all-24h  |
| Y2 2022-23  | 1.001 (+5)        | 1.038 (+193)             | inside(marg) |
| Y3 2023-24  | 1.224 (+1294)     | 1.183 (+1018)            | all-24h  |
| Y4 Val 24-25| 1.064 (+404)      | 1.039 (+172)             | all-24h  |
| Y5 Test 25-26| 1.277 (+1458, 199tr, DD4.37)| 1.275 (+1073, 145tr, DD5.12) | all-24h |

all-24h wins/ties on PF in 4 of 5 years (only Y2 marginally favours the filter, +$188 on a
break-even year) and beats inside on NET PROFIT in ALL 5 years. Test (touched once here): PF
tied (1.277 vs 1.275) but all-24h delivers +$385 more, +54 trades, LOWER drawdown. The filter
TURNS A WINNING YEAR (Y1, all-24h +$297) INTO A LOSS (−$518) with no compensating benefit
elsewhere. Test set now CONSUMED for the session-filter family (rule 2 — one touch).

5-yr hour bucket (ungated, trades by entry hour): outside-[14,18) 777 tr PF 1.142 (+$3493);
inside 222 tr PF 1.105 (+$743). BOTH profitable, but the filter keeps only the smaller,
lower-PF slice. Within the current window, hours 15 & 16 are net LOSERS (−$296, −$460);
worst hours overall are 22-23 (rollover, −$928/−$306).

### 4. Robustness / caveats (why this is ADOPT-CANDIDATE, not auto-adopt)

- Per-year: ✓ (all-24h ≥ inside in 4/5 yr, incl. the decisive Y1 sign flip). PASSES the same
  bar tp 2.5 FAILED.
- **Unmodeled live risks** (backtest cannot see these; session filter partly proxied for them):
  (a) news veto is unmodeled — removing the session gate must NOT remove the news blackout;
  (b) Asia/rollover thin-liquidity: hours 22-23 are net-negative even WITH spread cost modeled;
  (c) DST drift in server time. So "all-24h" the safe way = drop the session gate BUT keep
  news blackout + a rollover/Friday guard, not a literal unconditional 24h.
- Overfit guard: resisted per-hour cherry-picking (24-dim, high overfit risk). Filter on/off is
  the 1-param, robust conclusion.

### 5. VERDICT — recommend REMOVING the `[14,18)` session gate (config change is the USER's call)

Evidence robustly favours trading beyond the overlap: all-24h beats `[14,18)` in 4/5 years incl.
Test, and the filter caused a full losing year (Y1). This REVERSES EXP-001's "keep default"
only because EXP-001 never tested removal. `config/base.yaml` NOT modified by me (analysis-only
mandate) — awaiting user confirmation. If adopted, implement as: widen/disable the session gate
while KEEPING news blackout + Friday/rollover guard; re-verify once Watchman exits are modeled.
Auditor gate thresholds NOT touched (rule 8).

---

## EXP-004 2026-07-21 — Session gate `[0,22)` (rollover-hours-excluded) vs all-24h and `[14,18)`

Status: PRE-REGISTERED (running) — Train+Validation only, Test set NOT touched (already
CONSUMED for the session-filter family per EXP-003; rule 2 — one touch).

### 0. Relation to EXP-001/002/003 + why this specific candidate

EXP-003 found that REMOVING the `[14,18)` gate (trading all-24h) beats the filter in 4/5 years
incl. the Test year, and CONSUMED the Test set for the session-filter family. But its own §4
caveat (b) flagged that rollover hours 22-23 are net-negative even with spread cost modeled
(5-yr hour bucket: 22:00 −$928, 23:00 −$306), and its §5 recommendation was NOT "literal
unconditional 24h" but "drop the session gate BUT keep a rollover/Friday guard." That specific
compromise config — `session_start_hour=0, session_end_hour=22` (admit all hours EXCEPT the
documented net-losing rollover hours 22-23), news blackout + Friday-close guard unchanged — was
RECOMMENDED but never backtested as its own explicit condition in EXP-003. This experiment tests
exactly that config, on Train+Validation ONLY.

An undisciplined ad-hoc peek at the already-CONSUMED Test year was taken by the orchestrator
(PF 1.32 / 184 tr / DD 3.97% / +$1,539). Per rule 2 that is a spent-Test-set peek and is NOT
valid evidence; it is recorded here only as the context that prompted this disciplined run. This
experiment's verdict stands solely on the Train+Validation evaluation below and does NOT reuse or
confirm that number.

### 1. Hypothesis / mechanism

Mechanism is explicit and pre-committed from EXP-003's own hour-bucket data: hours 22-23
(rollover / thin-liquidity) are net-losing even after spread+commission+slippage. If those two
hours are the *only* genuinely-bad slice, then excluding just them — `[0,22)` — should (a) match
or beat literal all-24h (it removes a documented loss-making slice), and (b) beat the narrow
`[14,18)` filter (which EXP-003 already showed discards profitable London/NY/Asia hours and even
flips Y1 into a losing year). Open question / failure mode: the 22-23 loss could be an aggregate
artifact of one regime, or removing those hours could remove too few trades to matter, or could
itself flip a year — the per-year robustness bar (below) is the guard.

### 2. Data splits (reuse EXP-001/002/003's; chronological, no shuffling)

| Split      | Range                    | span | use here |
|------------|--------------------------|------|----------|
| Train      | 2021-07-22 → 2024-07-21  | 3 yr | evaluate per-year (Y1,Y2,Y3) |
| Validation | 2024-07-21 → 2025-07-21  | 1 yr | evaluate (Y4) |
| Test       | 2025-07-21 → 2026-07-21  | 1 yr | **NOT TOUCHED — CONSUMED by EXP-003** |

Per-year boundaries: Y1 2021-07-22→2022-07-21, Y2 →2023-07-21, Y3 →2024-07-21, Y4 →2025-07-21.

### 3. Conditions (one gate parameter; candidate + 2 plateau neighbors + 2 existing baselines)

| id | gate      | role                                            |
|----|-----------|-------------------------------------------------|
| A  | none/24h  | baseline 1 — EXP-003's all-24h (rollover incl.) |
| F  | [14,18)   | baseline 2 — current live config                |
| K  | **[0,22)**| **CANDIDATE** — rollover 22-23 excluded         |
| N1 | [0,21)    | plateau neighbor (end −1h)                       |
| N2 | [0,23)    | plateau neighbor (end +1h)                       |

All re-run through the SAME `experiments/session_window_harness.py` for apples-to-apples (cost
model on: commission $7/lot, slippage = bar's own spread; news/Friday guards are Risk-Voice-live
concerns unmodeled in the engine and identical across all conditions, so they don't bias the
comparison). Family multiple-testing count for the session-filter family is now: EXP-001's 8
windows + EXP-003's {all-24h} + this experiment's {[0,22),[0,21),[0,23)} = 12 distinct windows
(still < 20). Candidate K is ONE new condition; N1/N2 are its plateau guards, not extra bets.

### 4. Metric that decides + acceptance criterion (pre-registered)

Deciding metric: **per-year profit factor + net $**, gated by trade floor and plateau (mirrors
EXP-002/003's per-year robustness bar — the bar that caught tp 2.5's regime artifact and passed
all-24h). ADOPT-CANDIDATE `[0,22)` iff ALL of:
- (a) Trade floor: `trades(K) >= 100` in every one of the 4 years (Y1-Y4);
- (b) vs `[14,18)`: K's PF ≥ F's PF in a MAJORITY of the 4 years AND K is not materially worse
      (PF gap > 0.05 against it) in ANY year — i.e. K robustly dominates the current live filter;
- (c) vs all-24h (A): K's PF ≥ A's PF − 0.02 in every year (K must not be materially worse than
      simply trading 24h; the whole point of excluding 22-23 is that it should NOT cost us) AND
      K beats A on net $ or PF in a MAJORITY of years (the rollover exclusion earns its keep);
- (d) No sign flip: K does not turn any all-24h-positive year net-negative (the EXP-002 tp-2.5
      failure mode / EXP-003 Y1 filter failure mode);
- (e) Plateau (rule 5): neighbors N1 `[0,21)` and N2 `[0,23)` are within ~15% PF of K on the
      aggregate Train+Val AND agree in direction (no sharp isolated peak at exactly end=22).
Else REJECT (state which baseline to keep) or INSUFFICIENT DATA. Test set NOT touched (rule 2).
Auditor gate thresholds NOT touched (rule 8).

### 5. Spec bounds

§1.5 tags the session hours `[adjustable]`; no hard numeric bound. `[0,22)` and its neighbors are
within bounds. `friday_close_hour`, `news_blackout_*` untouched (separate params). Analysis-only:
`config/base.yaml` NOT modified regardless of verdict — the user makes the final call.

### 6. Results

Harness: `experiments/session_window_harness.py` logic, all 5 conditions re-run through the
SAME code path (cost model on: commission $7/lot, slippage = bar's own spread; Risk Voice OFF /
signal-gate injection only). Fidelity: conditions A and F reproduce EXP-002/003's per-year
all-24h and inside-[14,18) figures to the cent (A: Y1 1.050/+297, Y2 1.001/+5, Y3 1.224/+1294,
Y4 1.064/+404; F: Y1 0.898/−518, Y2 1.038/+193, Y3 1.183/+1018, Y4 1.039/+172) — apples-to-apples
confirmed. Batch: `scratchpad/exp004_batch.py`. Test year NOT run.

#### 6.1 Per-year grid (PF / net $ / trades / DD%) — Test year EXCLUDED

| Year | A all-24h | F [14,18) | **K [0,22)** | N1 [0,21) | N2 [0,23) |
|------|-----------|-----------|--------------|-----------|-----------|
| Y1 21-22 (Tr) | 1.050 / +297 / 197 / 5.74 | 0.898 / −518 / 168 / 10.20 | 1.011 / +69 / 206 / 7.03 | 1.010 / +63 / 206 | 1.011 / +68 / 202 |
| Y2 22-23 (Tr) | 1.001 / +5 / 202 / 11.24 | 1.038 / +193 / 165 / 10.78 | 1.037 / +239 / 206 / 11.23 | 1.035 / +226 / 206 | 1.020 / +125 / 203 |
| Y3 23-24 (Tr) | 1.224 / +1294 / 183 / 9.59 | 1.183 / +1018 / 178 / 6.49 | 1.258 / +1477 / 181 / 8.95 | 1.242 / +1381 / 181 | 1.239 / +1372 / 182 |
| Y4 24-25 (Val)| 1.064 / +404 / 223 / 4.66 | 1.039 / +172 / 155 / 4.72 | 1.020 / +124 / 222 / 6.08 | 1.034 / +214 / 219 | 1.047 / +305 / 230 |
| **Aggregate net** | **+2000 / 805 tr** | +866 / 666 tr | +1909 / 815 tr | +1884 / 812 tr | +1870 / 817 tr |

Min trades/year for K = 181 (≥100 floor ✓, all conditions clear it).

### 7. Robustness / acceptance-criteria evaluation

Scoring K [0,22) against the pre-registered §4 criteria:

- **(a) Trade floor ≥100 every year: PASS.** K = 206/206/181/222.
- **(b) K vs current live [14,18) (F): PASS.** K ≥ F PF in Y1 (1.011 vs 0.898 — a decisive sign
  flip: +$69 vs −$518), Y3 (1.258 vs 1.183); ties Y2 (1.037 vs 1.038, noise); Y4 marginally below
  (1.020 vs 1.039, gap 0.019 < 0.05 → not material). Majority-win, no material loss in any year.
  K robustly beats the current filter and, unlike F, does NOT flip Y1 negative.
- **(c) K vs all-24h (A): FAIL.** Pre-registered bar = "K ≥ A PF − 0.02 in EVERY year AND K beats
  A on net/PF in a majority." K falls MORE than 0.02 below A in **two** years: Y1 (1.011 vs 1.050,
  gap −0.039) and Y4/Val (1.020 vs 1.064, gap −0.044). On net, K beats A in only 2/4 years
  (Y2 +239 vs +5, Y3 +1477 vs +1294) and loses Y1 (+69 vs +297) and Y4 (+124 vs +404). Aggregate
  net: A +$2000 > K +$1909. **Excluding rollover hours 22-23 does NOT earn its keep vs simply
  trading 24h** — it makes ~$91 LESS in aggregate and is materially worse than all-24h in half the
  Train+Val years.
- **(d) No sign flip: PASS.** A is net-positive in all 4 years; K stays net-positive in all 4
  (69/239/1477/124). K is safe (does not manufacture a losing year the way F does to Y1).
- **(e) Plateau (rule 5): PASS (flat).** N1 [0,21) and N2 [0,23) track K within ~1-2% PF every
  year (Y3 K 1.258 / N1 1.242 / N2 1.239; Y4 K 1.020 / N1 1.034 / N2 1.047). No sharp isolated
  peak at end=22 — but the plateau is flat precisely BECAUSE moving the tail cut by ±1h barely
  changes anything (one-position-at-a-time: very few fresh entries occur at hours 21/22/23).

**Why (c) fails — mechanism.** EXP-003's motivating stat (5-yr hour bucket: 22:00 −$928, 23:00
−$306) is a MULTI-YEAR AGGREGATE dominated by specific years (incl. the now-consumed Test year
and Y3), not a per-year-stable loss. Per year, entries at 22-23 were net-POSITIVE in Y1 and Y4,
so excluding them removed profitable trades and (via one-position sequencing — a trade held into
22-23 vs a fresh entry) shuffled the downstream trade set unfavourably. The "rollover hours are
uniformly bad" premise does not survive the per-year split — the same failure mode EXP-002's
tp-2.5 hit (an aggregate edge that was really a regime artifact).

**Multiple-testing (rule 7):** session-filter family now 12 distinct windows; K's aggregate edge
over all-24h is negative, so magnitude is moot — no favourable result to over-credit.

**Note on the undisciplined Test peek.** The orchestrator's ad-hoc [0,22) run on the CONSUMED
Test year (PF 1.32 / 184 tr / +$1,539) is NOT used here and does not enter the verdict (rule 2 —
Test is spent for this family). Even taken at face value it would only show [0,22) ≈ all-24h on
Test (EXP-003 all-24h Test = PF 1.277 / +$1,458), consistent with the Train+Val finding that
the rollover exclusion is roughly net-neutral-to-slightly-worse vs all-24h, not a new edge.

### 8. VERDICT — REJECT the `[0,22)` candidate (rollover exclusion NOT justified on Train+Val)

`session_start_hour=0, session_end_hour=22` **fails its own pre-registered acceptance** (criterion
(c)): excluding rollover hours 22-23 does not beat simply trading all-24h — it earns ~$91 LESS in
aggregate over Train+Val and is materially worse than all-24h in Y1 and Y4 (the Validation year).
The 22-23-are-bad premise is a multi-year-aggregate artifact that dissolves per-year (rule 5/9).

Two honest sub-findings, both matter for the user's decision:
1. **[0,22) IS clearly better than the current live [14,18) filter** (fixes the Y1 sign flip,
   +$1,043 more aggregate net, more trades). If the choice were only "current filter vs [0,22)",
   [0,22) wins. But that is not the decision on the table.
2. **[0,22) is NOT better than EXP-003's actual recommendation (all-24h + news/Friday guards).**
   The specific rollover-exclusion refinement adds no value over plain all-24h and slightly hurts.
   EXP-003's recommendation stands as-is; this experiment does NOT extend or improve it.

Recommendation to user: do NOT adopt `[0,22)` as a distinct config. If acting on the session
family, EXP-003's all-24h (gate removed, news blackout + Friday/rollover guard KEPT as live-risk
controls that the backtest cannot see) remains the better-supported option; carving out 22-23
specifically is not backtest-justified. Re-verify once Watchman exits are modeled in the engine.

**Test set (2025-07-21 → 2026-07-21) NOT touched** — remained CONSUMED from EXP-003; this verdict
rests entirely on Train+Validation (rule 2). `config/base.yaml` NOT modified (analysis-only).
Auditor gate thresholds NOT touched (rule 8).

---

## EXP-005 2026-07-21 — M15 lower-timeframe as an entry/exit CONDITION (feasibility, NEW family)

Status: REJECT (no robust M15 entry-discrimination edge found) + roadmap finding for exits.
Analysis-only — no config/engine/council change. Test year NOT touched (new family, left pristine).

### 0. What this is / why a new family

User hypothesis (verbatim reasoning "เล่นเร็วออกเร็ว" = trade fast/get out fast): should
lower-timeframe data (start with M15) become an ADDITIONAL condition on the H1 strategy —
either as an entry confirmation/timing filter, or to enable a faster-than-H1 exit? This is a
genuinely NEW experiment family (M15-timing), NOT the session-window family — so its own
multiple-testing budget and its own (unspent) one-touch Test allowance. No `[adjustable]` M15
param exists in `config/base.yaml`; this is hypothesis-generation, not tuning an existing knob.

Entry mechanics an M15 condition would attach to (confirmed by reading
`council/decision_matrix.py` + `backtest/engine.py`): Council scores on H1 bar `i` (as_of =
close of bar `i`); `entry_price = close[i]`; the order fills at bar `i+1` OPEN. So the M15 bars
"visible at decision time" are exactly the 4 M15 bars composing H1 bar `i` (times
[sig_open, sig_open+1h)), which close at sig_open+1h = the fill-bar open. Features use only M15
bars with `time < entry_time` (no lookahead).

### 1. Data acquisition (Step 1)

Downloaded XAUUSD M15 (analysis target) + M5 (opportunistic) via the SAME generic
`feed.historical.download_historical(symbol, timeframe, days)` the CLI uses, called with an
explicit timeframe string from a scratch script — `config/base.yaml`'s `global.timeframe` (H1)
NOT mutated. The single 5-yr `copy_rates_range` call fails ([-2] Invalid params: per-request
history-depth cap), so a chunked pull (40-day windows for M15) + dedup/save in the identical CSV
schema was used (`scratchpad/dl_chunked.py`). M30 is unsupported without a code change (absent
from `feed.historical._TIMEFRAME_DELTA` / `feed.poller.TIMEFRAME_MAP`) — not pursued (out of
scope). Coverage caveat (material): the IC Markets demo terminal only caches intraday history to
a limited depth — M15 reaches back to 2022-04-28 (not H1's 2021-07-22), M5 only to 2025-02-20.
So the M15 analysis covers Train-PARTIAL (2022-04-28 to 2024-07-21, ~2.2 of 3 yr) + full
Validation; the 2021-07-22 to 2022-04-28 slice of Train has no M15 data. M5 is too shallow for
Train/Val use (grabbed only for future passes). Files: `data/historical/XAUUSD_M15.csv` (99,978
bars), `XAUUSD_M5.csv` (both gitignored per `.gitignore` `data/historical/*`). M15 verified clean:
exactly 4 bars/H1-hour, minutes {0,15,30,45}, and M15 OHLC aggregates BYTE-EXACT to H1 OHLC on
spot checks -> apples-to-apples confirmed.

### 2. Trade set (Step 2)

Ran the STOCK H1 engine + stock signal fn + cost model (commission $7/lot, slippage = bar's own
spread; Risk Voice OFF; tp 2.0; all-24h = the EXP-003-adopted live-equivalent) over
2022-04-28 to 2025-07-21 (Train-partial + Val; Test EXCLUDED). 651 trades, all with M15 context.
Fidelity: the Validation slice = 223 trades — IDENTICAL count to EXP-002/EXP-003's all-24h Y4
(223 tr), PF 1.07 vs 1.064 (trivial delta from a different equity-compounding start date). Harness
`experiments/m15_feature_harness.py` (per-trade JSONL); analyzers in scratchpad.

### 3. Pre-registered hypotheses + decision rule

Two mechanisms, pre-registered BEFORE scoring:
- H-entry (mechanisms a/b/c): some M15 structure in the last <=3h before the H1 signal closes
  discriminates winners from losers (or fast-winners from slow-grinders). Eight direction-signed
  M15 features: slope_1h, slope_2h, ema9-vs-21 alignment, consec bars in trade dir, last-bar
  range expansion/contraction, signal-dir rejection wick, RSI-aligned, position-in-range/extension.
- H-exit (mechanism d): losing H1 trades reach meaningful positive MFE before reversing to the
  stop, so a faster (M15-cadence) exit could bank it -> higher expectancy.
Deciding metric: expectancy in R + PF per bucket. A feature counts as a real edge ONLY if it
discriminates in the SAME direction on BOTH the Train-partial and the held-out Validation split
(out-of-sample respect), AND the per-value response is coherent/monotonic (not a median-placement
artifact), AND survives a per-year check, AND the surviving subset still clears the >=100-trade
floor. Multiple-testing: 8 features x {binary, tercile} ~= 16 bucket-tests -> a NEW-family count of
~16; demand cross-split consistency, not a single best aggregate.

### 4. Results — H-entry (mechanisms a/b/c): NO robust edge

Baseline (all-24h, this run): ALL 651 tr, wr 0.358, avgR 0.056, PF 1.073 | TRAIN 428 tr PF 1.074
| VAL 223 tr PF 1.070. Per-feature LO-vs-HI dAvgR (HI minus LO), TRAIN then VAL:

| feature          | TRAIN dAvgR | VAL dAvgR | verdict |
|------------------|-------------|-----------|---------|
| m15_slope_1h     | +0.118      | -0.108    | SIGN FLIP -> noise |
| m15_slope_2h     | -0.010      | -0.086    | weak; terciles: TRAIN best=low-slope, VAL best=high-slope -> OPPOSITE -> regime artifact |
| m15_ema_align    | -0.112      | +0.182    | SIGN FLIP (train: misaligned better; val: aligned better) -> noise |
| m15_consec       | +0.122      | +0.135    | same sign — investigated, see below |
| m15_range_exp    | -0.150      | +0.282    | HARD SIGN FLIP (train: contraction better; val: expansion better) -> classic regime artifact |
| m15_wick         | -0.144      | -0.002    | inconsistent/weak -> noise |
| m15_rsi_aligned  | -0.067      | +0.054    | SIGN FLIP -> noise |
| m15_pos_ext      | +0.030      | +0.055    | tiny; terciles U-shaped non-monotonic -> noise |

7 of 8 features flip sign across Train<->Val or are negligible — exactly the rate expected by
chance (each has ~50% sign-agreement odds; ~4 expected to agree). The ONE same-sign survivor,
m15_consec (consecutive M15 bars closing in the trade direction just before entry), DISSOLVES
under the per-exact-value check: response is non-monotonic noise — consec=0 PF 1.073 (fine),
consec=1 PF 0.842 (bad, n=133), =2 1.177, =3 0.930, =4 1.190, =5 1.792 (n=27), =7 0.685, =8 inf
(n=3). If "more M15 momentum = better" there would be a monotone ramp; instead it zig-zags. The
binary median split (cut between 1 and 2) only looked clean because it happened to isolate the
single bad consec=1 bucket into LO. "Exactly one M15 up-bar is uniquely bad" is not a
pre-registerable mechanism -> treated as a median-placement artifact, not an edge. Its VAL HI
subset is also n=89 (< 100 floor). REJECT H-entry.

### 5. Results — H-exit (mechanism d): give-back is real, but M15 is the wrong lever NOW

`scratchpad/mfe_harness.py` — per-trade MFE (H1 and M15) over the holding period, stop distance
re-derived from each trade's own r/net.

- Losers' MFE before stopping (M15): median 0.41R; 45.2% reach >=0.5R, 22.5% (94/418) reach
  >=1.0R, 10.3% reach >=1.5R favorable before reversing to the stop. 94 "give-back" trades
  (14.5% of all) reached >=1R then ended net -1.02R (mean MFE 1.47R). Real money on the table ->
  superficially supports a "fast out" mechanism.
- BUT a naive "+1R fast exit" (bank 1R if ever reached, else keep actual) LOWERS mean R from
  0.0563 to 0.0082. Capping winners destroys the +2R TP payoffs that carry the entire edge (all
  winners' MFE >= ~1.4R, median 1.91R). Classic cut-the-winners trap.
- The correct tool for give-back WITHOUT capping winners is a breakeven/trailing stop — i.e. the
  Watchman (`breakeven_at_r`, `trail_start_r`, `trail_distance_atr`). Two blockers make M15
  premature: (i) the harness runs here have `watchman_cfg=None` (Watchman NOT modeled), so this
  backtest OVERSTATES give-back — live H1 breakeven-at-R already protects many of the 94; (ii)
  winners are held LONGER than losers (bars_held median: TP 25h vs SL 12h; 25% of losers stop
  within 3h), so "fast out" fundamentally fights the 2R design — losers already exit fast; a
  faster exit mostly hurts winners. M15 faster-exit would be a refinement of a mechanism (H1
  Watchman) that is not even baselined yet -> wrong sequencing.

### 6. Robustness / multiple-testing honesty (self-flagged)

- Cross-split consistency was the primary guard and it eliminated 7/8 features outright — the
  discipline worked exactly as intended (mirrors how EXP-002's per-year bar caught tp-2.5).
- Regime-artifact risk is EXPLICIT in the data: m15_range_exp and m15_slope_2h literally reverse
  which bucket wins between the 2022-24 and 2024-25 regimes — any M15 filter tuned on one would
  have mis-generalized. Same failure mode this project has caught twice before.
- Multiple testing: ~16 bucket-tests in a new family. Finding one "consistent" feature
  (m15_consec) is fully expected by luck; it failed the coherence follow-up. No feature earns a
  config change.
- Coverage caveat: M15 Train is partial (from 2022-04-28); the 2021-22 choppy regime — the very
  one that broke tp-2.5 and the [14,18) filter in Y1 — is ABSENT from the M15 sample, so any M15
  edge that did appear would be LESS regime-tested than the H1 experiments, not more. Argues for
  extra caution, not less.

### 7. VERDICT — REJECT M15 as an entry condition; DEFER M15 exits behind H1-Watchman modeling

1. M15 pre-entry structure does NOT robustly discriminate H1 trade outcomes. 7/8 features flip
   sign out-of-sample; the 8th is a median artifact. An M15 entry-confirmation/timing filter has
   no evidenced edge and would mainly shrink trade count (worsening the 200-trade gate shortfall)
   and add signal-to-fill latency. No further M15 entry work recommended on current evidence.
2. The "fast out" instinct points at a real phenomenon (give-back: 22.5% of losers reach >=1R
   first) but at the WRONG tool. The lever is breakeven/trailing (the Watchman), not timeframe
   resolution. Correct roadmap order: (a) MODEL the existing H1 Watchman in the backtest engine
   (the engine now SUPPORTS `watchman_cfg`, but every experiment harness still passes None, so its
   value is unmeasured) and establish that baseline; (b) ONLY THEN ask whether M15-cadence stop
   management beats H1-cadence — a question that is meaningless until (a) exists.
3. Implementation cost/risk if pursued later (honest estimate): an M15-cadence Watchman is NOT a
   small add-on. It needs (i) a dual-timeframe data feed live + in backtest (M15 bars aligned to
   H1 positions), (ii) new pure decision-function work for M15 stop stepping, (iii) engine changes
   to drive Watchman on an M15 clock while entries stay H1, and (iv) the SAME live/backtest parity
   discipline this project already enforces for Risk Voice and Watchman. Sequenced strictly AFTER
   H1 Watchman is modeled and shown to add value.

Test set (2025-07-21 to 2026-07-21) NOT touched — new family, no candidate earned a Test
confirmation, so its one-touch budget is unspent and Test stays pristine. `config/base.yaml`,
`council/`, `backtest/engine.py` NOT modified (analysis-only). Auditor gate thresholds NOT touched
(rule 8). Harnesses: `experiments/m15_feature_harness.py` (committable); scratchpad analyzers +
`dl_chunked.py` are session-local.

---

## EXP-007 2026-07-21 — M30 lower-timeframe as an entry/exit CONDITION (feasibility, NEW family)

Status: REJECT (no robust M30 entry-discrimination edge found). Analysis-only — no
config/engine/council/feed change. Test year NOT touched (new family, left pristine).
Direct M30 replication of EXP-005's M15 study, requested by the user ("param-tuner สำรวจ M30").

### 0. What this is / prior / why a new family

User follow-up to EXP-005: run the SAME feasibility study for M30 instead of M15. This is a
genuinely NEW family (M30-timing) — its own multiple-testing budget and its own unspent
one-touch Test allowance. Honest prior stated BEFORE running (scratchpad/exp007_prereg.md):
M30 is COARSER than M15 (2 M30 bars per H1 bar vs 4 M15 bars), so it carries LESS intra-hour
information. M15 (higher-res) already showed no robust edge; a-priori M30 is LESS likely to
beat H1, not more. Expectation going in: clean rejection. Tested anyway (user asked; negative
results are results), framed around that prior rather than as a fresh coin-flip.

### 1. Data acquisition (Step 1) + coverage — MATERIALLY BETTER than M15

M30 is absent from the production `feed.poller.TIMEFRAME_MAP` / `feed.historical._TIMEFRAME_DELTA`
(EXP-005 flagged this). Since this experiment may NOT modify those feed modules, M30 was pulled
by a session-local scratch script (`scratchpad/dl_m30.py`) that replicates `download_historical`'s
logic (chunked `copy_rates_range`, dedup, still-forming-bar drop, identical CSV schema) but calls
`mt5.copy_rates_range` with `mt5.TIMEFRAME_M30` DIRECTLY — same precedent as EXP-005's
`dl_chunked.py`. `config/base.yaml`'s `global.timeframe` (H1) NOT mutated.

Coverage (the honest headline): M30 cache reaches back to **2020-06-22** — EARLIER than H1's own
2021-07-22 start, so **FULL H1 Train+Validation+Test coverage**, 71,860 bars. This REVERSES
EXP-005's M15 caveat: M15 only reached 2022-04-28 and MISSED the 2021-22 choppy regime (the one
that broke EXP-002's tp-2.5 and EXP-004's [0,22)); M30 INCLUDES it. So M30 is MORE regime-tested
than M15, which cuts both ways: a survivor here would be more trustworthy, but a rejection is also
more damning (it fails even with the hard 2021-22 regime present). Data validated: minutes {0,30}
only; 29,509/29,543 H1 hours have exactly 2 M30 bars (34 single-bar session-edge hours); M30 OHLC
aggregates BYTE-EXACT to H1 (max abs diff 0.0000 on open/high/low/close). File:
`data/historical/XAUUSD_M30.csv` (gitignored per `.gitignore` `data/historical/*`).

### 2. Trade set (Step 2)

STOCK H1 engine + stock signal fn + cost model (commission $7/lot, slippage = bar's own spread;
Risk Voice OFF; tp 2.0; all-24h = the EXP-003-adopted live-equivalent) over 2021-07-22 -> 2025-07-21
(FULL Train + Val; Test EXCLUDED). 809 trades, ALL with M30 context (skipped=0 — full coverage,
vs M15's 651 from its shorter window). Fidelity: Train slice = 587 trades — IDENTICAL to EXP-002's
all-24h Train (587); Val slice = 222 tr, PF 1.072 (EXP-005 M15 Val 223/1.070; EXP-002/003 all-24h
Y4 223/1.064) — apples-to-apples confirmed. Harness `experiments/m30_feature_harness.py` (committable,
adapted 1:1 from `m15_feature_harness.py` with all bar-counts re-derived in M30 units so the
wall-clock windows match: 1h=2 M30 bars, 2h=4, 3h context=6). Analyzers in scratchpad.

### 3. Pre-registered hypotheses + decision rule (committed BEFORE scoring — scratchpad/exp007_prereg.md)

Same 8 direction-signed features as EXP-005, recomputed at M30: slope_1h, slope_2h, ema9-vs-21
align, consec bars in trade dir, last-bar range expansion, signal-dir rejection wick, RSI-aligned,
position-in-range/extension. Deciding metric: expectancy in R + PF per bucket. A feature is a real
edge ONLY IF (a) SIGN-CONSISTENCY: dAvgR(HI-LO) same sign on Train AND Validation (primary filter);
(b) COHERENCE: per-exact-value/tercile response monotone & mechanistically sensible (the same
follow-up EXP-005 ran for m15_consec), not a median-placement artifact; (c) subset >=100 trades;
(d) PER-YEAR: not owed to a single year. Multiple-testing self-count: 8 features x {binary, tercile}
~= 16 bucket-tests for this M30 family (same as M15) + per-value/per-year follow-ups on the survivors.

### 4. Results — H-entry: NO robust edge (5 crude survivors, ALL dissolve under coherence)

Baseline (all-24h): ALL 809 tr avgR 0.0523 PF 1.08 | TRAIN 587 PF 1.080 | VAL 222 PF 1.072.
Binary median-split dAvgR (HI-LO), TRAIN then VAL:

| feature         | TRAIN dAvgR | VAL dAvgR | binary verdict |
|-----------------|-------------|-----------|----------------|
| m30_slope_1h    | +0.094      | -0.182    | SIGN FLIP -> noise |
| m30_slope_2h    | -0.027      | -0.001    | same-sign but VAL ~0 -> investigated |
| m30_ema_align   | +0.005      | +0.272    | TRAIN ~0 -> investigated |
| m30_consec      | +0.032      | +0.205    | same-sign -> investigated |
| m30_range_exp   | +0.093      | +0.151    | same-sign -> investigated |
| m30_wick        | -0.110      | -0.168    | same-sign -> investigated |
| m30_rsi_aligned | -0.046      | +0.036    | SIGN FLIP -> noise |
| m30_pos_ext     | +0.093      | -0.095    | SIGN FLIP -> noise |

At the crude binary level M30 shows MORE apparent survivors than M15 (5 vs 1) — precisely why
binary sign-agreement alone is NOT evidence (rule 7). Every one fails the pre-registered coherence
/ per-year follow-up:

- **slope_2h** — tercile response is OPPOSITE across splits: TRAIN monotone-DOWN (lo +0.086, mid
  +0.078, hi -0.007 -> more momentum worse), VAL hi is BEST (lo +0.003, mid -0.068, hi +0.206).
  Direction reverses between regimes — identical to M15's m15_slope_2h. Binary VAL effect was
  ~0 (-0.001) anyway. REJECT.
- **ema_align** — Train effect ~0 (align0 +0.048 vs align1 +0.053, delta +0.005). The Val "edge"
  (+0.272) is a tiny-n artifact: it's carried by the misaligned bucket being very negative
  (align0 n=36, avgR -0.181), concentrated in 2025 (align0 n=17, -0.306). Per-year sign is
  NEGATIVE in 2022 (delta -0.012). Negligible on Train + small-n one-year Val artifact. REJECT.
- **consec** — wild per-value zig-zag that sign-flips between splits: consec=1 TRAIN +0.105 (good)
  vs VAL -0.351 (terrible); consec=2 TRAIN +0.089 vs VAL -0.284; consec=6 TRAIN -0.202 vs VAL
  +0.493 (n=6). No monotone mechanism — the exact failure mode EXP-005 documented for m15_consec.
  The binary median split looked "same-sign" only by accident of where the cut fell. REJECT.
- **range_exp** — the closest to real: TRAIN terciles monotone-UP (lo -0.019, mid +0.076, hi
  +0.100). But VAL is NON-monotone (lo +0.017, mid -0.042, hi +0.166 — mid dips), and the per-year
  HI-LO decomposition shows it owes to TWO years: 2021 +0.330, 2024 +0.349; it goes NEGATIVE in
  2023 (-0.014) and 2025 (-0.042, the tail of the Val window). Remove 2021+2024 and it's flat-to-
  negative. Fails criterion (d) (single-regime dependence) and (b) (non-monotone in Val). At M15
  this same feature was a HARD sign-flip; at M30 it's directionally more consistent in aggregate
  but per-year exposes it as regime-carried, not a stable mechanism. REJECT.
- **wick** — TRAIN inverted-U (lo +0.055, mid +0.153, hi -0.051 -> non-monotone), VAL essentially
  FLAT (lo +0.045, mid +0.058, hi +0.039 -> no discrimination). No real Val effect. REJECT.

Survivors of the FULL pre-registered bar (sign + coherence + per-year): **NONE.** Same clean
rejection as M15 — reached through the coherence/per-year layer rather than the crude sign-check.

### 5. H-exit (give-back / faster exit) — unchanged, resolution-independent

Not re-run at M30 cadence. EXP-005's exit finding (give-back is real — 22.5% of losers reach >=1R
first — but the correct lever is the Watchman's breakeven/trailing, NOT timeframe resolution, and
a naive fast-exit caps the +2R winners that carry the whole edge) is resolution-INDEPENDENT: M30
cadence is COARSER than M15, so if M15-cadence give-back capture was the wrong lever, M30 is a
fortiori. The sequencing conclusion (model the H1 Watchman baseline FIRST, then reconsider any
lower-timeframe exit) is unchanged and not something an M30 pass could overturn.

### 6. Robustness / multiple-testing honesty (self-flagged)

- Cross-split sign-consistency + per-value/per-year coherence was the primary guard; it eliminated
  all 8 features. The crude binary layer alone would have "found" 5 — a concrete demonstration that
  sign-agreement on one median split is noise; the discipline is what separates it (rule 5/7).
- Regime-artifact risk is EXPLICIT again: slope_2h and consec literally reverse per-value ranking
  between the 2021-24 and 2024-25 regimes; range_exp reverses per YEAR. Same failure mode this
  project has now caught five times (EXP-001 C4, EXP-002 tp-2.5, EXP-004 [0,22), EXP-005 M15, here).
- Multiple testing: ~16 bucket-tests this new family; expecting ~4/8 to agree in sign by chance —
  observed 5 crude same-sign, fully consistent with luck, none survived coherence. No config change.
- Coverage: UNLIKE M15, M30 covers the full 2021-22 choppy regime — so this rejection is the
  STRONGER of the two (M30 failed to discriminate even WITH the hard regime in-sample), not weaker.

### 7. VERDICT — REJECT M30 as an entry condition; DEFER M30 exits behind H1-Watchman modeling

1. M30 pre-entry structure does NOT robustly discriminate H1 trade outcomes. 3/8 features sign-flip
   outright on the binary split; the other 5 pass the crude sign-check but every one dissolves under
   the per-exact-value/tercile + per-year coherence follow-up (opposite tercile ranking, ~0 Train
   effect, per-value zig-zag, or single-regime dependence). No M30 entry-confirmation/timing filter
   has an evidenced edge; as with M15 it would mainly shrink trade count (worsening the 200-trade
   gate shortfall) and add signal-to-fill latency. This REINFORCES EXP-005, and does so on STRONGER
   footing (full-Train coverage incl. 2021-22). The coarser-than-M15 prior held: M30 did not do
   better; it did (crudely) look noisier, and cleaned up to the same "nothing" under discipline.
2. Sequencing UNCHANGED: (a) MODEL the existing H1 Watchman in the backtest engine and baseline it
   (the concurrent EXP-006 Watchman-parameter sweep is the right next step); (b) ONLY THEN ask
   whether ANY lower-timeframe (M15 or M30) stop management beats H1-cadence. A lower-timeframe exit
   remains meaningless until (a) exists. Nothing in the M30 result advances or reorders that roadmap.

Test set (2025-07-21 -> 2026-07-21) NOT touched — new family, no candidate cleared the pre-registered
Train/Val bar, so the one-touch budget is UNSPENT and Test stays pristine (mirrors M15 — did not fish
for a story). `config/base.yaml`, `council/`, `backtest/engine.py`, `feed/` NOT modified (analysis-
only). Auditor gate thresholds NOT touched (rule 8). Harness: `experiments/m30_feature_harness.py`
(committable); `scratchpad/dl_m30.py` + analyzers + `exp007_prereg.md` are session-local.

---

## EXP-006 2026-07-21 — Watchman exit-management parameters (NEW family, first-ever measurable)

Status: REJECT all 5 (defaults on the plateau). Analysis-only — no config/engine change. Test year
NOT touched (no candidate cleared the Train+Val bar robustly; new family's one-touch budget UNSPENT).
Appended after EXP-007 per append-only discipline (numbered 006, written later than 007).

### 0. Why this is a NEW family and why it matters now

As of engine commit 67df406 (today), `backtest/engine.py` ACTUALLY simulates Watchman's exit
management (breakeven -> SL to entry; ATR-trailing stop; structure-invalidation; time-stop/dead-trade)
when a `WatchmanConfig` is passed, and `scripts/run_backtest.py` now ALWAYS builds one from
`config/base.yaml`'s `watchman:` block. Every prior experiment (EXP-001..005) ran `watchman_cfg=None`
(confirmed in EXP-002 §0). So for the FIRST time these five parameters genuinely move backtest output.
New family => own multiple-testing budget + own UNSPENT one-touch Test allowance. Params, causal role
verified in `watchman/stop_logic.py` + `exit_conditions.py`: breakeven_at_r (profit>=R -> SL to entry),
trail_start_r (profit>=R -> begin ATR trail), trail_distance_atr (ATR distance of trailing stop),
time_stop_hours (dead-trade timeout), dead_trade_r_band (+/-R band defining "dead"). Structure
invalidation is always-on and NOT tunable via these 5 — held fixed.

Pre-registration committed BEFORE observing any sweep result (scratchpad/exp006_prereg.md, written
right after baseline reproduction, before any sweep file was read). Grid, deciding metric and full
acceptance criteria (a)-(e) reproduced in §3-§4 below verbatim from that file.

### 1. Baseline reproduction (Step 1) — CONFIRMED

`scripts/run_backtest.py XAUUSD --out-of-sample --start-date 2025-07-21 --end-date 2026-07-21
--commission-per-lot 7.0` reproduced the session baseline to the cent: **243 trades, PF 1.21,
net +$1,121.45, maxDD 3.81%, PF-excl-top5 1.11** (Gate 1 fails only on PF>=1.3; all else passes).
This is the CURRENT-defaults Test run — a "where we stand" measurement, NOT a parameter search, so it
does NOT spend the family's one-touch Test budget. Harness `experiments/watchman_param_harness.py`
(builds the SAME RiskVoiceConfig + WatchmanConfig from base.yaml as the CLI, cached IC Markets
SymbolSpec, cost model commission $7/lot + slippage = bar's own spread) reproduces the CLI byte-for-
byte; used for all in-process sweeping.

### 2. THE HEADLINE FINDING — modeling Watchman at current defaults DEGRADES the backtest edge

Baseline WITH Watchman modeled (current defaults) vs the EXP-002/003 Watchman-OFF (all-24h, tp2.0)
per-year figures already on record:

| Year          | Watchman ON (defaults) PF / net | Watchman OFF (EXP-002/3) PF / net |
|---------------|----------------------------------|-----------------------------------|
| Y1 2021-22 Tr | 0.9615 / -273   (DD 9.25)        | 1.050 / +297                      |
| Y2 2022-23 Tr | **0.8758 / -911** (DD 17.32)    | 1.001 / +5                        |
| Y3 2023-24 Tr | 1.0996 / +647   (DD 7.54)        | 1.224 / +1294                     |
| Y4 2024-25 Val| 0.9884 / -74    (DD 6.43)        | 1.064 / +404                      |
| Y5 2025-26 Test| 1.21 / +1121   (DD 3.81)        | 1.277 / +1458                     |
| Train 3yr agg | 0.9922 / -156   (DD 17.21)      | 1.084 / +1544 (EXP-002 §5.1)      |

Watchman's exit management, as specced, is net-HARMFUL in the backtest in EVERY window: it turns 3 of
5 years net-negative (Y1, Y2, Y4), makes Y2 a -$911 / DD 17.3% disaster, and lowers PF vs Watchman-off
even in the two profitable years (Y3, Y5). Mechanism: breakeven-to-entry + ATR-trail + dead-trade
time-stop keep exiting positions before the 2R TP can carry the winners that fund the edge (same
"cut-the-winners" phenomenon EXP-005 §5 flagged for a naive +1R exit), while also churning more trades
(Train count 587 -> 852). CAVEAT: the backtest cannot value Watchman's LIVE protections (news,
connectivity, structure breaks it does catch), so this is NOT a recommendation to disable Watchman
live. It IS strong evidence that the tunable exit params (breakeven/trail/time-stop/dead-trade) do not
add backtest value and mostly subtract it — a strategy-level finding for the roadmap, above parameter
tuning. This EXP tunes within that losing band; none of the 5 params recovers the Watchman-off edge.

### 3. Grid (one-factor-at-a-time, others at current default; pre-registered)

breakeven_at_r {0.75, 1.0*, 1.25, 1.5} | trail_start_r {1.25, 1.5*, 1.75, 2.0} | trail_distance_atr
{0.75, 1.0*, 1.25, 1.5} | time_stop_hours {24, 36, 48*, 72} (+refinement {18, 30}) | dead_trade_r_band
{0.2, 0.3*, 0.4}. (* = current default.) One refinement pass, spent on time_stop's short end (§6).
Family multiple-testing count = 16 non-default configs evaluated (< 20 threshold, rule 7).

### 4. Acceptance criterion (pre-registered, verbatim). Deciding metric: Val PF, gated by trades>=100
and avg_r. ADOPT candidate V over default iff ALL: (a) Val trades>=100 AND PF(V)>=PF(default) AND
avg_r(V)>=avg_r(default); (b) plateau — V's +/-1 grid-step neighbors on Val within ~15% PF (reject
isolated peaks, rule 5); (c) Train consistency — PF(V)>=PF(default) on Train; (d) per-year — V must not
turn any default-positive year net-negative AND PF(V)>=default in a MAJORITY of Y1..Y4 (the bar that
killed EXP-002 tp2.5 and EXP-004 [0,22)); (e) Test (touched once, only for the single best candidate
clearing a-d) confirms (a). Else REJECT. Gates NOT touched (rule 8). config NOT modified (analysis-only).

### 5. Sweep results — Train PF / Val PF (baseline: Train 0.9922, Val 0.9884, Val avgR 0.0048)

| param \ value        | (low)            | default          | (high)                     | shape on Val |
|----------------------|------------------|------------------|----------------------------|--------------|
| breakeven_at_r       | .75: .982/1.002  | 1.0: .992/.988   | 1.25:1.031/.994  1.5:1.026/1.000 | FLAT band .988-1.002; Train up as it relaxes |
| trail_start_r        | 1.25:.971/1.031  | 1.5: .992/.988   | 1.75:1.007/.973  2.0:1.005/.989  | ANTI-CORRELATED Train vs Val |
| trail_distance_atr   | .75:.983/.972    | 1.0: .992/.988   | 1.25:.995/1.001  1.5:.994/1.011  | Val monotone up as trail loosens; Train ~flat |
| time_stop_hours      | 18:1.032/.939 24:1.008/1.009 30:1.016/1.028 | 48:.992/.988 | 36:.982/.972  72:.965/.979 | JAGGED: bump at 24-30, both neighbors (18,36) sub-baseline |
| dead_trade_r_band    | .2:1.009/.962    | .3: .992/.988    | .4:1.002/.990              | Val: .2 hurts, .4 ~= baseline |

Configs passing BOTH (a) Val+ and (c) Train+ on aggregate: breakeven 1.5 (Val .9996 marginal),
trail_start 2.0 (Val .9889 ultra-marginal), trail_distance 1.25 & 1.5, time_stop 24 & 30. These went to
the per-year gate (d) and/or plateau refinement (b).

### 6. Robustness — per-year (d) + plateau refinement (b) killed every survivor

Per-year PF for the aggregate-passers (baseline per-year: Y1 .9615, Y2 .8758, Y3 1.0996, Y4 .9884):

| candidate            | Y1     | Y2 (net)         | Y3     | Y4/Val | verdict |
|----------------------|--------|------------------|--------|--------|---------|
| baseline (48h etc.)  | .9615  | .8758 (-911)     | 1.0996 | .9884  | —       |
| trail_distance 1.5   | .9871  | **.8721 (-931)** | 1.0984 | 1.0114 | FAIL (d): Val edge is Y4-only artifact; Y2 WORSE than baseline & still -$931; PF>=base in only 2/4 yr (tie, not majority) |
| time_stop 24         | 1.0109 | .9344 (-531)     | 1.0675 | 1.0088 | passes (d) 3/4 — but see plateau |
| time_stop 30         | .9907  | .9205 (-617)     | 1.1113 | 1.0278 | passes (d) 4/4 — but see plateau |
| time_stop 18 (refine)| 1.0225 | .9619 (-321)     | 1.1000 | **.9388**| Val sub-baseline |

- **trail_distance 1.5** (the cleanest aggregate signal): its +2.3% Val PF is a SINGLE-YEAR (Y4)
  artifact. Per-year it does NOT beat baseline in a majority (Y2 and Y3 slightly worse; only Y1, Y4
  better = 2/4 tie), and Y2 stays a -$931 loser. FAIL (d). Same regime-artifact trap as tp2.5.
- **time_stop 24 & 30** looked strongest: both beat baseline on Train AND Val AND per-year (30 beats
  baseline in ALL 4 years; 24 in 3/4), and RAISE trade count toward the 200-gate (Val 296-315 vs 282).
  A less careful pass would ADOPT time_stop=30 and spend Test on it. But the plateau refinement (rule 5)
  is decisive: the Val response is a narrow BUMP at 24-30 flanked by SUB-BASELINE values on BOTH sides
  — 18h -> .9388 (below baseline, avgR negative) and 36h -> .9721 (below baseline). A robust
  "shorter time-stop is better" would be monotone; instead 18<24<30>36<48>72 is jagged, the per-year
  optimum wanders (Y1 prefers 24, Y3 prefers 30), and 36h being the WORST value tested has no coherent
  mechanism. The 24-30 goodness is a sample-specific interaction of dead-trade exit timing with the
  one-position-at-a-time entry sequence — a non-robust isolated peak, not a plateau. FAIL (b). Also:
  even at its best (30h) Y2 is still PF .92 / -$617 and Train DD 15.1% (AT the gate ceiling) — the
  change does not fix the core problem and pushes DD to the limit.
- **breakeven, trail_start, dead_trade**: no robust edge. breakeven Val is a flat 0.988-1.002 noise
  band (its only real signal is Train, which anti-correlates with the flat Val = regime artifact).
  trail_start is perfectly anti-correlated across splits (1.25 good Val/bad Train; 1.75/2.0 reverse) —
  textbook noise. dead_trade shows no Val improvement (0.2 hurts Val to .962, 0.4 ~= baseline).

Multiple-testing honesty (rule 7): 16 configs evaluated; the whole family's Val PF lives in a ~0.94-1.03
band (baseline .988), i.e. a noise SD ~0.02-0.03. The best survivor's edge (time_stop 30, +0.04 Val PF)
is ~1.5-2 noise-SD as the max of 16 noisy draws — exactly the winner's-curse magnitude, and it fails
the plateau refinement. No result justifies a change.

### 7. VERDICT — REJECT all 5; keep current Watchman defaults. Test set NOT touched.

Keep breakeven_at_r 1.0 / trail_start_r 1.5 / trail_distance_atr 1.0 / time_stop_hours 48 /
dead_trade_r_band 0.3. No parameter delivers a robust, plateau-backed, per-year edge over the current
defaults: the aggregate "winners" are either anti-correlated across Train/Val (breakeven, trail_start,
dead_trade), a single-year artifact (trail_distance 1.5), or a jagged isolated bump (time_stop 24-30,
both neighbors sub-baseline). Per rule 5 (plateau beats peak), rule 7 (multiple-testing), rule 9
(negative results are results): **the current defaults are on the (noisy, flat) plateau — no change
recommended.**

I did NOT spend the new family's one-touch Test budget. The closest candidate (time_stop=30) failed the
plateau refinement on Train+Val, so per rule 2 it did not earn a Test confirmation and I refused to
touch Test to fish for a better story (mirrors EXP-005/007). **Test (2025-07-21 -> 2026-07-21) stays
PRISTINE for this family**, apart from the current-defaults "where we stand" baseline in §1 (not a
parameter candidate).

The dominant, roadmap-level result is §2, not any single parameter: Watchman's exit management is
net-negative in the backtest at these defaults AND across the entire tested neighborhood. Parameter
tuning cannot fix it — it shuffles performance within a sub-Watchman-off band. Recommended next step
(user's call, NOT a config change): treat this as a STRATEGY question — either (i) reconsider whether
the breakeven/trail/dead-trade logic suits an H1 fixed-2R system (it appears to cut the winners that
carry the edge), or (ii) keep Watchman purely for its live protections (news/connectivity/structure)
while accepting it is backtest-neutral-to-negative — but do NOT tune these 5 knobs expecting an edge.
Interaction noted but not swept jointly (per house one-factor practice): breakeven_at_r, trail_start_r
and tp_r_multiple(=2.0) jointly gate WHEN protection engages relative to the 2R target; a joint
breakeven x trail_start x tp grid is the only place a genuinely different Watchman regime might live,
but it is high-dimensional/high-overfit-risk and should follow a structural rethink, not precede it.

`config/base.yaml`, `council/`, `backtest/engine.py`, `feed/` NOT modified (analysis-only). Auditor
promotion-gate thresholds NOT touched (rule 8). Harness: `experiments/watchman_param_harness.py`
(committable, reusable for future Watchman sweeps); `scratchpad/exp006_{driver,resume,analyze}.py` +
`exp006_prereg.md` + `_exp006_all.jsonl` raw results are session-local.

---

## EXP-008 2026-07-22 — Watchman sub-mechanism ISOLATION + joint be/trail retune (CONTINUATION of EXP-006's family)

Status: PRE-REGISTERED (running). Continuation of the Watchman exit-management family opened
by EXP-006 — NOT a new family (shares EXP-006's multiple-testing budget AND its one-touch Test
allowance). Analysis-only: no `config/base.yaml`, `council/`, `backtest/engine.py`, `feed/` edits.

### 0. Why this experiment / relation to EXP-006

EXP-006 found (its §2 headline) that modeling Watchman at CURRENT DEFAULTS is net-HARMFUL in the
backtest in every window (3/5 years net-negative; Y2 −$911/DD17.3%), and that one-factor-at-a-time
tuning of the 5 params recovers nothing (all REJECT). Its §7 explicitly recommended, as the only
remaining place a different Watchman regime might live, a JOINT breakeven_at_r × trail_start_r × tp
grid — and flagged the diagnosed mechanism: breakeven_at_r (1.0) and trail_start_r (1.5) both sit
BELOW tp_r_multiple (2.0), so protection engages on nearly every winner well before the 2R target,
"cutting the winners that carry the edge." The user's instruction ("ลองดู เงื่อนไขไหนมีปัญหาตัดออกได้เลย"
= "whichever condition has a problem, cut it out") authorizes testing FULL DISABLE of individual
Watchman sub-mechanisms as explicit candidate conditions, not just retuned thresholds.

### 0a. Mechanism-isolation method (verified by reading watchman/{evaluate,stop_logic,exit_conditions}.py)

`evaluate_watchman` runs 3 sub-mechanisms in priority order: (1) structure-invalidation → CLOSE,
(2) time-stop/dead-trade → CLOSE, (3) breakeven+ATR-trail → MODIFY_SL. Disable-ability via config
(WatchmanConfig) ALONE, no engine edit:
- **breakeven** — fires when `profit_r >= breakeven_at_r`; set `breakeven_at_r = 1e9` → never fires.
- **trailing** — fires when `profit_r >= trail_start_r`; set `trail_start_r = 1e9` → never fires.
- **time-stop** — fires when age `>= time_stop_hours`; set `time_stop_hours = 1e9` → never fires.
- **structure-invalidation** — HAS NO CONFIG GATE. `check_structure_invalidation` is called
  unconditionally in `evaluate_watchman`; it is ALWAYS ON whenever `watchman_cfg is not None`. The
  ONLY way to turn it off is `watchman_cfg=None`, which also turns off everything else. **Key design
  finding:** structure-invalidation cannot be selectively cut without an engine code change.

Consequence for design: structure-invalidation is the common baseline of every non-None condition.
I therefore ISOLATE each mechanism's MARGINAL effect by differencing, using a 5-cell design (below).
This still fully answers "which mechanism is the harmful piece" — structure's own marginal harm is
`StructOnly − OFF`; be/trail's is `(+BTrail) − StructOnly` (and `AllDefaults − (+Time)`); time's is
`(+Time) − StructOnly` (and `AllDefaults − (+BTrail)`) — two independent estimates each, which also
exposes any interaction.

### 1. Hypotheses (pre-registered, committed BEFORE observing StructOnly/+BTrail/+Time results)

- H1 (isolation): EXP-006's diagnosis says breakeven+trail is the "cuts winners" culprit. So
  `Struct+BTrail` should be the WORST non-OFF condition, and `StructOnly` / `Struct+Time` should be
  CLOSER to OFF. If instead structure-invalidation or time-stop are ALSO net-harmful on their own
  marginal, that reorders the "cut this" recommendation.
- H2 (Stage-2 retune): IF be/trail is worth keeping at all (i.e. its marginal harm is small/regime-
  dependent, not uniform), then moving breakeven_at_r/trail_start_r UP toward the 2R target (so
  protection engages only once a trade is already near target) should recover the lost edge. IF
  Stage-1 shows be/trail is uniformly net-harmful even before retuning, Stage 2 is expected to fail
  and the recommendation is to CUT be/trail, not retune it.

### 2. Data splits (reuse EXP-001..006; chronological, no shuffling)

| Split | Range | use |
|-------|-------|-----|
| Train | 2021-07-22 → 2024-07-21 | per-year Y1,Y2,Y3 + 3yr aggregate |
| Validation | 2024-07-21 → 2025-07-21 | Y4 |
| Test | 2025-07-21 → 2026-07-21 | **budget = EXP-006's UNSPENT one-touch** (EXP-006 §7 did NOT spend it). Touch ONCE only if ONE clearly-best candidate clears the full Train+Val bar. |

### 3. Conditions

**Stage 1 — mechanism isolation (5 cells; OFF & AllDefaults are known reference points, not new bets):**
| id | breakeven_at_r | trail_start_r | time_stop_hours | mechanisms active |
|----|----------------|---------------|-----------------|-------------------|
| OFF | (watchman_cfg=None) | — | — | none (= EXP-002/003 all-24h) |
| AllDefaults | 1.0 | 1.5 | 48 | S + BE/Trail + Time (= EXP-006 baseline) |
| StructOnly | 1e9 | 1e9 | 1e9 | S only |
| Struct+BTrail | 1.0 | 1.5 | 1e9 | S + BE/Trail |
| Struct+Time | 1e9 | 1e9 | 48 | S + Time |
(trail_distance_atr=1.0, dead_trade_r_band=0.3 fixed throughout.)

**Stage 2 — joint breakeven_at_r × trail_start_r grid (run ONLY if Stage 1 shows be/trail worth
keeping, i.e. not uniformly net-harmful):** be ∈ {1.0,1.5,1.75} × trail ∈ {1.5,1.75,2.0}, constraint
trail ≥ be (design-implied ordering; verified code permits but doesn't require it) → 8 cells (7 new
beyond the be1.0/trail1.5 default). trail_distance_atr=1.0, time_stop=48, dead=0.3 fixed.

### 4. Multiple-testing accounting (rule 7) — HIGHER BAR because joint grids

Watchman-family cumulative count: EXP-006's 16 + Stage-1's 3 genuinely-new (StructOnly, +BTrail,
+Time) + Stage-2's up-to-7 new = up to **26 configs (> 20 threshold)**. Per rule 7 I therefore demand
a LARGER edge and rely on per-year robustness across ALL 4 Train+Val years with NO sign flips (not an
aggregate) — joint grids carry more overfitting risk than one-factor sweeps, stated explicitly.

### 5. Acceptance criteria (pre-registered)

Deciding metric: per-year PF + net $ vs the RELEVANT baseline = **OFF (Watchman-off all-24h)**, since
EXP-006 already established AllDefaults < OFF. A condition/candidate is "worth keeping / ADOPT" iff ALL:
- (a) trades ≥ 100 every year (Y1–Y4);
- (b) does NOT turn any OFF-positive year net-negative (OFF is +ve all 4 yrs: Y1 +297, Y2 +5, Y3
  +1294, Y4 +404) — the EXP-002 tp2.5 / EXP-004 [0,22) failure mode;
- (c) PF ≥ OFF in a MAJORITY of Y1–Y4 AND not materially worse (PF gap > 0.03) in ANY year;
- (d) plateau (Stage 2): the winning be/trail cell's ±1-grid-step neighbors within ~15% PF (reject
  isolated peaks);
- (e) Test (touched once) confirms — ONLY for a single clearly-best survivor of (a)–(d).
Mechanism-cut recommendation logic: a sub-mechanism is "CUT IT" iff removing it (the differenced
marginal) improves or is net-neutral vs keeping it, robustly per-year. Else "keep."
If NOTHING clears (a)–(d) vs OFF, verdict = "no fix found via subset/retune; strategy-level rework"
(EXP-006 §7's flagged possibility). Auditor gate thresholds NOT touched (rule 8). config NOT modified.

### 6. Results

Harness: `experiments/watchman_param_harness.py`'s `run_slice` reused unchanged, driven by
`scratchpad/exp008_driver.py` which builds arbitrary WatchmanConfig (or None) per condition
(sentinel 1e9 disables be/trail/time as described in §0a). Cost model on (commission $7/lot,
slippage = bar's own spread), Risk Voice from base.yaml (all-24h [0,24)). Per-year windows only
(decisive metric is per-year; `val`==`y4`; the 3yr aggregate omitted — its O(n^2) Watchman-eval
cost ≈ 3× y1+y2+y3 combined, no extra information over the per-year rows).

**IMPORTANT baseline note (differs from EXP-006 §2's cross-reference):** EXP-006 §2 compared its
Watchman-ON baseline against EXP-002/003's Watchman-OFF numbers — but those EXP-002/003 runs had
Risk Voice OFF (`risk_voice_cfg=None`), whereas EXP-006's and this experiment's runs have Risk
Voice ON (from base.yaml). That cross-comparison therefore carried a Risk-Voice confound. THIS
experiment removes it: all 5 Stage-1 conditions share the SAME Risk-Voice-ON config; ONLY the
Watchman mechanism varies, so every comparison here is clean apples-to-apples. Fidelity confirmed:
`AllDefaults` reproduces EXP-006's baseline to the cent (Y1 0.9615/−273, Y2 0.8758/−911, Y3
1.0996/+647, Y4 0.9884/−74). The fresh Risk-Voice-ON, Watchman-OFF anchor (`OFF`) is the correct
baseline for this family and is used as such below (it differs from EXP-002/003's Risk-Voice-OFF
figures — e.g. Y2 is already −$499 here at the entry level, before any Watchman).

#### 6.1 Stage 1 — mechanism isolation (per-year PF / net $ / trades)

| cond | Y1 21-22 | Y2 22-23 | Y3 23-24 | Y4/Val 24-25 | AGG net |
|------|----------|----------|----------|--------------|---------|
| **OFF** (no watchman) | 1.038 / +222 / 197 | 0.927 / −499 / 212 | 1.136 / +749 / 177 | 1.046 / +294 / 227 | **+766** |
| **StructOnly** (S) | 1.058 / +343 / 200 | 0.981 / −125 / 206 | 1.139 / +763 / 177 | 1.028 / +176 / 230 | **+1158** |
| **Struct+Time** (S+T) | 1.022 / +156 / 271 | 0.959 / −314 / 260 | 1.148 / +901 / 229 | 1.051 / +337 / 263 | **+1080** |
| **Struct+BTrail** (S+BE/Trail) | 0.971 / −187 / 269 | 0.920 / −565 / 254 | 1.126 / +763 / 229 | 0.941 / −389 / 269 | **−378** |
| **AllDefaults** (S+BE/Trail+T) | 0.962 / −273 / 300 | 0.876 / −911 / 285 | 1.100 / +647 / 264 | 0.988 / −74 / 282 | **−611** |

Marginal effect of each mechanism (net $ difference, per year | aggregate), two independent
estimates each where possible:

| mechanism | Y1 | Y2 | Y3 | Y4 | AGG | read |
|-----------|----|----|----|----|-----|------|
| structure-invalidation (StructOnly − OFF) | +121 | +374 | +14 | −118 | **+392** | net-BENEFICIAL: softens the Y2 disaster, helps Y1/Y3, tiny Y4 cost |
| BE/trail estA (S+BTrail − StructOnly) | −530 | −440 | −1 | −565 | **−1535** | UNIFORMLY, SEVERELY HARMFUL |
| BE/trail estB (AllDefaults − S+Time) | −429 | −598 | −254 | −411 | **−1691** | UNIFORMLY, SEVERELY HARMFUL (both estimates agree) |
| time-stop estA (S+Time − StructOnly) | −187 | −189 | +138 | +161 | **−78** | roughly NEUTRAL (mixed sign) |
| time-stop estB (AllDefaults − S+BTrail) | −86 | −346 | −115 | +314 | **−233** | roughly NEUTRAL-to-slightly-negative (mixed) |

**Stage 1 verdict — the harmful piece is breakeven+trailing, isolated and confirmed.**
breakeven+trailing is net-harmful in BOTH independent marginal estimates (−$1,535 and −$1,691
aggregate) and in essentially every single year (Y1 −429/−530, Y2 −440/−598, Y4 −411/−565; Y3
neutral-to-−254) — the exact "cuts the winners before the 2R target" mechanism EXP-006 §2
diagnosed, now proven by direct isolation. structure-invalidation, by contrast, is net-BENEFICIAL
(+$392; it beats OFF in 3/4 years and softens the Y2 loss from −499 to −125). time-stop is roughly
neutral (small, sign-inconsistent). The single best condition is **StructOnly** (+$1,158 agg, PF ≥
OFF in 3/4 yrs, no OFF-positive year turned negative → PASSES the §5 (a)-(c) ADOPT bar vs OFF).

#### 6.2 Stage 2 — joint breakeven_at_r × trail_start_r retune (trail ≥ be; dist 1.0, time 48, dead 0.3)

| cell | Y1 | Y2 | Y3 | Y4 | AGG net | PF≥OFF yrs |
|------|----|----|----|----|---------|-----------|
| be1.0 tr1.5 (=AllDefaults) | 0.962/−273 | 0.876/−911 | 1.100/+647 | 0.988/−74 | −611 | 0/4 |
| be1.0 tr1.75 | 0.984/−109 | 0.907/−673 | 1.115/+723 | 0.973/−171 | −231 | 0/4 |
| be1.0 tr2.0 | 0.996/−26 | 0.895/−749 | 1.104/+641 | 0.989/−71 | −206 | 0/4 |
| be1.5 tr1.5 | 0.980/−144 | 0.925/−577 | 1.175/+1152 | 1.000/−3 | +428 | 1/4 |
| be1.5 tr1.75 | 0.992/−58 | 0.955/−339 | 1.162/+1034 | 0.990/−63 | +574 | 2/4 |
| be1.5 tr2.0 | 1.001/+8 | 0.943/−418 | 1.146/+902 | 1.002/+12 | +504 | 2/4 |
| **be1.75 tr1.75** | 1.010/+69 | 0.970/−228 | 1.164/+1051 | 1.039/+252 | **+1143** | 2/4 |
| be1.75 tr2.0 | 1.022/+156 | 0.963/−280 | 1.144/+895 | 1.051/+337 | +1108 | 3/4 |

**Stage 2 verdict — the retune confirms the diagnosis but does NOT salvage be/trail.** Raising
breakeven_at_r monotonically shrinks the harm (be1.0 rows −611/−231/−206 → be1.5 rows
+428/+574/+504 → be1.75 rows +1143/+1108): breakeven at 1.0R is the dominant poison; pushing it to
1.75R (just below the 2.0 TP) recovers ~$1,750 aggregate. This is exactly EXP-006's "protection
engages too early" mechanism. BUT the best cell **be1.75_tr1.75 (+$1,143) only reaches PARITY with
StructOnly (+$1,158, i.e. be/trail simply OFF) — it never beats it**, and the response is
monotone-increasing toward the grid edge (later breakeven always better, extrapolating to be→∞ =
disabling breakeven). That is NOT an interior plateau (rule 5): its neighbor be1.5_tr1.75 is +$570
LOWER, and the "optimum" is asymptotically approaching the cut-it condition from below. Mechanism:
be=1.75 barely ever triggers before the 2.0R TP, so it is nearly equivalent to no breakeven — the
grid's own trend POINTS AT REMOVAL, not retuning. Also, StructOnly best-protects the disaster year
(Y2 −125, better than every Stage-2 cell's best of −228). No retuned be/trail pair earns adoption
over simply cutting be/trail.

#### 6.3 Multiple-testing honesty (rule 7)

Watchman-family cumulative count = EXP-006's 16 + Stage-1's 3 new + Stage-2's 7 new = **26 (> 20)**,
so I hold the higher bar (rule 7). The decisive result does NOT rest on picking a best-of-26 peak
(that would be winner's-curse): it rests on (i) a MECHANISM REMOVAL whose marginal harm is
consistent across all 4 years AND two independent estimates (the opposite of curve-fitting — I am
deleting a lever, not fitting one), and (ii) the retune grid's monotone-to-the-edge trend
independently pointing at the same removal. The candidate (cut be/trail) is chosen by mechanism, not
by its rank among noisy configs.

#### 6.4 Test-confirmation — PRE-REGISTERED before touching Test (spending EXP-006's UNSPENT budget)

Single clearly-best candidate that earns the family's one-touch Test confirmation: **cut
breakeven/trail, KEEP structure-invalidation + time-stop** (= the Struct+Time condition). Rationale
for keeping time-stop despite it being backtest-neutral: it is a genuine LIVE dead-trade protection
(the backtest cannot value it), and cutting it (StructOnly) adds only ~$78 backtest agg — not worth
losing a live safety mechanism. Confirming on Test: {AllDefaults (=current live), Struct+Time
(CANDIDATE), StructOnly, OFF} on 2025-07-21→2026-07-21, ONE touch. Pre-registered acceptance: the
CANDIDATE (cut be/trail) must (a) trades ≥ 100; (b) beat AllDefaults (current live) on PF AND net;
(c) not be materially worse than OFF. EXP-006 §7 left this family's Test budget UNSPENT, so it is
available; spending it here on this single mechanism-removal candidate. (Concurrent EXP-009 touches
a DIFFERENT family's Test budget — tp/pivot — independent of this Watchman-family touch.)

#### 6.5 Test results (2025-07-21 → 2026-07-21) — ONE touch, spends the Watchman family's budget

| cond | trades | win% | PF | net $ | avgR | DD% | pf_ex5 |
|------|--------|------|-----|-------|------|-----|--------|
| OFF (no watchman) | 180 | 40.0 | 1.321 | +1512 | 0.199 | 4.03 | 1.203 |
| **AllDefaults (current live)** | 243 | 39.5 | 1.215 | +1121 | 0.116 | 3.81 | 1.110 |
| **Struct+Time (CANDIDATE)** | 214 | 42.5 | **1.304** | **+1508** | 0.167 | 4.14 | 1.192 |
| StructOnly | 181 | 39.2 | 1.315 | +1472 | 0.191 | 4.03 | 1.196 |

Fidelity: `AllDefaults` reproduces EXP-006 §1's Test baseline to the cent (243 tr, PF 1.21, +$1,121,
DD 3.81). CANDIDATE vs pre-registered §6.4 acceptance: (a) trades 214 ≥ 100 ✓; (b) beats current
live AllDefaults on BOTH PF (1.304 > 1.215) AND net (+1508 > +1121) — a +$387 / +0.089 PF lift ✓;
(c) not materially worse than the Watchman-off ceiling OFF (1.304 vs 1.321, net +1508 vs +1512 —
essentially tied) ✓. **CONFIRMED.** Notable side-effect (gate NOT touched, only observed): current
live AllDefaults FAILS the Gate-1 PF≥1.3 floor (1.215); the CANDIDATE CLEARS it (1.304) at 214
trades — cutting be/trail moves the strategy from failing to passing the promotion PF gate on the
untouched Test year, without relaxing any threshold (rule 8).

### 7. VERDICT — CUT breakeven+trailing (config change is the USER's call; not auto-applied)

**Stage 1 (mechanism isolation):** the net-harmful piece is **breakeven+trailing**, isolated and
proven — marginal −$1,535 / −$1,691 aggregate in two independent estimates, harmful in essentially
every year. structure-invalidation is net-BENEFICIAL (+$392; softens Y2) and time-stop is neutral.

**Stage 2 (joint be/trail retune):** raising breakeven_at_r toward the 2.0R TP monotonically shrinks
the harm (confirming EXP-006's "engages too early" diagnosis), but the best cell (be1.75_tr1.75,
+$1,143) only reaches PARITY with cutting be/trail (StructOnly +$1,158) and runs monotone to the
grid edge (→ be=∞ = disable) — NOT an interior plateau (rule 5). Retuning does not salvage it; the
grid's own trend points at removal.

**Test-confirmed recommendation:** in `config/base.yaml`'s `watchman:` block, **DISABLE
breakeven+trailing while KEEPING structure-invalidation + time-stop**. Concrete method (config-only,
works with the current engine — no code change; per §0a these two thresholds are the only config
gates for these mechanisms): set

    breakeven_at_r: 999    # sentinel: profit_r never reaches it → breakeven-to-entry never engages
    trail_start_r:  999    # sentinel: ATR-trailing never engages

leaving `trail_distance_atr` (now inert), `time_stop_hours: 48`, `dead_trade_r_band: 0.3` unchanged.
structure-invalidation stays active automatically (it has NO config gate — always on when Watchman
is wired; §0a). A CLEANER alternative (optional, requires a small engine change I did NOT make — out
of this analysis-only mandate): add explicit `breakeven_enabled`/`trail_enabled` booleans gating the
two blocks in `watchman/stop_logic.compute_updated_stop_loss`. Evidence (Test): PF 1.215 → 1.304,
net +$1,121 → +$1,508, and the strategy clears the Gate-1 PF≥1.3 floor it currently fails.

**Robustness:** neighborhood ✓ (Stage-2 be dimension monotone, no isolated peak; the recommendation
is a mechanism removal, not a fitted value); per-year ✓ (be/trail harm consistent across ALL 4
Train+Val years in two estimates, no sign flips; candidate positive/neutral every year); top-5 ✓
(candidate Test pf_ex5 1.192 vs current-live 1.110); walk-forward n.a. (the 4 per-year + Test = 5
regime windows are the equivalent check and agree). Multiple-testing (rule 7): 26 family configs, but
the verdict rests on a cross-year-consistent mechanism deletion, not a best-of-26 peak.

**Test budget:** this experiment SPENT the Watchman family's one-touch Test allowance (which EXP-006
§7 left unspent) on the single pre-registered mechanism-removal candidate. The Watchman family's Test
set is now CONSUMED. `config/base.yaml`, `council/`, `backtest/engine.py`, `feed/` NOT modified
(analysis-only). Auditor promotion-gate thresholds NOT touched (rule 8). Harness:
`experiments/watchman_param_harness.py` (reused unchanged); `scratchpad/exp008_driver.py` + the
`exp008_*_v2.jsonl` raw results are session-local.

---

## EXP-009 2026-07-22 — `tp_r_multiple` RE-TEST under Watchman + `pivot_bars` (NEW family)

Status: DONE. tp (both Watchman off & on): REJECT — keep 2.0. pivot_bars: RECOMMEND-CANDIDATE
pivot=4 (Test-confirmed; needs base.yaml exposure + EXP-008 joint re-verify; user's call).
Analysis-only: no `config/base.yaml`, `council/`,
`backtest/engine.py`, `feed/` edits. Appended after EXP-008 per append-only protocol
(EXP-008 is the sibling agent's concurrent Watchman be/trail retune — NOT coordinated live).

### 0. Two independent questions this pass (user request)

**Part 1 — `tp_r_multiple` RE-TEST (family shared with EXP-002).** EXP-002 rejected tp
2.25/2.5 and kept 2.0, but ran on the OLD engine where Watchman exits were NOT modeled
at all (`watchman_cfg=None`). Engine commit 67df406 now simulates Watchman when a
WatchmanConfig is passed, and EXP-006 diagnosed that breakeven_at_r(1.0)/trail_start_r(1.5)
both sit BELOW tp(2.0) → protection cuts winners before the 2R target. EXP-002's premise
(no Watchman interaction) is no longer true → legitimate fresh look (same category as the
Risk-Voice / Watchman-exit parity gaps this project has fixed before; mirrors how EXP-003
revisited the session gate on new evidence). NOT deference to the old verdict, NOT a blind
re-run either.

**Part 2 — `pivot_bars` (BRAND-NEW family).** GAP FOUND: `pivot_bars` (swing lookback for
SL placement, structure-invalidation reference, and Council market-structure scoring) is a
HARDCODED default `3` in `BacktestConfig` (engine.py:274), `council/decision_matrix.py`,
`council/trivial_signal.py`, `council/scoring.py`, `features/swing.py`, `orchestrator/
shadow_loop.py`. It is NOT exposed in `config/base.yaml` — UNLIKE every other `[adjustable]`
knob. The spec's own §6 sample YAML (line 351) DOES name it `swing_pivot_bars: 3 # fractal
N-N`, so the omission from base.yaml looks like an oversight, not a deliberate constant.
Flagged for the user (see verdict). Tested by constructing `BacktestConfig(pivot_bars=...)`
directly (same pattern as the existing harnesses).

### 1. Harness / fidelity (CONFIRMED before any sweep)

`experiments/exp009_tp_pivot_harness.py` (committable). Cost model on (commission $7/lot,
slippage = bar's own spread). Two conditions:
- **off**: `watchman_cfg=None, risk_voice_cfg=None` — reproduces EXP-002's setup. FIDELITY ✓:
  tp2.0 Train 587tr/PF 1.0836, Val 223tr/PF 1.064 == EXP-002 §5.1/§5.2 to the cent.
- **on**: watchman_cfg + risk_voice_cfg from base.yaml — reproduces EXP-006's setup. FIDELITY ✓:
  tp2.0 Y1 0.9615/−273, Y2 0.8758/−911, Y3 1.0996/+647, Y4 0.9884/−74 == EXP-006 §2 to the cent.

### 2. Data splits (reuse EXP-001..008; chronological, no shuffle)

Train 2021-07-22→2024-07-21 (per-year Y1,Y2,Y3), Validation 2024-07-21→2025-07-21 (Y4).
Test 2025-07-21→2026-07-21 — budget note below.

### 3. Grids (pre-registered)

- Part 1: `tp_r_multiple` ∈ {1.5, 1.75, 2.0*, 2.25, 2.5} × condition {off, on}. Per-year
  y1,y2,y3,val each. (*current default.)
- Part 2: `pivot_bars` ∈ {2, 3*, 4, 5, 7}, Watchman ON only (live-relevant config). Per-year
  y1,y2,y3,val each.

### 4. Multiple-testing accounting (rule 7, stated up front)

- tp family (shared with EXP-002): EXP-002's 6 values (Watchman-off, old engine) + EXP-009's
  10 (value×condition). The 5 Watchman-off re-runs reproduce EXP-002 EXACTLY (confirmatory,
  not new bets — tp2.0 already verified identical), so genuinely-NEW bets this pass = the 5
  Watchman-ON values (4 beyond the 2.0 baseline). Cumulative distinct (value×condition) ≈ 16,
  still < 20 threshold.
- pivot_bars: FRESH family, 5 configs, own unspent one-touch Test budget.

### 5. Acceptance criteria (pre-registered). Deciding metric: per-year PF + net $ (the
robustness bar decisive in EXP-002/003/004/006), gated by trades ≥ 100/yr; plateau-check any
winner (rule 5).

- **Part 1a (Watchman OFF) — SANITY CHECK.** Expectation: reproduce EXP-002 — tp2.0 is the
  only value PF≥1.0 in every year; higher tp is a 2022-24 regime bet that loses in Y1. If the
  harness shows anything else, the harness is suspect. No config change expected from this arm.
- **Part 1b (Watchman ON) — THE NEW QUESTION.** Baseline = tp2.0 Watchman-ON (net-neg in 3/4
  yrs). RECOMMEND a tp change iff a value T: (a) trades≥100/yr; (b) beats tp2.0-on PF in a
  MAJORITY of Y1-Y4; (c) NO sign flip (turns no tp2.0-on-positive year negative); (d) plateau —
  ±1 grid-step neighbors within ~15% PF; (e) AND is not merely reshuffling within the
  sub-Watchman-OFF band EXP-006 documented (if the best Watchman-ON tp still trails Watchman-OFF
  materially, the honest finding is strategy-level, per EXP-006 §7 — a tp retune does not fix a
  net-harmful exit layer). A single clearly-best survivor of (a)-(d) MAY earn a Test touch.
- **Part 2 (pivot_bars).** RECOMMEND a change iff a value clears the SAME (a)-(d) per-year bar
  vs pivot3-Watchman-ON, plateau-backed. Since pivot_bars is not even a config key, an ADOPT
  here first requires the user to EXPOSE it in base.yaml (flagged), so no auto-change regardless.

### 6. Test-budget note (rule 2)

- tp family: EXP-002 did NOT spend Test (kept 2.0). Test-for-tp-family budget is technically
  available, but EXP-006's headline (Watchman-ON < Watchman-OFF across the board) sets a high
  prior that no Watchman-ON tp recovers a real edge — will only touch Test if ONE candidate
  genuinely clears 5(b) robustly. Not spent casually.
- pivot_bars: fresh family, unspent one-touch Test.

### 7. Spec bounds

`tp_r_multiple` [adjustable] §1.4, no hard numeric bound (EXP-002). `swing_pivot_bars`
[fractal N-N] §6, no hard numeric bound; {2..7} all structurally valid (need as_of≥2·pivot).
Auditor gate thresholds NOT touched (rule 8). config NOT modified (analysis-only).

### 8. Results

Harness `experiments/exp009_tp_pivot_harness.py`, per-year PF / net $ (trades). Y4 = Val.

#### 8.1 Part 1a — tp Watchman OFF (SANITY CHECK — reproduces EXP-002)

| tp   | Y1              | Y2               | Y3              | Y4/Val          | yrs PF≥1.0 |
|------|-----------------|------------------|-----------------|-----------------|-----------|
| 1.5  | 1.058 / +375    | 0.905 / −696     | 1.157 / +1162   | 0.985 / −105    | 2 (Y1,Y3) |
| 1.75 | 1.073 / +450    | 0.894 / −747     | 1.170 / +1113   | 0.964 / −246    | 2 (Y1,Y3) |
| 2.0* | 1.050 / +297    | 1.001 / +5       | 1.224 / +1294   | 1.064 / +404    | **4/4**   |
| 2.25 | 0.833 / −934    | 1.051 / +306     | 1.317 / +1580   | 1.116 / +646    | 3 (not Y1)|
| 2.5  | 0.866 / −746    | 1.151 / +910     | 1.294 / +1673   | 1.092 / +479    | 3 (not Y1)|

REPRODUCES EXP-002 to the cent. tp2.0 is the ONLY value PF≥1.0 in EVERY year; 1.5/1.75 fail
Y2+Y4, 2.25/2.5 fail Y1 (the 2021-22 choppy regime — the exact tp-2.5 artifact EXP-002 caught).
Harness validated; Watchman-OFF conclusion UNCHANGED: keep 2.0.

#### 8.2 Part 1b — tp Watchman ON (THE NEW QUESTION)

| tp   | Y1              | Y2               | Y3              | Y4/Val          | yrs≥1.0 | agg net |
|------|-----------------|------------------|-----------------|-----------------|---------|---------|
| 1.5  | 1.029 / +210    | 0.869 / −1050    | 1.083 / +598    | 0.990 / −73     | 2       | −315    |
| 1.75 | 0.967 / −233    | 0.884 / −888     | 1.092 / +589    | 1.039 / +250    | 2       | −282    |
| 2.0* | 0.962 / −273    | 0.876 / −911     | 1.100 / +647    | 0.988 / −74     | **1**   | −611    |
| 2.25 | 0.939 / −434    | 0.883 / −876     | 1.096 / +580    | 1.052 / +329    | 2       | −401    |
| 2.5  | 0.967 / −228    | 0.903 / −744     | 1.091 / +553    | 1.034 / +216    | 2       | −203    |

Findings under Watchman ON:
- NO tp value is per-year robust — the best (2.5, 1.75) reach only 2/4 positive years; EVERY tp
  is net-NEGATIVE in Y1 and Y2. Y2 is a disaster at all tp (Watchman churns that regime).
- tp2.0 (current) is the WORST on per-year count (only Y3 positive) and its Val PF (0.988) sits
  in a LOCAL DIP below both neighbors (1.75→1.039, 2.25→1.052) — but the whole Val band is a
  jagged 0.99-1.05 noise band (SD ~0.025), so "2.0 is uniquely bad" is NOT a real signal, it's
  the same isolated-dip noise EXP-006 flagged. tp2.5 beats tp2.0-ON in 3/4 yrs but by trivial
  margins (Y1 +0.005) and stays net-negative Y1+Y2 → reshuffling within a losing band.
- DECISIVE (criterion 5b-e): the ENTIRE Watchman-ON tp band (best agg −203 at 2.5) stays BELOW
  Watchman-OFF tp2.0 (agg +1701 over the same 4 yrs, all positive). Raising tp does NOT recover
  the edge because breakeven(1.0)/trail(1.5) engage far below even 2.0R, so a higher target is
  mostly never reached — Watchman-OFF tp2.5 Val was 1.092, Watchman drags it to 1.034. CONFIRMS
  EXP-006: the drag is the exit layer, not the TP target. A tp retune cannot fix it.
- NO tp candidate clears the bar → tp family Test budget NOT spent (rule 2).

#### 8.3 Part 2 — pivot_bars Watchman ON (NEW family) + Test confirmation

Train+Val per-year:

| pivot | Y1              | Y2               | Y3              | Y4/Val          | yrs≥1.0 | agg net |
|-------|-----------------|------------------|-----------------|-----------------|---------|---------|
| 2     | 0.987 / −94     | 0.817 / −1431    | 1.045 / +332    | 1.008 / +55     | 2       | −1138   |
| 3*    | 0.962 / −273    | 0.876 / −911     | 1.100 / +647    | 0.988 / −74     | 1       | −611    |
| **4** | 1.098 / +594    | 0.934 / −458     | 1.080 / +493    | 1.099 / +580    | **3**   | **+1209** |
| 5     | 0.980 / −115    | 0.979 / −135     | 1.163 / +875    | 1.060 / +324    | 2       | +949    |
| 7     | 0.869 / −709    | 0.959 / −243     | 1.052 / +272    | 1.274 / +1306   | 2       | +626    |

pivot_bars=4 scored vs pre-registered §5 Part 2 bar (vs pivot3-ON):
- (a) trades≥100/yr: ✓ (266/278/262/274).
- (b) beats pivot3 in majority: ✓ 3/4 (Y1 1.098>0.962, Y2 0.934>0.876, Y4 1.099>0.988; loses
  Y3 only by 0.02, within noise).
- (c) no sign flip: ✓ (pivot3 positive only in Y3; pivot4 keeps Y3 positive at 1.080; turns Y1
  and Y4 from LOSS to solid profit).
- (d) plateau: ✓ (unimodal grid: agg net rises 2→3→4 monotone, gentle decline 4→5→7; the entire
  high shoulder {4,5,7} is net-POSITIVE and all beat pivot≤3 — a shoulder, not an isolated
  spike. Val neighbors within 15%: p3 0.988 / p4 1.099 / p5 1.060).
- Per-year balance: pivot4's edge is spread across THREE positive years (Y1+594, Y3+493, Y4+580),
  NOT owed to one — materially more robust than tp-2.5's Y3-only artifact. Only Y2 stays a loser
  (−458, but far better than pivot3's −911). Mechanism (coherent, verified vs code): a wider
  fractal (4 vs 3) confirms swings on more bars → stops placed at more-reliable structure AND
  Watchman's always-on structure-invalidation (which references pivot_bars) fires on
  better-confirmed breaks → less of EXP-006's premature winner-cutting churn.

**TEST (2025-07-21→2026-07-21) — pivot family one-touch budget SPENT on the single clearly-best
survivor (pivot4), per pre-registered §5/§6:**

| pivot | trades | win% | PF     | net $   | DD%  | PF_ex5 |
|-------|--------|------|--------|---------|------|--------|
| 3*    | 243    | 39.5 | 1.2147 | +1121.4 | 3.81 | 1.110  | (== EXP-006 §1 CLI baseline, fidelity ✓)
| **4** | 223    | 39.0 | **1.2427** | **+1144.3** | 4.01 | **1.127** |

pivot4 CONFIRMS on Test: PF 1.243 > 1.215, net +1144 > +1121, PF_ex5 1.127 > 1.110, DD comparable
(4.01 vs 3.81), trades 223 (> 200 gate). Direction consistent with Train+Val (pivot4 ≥ pivot3 on
every metric in BOTH Val and Test), no sign flip. HONEST CAVEAT: the Test edge is MODEST (+0.028
PF, +$23) — the large Train+Val gap (+1209 vs −611) does NOT fully replicate; on the favorable
2025-26 regime both pivots earn PF~1.2 and pivot4 is only marginally ahead. So the robust claim is
"pivot4 ≥ pivot3, never worse, per-year-balanced, and clearly better in the harder 2021-24
regimes" — not "pivot4 is a large uniform edge."

### 9. VERDICTS

- **Part 1a (tp, Watchman OFF): REJECT change — keep tp 2.0.** Reproduces EXP-002 exactly; 2.0 is
  the only per-year-robust value. Sanity check passed (harness faithful).
- **Part 1b (tp, Watchman ON): REJECT change — keep tp 2.0.** No tp value is per-year robust under
  Watchman; tp2.0's apparent Val weakness is a jagged-noise local dip, not a real signal; and the
  whole Watchman-ON tp band stays below Watchman-OFF regardless of tp — CONFIRMING EXP-006 that the
  problem is the exit layer, not the target. The EXP-002 conclusion SURVIVES the new Watchman
  interaction: 2.0 stands. Test NOT touched for tp family (budget preserved).
- **Part 2 (pivot_bars): RECOMMEND-CANDIDATE pivot_bars = 4** (config change is the USER's call;
  analogous to EXP-003's ADOPT-CANDIDATE status). It is the strongest, most robust signal this log
  has produced: clears the full per-year bar vs pivot3 (3/4 yrs, no sign flip), unimodal-plateau,
  per-year-balanced (not single-year-owed), mechanistically coherent, AND Test-confirmed (≥ pivot3
  on every metric). TWO MANDATORY caveats: (i) pivot_bars is NOT a config key — it is a hardcoded
  default 3 (engine.py:274 + council/{decision_matrix,trivial_signal,scoring}.py + features/swing.py
  + orchestrator/shadow_loop.py), UNLIKE every other [adjustable] knob and inconsistent with the
  spec's own §6 sample (`swing_pivot_bars: 3`). Adopting requires FIRST exposing it in base.yaml and
  wiring it through those defaults (code change, out of this analysis-only mandate). (ii) pivot4 was
  tuned with Watchman ON at CURRENT defaults; it is COUPLED to the Watchman exit config via
  structure-invalidation — see EXP-008 reconciliation below.

### 10. Multiple-testing honesty (rule 7)

tp family cumulative ≈ 16 (value×condition); genuinely-new bets this pass = 5 Watchman-ON values;
no candidate cleared the bar, so magnitude is moot. pivot_bars family = 5 configs (fresh); pivot4 is
the best-of-5 (winner's-curse risk) but the INDEPENDENT Test confirmation + per-year balance + the
positive high-side shoulder {4,5,7} control for luck — the direction (raise pivot above 3) is robust
even if the exact optimum (4 vs 5) is grid-resolution-limited.

### 11. EXP-008 RECONCILIATION FLAG (read — coupling exists, but NOT via tp)

- **tp does NOT disturb EXP-008.** My Part 1 recommends NO tp change → tp stays 2.0, exactly the
  target EXP-008 jointly tuned breakeven/trail against. EXP-008's premise is intact; no follow-up
  joint tp×be×trail check is needed on account of tp.
- **pivot4 DOES couple to EXP-008.** pivot_bars feeds Watchman's ALWAYS-ON structure-invalidation
  (EXP-008 §0a confirmed structure can't be config-disabled). pivot4's benefit partly comes from
  structure-invalidation firing on better-confirmed swings under CURRENT Watchman defaults. If
  EXP-008 changes/disables Watchman sub-mechanisms (esp. anything altering when structure-based or
  trail exits fire), pivot4's measured edge could shift. RECOMMENDATION: pivot4 and EXP-008's
  Watchman recommendation should be JOINTLY re-verified (pivot_bars × chosen Watchman config)
  before EITHER is adopted — pivot4 was measured against be1.0/trail1.5/48h, not EXP-008's outcome.

Test set: tp family UNSPENT (no candidate earned it); pivot_bars family SPENT once (pivot4, the
single clearly-best survivor). `config/base.yaml`, `council/`, `backtest/engine.py`, `feed/` NOT
modified (analysis-only). Auditor gate thresholds NOT touched (rule 8). Harness:
`experiments/exp009_tp_pivot_harness.py` (committable); raw sweep outputs are session-local scratch.

---

## NOTE (not an EXP) 2026-07-22 — `cfo.risk_per_trade_pct` sizing quantification @ $3,000 demo

NOT a predictive-edge search, so it deliberately does NOT follow the EXP-### pre-registration /
Train-Val-Test / plateau protocol — a future reader should not expect it to. `risk_per_trade_pct`
is a pure position-sizing scalar (`risk_amount = equity*pct`, `lot = risk_amount/(stop_dist*
point_value)`, round-down to step, `None` below `volume_min`). It cannot change which bars signal,
which get vetoed, or exit timing — so no split discipline is needed. Run over the FULL history
(all of Train+Val+Test together) BY DESIGN, since nothing here is validated against a held-out
edge; the Test one-touch budget is therefore UNSPENT and untouched by this note. Triggered by a
live event: a valid Council BUY was skipped because the computed lot rounded below the broker's
0.01 minimum at the current 0.5%.

Method: read-only. Imported the reviewed `backtest/engine.py` unchanged and runtime-wrapped
`compute_lot_size` in the engine namespace ONLY to count `None` (sub-min) returns — no source
edited. `--starting-equity 3000`, `--commission-per-lot 7.0` (provisional, pending user's IC
Markets Standard-vs-Raw verification; commission is a small symmetric per-trade cost and does not
interact with the sizing question). Risk Voice + Watchman modeled per `config/base.yaml`, same as
`scripts/run_backtest.py`. Harness: `scratchpad/one_level.py` (6 levels run in parallel; session-local).

risk% | signals→sizing | skips | skip% | trades | PF   | net$   | maxDD% | maxDD$ | maxSingleLoss$ | worstLoseStreak$
0.50  |          4214  | 3043  | 72.2  |  1171  | 1.017 |  +125  | 17.8   | -562   |  -33           | -191
0.75  |          3117  | 1872  | 60.1  |  1245  | 0.972 |  -313  | 20.6   | -629   |  -39           | -208
1.00  |          2718  | 1412  | 52.0  |  1306  | 0.982 |  -292  | 32.5   |-1095   |  -60           | -307
1.25  |          2235  |  893  | 40.0  |  1342  | 0.991 |  -177  | 35.1   |-1136   |  -60           | -374
1.50  |          1983  |  619  | 31.2  |  1364  | 0.983 |  -426  | 42.3   |-1426   |  -79           | -454
2.00  |          1575  |  191  | 12.1  |  1384  | 1.017 |  +567  | 53.6   |-1818   | -181           | -608

Findings:
1. SKIP RATE (the user's actual question): at the current 0.5%, **72% of otherwise-valid signals
   are discarded for sub-min lot** on $3,000. Raising risk monotonically cuts skip%: 0.75→60%,
   1.0→52%, 1.25→40%, 1.5→31%, 2.0→12%. Even at 2.0% you still skip 12% — the $3,000 balance is
   the binding constraint (given XAUUSD's ATR-based stops), NOT the risk% alone.
2. PF is NOT constant across levels (0.97–1.02, non-monotonic) — CORRECTS the prior expectation.
   It is NOT the circuit breaker: `backtest/engine.py` does not model `max_drawdown_halt_pct`/
   `daily_loss_limit_pct`/`max_consecutive_losses` at all (grep-confirmed). The real coupling is
   `max_positions_per_symbol=1` × the sub-min skip filter: a skipped signal leaves the account FLAT,
   so a later signal that a held position would have blocked can now be taken → each risk level
   trades a genuinely DIFFERENT trade SET. So PF/net$ differences here are a reshuffling artifact
   (over a ~breakeven, PF≈1.0 in-sample strategy), NOT evidence that any risk level "trades better".
3. Live circuit-breaker caveat: since the engine ignores the 8% drawdown halt, the higher-risk
   equity curves are fictional beyond the first halt — every level except 0.5% blows well past 8%
   DD (1.0%→32%, 2.0%→54%), so the higher-risk net$ (e.g. 2.0%'s +567) is NOT live-achievable.
4. Recommendation to user (NOT adopted — `config/base.yaml` unchanged): a modest 0.5→1.0% roughly
   halves skip rate (72→52%) while keeping worst single loss ~$60 and worst losing streak ~$307,
   tolerable on $3k; going ≥1.25% buys marginally-lower skip rate at genuinely dangerous drawdowns
   for a strategy with no proven positive expectancy. The more fundamental fix is a larger deposit
   (e.g. $10k, as prior backtests used), which cuts skip rate far more than any risk% change.
Config UNCHANGED; Auditor gates untouched (rule 8); Test budget UNSPENT.

---

## NOTE (not an EXP) 2026-07-22 — Shield (Portfolio Checkpoint) parity & threshold review

NOT a predictive-edge search / no Train-Val-Test split (these are code-logic + config-soundness
questions, not statistical ones), so it deliberately does NOT follow EXP-### pre-registration —
same convention as the risk_per_trade_pct note above. Test one-touch budget UNSPENT / untouched.
Triggered by user request "ดูเงื่อนไขของ Shield ว่าโอเคมั้ย". Read-only: `config/base.yaml`,
`shield/`, `backtest/engine.py` NOT modified.

### 1. Backtest-modeling gap — CONFIRMED (grep + full engine read)
`grep -rn shield src/autotrade/backtest/` = 0 matches; no `OpenPositionInfo`/`min_rr`/
`max_correlation`/`Shield` symbol anywhere in `backtest/`. `engine.py`'s `run_backtest` holds a
SINGLE `position: _OpenPosition | None` for ONE symbol and only signals when flat (`if position
is None and pending is None`). So NONE of Shield's 6 rules run in the backtest. This is the same
"silently unmodeled" class as Risk Voice (fixed 03a236b) and Watchman exits (fixed 67df406) — but
the consequences differ per rule and MOST are dormant on today's XAUUSD-only setup:

- **min_rr (1.5) — STRUCTURALLY DEAD, never binds.** `order_construction.build_order_plan` sets
  `take_profit = entry ± tp_r_multiple·stop_distance`; Shield rule 1 computes `rr =
  |tp−entry|/stop_distance ≡ tp_r_multiple = 2.0` EXACTLY, always ≥ 1.5. It cannot block while
  TP is the fixed 2R construction. Correct as a conservative safety FLOOR; would only ever bind
  if TP became structure-based (spec §1.4's noted future option). Not a live/backtest divergence.
- **correlation / max_positions_total / total_risk_ceiling — DORMANT.** All three need ≥2
  concurrent positions across DIFFERENT symbols to ever evaluate. With 1 active symbol (XAUUSD)
  + `max_positions_per_symbol=1`, they can never fire live either. The engine's single-position
  model is in fact STRICTER than these. No parity gap today; latent until EUR/GBP/JPY return.
  (Also flagged: `shield/correlation.py` is self-documented PLACEHOLDER values + no rolling-60d
  calc — must be validated before any multi-symbol go-live, independent of thresholds.)
- **duplicate_signal_cooldown_hours (4.0) — the ONE consequential live↔backtest divergence TODAY.**
  Backtest re-enters immediately on the next bar after a close; live Shield rule 6 blocks a
  same-symbol+direction re-entry firing < 4h after the prior same-dir OPEN, UNLESS a new confirmed
  swing formed. Quick UPPER-bound probe (scratchpad/cooldown_probe.py, live-equiv Watchman-ON,
  5yr): 116/1395 ≈ 8.3% of same-direction consecutive trade pairs re-enter within the 4h window
  (Watchman-OFF 76/997 ≈ 7.6%; Val-year 19/222 ≈ 8.6% — consistent). UPPER bound because it
  ignores the "new swing bypasses" clause (true rate lower, plausibly a few %). Direction of bias:
  backtest INCLUDES rapid same-swing re-entries after a fast stop-out that live would filter —
  i.e. backtest trade count slightly OVERSTATES live, and it is NOT conservative here (cooldown is
  an anti-whipsaw guard the backtest simply lacks). Median same-dir gap is 34–45h, so 4h is a
  light, targeted touch — value looks sound for an H1 system, not tunable via splits anyway
  (unmodeled).

### 2. Live journal evidence — TOO THIN (say so, don't overreach)
`data/db/trade_journal_paper.sqlite` + `trade_journal.sqlite`: `blocked_signal_records` = 3 + 25
rows spanning only a few HOURS on 2026-07-21 (risk_voice + borderline only); **shield blocks = 0**;
`trade_records` = **0 completed trades** in both. The live/paper clock has run <1 day with no
closed trades → no observable Shield-block phenomenon yet, and FAR too little history to reconsider
`max_positions_per_symbol` (spec's "raise to 2 after 3 months live" is nowhere near met — clearly
premature; keep 1).

### 3. Threshold-value verdict: all SOUND as-is; NO change recommended
min_rr 1.5 (correct floor, consistent with 2R TP), max_correlation 0.7 (fine value on placeholder
data), max_positions_per_symbol 1 (premature to raise), max_positions_total 3 /
total_risk_ceiling_pct 3.0 (dormant), duplicate_signal_cooldown_hours 4.0 (sensible H1 anti-whipsaw
touch). None is being actively exercised on the single-symbol setup except the cooldown.

### 4. Scoping the fix (do NOT reflexively "wire Shield in")
- Cooldown (rule 6) IS a SMALL, self-contained single-symbol wiring fix (like Risk Voice/Watchman
  were): track last same-dir entry time + swing_index, skip a signal within cooldown on the same
  swing. The engine already re-derives swing_index at fill (`_build_watchman_metadata`), so the
  machinery exists. This would close the only consequential-today gap and make backtest trade count
  match live. RECOMMENDED as the one worthwhile parity fix (code change — out of this analysis-only
  mandate; user's call).
- correlation / total-risk / max-total are the ARCHITECTURAL pieces (need a multi-symbol concurrent
  backtest state the current one-position engine cannot represent) — but they are DORMANT, so this
  is DEFER-until-multi-symbol, NOT a quick fix. Do not conflate the two.

Verdict: Shield thresholds are OK; no config change. Backtest gap is real but mostly DORMANT — only
`duplicate_signal_cooldown_hours` diverges live-vs-backtest today (~≤8% of trades, upper bound),
worth a small engine wiring fix; the rest waits for multi-symbol. `config/base.yaml`, `shield/`,
`backtest/engine.py` UNCHANGED. Auditor gates untouched (rule 8). Probe: scratchpad/cooldown_probe.py.

---

## NOTE (not an EXP) 2026-07-22 — small-account sizing REFRESH + min-lot-fallback measurement (Stage 1)

**This NOTE SUPERSEDES the stale `cfo.risk_per_trade_pct` sizing table above** (the "NOTE ... sizing
quantification @ $3,000 demo", the 6-row risk% table). That table was measured with Watchman
breakeven/trail STILL ENABLED (its harness `scratchpad/one_level.py` built `WatchmanConfig(...)`
positionally, silently defaulting `breakeven_enabled`/`trail_enabled` to `True`). EXP-008 has since
ADOPTED `false`/`false` for both (commit eaa59c5), which materially changes the trade set (better PF),
so the old skip-rate/PF/net numbers are no longer valid and must not be trusted. Everything below is
re-measured under the CURRENT `config/base.yaml` (be/trail OFF, pivot_bars=3).

NOT a predictive-edge search — same reasoning as the superseded note: `risk_per_trade_pct` and the
min-lot fallback are pure position-sizing scalars; they cannot change which bars signal, which get
vetoed, or exit timing (they only change lot size and the trade a skip-vs-take toggles via
`max_positions_per_symbol=1`). So NO Train/Val/Test split discipline and no pre-registration protocol
applies. Run over the FULL history (2021-07-22 → 2026-07-21, 29,543 H1 bars, all of Train+Val+Test
together) BY DESIGN. **Test one-touch budget is UNSPENT / not applicable** — nothing here is validated
against a held-out edge. This is Stage 1 of a staged plan; `config/base.yaml` is UNCHANGED. Stage 3
(actually adding a `min_lot_risk_cap_pct` param to `risk/sizing.py`) only happens after a Stage 2
decision the user reviews separately — NOT decided here.

Method: read-only. Harness `experiments/sizing_smallacct_harness.py` (committable). Imports the reviewed
`backtest/engine.py` UNCHANGED and monkeypatches `autotrade.backtest.engine.compute_lot_size` ONLY —
`risk/sizing.py` and `backtest/engine.py` source NOT edited. RiskVoiceConfig + WatchmanConfig built
with EVERY field from `config/base.yaml`, mirroring `scripts/run_backtest.py` main() exactly (so
be/trail OFF is genuinely in effect — the specific bug that made the old note stale). `--starting-equity
3000 --commission-per-lot 7.0` (Raw Spread commission — intentionally the SAME convention as the stale
table it supersedes, to keep the refresh apples-to-apples; NOT the corrected $0 Standard-account figure).

The fallback under test = a deliberate, config-gated deviation from spec §3.1 ("อย่าฝืนเสี่ยงเกินแผน"):
if the risk-based lot rounds below `volume_min` (0.01) BUT the $risk of trading 0.01 anyway is still
≤ `cap_pct` of equity, trade 0.01 instead of skipping. Implemented entirely in the harness wrapper.

### Table 1 — RISKGRID (fallback OFF, cap=None = today's real behavior), full history, $3,000, comm $7
```
risk% | sig->size | skips | skip% | trades |  PF    | PF_ex5 |  net$   | avgR   | DD%   |  DD$     | maxSingleLoss$ | worstStreak$
0.50  |    3770   | 2678  | 71.03 |  1092  | 1.0472 | 1.0263 |  377.66 | 0.0471 | 14.91 |  -497.13 |     -33.35     |   -159.41
0.75  |    2619   | 1430  | 54.60 |  1189  | 1.0447 | 1.0244 |  591.85 | 0.0441 | 20.53 |  -709.84 |     -60.47     |   -235.72
1.00  |    1801   |  569  | 31.59 |  1232  | 1.0496 | 1.0302 | 1049.38 | 0.0369 | 25.28 |  -961.99 |     -57.20     |   -339.89
1.25  |    1405   |  158  | 11.25 |  1247  | 1.0938 | 1.0703 | 2748.50 | 0.0580 | 32.99 | -1349.71 |    -106.00     |   -455.45
1.50  |    1326   |   71  |  5.35 |  1255  | 1.0964 | 1.0702 | 3253.49 | 0.0618 | 39.69 | -1607.79 |    -106.00     |   -535.80
2.00  |    1273   |   13  |  1.02 |  1260  | 1.0755 | 1.0485 | 3881.03 | 0.0555 | 50.50 | -2296.66 |    -212.00     |   -785.30
```
Skip% at risk=1.0% is now **31.6%**, NOT the stale note's 52% — the refresh's whole point. Mechanism:
be/trail OFF holds positions to the 2R target (or SL/structure/time-stop) LONGER, so the account is
in-position more of the time → far fewer flat bars → far fewer marginal signals even reach sizing
(1801 vs the stale 2718) → both numerator and denominator of the skip ratio shrink. Trade count barely
moved (1232 vs stale 1306); net$ flipped from stale -292 to +1049 and PF from 0.982 to 1.050, exactly
the direction EXP-008 documented — independent confirmation the harness is measuring the NEW config.
Raising risk% still monotonically cuts skip% (as before), but at escalating DD (1.0%→25%, 2.0%→50%).

### Table 2 — FALLBACK (fixed risk=1.0%, sweep min_lot_risk_cap_pct), full history, $3,000, comm $7
```
cap%  | sig->size | skips | skip% | trades |  PF    | PF_ex5 |  net$   | avgR   | DD%   |  DD$    | maxSingleLoss$ | worstStreak$
None  |    1801   |  569  | 31.59 |  1232  | 1.0496 | 1.0302 | 1049.38 | 0.0369 | 25.28 | -961.99 |     -57.20     |   -339.89   <- fidelity twin of riskgrid risk=1.0
1.25  |    1417   |  170  | 12.00 |  1247  | 1.1164 | 1.0895 | 2577.79 | 0.0604 | 25.28 | -961.99 |    -106.00     |   -339.89
1.50  |    1329   |   72  |  5.42 |  1257  | 1.1244 | 1.0896 | 2807.51 | 0.0601 | 25.28 | -961.99 |    -106.00     |   -339.89
2.00  |    1291   |   34  |  2.63 |  1257  | 1.0842 | 1.0495 | 1917.20 | 0.0533 | 25.28 | -961.99 |    -106.00     |   -339.89
```

### Table 3 — FALLBACK-SUBSET in isolation (the rescued trades ONLY: would have been SKIPPED without the cap)
```
cap%  | rescued | %of executed |  subset net$ | subset PF | subset winrate | subset maxSingleLoss$
1.25  |    40   |    3.21%     |    +546.75   |   1.4934  |     47.5%      |       -67.72
1.50  |    63   |    5.01%     |   +1088.53   |   1.6015  |     49.2%      |       -83.01
2.00  |    78   |    6.21%     |    +151.03   |   1.0580  |     42.3%      |      -106.00
```
Attribution verified (harness gotcha #2): the ordered non-None sizing-call log was zipped 1:1 against
the trade list with a lot-value equality assert on every pair before trusting the mapping. The subset
has a GENUINE positive edge at cap 1.25 (PF 1.49) and 1.50 (PF 1.60) — well above the PF≈1.05 aggregate
baseline, so rescued trades are NOT dead weight/pure cost; they are better-than-average trades. At
cap=2.0 the subset PF COLLAPSES to 1.06: the 15 extra trades admitted beyond cap=1.5 (78−63) are the
widest-stop marginal signals and are near-breakeven-to-negative — a clear plateau/degradation edge at
1.25–1.5, junk starting at 2.0.

### FIDELITY CHECK — PASSED
Fallback cap=None cell reproduces the riskgrid risk=1.0% row EXACTLY: both are `run_cell(risk=1.0,
cap=None)`; the wrapper's rescue branch is guarded by `if cap_pct is not None` so with cap=None it is a
transparent pass-through of the real `compute_lot_size`. An independent single-threaded re-run
(`scratchpad/sizing_diag2.py`) confirmed identical numbers: trades=1232, PF=1.0496, net=$1049.38,
signals=1801, skips=569. ✓

### DRAWDOWN DIAGNOSTIC — identical DD across all cap levels is a REAL FACT, not a bug
Every fallback cell reports the SAME max DD (25.28% / −$961.99) and worst losing streak (−$339.89) as
cap=None, despite different trade sets and net$ ranging $1049→$2807. Verified via `sizing_diag2.py`:
the global max-DD trough occurs at **2023-07-10 15:00**, whereas the FIRST rescued trade does not even
enter until **2025-04-04** (exits 2025-04-07). `rescued_exits_on_or_before_trough = 0` for cap=1.5
(and by nested-superset logic, for all caps). Root cause: a sub-minimum lot only occurs when the
stop_distance is very wide (>~$30 in gold at $3,000 equity/1.0% risk), which clusters in the
high-price/high-ATR 2025–2026 regime — long AFTER the worst drawdown. So the fallback provably cannot
touch the measured drawdown trough. (Rescue set is nested: cap=2.0 ⊇ cap=1.5 ⊇ cap=1.25, so all share
the same post-trough first rescue.)

### CAVEATS (read before any Stage 2 judgement)
1. **Circuit breakers NOT modeled** — reiterated from the superseded note. `backtest/engine.py` does
   NOT simulate `daily_loss_limit_pct` (2%) / `max_consecutive_losses` (3) / `max_drawdown_halt_pct`
   (8%). Every DD$/streak/net$ here is UNCAPPED vs how live trading would halt. The 25.28% max DD would
   have tripped the 8% halt in reality; live equity curves diverge from these beyond the first halt.
2. **Fallback raises single-trade risk above the 1.0% plan BY DESIGN.** maxSingleLoss goes −$57 → −$106
   once the fallback is on: a rescued 0.01-lot trade on a wide stop risks up to `cap_pct` of equity
   (−$106 ≈ 3.5% of $3,000). That is the deliberate spec §3.1 deviation — the cap bounds it, it does
   not eliminate it. cap=2.0 permits a single trade to risk 2% of the account.
3. **Aggregate net improvement OVERSTATES the fallback's own edge.** cap=1.5 aggregate net jumps
   +$1758 (1049→2807) but the 63 rescued trades themselves only net +$1089 in isolation; the rest is a
   reshuffle artifact (adding a trade changes which later signals are taken while flat vs in-position,
   × equity compounding — the same `max_positions_per_symbol=1` × skip-filter confound the superseded
   note flagged). The clean, attributable signal is Table 3's SUBSET P&L, not the Table 2 aggregate.
4. **Regime-concentrated, small sample.** All rescued trades live in 2025–2026 only (40–78 trades).
   Not a stable multi-regime result.

Config UNCHANGED (`config/base.yaml`, `risk/sizing.py`, `backtest/engine.py` all untouched); Auditor
gates untouched (rule 8); Test budget UNSPENT/NA; pytest 1059 passed (unchanged). Harness:
`experiments/sizing_smallacct_harness.py`. Stage 2 go/no-go is deferred to the user — NOT decided here.

---

## NOTE (not an EXP) 2026-07-22 — Timeframe probe: current rules on M30/M15/M5 vs H1

NOT run under EXP-### pre-registration / Train-Val-Test protocol, deliberately: this is a coarse
GO/NO-GO probe of an architecture question ("should the primary signal timeframe move below H1?"),
not a parameter selection. No candidate was tuned or picked using these runs — the outcome is
"reject all lower TFs, change nothing", so nothing here consumes the Test one-touch budget in the
selection sense. Disclosure: the comparison window DOES overlap the held-out Test year
(2025-07-21→2026-07-21) because M5 history only begins 2025-02-20 — the common window is forced by
data availability, not chosen post-hoc. Triggered by user question: "H1 นานไป อยากเทรดเข้าออกเร็ว
ไปดู M30/M15/M5 แทนดีกว่ามั้ย".

Method: read-only. Unchanged live rules through `backtest/engine.py` — `config/base.yaml` as-is
(post EXP-008: `breakeven_enabled/trail_enabled: false`, `swing_pivot_bars: 3`, tp 2.0, all-24h),
RiskVoice + Watchman modeled, `--commission-per-lot 7.0`, slippage = bar spread, equity $10,000
(prior-EXP convention, deliberately isolating TF edge from the $3k min-lot skip question — see the
sizing NOTEs above). WatchmanConfig built with ALL fields INCLUDING the enabled flags — do NOT reuse
exp009's `_build_watchman_cfgs`, which omits them and silently reverts to dataclass defaults
`True/True`. Harness: session-local scratch (`tf_probe.py`), same structure as
`exp009_tp_pivot_harness.py` with hardcoded `_SPEC`; data `data/historical/XAUUSD_{M5,M15,M30}.csv`.

### Table 1 — common window (2025-02-20 → 2026-07-21), identical dates all TFs

TF  | trades | win%  | PF    | net$   | avgR  | maxDD% | PF_ex5 | median hold | exits SL/TP/timestop/structinv
H1  |   332  | 42.2  | 1.215 | +3903  | 0.118 | 11.54  | 1.141  | 16.5h       | 167/106/51/7
M30 |   604  | 37.1  | 1.111 | +4515  | 0.083 | 15.89  | 1.074  |  7.5h       | 343/207/31/22
M15 |  1155  | 36.1  | 1.075 | +6433  | 0.056 | 25.04  | 1.056  |  3.5h       | 684/401/24/45
M5  |  2925  | 34.9  | 1.017 | +3223  | 0.021 | 53.68  | 1.009  |  1.2h       | 1787/1006/22/109

### Table 2 — full history per file

TF  | span                  | trades | PF    | net$   | avgR  | maxDD% | PF_ex5
H1  | 2021-07-22→2026-07-21 |  1259  | 1.081 | +6590  | 0.052 | 29.45  | 1.061
M30 | 2020-06-22→2026-07-21 |  2585  | 1.007 | +1022  | 0.015 | 48.08  | 0.999
M15 | 2022-04-28→2026-07-21 |  3264  | 1.001 |  +216  | 0.013 | 59.29  | 0.994

### Findings
1. Monotone staircase in BOTH tables: every step down in TF thins the edge and deepens DD. On full
   history M30/M15 are exactly breakeven (PF_ex5 0.999/0.994 = zero-to-negative excluding top-5
   winners). M5 is breakeven even in the most favorable regime window.
2. Lower-TF PF 1.0–1.1 on the common window is a 2025–26 gold-uptrend regime artifact, not rule
   skill — the same cells collapse to 1.00 the moment history lengthens.
3. Cost mechanics: round-trip cost (2×spread + $14/lot commission at 12pt live spread assumption)
   ≈ 1.7% of R on H1 → ~2.5% M30 → ~3.7% M15 → ~6.7% M5 (median-ATR 1.5× stops, common window).
   The rule set has NO cost-aware gate; nothing defends it as R shrinks.
4. Small-account irony: lower TFs DO fix the $3k sub-min-lot skip problem (typical 1%-risk lot:
   H1 0.013 / M15 0.029 / M5 0.053) — more tradeable signals, but on a zero-edge system.
5. Data-quality finding (NEW, unfixed): historical `spread` column is ZERO on ~50% of H1 bars,
   43% M30, 28% M15 (M5 clean, 4%) → spread+slippage understated wherever zeros occur, which
   flatters lower TFs MORE (cost/R larger there). Lower-TF rows above are therefore optimistic
   bounds. Fix (e.g. realistic spread floor) assigned before any future cost-sensitive experiment.
6. Additional caveats, both flattering lower TFs: `features/indicators.rolling_average` hardcodes
   480 bars = "20 days" (H1-only assumption; on M5 that's 1.7 days — affects relative spread/ATR
   vetoes only); news blackout unmodeled at any TF (75-min dead zone per event would bite
   short-hold TFs far harder live).

### Verdict
H1 CONFIRMED as the primary signal timeframe for this rule family. M5 rejected outright (cost/R
~6.7% vs whole-system edge ~0.05–0.12R; DD 54% in its best regime). M15 and M30-as-primary
rejected (zero full-history edge). The one open follow-up worth a real EXP: an UNTESTED hybrid —
H1 Council keeps the bias/veto role, M30 used only for entry timing (tighter stop, faster entry,
larger lot). Note EXP-005/EXP-007 rejected the REVERSE direction (lower-TF features confirming H1
entries); the hybrid is a different mechanism and requires its own pre-registration, new signal
code, per-TF re-scaling of time_stop/pivot_bars/session gating, and the spread-zero data fix as a
prerequisite. Config UNCHANGED; Auditor gates untouched (rule 8).

---

## EXP-010 2026-07-22 — H1→M30 HYBRID: H1 Council bias/veto + M30 entry TIMING (NEW family)

Status: **PRE-REGISTERED, NOT RUN.** No results below — this entry is the pre-commit only.
Analysis-only mandate: `config/base.yaml`, `council/`, `backtest/engine.py`, `feed/`, `risk/`,
`watchman/` NOT modified. This is the single surviving open question from the 2026-07-22 Timeframe
probe NOTE (the last entry above): reject lower-TF as PRIMARY signal stands; test the HYBRID.
**BLOCKED — do NOT execute until the prerequisites in §6 clear (esp. the spread-zero data fix).**

### 0. What this is / NEW family / why EXP-005 & EXP-007 do NOT already answer it

The TF-probe NOTE confirmed H1 as the only timeframe with proven edge (H1 full-history PF 1.081;
M30/M15-as-primary collapse to PF_ex5 0.999/0.994) and flagged exactly one untested follow-up: a
HYBRID where the **H1 Council keeps its bias/veto role unchanged** (same signals, same all-24h
gate, same be/trail-OFF Watchman) but the **ENTRY is timed on M30** — a tighter, structure-based
stop and a better-priced/faster fill, motivated by (i) the user's "เทรดเข้าออกเร็ว" (faster
in/out) and (ii) the $3,000 min-lot skip problem (a tighter M30 stop → larger lot for the same
$risk → fewer sub-min-lot skips; deposit will NOT be increased — user constraint).

This is a **genuinely NEW family** (H1→M30 entry-timing) with its own multiple-testing budget and
its own UNSPENT one-touch Test allowance. It is **mechanically different** from EXP-005 (M15) and
EXP-007 (M30), which must be stated because a careless reader would think those rejections already
close it:

- **EXP-005/007 asked:** does lower-TF pre-entry STRUCTURE *discriminate* which H1 signals win vs
  lose — i.e. lower-TF as a FILTER on the SAME H1 entry (same bar-i+1-open price, same H1 stop, same
  trade set minus the ones filtered out). Verdict: no M15/M30 feature predicts H1 outcome (7/8 and
  8/8 features fail cross-split / coherence).
- **EXP-010 asks a DIFFERENT question:** take the SAME H1 signals (no filtering, no signal
  selection) but change the **entry PRICE and STOP PLACEMENT** — enter on an M30 trigger with an
  M30-structure stop. This alters R-per-trade, lot size, and the give-back/whipsaw profile; it does
  NOT claim any predictive edge in M30 structure. **You can have ZERO M30 predictive edge (EXP-005/007's
  finding) and STILL change expectancy purely by paying a better entry price and risking less per
  trade.** EXP-005/007 therefore do not bear on this. (They ARE relevant as priors: EXP-005 §5's
  give-back finding — losers already exit fast, median SL hold 12h vs TP 25h — warns that a TIGHTER
  M30 stop risks *increasing* the stop-out/whipsaw rate, one of the falsifiable failure modes in §1.)

Also NOT assumed to transfer: **EXP-003's all-24h session result was established on H1 entry timing
only.** The H1 Council still fires all-24h (inherited, unchanged), but the actual M30 FILL hour is a
NEW distribution (a pullback trigger can fire at :30 past the hour, or slip into a thinner-liquidity
pocket than the H1 bar-i+1 open would). The all-24h choice is INHERITED (not re-tuned here) but the
M30-fill-hour distribution is a mandated post-hoc DIAGNOSTIC (§5 (i)), not a swept parameter.

### 1. Hypothesis (falsifiable, pre-registered)

**H1:** Entering each H1 Council signal via an **M30 pullback-then-resume** trigger with an
**M30-swing-structure stop** improves risk-adjusted performance (per-year PF and avgR) AND
small-account tradability (lower stop distance → fewer sub-min-lot skips at $3,000/1.0%) versus
plain H1 bar-i+1-open entry with the H1 ATR stop — WITHOUT dropping trade count below the 100/yr
floor and WITHOUT turning any H1-positive year net-negative.

**Null / falsifiers (any one ⇒ REJECT, H1-as-is stands):**
- (F1) Cost/R erosion: the TF-probe measured round-trip cost rising ~1.7%→~2.5% of R from H1→M30
  (tighter stop = smaller R = larger % cost). A better entry price may not clear this drag.
- (F2) Whipsaw: a tighter M30 stop raises the stop-out rate on the SAME signals (EXP-005 §5), giving
  back the better entry.
- (F3) Trade-count starvation: a pullback that never resumes → signal EXPIRES → fewer trades; may
  breach the 100/yr floor (worse than H1, and worse for the 200-trade gate).
- (F4) sl_min_atr clamp defeats the motivation: if the M30 stop is floored back up to the H1-scale
  minimum, the "tighter stop → larger lot" benefit evaporates (see §2 ATR-unit decision).
- (F5) Regime artifact: an edge owed only to the 2025-26 gold uptrend (M30 covers the full 2021-22
  choppy regime — unlike M15 — so Y1 is a hard, mandatory test; TF-probe finding #2).

### 2. Mechanism spec (concrete enough to implement)

H1 Council fires signal `dir∈{BUY,SELL}` at H1 bar `i` close (as_of = close[i]) — UNCHANGED from
production. Instead of the current fill at bar `i+1` OPEN, ARM a window of `N` M30 bars over
`[close[i], close[i] + N·30min)` and apply the entry trigger:

**PRIMARY mechanism — "M30 pullback-then-resume" (the one that decides this experiment):**
1. Walk the M30 bars in the arming window in order. First find a **pullback** M30 bar against `dir`
   (for BUY: an M30 bar making a lower low than the prior M30 bar / closing below the prior M30
   close; symmetric for SELL). Record the pullback extreme (`pb_low` for BUY / `pb_high` for SELL).
2. Enter on the first SUBSEQUENT M30 bar that **resumes** in `dir` (for BUY: closes above the high
   of the pullback bar; symmetric SELL). Fill = that M30 bar's close→next-M30-open convention,
   mirroring the engine's bar-close-decide / next-bar-open-fill discipline (no lookahead).
3. **SL** = `pb_low − sl_buffer_atr·ATR_M30` (BUY) / `pb_high + …` (SELL), i.e. off the M30 swing
   structure, then **clamped to [sl_min_atr, sl_max_atr]·ATR_M30**. **TP** = entry ± `tp_r_multiple`
   (2.0, UNCHANGED) · stop_distance — R-multiple based, off the NEW tighter stop.
4. If no pullback-then-resume completes within `N` M30 bars, the **signal EXPIRES** (no trade) —
   this is the F3 trade-count risk, measured, not assumed away.

**ALTERNATIVE mechanism (at most one, declared, NOT run in this experiment):** "M30 structure
break / momentum continuation" — enter on the first M30 bar that breaks the M30 swing high (BUY)
formed inside the arming window (breakout confirmation instead of pullback). This is a DISTINCT
entry rule and would need its OWN pre-registration; running BOTH and keeping the better one would
inflate the family count and invite winner's curse (rule 7). **Only the PRIMARY decides EXP-010.**

**ATR-unit decision (pinned, NOT swept — flagged as a load-bearing assumption):** `ATR_M30` = a
short-lookback (14-bar) M30 ATR, computed INDEPENDENTLY of the H1 Council. sl_buffer/min/max_atr
are applied in **M30-ATR units** so the floor stays proportional to M30 volatility and the tighter
stop SURVIVES. If H1-ATR units were used instead, sl_min_atr=0.8·ATR_H1 would clamp most M30 stops
back up to ~H1 width — defeating the whole motivation (F4). This choice is held FIXED; if the
primary mechanism fails, whether the ATR-unit choice caused it is a separate follow-up, not swept
here.

**Risk Voice re-check timing (pinned):** Risk Voice bias/veto runs at the H1 signal bar `i` and
gates whether we ARM at all (unchanged). Because the actual fill now occurs up to `N` M30 bars
later, a **lightweight Risk Voice RE-CHECK runs at the candidate M30 entry bar** before the fill:
spread (`max_spread_multiple`, `max_spread_points_xauusd`), news blackout, ATR panic
(`max_atr_panic_multiple`), and `max_stop_atr_multiple`/`sl_max_atr` (2.5) evaluated against the
**NEW M30 stop**. Rationale: spread and imminent-news conditions can change during the arming
window; a stale H1-bar Risk Voice check would let a fill through into freshly-bad conditions. The
all-24h session gate is inherited (M30-fill-hour is diagnostic-only, §5 (i)).

### 3. Sweep families (staged; each is ONE coupled-pair experiment — NOT a 4-D joint grid, rule 3)

Two staged coupled-pair sub-experiments. 10b runs ONLY if 10a yields a candidate clearing §5 vs
H1-as-is (avoids sweeping Watchman re-scale over a mechanism that already failed).

| stage | coupled pair (2-D grid) | grid | held-fixed while sweeping |
|-------|-------------------------|------|---------------------------|
| **10a** (primary: entry+stop micro-structure) | arming window `N` (M30 bars) × M30 `pivot_bars` (swing lookback for the stop) | N ∈ {2,4,6,8} (=1h/2h/3h/4h) × pivot ∈ {2,3,4} → 12 cells | time_stop_hours=48, dead_trade_r_band=0.3 (H1 defaults), be/trail OFF, tp 2.0, all-24h |
| **10b** (Watchman re-scale for shorter M30 holds — CONDITIONAL on 10a) | time_stop_hours × dead_trade_r_band | time ∈ {12,24,36,48} × dead ∈ {0.2,0.3,0.4} → up to 12 cells (coupled pair) | 10a's winning (N × pivot); be/trail OFF; tp 2.0 |

10b's rationale: H1 median hold is 16.5h and M30-as-primary was 7.5h, so the hybrid's holds are
likely SHORTER than H1 → the 48h time-stop may rarely bind and the ±0.3R dead-trade band may need
re-scaling for the faster cadence. These are the two Watchman params whose H1 calibration is most
suspect under shorter holds; they are re-scaled, NOT re-optimized for their own sake.

Multiple-testing (rule 7): PRIMARY path = 10a's 12 cells (+ up to 12 in 10b IF triggered) = ≤24 in
this new family. If ≤20 evaluated when a candidate is chosen, standard bar; if >20 (i.e. 10b runs),
hold the HIGHER bar (larger per-year edge, no sign flips across all 4 years) per rule 7. The
ALTERNATIVE breakout mechanism is explicitly OUT (own future pre-registration) to keep the count honest.

### 4. Baseline to beat — H1-AS-IS (must beat H1, NOT merely beat M30-as-primary)

The hybrid must beat plain-H1 CURRENT-LIVE config (be/trail OFF, pivot 3, all-24h, tp 2.0). On the
log's own splits, that baseline = EXP-008's `Struct+Time` condition (= current `config/base.yaml`):

| window | H1-as-is PF / net (trades) | source |
|--------|----------------------------|--------|
| Y1 2021-22 (Train) | 1.022 / +156 (271) | EXP-008 §6.1 Struct+Time |
| Y2 2022-23 (Train) | 0.959 / −314 (260) | " |
| Y3 2023-24 (Train) | 1.148 / +901 (229) | " |
| Y4 2024-25 (Val)   | 1.051 / +337 (263) | " |
| Y5 2025-26 (Test)  | 1.304 / +1508 (214) | EXP-008 §6.5 Struct+Time |

Cross-check (informational, NOT the Train/Val bar): TF-probe common-window (2025-02→2026-07) H1 =
PF 1.215 / DD 11.54% / avgR 0.118 / 332 tr. The hybrid must clear the H1 per-year table above — NOT
the rejected M30-as-primary (full-history PF 1.007 / PF_ex5 0.999), which is NOT a valid baseline.

### 5. Acceptance criteria (pre-registered)

Deciding metric: **per-year PF + net $ + avgR** vs H1-as-is (the robustness bar decisive in
EXP-002/003/004/006/008), gated by trade floor and plateau. ADOPT-CANDIDATE the hybrid (config +
spec change is the USER's call — analysis-only) iff ALL:
- (a) **Trade floor:** ≥100 trades in EVERY year Y1–Y4 (and ≥100 on Test). If the pullback-expiry
  (F3) drops any year <100 → report **INSUFFICIENT DATA** for that config (widen window or state
  insufficient) — never extrapolate (rule 6).
- (b) **vs H1 per-year:** hybrid PF ≥ H1 PF in a MAJORITY of Y1–Y4, AND not materially worse (PF
  gap > 0.03) in ANY year, AND avgR(hybrid) ≥ avgR(H1) in a majority.
- (c) **No sign flip (F5/whipsaw guard):** hybrid turns NO H1-positive year net-negative (the
  EXP-002 tp2.5 / EXP-004 [0,22) / EXP-006 failure mode).
- (d) **Plateau (rule 5):** the winning 10a `(N × pivot)` cell's ±1-grid-step neighbors within ~15%
  PF; reject isolated peaks. Same for the 10b re-scale pair if run.
- (e) **Per-year incl. 2021-22 (F5):** M30 covers the full Y1 choppy regime — a candidate owing its
  edge to the 2025-26 uptrend only is REJECTED. Y1 is a hard, mandatory pass.
- (f) **Small-account tradability (secondary check, must-not-regress; NOT an edge claim):** at
  $3,000 / 1.0% risk, hybrid sub-min-lot skip% ≤ H1's (the motivation). A hybrid that improves
  skip% but FAILS (a)–(e) is still REJECTED — tradability never overrides the edge/robustness bar.
- (g) **Cost honesty (rule 1):** (a)–(f) computed ONLY after the §6(a) spread-zero fix. Any run on
  zero-spread-contaminated data is EXPLORATORY ONLY and can never justify adoption (the zero-spread
  bias flatters the tighter-stop hybrid MORE than H1 — cost/R is larger at M30).
- (h) **Test (touched ONCE):** the single best survivor of (a)–(g) confirms (a)–(c) on
  2025-07-21→2026-07-21 vs H1-as-is. One touch only; refuse to re-touch to fish for a better story.

Else REJECT (H1-as-is stands) or INSUFFICIENT DATA. Auditor gate thresholds NOT touched (rule 8).

### 6. Prerequisites / BLOCKERS (must clear before any sweep — currently BLOCKED)

- **(a) Spread-zero data fix — HARD BLOCKER (rule 1).** TF-probe finding #5: historical `spread`
  is ZERO on ~50% of H1 bars and **43% of M30 bars**, so spread+slippage is understated wherever
  zeros occur, and this flatters lower TFs MORE (cost/R is larger at M30). The hybrid's entire
  thesis is a small-R trade whose viability hinges on honest cost — so a realistic spread floor
  (assigned to another session, NOT yet done) is a precondition. **Per rule 1, I will REFUSE to run
  the sweep until the M30 cost model is honest**; any pre-fix run is exploratory (criterion §5(g)).
- **(b) H1→M30 bridge harness — NEW code, does not exist.** Nothing in `backtest/engine.py` supports
  two-TF replay (grep: single-symbol, single-TF `position` loop; §Shield-NOTE confirmed). Minimal
  design, PRODUCTION UNTOUCHED (lives in `experiments/`, like the EXP-005/007/009 harnesses):
  (1) reuse the stock H1 engine / stock signal fn to emit each H1 signal with `(as_of, dir, H1-ATR)`
  — exactly the EXP-005/007 pattern; (2) NEW `m30_entry_bridge`: for each H1 signal, walk M30 bars
  in the arming window, apply the §2 pullback-then-resume trigger + Risk Voice M30 re-check, compute
  the M30 stop; (3) simulate the resulting position forward on the M30 clock (SL/TP + always-on
  structure-invalidation + time-stop/dead-trade) driving `watchman/evaluate.py::evaluate_watchman`
  on M30 bars, REUSING `backtest/cost_model.py` (commission $7/lot + slippage = bar's own spread)
  and the cached IC Markets `SymbolSpec` UNCHANGED. Fidelity check REQUIRED before any sweep: the
  degenerate config (N=1, trigger = "enter at first M30 open, H1-ATR stop") must approximately
  reproduce the H1 bar-i+1-open baseline. Data already in hand: `data/historical/XAUUSD_M30.csv`
  (EXP-007 validated M30→H1 OHLC BYTE-EXACT, full coverage back to 2020-06-22 — a prior, not a blocker).
- **(c) `features/indicators.rolling_average` 480-bar H1 assumption.** It hardcodes 480 bars =
  "20 days" — true only on H1 (480 M30 bars = 10 days). The harness must NOT naively call it on M30
  bars. Handling: the H1 Council keeps H1 data + its native 480-bar (20-day) rolling_average
  UNCHANGED (bias/veto stays pure-H1); the M30 layer computes its OWN short-lookback (14-bar) ATR
  for stop sizing and does NOT reuse the 480-bar H1 rolling_average. If any M30 relative-spread/ATR
  context ever needs the 20-day window, the harness must use 960 M30 bars to preserve wall-clock —
  flagged so it is a conscious choice, not a silent 10-day contamination.

### 7. Data & split discipline

Usable overlap 2021-07-22→2026-07-21 (H1 start binds; M30 CSV runs earlier, 2020-06-22, but the H1
Council needs H1 data). Chronological, no shuffle, reuse the whole log's splits:

| Split | Range | use |
|-------|-------|-----|
| Train | 2021-07-22 → 2024-07-21 | sweep 10a/10b, per-year Y1,Y2,Y3 |
| Validation | 2024-07-21 → 2025-07-21 | compare candidates, Y4 |
| Test | 2025-07-21 → 2026-07-21 | **NEW family's UNSPENT one-touch** — single best candidate ONLY, after Train+Val |

**Test-overlap disclosure (rule 2 honesty):** the TF-probe NOTE ran UNSELECTED baselines (no
candidate tuned or picked) over 2025-02-20→2026-07-21, which OVERLAPS Test — a window forced by M5
data availability, not chosen post-hoc. Per that NOTE's reasoning, running unselected baselines does
not spend the selection budget. Treatment here: those TF-probe M30-as-PRIMARY numbers are a
DIFFERENT mechanism and are NOT reused as hybrid evidence; the hybrid family's one-touch Test
allowance is UNSPENT, and its §5(h) confirmation will be a FRESH run of the specific tuned hybrid
candidate vs H1-as-is on the exact Test window — touched once.

### 8. Spec bounds

- M30 `pivot_bars` {2,3,4}: `swing_pivot_bars` is `[adjustable]` (§6), no hard bound — in bounds.
- `tp_r_multiple` stays 2.0 (unchanged). `time_stop_hours`/`dead_trade_r_band` `[adjustable]`.
- `sl_max_atr` ≤ 2.5 is the Risk-voice ceiling (Appendix A) — the M30 stop MUST still respect
  sl_max_atr=2.5 (in M30-ATR units); within bounds, not swept.
- **Arming window `N` is a NEW parameter with NO spec entry.** Appendix A does not describe two-TF
  entry timing. Adopting the hybrid would therefore require the USER to **amend the spec FIRST**
  (add the M30-entry-timing rule + `N`/M30-ATR-unit conventions), BEFORE any `config/base.yaml`
  change. This pre-registration flags that — it does NOT amend the spec and does NOT modify config
  (rule 10 / analysis-only). Auditor gate thresholds NOT touched (rule 8).

**Status (superseded): PRE-REGISTERED, then RUN 2026-07-22 after §6 prerequisites cleared. See §9–§12 RESULTS below.**

---

## EXP-010 RESULTS (run 2026-07-22) — VERDICT: REJECT (H1-as-is stands); 10b NOT triggered

Analysis-only: `config/base.yaml`, `council/`, `backtest/engine.py`, `feed/`, `risk/`, `watchman/`
NOT modified. New code lives only in `experiments/exp010_h1_m30_hybrid_harness.py` (bridge harness,
production pure-functions reused verbatim; driver/caching in scratchpad). Test one-touch budget for
this NEW family: **UNSPENT** (no candidate survived Train+Val → §5(h) never invoked; Test not touched).

### §9. Corrections applied vs the pre-registration (both flagged before running)
1. **Commission = $0/lot** (IC Markets **Standard**), NOT the $7 in §6(b)'s note (a Raw-Spread
   assumption since corrected). This makes F1 (cost/R erosion) *EASIER* to clear than the
   pre-registration assumed — honestly noted; and as the results show, F1 is not even the binding
   falsifier, so the cheaper cost does not rescue the hybrid.
2. **§4 baseline re-computed fresh** on floored data at comm $0 (NOT the stale EXP-008 Test PF 1.304).
   Fresh H1-as-is per-year table (current-LIVE config: Watchman struct+time ON / be+trail OFF, pivot 3,
   all-24h, tp 2.0; equity $10k; RV all-24h ON; comm $0) — reproduces the RE-VERIFICATION P1 BOTH-OFF
   column to the cent:

| Year | H1-as-is PF | net $ | trades | avgR | DD% |
|------|-------------|-------|--------|------|-----|
| Y1 2021-22 (Train) | **1.001** | +20  | 277 | 0.010 | 14.3 |
| Y2 2022-23 (Train) | 0.967 | −548 | 256 | −0.022 | 28.9 |
| Y3 2023-24 (Train) | 1.199 | +2935 | 234 | 0.123 | 15.2 |
| Y4 2024-25 (Val)   | 1.101 | +1557 | 262 | 0.068 | 10.4 |

### §10. Fidelity check (REQUIRED before the sweep, §6(b)) — PASSED
Degenerate config (N=1, "enter at first M30 open, H1-ATR stop") on the M30 clock, Watchman OFF +
M30-recheck OFF (the H1-as-is baseline has neither an M30 re-check nor — for this check — Watchman),
vs the STOCK H1 engine (`run_backtest`, Watchman OFF, RV all-24h, comm $0). Both SL/TP-only:

| Year | H1 engine (SL/TP) tr / PF / net | Degenerate M30 twin tr / PF / net |
|------|----------------------------------|-----------------------------------|
| Y1 | 198 / 1.052 / +656  | 210 / 1.085 / +1174 |
| Y2 | 210 / 0.940 / −892  | 207 / 0.992 / −128  |
| Y3 | 179 / 1.181 / +2406 | 188 / 1.292 / +4494 |
| Val| 223 / 1.129 / +1925 | 235 / 1.118 / +1857 |

Same net **sign every year**, trade counts within ~6% (198↔210, 210↔207, 179↔188, 223↔235). The
twin runs a touch HOTTER (PF +0.03…+0.11) because M30 exits resolve intrabar SL/TP more finely than
the engine's *pessimistic* "SL-priority on an H1 same-bar double-touch" — an expected, directionally-
sensible modelling difference (largest in Y3, the most large-range/trending year), NOT a harness bug.
**Honesty caveat carried forward (the "M30-granularity premium"):** because the hybrid is simulated on
M30 while the H1-as-is baseline is on H1, a hybrid PF ~0.03–0.11 above the H1 baseline could be pure
simulation granularity, not entry-timing edge. The hybrid must clear the baseline by MORE than that to
be real — and (see §11) it does not clear it at all; it fails badly, so this caveat only makes the
REJECT more secure. RV M30-recheck decoupled via `--m30-recheck` (docstring: an H1-scale stop checked
against the smaller M30-ATR spuriously vetoes ~84% in degenerate mode; irrelevant to the real
pullback mode, whose stop IS M30-ATR-scaled).

### §11. 10a sweep — arming window N × M30 pivot depth (12 cells), per-year PF (trades)
Pullback-then-resume, Watchman struct+time ON / be+trail OFF, RV all-24h + M30 entry-bar re-check ON,
tp 2.0, time_stop 48h, dead-band 0.3, equity $10k, comm $0. Stop = M30 pullback pivot − 0.2·ATR_M30,
clamped to [0.8, 2.5]·ATR_M30 (M30-ATR units, §2). Mechanism/ambiguity calls documented in the harness
docstring (pb-anchor = left-side fractal pullback pivot of depth `pivot`; entry = resume close→next
M30 open). **PF per year (trades in parens); bold = beats the H1-as-is baseline PF that year:**

| cell | Y1 (base 1.001) | Y2 (0.967) | Y3 (1.199) | Val (1.101) |
|------|-----------------|------------|------------|-------------|
| N2·p2 | 0.702 (116) | **1.105** (111) | 0.938 (109) | **1.265** (116) |
| N2·p3 | 0.656 (88❌) | **1.060** (93❌) | 0.943 (91❌) | **1.201** (99❌) |
| N2·p4 | 0.674 (74❌) | **1.122** (81❌) | 0.968 (77❌) | **1.324** (85❌) |
| N4·p2 | 0.770 (228) | 0.948 (227) | 0.912 (227) | 1.018 (220) |
| N4·p3 | 0.767 (195) | 0.892 (196) | 0.973 (199) | 1.021 (179) |
| N4·p4 | 0.734 (173) | 0.947 (171) | 1.032 (166) | 1.016 (154) |
| N6·p2 | 0.877 (279) | 0.872 (301) | 0.894 (266) | 1.081 (275) |
| N6·p3 | 0.801 (253) | 0.919 (264) | 0.965 (235) | 1.099 (231) |
| N6·p4 | 0.782 (235) | 0.948 (243) | 0.991 (208) | 1.023 (211) |
| N8·p2 | **0.943** (313) | **0.971** (319) | 0.889 (306) | 1.084 (313) |
| N8·p3 | 0.902 (274) | **0.976** (289) | 0.957 (278) | 1.063 (264) |
| N8·p4 | 0.817 (262) | **1.019** (271) | 0.973 (241) | **1.092** (239) |

(❌ = fails the ≥100-trade/year floor, §5(a). N2·p3 and N2·p4 are INSUFFICIENT DATA on that ground
alone. Val PF figures are exact from the run: N6·p2 1.081, N8·p2 1.084.)

### §12. Acceptance-criteria scoring (§5) and VERDICT — REJECT
- **(e) Mandatory Y1 (2021-22 chop, F5): FAIL — every one of the 12 cells.** Baseline Y1 is
  net-POSITIVE (PF 1.001 / +$20). EVERY hybrid cell has Y1 PF < 1.0 and is net-NEGATIVE; the best is
  N8·p2 at 0.943 / −$1,018 (worst-case cells reach −$3,020). The tighter M30 stop + pullback entry is
  whipsawed in the choppy 2021-22 regime — the F2 (whipsaw) × F5 (regime) failure the pre-registration
  explicitly flagged, confirmed decisively.
- **(c) No sign flip: FAIL.** Every cell turns the H1-positive Y1 net-negative; MOST cells also turn
  Y3 (baseline's best year, +$2,935 / PF 1.199) net-negative or marginal (Y3 hybrid 0.89–1.03) — the
  tight M30 stop gives back the wide-H1-stop's trend capture in the 2023-24 uptrend.
- **(b) vs H1 per-year: FAIL.** No cell beats the H1 baseline PF in a MAJORITY of Y1–Y4 without a
  material (>0.03) regression somewhere; every cell is materially worse in BOTH Y1 and Y3. Cells win
  on Y2 (a baseline losing year anyway) and Val, but never on the two years that carry the strategy.
- **(a) Trade floor: N2·p3 / N2·p4 fail (<100/yr) → INSUFFICIENT.** Others clear it.
- **(d) Plateau: moot** — there is no passing region to be on a plateau of. (Response surface: larger
  N mitigates Y1 damage — N8·p2 is the least-bad Y1 — but never enough to pass, and larger N erodes
  Val; no stable good cell exists.)
- **(g) Cost honesty: satisfied and STRENGTHENS the reject.** Run on floored data at the correct
  comm $0 (F1 made easier than the pre-reg's $7). The hybrid still fails — so cost/R erosion (F1) is
  NOT the binding constraint; whipsaw (F2) and regime (F5) are. F4 (min-clamp defeats tight stop) was
  NOT reached — the M30 stop stayed genuinely tighter (see §5(f)).
- **(f) Small-account tradability (secondary, must-not-regress): confirmed mechanically but marginal
  and moot.** At $3,000 / 1.0% / live `min_lot_cap=1.5`, the tighter M30 stop cuts sizing-skips
  0.5%→0.0% (motivation holds directionally) — BUT the H1 baseline skip is ALREADY ~0.5% because the
  adopted `min_lot_risk_cap_pct=1.5` fallback already solved the skip problem, and the pullback trigger
  EXPIRES in ~82% of arming windows (F3 trade-count starvation; e.g. N4·p3 filled 769 vs expired
  3495 across Train+Val). Per §5(f), tradability never overrides the edge/robustness bar → REJECTED
  regardless.
- **Multiple-testing (rule 7): 12 cells this NEW family (<20)** → standard bar; moot (nothing passes).
- **10b NOT triggered** (§3: runs only if 10a yields a §5-clearing candidate — none did). The Watchman
  time_stop×dead_band re-scale is not explored, correctly, since it cannot rescue a mechanism that
  fails on entry/stop structure in the two decisive years.

**VERDICT — REJECT. H1-as-is entry stands; the H1→M30 pullback-entry hybrid is NOT adopted.** Falsifiers
F2 (whipsaw), F3 (expiry starvation) and especially F5 (Y1 regime) all fire; F1 is not binding; F4 not
reached. The hybrid's tighter M30 stop trades away exactly what the H1 wide-ATR stop earns — riding the
2021-22 chop without over-tightening (Y1) and riding the 2023-24 trend (Y3). Adopting it would require a
spec amendment for the NEW parameter `N` (§8) — MOOT, since there is no candidate to adopt. **Test set
(2025-07-21→2026-07-21) NOT touched — this family's one-touch budget remains UNSPENT/preserved** (rule 2).
`config/base.yaml` NOT modified (analysis-only). Auditor gate thresholds NOT touched (rule 8).

---

## NOTE (not an EXP) 2026-07-22 — Historical `spread` zero-value floor (cost-model data-integrity fix)

Data-integrity fix, NOT a parameter search / edge selection — so NO EXP-### pre-registration, NO
Train/Val/Test discipline, NO plateau protocol (same convention as the sizing/Shield NOTEs above).
Test one-touch budget UNSPENT/NA. **Directly CLEARS the EXP-010 §6(a) HARD BLOCKER** (spread-zero
fix), which per rule 1 was gating that experiment's sweep. `config/base.yaml`, `src/`, `tests/`
UNCHANGED (this is a `data/historical/*` fix only). pytest: 1072 passed (unchanged — no code touched).

### What was wrong
`backtest/cost_model.py` uses the bar's OWN `spread` column as both the spread cost AND (via
`slippage_points=None`) the min-1-spread slippage assumption — effective modeled round-trip cost =
`(spread + slippage)*point*point_value*lot` = **2x the raw per-bar spread points** ($/lot; slippage
defaults to spread). But MT5's `copy_rates_range`/`copy_rates_from` does NOT retro-populate real
spread for older historical bars — it returns `spread=0` (bid==ask, non-physical, never a real
market condition). Any bar with `spread=0` was therefore modeled at **ZERO transaction cost** (both
spread and slippage), understating cost on ~half the dataset and flattering every past/future
backtest's PF/net on the affected bars. `scripts/download_historical.py` -> `feed/historical.py`
does NOT post-process spread (grep: 0 matches), so this is inherent to the raw MT5 pull.

### Investigation (measured this session; XAUUSD_H1 2021-07-22->2026-07-21, 29,543 bars)
| symbol | zero% | zeros | nonzero mode / median / mean | note |
|--------|-------|-------|------------------------------|------|
| XAUUSD H1 | 50.2% | 14,839 | 5 / 5 / 4.86 | clean, large real sample (14,704 bars) |
| EURUSD H1 | 95.9% | 11,902 | 21 / 15 / 14.95 | only 511 real bars (thin, likely news-biased) |
| GBPUSD H1 | 29.7% | 3,692 | 1 / 1 / 2.94 | nonzero DOMINATED by implausible 1-pt (0.1 pip) |
| USDJPY H1 | 77.2% | 9,588 | 1 / 1 / 8.49 | nonzero also dominated by implausible 1-pt |

Zeros are NOT confined to old history — they are scattered across the whole 5-yr span, only
CONCENTRATED in older years: XAUUSD H1 zero% by year = 2021 90%, 2022 91%, 2023 56%, 2024 16%,
2025 28%, 2026 36%. (So the recent Test year 2025-26 is only ~30% contaminated; the Train years
2021-22 are ~90% — this matters for the sanity check below.) XAU intraday files show the same:
M30 44%, M15 28%, M5 4% zeros. Matches the 2026-07-22 TF-probe NOTE finding #5 (50%/43%/28%).

### Floor derivation (POINTS) — reconciling empirical vs published $7/lot
XAUUSD SymbolSpec (harness-validated in the log header; confirmed by 2-digit price 1799.09):
`point=0.01, tick_size=0.01, tick_value=1.0 -> point_value=tick_value/tick_size=100 $/price-unit/lot`.
So **1 spread point = $1/lot** raw round-trip (S*0.01*100). IC Markets Standard published XAU
round-trip ~ **$7/lot** (all spread, no commission) => **7 points** as an AVERAGE (tail-inflated by
news/rollover bars). Empirical NON-zero spread on THIS feed: mode 5, median 5, mean 4.86 (from 14,704
real bars) => **5 points** as the TYPICAL/modal bar.

Reconciliation: 5 (empirical typical) vs 7 (published average) agree to order-of-magnitude; the gap
is exactly the tail (news/rollover bars pull the mean toward 7 while the mode stays 5) plus a possible
account-tier difference (this demo feed vs a retail Standard table). **Chosen floor = 5 points**,
because (i) it is the mode = median ~ mean of a large, clean real-bar sample from the exact feed being
patched — the best point-estimate of the missing spread on a typical bar; (ii) it does NOT overwrite
the tail — a floor only lifts values BELOW it; (iii) CRUCIALLY, the cost model's 2x convention means a
floored bar's EFFECTIVE modeled cost = 2*5 = **$10/lot** round-trip, already MORE conservative than
the published $7/lot real all-in cost. Flooring at the full 7 would give effective $14/lot (2x reality)
— double-conservative and punitive (could reject genuinely-good configs), which is why the raw floor is
NOT the whole $7 figure. Per spec §5.2 the spread column should hold the REAL average spread and the
min-1-spread slippage is a SEPARATE additive conservative buffer ON TOP — so 5 (real) + 5 (slippage
buffer) is the spec-correct construction, NOT netting slippage out to force effective=$7.

Applied ONLY to `spread == 0` rows (the non-physical MT5 quirk). Populated nonzero values 1-4 pts were
LEFT UNTOUCHED — they are genuine recorded observations, not the not-populated defect (which is exactly
0); overwriting them would fabricate cost on legitimately-tight bars.

FX floors (per-symbol, NOT XAUUSD's number — different point conventions; all 5-digit except JPY
3-digit => 1 pip = 10 points on every one here). FX empirical distributions are too degraded to trust
(EUR: 511 real bars; GBP/JPY: nonzero DOMINATED by an implausible 1-pt = 0.1-pip value), so the floor
falls back to published IC Markets Standard typical spread — the more trustworthy of the two views for
these symbols (the opposite call from XAUUSD, where the huge clean empirical sample wins). Floors:
EURUSD **10 pts** (~1.0 pip), GBPUSD **13 pts** (~1.3 pip), USDJPY **10 pts** (~1.0 pip). Again applied
to `spread == 0` rows only. CAVEAT: for GBP/JPY the zero-floor does NOT fully fix the file — the BULK of
their POPULATED spreads are an implausible 1 pt (0.1 pip), so those feeds are fundamentally unreliable
and MUST be re-downloaded with proper spread capture before any FX go-live, not merely patched. FX
pairs are disabled in `config/base.yaml` and read by no current backtest, so this is precautionary.

### Files edited (all gitignored — `.gitignore` line 14 `data/historical/*`; NOT committed, exist on disk)
XAUUSD_H1 (14,839->5), EURUSD_H1 (11,902->10), GBPUSD_H1 (3,692->13), USDJPY_H1 (9,588->10),
XAUUSD_M30 (31,267->5), XAUUSD_M15 (27,622->5), XAUUSD_M5 (4,284->5). Verified byte-level: for XAUUSD_H1,
ALL non-spread columns (OHLC/tick_volume/real_volume) are IDENTICAL to the pre-edit backup (0 diffs),
only the 14,839 zero-spread cells changed, 0 populated cells touched, 0 zeros remaining. Same
zeros-only logic on every file.

### Before/after backtest sanity check (magnitude of the bug)
Engine bakes spread into the FILL price, so changing spreads shifts fills -> SL/TP timing -> with
`max_positions_per_symbol=1` the downstream trade SET RESHUFFLES (a trade count change, not just a
per-trade cost delta). So the clean directional effect only shows where zeros dominate:
- **Full history (2021-07-22->2026-07-21, comm $7, equity $10k):** before PF 1.0800 / net +$6,522.67 /
  DD 29.45% / 1259 tr -> after PF 1.0782 / net +$6,213.65 / DD 31.05% / 1277 tr. i.e. **net -$309
  (-4.7%), DD +1.6pp, PF ~ flat** — the EXPECTED direction (more cost) once the zero-heavy 2021-22
  years (~90% zeros) dominate. Effect is modest because only the ENTRY bar's spread is charged once
  (0->5 adds 10 effective pts = $10/lot on that entry), and the reshuffle partly offsets.
- **Test window only (2025-07-21->2026-07-21, comm $0, `--out-of-sample`):** before PF 1.22 / net
  +$2,672.18 / DD 12.41% / 228 tr -> after PF 1.23 / net +$2,897.84 / DD 10.08% / 241 tr. Here the sign
  FLIPS (net UP, DD DOWN) — NOT a contradiction: this window is only ~30% zero-spread, so the direct
  cost bump is small and the fill-driven RESHUFFLE (+13 trades) dominates and happened to be favourable.
  This is exactly why the full-history number is the honest magnitude and the recent-window delta is not.
  (This before/after pair is a cost-model magnitude check run IDENTICALLY both sides, NOT an edge
  evaluation — it does NOT spend any parameter family's Test one-touch budget.)

### RECURRING GOTCHA — re-apply after ANY re-download
`scripts/download_historical.py` -> `feed/historical.py` saves raw `copy_rates_*` output and does NOT
floor spread. A fresh download of the SAME date range WILL reintroduce `spread=0` on older bars (the
MT5 quirk is a function of history depth, not of when you pull). **This floor must be RE-APPLIED after
every re-download / new-symbol download**, per-symbol: XAUUSD (any TF) -> 5 pts; EURUSD -> 10; GBPUSD ->
13 (+ re-download, feed unreliable); USDJPY -> 10 (+ re-download). Rule: replace `spread==0` only; leave
populated values. A permanent fix would post-process spread inside `feed/historical.py` (a code change,
out of this data-only mandate — flagged for the user). Verified by: re-run the per-symbol zero-count
check; all should read 0 zeros after flooring.

---

## ADDENDUM 2026-07-22 — to the TF-probe NOTE above: its tables predate two cost corrections

The "Timeframe probe: current rules on M30/M15/M5 vs H1" NOTE's tables were computed (a) BEFORE the
spread zero-value floor (previous entry) and (b) WITH `--commission-per-lot 7.0`, which commit
`eaa59c5` has since established is WRONG for this account (IC Markets **Standard** — zero commission,
cost lives in the spread; the $7 assumption double-counted). The two corrections push lower-TF
numbers in OPPOSITE directions: removing phantom commission helps lower TFs MORE (more trades,
larger lots per $ of R — e.g. M5 common-window paid ~$3.6k phantom commission vs H1's ~$0.4k), while
the spread floor hurts H1/M30/M15 more (50/44/28% zero bars vs M5's 4%). Directionally these
partially offset; the probe's VERDICT (monotone edge staircase, full-history collapse to PF~1.00 on
M30/M15, DD staircase 11%→54%, H1 confirmed) is not commission-driven and stands — but do NOT quote
the probe's exact PF/net figures as current. Any EXP-010 work must FIRST re-run its H1/M30 baselines
on the floored data with a consciously-chosen commission (now a required CLI arg) — the baseline
figures cited in EXP-010 §4 are stale for the same reason. No re-run performed here (compute-heavy;
EXP-010 re-baselines as its own first step anyway). Config UNCHANGED; Test budget UNSPENT.

---

## NOTE (not an EXP) 2026-07-22 — `feed/historical.py` now floors zero-spread at the source (closes the re-download recurrence gap)

Code-level fix, NOT a parameter search / edge selection — same convention as the two NOTEs above
(no EXP-### pre-registration, no Train/Val/Test discipline, no plateau protocol, Test one-touch
budget UNSPENT/NA). `config/base.yaml` UNCHANGED. `src/autotrade/feed/historical.py` and
`tests/unit/test_historical_download.py` CHANGED. pytest: 1085 passed (was 1072 recorded in the
prior NOTE; +8 new tests for this change, +5 from a pre-existing `test_run_shadow_loop.py` doc-count
drift found and corrected while updating `docs/test_cases.md`, unrelated to this fix).

### What this closes
The "Historical `spread` zero-value floor" NOTE above fixed the existing `data/historical/*.csv`
files on disk by flooring `spread==0` rows, but flagged a "RECURRING GOTCHA": those files are
gitignored and the fix was applied by hand, so a fresh `download_historical()` call would silently
reintroduce `spread=0` on older bars every time, with no code change to prevent it. This closes that
gap: `download_historical()` in `src/autotrade/feed/historical.py` now floors any `spread==0` row to
a per-symbol constant (`SPREAD_ZERO_FLOOR_POINTS`, defined at module scope next to
`_TIMEFRAME_DELTA`) immediately before the CSV is written, applying to whichever timeframe was
requested (not H1-only) — same floors as the manual fix: XAUUSD 5, EURUSD 10, GBPUSD 13, USDJPY 10
points. Only `spread==0` rows are touched; any populated nonzero value (even 1-4 pts) is left as-is,
matching the manual fix's deliberately conservative rule. A symbol requested with no entry in
`SPREAD_ZERO_FLOOR_POINTS` raises `HistoricalDownloadError` (matching this codebase's established
"fail loudly on missing symbol-specific config" convention, e.g. `common/symbols.py`'s
`UnknownSymbolError` for an unmapped canonical name) rather than silently skipping the floor.

### Verification
Unit tests (`tests/unit/test_historical_download.py`, mocked MT5): zero-spread rows get floored to
the correct value; nonzero rows (1, 3, 4 pts) are left untouched; each of the 4 known symbols gets
its own distinct correct floor; an unconfigured symbol raises `HistoricalDownloadError`; the floor
applies on a non-H1 timeframe (M15) too. Real-world sanity check: ran `download_historical("XAUUSD",
"H1", days=10)` against the live MT5 demo connection (writing to a scratch directory, NOT
`data/historical/`, so the already-fixed on-disk CSVs were not touched) — 38 of 167 fresh bars came
back `spread=0` from MT5 as expected, and the saved CSV had **0** zero-spread rows after the fix
(spread value_counts: 1×7, 2×6, 3×5, 4×5, 5×144 — confirms the floor lands exactly on the zero rows
and nowhere else).

### Scope / what does NOT need to change
The existing on-disk `data/historical/*.csv` files (already manually floored per the prior NOTE) do
NOT need to be re-touched or re-downloaded because of this change — this fix only prevents the bug
from being reintroduced on FUTURE downloads/re-downloads. `data/historical/*.csv` UNCHANGED by this
pass (verified: only the scratch-directory copy was written to during the live sanity check above).

---

## RE-VERIFICATION 2026-07-22 — EXP-008 / EXP-002+009 / EXP-003 re-run after TWO cost-model corrections

Status: DONE. P1 (EXP-008, LIVE config): **ADOPTED-DECISION STANDS — strengthened.** P2 (EXP-002/009
tp): **REJECT STANDS — keep tp 2.0.** P3 (EXP-003 session): **all-24h STANDS — strengthened.**
Analysis/report-only mandate: `config/base.yaml`, `src/`, `tests/` NOT modified. NOT a new parameter
selection — this re-verifies THREE already-decided questions against corrected costs. One LIVE-relevant
side-finding needs the user's attention (P1 §Gate note). Appended after re-reading the log END (another
session was concurrently appending the `feed/historical.py` floor NOTE above; no collision).

### 0. What changed vs every prior EXP (both errors now fixed)
1. **Spread zero-value floor** (NOTE above): `spread==0` bars (XAUUSD H1 ~50% overall; 90% in 2021-22,
   ~30% in the 2025-26 Test year) floored to 5 pts on disk → prior backtests understated cost, worst on
   the OLD (Train) years. Verified this session: `XAUUSD_H1.csv` now has 0 zero-spread bars, mode 5.
2. **Commission corrected to $0** (commit eaa59c5): account is IC Markets **Standard** (cost in spread,
   commission_per_lot=0), NOT Raw $7/lot as every prior EXP assumed. All re-runs use `--commission 0.0`,
   consciously chosen. Removing phantom $7/lot helps HIGH-turnover (low-TP, all-24h) configs relatively
   more; the spread floor hurts the zero-heavy OLD years more. The two pull in opposite directions.

Harness: `experiments/exp_reverify_costfix_harness.py` (committable, NEW) — builds `WatchmanConfig` with
ALL SEVEN fields INCLUDING `breakeven_enabled`/`trail_enabled`, exactly like `scripts/run_backtest.py`
main() (NOT exp009's `_build_watchman_cfgs`, which omits the two flags and silently defaults True/True —
the documented TF-probe gotcha). P2's Watchman-OFF arm reused the fidelity-validated
`exp009_tp_pivot_harness.py --watchman off`. Equity $10,000, pivot 3, cost model ON throughout (spread
baked into fill + min-1-spread slippage + commission 0). WINDOWS: Train per-year y1 2021-22 / y2 2022-23
/ y3 2023-24, val=y4 2024-07-21→2025-07-21, test 2025-07-21→2026-07-21.

---
### P1 — EXP-008 re-verification (Watchman be/trail BOTH-OFF vs BOTH-ON), Risk-Voice ON, all-24h, tp 2.0
**This RE-TOUCHES the same Test window EXP-008 already spent, with corrected data — a re-verification of
an already-spent touch, NOT a new selection.** No parameter is being chosen; both configs are fixed and
pre-decided. Structure-invalidation + time-stop are always on (BOTH-OFF = current LIVE `config/base.yaml`
= EXP-008 "Struct+Time"; BOTH-ON = pre-EXP-008 live = "AllDefaults").

| Year | BOTH-OFF (LIVE) PF / net / tr | BOTH-ON (AllDefaults) PF / net / tr | winner |
|------|------------------------------|-------------------------------------|--------|
| Y1 2021-22 (Train) | 1.001 / +21 / 277  | 0.938 / −886 / 300  | OFF (PF & net) |
| Y2 2022-23 (Train) | 0.967 / −548 / 256 | 0.885 / −1786 / 285 | OFF (PF & net) |
| Y3 2023-24 (Train) | 1.199 / +2935 / 234| 1.133 / +2058 / 275 | OFF (PF & net) |
| Y4 2024-25 (Val)   | 1.101 / +1557 / 262| 1.016 / +225 / 292  | OFF (PF & net) |
| Y5 2025-26 (Test)  | **1.268 / +3357 / 243** | 1.161 / +2037 / 278 | OFF (PF & net) |

**VERDICT P1 — ADOPTED false/false DECISION STANDS, and is STRENGTHENED.** BOTH-OFF beats BOTH-ON on
BOTH PF and net in ALL 5 years — no exception, no sign flip. Per-year sign stability (the mandate's
`max_positions=1` reshuffle concern) is clean: OFF is net-positive Y1/Y3/Val/Test, negative only Y2; ON
is negative in Y1 AND Y2. The be/trail-cut decision is more robust under honest costs, not less
(removing phantom commission does not rescue the be/trail-ON config; it stays uniformly worse). The
mechanism is unchanged from EXP-008: breakeven(1.0)/trail(1.5) engage below the 2R TP and cut winners.

**GATE NOTE — LIVE-RELEVANT, needs user attention (rule 8: reported, gate NOT touched).** EXP-008's
headline claim was that cutting be/trail moved the strategy from FAILING (PF 1.215) to PASSING (PF 1.304)
the Backtest→Paper Gate-1 `PF≥1.3` floor on the Test year. Under corrected costs that ABSOLUTE claim NO
LONGER HOLDS: the LIVE config's Test PF is now **1.268 < 1.3** (was 1.304 under phantom comm $7 +
unfloored spreads). The RELATIVE decision is unaffected (OFF 1.268 ≫ ON 1.161), and the other Gate-1
sub-criteria still pass (trades 243 ≥ 200, DD 9.2% ≤ 15%, PF_ex5 1.167 ≥ 1.0) — but the strategy as it
stands does **not** currently clear the promotion PF gate on honest costs. The Auditor gate is UNCHANGED
and must NOT be relaxed to make it pass (rule 8); the strategy must earn PF≥1.3, or stay in backtest.
(Net$ is much higher than EXP-008's — $3357 vs $1508 — because comm $0 + compounding; PF, not net, is the
gate metric, and PF fell.)

---
### P2 — EXP-002/009 tp_r_multiple re-check, Watchman OFF + Risk-Voice OFF (exact EXP-002 rejection basis)
Re-runs the EXACT condition whose rejection is under re-verification (EXP-002 = Watchman OFF, Risk Voice
OFF; the cleanest TP-isolation). Train per-year + Val ONLY. Test NOT touched.

| tp   | Y1 2021-22 PF/net | Y2 2022-23 PF/net | Y3 2023-24 PF/net | Val 2024-25 PF/net | yrs PF≥1.0 |
|------|-------------------|-------------------|-------------------|--------------------|-----------|
| 1.5  | 1.042 / +575      | 0.914 / −1343     | 1.160 / +2877     | 0.986 / −211       | 2 (Y1,Y3) |
| 1.75 | 1.039 / +505      | 0.894 / −1607     | 1.183 / +2929     | 0.974 / −400       | 2 (Y1,Y3) |
| **2.0\*** | 1.029 / +368 | 0.983 / −238      | 1.225 / +3145     | 1.095 / +1390      | **3 (Y1,Y3,Val)** |
| 2.25 | **0.805 / −2152** | 1.039 / +513      | 1.318 / +3755     | 1.125 / +1586      | 3 (not Y1) |
| 2.5  | **0.848 / −1694** | 1.134 / +1887     | 1.301 / +4080     | 1.106 / +1248      | 3 (not Y1) |

**VERDICT P2 — REJECT change; tp=2.0 STANDS.** Both prior rejection directions survive the cost
correction, and the mandate's specific worry (that the Y1 rejection of 2.25/2.5 was a 90%-zero-spread
2021-22 artifact) is DISPROVEN — under honest cost 2.25/2.5 fail Y1 *harder* than before (2.25 Y1 PF
0.833→**0.805**, net −934→**−2152**; 2.5 Y1 0.866→0.848). Lowering TP (1.5/1.75) still goes net-negative
in Val AND Y2 (unchanged). tp=2.0 is no longer *strictly* PF≥1.0 every year — Y2 now dips to 0.983
(−$238) where it was 1.001 (+$5), because the spread floor adds real cost to the zero-heavy 2022-23 bars
— but 2.0 still has the **smallest worst-year loss of any value** (−$238, vs 2.25's −$2152 / 2.5's
−$1694 / 1.5's −$1343 / 1.75's −$1607) and 3 positive years. No candidate clears the per-year robustness
bar to DISPLACE 2.0. Aggregate would favor 2.5/2.25 (huge Y3 +$4080/+$3755) but that is exactly the
Y1-choppy-regime bet EXP-002 caught, now more expensive — the per-year bar is decisive (rule 5). **tp
family Test budget NOT spent** (no candidate adopted; consistent with EXP-002/009 which never spent it).
Scope note: this re-verifies the EXP-002 Watchman-OFF basis; live now runs Struct+Time, but EXP-009 §8.2
already showed tp does not rescue anything under Watchman churn and P1 confirms the exit layer is fixed —
a full Struct+Time tp re-sweep is a separate low-priority follow-up, not needed to uphold "keep 2.0".

---
### P3 — EXP-003 session-gate sanity check: all-24h [0,24) vs [14,18), under LIVE Watchman (be/trail OFF)
all-24h side reuses P1's BOTH-OFF rows (that IS the live all-24h config); [14,18) re-run under identical
Watchman/RiskVoice, comm 0. Train per-year + Val. No Test re-touch (session family's Test consumed by
EXP-003; this is a Train+Val confirmation pass).

| Year | all-24h [0,24) PF/net/tr | [14,18) PF/net/tr | winner |
|------|--------------------------|-------------------|--------|
| Y1 2021-22 | 1.001 / +21 / 277   | **0.896 / −1214 / 197** | all-24h (Y1 sign flip) |
| Y2 2022-23 | 0.967 / −548 / 256  | 0.992 / −90 / 190       | [14,18) marg. (both net-neg) |
| Y3 2023-24 | 1.199 / +2935 / 234 | 1.113 / +1375 / 192     | all-24h |
| Y4 Val 24-25 | 1.101 / +1557 / 262 | 1.054 / +495 / 167    | all-24h |
| **Aggregate** | **+3964 / 1029 tr** | +566 / 746 tr        | all-24h (~7×) |

**VERDICT P3 — all-24h STANDS, STRENGTHENED.** all-24h wins PF in 3/4 years and net in 3/4 (only Y2, a
net-negative break-even year, marginally favors the filter). As in EXP-003 the filter TURNS Y1 POSITIVE
INTO A LOSS — and the flip is now LARGER (+$21 → −$1214, vs EXP-003's +$297 → −$518) because comm $0
rewards the higher-trade-count 24h side and the spread floor did not reverse the ranking. The decision to
remove the `[14,18)` gate only strengthens under corrected costs, exactly as the mandate predicted. No
config change (already live all-24h); Test NOT re-touched.

---
### Out-of-scope items (documented, deliberately NOT re-run)
- **EXP-005 / EXP-007 (lower-TF feature rejection): cost-INSENSITIVE, no re-run.** Their verdict rests on
  cross-split SIGN-AGREEMENT of per-feature winner-vs-loser discrimination; a uniform cost applied to
  BOTH arms of every bucket cannot manufacture discrimination or flip a sign-flip (7/8 & 8/8 features
  failed) — the finding is structurally immune to the cost corrections.
- **TF probe:** already handled by the "ADDENDUM 2026-07-22" entry (verdict cost-robust; exact figures
  stale — do not quote). No action.
- **EXP-010 (H1→M30 hybrid):** PRE-REGISTERED/NOT RUN; it re-baselines itself on floored data + chosen
  commission as its own first step. Untouched.
- **min-lot fallback (owned by another session):** its Stage-1 tables (`sizing_smallacct_harness.py`)
  were run with `--commission-per-lot 7.0` on the (now-superseded) cost assumption — those tables should
  be REFRESHED at comm $0 by that workstream BEFORE any Stage-2 `min_lot_risk_cap_pct` adoption decision.

### Test-budget accounting
- P1: re-touched the Watchman family's already-CONSUMED Test window (EXP-008 spent it) — a re-verification
  of a spent touch with corrected data, NOT a new selection; spends no fresh budget.
- P2: tp family Test budget UNSPENT/preserved (no candidate adopted).
- P3: session family Test remains CONSUMED (EXP-003); no re-touch here.
`config/base.yaml`, `src/`, `tests/` UNCHANGED. Auditor promotion-gate thresholds NOT touched (rule 8) —
the P1 Gate note REPORTS a threshold shortfall, it does not relax the gate.

---

## NOTE (not an EXP) 2026-07-22 — min-lot fallback Stage-1 REFRESH at HONEST cost (comm $0 + spread-floor)

Closes the gap the P1/P2/P3 cost-re-verification entry (above) explicitly flagged: the Stage-1 tables in
the earlier "small-account sizing REFRESH + min-lot-fallback measurement" NOTE were taken with
`--commission-per-lot 7.0` (the superseded "Raw Spread" assumption) AND on the pre-fix `spread` column
(zero-value defect, now floored per-symbol). The account is confirmed IC Markets **Standard** (commission
`$0`, cost recovered via spread). `cfo.min_lot_risk_cap_pct: 1.5` is **already LIVE** (adopted Stage-2,
commit `7ee51bc`). This refresh re-runs both sweeps at `--commission-per-lot 0.0` on the floored data to
confirm/refute that the LIVE `cap=1.5` is still the right choice under honest costs. NOT an EXP: pure
position-sizing scalars, no predictive-edge search, full history by design, no Train/Val/Test split (same
protocol as the superseded NOTE and the P1/P2/P3 passes). Test one-touch budget UNSPENT/NA.

Method: read-only. Harness `experiments/sizing_smallacct_harness.py` REUSED UNCHANGED (RiskVoice+Watchman
built with every field from `config/base.yaml`, be/trail OFF genuinely in effect). Full history
(29,543 H1 bars, 2021-07-22→2026-07-21), `--starting-equity 3000`, `--commission-per-lot 0.0`. Cost model
stays fully ON (spread + slippage=1×bar-spread still charged; $0 commission is the honest Standard-account
value, not a disabled cost model). Cells were driven one-at-a-time via a resume-able wrapper
(`scratchpad/driver.py`, imports `H.run_cell` unchanged) that fsyncs each cell to disk, because under heavy
concurrent-session CPU contention a single full-history cell costs ~950–2050 s and the environment reaped
the multi-cell background task ~3× mid-sweep; completed cells survived each reap.

### Table 2 — FALLBACK (fixed risk=1.0%, sweep min_lot_risk_cap_pct), full history, $3,000, comm $0  [COMPLETE]
```
cap%  | sig->size | skips | skip% | trades |  PF    | PF_ex5 |  net$   | avgR   | DD%   |  DD$     | maxSingleLoss$ | worstStreak$
None  |    1754   |  478  | 27.25 |  1276  | 1.0914 | 1.0694 | 2127.04 | 0.0542 | 25.95 | -1000.56 |     -71.86     |   -364.36   <- fidelity twin of riskgrid risk=1.0
1.25  |    1477   |  207  | 14.01 |  1270  | 1.1207 | 1.0935 | 2808.89 | 0.0636 | 25.95 | -1000.56 |     -72.15     |   -364.36
1.50  |    1357   |   79  |  5.82 |  1278  | 1.1294 | 1.0958 | 3061.44 | 0.0649 | 25.95 | -1000.56 |     -76.37     |   -364.36
2.00  |    1318   |   41  |  3.11 |  1277  | 1.1018 | 1.0657 | 2428.05 | 0.0586 | 25.95 | -1000.56 |    -107.94     |   -364.36
```
Aggregate net and PF both PEAK at **cap=1.5** (net inverted-U None 2127 < 1.25 2809 < **1.5 3061** > 2.0 2428;
PF likewise peaks at 1.5 = 1.1294) — same shape as the superseded comm-$7 Table 2, and every cap is a bit
better than its comm-$7 twin (cap=1.5 PF 1.1244→1.1294, net 2807→3061). Aggregate DD is constant
25.95%/−$1000.56 across all caps (the drawdown-diagnostic fact from the original still holds: rescued trades
are all post-trough 2025–26, so the fallback provably cannot touch the measured DD trough).

### Table 3 — FALLBACK-SUBSET in isolation (rescued trades ONLY), comm $0  [COMPLETE]
```
cap%  | rescued | %of executed |  subset net$ | subset PF | subset winrate | subset maxSingleLoss$
1.25  |    39   |    3.07%     |    +382.23   |   1.3487  |     53.85%     |       -72.15
1.50  |    51   |    3.99%     |    +828.38   |   1.5384  |     45.10%     |       -76.37
2.00  |    66   |    5.17%     |    +270.15   |   1.1195  |     42.42%     |      -107.94
```
Attribution verified (harness gotcha #2: ordered non-None sizing calls zipped 1:1 vs trades with a lot-equality
assert). The rescued subset KEEPS a genuine edge at cap=1.5 (**PF 1.5384**, well above the ~1.13 aggregate) —
rescued trades are still better-than-average, not dead weight. Marginal-trade read (rescue set is nested
2.0⊇1.5⊇1.25): the +12 trades from cap=1.25→1.5 add +$446 (good); the +15 from 1.5→2.0 collectively LOSE
~$558 (subset net falls 828→270, subset PF collapses 1.54→1.12) — the widest-stop marginal signals are
net-negative. So the plateau/degradation edge sits exactly at **cap≤1.5, junk starting at 2.0** — the SAME
qualitative verdict as comm-$7, and cap=1.5 is the plateau edge.

### FIDELITY CHECK — PASSED (genuine cross-invocation)
Fallback cap=None (measured in one driver run) reproduces riskgrid risk=1.0 (measured in a SEPARATE driver
run) byte-for-byte on all 11 metrics: trades 1276, PF 1.0914, PF_ex5 1.0694, net $2127.04, avgR 0.0542,
DD 25.95%/−$1000.56, maxSingleLoss −$71.86, worstStreak −$364.36, signals 1754, skips 478. ✓ (deterministic
engine; cap=None wrapper is a transparent pass-through, `if cap_pct is not None` guards the rescue branch.)

### Table 1 — RISKGRID (fallback OFF, cap=None), full history, $3,000, comm $0  [PARTIAL — 3/6 rows]
```
risk% | sig->size | skips | skip% | trades |  PF    | PF_ex5 |  net$   | avgR   | DD%   |  DD$     | maxSingleLoss$ | worstStreak$
0.50  |   (pending — not yet computed)
0.75  |   (pending — not yet computed)
1.00  |    1754   |  478  | 27.25 |  1276  | 1.0914 | 1.0694 | 2127.04 | 0.0542 | 25.95 | -1000.56 |     -71.86     |   -364.36
1.25  |    1420   |  149  | 10.49 |  1271  | 1.1182 | 1.0929 | 3590.08 | 0.0648 | 32.40 | -1314.53 |     -82.83     |   -482.07
1.50  |    1343   |   66  |  4.91 |  1277  | 1.0924 | 1.0680 | 3376.40 | 0.0610 | 40.26 | -1633.77 |    -143.72     |   -579.51
2.00  |   (pending — not yet computed)
```
Rows 1.0/1.25/1.5 refresh the doubly-stale comm-$7 table (all PFs up modestly vs their comm-$7 twins; skip%
at risk=1.0 is now 27.25% vs the comm-$7 refresh's 31.6% — the spread floor changes which bars pass the
RiskVoice spread veto). Raising risk% still monotonically cuts skip% at escalating DD (1.0%→25.95%,
1.5%→40.26%). The **3 low-risk rows (0.5, 0.75, 2.0) are NOT YET COMPUTED** — the background task was reaped
by the environment ~3× under concurrent CPU contention before finishing; each remaining cell is ~1000–2000 s.
They are PENDING, not failed. Riskgrid is secondary context here (its decision-relevant anchor, risk=1.0, is
done and is the fidelity twin); the fallback tables — the deliverable that confirms the LIVE cap — are COMPLETE.

### VERDICT — LIVE `cap=1.5` STILL JUSTIFIED under honest ($0) cost. Config UNCHANGED, no user action needed.
The picture does NOT change adversely. cap=1.5 is the aggregate net peak ($3061) AND aggregate PF peak
(1.1294), has the strongest rescued-subset PF (1.5384 ≫ 1.13 baseline), and the marginal-trade analysis shows
trades beyond 1.5 turn net-negative — so cap=1.5 is the plateau edge, exactly as the comm-$7 tables found. The
mandate's expected DIRECTION is confirmed: aggregate net/PF are better than the comm-$7 twins.

Honest nuance (task item #4/#5, reported not buried): the mandate's stated MECHANISM — "rescued trades no
longer pay a phantom $7/lot commission dragging their P&L" — is technically near-zero for THIS subset, because
rescued trades are all 0.01-lot, so their commission was only $7 × 0.01 = **$0.07/round-trip** (~$3.6 total
across 51 trades). The real driver of the changed subset numbers is the SPREAD-FLOOR data fix reshaping the
trade set: it CUT the rescued count 63→51 (fewer wide-stop signals pass the spread veto in the high-ATR
2025–26 regime) and consequently subset net fell $1089→$828 and subset PF eased 1.60→1.54. So the subset's
absolute edge is slightly SMALLER than the phantom-comm table showed — but it remains clearly genuine (PF 1.54)
and cap=1.5 remains unambiguously the best cap. This is a refinement of the numbers, NOT a reversal: nothing
here contradicts the live config or warrants changing `min_lot_risk_cap_pct` off 1.5.

CAVEATS (carried over, still apply): (1) circuit breakers NOT modeled — the 25.95% aggregate DD would have
tripped the 8% halt live. (2) Fallback raises single-trade risk above the 1.0% plan BY DESIGN (maxSingleLoss
−$71→−$76 at cap=1.5, −$108 at cap=2.0); the cap bounds it, doesn't eliminate it. (3) Aggregate net overstates
the fallback's own edge (reshuffle × `max_positions_per_symbol=1`); Table 3's subset P&L is the clean signal.
(4) Rescued trades are regime-concentrated (2025–26 only), small sample (39–66).

Config UNCHANGED (`config/base.yaml`, `risk/sizing.py`, `backtest/engine.py`, `src/`, `tests/` all untouched —
this was read-only). Auditor gates untouched (rule 8). Test budget UNSPENT/NA. pytest was NOT re-run (nothing
under `src/`/`tests/` was touched, so the suite is unaffected — last recorded 1059 passed stands; deliberately
not re-run to avoid adding CPU load to an already-contended measurement). Harness reused:
`experiments/sizing_smallacct_harness.py`; durable results: `scratchpad/sizing_comm0_results.jsonl` (7 cells).

---

## EXP-011 2026-07-22 — M30 / M15 as the PRIMARY decision timeframe with an INDEPENDENTLY-TUNED param set (NEW family)

Status: **REJECT — no independently-tuned lower-TF-primary config clears the per-year robustness bar; H1
stays the sole primary timeframe.** Analysis-only: `config/base.yaml`, `council/`, `backtest/engine.py`,
`feed/`, `risk/`, `watchman/` NOT modified. New code lives only in `experiments/exp011_native_tf_harness.py`.
Test one-touch budget for this NEW family: **UNSPENT** (no candidate survived Train → Test never touched).

### 0. What this is / NEW family / why the TF-probe, EXP-005/007 and EXP-010 do NOT already answer it

User question (verbatim intent): could M30 **or** M15 run as the **PRIMARY** decision timeframe — Council
scoring, Risk Voice, Shield, CFO, Watchman all native on M30/M15 bars, NOT H1 — with an **ENTIRELY,
INDEPENDENTLY-TUNED** parameter set (not the H1-tuned `config/base.yaml` reused on faster bars) — and thereby
(a) get in/out faster than H1, (b) while staying "acceptably" profitable, as a **parallel/independent** second
strategy alongside the H1 system (user: "อิสระต่อกันกับที่เราใช้อยู่อีกชุด"), NOT necessarily beating H1.

This is a genuinely NEW family, distinct from all prior lower-TF work — stated explicitly so no reader thinks
those rejections already close it:
- **TF-probe NOTE (2026-07-22):** ran M30/M15/M5 as PRIMARY but with the **SAME H1-tuned thresholds/ATR-mults/
  pivot/time-stop** — a known mismatched comparison (fast bars, slow-TF-tuned knobs). EXP-011 removes exactly
  that mismatch by re-tuning the faster TF on its own terms. THIS is the "fresh independent tuning" follow-up
  the TF-probe did NOT run.
- **EXP-005 (M15) / EXP-007 (M30):** lower-TF structure as a FILTER/CONDITION on the SAME H1 entry (does M15/M30
  pre-entry structure discriminate H1 outcomes). Different mechanism (H1 still decides); rejected on
  cross-split sign-flips.
- **EXP-010 (H1→M30 hybrid):** H1 Council keeps ALL decisions, M30 used ONLY for entry timing/stop placement.
  Different mechanism (H1 still the brain); rejected on whipsaw/regime (F2/F5). EXP-011 instead makes the
  faster TF the BRAIN — every voice scores natively on M30/M15 bars.

### 1. Hypothesis (falsifiable, pre-registered)

**H1:** With an independently-tuned parameter set, an M30-primary (and/or M15-primary) Council system enters
and exits FASTER than H1 (lower median holding time, higher trade frequency) AND stays profitable per-year at a
bar comparable to what H1 must clear — specifically net-positive (PF ≥ 1.0) in a majority of the four
Train+Val years, with NO catastrophic (PF < 0.9 / large net-loss) year, so it is viable as an independent
parallel strategy.

**Null / falsifiers (any one ⇒ REJECT that timeframe):**
- (F1) Cost/R erosion: the 5-pt spread floor is a larger fraction of R as stops tighten on faster bars
  (TF-probe finding #3) — the faster TF may be structurally net-negative regardless of thresholds.
- (F2) No latent entry edge: the native lower-TF Council signal has PF_ex5 < 1.0 (loses even excluding top-5
  winners) and independent tuning of the highest-leverage knobs (selectivity, stop width) cannot lift it to a
  per-year-robust positive.
- (F3) Regime luck, not edge: any PF > 1.0 shows up in DIFFERENT years across TFs/configs (reshuffling), i.e.
  it is not a stable structural edge — the exact failure mode this log has caught 5+ times.
- (F4) Y1-2021-22 (choppy regime) fails: M30 covers the full 2021-22 regime (mandatory hard test); a candidate
  net-negative there is regime-fragile. (M15 CANNOT test Y1 at all — data starts 2022-04-28 — so any M15
  result is inherently LESS regime-tested, per EXP-005's coverage caveat.)
- (F5) Drawdown blowout: DD balloons far past H1's ~10–15% (TF-probe DD staircase 11%→54%), making it
  un-tradeable at the $3,000 constraint even if PF were ≥ 1.0.

### 2. Mechanism / what IS and ISN'T independently tunable (verified by reading engine + council + indicators)

`backtest.engine.run_backtest` is timeframe-agnostic: it consumes a bar DataFrame and runs Bull/Bear scoring,
Decision Matrix, order construction, sizing, (opt) Risk Voice + Watchman NATIVELY on whatever bars are fed. So
feeding the floored `XAUUSD_M30.csv` / `XAUUSD_M15.csv` runs the whole Council natively on that TF. Knobs:
- **Config-exposable (swept via `BacktestConfig`, no source edit — this is what "independent tuning" covers
  here):** `sl_buffer_atr`, `sl_min_atr`, `sl_max_atr` (ATR-stop mults — the cost/R + whipsaw lever),
  `tp_r_multiple`, `pivot_bars` (swing lookback), `bull/bear/conflict` thresholds (selectivity), Risk-Voice
  session/spread, Watchman `time_stop_hours`/`dead_trade_r_band` (hours = wall-clock, TF-agnostic). Per rule 3
  I tune the SMALLEST high-leverage subset sequentially, NOT a joint grid.
- **NOT config-exposable (module constants — would need a monkeypatch/code change, OUT of scope, FLAGGED):**
  EMA 20/50/200 trend backbone, RSI 14, MACD 12/26/9 (`council/scoring.py`), and the 480-bar (=20d H1)
  `rolling_average` (`features/indicators.py`). On M30 these span HALF the wall-clock of H1 (EMA200 = 100h not
  200h). Leaving them native = a legitimate "M30 reacts faster to the same indicator definitions" reading;
  rescaling them (e.g. EMA 40/100/400 on M30) is a deeper separate follow-up — see §7. This is the honest
  boundary of what "independent tuning" reached in this pass.

### 3. Sweep design (sequential/nested per rule 3 — NOT a joint grid) + method

Primary sweep run with **Watchman OFF + Risk Voice OFF (SL/TP-only, EXP-002 methodology)** — the cleanest
isolation of the native entry/stop edge and the fastest (Watchman ON is O(n²) on ~2–4× the bars). Rationale:
if the native lower-TF ENTRY signal has no latent edge, adding Watchman/Risk-Voice cannot manufacture one (a
prior this log has established repeatedly). Cost model ON throughout: comm $0 (IC Markets Standard, corrected),
slippage = bar's own spread, spread baked into fill; spread-floored CSVs (0 zero-spread bars verified). Equity
$10k, risk 1.0%, per-year windows Y1–Y4 (Y5=Test reserved). Sequence, highest-leverage first:
- **Stage A — Council selectivity** (the signal-quality lever): bull=bear ∈ {70(H1 default), 80}, conflict
  scaled (55→60). If raising selectivity does not rescue per-year PF, the entry has no latent edge.
- **Stage B — stop width** (the cost/R + whipsaw lever): `sl_min_atr` 0.8 → 1.6 at baseline thresholds.
- (Stages C tp/pivot + Watchman/Risk-Voice ON confirmation would run ONLY if A or B produced a per-year-robust
  positive candidate — none did, so they were correctly NOT run, mirroring EXP-010's 10b-not-triggered logic.)

Multiple-testing (rule 7): M30 family = 3 configs (thr70, thr80, sl_min1.6); M15 family = 1 baseline. Well
under 20. Since nothing cleared Train, no winner was carried to Validation-as-selection or to Test.

### 4. Acceptance criteria (pre-registered). ADOPT-CANDIDATE a lower-TF-primary config as a viable independent
parallel strategy iff ALL: (a) trades ≥ 100 in every year Y1–Y4; (b) "faster": median hold materially below
H1's ~16.5h AND/OR higher trade frequency; (c) "acceptable profit": PF ≥ 1.0 in a MAJORITY of Y1–Y4 with NO
catastrophic year (no PF < 0.9 / large net loss), AND PF_ex5 ≥ 1.0 in a majority (a real edge, not top-5-
winner-carried); (d) NOT regime-luck: the positive years are stable, not flipping across TF/config (F3);
(e) Y1-2021-22 not net-negative for M30 (F4); (f) DD not wildly above H1 (F5); (g) plateau on any winning knob
(rule 5); (h) Test touched ONCE only for a single Train+Val survivor. Else REJECT. Auditor gates NOT touched
(rule 8); spec bounds respected (sl_max ≤ 2.5, thresholds/pivot/tp all `[adjustable]`).

### 5. Baseline to compare against — H1-as-is (current live config, honest costs, from the RE-VERIFICATION entry)

H1-primary, Watchman OFF, tp 2.0, comm $0, floored data (RE-VERIFICATION P2 row tp=2.0): Y1 **1.029/+368**,
Y2 0.983/−238, Y3 **1.225/+3145**, Y4 **1.095/+1390** — POSITIVE in 3/4 years, worst year only −$238, median
hold ~16.5h. This is the "profitable in a majority of years, no catastrophic year" bar the lower TF must reach
to be an "acceptable" parallel strategy.

### 6. Results — native lower-TF-primary, per-year PF / net $ / trades / DD% / PF_ex5 / median-hold

Harness `experiments/exp011_native_tf_harness.py` (committable). Y4 = Validation; Y1–Y3 = Train.

**M30-primary (full Train+Val coverage incl. the hard 2021-22 regime):**

| config | Y1 2021-22 | Y2 2022-23 | Y3 2023-24 | Y4/Val 2024-25 | hold | yrs PF≥1.0 |
|--------|-----------|-----------|-----------|----------------|------|-----------|
| **A0 thr70 (H1 default)** | 0.812 / −3549 / 357 / DD37 / ex5 0.765 | 0.920 / −1902 / 401 / DD28 / ex5 0.879 | 0.948 / −1302 / 381 / DD34 / ex5 0.902 | 1.061 / +1705 / 421 / DD16 / ex5 1.019 | 5.5–8h | **1/4** |
| A1 thr80 (more selective) | 0.836 / −2788 / 318 / DD34 / ex5 0.783 | 0.863 / −3010 / 372 / DD38 / ex5 0.818 | 1.007 / +159 / 350 / DD22 / ex5 0.959 | 1.069 / +1895 / 389 / DD13 / ex5 1.024 | 5.8–8.5h | 2/4 |
| B1 sl_min1.6 (wider stops) | 0.773 / −4023 / 341 / DD41 / ex5 0.722 | 0.936 / −1417 / 356 / DD23 / ex5 0.892 | 0.943 / −1355 / 367 / DD33 / ex5 0.896 | 1.047 / +1209 / 398 / DD13 / ex5 1.005 | 8–10h | 1/4 |

**M15-primary (baseline thr70; CANNOT test Y1 — M15 data starts 2022-04-28):**

| config | Y3 2023-24 | Y4/Val 2024-25 | hold |
|--------|-----------|----------------|------|
| M15 thr70 | 1.055 / +2937 / 675 / DD31 / ex5 1.028 | **0.971 / −1209 / 730 / DD34 / ex5 0.947** | 3.5–3.8h |

### 7. Acceptance scoring + VERDICT — REJECT (both M30 and M15)

- **(b) "Faster": PASS (the one thing that works).** Median hold M30 5.5–8h, M15 3.5–3.8h vs H1 ~16.5h; trade
  frequency ~2× (M30) to ~3× (M15) H1's. The turnover motivation is genuinely achievable — but on a losing
  system it is worthless.
- **(c) "Acceptable profit": FAIL, decisively.** M30 baseline is net-NEGATIVE in 3/4 years (only the favorable
  2024-25 positive); **PF_ex5 < 1.0 in every Train year (0.77/0.88/0.90)** and avgR NEGATIVE in Y1/Y2/Y3 — the
  native M30 entry LOSES even excluding its top-5 winners, at ~30–33% win rate against a 2R target. H1 by
  contrast is positive in 3/4 years. This is the opposite of "acceptable."
- **(F2) Independent tuning does NOT rescue it — the crux of the whole question.** Raising selectivity
  (thr80): nudged Y1 (0.812→0.836) and Y3 (0.948→1.007 breakeven) but HURT Y2 (0.920→0.863/−$3010); still
  net-negative in 2/4 years, positive years barely >1.0, PF_ex5 still <1.0 all three Train years — pure
  regime-reshuffling, no stable lift. Widening stops (sl_min1.6): made it WORSE overall (Y1 0.812→0.773/
  −$4023, Y4 1.061→1.047) AND lengthened holds to 8–10h, eroding the very speed advantage that motivated M30.
  Both of the two highest-leverage independent knobs fail — the deficit is a structural entry-quality/cost-per-R
  gap, not a threshold-calibration artifact.
- **(F3/d) Regime-luck confirmed:** M30's only positive year is Y4; M15's only positive of {Y3,Y4} is Y3 and
  its Y4 is NEGATIVE (0.971) — the positive year FLIPS between the two timeframes. Any PF>1.0 is which-regime-
  you-landed-in, not edge. Same failure mode caught in EXP-002/004/005/006/007/010.
- **(F4/e) Y1-2021-22: FAIL for every M30 config** (best 0.836, deeply net-negative −$2,788; worst −$4,023) vs
  H1's +$368. The tighter/faster M30 gets whipsawed in chop exactly as EXP-010 found for the hybrid.
- **(F5/f) DD blowout: FAIL.** M30 DD 22–41%, M15 DD 31–34% per year — vs H1's ~10–15%. Un-tradeable at $3,000
  even before the profit problem (and circuit breakers, unmodeled here, would have halted well before these).
- **(a) Trade floor: PASS** (all ≥ 100/yr) — the one criterion faster bars clear trivially. **(g) plateau:
  moot** — no passing region exists.
- **M15 is strictly the weaker candidate:** finer bars → higher cost/R (TF-probe #3), it cannot test the hard
  Y1 regime at all (data gap), and its covered years already fail (Y4 net-negative, DD 34%). Nothing about M15
  is more promising than M30, which itself failed.

**VERDICT — REJECT. A fresh, independently-tuned M30-or-M15-PRIMARY system is NOT worth pursuing as a
parallel/independent alternative to the current H1 system.** The user's fair critique of the TF-probe (it
reused H1 thresholds) is answered directly: re-tuning the two highest-leverage knobs the faster TF genuinely
scales (selectivity + stop width) on its own terms does NOT change the conclusion — the native lower-TF entry
signal has no latent edge (PF_ex5 < 1.0, negative avgR in the hard regimes), the faster turnover is real but
sits on a structurally losing base, drawdowns are 2–3× H1's, and the occasional profitable year is regime luck
that flips between M30 and M15. This is the same monotone-staircase collapse the TF-probe saw, now confirmed to
survive independent threshold/stop tuning — so it is not a mis-tuning artifact. "Faster" is achievable; "good
enough to trade" is not.

**Honest scope caveat (what this pass did NOT tune):** the Council's indicator PERIODS (EMA 20/50/200, RSI 14,
MACD 12/26/9, 480-bar rolling avg) stayed H1-native (module constants; rescaling them needs a code change, out
of the analysis-only mandate). It is conceivable — though NOT evidenced and against a very strong prior — that
a full wall-clock rescale of the trend backbone (e.g. EMA 40/100/400 on M30) plus a from-scratch re-tune could
behave differently. But that is a much larger, code-changing, high-overfit-risk research program (effectively
designing a new strategy, not tuning the existing one), and the entry-quality deficit shown here (loses ex-top-5
in every hard year, DD 2–3× H1) argues strongly against it paying off. If the user wants to pursue it, it needs
its own spec/design track and pre-registration — not a parameter sweep.

**Test set (2025-07-21→2026-07-21) NOT touched** — no candidate cleared Train, so both the M30-primary and
M15-primary families' one-touch Test budgets remain UNSPENT/pristine (rule 2). `config/base.yaml` NOT modified
(analysis-only, rule 10). Auditor gate thresholds NOT touched (rule 8). Harness:
`experiments/exp011_native_tf_harness.py` (committable, reusable for any future native-TF sweep); raw run
outputs are session-local background-task logs.

---

## EXP-012 2026-07-22 — H1 + M30 momentum CONFIRMATION FILTER (pure-additive; NEW family "confluence filter")

Status: PRE-REGISTERED, then RUN 2026-07-22 (see RESULTS block below). Analysis-only:
`config/base.yaml`, `council/`, `backtest/engine.py`, `feed/`, `risk/`, `watchman/` NOT modified.
New code lives only in `experiments/exp012_013_confluence_harness.py` (signal_fn gate wrapper;
production pure-functions reused verbatim). Test one-touch budget for this NEW family: reserved,
NOT to be touched without explicit user approval (per this task's ground rule 5 — Train/Val only).

### 0. What this is / NEW family / why EXP-005/007/010/011 do NOT already answer it

The idea (user-approved): keep the CURRENT-LIVE H1 pipeline (Council/RiskVoice/Shield/CFO/Watchman)
COMPLETELY UNCHANGED as the decision-maker, and add a PURE ADDITIVE boolean gate on top — an H1
signal that would normally trade is TAKEN ONLY IF a second timeframe agrees in the same direction.
This can ONLY REDUCE trade count (a filter; it never adds signals). Distinct from all prior lower-TF
work and its own family (own multiple-testing budget, own unspent one-touch Test):
- **EXP-005 (M15) / EXP-007 (M30):** asked whether lower-TF pre-entry STRUCTURE *discriminates* which
  H1 signals win vs lose — a predictive-feature search. Rejected (no cross-split sign-consistent
  feature). EXP-012 does NOT claim M30 predicts H1 outcome; it is a coarse same-direction MOMENTUM
  agreement gate whose only job is to skip signals fired while M30 is moving the OTHER way (the
  choppy/conflicting condition that whipsawed EXP-010/011).
- **EXP-010 (H1→M30 hybrid) / EXP-011 (M30/M15 as PRIMARY):** both CHANGED the decision/entry/stop
  mechanics (tighter M30 stop, or M30 as the brain) → whipsawed in 2021-22 chop (F2/F5) and gave back
  the 2023-24 trend. EXP-012 changes NOTHING about entry price, stop, or decision — same H1 bar-i+1
  open fill, same H1 ATR stop, same Watchman. It ONLY drops a subset of H1 signals. So EXP-010/011's
  whipsaw failure mode (tighter stop) structurally CANNOT recur here; the open question is whether the
  agreement gate removes the *right* (losing) H1 signals in Y1/Y2 without gutting Y3/Val or trade count.

### 1. Hypothesis (falsifiable, pre-registered)

**H1:** Requiring the last CLOSED M30 bar to show momentum in the SAME direction as the H1 signal
(m30_close on the signal's side of a short M30 EMA(P)) screens out the choppy/conflicting-direction
H1 entries that lost in 2021-22 (Y1, PF 1.001) and 2022-23 (Y2, PF 0.967) — RAISING those two weak
years' PF — WITHOUT materially degrading the trend-capture years (Y3 1.199, Val 1.101) and WITHOUT
dropping any year below the 100-trade floor.

**Mechanism choice (ONE, justified — NOT a grid over mechanisms, per ground rule 7):** "last closed
M30 close vs short M30 EMA(P)". Chosen over (i) "last N M30 closes strictly monotonic" (noisy, jumps
in strictness with N) and (ii) "M30 RSI vs 50" (RSI adds its own 14-bar smoothing constant, muddying
what the single knob controls) because close-vs-EMA is smooth, has ONE clean tunable knob P (the EMA
lookback = filter strictness), and is a direct read of "is M30 moving the same way as the H1 signal
RIGHT NOW." Small P ≈ price-vs-recent-price (loose, agrees with a fresh H1 signal almost always →
removes few trades); large P ≈ price-vs-longer-average (strict → removes more). ATR/indicator periods
untouched. NO LOOKAHEAD: the M30 bar used is the last one CLOSED at/before the H1 decision instant
(H1 open+1h); an M30 bar closing exactly at the H1 close is composed only of price ≤ that instant.

**Null / falsifiers (any one ⇒ REJECT, H1-as-is stands):**
- (F1) No Y1/Y2 repair: the gate does NOT raise Y1 AND Y2 PF (it removes losers and winners roughly
  equally — the agreement condition is near-collinear with "H1 just fired", so it carries no extra info).
- (F2) Trend give-back: it materially degrades Y3 or Val (PF drop > 0.03, or turns either net-negative).
- (F3) Trade starvation: any year drops < 100 trades → INSUFFICIENT for that config; and if the viable
  configs cut frequency so hard the $3,000 small-account trade-frequency need is breached, that is a
  reported tradeoff even if edge improves (never hidden — ground rule 8).
- (F4) Regime luck / no plateau: any PF lift shows up in different years across P, i.e. no stable
  same-direction improvement and no plateau over neighboring P (rule 5).

### 2. Baseline to beat — H1-AS-IS (current live config, honest costs)

Reproduced fresh in this harness (`--filter none`), BYTE-FOR-BYTE matching EXP-010 §9 / RE-VERIFICATION
P1 BOTH-OFF (Watchman struct+time ON / be+trail OFF, RV all-24h ON, tp 2.0, pivot 3, equity $10k,
comm $0, spread-floored data):

| Year | H1-as-is PF | net $ | trades | avgR | DD% | PF_ex5 |
|------|-------------|-------|--------|------|-----|--------|
| Y1 2021-22 (Train) | 1.001 | +20  | 277 | 0.010 | 14.3 | 0.935 |
| Y2 2022-23 (Train) | 0.967 | −548 | 256 | −0.022 | 28.9 | 0.894 |
| Y3 2023-24 (Train) | 1.199 | +2935 | 234 | 0.123 | 15.2 | 1.111 |
| Y4 2024-25 (Val)   | 1.101 | +1557 | 262 | 0.068 | 10.4 | 1.024 |

### 3. Sweep (ONE knob, per rule 3) + splits

Knob = M30 EMA period P ∈ {6, 10, 14, 20, 30} M30 bars (= 3h/5h/7h/10h/15h wall-clock momentum
context). 5 configs (family multiple-testing count for EXP-012 = 5, < 20). Splits reuse the whole
log's: Train per-year Y1/Y2/Y3 (2021-07-22→2024-07-21), Validation Y4 (2024-07-21→2025-07-21). Test
(2025-07-21→2026-07-21) reserved/UNSPENT — NOT touched without explicit user approval.

### 4. Acceptance criteria (pre-registered — mirrors the log's per-year robustness bar)

Deciding metric: **per-year PF + net $ + avgR** vs H1-as-is, gated by trade floor and plateau.
Candidate P is worth-pursuing iff ALL:
- (a) Trade floor ≥ 100 in EVERY year Y1–Y4 (else INSUFFICIENT for that P);
- (b) **Fixes the target weakness:** raises PF in Y1 AND Y2 (the two years this is meant to repair),
  ideally turning Y2 net-positive, without a sign flip elsewhere;
- (c) **Preserves trend capture:** Y3 and Val stay net-positive and PF not materially worse (drop ≤ 0.03);
- (d) No sign flip: turns NO H1-positive year (Y1/Y3/Val) net-negative;
- (e) Beats H1 PF in a MAJORITY of Y1–Y4 AND avgR in a majority;
- (f) Plateau (rule 5): the winning P's ±1-grid-step neighbors within ~15% PF and same direction;
- (g) Trade-frequency cost quantified and judged against the $3,000 small-account need (reported as a
  tradeoff regardless of verdict).
Else REJECT (H1-as-is stands) or INSUFFICIENT. Test NOT touched (ground rule 5). Auditor gates NOT
touched (rule 8). Spec bounds: no new persisted param proposed (analysis-only); a real M30-confirm rule
would need a spec amendment for the new knob P before any config change (flagged, not done).

---

## EXP-013 2026-07-22 — H1 + H4 trend AGREEMENT FILTER (pure-additive; same "confluence filter" family)

Status: PRE-REGISTERED, then RUN 2026-07-22 (see RESULTS block below). Analysis-only (same scope as
EXP-012). New code: same harness `experiments/exp012_013_confluence_harness.py` (`--filter h4`).

### 0. What this is (sibling of EXP-012, slower-TF check)

Identical pure-additive gate concept as EXP-012, but the confirming timeframe is a HIGHER, slower one:
a larger-picture trend-agreement check rather than M30's faster momentum. Same "H1 pipeline unchanged,
only drop non-agreeing signals" mechanism (so EXP-010/011's tighter-stop whipsaw cannot recur).

**Timeframe choice: H4 (NOT Daily) — justified, not a grid.** H4 is 4× H1 (a genuine higher-TF trend)
yet not as trade-starving as Daily. On the ~1-year evaluation windows a Daily EMA changes direction
only a handful of times and a price-vs-Daily-EMA gate would slash the already-modest ~256-trade/yr H1
count hard — directly hostile to this project's binding $3,000 small-account trade-frequency constraint
(project_small_account_philosophy). H4 gives a "bigger picture than H1" agreement check while keeping
more of the frequency. Picking H4 over Daily is a pre-committed design call (ground rule 7), NOT a
joint H4×Daily grid.

**H4 data:** DERIVED from H1 by a byte-exact 4h OHLC resample (aligned to server hours 0/4/8/12/16/20)
— no re-download needed, and because FILLS still use the H1 bars, the cost model / spread floor is
entirely unaffected (H4 is used only to compute a trend-direction boolean). NO LOOKAHEAD: the H4 bar
used at each H1 decision is the last H4 bar CLOSED at/before the H1 decision instant (H1 open+1h).

### 1. Hypothesis / falsifiers

**H1:** Requiring the last closed H4 bar's close to be on the same side of an H4 EMA(Q) as the H1
signal direction (higher-TF trend agrees) screens out counter-higher-trend H1 entries — improving the
weak chop/transition years (Y1/Y2) while preserving Y3/Val — without breaching the 100-trade floor.
**Falsifiers:** same F1–F4 as EXP-012 (no Y1/Y2 repair; trend give-back on Y3/Val; trade starvation
below floor or unacceptable frequency cut; regime luck / no plateau over Q).

### 2. Baseline / 3. Sweep / 4. Acceptance

Baseline = the SAME H1-as-is table as EXP-012 §2. Knob = H4 EMA period Q ∈ {10, 20, 30, 50} H4 bars
(= 40h/80h/120h/200h ≈ 1.7d/3.3d/5d/8.3d trend lookback). 4 configs (EXP-013 family count = 4; the
"confluence filter" family total across EXP-012+EXP-013 = 9, < 20). Splits + acceptance criteria
(a)–(g) IDENTICAL to EXP-012 §4 (per-year robustness bar). Test reserved/UNSPENT — NOT touched without
explicit user approval. Auditor gates NOT touched (rule 8). A real H4-agreement rule would need a spec
amendment for the new knob Q before any config change (flagged, not done).

---

## EXP-012 + EXP-013 RESULTS (run 2026-07-22) — VERDICT: REJECT BOTH (H1-as-is stands)

Analysis-only: `config/base.yaml`, `council/`, `backtest/engine.py`, `feed/`, `risk/`, `watchman/` NOT
modified. Code: `experiments/exp012_013_confluence_harness.py` (signal_fn gate; production pure-functions
reused verbatim) + `scratchpad/conf_driver.py` (resumable driver). Both NEW-family Test budgets remain
UNSPENT (no candidate cleared Train+Val → Test never touched). Cost model ON throughout (comm $0 IC
Markets Standard, slippage = bar's own spread, spread-floored CSVs). Equity $10k, all-24h RV, Watchman
struct+time ON / be+trail OFF, tp 2.0, pivot 3 — the exact current-live H1 pipeline, UNCHANGED.

### §A. Fidelity (`--filter none`) — PASSED
Gate pass-through reproduces the EXP-010 §9 / RE-VERIFICATION P1 BOTH-OFF baseline to the cent:
Y1 1.001/+20/277, Y2 0.967/−548/256, Y3 1.199/+2935/234, Val 1.101/+1557/262. The gate composes the
stock council signal fn correctly; any per-year delta below is the FILTER, not a harness artifact.

### §B. Per-year grid — PF / net $ / trades (bold = beats baseline PF that year)
Baseline: **Y1 1.001/+20/277 | Y2 0.967/−548/256 | Y3 1.199/+2935/234 | Val 1.101/+1557/262**

**EXP-012 M30 momentum (close vs M30 EMA(P)):**

| P | Y1 | Y2 | Y3 | Val |
|---|----|----|----|-----|
| 6  | **1.093** / +1418 / 266 | 0.908 / −1288 / 236 | 1.191 / +2929 / 241 | 1.049 / +689 / 248 |
| 10 | **1.045** / +653 / 264  | 0.921 / −1132 / 236 | 1.192 / +2699 / 229 | 1.068 / +989 / 251 |
| 14 | **1.087** / +1277 / 263 | 0.909 / −1299 / 237 | **1.242** / +3415 / 229 | 1.060 / +872 / 252 |
| 20 | **1.048** / +703 / 264  | 0.932 / −992 / 236  | 1.192 / +2681 / 228 | **1.123** / +1798 / 248 |
| 30 | **1.055** / +821 / 265  | 0.965 / −530 / 244  | 1.131 / +1835 / 230 | **1.138** / +2011 / 247 |

**EXP-013 H4 trend (H4 close vs H4 EMA(Q); H4 = byte-exact 4h resample of H1):**

| Q | Y1 | Y2 | Y3 | Val |
|---|----|----|----|-----|
| 10 | **1.006** / +98 / 276  | 0.881 / −1814 / 247 | 1.138 / +2021 / 238 | **1.147** / +2150 / 246 |
| 20 | 0.952 / −715 / 276     | 0.916 / −1341 / 250 | 1.153 / +2224 / 233 | **1.089** / +1248 / 246 |
| 30 | 0.971 / −410 / 264     | **0.970** / −480 / 247 | 1.106 / +1575 / 234 | 1.044 / +576 / 241 |
| 50 | 0.939 / −807 / 247     | **1.031** / +445 / 233 | 1.193 / +2605 / 217 | 1.044 / +557 / 225 |

(All configs clear the ≥100-trade/year floor — criterion (a) PASS everywhere; trade counts are barely
reduced, which is itself the core finding — see §C.)

### §C. Acceptance-criteria scoring + VERDICT

**Overarching finding — both filters are NEAR-COLLINEAR with the H1 signal, so they barely filter.**
An H1 signal fires precisely because H1 momentum/trend points that way, and M30/H4 are highly correlated
with H1 on direction — so "M30/H4 agrees with the H1 signal" is true almost whenever H1 fires. Loose
settings remove almost nothing (M30: 277→263–266, ~5%; H4 Q=10: 277→276, ~1 trade). The small per-year
PF wiggles at those settings are trade-set RESHUFFLE noise (max_positions=1), not screening. This is the
mechanism reason the confluence idea does not deliver here.

**EXP-012 (M30) — REJECT.**
- (b) Repair BOTH weak years: **FAIL.** Y1 PF rises for every P (good), but Y2 is NEVER repaired — every
  P leaves Y2 PF ≤ baseline 0.967 (0.908–0.965; best P=30 merely TIES, and only by removing ~13 losers
  in a coin-flip reshuffle). The filter does not systematically screen Y2's losers.
- (c) Preserve trend years: partial fail — P=6/10/14 drag Val >0.03 below baseline (1.049/1.068/1.060);
  P=30 drags Y3 to 1.131 (−0.068). No single P holds BOTH Y3 and Val.
- (e) Beat baseline PF in a MAJORITY of Y1–Y4: **FAIL** for every P (best, P=20, wins only Y1 & Val = 2/4;
  avgR likewise 2/4). (f) Plateau: **FAIL** — as P rises, Y3 falls (1.242→1.131) while Val rises
  (1.060→1.138): an opposing-tradeoff surface, not a plateau (rule 5). Regime-reshuffle (F4), no stable edge.
- (g) Frequency: ~5% fewer trades — negligible, and buys nothing; no favorable frequency/edge tradeoff
  for the $3,000 account (you neither gain edge nor meaningfully change count).

**EXP-013 (H4) — REJECT.**
- (d) No sign flip: **FAIL, decisively.** As soon as the H4 gate is strict enough to actually remove trades
  (Q≥20), it turns the H1-POSITIVE Y1 (2021-22, +$20) NET-NEGATIVE: Q20 −$715, Q30 −$410, Q50 −$807. In the
  2021-22 CHOP the H4 "trend" is itself whipsawing, so "counter-H4-trend" H1 entries are disproportionately
  the chop-REVERSAL trades that go on to win — stripping them removes the wrong trades in exactly the regime
  the filter was meant to fix. Same whipsaw-by-regime failure mode as EXP-010/011, arriving through a
  different door (signal removal, not a tighter stop).
- (b) Repair BOTH weak years: **FAIL.** Only Q=50 repairs Y2 (0.967→1.031) — and it does so precisely BY
  flipping Y1 negative (0.939/−$807). No Q raises Y1 and Y2 together. (c)/(f): Q=10 drags Y3 (−0.061) and is
  a ~1-trade no-op; the response is a Y1↔Y2 regime swap as Q rises (no plateau, F4).
- (g) Frequency: strict Q cuts up to ~11% (Y1 277→247 at Q=50) but only by deleting profitable chop-reversal
  trades — the worst possible frequency reduction for a small account (fewer trades AND worse Y1).

**Multiple-testing (rule 7):** confluence family = 9 configs (5 M30 + 4 H4), < 20 → standard bar; moot, as
nothing passes. **Plateau (rule 5): moot** — no passing region exists for either sibling.

### §D. VERDICT — REJECT BOTH. Neither confluence filter is worth pursuing.
Neither a fast M30 momentum-agreement gate nor a slower H4 trend-agreement gate fixes the 2021-22 (Y1)
whipsaw the way the hypothesis hoped. Two robust reasons: (1) same-direction cross-TF agreement is
near-collinear with the H1 signal itself, so loose settings are no-ops (remove ~1–5% of trades, change
nothing but reshuffle noise); (2) the only setting strict enough to bite (H4 Q≥20) removes the WRONG trades
— the chop-reversal H1 entries that carry Y1 — turning a positive year net-negative (the exact whipsaw-by-
regime failure EXP-010/011 hit). No config repairs BOTH weak years (Y1 & Y2); improving one degrades the
other (regime tradeoff, not edge — the reshuffling failure mode this log has now caught 7+ times). The
trade-frequency tradeoff is unfavorable for the $3,000 small-account constraint either way: you either
remove almost nothing (no benefit) or remove profitable trades (harm). The current H1-as-is pipeline stands.

**Honest tradeoff note (ground rule 8, not buried):** the confluence idea's stated tension — "filter →
fewer trades → worse for a small account that needs frequency" — turned out to be the LESSER problem. The
real problem is the filter has no genuine edge to trade the frequency AWAY for: at the frequency cost small
enough to tolerate (~5%) it does nothing, and at the frequency cost large enough to matter it harms Y1.

**Test set (2025-07-21→2026-07-21) NOT touched** — both the EXP-012 and EXP-013 (confluence-filter family)
one-touch Test budgets remain UNSPENT/pristine (rule 2; ground rule 5). `config/base.yaml` NOT modified
(analysis-only, rule 10). Auditor gate thresholds NOT touched (rule 8). Harness:
`experiments/exp012_013_confluence_harness.py` (committable, reusable for any future cross-TF gate).

---

## EXP-014 2026-07-23 — EURUSD as a NEW independent instrument: timeframe selection (H1/M30/H4) + fresh independent tuning (NEW family, first non-XAUUSD)

Status: PRE-REGISTERED, then RUN (see RESULTS block below). Analysis-only: `config/base.yaml`,
`council/`, `backtest/engine.py`, `feed/`, `risk/`, `watchman/` NOT modified. EURUSD is COMMENTED
OUT in `config/base.yaml` symbols (Phase-9 note: forex majors lost OOS with XAUUSD thresholds) —
this experiment does NOT re-enable it; EURUSD is NOT going live from this exploration regardless of
outcome (ground rule 6). New code lives only in `experiments/exp014_eurusd_tf_harness.py` (a EURUSD
SymbolSpec twin of `exp011_native_tf_harness.py`). Test one-touch budget for this NEW family:
**reserved / NOT to be touched without explicit user approval** (ground rule 5 — Train/Val only).

### 0. What this is / NEW family / why prior EXPs do NOT answer it

User question: if EURUSD were traded by the SAME rule-based architecture (Council/RiskVoice/Shield/
CFO/Watchman), (1) which timeframe suits it best, and (2) with a FRESHLY, INDEPENDENTLY tuned param
set (NOT reusing XAUUSD's H1-tuned `config/base.yaml`) — prioritizing FASTER entry/exit than the
current live XAUUSD-H1 approach AND good profitability, as a genuinely NEW, independent parallel
trading track. This mirrors EXP-011's structure (M30/M15-vs-H1 for XAUUSD, REJECTED) but for an
ENTIRELY NEW INSTRUMENT — so it has its own multiple-testing budget and its own UNSPENT one-touch
Test allowance. Distinct from every prior EXP: EXP-001..013 are ALL XAUUSD. The only prior EURUSD
touch is the Phase-9 OOS backtest (EURUSD H1 PF 0.84, net-negative) — but that reused XAUUSD's exact
thresholds AND the wrong commission ($7 Raw-Spread, since corrected to $0 Standard) AND only a
~730-day window; it is precisely the mismatched-config artifact this experiment is designed to correct
for, exactly as the ORIGINAL XAUUSD TF-probe was corrected by EXP-011.

### 1. Data acquisition (fresh, this experiment)

The pre-existing `data/historical/EURUSD_H1.csv` covered ONLY 2024-07-22 → 2026-07-21 (~730 days,
the config default) — i.e. only Y4/Y5, NO Train years. Re-downloaded fresh via chunked
`copy_rates_range` (90-day backward windows to beat MT5's per-request depth cap) for H1, M30
(mt5.TIMEFRAME_M30 directly — M30 is absent from feed/poller.TIMEFRAME_MAP, so unsupported by the
stock CLI, same as EXP-005 noted), and H4. Coverage now 2021-07-01 → 2026-07-22 for all three, matching
the log's per-year splits. Spread-zero floor (EURUSD=10 points, per SPREAD_ZERO_FLOOR_POINTS) applied on
download; **0 zero-spread bars verified** in all three files (H1 31,492 bars / M30 62,978 / H4 7,876).
EURUSD SymbolSpec pulled LIVE from MT5 (IC Markets Standard demo): digits=5, point=1e-05, tick_size=1e-05,
tick_value=1.0, contract_size=100,000 -> **point_value = 100,000 $/price-unit/lot (XAUUSD's is 100)** —
hardcoded correctly in the harness, NOT inherited from Gold. Data files gitignored (`data/historical/*`).

### 2. Mechanism / what IS and ISN'T independently tunable (identical boundary to EXP-011 §2)

`backtest.engine.run_backtest` is timeframe- AND symbol-agnostic (consumes a bar DataFrame + a
SymbolSpec). Config-exposable knobs swept via `BacktestConfig` (this is what "independent tuning"
covers): `sl_buffer/min/max_atr`, `tp_r_multiple`, `pivot_bars`, `bull/bear/conflict` thresholds,
Risk-Voice session/spread, Watchman `time_stop_hours`/`dead_trade_r_band`. NOT config-exposable (module
constants — would need a code change, OUT of scope, FLAGGED): EMA 20/50/200 backbone, RSI 14, MACD
12/26/9, 480-bar rolling avg. Same honest boundary as EXP-011: the Council's indicator PERIODS stay
native; on M30 they span half the wall-clock of H1. Rescaling them = a separate, code-changing,
new-strategy-design track, not a parameter sweep.

### 3. Method / sweep design (sequential, NOT joint — rule 3)

Primary sweep = **Watchman OFF + Risk Voice OFF (SL/TP-only, EXP-002/011 methodology)** — the cleanest
isolation of the native entry/stop edge; if the native EURUSD entry signal has no latent edge, adding
Watchman/RV cannot manufacture one (a prior this log has established repeatedly). Cost model ON: comm $0
(IC Markets Standard), slippage = bar's own spread (min-1-spread), spread baked into fill; spread-floored
CSVs. Equity $10k, risk 1.0% (matches EXP-011; sizing-confound-free — the $3,000 small-account
constraint is checked SEPARATELY via trade frequency, not by shrinking equity). Per-year Y1–Y4 (Y5=Test
reserved). Two stages, highest-leverage first, and ONLY on whichever TF(s) the first pass shows latent
signal:
- **Step 1 — timeframe probe (first pass):** run H1, M30, H4 with the CURRENT XAUUSD-tuned config
  (thr 70/70/55, sl 0.2/0.8/2.5, tp 2.0, pivot 3). Reasoning for reusing XAUUSD's thresholds here (my
  call, stated per ground rule 1): a cheap mismatch diagnostic BEFORE spending tuning effort — it reveals
  (a) whether any TF has latent signal at all and (b) how badly Gold's thresholds mismatch a forex major,
  exactly as the original XAUUSD TF-probe did before EXP-011 re-tuned. Fresh tuning follows only for a TF
  that shows promise.
- **Step 2 — fresh independent tuning** (the two highest-leverage knobs EXP-011 used, sequential):
  Stage A Council selectivity (bull=bear 70->80, conflict scaled), Stage B stop width (sl_min 0.8->1.6),
  on the most-promising TF(s). Stage C (tp/pivot) + Watchman/RV-ON confirmation run ONLY if A or B yield a
  per-year-robust positive (mirrors EXP-010/011's "10b-not-triggered" gating).

Multiple-testing (rule 7): NEW EURUSD family. Step-1 probe = 3 configs; Step-2 tuning ~2–4 per promising
TF. Target family size < 20; exact count tallied in the results block. No winner carried to Val-as-selection
or Test unless it clears Train.

### 4. Hypothesis + falsifiers (pre-registered, mirrors EXP-011)

**H1:** With an independently-tuned param set, some EURUSD timeframe (candidate: M30 or H1) enters/exits
FASTER than XAUUSD-H1 (median hold materially below ~16.5h and/or higher trade frequency) AND stays
profitable per-year at a bar comparable to what the live H1 system clears — net-positive (PF ≥ 1.0) in a
MAJORITY of Y1–Y4 with NO catastrophic (PF < 0.9 / large net-loss) year — so it is viable as a NEW
independent parallel track.

**Null / falsifiers (any one ⇒ REJECT that timeframe):**
- (F1) Cost/R erosion: EURUSD's spread floor (10 pts) is a larger fraction of R as stops tighten on
  faster bars — a TF may be structurally net-negative regardless of thresholds.
- (F2) No latent entry edge: the native EURUSD Council signal has PF_ex5 < 1.0 (loses even excluding
  top-5 winners) and tuning the two highest-leverage knobs cannot lift it to per-year-robust positive.
- (F3) Regime luck, not edge: any PF > 1.0 shows up in DIFFERENT years across TFs/configs (reshuffling)
  — the failure mode this log has caught 7+ times (EXP-002/004/005/006/007/010/011/012/013).
- (F4) Y1-2021-22 (choppy regime) fails: a candidate net-negative in Y1 is regime-fragile (the whipsaw
  year that broke the XAUUSD hybrid/primary attempts).
- (F5) Drawdown blowout: DD far past H1's ~10–15%, un-tradeable at the $3,000 constraint even if PF ≥ 1.0.

### 5. Acceptance criteria (pre-registered)

ADOPT-CANDIDATE a EURUSD-TF config as a viable independent parallel track iff ALL: (a) trades ≥ 100 in
every year Y1–Y4; (b) "faster": median hold materially below XAUUSD-H1's ~16.5h AND/OR higher trade
frequency; (c) "acceptable profit": PF ≥ 1.0 in a MAJORITY of Y1–Y4 with NO catastrophic year (no PF <
0.9 / large net loss), AND PF_ex5 ≥ 1.0 in a majority (real edge, not top-5-carried); (d) NOT regime-luck
(positive years stable, not flipping across TF/config — F3); (e) Y1 not net-negative (F4); (f) DD not
wildly above H1's ~10–15% (F5); (g) plateau on any winning knob (rule 5); (h) Test touched ONCE only, for
a single Train+Val survivor, and only with explicit user approval (ground rule 5). Else REJECT or
INSUFFICIENT. Auditor gates NOT touched (rule 8); spec bounds respected (sl_max ≤ 2.5; thresholds/pivot/tp
all `[adjustable]`). EURUSD stays commented-out in config regardless (ground rule 6).

### 6. Baseline for the "faster + good profit" comparison (structure only, not a direct profit contest)

Current live XAUUSD-H1 (the standard EURUSD must reach the same ACCEPTANCE STRUCTURE against, not beat on
raw $): positive in 3/4 Train+Val years, worst year small, median hold ~16.5h, DD ~10–15%. EURUSD is a
different instrument/edge, so this is a viability bar (per-year PF ≥ 1.0 majority, no catastrophic year,
tradeable DD, faster turnover), NOT a head-to-head $ comparison.

### 7. RESULTS (run 2026-07-23) — VERDICT: REJECT (EURUSD not viable at any tested timeframe)

Harness `experiments/exp014_eurusd_tf_harness.py`; driver `scratchpad/probe_driver.py` (resumable JSONL).
Watchman OFF + Risk Voice OFF (SL/TP-only), cost model ON (comm $0, slippage = bar's own spread,
spread-floored EURUSD CSVs), equity $10k, risk 1.0%, per-year Y1–Y4. Test (Y5) NOT touched — this NEW
family's one-touch budget remains UNSPENT/pristine (ground rule 5, rule 2). Family multiple-testing count =
**7 configs** (3-TF probe + 4 tuning), < 20.

#### 7.1 Step-1 timeframe probe — current XAUUSD config (thr 70/70/55, sl 0.2/0.8/2.5, tp 2.0, piv 3)
PF / net $ / trades / DD% / pf_ex5 / median-hold; bold = PF ≥ 1.0 that year.

| TF  | Y1 2021-22 | Y2 2022-23 | Y3 2023-24 | Y4/Val 2024-25 | hold | yrs PF≥1.0 |
|-----|-----------|-----------|-----------|----------------|------|-----------|
| H1  | 0.819 / −2573 / 236 / DD36 / ex5 0.751 | 0.942 / −804 / 218 / DD19 / ex5 0.872 | 0.773 / −3077 / 202 / DD38 / ex5 0.699 | 0.786 / −2681 / 198 / DD34 / ex5 0.711 | 9–15h | **0/4** |
| M30 | 0.623 / −6057 / 340 / DD61 / ex5 0.571 | 0.776 / −3989 / 324 / DD46 / ex5 0.727 | 0.605 / −6422 / 346 / DD66 / ex5 0.554 | 0.751 / −4871 / 383 / DD52 / ex5 0.711 | 5.5–8h | **0/4** |
| H4  | 0.653 / −1198 / 51 / DD14 / ex5 0.390 | 0.842 / −591 / 59 / DD10 / ex5 0.596 | 0.962 / −155 / 62 / DD12 / ex5 0.723 | **1.042** / +147 / 54 / DD8 / ex5 0.759 | 36–100h | 1/4 |

The XAUUSD thresholds are badly mismatched on EURUSD — WORSE than the original XAUUSD TF-probe's mismatch
(where XAUUSD-H1 was ~PF 1.0). Every EURUSD-H1/M30 year is net-NEGATIVE; M30 (the user's "faster" target)
is CATASTROPHIC (PF 0.60–0.78, DD 46–66% — a full F5 blowout). H4 has the least-bad PF and the only
tradeable DD (8–14%), BUT clears only ~51–62 trades/year (FAILS the ≥100 floor, criterion (a)) and its
median hold 36–100h is SLOWER than XAUUSD-H1 (16.5h) — it fails "faster" (criterion (b)) structurally.

#### 7.2 Step-2 fresh independent tuning — the two highest-leverage knobs on the faster/tradeable TFs (H1, M30)
Sequential (rule 3), one knob off the base each: Stage A selectivity (thr 80/80/60), Stage B stop width
(sl_min 0.8→1.6). (H4 not tuned: more selectivity worsens its already-sub-100 count and it is structurally
slower than H1 — neither the trade-floor nor the "faster" failure is a tunable-threshold artifact.)

| config | Y1 | Y2 | Y3 | Y4/Val | hold | yrs PF≥1.0 |
|--------|----|----|----|--------|------|-----------|
| H1 thr80  | 0.818 / −1726 / 145 / DD27 / ex5 0.712 | 0.948 / −507 / 157 / DD18 / ex5 0.853 | 0.795 / −1966 / 145 / DD28 / ex5 0.697 | 0.914 / −827 / 143 / DD19 / ex5 0.811 | 14–16h | **0/4** |
| H1 sl_min1.6 | 0.825 / −2169 / 203 / DD37 / ex5 0.744 | 0.946 / −727 / 209 / DD19 / ex5 0.872 | 0.749 / −3084 / 186 / DD38 / ex5 0.669 | 0.833 / −2075 / 188 / DD28 / ex5 0.756 | 12–16h | **0/4** |
| M30 thr80 | 0.674 / −4674 / 243 / DD48 / ex5 0.608 | 0.736 / −3452 / 228 / DD40 / ex5 0.671 | 0.759 / −3301 / 219 / DD37 / ex5 0.693 | 0.625 / −5415 / 271 / DD58 / ex5 0.567 | 7–10h | **0/4** |
| M30 sl_min1.6 | 0.629 / −5673 / 315 / DD59 / ex5 0.573 | 0.783 / −3799 / 308 / DD43 / ex5 0.732 | 0.640 / −5817 / 319 / DD60 / ex5 0.587 | 0.808 / −3789 / 348 / DD40 / ex5 0.766 | 7.5–10.5h | **0/4** |

#### 7.3 Acceptance-criteria scoring + falsifiers

- **(F2) NO latent entry edge — the decisive, universal finding.** `pf_ex5 < 1.0` in EVERY config, EVERY
  year, EVERY timeframe (H1 0.67–0.87, M30 0.55–0.77, H4 0.39–0.76). The native EURUSD Council entry LOSES
  even excluding its top-5 winners, at a ~28–35% win rate against a 2R target (break-even needs ~33%+). This
  is not a threshold-calibration miss — it is the absence of a structural edge.
- **(c) "Acceptable profit": FAIL, decisively.** H1 and M30 are 0/4 years PF ≥ 1.0 — net-NEGATIVE in ALL
  four years for every config. H4 reaches PF ≥ 1.0 in only 1/4 (Y4, +$147) and fails the trade floor. No
  EURUSD config is net-positive in a MAJORITY of years. XAUUSD-H1 by contrast is positive in 3/4.
- **(F2/independent tuning does NOT rescue it — the crux of the whole question, answered as in EXP-011.)**
  Raising selectivity (thr80) on H1 only shrank the losses (Y4 0.786→0.914) and LENGTHENED holds to 16h
  (killing the "faster" advantage) while staying 0/4 net-negative; on M30 it reshuffled the worst year (Y1
  0.623→0.674 but Y4 0.751→0.625) — no stable lift, still 0/4. Widening stops (sl_min1.6) was neutral-to-worse
  on both and did NOT tame the DD. Both highest-leverage independent knobs fail: the deficit is a structural
  entry-quality/cost-per-R gap, not a mis-tuning artifact.
- **(F1) Cost/R erosion CONTRIBUTES.** EURUSD's 10-pt spread floor → ~2-pip modeled round-trip cost is a
  large fraction of a tight forex ATR stop (H1 ATR ≈ 10–15 pips; the 0.8·ATR min stop ≈ 8–12 pips), a heavier
  drag than on XAUUSD — consistent with the depressed ~30% win rate. It compounds on faster bars (M30 worst).
- **(F3) Regime luck, not edge.** The lone positive cell (H4 Y4) does not reproduce on any other TF/config;
  M30_thr80's best year flips from Y4 (base) to Y3 — pure reshuffle. Same failure mode caught 7+ times in
  this log (EXP-002/004/005/006/007/010/011/012/013).
- **(F4) Y1-2021-22 chop: FAIL every config** (H1 ≈0.82 / −$1.7–2.6k; M30 0.62–0.67 / −$4.7–6.1k). The
  whipsaw regime that broke every XAUUSD lower-TF/hybrid attempt breaks EURUSD even harder.
- **(F5) DD blowout: FAIL** for M30 (36–66%) and marginal-to-fail for H1 (18–38%) vs H1's ~10–15% target —
  un-tradeable at the $3,000 constraint (`project_small_account_philosophy`); circuit breakers (unmodeled
  here) would have halted these long before year-end.
- **(a) Trade floor:** H1/M30 pass on count; **H4 FAILS (< 100/yr)**. **(b) "Faster":** achievable only on the
  LOSING TFs (H1 base 9–15h, M30 5.5–8h); the only PF-respectable TF (H4) is SLOWER than XAUUSD-H1 — the
  speed goal and any hint of profit point in opposite directions. **(d/g) regime-stability / plateau: moot** —
  no passing region exists.

**Multiple-testing (rule 7):** EURUSD family = 7 configs (< 20). Moot — nothing passes; there is no
favorable result to over-credit.

#### 7.4 VERDICT — REJECT. EURUSD is NOT viable as a new independent trading track at any tested timeframe.

The core question — "could a freshly, independently tuned EURUSD system be faster in/out than XAUUSD-H1 AND
good on profit?" — answers cleanly NO. The failure is deeper than EXP-011's XAUUSD-M30/M15 rejection: XAUUSD
at least has a genuine H1 edge (positive 3/4 years); EURUSD has NO latent edge on ANY timeframe with this
Council rule set (pf_ex5 < 1.0 universally, net-negative in every H1/M30 year, ~30% win rate against 2R).
"Faster" is trivially achievable (M30 5.5–8h, H1 9–15h vs 16.5h) but sits on a structurally losing base with
2–4× the drawdown; the only timeframe with a respectable profit factor (H4) is SLOWER than XAUUSD-H1 AND too
sparse to clear the trade floor. Independent tuning of the two highest-leverage knobs (selectivity, stop
width) does not change the conclusion — confirming the deficit is structural, not a mis-tuning artifact.
This directly corroborates the Phase-9 finding (EURUSD/GBPUSD/USDJPY lost OOS with the Council logic) and
shows it survives fresh per-timeframe tuning and the correct $0 commission — the Appendix-A thresholds/rules
do not generalize to a forex major; they encode an edge specific to Gold's volatility/trend character.

**Honest scope caveat (what this pass did NOT tune, same boundary as EXP-011):** the Council's indicator
PERIODS (EMA 20/50/200, RSI 14, MACD 12/26/9, 480-bar rolling avg) stayed native (module constants;
rescaling needs a code change, out of the analysis-only mandate). It is conceivable — though NOT evidenced
and against a very strong prior (pf_ex5 < 1.0 in every single cell) — that a full indicator-period rescale
plus a from-scratch redesign could behave differently. But that is effectively designing a NEW strategy for
a mean-reverting/range-bound instrument, not tuning the existing Gold-tuned one; it needs its own spec/design
track and pre-registration, not a parameter sweep. Recommendation: do NOT pursue EURUSD as a parallel track
under the current Council rule set. If FX is desired, it is a strategy-design project, not a tuning project.

**Test set (2025-07-21→2026-07-21) NOT touched** — no candidate cleared Train, so the EURUSD family's
one-touch Test budget remains UNSPENT/pristine (rule 2; ground rule 5). `config/base.yaml` NOT modified;
EURUSD stays commented-out in symbols (ground rule 6). Auditor gate thresholds NOT touched (rule 8).
Harness `experiments/exp014_eurusd_tf_harness.py` (committable; EURUSD SymbolSpec twin of exp011's,
reusable for any future FX native-TF sweep).

---

## NOTE (not an EXP) 2026-07-23 — Shield's duplicate-signal cooldown wired into the backtest engine

NOT a predictive-edge search — a code-level parity fix, following through on the ONE actionable item
the 2026-07-22 Shield NOTE above left open ("RECOMMENDED as the one worthwhile parity fix ... user's
call"). No Train/Val/Test split, no config values changed, Test budget UNSPENT/untouched.

**What changed:** `backtest/engine.py` now instantiates the real `shield/checkpoint.Shield` (never
reimplemented) and consults it right after `signal_fn` returns a plan, gated by an optional
`BacktestConfig.shield_cfg: ShieldConfig | None` (default `None`, same explicit-placeholder convention
as `risk_voice_cfg`/`watchman_cfg`). The swing index rule 6 keys its cooldown state on is re-derived at
SIGNAL time (`_swing_index_at`, mirroring `_build_watchman_metadata`'s fill-time re-derivation and
`orchestrator/shadow_loop.py`'s live ordering) — a signal Shield blocks never becomes a `_PendingOrder`;
`shield.record_trade_opened()` fires only once a shield-approved signal actually FILLS, not merely when
approved, matching live's "only after the broker confirms placement" semantics exactly. Defensively
(same "should not normally happen" fallback `_build_watchman_metadata` already uses), a signal with no
re-derivable confirmed swing at signal time skips the Shield check rather than crashing.

**Scope, as the prior NOTE specified:** only rule 6 (cooldown) has real effect in this single-position
engine. The other 5 rules ARE exercised too (the real `Shield.check()` is called, never a hand-rolled
subset) but are structurally inert here: `min_rr` always passes because `tp_r_multiple` fixes R:R at
exactly 2.0, and `open_positions` passed to `check()` is always `[]` (this engine only ever holds one
position, so correlation/max-positions/risk-ceiling can never evaluate against a second one) — the
correlation/multi-position architecture gap stays DEFERRED until multi-symbol, exactly as scoped before.

**Wiring completed end-to-end, matching the existing Risk Voice/Watchman precedent exactly** (not scope
creep — completing an established 3-file pattern): `scripts/run_backtest.py`'s CLI now always builds a
real `ShieldConfig` from `config/base.yaml`'s `shield:` block (no opt-out, same as `risk_voice_cfg`/
`watchman_cfg`) and records `shield_modeled` in the report envelope;
`auditor/backtest_results.BacktestReportEnvelope` gained the `shield_modeled` field;
`auditor/promotion.evaluate_backtest_to_paper_gate` (Gate 1) gained a `shield_modeled` hard-fail
criterion alongside the existing `risk_voice_modeled`/`watchman_exits_modeled` ones — a backtest run
that didn't model Shield's cooldown now can't silently feed a Backtest→Paper promotion decision, same
"don't count an incomplete simulation" philosophy as the other two.

**Verification:** 15 new/updated tests (`tests/unit/backtest/test_engine.py`: cooldown blocks a same-
swing signal within the window / approved once elapsed / `shield_cfg=None` never gates / defensive no-
swing-at-signal-time doesn't crash; `tests/unit/test_run_backtest.py`: `ShieldConfig` field-by-field
mapping from config, envelope `shield_modeled` flag; `tests/unit/auditor/test_backtest_results.py` +
`test_promotion.py`: schema/gate coverage). Full suite: 1141 passed, 0 failed. No re-run of any
promotion-gate-feeding backtest was needed or performed — this is a modeling-completeness fix, not a
strategy change, and does not alter what any PAST config-driven run measured.

`config/base.yaml` UNCHANGED (no threshold touched). Auditor gate THRESHOLDS untouched (only a new
hard-fail CRITERION was added, rule 8's "don't touch gate thresholds" is about the numeric bars, not
about adding a completeness check already precedented twice). Files touched: `shield/checkpoint.py`
(new `ShieldConfig`), `backtest/engine.py`, `scripts/run_backtest.py`, `auditor/backtest_results.py`,
`auditor/promotion.py`, plus the test files listed above.

---

## NOTE (not an EXP) 2026-07-23 — MEASURED impact of the now-wired Shield cooldown on the CURRENT adopted config (does it overturn any adopted decision?)

NOT a predictive-edge search — a modeling-parity DIAGNOSTIC that turns yesterday's ~8% UPPER-bound
ESTIMATE (the 2026-07-22 Shield NOTE above) into a REAL measured before/after now that rule 6 is
actually wired (2026-07-23 NOTE directly above). No Train/Val/Test pre-registration, no config touched,
Test one-touch budget NOT spent/consumed (this is the same "informational NOTE" convention as the two
Shield NOTEs above and the TF-probe / sizing NOTEs). Read-only: `config/base.yaml`, `shield/`,
`backtest/engine.py` UNCHANGED. Auditor gates untouched (rule 8).

### Method
Standalone script (`scratchpad/shield_impact2.py`, NOT a change to `scripts/run_backtest.py`) calls
`backtest.engine.run_backtest` TWICE over the FULL `data/historical/XAUUSD_H1.csv` (29,543 bars,
2021-07-22→2026-07-21): once with a real `ShieldConfig` from `config/base.yaml`'s `shield:` block (the
new default), once with `shield_cfg=None` (the OLD behaviour every prior EXP/NOTE measured under) —
everything else IDENTICAL and equal to the adopted live config: pivot_bars=3, tp=2.0, all-24h Risk
Voice, be/trail OFF (EXP-008), risk 1.0% / min_lot_cap 1.5, commission 0.0 (IC Standard), $10k equity,
Risk Voice + Watchman both modeled. MT5 session hung on connect, so the run used the real IC Markets
XAUUSD spec directly (tick_value 1.0 @ tick_size 0.01, vmin/step 0.01) — this only scales absolute $;
PF/DD%/trade-count are ratios/counts, and BOTH runs use the identical spec, so the ON-vs-OFF DELTA (the
whole point here) is exact regardless. Per-window metrics sliced from the two full runs by entry_time.

### Result — full history (2021-07-22→2026-07-21)
| mode | trades | winrate | PF | net $ | DD% | avgR | PF-excl-top5 |
|---|---|---|---|---|---|---|---|
| shield OFF (old, every prior EXP) | 1277 | 0.387 | 1.104 | 8,864.71 | 28.92 | 0.059 | 1.082 |
| shield ON  (new default)          | 1246 | 0.389 | 1.112 | 9,870.71 | 29.66 | 0.067 | 1.090 |

NET trade delta = **−31 (−2.4%)**, NOT −8%. 104 specific OFF-run entries (8.1%) are absent from the ON
run — that 8.1% matches yesterday's 8.3% blocked-pairs upper bound almost exactly — but because a
cooldown-blocked re-entry leaves the single-position engine FLAT and thus eligible for later signals it
would otherwise have sat through, ~73 substitute entries re-enter the count, so the NET reduction is
only 2.4% (yesterday's NOTE predicted the true rate is "lower once the new-swing-bypass is accounted
for" — confirmed, and the slot-refill dynamic makes the NET effect on trade COUNT smaller still).

### Result — per window (trades / PF / net$, ON vs OFF)
| window | trades ON/OFF | PF ON/OFF | net$ ON/OFF |
|---|---|---|---|
| Train 2021-07-22→2024-07-21 | 755 / 773 | 1.083 / 1.059 | 3929.9 / 2754.6 |
| Val   2024-07-21→2025-07-21 | 251 / 260 | 1.049 / 1.080 |  986.0 / 1512.1 |
| Test  2025-07-21→2026-07-21 | 238 / 242 | 1.242 / 1.240 | 4959.6 / 4602.8 |

All windows stay well above the 100-trade floor (rule 6) both ways — no sample concern. |ΔPF| ≤ 0.031
on any window, and the SIGN is non-systematic: shield HELPS Train (+0.024) and full-history PF (+0.008),
slightly HURTS Val (−0.031), is NEUTRAL on Test (+0.002). That is noise-level, not a directional bias.

### Does this overturn any adopted decision? — NO.
- **EXP-008 (be/trail disabled)** was decided by a Test-window PF gap of ~0.05–0.11 between two configs
  BOTH measured shield-unmodeled. Shield perturbs the Test window by +0.002 PF (1.240→1.242) — an order
  of magnitude smaller than that gap — and shifts BOTH compared configs the same tiny direction, so the
  DELTA that drove the verdict is essentially invariant. Its headline numbers (measured shield-off)
  would move by ≤ noise; the conclusion (disabling be/trail beats enabling) is unaffected.
- **Session gate EXP-003, tp=2.0 EXP-002/009, pivot=3 EXP-009** — each decided by margins far larger
  than shield's ≤0.03 PF / ≤2.4%-trade wobble (EXP-003 alone flipped a year +$297→−$518; EXP-009's
  pivot=4 was rejected on a clear multi-method agreement). None is within reach of this perturbation.
- **Direction of bias is reassuring, not alarming:** on the full history modeling Shield RAISES PF
  (1.104→1.112) and expectancy (avgR 0.059→0.067, net +$1,006) — the intended anti-whipsaw effect of
  dropping some fast-stop same-swing re-entries. So the prior shield-UNMODELED runs were, in aggregate,
  if anything marginally PESSIMISTIC on PF, not optimistic — no adopted decision was FLATTERED by the
  omission (the failure mode that would demand a retest). PF-excl-top5 stays >1.0 both ways (1.082→1.090).

### Recommendation
**NO retest / re-verification of any adopted decision is warranted.** The real measured effect (net
−2.4% trades, |ΔPF| ≤ 0.03 per window, non-systematic in sign, aggregate PF/expectancy slightly
IMPROVED) is comfortably inside the noise the adopted decision margins already clear, and lands where
yesterday's ~8% upper bound predicted once new-swing-bypass + single-position slot-refill are accounted
for. Adopted config stands as-is; `config/base.yaml` UNCHANGED. Going FORWARD, new promotion-gate-feeding
backtests SHOULD (and now do by default) model Shield — but no historical conclusion needs re-running.
Script: `scratchpad/shield_impact2.py` (diagnostic only, not committed to `scripts/`).

---

## NOTE (not an EXP) 2026-07-23 — Council Bull/Bear SCORING-FORMULA component audit (base rates + outcome discrimination; NEVER-touched surface)

NOT a predictive-edge search / no Train-Val-Test pre-registration and no Test one-touch budget spent —
this is a component-level diagnostic of the Council's OWN scoring formula (`council/scoring.py`
`score_bull_voice`/`score_bear_voice` + `decision_matrix.py`'s 70/40/55 rows), same "informational NOTE"
convention as the Shield/sizing/TF-probe NOTEs above. Grep confirmed this scoring formula (the 5 point
components, EMA/RSI/MACD periods, and the 70/55/40 thresholds) has NEVER been touched by any prior EXP —
EXP-012/013's "confluence" are a DIFFERENT thing (a cross-TF confirmation FILTER, both rejected), not this
module's internal `confluence` component. Read-only: `config/base.yaml`, `council/`, `features/` UNCHANGED.
Auditor gates untouched (rule 8). Question answered: should any scoring condition/weight be adjusted,
added, or removed?

### Method
`scratchpad/council_diag.py` + `council_diag2.py`. The four EWM-based components (trend/RSI/MACD, all
`adjust=False` EWM → causal, so full-series value at i EQUALS the module's per-slice `.iloc[-1]`) are
vectorised exactly; swings precomputed once (`detect_swings`, offline == confirmed for the market-structure
component). VALIDATED against the REAL `score_bull_voice`/`score_bear_voice` on 150 random bars: **150/150
match to the point, every component** — numbers below are the production scorer's, not an approximation.
Full XAUUSD H1 history (29,543 bars) + Train slice; trade-outcome link uses the STOCK engine (999 trades,
tp2.0, pivot3, be/trail OFF, commission 0, all-24h — the adopted live-equivalent config), scoring each
trade's SIGNAL bar (entry_idx−1, the bar Council actually scored) on its winning voice.

### 1. Component BASE RATES (full history; Train nearly identical, so stable) — bull / bear
| component (pts) | bull fire % | bear fire % | reading |
|---|---|---|---|
| trend_alignment full (30) | 42.3 | 30.5 | healthy, discriminating |
| trend_alignment partial (15) | 12.3 | 14.8 | small tier |
| momentum_rsi (20) | 46.4 | 40.8 | ~half the time |
| momentum_macd (15) | 25.7 | 26.1 | strict-expand fires ~1/4 — NOT dead, NOT always |
| market_structure (20) | 44.8 | 38.9 | fires ~2/5 |
| **confluence (15)** | **100.0** | **100.0** | **CONSTANT — fires on EVERY bar** |

**Confluence is a dead component.** Breakdown: `near_round` fires 100.0%, `near_pivot` only 18.5%. Mechanism
(and it is structural, not a data fluke): `nearest_round_number` uses a 0.50 granularity for gold, so the
distance from any price to its nearest round level is ≤ 0.25, while the gate is `≤ 0.5×ATR` and gold H1 ATR
is ~$3–15 → `0.5×ATR ≥ 1.5 ≫ 0.25` on essentially every bar. So the `+15` is a CONSTANT added to every Bull
and Bear score — it does zero discriminating work; it merely shifts the effective scale so `bull≥70` really
means "the other four components sum to ≥55 (of their 85 possible)", and `conflict 55` means "others ≥40".

### 2. OUTCOME discrimination (999 trades, overall avgR +0.084, win 36.6%)
Component present-vs-absent avg R AMONG FIRED trades (heavy selection/collinearity caveat: a trade only
fires at score≥70, so "component OFF" trades bought their 70 elsewhere — "OFF did better" can be compensation,
not proof of harm; the unconfounded reads are confluence-is-constant and the trend three-way):
- **trend three-way (cleanest):** full(30) avgR **+0.125** (win 38.0%, n637) / partial(15) **−0.022** (win
  33.1%, n302, net LOSER) / none(0) +0.192 (n60, tiny). The **partial-alignment tier earns points on trades
  that lose money** — the single most interesting lead here.
- **macd: pulls POSITIVE weight** — ON +0.119 vs OFF +0.037. The strict "expanding vs prior bar" requirement
  is NOT redundant with trend and NOT dead weight; keep it.
- **market_structure: no discrimination** — ON +0.085 ≈ OFF +0.083 (identical). Contributes 20 pts but does
  not separate winners from losers in the fired set (confounded, but flat).
- **rsi: weak/negative in-set** — ON +0.064 vs OFF +0.169 (selection-confounded; not clearly harmful).
- **confluence: no signal, and its ONE selective sub-part is NEGATIVE** — `near_pivot=True` trades avgR
  **−0.038** (win 32.5%, n123) vs `near_pivot=False` **+0.101** (n876). So even repairing the round-number
  granularity to make confluence selective is contraindicated: price-near-a-key-level correlates with WORSE
  gold trades (mean-reversion zones), not better. There is no edge to recover here.

### 3. Is the 70 threshold doing real gatekeeping? — partly, but score is a WEAK, NON-MONOTONE predictor
Fired-trade score at the signal bar: min 70, p25 70, median 80, p75 85. 25.7% of trades sit right at [70,74]
(the cliff does bind on ~a quarter), but 41% clear it to ≥85. Crucially, higher score does NOT mean better
trade — outcome by bucket: **[70,75) +0.075 · [75,85) +0.132 · [85,101) +0.051**. Hump-shaped: the
HIGHEST-conviction signals (85+, "almost everything fired", n410) are the WEAKEST, a classic
"all-aligned = late in the move" effect. Implication: **raising** bull/bear_threshold above 70 cannot improve
edge (it preferentially keeps the weakest 85+ bucket and drops the middling [70,75) one); **lowering** it only
adds still-lower-conviction volume. The threshold is essentially a TRADE-COUNT knob, not a quality knob, and
70 is a defensible compromise. This is also the project's declared highest-overfitting-risk knob (interacts
with everything) — the non-monotone curve says there is no quality gradient to exploit by moving it.

### 4. Add / remove / reweight verdict
- **REMOVE candidate — confluence:** it is inert (a literal +15 constant). But removing it is NOT a free edge:
  behaviour-neutral removal requires simultaneously dropping all three thresholds by 15 (identical trade set,
  pure cosmetic simplification, expected ΔPF = 0); removing it WITHOUT rescaling = silently raising the
  effective bar (fewer trades, and §3 shows the high-conviction survivors are the weakest slice — a net
  negative for both PF and the 200-trade gate). And repairing it to be selective is contraindicated by the
  negative `near_pivot` read (§2). So: confluence is a genuine mis-specification but a BENIGN one; no
  edge-positive action exists. Flag it, don't chase it.
- **ADD candidate:** none. No observed gap motivates a new indicator; the existing four non-constant
  components already span trend/momentum/structure, and "more agreement" (85+) is empirically worse, so
  piling on another confirmation component would push further into the weak high-conviction regime.
- **KEEP:** momentum_macd (positive discriminator), the full-trend(30) tier (best single discriminator),
  bull/bear_threshold 70 & conflict 55 (no quality gradient to move along), EMA 20/50/200, RSI 14, MACD
  12/26/9 (standard; RSI/struct are confounded-flat, not clearly mis-set).

### 5. Recommendation — NO sweep is compelling right now; ONE optional, well-motivated lead exists
(a) **Nothing here is mis-set in an edge-positive way that warrants spending Test budget.** The one
unambiguous defect (confluence-is-constant) has zero-or-negative expected payoff to "fix". thresholds are
demonstrably on a flat/non-monotone response surface — the classic "default is already fine" (rules 5/9),
and they are the declared last-priority, highest-overfit knob.
(b) **The single lead with a real mechanism** is the **trend partial-alignment (15-pt) tier**, which awards
points to a subset that is net-LOSING (−0.022R, §2). A SCOPED, pre-registerable EXP would be: one parameter —
the partial-tier weight — candidate values {15 (baseline), 0 (drop it → require full alignment for any trend
credit)}, possibly {7} as a midpoint; deciding metric avgR + PF on Validation with the standard 60/20/20
split; ADOPT only if 0 beats 15 on BOTH Train and Validation, per-year consistent (no single-regime owe),
top-5-robust, and plateau-stable. CAVEATS to pre-register honestly: it is a `scoring.py` module-constant
CODE change (not a `config/base.yaml` knob — outside a pure config-tuning mandate), the −0.022R read is
selection-confounded (must be re-measured as an actual trade-set change, not just a conditional split), and
dropping the tier reduces trades (gate-count cost). Expected payoff: modest and uncertain. Given the
project's "default is fine unless overwhelming" culture and that this is the highest-overfit family, this
lead is OPTIONAL, not urgent.
(c) **If nothing is pursued, that is the correct call.** The scoring formula is running soundly; confluence
is inert-but-harmless; the thresholds sit on a flat surface; macd/full-trend are doing real work. Clean,
evidence-backed "not worth a sweep" — same spirit as EXP-011/014 and the confluence-FILTER EXP-012/013.

`config/base.yaml`, `council/`, `features/` UNCHANGED. Test one-touch budget UNSPENT for this (new) scoring
family. Scripts: `scratchpad/council_diag.py`, `scratchpad/council_diag2.py` (diagnostic only).

---

## EXP-015 2026-07-23 — Council scoring WEIGHT-REALLOCATION: confluence's dead +15 → discriminating components (scoring-formula family)
Status: REJECTED (all 3 candidates lose to baseline on Train, per-year-consistent) — Train-only pass; Validation NOT reached, Test NOT touched.

### 0. Relation to the 2026-07-23 scoring NOTE + why this is a real EXP now
Direct follow-up to the Council scoring-formula NOTE above. That NOTE found `confluence` (+15) is a structural
CONSTANT (fires 100% of bars: `nearest_round_number`'s 0.5 gold granularity keeps price ≤0.25 from a round
level, always inside the ≤0.5×ATR gate) doing zero gatekeeping, and concluded pure removal only makes sense
paired with rescaling all 3 thresholds down by 15 (behaviourally identical, ΔPF≈0). It did NOT test
REDISTRIBUTING those 15 pts to the components that DO discriminate (macd, full-trend) — the confounded
present-vs-absent reads (macd ON +0.119/OFF +0.037; full-trend +0.125) suggested that might help. This EXP
tests exactly that, and (per the user's "don't conflate two knobs" instruction) keeps it ISOLATED from the
separate trend-partial-tier lead (§4b of the NOTE — untouched here).

### 1. Hypothesis / mechanism
Confluence's +15 is inert. If it is reallocated to a component that separates winners from losers, and the
score max is kept at 100 (so bull/bear_threshold 70 & conflict 55 keep their exact meaning), the fired trade
set should tilt toward higher-quality signals → higher PF/avgR. KEY mechanical subtlety making this a genuine
test and not a relabeling: confluence being CONSTANT means "remove 15 const + add 15 to a CONDITIONAL
component" is NOT neutral — a bar where that component FIRES is unchanged (loses 15 const, gains 15 cond ⇒
net 0), but a bar where it does NOT fire scores 15 LOWER. So each candidate effectively RAISES the effective
bar for all bars lacking that component ⇒ a different fired set. Failure mode to watch (flagged in the NOTE):
the +0.119/+0.125 reads are SELECTION-CONFOUNDED (a trade only fires at score≥70, so "component OFF" trades
bought their 70 elsewhere), so the conditional edge may not survive as an actual trade-set change.

### 2. Splits / config
Reuse the standard chronological 60/20/20 (Train 2021-07-22→2024-07-21 by year Y1/Y2/Y3; Val Y4; Test Y5).
THIS PASS = TRAIN ONLY (per-year Y1/Y2/Y3 + full-Train aggregate). Config = the diagnostic's adopted
live-equivalent: commission 0, all-24h (risk_voice=None), tp 2.0, sl 0.2/0.8/2.5, pivot 3, thresholds
70/70/55, Watchman/Shield OFF, cost model on (slippage = bar's own spread). Harness
`experiments/exp015_reweight_harness.py` (vectorised reweighted scorer, monkeypatched into the
decision_matrix namespace; STOCK engine + STOCK decision matrix + STOCK order construction otherwise).

### 3. Candidates (all keep score-max = 100; confluence dropped to 0 in every candidate)
| id | trend_full | trend_partial | rsi | macd | struct | confluence | max |
|----|-----------|---------------|-----|------|--------|-----------|-----|
| BASE (live) | 30 | 15 | 20 | 15 | 20 | 15 (const) | 100 |
| C1_macd30   | 30 | 15 | 20 | **30** | 20 | 0 | 100 |
| C2_trend45  | **45** | 15 | 20 | 15 | 20 | 0 | 100 |
| C3_split    | **38** | 15 | 20 | **22** | 20 | 0 | 100 |
Configs evaluated (this exp / cumulative for scoring family): 4 (BASE+3) / 4 — well under N>20.

### 4. Fidelity (harness validated BEFORE trusting any candidate)
- **Score exactness:** fast reweighted BASE scorer == real `score_bull_voice`/`score_bear_voice` on 300 random
  Train bars → **600/600 component-exact** (bull+bear), mismatch 0.
- **Engine integration:** fast-BASE backtest == STOCK backtest on 2022-01-01→2022-05-01, trade-for-trade
  (both 81 tr, PF 1.108, net +246.1, DD 3.40, PF_ex5 0.8918) — the vectorisation reproduces the production
  path byte-for-byte, so candidate deltas are the WEIGHT change and nothing else.
- **Independent 2nd-implementation cross-check (unplanned but decisive):** an earlier SLOW harness variant
  computed every candidate via the real per-slice path (recomputing `_confluence_score`,
  `is_higher_low`/`is_lower_high`, and all EWM indicators on `.iloc[:as_of_index+1]` each bar — a wholly
  different implementation from the fast vectorised arrays) and produced results IDENTICAL to the cent across
  ALL 16 window×weight cells below, and confirmed slow-reweighted-BASE == stock on the FULL Train window
  (587 tr / PF 1.086 / +1587.7). Two independent scorers agreeing exactly ⇒ the §5 numbers are not an
  artifact of the vectorisation.

### 5. Results — Train per-year PF / net$ (trades) + full-Train aggregate
| Year | BASE | C1_macd30 | C2_trend45 | C3_split |
|------|------|-----------|------------|----------|
| Y1 21-22 | **1.047 / +278 (197)** | 1.014 / +88 (216) | 0.991 / −45 (183) | 0.972 / −145 (182) |
| Y2 22-23 | **0.998 / −16 (202)** | 0.921 / −504 (209) | 0.951 / −300 (196) | 0.951 / −300 (196) |
| Y3 23-24 | **1.230 / +1324 (183)** | 1.134 / +737 (179) | 1.137 / +743 (178) | 1.148 / +796 (177) |
| **Full Train** | **PF 1.086 / avgR 0.052 / +1588 / DD 11.2 / 587 tr / PF_ex5 1.055** | PF 1.007 / avgR 0.007 / +133 / DD 12.5 / 605 tr | PF 1.017 / avgR 0.004 / +272 / DD 13.5 / 559 tr | PF 1.015 / avgR 0.002 / +237 / DD 13.3 / 557 tr |

### 6. Robustness / verdict evaluation
- **BASE dominates EVERY candidate in EVERY year AND on the full-Train aggregate** — PF, avgR, net$, and DD
  (each candidate also raises Train drawdown to 12.5–13.5% vs baseline's 11.2%). This is not an aggregate
  fluke: the rejection is per-year-consistent (Y1, Y2, Y3 all favour BASE), so it PASSES the same per-year bar
  that caught EXP-002's tp-2.5 regime artifact — here it caps the losers, not a winner.
- **Sign flips against the candidates:** C2/C3 turn Y1 net-NEGATIVE (−45/−145) where BASE is +278; all three
  roughly TRIPLE the Y2 loss (−300…−504 vs −16); all three roughly HALVE the Y3 profit (+737…+796 vs +1324).
- **Mechanism confirmed:** the confounded conditional reads did NOT translate into a real edge. Making 15 pts
  conditional (rather than a constant floor) raises the effective conviction bar; per the NOTE's §3 finding
  that the HIGHEST-conviction (85+) signals are the WEAKEST (hump-shaped response), tilting the fired set that
  way is edge-NEGATIVE. C1 even admits MORE trades (605 vs 587) yet earns less — lower-quality volume.
- neighborhood/plateau: n.a. (no winner to defend). per-year: ✓ (rejection consistent). top-5: candidates'
  Train PF_ex5 all drop below BASE (0.979/0.986/0.984 vs 1.055). walk-forward: n.a. (per-year IS the check).

### 7. VERDICT — REJECT all three; keep live weights (confluence stays as the benign inert +15)
No candidate is Train-positive vs baseline — every one is uniformly worse across all three Train years and the
aggregate. A candidate must clear Train before earning a Validation look; none does, so **Validation is NOT
run and the Test one-touch budget stays UNSPENT** for the scoring family. This closes the "redistribute
confluence's 15 pts" question: the reweighting is not just neutral but actively harmful, because the
component reads that motivated it were selection-confounded (exactly the caveat pre-registered in §1). The
NOTE's original recommendation stands unchanged: confluence is a genuine mis-specification but a BENIGN one —
the only edge-neutral action is remove-15-and-rescale-all-3-thresholds-down-15 (cosmetic, ΔPF≈0), NOT
redistribution.

**"Find a replacement parameter?" — NO (reaffirmed).** This pass surfaced no hole a new feature would fill; it
showed the four live non-constant components cannot be productively UP-weighted using confluence's dead points,
and the NOTE already argued a fresh confirmation component would push further into the empirically-weak
high-conviction regime (higher overfitting risk, out of pure-reweighting scope). No speculative indicator is
warranted. The separate, still-open lead is the trend PARTIAL-tier (15pt) net-loser question (NOTE §4b/§5b) —
untouched here by design; a joint test with any reweighting is now moot since no reweighting survives, so that
lead (if pursued) should be run on its own.

`config/base.yaml`, `council/scoring.py`, `decision_matrix.py`, `engine.py` UNCHANGED (monkeypatch lived only
in-process). Auditor gate thresholds NOT touched (rule 8). Harness: `experiments/exp015_reweight_harness.py`.

---

## EXP-016 2026-07-23 — Council scoring: trend_alignment PARTIAL-tier point value (scoring-formula family)
Status: REJECTED (both candidates lose to baseline on Train, per-year-consistent) — Train-only pass; Validation NOT reached, Test NOT touched.

### 0. Relation to the 2026-07-23 scoring NOTE + EXP-015 (why this is the right isolated EXP)
Follow-up to the scoring NOTE §4b/§5b — the one still-open lead after EXP-015. `trend_alignment` awards 30 pts
for FULL EMA alignment (EMA20>EMA50>EMA200 bull / reversed bear) and 15 pts for PARTIAL (EMA20>EMA50 only,
EMA200 not yet crossed). The NOTE's fired-trade three-way split found the partial(15) tier correlates with
NET-LOSING trades (avgR **−0.022**, win 33.1%, n302) vs full(30) at +0.125 — but explicitly flagged this as
SELECTION-CONFOUNDED (conditional on a trade already firing at score≥70) and said it must be re-measured as an
actual trade-set change. EXP-015 just proved that caveat is not academic: confounded conditional reads
(macd/full-trend "positive discriminators") evaporated once tested as real trade-set changes. This EXP isolates
the partial-tier weight ONLY — confluence stays the live inert +15, every other component & all 3 thresholds
held at live (NOT combined with EXP-015's already-rejected reweighting).

### 1. Hypothesis / mechanism
If the partial tier truly earns points on net-losing signals, cutting it (15→0, or →7) should raise the
effective conviction bar for partial-alignment bars (those bars drop 15/8 pts; some fall below 70/55 and stop
firing) → a genuinely NEW, smaller fired set tilted away from the losing subset → higher PF/avgR. Bars with
FULL alignment (30) or none (0) are unchanged, so this is a clean single-tier test. Pre-registered failure
mode (same as EXP-015): the −0.022R is selection-confounded — the partial-alignment bars removed may in fact
be net-positive contributors once measured as an actual trade-set delta, in which case dropping them HURTS.

### 2. Splits / config
Standard chronological 60/20/20 (Train 2021-07-22→2024-07-21 by year Y1/Y2/Y3; Val Y4 2024-07-21→2025-07-21;
Test Y5 2025-07-21→2026-07-21). THIS PASS = TRAIN ONLY (per-year + full-Train aggregate). Config = adopted
live-equivalent: commission 0, all-24h (risk_voice=None), tp 2.0, sl 0.2/0.8/2.5, pivot 3, thresholds
70/70/55, Watchman/Shield OFF, cost model on (slippage = bar's own spread). Harness
`experiments/exp016_trend_partial_harness.py` (reuses EXP-015's vectorised scorer + fidelity machinery by
import; only the WEIGHTS dict differs — trend_partial is the sole varied field, asserted in-code).

### 3. Candidates (only trend_partial changes; every other component == live)
| id | trend_full | **trend_partial** | rsi | macd | struct | confluence | max |
|----|-----------|-------------------|-----|------|--------|-----------|-----|
| BASE_p15 (live) | 30 | **15** | 20 | 15 | 20 | 15 (const) | 100 |
| P0_drop     | 30 | **0** | 20 | 15 | 20 | 15 | 100 (partial→none) |
| P7_mid      | 30 | **7** | 20 | 15 | 20 | 15 | 100 |
Configs evaluated (this exp / cumulative for scoring family): 3 (BASE+2) / 7 (4 from EXP-015 + 3 here, BASE
shared) — well under N>20.

### 4. Fidelity (harness validated BEFORE trusting any candidate)
- **Isolation guard:** in-code assert confirms all candidates differ from BASE only in `trend_partial`.
- **Score exactness:** fast BASE_p15 scorer == real `score_bull_voice`/`score_bear_voice` on 300 random Train
  bars → **600/600 component-exact** (bull+bear), mismatch 0.
- **Engine integration:** fast-BASE backtest == STOCK backtest on 2022-01-01→2022-05-01, trade-for-trade
  (both 81 tr, PF 1.108, net +246.1, DD 3.40, PF_ex5 0.8918) — byte-for-byte, so candidate deltas are the
  trend_partial change and nothing else. (Same proven machinery cross-validated by two independent scorers in
  EXP-015 §4.)

### 5. Results — Train per-year PF / net$ (trades) + full-Train aggregate
| Year | BASE_p15 (live) | P0_drop | P7_mid |
|------|-----------------|---------|--------|
| Y1 21-22 | **1.047 / +278 (197)** | 0.968 / **−181** (199) | 1.006 / +31 (195) |
| Y2 22-23 | 0.998 / −16 (202) | 0.988 / −72 (194) | **1.013 / +77 (194)** |
| Y3 23-24 | **1.230 / +1324 (183)** | 1.114 / +635 (180) | 1.157 / +848 (175) |
| **Full Train** | **PF 1.086 / avgR 0.052 / +1588 / DD 11.2 / 587 tr / PF_ex5 1.055** | PF 1.014 / avgR 0.008 / +240 / DD 13.0 / 574 tr / PF_ex5 0.984 | PF 1.047 / avgR 0.030 / +809 / DD 12.3 / 565 tr / PF_ex5 1.016 |

### 6. Robustness / verdict evaluation
- **BASE dominates the full-Train aggregate on every metric** (PF, avgR, net$, DD, PF_ex5) vs BOTH candidates.
  Cutting the partial tier RAISES Train drawdown (13.0 / 12.3 vs 11.2) — the opposite of a quality improvement.
- **P0_drop (the a-priori favourite) is clearly harmful:** it turns Y1 net-NEGATIVE (−181 vs BASE +278),
  roughly HALVES the Y3 profit (+635 vs +1324), and its Train PF_ex5 falls below 1.0 (0.984). Losing to BASE
  in all three years → not an aggregate fluke; per-year-consistent rejection (same bar that caught EXP-002 /
  EXP-015). This directly confirms the pre-registered confound: the partial-alignment bars are NET-POSITIVE
  contributors as an actual trade-set, not the net-losers the conditional −0.022R read suggested.
- **P7_mid is also rejected:** worse than BASE on the aggregate (PF 1.047 / +809 vs 1.086 / +1588) and in Y1 &
  Y3. Its ONLY per-year edge is Y2 (+77 vs −16) — the ~breakeven year — which is NOT the "per-year consistent
  across all 3 Train years" bar this protocol requires; a single marginal-year win on an otherwise-losing
  candidate is exactly the regime-owed artifact the per-year check exists to reject.
- neighborhood/plateau: n.a. (no winner to defend; if anything the response 0→7→15 is MONOTONE-increasing in
  net$/PF over the tested range, i.e. the live value 15 sits at the good end, not on an isolated peak).
  per-year: ✓ (rejection consistent for P0; P7's lone Y2 win insufficient). top-5: both candidates' Train
  PF_ex5 ≤ BASE (0.984 / 1.016 vs 1.055). walk-forward: n.a. (per-year IS the check).

### 7. VERDICT — REJECT both; keep live trend_partial = 15 (`council/scoring.py` UNCHANGED)
Neither candidate clears Train — a candidate must beat baseline on Train (per-year-consistent) before earning a
Validation look, and both are uniformly worse on the aggregate with no clean per-year win. Per protocol,
**Validation is NOT run and the Test one-touch budget stays UNSPENT** for the scoring family. This closes the
last open lead from the 2026-07-23 scoring NOTE: the partial-tier's −0.022R was a SELECTION-CONFOUND artifact,
NOT a removable defect — dropping the tier is actively harmful (Y1 flips negative, Y3 halves, DD rises). Same
lesson as EXP-015, now on the sibling lead: conditional present-vs-absent reads on the fired set do NOT survive
as real trade-set changes. The scoring formula's trend_alignment tiering (30/15/0) is running soundly and the
default is on the correct side of a monotone response — a textbook "default is already fine" (rules 5/9).

**"Find a replacement / keep hunting?" — NO.** With EXP-015 (reweighting) and EXP-016 (partial tier) both
rejected, the scoring-formula family has no remaining edge-positive lead: confluence is inert-but-benign,
thresholds sit on a flat/non-monotone surface (NOTE §3, highest-overfit knob — leave alone), macd & full-trend
are doing real work, and both plausible "fixes" to the two suspicious components proved harmful once measured
honestly. The scoring family is closed as "sound, no change warranted."

`config/base.yaml`, `council/scoring.py`, `decision_matrix.py`, `engine.py` UNCHANGED (monkeypatch in-process
only). Auditor gate thresholds NOT touched (rule 8). Harness: `experiments/exp016_trend_partial_harness.py`
(reuses `experiments/exp015_reweight_harness.py`).

---

## NOTE (not an EXP) 2026-07-23 — "add-to-a-loser" second position (martingale/averaging) feasibility @ $3,000

Exploratory, TRAIN-ONLY (2021-07-22 → 2024-07-21, 17,737 H1 bars). Validation and Test DELIBERATELY
NOT TOUCHED — this is a risk-first diagnostic for a genuinely NEW strategy family; the risk profile
alone may disqualify it before any split-based tuning is warranted (same convention as EXP-010/the
sizing NOTEs). User idea (verbatim TH): "หลัง order 1 ไม้ไปแล้ว ... Profit ติดลบ ...% อนุญาติให้บอท
เปิดไม้เพิ่ม 1 ไม้ ... เผื่อเอากำไรมาถัวกับที่เสียไป" = after leg 1 is floating at a loss of X%, open
ONE extra position to average out / offset the loss. This is a martingale/grid "add-to-a-loser" pattern.
User explicitly waived the current Shield constraints (max_positions_per_symbol=1 etc.) for this probe;
`config/base.yaml` and all src/ are UNCHANGED — standalone harness only.

### 0. What was built
`experiments/exp_martingale_secondleg_harness.py` — a two-slot simulation that REUSES production code
verbatim (engine `_council_signal_fn`, `check_exit`, cost model, Watchman `evaluate_watchman`, Shield
cooldown, `compute_lot_size`, `build_order_plan`) under the CURRENTLY-ADOPTED config (pivot 3, tp 2.0,
all-24h [0,24), be/trail OFF + structure/time-stop ON, Shield cooldown ON, min_lot_risk_cap 1.5, risk
1.0%). Start equity $3,000. Commission $0 (the user's REAL IC Markets **Standard** account, per memory);
spread baked per-bar from the data's own spread column (min-1-spread). Trigger X is expressed as a
fraction of leg-1's own initial risk R (regime-normalized; at 1% risk, −0.5R ≈ −0.5% to −0.75% of
equity given the min-lot fallback). The council signal at each bar is identical across variants, so it
is precomputed ONCE and reused (O(n) per variant). **FIDELITY PASSED**: harness baseline reproduces the
stock `run_backtest` on a 5,000-bar slice to the cent (235 trades, PF 1.0511, net $180.6, both).

Two readings of the ambiguous idea, tested separately:
- (A) SAME-DIRECTION averaging/martingale: while leg 1 open and floating ≤ −X·R, open leg 2 SAME
  direction (no fresh signal), leg-1 stop-distance as risk unit, own 2R TP, independent exit. Sizing
  1.0x (pure averaging) and 2.0x (double-down).
- (B) INDEPENDENT-SECOND-SLOT unlock: while leg 1 floating ≤ −X·R, unlock a 2nd slot for a genuinely
  fresh Council/Risk-Voice-approved signal (any direction), sized normally. Not averaging the same trade.

Concurrency capped at 2 legs; a fresh leg-1 signal only fires when fully flat. "Mark-to-market (MTM)"
equity = realized + floating of both open legs, per bar — the honest curve for drawdown/ruin.

### 1. Results (Train, $3,000, comm $0)
```
label                trades  PF      net$     MTM_maxDD%  worst_single  worst_streak  per-year PF (2021/22/23/24)
B0_baseline (1 pos)    755   1.1019  +1309.9    25.54       -45.3        -276.1       0.963 / 1.187 / 1.123 / 1.059
A1 same -0.5R 1x       963   1.0558   +922.6    31.47       -45.3        -371.4       1.138 / 1.169 / 0.989 / 0.944
A2 same -0.5R 2x       963   1.0671  +1573.8    31.95      -113.0        -546.3       1.166 / 1.212 / 0.982 / 0.953
A3 same -0.75R 2x      845   1.0844  +1205.2    28.92       -80.4        -276.2       0.926 / 1.163 / 1.164 / 0.990
B1 fresh slot -0.5R    902   1.0916  +1419.1    23.06       -47.0        -273.8       0.987 / 1.104 / 1.167 / 1.041
```
Min MTM equity trough (worst point reached, from $3,000 start): B0 $2,810 / A1 $2,825 / A2 $2,813 /
A3 $2,644 / B1 $2,684. NO variant busts the account (never near $0) in the Train history.

### 2. Risk findings (the point of this probe)
1. **Worst-case sequential drawdown / ruin @ $3,000.** No variant blows the account in the Train data —
   but NOT because martingale is safe: it is because at $3,000 with 1% risk + the min-lot fallback,
   positions are already pinned to the broker floor (0.01 lot; 0.02 at the 2x leg), so absolute
   per-trade dollars are tiny ($45–$113). The min-lot floor MASKS the martingale tail while delivering
   none of its upside — the account is too small to size the "recover faster" leg up meaningfully. The
   danger it DOES add is visible in the tails: 2x (A2) roughly TRIPLES worst single loss (−$45 → −$113,
   ≈3.8% of the account in one trade) and DOUBLES worst losing streak (−$276 → −$546, ≈18% of account),
   and pushes MTM drawdown to ~32%. On a LARGER account where risk-sizing produces real lots, the same
   2x double-down into a sustained adverse trend is the classic single-sequence ruin — the $3,000 floor
   is the only thing hiding it here.
2. **Circuit-breaker reality.** Even BASELINE MTM drawdown is 25.5% — already 3x past the 8%
   `max_drawdown_halt_pct` (unmodeled in the engine, same caveat as every prior NOTE). Variant A makes
   it strictly worse (29–32%). So A2's headline net "+$1,574" is FICTIONAL live: the halt would have
   stopped trading long before, multiple times. Not a live-achievable number.
3. **Margin feasibility @ $3,000: not the binding constraint.** A 0.02-lot XAUUSD position at 2026 gold
   (~$3,300) is ~$6,600 notional → ~$13 margin at 1:500 (≈$66 even at 1:100). Trivially affordable on
   $3,000. But this is cold comfort: the account CAN open the second leg, it just can't size it big
   enough for the martingale premise ("bigger size recovers faster") to matter, while still eating the
   fatter downside. Margin never saves or sinks this idea; sizing economics and tail risk do.

### 3. Risk-adjusted verdict vs baseline
- **Variant A (same-direction averaging / martingale) — REJECT.** It does NOT improve risk-adjusted
  return; it degrades it. PF falls below baseline (1.056–1.084 vs 1.102), MTM drawdown rises
  (29–32% vs 25.5%), tails fatten with the multiplier. Decisively, it BREAKS per-year robustness: the
  baseline is PF≥1.06 in three of four years and 0.96 in the fourth, whereas A1/A2 FLIP the two most
  RECENT years (2023 AND 2024) net-NEGATIVE. A2's higher aggregate net is a pure regime artifact —
  it juices the single 2022 trending year (+$1,530 vs baseline +$710) while bleeding in the choppier
  2023–24. This is the textbook martingale signature: same ~breakeven expectancy reshaped into a
  lumpier, fatter-left-tailed distribution (more small averaging wins, offset by rarer bigger losses and
  worse recent-regime behavior) — exactly the failure mode the user should NOT want on a small account.
- **Variant B (independent 2nd slot on a fresh signal) — NEUTRAL, and not the user's idea.** Roughly
  matches baseline (PF 1.092 vs 1.102 — marginally WORSE PF), slightly higher net (+$1,419), slightly
  LOWER MTM drawdown (23.1%), per-year consistent (no year flipped hard negative). It is safe but
  delivers no clear edge, and mechanically it is "allow 2 concurrent independent trades" — a
  Shield `max_positions_per_symbol` question (spec: "raise to 2 only after 3 months live"), NOT the
  averaging mechanic the user described. No basis to pursue it now over the spec's own guidance.

### 4. Recommendation — DO NOT PURSUE (no promotion to a pre-registered EXP)
The same-direction averaging idea (the user's actual intent) is a net-negative for a $3,000 account:
worse PF, worse drawdown, fatter tails, and it converts the two most recent robust years into losers.
At this account size it also can't even be sized to do what martingale is supposed to do. Nothing here
survives the risk sanity-check, so no Validation/Test EXP is warranted (Test one-touch budget UNSPENT).
`config/base.yaml` and all src/ UNCHANGED; Auditor gate thresholds NOT touched (rule 8). If the user
still wants a "use the idle slot" behavior, the disciplined path is the DIFFERENT question of
`max_positions_per_symbol: 1→2` after the spec's 3-months-live condition — evaluated on its own merits,
not as loss-averaging. Cross-ref: a $7/lot Raw-Spread pass was not run (it lowers every variant's PF
roughly uniformly and would not change this ranking). Harness: `experiments/exp_martingale_secondleg_harness.py`.

---

## NOTE (not an EXP) 2026-07-23 — OPPOSITE-DIRECTION "hedge" second position feasibility @ $3,000

Exploratory, TRAIN-ONLY (2021-07-22 → 2024-07-21, 17,737 H1 bars). Validation and Test DELIBERATELY
NOT TOUCHED — same risk-first convention as the martingale NOTE directly above (a genuinely NEW strategy
family disqualified by its risk shape alone needs no split-based tuning). User idea (verbatim TH):
"ในทางกลับกันถ้าทำ hedging ถ้า buy อยู่ แล้ว sell สวนมาอีกไม้จะเป็นยังไง ทำเฉพาะ Profile เริ่มติดลบมากๆ"
= conversely to martingale, HEDGE — while leg 1 (say BUY) is open and floating heavily negative, open
leg 2 in the OPPOSITE direction (SELL). This is a DIFFERENT mechanism from martingale (which doubled the
SAME bet): a hedge is NET-FLAT while both legs are open — it caps further loss on leg 1 but also caps
leg 1's recovery, and adds a fresh whipsaw + double-spread failure mode. Judged on its own merits.

### 0. What was built
`experiments/exp_hedge_secondleg_harness.py` — adapts the martingale two-slot harness; REUSES production
code verbatim (engine `_council_signal_fn`, `check_exit`, cost model, Watchman `evaluate_watchman`,
Shield cooldown, `compute_lot_size`, `build_order_plan`) under the CURRENTLY-ADOPTED config (pivot 3,
tp 2.0, all-24h [0,24), be/trail OFF + structure/time-stop ON, Shield cooldown ON, min_lot_risk_cap 1.5,
risk 1.0%). Start equity $3,000. Commission $0 (IC Markets **Standard**, per memory); spread baked
per-bar (min-1-spread). **FIDELITY PASSED**: baseline reproduces stock `run_backtest` on the 5,000-bar
slice to the cent (235 trades, PF 1.0511, net $180.6), and B0 on full Train reproduces the martingale
NOTE's B0 exactly (755 trades, PF 1.1019, net $1,309.9, MTM maxDD 25.54%, per-year 0.963/1.187/1.123/1.059).

Hedge leg = **SAME lot as leg 1** (net-flat while both open; a bigger hedge would be a disguised
reversal, out of scope). Trigger X = fraction of leg-1's own initial risk R, swept at −0.5R and −1.0R.
THE EXIT RULE IS THE CRUX of a hedge (unlike martingale's independent adds), so two well-motivated rules
were tested separately:
- **(Ha) INDEPENDENT MIRRORED EXITS** — direct analog of the martingale leg-2 handling, flipped: hedge
  gets its own SL (=entry∓d) and 2R TP (=entry±2d), d = leg-1 stop distance; both legs exit independently.
  Exposes the WHIPSAW failure (leg 1 stops out, then price reverses and stops the hedge too).
- **(Hb) LOCK-AND-RELEASE** — the classic "cap the loss, wait" intent: hedge has no exit of its own;
  it closes when (1) leg 1 recovers to breakeven (lock the small hedge loss, let leg 1 ride on), or
  (2) leg 1 hits its own SL/Watchman exit (close BOTH same bar → combined loss CAPPED below −1R). Hedge
  can never outlive leg 1; hedge exit price = bar close (realistic, not the optimistic leg-1 SL touch).
"Net position" P&L is honest: MTM equity = realized + floating of BOTH legs each bar (floats offset while
net-flat). Per-EPISODE combined P&L (leg1 total + hedge total, from equity_at_trigger to fully-flat) is
tracked to see if the hedge CAPS the tail. Concurrency capped at 2; fresh leg-1 signal only when flat.

### 1. Results (Train, $3,000, comm $0)
```
label                trades  PF      net$    maxDD%  worst_single  worst_streak  hedge_eps  neg%   hedge_eps_net$  per-year PF (21/22/23/24)
B0_baseline (1 pos)    755  1.1019  +1309.9  25.54    -45.3         -276.1          0        --        --          0.963/1.187/1.123/1.059
Ha_indep  -0.5R        862  1.0573   +758.3  26.12    -41.6         -274.6        246       66%     -3872         0.951/1.152/1.069/0.967
Ha_indep  -1.0R        756  1.1023  +1318.9  26.21    -45.3         -276.1          3      100%      -145         0.967/1.197/1.112/1.060
Hb_lock   -0.5R       1069  1.0419   +602.8  25.88    -92.9         -227.7        314       86%     -5115         0.910/1.144/1.071/0.961
Hb_lock   -1.0R        758  1.1023  +1316.5  25.84    -45.3         -276.1          3      100%       -88         0.965/1.186/1.120/1.065
```
"hedge_eps_net$" = aggregate combined P&L of just the episodes where a hedge fired. MTM trough (worst
equity reached from $3,000): B0 $2,810 / Ha-.5 $2,760 / Hb-.5 $2,689 — no variant approaches ruin.

### 2. Risk findings (the point of this probe)
1. **Does the hedge CAP the tail? Yes — but that's not where it fails.** Unlike martingale (which FATTENED
   the left tail: worst single −45→−113, worst streak −276→−546), hedging leaves the extreme tail roughly
   NEUTRAL-to-slightly-better: no single episode blows up (worst combined episode −75 for Ha, and Hb's
   worst STREAK is actually milder, −228 vs −276), MTM drawdown ~26% ≈ baseline. So the hedge does what it
   promises mechanically — it bounds any single sequence. The problem is the OPPOSITE end.
2. **It fails on CHRONIC EXPECTANCY EROSION, not tail risk.** At the only trigger that actually fires
   (−0.5R), the hedge activates 246–314 times and LOSES on 66% (Ha) to 86% (Hb) of those episodes, for an
   aggregate drag of −$3,872 (Ha) to −$5,115 (Hb). The account stays net-positive ONLY because the
   non-hedged trades carry it; the hedge activity itself is pure bleed. Net drops 42% (Ha, +1310→+758) to
   54% (Hb, +1310→+603); PF falls 1.102→1.057/1.042. This is exactly the predicted "whipsaw + double-spread
   + mechanical cancellation of leg-1's edge" failure: the worst episodes' min-float is only −$15 to −$35
   (shallow chop, NOT deep disasters) — the hedge got opened into noise, paid spread twice, and cancelled
   the directional edge it was supposed to protect. Hb is worse than Ha because "lock at breakeven"
   systematically pays the hedge cost right before leg 1 would have recovered on its own, and its
   forced both-legs-close can even WORSEN worst single trade (−92.9 vs −45.3).
3. **No trigger sweet-spot exists.** At −1.0R the trigger essentially never fires before leg-1's own SL
   closes the trade (only 3 episodes across 3 years) → both rules collapse back to baseline (inert). So
   the hedge is a strict lose-lose: shallow trigger = chronic bleed, deep trigger = does nothing. There is
   no X where it earns its keep.
4. **Robustness break — same signature as martingale.** The −0.5R hedges FLIP the most RECENT year 2024
   net-NEGATIVE (Ha 0.967, Hb 0.961) and Hb also drags 2021 to 0.910, exactly the recent-regime
   degradation the martingale variants showed. An idea that only "works" by not firing, and hurts the
   moment it does, is not a robust edge.
5. **Margin / circuit-breaker: not the binding constraints (same as martingale).** Hedge leg is 0.01 lot,
   ~$6,600 notional → ~$13 margin at 1:500; trivially affordable at $3,000. MTM drawdown ~26% is still 3x
   past the 8% `max_drawdown_halt_pct` (unmodeled) for EVERY variant incl. baseline, so headline nets are
   not live-achievable — but the halt caveat is identical across variants and doesn't change the ranking.

### 3. Verdict vs baseline AND vs the martingale NOTE
- **Hedge (both Ha and Hb) — REJECT.** It does NOT improve risk-adjusted return; it erodes it. Instructive
  contrast with martingale: **martingale kills you with TAIL risk** (rare fat losses, fatter left tail,
  same central expectancy), whereas **hedging kills you with CENTRAL expectancy** (no fat tail — arguably
  safer in the pure ruin sense — but a chronic, pervasive bleed that cancels the strategy's edge on every
  shallow whipsaw and charges spread twice). Both share the same recent-year robustness break (2024 flips
  negative). Neither is worth pursuing; they fail for opposite reasons but fail equally.
- The "lock-and-release" intent the user was reaching for (cap the loss, wait it out) DOES bound the
  single-episode loss as designed — but empirically that protection is worth far less than it costs,
  because the strategy's losers are mostly shallow chop, not deep runaway trends, so the hedge mostly
  "protects" trades that would have recovered anyway, at full double-spread price.

### 4. Recommendation — DO NOT PURSUE (no promotion to a pre-registered EXP)
Opposite-direction hedging is a net-negative for a $3,000 account: lower PF, lower net (−42% to −54%),
comparable drawdown, and it flips 2024 (and Hb also 2021) negative — while the tail protection it does
deliver is not the account's actual problem. There is no trigger value that earns its keep (shallow =
bleed, deep = inert). Nothing survives the risk sanity-check, so no Validation/Test EXP is warranted
(Test one-touch budget UNSPENT). `config/base.yaml` and all src/ UNCHANGED; Auditor gate thresholds NOT
touched (rule 8). If the user wants downside protection on deep-underwater trades, the disciplined
alternatives are (a) a tighter/earlier structure-based exit on leg 1 (tune the existing Watchman
`dead_trade_r_band` / time-stop — a real EXP on the ONE stop already in the pipeline), or (b) simply
smaller risk% — both of which reduce the same downside WITHOUT paying spread twice or cancelling the
edge. Cross-ref: a $7/lot Raw-Spread pass was not run (uniformly lowers PF, would only worsen every hedge
variant relative to baseline). Harness: `experiments/exp_hedge_secondleg_harness.py`.

---

## NOTE (not an EXP) 2026-07-23 — Winter/Summer SEASONALITY + DST / server-time-mechanics probe

Diagnostic, not a sweep. No Train/Val/Test pre-registration; Test budget UNTOUCHED. Standalone harness
`experiments/analysis_seasonality_dst.py` reuses production `run_backtest` OFFLINE (manual SymbolSpec,
same pattern as the martingale harness) under the CURRENTLY-ADOPTED config (pivot 3, tp 2.0, all-24h
[0,24), be/trail OFF + structure/time-stop ON, Shield cooldown ON, risk 1.0%, min_lot_cap 1.5, comm $0,
spread per-bar min-1). Full 5yr XAUUSD H1 (2021-07-22 → 2026-07-21, 29,543 bars; 1,245 trades, overall
PF 1.126, net +$3,148 @ $3,000). `config/base.yaml` and src/ UNCHANGED. First check of this surface —
grep confirmed no prior season/DST EXP (only EXP-003 §4 flagged "DST drift" as an unquantified caveat;
this NOTE resolves it empirically).

### Q1 — Calendar / seasonal pattern in the trades
Month-of-year (all 5yr pooled), PF (net$): Jan 1.03(+304) Feb 1.32(+524) Mar 1.51(+891) Apr 0.79(-387)
May 0.89(-179) Jun 0.81(-185) Jul 1.02(-22) Aug 1.44(+710) Sep 1.53(+735) Oct 1.21(+267) Nov 1.51(+848)
Dec 0.81(-358). Season pooled: winter(NDJF) PF 1.132(+1318,431tr); summer(MJJA) PF 1.021(+324,422tr);
shoulder(MA+SO) PF 1.242(+1506,392tr).

Per-year robustness (the bar every finding here must clear) — PF(net$,n) by season × trade-year:
```
season           2021          2022          2023          2024          2025          2026(part)
winter(NDJF)  1.09(+77,102) 1.01(+42,86)  1.15(+243,86) 1.10(+156,88) 1.39(+799,69)   --
summer(MJJA)  0.80(-195,71) 1.05(+121,90) 0.98(-137,82) 1.11(+247,82) 1.28(+465,81)  0.53(-176,16)
shoulder      1.07(+64,82)  1.00(+12,71)  1.82(+864,72) 1.30(+407,78) 1.18(+159,89)   --
```
Findings: (a) winter (Nov–Feb) is the ONLY season that is net-positive AND PF≥1.0 in EVERY one of the 5
years — a genuinely per-year-consistent mild tilt. (b) But it is NOT clean: within "winter", Dec alone is
a LOSER (PF 0.81, -$358) — the winter edge is really Feb+Nov, not a solid 4-month block. (c) The pooled
shoulder headline (1.24) is INFLATED by a single outlier year: 2023's Mar–Apr/Sep–Oct at PF 1.82 (+$864);
strip 2023 and shoulder collapses to ~1.1. (d) The most consistent WEAK stretch is Apr–Jun (pooled PF
0.79/0.89/0.81) — but summer as a whole is per-year mixed (negative 2021 & 2023, positive 2022/24/25), i.e.
roughly break-even, not reliably losing. Net: there is a real, modest winter>summer bias that survives
per-year checks, but the monthly structure is noisy (Dec breaks winter; shoulder rides one year) and the
effect size is small (winter 1.13 vs summer 1.02). A month/season entry-filter is a 12-dim calendar
overlay = high curve-fit risk for a ~0.1-PF pooled gap. VERDICT: mild real signal, NOT clean/large enough
to justify a calendar filter now. If ever pursued, the ONLY disciplined framing is ONE narrowly-scoped,
mechanistically-motivated EXP (e.g. "de-risk/skip Apr–Jun" as a single binary, pre-registered, per-year-
gated hypothesis) — LOW priority, and expected to be fragile. Not recommending an EXP.

### Q2 — DST / server-time mechanics (the structural question)
Server clock = MT5 SERVER time (`global.timezone: server`); every hour threshold, incl. the ACTIVE
`risk_voice.friday_close_hour: 20`, is in server time. Empirical DST test: mean H1 bar range (high−low) by
hour-of-day, summer (Apr–Oct) vs winter (Nov–Mar). The London/NY volatility ramp sits at SERVER hours
15–17 in BOTH regimes (summer peak-range hours 16,17,15; winter 17,16,15) — NO 1-hour seasonal shift.
Per-calendar-month peak-range hour is pinned at server 16±1 all 12 months. **Conclusion: this broker's
server clock DOES observe DST (EET/EEST, i.e. UTC+2 winter / UTC+3 summer — the standard IC Markets
behavior).** If it were a fixed UTC offset, the NY ramp would visibly shift 1h between summer/winter; it
does not. Mechanically: NY open (13:30 UTC summer / 14:30 UTC winter) maps to ~16:30 server in BOTH
regimes precisely because the +3/+2 server offset absorbs the shift.

Consequence for `friday_close_hour: 20`: because the server auto-adjusts, 20:00 server maps to a STABLE
real-world market time year-round (~3h before the 23:00-server NY close in both regimes). So the hypothesized
"weekend-gap protection drifts up to 1h across the DST calendar" gap DOES NOT EXIST for this broker — the
DST-observing server is the SAFE regime, and no config change is warranted. (Minor caveat: US and EU switch
DST on different dates; during the ~2–3 shoulder weeks where only one has switched, the NY session sits 1h
off in server time — visible as the tiny Apr=17/May=16 and Oct=17/Nov=16 wobble in the monthly peak-hour
table. Immaterial to a 20:00 threshold with a 3h buffer.) **Actionable takeaway: this finding is only
valid because the server observes DST — if the account is ever moved to a broker whose server is a fixed
UTC offset, all hour thresholds (friday_close_hour especially) WOULD drift 1h twice a year and must be
re-verified. Worth documenting, not worth a config change now.**

Data quality in DST-transition weeks (all 5yr, US ~2nd-Sun-Mar/1st-Sun-Nov + EU last-Sun-Mar/Oct):
every transition week clean — zero_spread=0 (spread-floor holding), spreads 1–9 (one 29-pt bar 2025-03-30,
still floored/finite), no missing-bar anomalies beyond the normal daily ~1h maintenance break and the
standard weekend close. Only oddity: 2024-03-31 has fewer bars (46) + a 3-day max gap = EU DST weekend
coinciding with Easter Sunday, a legitimate holiday close, not a feed glitch. No thin/glitchy transition-
weekend liquidity signature.

### Bottom line
Q1: checked — a mild, per-year-consistent winter>summer tilt EXISTS but is small and structurally noisy
(Dec negative, shoulder inflated by 2023); NOT worth a calendar filter (overfit risk ≫ edge). Q2: checked
— server observes DST, so `friday_close_hour` and all hour gates are DST-stable in real-world terms; the
suspected seasonal weekend-gap hole is a NON-issue on this broker (structural knowledge worth keeping, not
urgent). Neither finding warrants a config change or an immediate EXP. Harness:
`experiments/analysis_seasonality_dst.py`.

## EXP-017 2026-07-23 — ADDITIVE "deep-loss timeout" exit condition (NEW family "loss-cutting exit")
Status: REJECTED
Hypothesis: today's live paper trade (BUY XAUUSD, held ~7h56m, closed −0.90R / −$36.12 via
structure_invalidation) suggests a deeply-losing trade bleeds too long before an existing exit fires. The
current exits structurally cannot cut a deep loser early: fixed SL only at −1R; structure_invalidation
waits for a closed-bar swing break; time_stop needs ≥48h AND ±0.3R ("dead/flat" only — a −0.9R trade is far
outside its band). Genuine never-tested gap. NOT EXP-008's breakeven/trail (those act on PROFIT and cut
WINNERS). New rule: while open, if floating R ≤ −X AND held ≥ Y hours AND no existing exit fired this bar,
close at the bar's close. X is always a NEGATIVE-R threshold, so it can never touch a flat/profitable trade.
Pre-registered range & metric: 2D grid X∈{0.5,0.7}R × Y∈{4,8,12}h = 6 configs, Train-only per-year (y1
2021-22, y2 2022-23, y3 2023-24). Decision metric = per-year PF/net$/avgR AND worst-single-trade / worst-10%
tail severity. Acceptance (ALL required): (a) meaningfully reduce worst loss / left-tail, (b) preserve or
improve PF/net$/avgR across ALL 3 Train years, (c) winners genuinely untouched (fidelity: X=−5 ⇒ byte-
identical to baseline), (d) plateau (neighboring X/Y behave similarly, not a lucky spike).
Configs evaluated (this exp / cumulative for this param family): 6 / 6 (first-ever in this family).
Baseline = live adopted config: pivot 3, tp 2.0, all-24h Risk Voice, Shield cooldown, min-lot cap 1.5, be/
trail OFF (EXP-008), structure+time ON, risk 1.0%, commission $0 (IC Markets Standard), $10k equity.
Harness: experiments/exp017_deeploss_timeout_harness.py — a standalone VERBATIM copy of
backtest.engine.run_backtest's loop reusing every engine helper, adding ONLY the deep-loss check at the
required precedence (existing exits always win). No src/ or config touched.

FIDELITY (mandatory, on y1): real run_backtest = my memoized re-sim with deep-loss DISABLED = my re-sim with
unreachable X=5.0, all three 265 trades BYTE-FOR-BYTE identical. Harness trusted. Signal/ATR memoization
(pure fn of (df,i)) validated by the same check.

RESULTS — PF | net$ | avgR by config, per Train year (baseline bold-equiv first):
                     y1 (2021-22)            y2 (2022-23)            y3 (2023-24)
baseline        PF1.033  +502  0.027    PF0.975  −406  −0.016   PF1.224 +3321  0.139
X0.5 Y4         PF0.945  −859 −0.025    PF0.976  −382  −0.008   PF1.094 +1594  0.062
X0.5 Y8         PF0.966  −533 −0.012    PF1.017  +287  +0.015   PF1.056  +944  0.044
X0.5 Y12        PF0.976  −388 −0.007    PF1.050  +828  +0.036   PF1.077 +1270  0.053
X0.7 Y4         PF1.047  +744  0.035    PF0.981  −306  −0.010   PF1.127 +2127  0.089
X0.7 Y8         PF1.020  +320  0.019    PF0.982  −291  −0.009   PF1.101 +1626  0.075
X0.7 Y12        PF1.012  +186  0.013    PF0.989  −180  −0.004   PF1.120 +1947  0.086
Worst single-trade net (tail): baseline y1 −157.49 / y2 −129.1 / y3 −142.7 — UNCHANGED (±few $) under EVERY
config. Deep-loss fires on bar-CLOSE floating R; the genuinely worst trades are gap-through-SL fills it
structurally cannot pre-empt. worst-10% tail mean flat-to-slightly-better only because the rule ADDS more
small losers (churn), diluting the pool — not because it truncates the real left tail. Trade counts RISE
(y1 265→322) and win-rate FALLS (y1 0.370→0.308) as would-recover holds become realized losers + reshuffle
downstream selection (single-position engine).

Robustness: neighborhood ✗ (best (X,Y) FLIPS by year: y1→X0.7/Y4, y2→X0.5/Y12; no shared plateau), per-year
✗ (NO config preserves/improves all 3 years — every config bleeds y3, the strongest baseline year, PF
1.224→1.09–1.13, net −$1.2k to −$1.7k), top-5 n.a. (rejected earlier), walk-forward n.a.
Decision & rationale: REJECT all 6 (X,Y). Fails acceptance (a) — no meaningful worst-loss/tail reduction
(worst losses are gaps the rule can't catch). Fails (b)/(d) — no config improves all 3 years and there is no
plateau; the per-year optimum flips with regime = noise. Confirms the exact predicted failure mode: the rule
cuts trades that would have recovered past structure_invalidation's own exit, gutting the best year (y3). The
one config that helps the two weak years marginally (X0.7/Y4: y1 +0.014 PF, y2 +0.006 PF) destroys y3
(−0.097 PF, −$1,194). Net: the current exit stack (SL/structure/time) already handles deep losers better
than a blanket floating-R timeout. Do NOT proceed to Validation; Validation/Test untouched (as required).
Today's −0.90R trade is within normal variance, not evidence of a systemic early-exit gap.

## EXP-018 2026-07-24 — Overnight SWAP / rollover cost model (NEW cost-component; backtest/live PARITY gap)
Status: BUILT + PRE-REGISTERED (capability added, default OFF = "not modeled"); materiality assessed. NO promotion, NO gate change.
Trigger: cross-project out-of-sample study (D:\ForexTrade EXP-053..058, XAUUSD 2009-2019 — a market era this
project's own 5-yr window (2021-07 → 2026-07) never covered) flagged (a) our backtest models no swap/rollover
while live DOES pay it, and (b) our edge does not survive pre-2021 regimes.

VERIFICATION of the three cross-project claims (done from inside this repo, not taken on faith):
- (1) cost_model.py swap gap — CONFIRMED. `src/autotrade/backtest/cost_model.py` (pre-EXP-018) modeled only
  spread+slippage+commission; no swap term anywhere. MEANWHILE the LIVE side already pays real MT5 swap:
  `execution/adapter.py` + `store/models.py` fold "commission + swap" into `TradeRecord.cost`. So this was a
  genuine backtest↔live PARITY gap (live pays a cost the simulator ignored), not merely a missing refinement.
- (2) replica fidelity — CONFIRMED reproducible. `data/db/backtest_reports/XAUUSD_20260722T055933Z.json`
  exists in THIS repo and shows n=1259, PF 1.0799, DD 29.4%, win 0.382, full 5-yr window, commission $7 —
  matching the cross-project "n=1259, PF 1.08" reproduction claim exactly.
- (3) internal per-year consistency — CONFIRMED and WELL-CALIBRATED. The report's "2022-23 sideways ≈ PF 0.98"
  matches THIS repo's own EXP-017 baseline (current live config: be/trail OFF, $0 commission, $10k) per-year:
  y1 2021-22 PF 1.033 (+$502), y2 2022-23 PF 0.975 (−$406, LOSING), y3 2023-24 PF 1.224 (+$3,321). The edge is
  concentrated in the single trending year; the flat/sideways year is net-negative — exactly the cross-project
  thesis. (Note: the older EXP-002 showed y2=1.001, but that was a pre-EXP-008 config; the current config's
  0.975 is the right comparison and the report's 0.98 is accurate against it.)

Hypothesis / mechanism: XAUUSD is held overnight ~1 night/trade; broker books swap per night (triple Wednesday),
long-negative on gold. A per-trade swap charge on the order of the strategy's own per-trade expectancy would
materially erode or erase the edge — and does so WORST in exactly the gold-bull regime that produces the apparent
edge (net-long bias ⇒ maximal long-swap drag). This couples the "beta" and the cost: the regime that flatters PF
is the regime that pays the most swap.
Metric that decides materiality: swap-in-R per trade vs. baseline avg-R per trade (a LOT-SIZE- and EQUITY-
INDEPENDENT comparison: swap_R = rate·nights / (stop_distance·point_value); the $/lot·night figure and the
account size both cancel). Acceptance for "material": swap_R is a non-trivial fraction of avg_R.

Rates used (source stated, per mandate): long −$53.2 / short +$36.8 per 1.0 lot per night, 3× Wednesday — the
figures the cross-project report cited from an IC Markets demo measurement. NO more-authoritative swap rate is
recorded anywhere in THIS repo (checked config/base.yaml, .env, execution/*, store/*: live reads swap from MT5
at runtime, never persists a static rate). These rates are broker- & time-varying (~±20%); treated as an
order-of-magnitude input, not a precise constant. A real promotion run should pass the account's own live rates.

MATERIALITY (analytical, anchored to reproduced report): full-5yr avg_R = 0.0525 (a THIN edge). For a net-long
gold trade, swap_R ≈ 53.2·1 / (stop_distance·100). At a representative H1 ATR stop ~$8–$15 (price) that is
≈ 0.035–0.066 R of drag PER overnight long — i.e. same order of magnitude as the ENTIRE 0.0525R expectancy.
Shorts earn ~0.037R, so net impact scales with long/short balance and is largest in a long-biased bull regime.
CONCLUSION: swap is MATERIAL to expectancy in PF/R terms, independent of the small $-figures on a $3k account.
CAVEAT on the report's framing: its headline $5.3–9.5k / "−$1,561 → −$6,895" swap figures are at 1.0 lot and do
NOT translate to this $3k / ~0.01–0.05-lot account's dollar P&L (over-dramatic in absolute $ for this account) —
but the PROPORTIONAL PF/edge erosion DOES carry over regardless of lot size, and that is what matters for a
promote decision. So the report is directionally right and materially important; only its absolute-$ drama is
mis-scaled for this account.

IMPLEMENTATION (capability only; behavior unchanged unless a caller opts in):
- `backtest/cost_model.py`: new `SwapModelConfig` (long/short per-lot-per-night, triple_swap_weekday=Wed,
  rollover_hour=0; Sat/Sun rollovers 0× — weekend carry recovered by Wed 3×, faithful to broker booking),
  `effective_swap_nights()`, `swap_cost()` (returns positive-when-charged, mirroring store/models.py's live
  `cost` sign convention so backtest↔live agree). New optional `CostModelConfig.swap_model` field.
- `backtest/engine.py`: `_close_trade` adds swap into `cost` when `swap_model is not None`; ClosedTrade.cost
  docstring updated (now "commission + swap", matching live).
- `scripts/run_backtest.py`: optional `--swap-long-per-lot`/`--swap-short-per-lot` (must be given together);
  new envelope flag `swap_modeled` alongside the existing risk_voice/watchman/shield honesty flags.
- DEFAULT everywhere = `None` = "not modeled" (same explicit-placeholder convention as risk_voice_cfg/
  watchman_cfg/shield_cfg). No adopted result changes; no existing backtest silently altered.
- Tests: 7 new known-input cases in tests/unit/backtest/test_cost_model.py (Wed 3×, weekend 0×, intraday 0,
  multi-night, long-charge/short-credit sign, lot scaling). Full backtest unit suite: 87 passed.

GATE POLICY — DELIBERATELY NOT DECIDED HERE (rule 8, never tune the referee): whether a promotion-relevant run
MUST have swap_modeled=true (i.e. folding swap into `cost_model_complete` / the promotion gate) is a human
decision. `cost_model_complete` is left UNCHANGED (still slippage-only). Escalated to user.

Robustness: neighborhood n.a. (not a tuned parameter — a cost correction), per-year n.a. (see EXP-017 for the
current-config per-year picture this corroborates), top-5 n.a., walk-forward n.a.
EMPIRICAL on-our-data re-run (Train+Val 2021-07→2025-07, Test year EXCLUDED, $50k equity per EXP-008 sizing-
confound-free method, commission $0 IC Markets Standard, swap long −53.2 / short +36.8):
  SWAP OFF: n=1006, PF 1.0716, avgR 0.0498, net +$25,303, DD 30.40% (long 558 / short 448, avg 1.09 nights/trade)
  SWAP ON : n=1006, PF 1.0532, avgR 0.0393, net +$17,885, DD 31.79%
  DELTA   : PF −0.0185, avgR −0.0105 (= −21% of expectancy), net −$7,418, DD +1.4pp.
So swap costs ~0.0105R/trade on average (blended: 45% are shorts earning the credit, which partly offsets the
long drag — hence smaller than the ~0.035–0.066R per-LONG analytical figure, and consistent with it). On the
gold-bull Train+Val window the edge SURVIVES (PF still 1.05, positive) but loses a fifth of its expectancy. The
regime read is the sharp part: the report's 2009-2019 bear/sideways decade already sat at PF ~0.96–0.99 WITHOUT
swap, so a −0.0185 PF haircut pushes it further underwater — corroborating the cross-project "swap flips the
decade more negative" finding. And a more strongly long-biased bull run would take a larger hit than this
mildly-biased (558/448) window did.

Decision & rationale: ADOPT the swap-model CAPABILITY (code + tests), default OFF. Do NOT promote, do NOT change
any gate. The evidence — verified parity gap + thin 0.0525R edge + swap drag of the same order + corroborating
current-config per-year (y2 2022-23 PF 0.975) — supports the cross-project warning: the Test-year PF 1.28 is
best read as gold-bull regime beta, not established stable alpha, and should NOT by itself justify live/scale.
Recommended next steps (each its OWN pre-registered experiment): (i) real-rate swap re-run of the full history;
(ii) regime-awareness as an explicit hypothesis tested on 2009-2019 data; (iii) keep the free real out-of-sample
(paper trading) running.

## EXP-019 2026-07-24 — SWAP-ON full-pipeline re-run of the ADOPTED config (Option 1; cost-honesty, NO behavior change)
Status: PRE-REGISTERED (running) — Train+Val only; Test (2025-07-21 → 2026-07-21) NOT touched (see Test-touch note).
Follow-up to EXP-018 (swap capability built, default OFF). This is NOT a parameter tune and NOT a strategy change:
it re-runs the CURRENT adopted config with the swap model ENABLED using EXP-018's sourced IC Markets demo rates
(long −$53.2 / short +$36.8 per lot per night, 3× Wednesday) and asks what the promotion-gate picture looks like
once total costs are complete, vs. the swap-OFF numbers already on record.
Hypothesis / mechanism: live already pays swap (store/models.py folds it into TradeRecord.cost); the backtest did
not. Folding it in can only REDUCE PF/avgR (long-swap drag > short-swap credit on a mildly long-biased book). The
question is whether the drag changes any gate verdict.
Metric that decides: Gate-1 (run_auditor promotion --gate backtest) criteria — PF ≥ 1.3, DD ≤ 15%, trades ≥ 200,
PF_excl_top5 > 1.0 — evaluated on Train+Val (n≈1006 ≥ 200, so the numeric thresholds are meaningfully checkable
without the held-out Test year). Report swap-OFF vs swap-ON side by side; per-year y1/y2/y3 + Val breakdown.
Harness: experiments/exp019_020_swap_regime_harness.py (VERBATIM run_backtest loop; swap needs no loop change —
CostModelConfig.swap_model is consumed by the reused _close_trade). Commission $0 (IC Markets Standard, current
account per MEMORY), $10k equity (matches EXP-017's per-year baseline for direct comparability). FIDELITY:
regime-OFF + swap-OFF re-sim == real run_backtest byte-for-byte on y1 (265 trades) — confirmed.
Test-touch note (rule 2): a TRUE Gate-1 promotion run must be out-of-sample (is_out_of_sample=True), i.e. the
held-out Test year. Per project convention Test has already been CONSUMED by other families (EXP-003 session,
EXP-008 watchman) but each NEW family gets its own one-touch; this cost-honesty re-run is not a new tuning family
and does not, by itself, justify spending a Test touch. Per the user's explicit instruction, Train+Val is
reported as the gate PICTURE and the definitive Test-year gate run is flagged as needing explicit user sign-off.

## EXP-020 2026-07-24 — Trend-REGIME filter (Option 2; a genuinely NEW pre-registered strategy hypothesis)
Status: PRE-REGISTERED (running) — Train (y1/y2/y3) tune + Val confirm ONLY; Test NOT touched regardless of result.
Hypothesis / mechanism: the cross-project finding + this repo's own per-year record (y2 2022-23 sideways PF 0.975,
net −$406, LOSING; y3 2023-24 trending PF 1.224, +$3,321) say the edge is trend/bull-regime beta, near-absent in
sideways/counter-trend regimes. A trend-ALIGNMENT gate that stands aside when price is not trending in the signal's
direction should cut the sideways-regime losers (improve y2) without gutting the trending edge (preserve y3).
Exact rule (pre-registered, causal, existing indicators only — features/indicators.ema): at the signalling closed
bar i, ALLOW a Council BUY only if close[i] > EMA(period)[i] AND EMA[i] > EMA[i−k] (rising); ALLOW a SELL only if
close[i] < EMA[i] AND EMA[i] < EMA[i−k] (falling); otherwise SKIP the signal. Applied at the accept step (before
Shield); everything else unchanged. This is NOT a re-tune of any existing threshold (bull/bear 70, tp 2.0, etc.).
Pre-registered grid (one coupled pair = the classifier): period ∈ {100,200,300} × k ∈ {12,24,48} = 9 configs
(< 20, multiple-testing OK). PRIMARY candidate declared up front: period=200, k=24 (≈8 trading-day trend, 1-day
slope). The other 8 exist ONLY for the plateau/robustness check, not to shop for a winner.
Pass/fail bar (pre-registered BEFORE seeing results; ALL required for a RECOMMEND):
 (a) y2 2022-23 (the losing sideways year): PF ≥ 1.00 (from 0.975) OR net loss cut ≥ 50% (from −$406) — i.e. it
     MUST measurably reduce losses in the historically-losing period, the whole point of the filter;
 (b) y3 2023-24 (trending): PF stays ≥ 1.04 (within 15% of baseline 1.224) AND net ≥ ~$1,650 (≥ ~50% of the
     +$3,321 baseline retained) — the filter must NOT destroy the trending-period edge;
 (c) y1 + Val: aggregate expectancy stays positive (PF ≥ 1.0), and the sample stays ≥ 100 trades/window (rule 6 —
     a filter that starves trade count below the floor is "insufficient sample", not a win);
 (d) Plateau (rule 5): the chosen (period,k) must have grid-neighbors behaving similarly — no lucky isolated spike.
Decision policy: even if ALL of (a)-(d) clear on Train/Val, do NOT auto-adopt into config/base.yaml and do NOT
touch any promotion gate (user instruction 3) — recommend back to the user with full evidence, same escalation
pattern as every gate-adjacent decision in this project. Test year stays pristine for a future user-authorised
confirmation. Harness: experiments/exp019_020_swap_regime_harness.py (regime gate OFF ⇒ byte-identical to the
engine; fidelity-checked). Cumulative regime-filter family multiple-testing count: 9 / 9 (first-ever this family).

### EXP-019 + EXP-020 RESULTS (run 2026-07-24) — raw output experiments/exp019_020_out.txt
FIDELITY: regime-OFF + swap-OFF re-sim == real run_backtest on y1, 265 trades byte-for-byte (identical=true). And
swap-OFF per-year PF/net reproduce EXP-017's baseline EXACTLY (y1 1.033/+502, y2 0.975/−406, y3 1.224/+3321) —
harness trusted. ($10k equity, commission $0 IC Markets Standard.) trainval (23k-bar) single-run was NOT completed
(the council precompute is ~O(n²) per-bar and impractically slow at 23k bars); EXP-018's already-recorded Train+Val
single-run numbers (n=1006, $50k) are used for the aggregate gate picture and are consistent with these per-year runs.

#### EXP-019 (Option 1 — swap ON vs OFF, NO behavior change), PF | net$ | avgR | DD% | PF_ex5:
              swap OFF                              swap ON
 y1   1.033 | +502  | 0.027 | 14.9 | 0.96      1.016 | +232  | 0.016 | 15.6 | 0.95
 y2   0.975 | −406  | −0.016| 28.9 | 0.90      0.954 | −733  | −0.028| 30.5 | 0.88
 y3   1.224 | +3321 | 0.139 | 14.5 | 1.13      1.212 | +3122 | 0.131 | 14.5 | 1.12
 val  1.078 | +1158 | 0.050 | 10.3 | 0.999     1.050 | +715  | 0.037 | 10.8 | 0.97
 Train+Val (EXP-018 recorded, n=1006, $50k):
      1.072 | +25,303 | 0.0498 | 30.4          1.053 | +17,885 | 0.0393 | 31.8   (Δ PF −0.019, avgR −21%, DD +1.4pp)
GATE-1 PICTURE on Train+Val (run_auditor promotion --gate backtest thresholds; PF≥1.3, DD≤15%, trades≥200,
PF_ex5>1.0): swap OFF → trades 1006 PASS, PF 1.072 FAIL, DD 30.4% FAIL, PF_ex5<1.0 FAIL ⇒ GATE FAILS.
swap ON → PF 1.053 FAIL, DD 31.8% FAIL, PF_ex5<1.0 FAIL, trades PASS ⇒ GATE FAILS (worse).
VERDICT EXP-019: Swap ON does NOT change the Gate-1 pass/fail — the gate ALREADY fails on Train+Val without swap
(PF 1.07 vs 1.3 required; DD ~30% vs 15%; PF_ex5 already <1.0). Swap makes the shortfall WIDER (−0.019 PF, −21%
of expectancy, +1.4pp DD), reproducing EXP-018's materiality finding on a fresh full-pipeline per-year run and
never flipping a year on its own but deepening y2's loss and eroding the Val edge. Cost-honesty CONFIRMED and now
routinely runnable via the harness; no behavior change, nothing to adopt into config. NOTE (Test-touch, rule 2):
a TRUE Gate-1 must be out-of-sample (the held-out Test year 2025-07-21→2026-07-21). Not run — flagged to user for
explicit sign-off. Even so, the prior Test-year figure on record (PF ~1.28, gold-bull year) is ALSO < 1.3 and a
swap haircut would push it further below, so a Test run is very unlikely to reverse the Train+Val FAIL.

#### EXP-020 (Option 2 — trend-regime filter), swap OFF, PF | net$  (baseline = regime OFF):
 config       y1 (2021-22)      y2 (2022-23)      y3 (2023-24)      VAL (deciding)
 baseline     1.033 | +502      0.975 | −406      1.224 | +3321     1.078 | +1158
 p100_k12     0.973 | −369      0.990 | −133      1.199 | +2755     1.120 | +1567
 p100_k24     0.903 | −1230     1.050 | +693      1.279 | +3676     1.034 | +409
 p100_k48     0.957 | −461      1.088 | +1109     1.252 | +3070     1.076 | +869
 p200_k12     0.989 | −136      1.080 | +1078     1.269 | +3460     1.104 | +1301
 p200_k24 *   1.084 | +996      1.142 | +1896     1.346 | +4100     1.050 | +602
 p200_k48     1.064 | +667      1.123 | +1556     1.278 | +3098     0.983 | −203
 p300_k12     1.002 | +24       1.041 | +501      1.303 | +3524     1.056 | +651
 p300_k24     0.958 | −460      1.105 | +1278     1.304 | +3505     1.026 | +269
 p300_k48     0.927 | −755      1.092 | +1080     1.276 | +2828     1.019 | +212
 (* = pre-declared PRIMARY. All trade counts 174–252/window, ≥100 floor OK.)

TRAIN story (encouraging, hypothesis DIRECTIONALLY REAL): nearly every config RESCUES the losing sideways year y2
(0.975→1.04–1.14, all positive) AND PRESERVES/improves the trending year y3 (1.20–1.35 vs 1.224). The primary
p200_k24 improves ALL THREE Train years (y1 +0.051 PF, y2 +0.167, y3 +0.122) — rare in this project. On Train the
filter genuinely does what it was designed to do.
VALIDATION story (the deciding window — REJECTS it): the primary p200_k24 falls to PF 1.050, BELOW the no-filter
baseline (1.078), and its direct grid-neighbor p200_k48 LOSES money (0.983 / −$203). The Val-best cell is p100_k12
(1.120) — but that cell is a Train UNDERPERFORMER (y1 0.973, y3 1.199, both below baseline), i.e. choosing it would
be fishing the Val set. The Train ranking and the Val ranking INVERT (Train-best ≠ Val-best) — the textbook overfit
signature this split exists to catch (same failure mode as EXP-001 C4, EXP-016). No config convincingly beats
baseline on BOTH splits; the most cross-consistent (p200_k12: +0.026 PF on Val) is inside the multiple-testing
noise band (rule 7: 9 configs, √ln9≈1.48; a ~0.03 PF gap on ~200 trades is ≲1 SE).
Robustness: neighborhood/plateau ✗ (primary's neighbor p200_k48 loses on Val; response surface inverts Train→Val),
per-year ✓ on Train only / ✗ out-of-sample (Val underperforms baseline), top-5 n.a. (rejected before Test), walk-
forward n.a.
Honest note on my own pre-registration: pass-bar (c) was under-strict — it required only Val PF ≥ 1.0, not Val >
baseline. Under that literal (too-weak) letter the primary squeaks by; under the STANDING project discipline (a
change must beat the no-filter baseline on the deciding Validation window, and sit on a plateau there) it does NOT.
The standing rules govern; a weak pre-registration cannot license adopting a candidate that is worse than baseline OOS.

### VERDICT EXP-020 — REJECT for adoption (config/base.yaml UNCHANGED; no gate touched; Test NOT touched)
The trend-regime hypothesis is the most promising regime signal this project has produced — on Train it reliably
converts the sideways-year loss to a profit and keeps the trending-year edge, across nearly the whole grid, which
is qualitatively different from a pure curve-fit. BUT it does not survive out-of-sample: the Train-optimal
parameterisation underperforms the no-filter baseline on Validation and the parameter ranking inverts across the
split. Per plateau-beats-peak (rule 5) and multiple-testing honesty (rule 7): no parameterisation earns a live
recommendation now. Do NOT adopt. RECOMMENDED (user decides): keep the idea alive and test it as its own pre-
registered experiment on the cross-project pre-2021 (2009-2019) different-regime data — a genuine out-of-regime OOS
— exactly EXP-018's next-step (ii); only if it beats baseline THERE should a Test-year confirmation be spent.
Test set (2025-07-21→2026-07-21) left UNTOUCHED — this new family's one-touch budget is UNSPENT (nothing cleared
Val, so nothing earned a Test confirmation). Auditor gate thresholds NOT touched (rule 8).

## EXP-021 2026-07-25 — Force-close before weekend vs hold-over-weekend (SL-only) (NEW family "weekend-gap exit")
Status: PRE-REGISTERED (before results) — Train (y1/y2/y3) + Val ONLY; Test (2025-07-21→2026-07-21) NOT touched regardless of result.
Trigger: user question — is it better to (A, CURRENT) hold open positions across the Sat/Sun market closure relying only
on the broker-side SL, or (B) force-close every open position before Friday's weekly close to avoid weekend gap risk?
Real motivating risk (not hypothetical): a broker SL is a LIMIT on the intended stop PRICE; across a weekend gap the
Monday-reopen can jump past the SL and the stop fills at the next available price, so realized loss can exceed intended
risk. Live context (NOT weighted): one XAUUSD SELL 0.01 lot is currently held into this weekend under Option A.

MECHANISM I AM COMPARING AGAINST (read council/risk_voice.py + backtest/engine.py first, confirmed):
- `friday_close_hour` (=20, server time) in risk_voice.py TODAY only vetoes NEW entries on Friday hour>=20 (condition 5).
  It does NOT close any already-open position. There is NO force-close-before-weekend mechanism anywhere in the codebase.
- backtest/engine.py ALREADY models the weekend gap faithfully IF the data has gap bars: SL fills at its nominal price
  UNLESS the bar's OPEN has gapped past it, in which case it fills at that bar's actual (worse) OPEN — engine.py module
  docstring "including weekend gap bars". So Option A's gap tail is MEASURABLE in-sample.
- DATA CHECK (instrument context, not a strategy outcome): XAUUSD_H1.csv has NO Sat/Sun bars; Friday runs to hour 23,
  Monday resumes ~hour 0-1 => a genuine weekend gap. 261 weekend boundaries; abs Monday open-gap: median $1.56, p90
  $13.35, p99 $52.44, max $75.46 — vs intraweek hourly abs open-move median $0.01 / p99 $0.40. Weekend gaps are ~1-2
  orders of magnitude larger than intraweek bar-to-bar moves: the tail mechanism is real and present in this data.

Option A (baseline) = current live adopted config, engine-exact, no forced weekend close (be/trail OFF per EXP-008,
structure+time ON, tp 2.0, pivot 3, all-24h Risk Voice, Shield cooldown, min-lot cap 1.5, risk 1.0%, commission $0
IC Markets Standard, $10k equity — same baseline as EXP-017/019 for direct per-year comparability).
Option B (hypothesis) = identical, PLUS: while a position is open, at the first bar that is Friday AND server hour >=
`friday_close_hour`, close it at that bar's CLOSE (same "close at bar close" convention Watchman CLOSE uses), reason
"weekend_close". Precedence: the existing SL/TP `check_exit` is still checked FIRST (an intra-week SL/TP that legitimately
fires that same bar still wins); the weekend-close is checked next, PRE-EMPTING the Watchman for that bar (irrelevant —
closing anyway). Re-entry: risk_voice condition 5 ALREADY vetoes new Friday hour>=20 entries, so no same-Friday re-entry;
Monday signals proceed normally (fresh entry, re-paying entry cost) — this is the symmetric reuse of friday_close_hour the
user asked for. Primary cutoff = 20 (the existing config value); NO config/base.yaml change either way.

This is a RISK-REDUCTION hypothesis, NOT an expectancy-improvement one: Option B spends expectancy (closes some winners
early on Friday + re-pays entry cost Monday) to BUY weekend-gap tail insurance. The bar is therefore asymmetric.

Pre-registered metric & pass/fail bar (set BEFORE seeing results; ALL required to RECOMMEND Option B):
 (a) TAIL REDUCTION IS REAL & MATERIAL (the whole point): on Train AND Val, Option B measurably reduces the worst single-
     trade net loss and the worst-10% tail-mean vs Option A. If it does not reduce the tail, REJECT outright.
 (b) THE TAIL BEING CUT IS ACTUALLY A WEEKEND-GAP TAIL: Option A's worst losses must demonstrably ALIGN with weekend-gap
     fills (exit bar follows a >30h data gap AND the SL filled at a gapped open, worse than nominal). If Option A's worst
     losses are NOT weekend gaps, the mechanism is absent in-sample => "no measurable benefit", REJECT.
 (c) EXPECTANCY COST IS ACCEPTABLE: Option B must not gut the edge to buy the insurance — per-year PF stays within ~15%
     of Option A and remains directionally intact; Val net stays positive if Option A's is.
 (d) SAMPLE DISCIPLINE (rule 6): trade count stays >=100/window; if forcing weekend closes materially cuts the sample,
     report it and do NOT extrapolate.
 (e) PLATEAU / NOT-A-KNIFE-EDGE (rule 5): cutoff-hour sensitivity friday_close_hour ∈ {18, 20, 22} must behave
     consistently (the tail-reduction/expectancy-cost trade-off must not flip sign on a 2-hour change). Primary = 20.
Configs evaluated (this exp / cumulative for this family): 4 (OptionA + OptionB@{18,20,22}) / 4 (first-ever this family).
Decision policy: this is an EXIT-RULE / strategy change — even if all (a)-(e) clear, do NOT auto-adopt into
config/base.yaml, do NOT add code to src/, do NOT touch any promotion gate (rule 8). Escalate to the user with full
evidence, same pattern as every gate-adjacent decision here. Test year stays pristine for a possible future user-
authorised confirmation. Harness: experiments/exp021_weekend_close_harness.py (VERBATIM run_backtest loop; weekend-close
DISABLED => byte-identical to the engine, fidelity-checked on y1 before any candidate number is trusted).

### EXP-021 RESULTS (run 2026-07-25) — VERDICT: REJECT Option B for adoption (Option A / hold-over-weekend STANDS)
Raw output: experiments/exp021_out.txt. Harness: experiments/exp021_weekend_close_harness.py.
FIDELITY: weekend-close DISABLED + swap OFF re-sim == real run_backtest on y1, 265 trades byte-for-byte (identical=true).
Per-year swap-OFF Option-A numbers reproduce EXP-017/019 baseline EXACTLY (y1 1.033/+502, y2 0.975/−406, y3 1.224/+3321,
val 1.078/+1158) — harness trusted. $10k equity, commission $0 IC Markets Standard, swap OFF (parity with recorded baseline).

PF | net$ | worst-single | worst-10%-mean | #gapped-SL-fills(Option A only) | #weekend-closes(B):
              Option A (hold)                      Option B @ cutoff 20 (primary)
 y1   1.033 | +502  | −157.49 | −109.68           1.033 | +493  | −157.49 | −107.02   (A: 2 gapped-SL fills, net −221.64)
 y2   0.975 | −406  | −129.10 | −121.86           0.981 | −293  | −125.88 | −120.28   (A: 0 gapped-SL fills)
 y3   1.224 | +3321 | −142.70 | −135.73           1.229 | +3483 | −141.00 | −135.16   (A: 0 gapped-SL fills)
 val  1.078 | +1158 | −132.06 | −118.25           1.087 | +1356 | −162.93 | −124.21   (A: 1 gapped-SL fill = the −132.06 worst)
 cutoff plateau (Option B, net$): y1 [18→184, 20→493, 22→418]; y2 [−372,−293,−266]; y3 [+3251,+3483,+3343]; val [+1557,+1356,+1419]

THE DECIDING FINDING — the motivating weekend-gap tail BARELY EXISTS in-sample:
- Across all ~1,000 Train+Val trades, only 4 trades EVER had an SL that filled through a gapped weekend open (y1=2, y2=0,
  y3=0, val=1). The engine DOES model this (SL fills at the worse gapped Monday open — fidelity-confirmed on real gap bars),
  so the near-absence is a real property of the strategy on this data, not a modelling gap: XAUUSD H1 stop distances
  (~0.8–2.5×ATR) usually exceed the typical weekend gap (median $1.56), the single-position engine is often flat over the
  weekend anyway, and most Monday-gap-bar exits (n_gap_exit 11–16/yr) are favorable or don't breach the stop.
- In 3 of the 4 windows (y1/y2/y3) the WORST single-trade loss is an ordinary intra-week 1R SL hit and is UNCHANGED by
  Option B (−157.49, −129→−126, −142.7→−141). Only in val is the single worst loss a weekend gap (−132.06) — and there,
  removing it made the tail WORSE, not better (see below). So the tail Option B is designed to cut is not, in fact, the tail.

Robustness vs pre-registered bars (ALL required; the hypothesis fails the two that matter most):
 (a) TAIL REDUCTION real & material — ✗ FAIL. On the DECIDING Validation window Option B makes the tail WORSE: worst single
     loss −132.06 → −162.93, worst-10% mean −118.25 → −124.21 (consistent across cutoffs 18/20/22, all −162.93 worst). On
     y1/y2/y3 the worst loss is essentially unchanged (±$3). Net across the board: no material tail reduction anywhere; a
     tail INCREASE on Validation. Cause: force-closing Friday reshuffles which trades the single-position engine takes next
     (fresh Monday re-entries), and that reshuffle introduced a −162.93 intra-week loser bigger than the gap it removed.
 (b) TAIL being cut is a WEEKEND-GAP tail — ✗ FAIL. 4 gapped-SL fills in ~1,000 trades; the worst losses are intra-week SL
     hits, not gaps, in 3 of 4 windows. The mechanism the user is worried about is real but almost never fires in this
     strategy/data, so Option B removes almost no gap risk while restructuring the whole trade sequence.
 (c) EXPECTANCY COST acceptable — ✓ at cutoff 20/22 (mixed: net roughly flat-to-slightly-BETTER in y2/y3/val, slightly
     worse in y1), ✗ at cutoff 18 (y1 +502 → +184). NOTE: any net IMPROVEMENT here is NOT from the hypothesized gap-
     insurance mechanism (4 trades) — it is downstream trade-reshuffling, the same non-causal single-position-engine
     artifact EXP-017/EXP-020 flagged. It cannot justify the change, and its sign is not stable.
 (d) SAMPLE discipline — ✓. All windows 240–288 trades (≥100). Counts RISE under B (extra Monday re-entries), not fall.
 (e) PLATEAU / not-a-knife-edge — ✗ FAIL. cutoff 18 vs 20 vs 22 give materially different net$ and the best cutoff flips by
     window (y1 favours 20, val favours 18, y3 favours 20) — the classic reshuffling-noise signature, no stable optimum.
 top-5 / walk-forward: n.a. (rejected before any Test touch).

Decision & rationale: REJECT Option B for adoption. Keep Option A (hold over weekend, broker-SL-only) as the CURRENT
behavior — config/base.yaml UNCHANGED, no src/ code added, no promotion gate touched (rule 8), Test year (2025-07-21→
2026-07-21) NOT touched (this family's one-touch budget UNSPENT — nothing cleared the pre-registered bar, so nothing earned
a Test confirmation). The in-sample evidence does not support force-closing: the weekend-gap tail it targets is nearly
absent (4/1000 trades), it delivers NO tail reduction (a tail INCREASE on Validation), and its only net-positive readings
are non-causal reshuffling noise that fails the cutoff-hour plateau check.

HONEST CAVEAT ESCALATED TO USER (this is a risk-appetite decision, not purely a backtest-expectancy one): the backtest can
only price the weekend gaps that OCCURRED in 2021–2026 (empirical abs Monday open-gap: median $1.56, p99 $52.44, max
$75.46). It CANNOT price a once-a-decade catastrophic weekend gap (geopolitical shock over a closed market), and the engine
fills a gapped SL at the Monday-open — likely OPTIMISTIC vs a real thin-Sunday-reopen fill that could slip further. So
force-closing before the weekend is a legitimate INSURANCE choice against an un-sampled fat tail — but it must be justified
as insurance the user chooses to pay for (a small, real expectancy/robustness cost + trade churn), NOT as a backtest-
supported improvement, because the backtest shows no measurable benefit and it is not free. Recommendation: do NOT adopt as
an edge; if the user wants the hard weekend-gap cap for peace-of-mind/tail-insurance reasons, that is a defensible manual
risk policy to enable deliberately (best implemented as a real Watchman/Risk-Voice exit, its own pre-registered change),
with eyes open that it slightly dilutes expectancy and adds Friday-close/Monday-reopen churn for a risk that has fired ~4
times in 1,000 trades on this data.

## EXP-022 2026-07-31 — `cfo.min_lot_risk_cap_pct` + the min-lot floor at ~$3,000 (NEW family "small-account min-lot floor"; supersedes the 2026-07-22 Stage-1 NOTE's numbers)
Status: REJECTED (no config change recommended) — for `min_lot_risk_cap_pct` the honest sub-verdict is INSUFFICIENT
(the parameter is structurally un-tunable on Train, see §3); every ALTERNATIVE tested is REJECTED on its own evidence.
Trigger: live shadow-loop session 2026-07-31 on the $2,940 demo — 11 of 19 evaluated H1 bars produced BUY signals that
passed Council + Risk Voice + Shield + confirmed-swing and were then refused at CFO sizing ("computed lot size below
broker minimum 0.01"), none rescued by `min_lot_risk_cap_pct: 1.5`. User asked to quantify the trade-off. NO live config
was changed by this experiment; no file under src/ or config/ was edited; no promotion/demotion gate touched (rule 8).

Harness: `experiments/exp022_minlot_harness.py` (committable, read-only). Reuses the 2026-07-22 NOTE's rescued-subset
attribution method verbatim (ordered sizing-call log zipped 1:1 against the trade list, lot-equality assert on every
pair) so results are comparable, with two deliberate upgrades: (i) the engine now supports `min_lot_risk_cap_pct`
NATIVELY, so the wrapper only OBSERVES (it calls the real sizer a second time forced to cap=None purely to LABEL a
rescue) and cannot alter behavior; (ii) COST MODEL COMPLETE per `scripts/run_backtest.py`'s own definition — slippage =
min-1-spread AND swap modeled (EXP-018 rates long -53.2 / short +36.8, 3x Wed), commission $0 (IC Markets Standard, the
real account) instead of the older $7 convention. Shield cooldown modeled (it was NOT in the 2026-07-22 harness).
FAST-PATH FIDELITY: the harness memoises full-series indicator/swing results to serve prefixes (exact, not approximate:
`ema`/`rsi`/`atr` are causal `ewm(adjust=False)` from bar 0; a fractal pivot at i depends only on i+-p and
`_confirmed_swing_indices` already restricts to i <= as_of_index - p). `--mode fidelity` ran a 3,000-bar window with and
without the shim: ALL metrics byte-identical (identical=True) before any candidate number below was trusted. OK

### 1. MAGNITUDE — the problem is REAL, STRUCTURAL, and NEW (it is the regime, not an outlier)
Regime table (`--mode regime`, trade-sequence-independent, $3,000 equity; 0.01 lot XAUUSD => $1 risk per $1 of stop, so
"affordable stop" in $ == risk% x equity):
```
window            med price  med ATR  ATR %px  med 0.8xATR  med 1.5xATR  med 2.5xATR | affordable@1.0%  @cap1.5%
y1 2021-22          1,814.8     4.20    0.231%       $3.36        $6.30       $10.50 |          $30.00     $45.00
y2 2022-23          1,838.8     4.39    0.239%       $3.51        $6.58       $10.96 |          $30.00     $45.00
y3 2023-24          2,033.4     4.36    0.215%       $3.49        $6.55       $10.91 |          $30.00     $45.00
y4 VAL 2024-25      2,751.9     7.26    0.264%       $5.81       $10.89       $18.16 |          $30.00     $45.00
y5 TEST 2025-26     4,288.9    17.86    0.417%      $14.29       $26.80       $44.66 |          $30.00     $45.00
```
TWO compounding drifts, not one: price level x2.4 (1,815 -> 4,289) AND volatility-as-a-fraction-of-price x1.8 (0.23% ->
0.42%). Dollar stop distance is the product => median mid-stop went $6.30 -> $26.80 (x4.3) while the affordable stop
stayed pinned at $30/$45 by the account size. In y5, 38.2% of bars have a typical (1.5xATR) stop already unaffordable at
risk 1.0%, and 12.0% unaffordable even at cap 1.5%. In y1-y3: 0.0%.
Backtest skip rates at the LIVE config (cap 1.5, risk 1.0%, $3,000, per-year windows, complete costs):
```
window            signals->sizing  sub-min%  rescued  STILL SKIPPED %
y1 2021-22                    266     0.38%        1            0.0%
y2 2022-23                    254     0.00%        0            0.0%
y3 2023-24                    233     0.00%        0            0.0%
y4 VAL 2024-25                263     7.98%       12            3.4%
y5 TEST 2025-26               589    78.95%      104           61.3%
```
**The live 11/19 = 58% refusal rate matches the y5 backtest's 61.3% almost exactly.** Today is NOT an outlier; it is the
2025-26 regime, and the mechanism (price level x ATR/price) does not mean-revert on any horizon this data can see.
CAVEAT on the denominator: "signals->sizing" is per-BAR, and a refused setup is re-offered on the next bar while the
engine is flat, so it is counted repeatedly (this is exactly why live logged 11 refusals from ~1 setup). Skip% is
therefore a refusal-EVENTS rate, NOT the fraction of distinct opportunities lost — see §2, which is the honest one.

### 2. WHAT THE FLOOR ACTUALLY COSTS — far less than it feels, because the engine simply takes the NEXT signal
Per-year, $3,000, risk 1.0%, complete costs. `wLoss%`/`w3%` = worst single loss / worst 3-consecutive-loss run, both as %
of equity AT THE TIME (equity compounds inside a window, so "% of starting equity" would misstate it):
```
y4 VAL 2024-25   cap:  None    1.25     1.5    1.75     2.0     2.5     3.0
  trades              257     258     254     254     255     255     255
  PF                1.038   1.044   1.096   1.096   1.081   1.081   1.081
  PF_ex_top5        0.954   0.947   0.987   0.987   0.974   0.974   0.974
  net$             +141.3  +166.6  +352.6  +351.2  +301.2  +301.2  +301.2
  maxDD%            11.78   14.02    9.99    9.99    9.85    9.85    9.85
  max planned risk%  1.00    1.24    1.45    1.50    1.78    1.78    1.78
  wLoss% / w3%     -1.10/-2.68  -1.28/-3.53  -1.42/-3.07  -1.46/-3.07  -1.78/-3.12   (2.5/3.0 identical to 2.0)
  rescued n|PF|net$    —   14|1.19|+49  12|2.18|+222  12|2.16|+221  15|1.41|+119     (2.5/3.0 identical to 2.0)

y5 TEST 2025-26  cap:  None    1.25     1.5    1.75     2.0     2.5     3.0     4.0   6.0/10.0(=unlimited)
  trades              209     222     228     234     240     239     239     239     239
  skip%              85.5*   79.7    61.3    45.1    28.6    20.6    13.7     1.2     0.0
  PF                1.071   1.032   1.190   1.197   1.191   1.158   1.118   1.124   1.110
  PF_ex_top5        0.974   0.937   1.067   1.067   1.046   1.007   0.958     —       —
  net$             +237.2  +127.1  +845.3  +952.3  +996.3  +838.5  +640.9  +690.5  +623.1
  maxDD%             9.52   11.02   12.39   16.03   14.64   15.43   17.89   20.96   22.56
  max planned risk%  1.00    1.25    1.49    1.75    2.00    2.47    2.94    3.93    4.85
  wLoss%            -1.77   -1.33   -1.94   -1.81   -1.97   -2.49   -2.92   -3.94   -4.84
  w3%               -3.62   -3.40   -4.20   -4.57   -5.52   -6.32   -6.96  -10.29  -12.01
  rescued n|PF|net$   —  83|0.68|-674 104|1.14|+375 119|1.03|+90 127|1.06|+235 134|1.04|+161 138|0.99|-59 137|1.01|+23 137|0.99|-44
  (* see §1's denominator caveat)
```
THE DECIDING FACT (Q1 "what fraction is lost / what does it cost"): **trade count barely moves.** y5 executes 209 trades
with the floor at its most restrictive (cap=None, ~85% refusal rate) vs 239 with the floor effectively removed (cap
unlimited) — a 14% difference, not the near-total paralysis the live log feels like. Cause: this is a
`max_positions_per_symbol: 1` engine; a refused signal leaves it FLAT, so it simply sizes the NEXT signal an hour later.
The floor DELAYS and RE-SELECTS entries; it does not switch trading off.
AND the re-selection is not harmful: taking EVERY refused signal (cap unlimited) yields a rescued set of 137 trades with
**PF 0.99 / net -$44 in isolation** — the wide-stop signals the floor currently discards have, as a group, NO measurable
edge in the current regime, while admitting them costs +10pp of max drawdown (12.4% -> 22.6%) and takes the worst
3-consecutive-loss run from -4.2% to -12.0% of equity. The marginal trades beyond cap 1.5 net **-$419 over ~33 trades**.

### 3. WHY NO NEW VALUE CAN BE ADOPTED FOR `min_lot_risk_cap_pct` (rule 6 + the split problem, stated plainly)
The parameter is **structurally un-tunable on Train**: y1/y2/y3 contain 1, 0 and 0 rescued trades respectively, because
the regime that activates the fallback (gold >$3,000, ATR >0.4% of price) does not exist before 2024. All information
about this parameter lives in y4 (VALIDATION, **12 rescued trades**) and y5 (the held-out TEST year, 104). n=12 on the
only legitimate deciding window is an order of magnitude below rule 6's 100-trade floor. Any value picked to fit y5 would
be fit on the Test set — refused. So:
- On the deciding window (y4/VAL) the CURRENT value 1.5 is already the joint best (PF 1.096, tied with 1.75) and sits on
  a plateau: its +-20% neighbours 1.25 (1.044, -4.7%) and 1.75 (1.096, +-0%) are both inside the ~15% band (rule 5). OK
- Every candidate ABOVE the plateau is worse on y4 (2.0/2.5/3.0 all PF 1.081) AND monotonically worse on tail risk.
- The status quo needs no new evidence; a CHANGE does, and there is none. Verdict: keep `min_lot_risk_cap_pct: 1.5`.
HONESTY UPDATE TO THE 2026-07-22 NOTE (it is quoted in `config/base.yaml` and `risk/sizing.py`'s docstring): its headline
"rescued subset PF 1.60" does NOT fully replicate. Re-running its own convention (full history compounding, $3,000,
commission $7, swap OFF) but WITH Shield modeled (which it predates) gives rescued PF **1.45** (53 trades, +$740, cap
1.5), and under the correct complete cost model in the current regime (y5, commission $0 + swap ON, per-year $3,000
anchor) the rescued subset is only **PF 1.14** (104 trades, +$375). Direction intact (rescued trades are NOT dead weight,
and cap 1.5 is still the peak of the cap sweep in both conventions), magnitude materially weaker. That comment/docstring
claim should be read as "modestly positive", not "PF 1.60".

### 4. ALTERNATIVES — all REJECTED on their own evidence
(a) **Tighter stops (`order.sl_max_atr` 2.5 -> 2.0/1.5/1.2/1.0)**, $3,000, cap 1.5. This is the one lever that genuinely
    fixes affordability (y5 sub-min 79% -> 13% at 1.2). It destroys the edge on Train:
```
  sl_max_atr      2.5     2.0     1.5     1.2     1.0
  y1 PF        1.0159  0.9215  0.8884  0.8977  0.8434
  y2 PF        0.9949  0.9592  0.9737  0.9625  0.8967
  y3 PF        1.2020  1.1018  1.0164  0.9509  0.9866
  y4 VAL PF    1.0961  1.1785  1.0084  1.0043  0.9814
  y5 PF        1.1903  1.2077  1.1355  1.2312  1.1216
```
    2.5 is the best value in ALL THREE Train years and tightening is monotonically destructive there (mechanism is causal
    and obvious: a stop placed to fit the ACCOUNT instead of the STRUCTURE sits inside noise and gets hit).
    `sl_max_atr=2.0` is better on y4 — but it is worse in all three Train years, i.e. the Train/Val ranking INVERTS, the
    exact overfit signature EXP-020 and EXP-001-C4 were rejected for. REJECT. (Spec bounds respected: 2.5 is the
    Risk-voice ceiling and everything tested was <= it.)
(b) **ATR volatility-halving (`risk/sizing.py`'s `current_atr`/`avg_atr_20d`/`volatility_multiplier_threshold`)** —
    CANNOT be tuned: it is **dead code today**. Verified by grep: there is no config key for it anywhere, and BOTH
    `backtest/engine.py` (BacktestConfig docstring: "left disabled ... both `None` at every `compute_lot_size` call")
    and `orchestrator/shadow_loop.py` (module docstring, Phase-3 simplification) pass `None` for both, deliberately.
    Tuning it is a no-op until someone wires it. ANALYTIC finding (not measured — wiring it would be a src/ change, out
    of scope here): if it WERE enabled it would make this exact problem STRICTLY WORSE. It halves `risk_per_trade`
    precisely when `current_atr > 1.5 x avg_atr_20d` — the high-volatility regime causing the refusals — halving the
    risk-based lot and pushing MORE signals below `volume_min`; and because the `min_lot_risk_cap_pct` check uses FULL
    equity independent of the halving (by design, see the sizing docstring), those extra sub-minimum signals land in the
    fallback and get traded at 0.01 lot at up to the FULL 1.5% cap. Net effect: fewer trades at planned risk, more trades
    at capped-MAXIMUM risk. Do not enable it as a response to this problem.
(c) **Raise `cfo.risk_per_trade_pct` (1.0 -> 1.25/1.5/2.0)** — the non-surgical version of the same idea (it raises risk
    on EVERY trade, not just the marginal wide-stop ones). Rejected on both metrics: y4 VAL PF 1.096 (1.0%) -> 1.061
    (1.25%) -> 1.062 (1.5%) -> 1.038 (2.0%), i.e. the current value is the VAL optimum; and drawdown explodes on the
    losing Train year y2 (26% -> 35% -> 40% -> 52%). It also barely helps the stated problem: y5 skip% only falls
    61% -> 59% at risk 1.25%. Lowering to 0.75% is worse still (y1/y2/y3 all flip to PF < 1.0). REJECT; 1.0 stays.
(d) **Change nothing** — the measured baseline above. y5 at the live config: 228 trades, PF 1.190, PF_ex_top5 1.067, net
    +$845 on a $3,000 account, max DD 12.4%, worst single loss 1.94% of equity. The system is not, in aggregate, unable
    to trade in this regime — it is refusing the widest-stop subset, which is the subset with no measurable edge.
    RECOMMENDED.

### 5. RISK ANALYSIS (Q4) — why every "just raise the cap" option is worse than it looks on a $3,000 account
`min_lot_risk_cap_pct` is, by construction, the maximum single-trade risk the system will ever take (the measured
`max planned risk%` tracks the cap to within 0.06pp in every cell, so it binds exactly as designed). It must be read
against the CFO's own circuit breakers in `config/base.yaml` — NOT promotion gates, but the account's survival logic:
`daily_loss_limit_pct: 2.0`, `max_consecutive_losses: 3`, `max_drawdown_halt_pct: 8.0`.
```
  cap    max single loss   worst 3-loss run (y5, measured)   coherence with the CFO breakers
  1.5          1.5%                  -4.2%                   OK — one full stop stays under the 2% daily limit
  1.75         1.75%                 -4.6%                   OK, marginal
  2.0          2.0%                  -5.5%                   BREAKS — ONE losing trade trips the 2% daily halt
  2.5          2.5%                  -6.3%                   BREAKS daily limit; 3 losses ~79% of the 8% master halt
  3.0          3.0%                  -7.0%                   BREAKS daily limit; 3 losses ~87% of the 8% master halt
  unlimited    4.9%                 -12.0%                   BREAKS everything — 3 losses blow through the 8% halt
```
So `cap = 2.0` is not merely "a bit more risk": it is the value at which a single ordinary stop-out trips the daily-loss
circuit breaker, making the breaker fire on NORMAL operation rather than on abnormal loss. That is a structural argument
independent of any backtest number, and it puts a hard ceiling of **< 2.0** on this parameter unless the user separately
and deliberately decides to loosen `daily_loss_limit_pct` — which I am NOT proposing and did not test (loosening a risk
control so a tuning result fits is the failure mode rule 8 exists for; recorded here as a refusal).
Ruin arithmetic at cap 1.5 on $2,940: worst measured single loss 1.94% (~$57); worst measured 3-loss run 4.2% (~$123),
which is where `max_consecutive_losses: 3` would stop it; worst measured full streak 8 losses / -7.2% (~$212). At cap 3.0
the same streaks become -7.0% and -8.9%, i.e. the 8% master halt fires and the account goes idle.

### 6. ROBUSTNESS SUMMARY
neighborhood/plateau OK (cap 1.5 sits on a flat 1.5-1.75 shoulder on y4; no sharp peak) — but see §3: that plateau rests
on 12 trades. per-year FAIL / informative-only: the parameter has zero effect in y1-y3, so per-year consistency cannot be
established at all — this is the core reason no change is adoptable. top-5 mixed: y5 cap-1.5 PF_ex_top5 = 1.067 (passes),
y4 cap-1.5 = 0.987 (fails — but it fails at EVERY cap including None = 0.954, so it is a strategy-level property of y4 at
$3,000, not cap-attributable; flagged, not attributed). walk-forward n.a. (only 2 of 5 windows contain any rescued trade;
a rolling walk-forward would be 3 empty windows and 2 near-identical ones). Multiple testing (rule 7): 7 cap values + 3
unlimited probes + 5 risk% + 5 sl_max = 20 configs this session, ~30 cumulative for this family including the 2026-07-22
NOTE's 10. At N~30 the required edge is ~1.8 SE; the best candidate's advantage over the status quo on the deciding
window is 0.000 PF (1.75 exactly ties 1.5). Nothing is remotely near the bar.

### 7. VERDICT — NO CONFIG CHANGE. `cfo.min_lot_risk_cap_pct` stays 1.5; `cfo.risk_per_trade_pct` stays 1.0;
`order.sl_max_atr` stays 2.5. `config/base.yaml` UNCHANGED, `src/` UNCHANGED, no promotion/demotion gate touched (rule
8). Test-year budget for this family: recorded as SPENT-FOR-MEASUREMENT-ONLY — y5 was run because the phenomenon under
study exists ONLY in y5 (same forced-window disclosure as the 2026-07-22 timeframe probe), and NO candidate was selected
using it; selection was made on y4/VAL, where the status quo won outright.
The min-lot floor is a real and permanent consequence of $3,000 equity x XAUUSD's 0.01-lot contract at gold $4,100 — not
a mis-set parameter. The signals it discards have no measurable edge as a group, and every knob that would admit them
raises single-trade risk into conflict with the account's own circuit breakers.
ESCALATED TO USER (not config changes, and deliberately not decided here):
 (i) OPERATIONAL, not risk-related: the 11-refusals-from-~1-setup pattern is the SAME signal re-offered every hour. A
     dedupe/cooldown on the "below broker minimum" log + Telegram path (mirroring `shield.duplicate_signal_cooldown_hours`)
     would remove the alarming appearance without touching a single risk number. Recommended; needs its own change.
 (ii) The genuinely binding fact: at gold >$4,000, $3,000 equity at 1.0% risk buys a $30 stop while the strategy's own
     structural stops need $40-$60. Within the fixed ~$3,000 constraint the only honest options are the ones measured
     above; the constraint itself is the thing that would have to move, and that is the user's call, not a parameter's.
 (iii) BEFORE any future change to this parameter could be adoptable: either (a) the cross-project pre-2021 (2009-2019)
     data EXP-018/EXP-020 already flagged as the out-of-regime OOS set, at a gold price level where the fallback actually
     fires, or (b) >=100 rescued trades of paper/live evidence at the current config. Until one of those exists, this
     parameter cannot clear rule 6 on any window legitimately available for selection.

## EXP-023 2026-08-03 — News-protection min-lot fallback: CLOSE-ALL (A) vs LOCK-STOP (B, `lock_frac`) (NEW family "news-protection min-lot fallback"; sibling of EXP-022's "small-account min-lot floor")
Status: REJECTED (mode B rejected at every `lock_frac`; mode A stands; no config change) — see §5 below. The block
immediately following was written and committed as a PRE-REGISTRATION BEFORE any result was looked at; only the
`### EXP-023 RESULTS` section and this Status line were added afterwards.
Scope: Train (y1/y2/y3) + Val (y4) ONLY; Test year
(2025-07-22→2026-07-21) NOT touched regardless of outcome. NO change to `config/base.yaml` and NO change to anything
under `src/` will be made by this experiment whatever the verdict (adoption is a separate, escalated decision).

TRIGGER (live evidence, cited as the motivation — NOT as a statistical sample):
Paper trading on the ~$2,940 IC Markets demo, 2026-07-22 → 2026-08-03, journal snapshot
`trade_journal_paper_vps_latest.sqlite`, 11 closed trades. Verified directly from that DB (not from the report text):
4 trades have `exit_reason='news_protection'` with r_multiple **+0.658, +0.670, +0.513, +0.516** — i.e. 4 of the 5
winning trades (the 5th is +1.272, `reconciled_system_close`). Losers ran −0.943, −1.335, −0.215, −1.339, −1.002,
−0.290. TP (2R) was never reached once. Live avg win ≈ +0.73R vs avg loss ≈ −0.85R at 5/11 WR.
Mechanism, from the live shadow-loop log (2026-08-03 08:15:04) and confirmed in code
(`watchman/loop.py::_half_volume_rounded` + `_act_on_news_decision`): at 0.01 lot, half rounds to 0.00 < `volume_min`,
so `_half_volume_rounded` returns `None` and the CLOSE_HALF_AND_BREAKEVEN branch recurses into **CLOSE_ALL**. At the
min-lot sizing that is the norm on a $3k account (EXP-022 §1), the spec's "de-risk half, let the rest run" (Appendix A
§4.5) therefore degenerates into "full exit at ~0.5R" on every trigger, while TP sits 2R away. This is a structural
asymmetry specific to min-lot accounts, not a read on 11 trades.

HYPOTHESIS / MECHANISM UNDER TEST:
Mode A (CURRENT LIVE) = when news protection fires and half-volume < `volume_min`, close the WHOLE position at the
current price. Mode B (candidate) = instead keep the position open and TIGHTEN the stop to `entry ± lock_frac × R`
(profit direction), TP untouched, stop never loosened (idempotent — a re-trigger recomputes the same level, so there is
no ratchet and no second treatment). B is still strictly risk-reducing vs doing nothing (the position can no longer
lose), and it is the closest min-lot-feasible analogue of the spec's own intent (at `lock_frac=0.0` it IS the spec's
"ย้าย SL เป็น break-even" half of the rule, minus the impossible half-close). Question: does B beat A on expectancy of
the AFFECTED trades without an unacceptable tail (news-spike reversal blowing through the locked stop)?

### DESIGN DEVIATIONS FORCED BY CODE READING (declared here, before results, per the "adjust only if ill-posed" clause)
D1. **The backtest does NOT model news protection at all.** `backtest/engine.py`'s module docstring, line 49: "One
    Watchman sub-condition remains genuinely unmodeled: news protection ... for the same reason not attempted here."
    Verified by grep — `check_news_protection` is never imported by the engine. The project memory note "news
    protection is modeled in the backtest since Phase 9" is WRONG (Shield is; news protection is not). Consequence
    that must be stated loudly: **every baseline in this log (EXP-001..EXP-022) is a mode-C run** (no news protection),
    i.e. none of them describe what the live system actually does today. Mode A has never been backtested.
D2. **There is no historical high-impact-news calendar, so the TRIGGER TIME cannot be simulated faithfully.**
    `backtest/news_stub.NoHistoricalNewsDataProvider` returns `[]` always, deliberately; `MQL5CalendarProvider` only
    exports the terminal's forward-looking calendar; no calendar file exists on disk. The trigger must therefore be
    PROXIED, and the proxy is declared up front, not chosen after seeing results:
      * **P1 "always-eligible"** (primary): every bar is trigger-eligible, so protection fires at the FIRST touch of
        +`profit_threshold_r` (0.5R). This is exactly the behavior when the calendar is unavailable
        (`StubNewsCalendarProvider` → `None` → `news_protection.py`'s documented fail-safe TRIGGERS protection), and it
        is the MAXIMUM-exposure bound: the largest possible affected subset, hence the largest legitimate sample.
      * **P2 "US-macro hours"** (robustness only, never used for selection): trigger-eligible only on Mon–Fri server
        hours {14,15,16,20,21} — a coarse stand-in for the hours in which high-impact USD releases (XAUUSD maps to
        USD only, `risk_voice._SYMBOL_CURRENCIES`) and FOMC actually land. It is a PROXY, not a calendar; its only job
        is to answer "does the A-vs-B effect survive when restricted to genuinely news-adjacent, higher-volatility
        bars?", which is precisely where B's locked stop is most at risk.
    Live today most likely runs with a WORKING calendar (MQL5 provider), not the fail-safe: the +1.272R trade passed
    through 0.5R without protection firing, which the always-fail-safe regime could not produce. So P1 overstates
    trigger FREQUENCY vs today's live; it does not distort the conditional A-vs-B comparison, which is the deciding one.
D3. **Primary comparison is trade-matched and conditional, not portfolio-level.** Because this is a
    `max_positions_per_symbol: 1` engine, closing early under A changes which signal is taken next; EXP-017/020/021 all
    established that the resulting portfolio deltas are dominated by non-causal RESHUFFLING noise. So: (i) DECIDING
    metric = per-trade outcome on the AFFECTED SUBSET with the trade sequence held FIXED (same entries, same lots, same
    bars; only the management rule differs) — a genuine treatment effect; (ii) full-sequence portfolio runs are ALSO
    reported for PF/DD/net$, but explicitly as a contamination check, and can only VETO a candidate (materially worse),
    never carry it.

### HARNESS
`experiments/exp023_news_lock_harness.py` (committable, read-only w.r.t. `src/` and `config/`). It contains a VERBATIM
copy of `run_backtest`'s bar loop with the news mechanism inserted (EXP-021's pattern), and reuses EXP-022's validated
fast-path memoisation shim. Fidelity gate, run BEFORE any candidate number is trusted: with the news mechanism OFF the
copy must reproduce the real `backtest.engine.run_backtest` trade-for-trade, field-for-field (`--mode fidelity`).
Per-bar ordering convention (a real methodology decision, declared before results): within a bar, priority is
**SL/TP `check_exit` (unchanged engine convention, SL wins a double-touch) > news-protection intrabar trigger >
Watchman CLOSE at bar close**. Rationale: the live loop polls every ~5s, so a level touched intrabar is acted on
BEFORE that bar's close, which is when Watchman's closed-bar structure/time verdict can first change. Live's own
ordering (Watchman first, then news, news skipped if Watchman already closed) is preserved for everything that is
decided at the bar's close. Trigger price: bar OPEN if the position is already ≥0.5R at the open, else the exact
+0.5R level. Exits fill nominally (engine convention; symmetric between A and B, and B's locked stop is a real
broker-side stop that CAN gap through — gap-throughs are counted, not assumed away).
COST MODEL COMPLETE per `scripts/run_backtest.py`'s own definition: slippage = min-1-spread (`slippage_points=None`)
AND swap modelled (EXP-018 rates long −53.2 / short +36.8, 3× Wed); commission $0.00 (IC Markets Standard, the real
account). Account context per EXP-022: **$3,000 starting equity, per-year anchored**, `min_lot_risk_cap_pct: 1.5`,
`risk_per_trade_pct: 1.0`, all-24h session, be/trail OFF, tp 2.0, pivot 3 — the adopted live config, unchanged.

### GRID (one mechanism, one parameter — `lock_frac`)
| id | mode | lock_frac | note |
|----|------|-----------|------|
| A  | close_all | n.a. | BASELINE = current live behavior |
| B0 | lock_sl   | 0.0  | breakeven — the spec's own §4.5 wording |
| B2 | lock_sl   | 0.2  | |
| B3 | lock_sl   | 0.3  | **PRIMARY CANDIDATE, declared up front** |
| B5 | lock_sl   | 0.5  | locks the full trigger profit; can only exit ≥+0.5R or at TP |
| C  | none      | n.a. | REFERENCE BOUND ONLY (= what every prior experiment measured). NOT a candidate for adoption: removing news protection is the removal of a deliberate risk control, out of scope here. |

Configs evaluated (this exp / cumulative for this family): 6 per proxy × 2 proxies = 12 / 12 (first-ever this family).
At N=12 rule 7's multiple-testing inflation is modest but the required edge is still ~1.6 SE.

### METRIC THAT DECIDES + ACCEPTANCE CRITERION (all required to RECOMMEND B; set before any result)
 (a) TREATMENT EFFECT, conditional/no-reshuffle, on the AFFECTED SUBSET (trades that trigger under A), under P1:
     mean R(B3) > mean R(A) on Train (y1+y2+y3) AND on Val (y4), by more than 1.6 SE of the paired difference.
     Paired, per-trade — the same trades under both rules — so the SE is of the DIFFERENCE, not of two means.
 (b) TAIL: B must not buy its expectancy with a fat tail. Report, per window: worst single R under B, the count and
     size of exits BELOW the locked level (gap-throughs of the locked stop), and the fraction of affected trades where
     B ends WORSE than A. Fail if B's worst affected-trade R is materially negative (a locked stop should make a
     negative outcome impossible except through a gap) or if the gap-through rate is non-trivial.
 (c) PLATEAU (rule 5): `lock_frac` neighbours ±1 grid step of the winner must land within ~15% of it on Val. A single
     sharp `lock_frac` is noise → REJECT even if it is the sweep's best.
 (d) PORTFOLIO NON-DEGRADATION (veto-only): full-sequence PF and max DD under B3 within ~15% of A on Train and Val.
 (e) SAMPLE (rule 6): ≥100 affected trades per evaluation window; below that the window reports INSUFFICIENT and is
     not used for selection.
 (f) PER-YEAR CONSISTENCY: the treatment effect must have the same sign in y1, y2, y3 and y4 — an effect owed to one
     regime is rejected (EXP-020/EXP-022(a) precedent: a Train/Val ranking inversion is the overfit signature).
 (g) P2 ROBUSTNESS: the sign of the treatment effect must survive restriction to news-adjacent hours. If B's edge
     exists only in quiet hours, it is not an edge on the mechanism actually being changed.
Rule 8 note: nothing in this experiment touches a promotion/demotion gate or a circuit-breaker threshold, and no such
change will be proposed as a way to make any result pass.

### EXP-023 ADDENDUM (logged mid-experiment, BEFORE the deciding windows were read — full disclosure of when)
Timing, stated plainly: this was added AFTER the y1/P1 conditional output was printed and BEFORE any y2/y3/y4 number
existed. It is a MECHANISM-FIDELITY fix, not a metric change and not a hypothesis change — and it works AGAINST the
status quo (it removes an artificial penalty on the candidate), so disclosing it costs the candidate nothing and the
alternative (leaving a known-unfaithful simulation in place because it happened to favour the baseline) would be the
dishonest option.
WHAT: when a `lock_sl` trigger fires, is the freshly-locked stop live for the REMAINDER of the triggering bar, or only
from the next bar? The engine's documented rule ("the bar that produces a tighter stop can never itself be the bar that
gets stopped out by it") exists because Watchman's MODIFY_SL is decided at the bar's CLOSE, when the bar is already
over. The news trigger here is INTRABAR, so that rationale does not transfer: live places the broker-side stop within
~5s of the trigger, and a reversal later in the same hour would fill AT the locked level. The next-bar-only convention
instead lets the NEXT bar's open gap far below the lock — manufacturing losses (y1 showed worst affected-trade R of
−1.13 under a supposedly "locked" stop, 35 of 143 lock exits filling below the lock) that live would not suffer.
RESOLUTION: both conventions are now run for every `lock_frac`, reported side by side (`0.3` vs `0.3@samebar`), and
**B must beat A under BOTH to be recommended.** `@samebar` fills exactly at the locked level (the price was at ≥+0.5R
when the stop was placed, so the level is inside the bar's remaining range, not gapped) and keeps the engine's
pessimistic same-bar-touches-both rule (if the bar reaches the lock AND the take-profit, the stop wins). Genuine
overnight/weekend gaps on LATER bars still fill at the gapped open under both conventions, so real gap risk is not
assumed away. Configs evaluated rises from 12 to 20 for this family (4 lock_frac × 2 conventions × 2 proxies, + A + C).

### EXP-023 RESULTS (run 2026-08-03) — VERDICT: REJECT mode B at every `lock_frac` (0.0/0.2/0.3/0.5), under BOTH intrabar conventions and BOTH trigger proxies. Mode A (close-all) STANDS as the live behavior. NO config change, NO src/ change.
Raw output: `experiments/exp023_cond_out.txt` (conditional/deciding), `experiments/exp023_port_out.txt` (portfolio/veto-only).
Harness: `experiments/exp023_news_lock_harness.py`.

FIDELITY (both gates passed before any candidate number below was read):
 1. `--mode fidelity`, 4,000-bar window, 197 trades: the harness's copied bar loop with the news mechanism OFF is
    IDENTICAL to the real `backtest.engine.run_backtest`, field-for-field — both with and without EXP-022's fast-path
    shim (`copy_off_identical=True copy_off_fastpath_identical=True`).
 2. Conditional-replay self-check (asserted in code, every window): replaying each mode-C trade one-by-one reproduces
    the full-sequence mode-C trade list EXACTLY, so the counterfactual replays differ from the baseline only by the
    management rule under test.
 3. EXTERNAL cross-validation against a previously RECORDED number: mode C on y4/VAL at $3,000 gives
    **254 trades, PF 1.0961, net +$352.60, maxDD 9.99%** — EXP-022's own y4 cap-1.5 cell to the last digit
    (254 / 1.096 / +352.6 / 9.99). The whole stack (interpreter, pandas build, fast path, config, cost model)
    reproduces this log's history.

### 1. DECIDING EVIDENCE — conditional treatment effect on the AFFECTED subset (sequence held fixed)
Affected = trades that trigger news protection (reach +0.5R while eligible). Sample per window: **P1 162/152/149/154,
P2 124/116/116/124** — every window clears rule 6's 100-trade floor. avgR on the affected subset:
```
P1 (always-eligible)     tot  aff | C(none) A(close_all) | B0.0  B0.0@sb  B0.2  B0.2@sb  B0.3  B0.3@sb  B0.5  B0.5@sb
y1 2021-22               266  162 |  0.496    0.501      | 0.543  0.249  0.443   0.253  0.424   0.340  0.423   0.507
y2 2022-23               254  152 |  0.526    0.497      | 0.398  0.227  0.412   0.273  0.397   0.325  0.445   0.492
y3 2023-24               233  149 |  0.674    0.494      | 0.518  0.288  0.510   0.348  0.498   0.372  0.486   0.494
y4 VAL 2024-25           254  154 |  0.627    0.495      | 0.499  0.288  0.484   0.342  0.524   0.326  0.502   0.495
```
POOLED paired treatment effect **B − A** (same trades, so the SE is of the DIFFERENCE; pre-registered bar: > +1.6 SE):
```
P1        TRAIN (n=463)                 VAL (n=154)
B0.0      -0.010 +-0.041 (t -0.25)      +0.004 +-0.071 (t +0.06)
B0.0@sb   -0.243 +-0.031 (t -7.97)      -0.207 +-0.057 (t -3.65)
B0.2      -0.043 +-0.032 (t -1.34)      -0.011 +-0.058 (t -0.20)
B0.2@sb   -0.207 +-0.018 (t -11.34)     -0.153 +-0.040 (t -3.86)
B0.3      -0.059 +-0.027 (t -2.14)      +0.029 +-0.054 (t +0.54)     <- PRIMARY CANDIDATE
B0.3@sb   -0.152 +-0.013 (t -11.85)     -0.169 +-0.018 (t -9.16)     <- PRIMARY, live-faithful convention
B0.5      -0.047 +-0.021 (t -2.28)      +0.007 +-0.041 (t +0.17)
B0.5@sb   +0.001 +-0.003 (t +0.16)      +0.000 +-0.000 (t  0.00)     <- degenerate: identical to A
P2        TRAIN (n=356)                 VAL (n=124)
B0.3      -0.010 +-0.036 (t -0.26)      +0.069 +-0.063 (t +1.08)
B0.3@sb   -0.097 +-0.026 (t -3.67)      -0.091 +-0.040 (t -2.29)
```
**Not one of the 8 B variants clears the pre-registered bar on Train AND Val. The primary candidate `lock_frac=0.3` is
significantly WORSE than A on Train (t −2.14) and indistinguishable on Val (t +0.54) under the generous convention, and
overwhelmingly worse under the live-faithful one (t −11.9 Train, −9.2 Val).**

### 2. WHY B FAILS — the mechanism, not the statistics
Exit mix of the affected subset on y4/VAL (n=154), the cleanest statement of the whole experiment:
```
  C (no protection):   75 take_profit (48.7%), 49 stop_loss, 27 time_stop  -> avgR 0.627, medR 0.313, worstR -1.346
  A (close all):      154 news_protection                                  -> avgR 0.495, medR 0.500, worstR +0.264
  B0.3 (next-bar):    127 lock-stop, 26 take_profit (16.9%), 1 time_stop   -> avgR 0.524, medR 0.300, worstR -0.685
  B0.3@samebar:       151 lock-stop,  3 take_profit (1.9%)                 -> avgR 0.326, medR 0.300, worstR +0.064
```
A gold H1 bar that pokes +0.5R retraces to within 0.3R of entry, IN THE SAME HOUR, in the overwhelming majority of
cases. So a stop parked at +0.3R is not "letting the rest run" — it is a near-certain scratch: it converts the 49%
of affected trades that would otherwise have reached the 2R take-profit into 1.9% (live-faithful) or 16.9% (generous).
B is therefore strictly worse than BOTH alternatives: worse than A (which at least banks the full +0.5R with
certainty) and worse than C (which keeps the real 2R upside). The hypothesis's premise — "preserve the upside to TP" —
is arithmetically incompatible with a stop placed inside the instrument's own hourly noise amplitude. This is the same
causal finding EXP-008 already established for `breakeven_at_r`/`trail_start_r` ("the breakeven-then-trail mechanism
engages well before trades reach the 2R target, cutting winners short") — the news lock is that mechanism again, just
triggered by a different clock.
Secondary but decisive on its own: the "locked" stop is NOT a floor. Under the engine-consistent next-bar convention
20-29% of lock exits (35/143, 43/136, 30/127, 34/127) fill BELOW the lock, worst single affected trade −0.53R to
−1.13R, versus A's guaranteed floor of +0.26R..+0.33R. Under the live-faithful same-bar convention that tail closes
(worst +0.06R..+0.13R) but only by paying it back in the mean.

### 3. PORTFOLIO (full-sequence; pre-registered as VETO-ONLY evidence, never selection evidence)
$3,000/window, complete cost model. PF | net$ | maxDD%:
```
P1                y1                    y2                     y3                    y4 VAL
C_none      1.016 |   +64 | 14.5   0.995 |   -22 | 26.1   1.202 |  +773 | 12.3   1.096 |  +353 | 10.0
A_close_all 1.004 |   +16 | 17.1   0.964 |  -175 | 20.0   1.076 |  +350 | 15.8   0.980 |   -86 | 12.5
B_lock_0.3  0.882 |  -433 | 22.2   0.900 |  -386 | 18.8   1.068 |  +275 | 19.3   1.056 |  +209 |  8.8
B_lock_0.3@sb 0.648| -1163 | 40.0   0.694 | -1068 | 36.8   0.777 |  -813 | 36.1   0.696 | -1034 | 38.6
P2 (news-hours)
A_close_all 1.030 |  +127 | 12.9   0.897 |  -456 | 23.8   1.115 |  +497 | 11.2   1.010 |   +39 | 14.0
B_lock_0.3  0.935 |  -241 | 14.0   0.955 |  -188 | 21.4   1.138 |  +541 | 13.6   1.086 |  +321 |  9.3
B_lock_0.3@sb 0.873|  -454 | 20.3   0.857 |  -556 | 29.0   0.978 |   -85 | 20.9   1.016 |   +58 | 11.2
```
Under the live-faithful convention B0.3 destroys the account in every window (PF 0.65-0.78, −$800 to −$1,160 on
$3,000, 36-40% drawdown) — an unambiguous veto. Under the generous convention it is worse than A on Train and better
on Val, i.e. exactly the Train/Val ranking INVERSION that EXP-020 and EXP-022(a) were rejected for. `B_lock_0.5@samebar`
reproduces A's portfolio numbers to 3 decimals in y3/y4 (identical trade counts, 486/554), confirming the degeneracy
noted above rather than offering a candidate.

### 4. ROBUSTNESS vs THE PRE-REGISTERED BARS (6 of 7 failed; only sample discipline passed)
 (a) treatment effect > +1.6 SE on Train AND Val — **FAIL** for all 8 variants (see §1).
 (b) tail — **FAIL**. Next-bar: 20-29% of lock exits fill below the lock, worst affected trade −0.53R..−1.13R vs A's
     +0.26R floor. Same-bar: tail clean, mean crushed. There is no convention under which B is both safe and better.
 (c) plateau — **N/A, and reported as such rather than as a pass**: on Val the four `lock_frac` values give
     +0.004/−0.011/+0.029/+0.007 (next-bar) — a flat field of noise around zero, not a plateau around a peak. There is
     no signal for a plateau to be the shape of.
 (d) portfolio non-degradation — **FAIL** (see §3).
 (e) sample >=100 per window — **PASS** (149-162 affected under P1, 116-124 under P2).
 (f) per-year sign consistency — **FAIL** under next-bar (B0.3: −0.077, −0.100, +0.004, +0.029 — sign flips);
     "consistent" under same-bar only in the sense of being consistently and heavily negative.
 (g) P2 news-hours robustness — **FAIL**: same picture, B0.3@sb t −3.67 Train / −2.29 Val.
 walk-forward: n.a. (nothing cleared the bar; a rolling confirmation would be confirming a rejected candidate).
 Multiple testing (rule 7): 20 configs evaluated this session (4 lock_frac × 2 conventions × 2 proxies, + A + C),
 20 cumulative for this family. At N=20 the required edge is ~1.7 SE; the best variant's edge over A is NEGATIVE.

### 5. VERDICT
**REJECT mode B (lock-the-stop) at every tested `lock_frac`.** `config/base.yaml` UNCHANGED (`watchman.news_close_mode`
stays `half`, `news_profit_threshold_r` stays 0.5, `news_window_minutes` stays 30), `src/` UNCHANGED — in particular
`watchman/loop.py`'s `_half_volume_rounded`-to-CLOSE_ALL fallback is NOT modified. No promotion/demotion gate or
circuit-breaker threshold was touched or proposed for change (rule 8). Test year (2025-07-22 → 2026-07-21) NOT touched;
this family's one-touch budget is UNSPENT, because nothing earned a Test confirmation.
The live observation that started this (4 of 5 winners banked at ~+0.5R while TP was never reached) is REAL and
correctly diagnosed — but the proposed remedy is worse than the disease. Trading a certain +0.5R for a stop parked
inside gold's own hourly noise does not "preserve the upside"; it deletes it (48.7% -> 1.9% TP rate on Val).

### 6. THE BIGGER FINDING — ESCALATED, NOT DECIDED HERE (this is the real result of EXP-023)
 (i) **BACKTEST/LIVE PARITY GAP, same class as EXP-018's swap gap.** News protection is not modeled in
     `backtest/engine.py` at all (D1). Every promotion-gate number this project has ever produced — including the
     Gate-1 arithmetic EXP-008 was adopted on — describes **mode C**, while the live system runs **mode A**. Measured
     cost of that gap on the affected subset (~60% of all trades under P1, ~46% under P2), pooled: A − C =
     **−0.066R ± 0.064 on Train, −0.132R ± 0.110 on Val** per affected trade (individually not significant, negative
     in 3 of 4 windows and largest in the two most recent). At portfolio level under P1 it turns Val from PF 1.096 /
     +$353 into PF 0.980 / −$86, and under P2 into PF 1.010 / +$39. The honest summary is: **news protection as
     currently implemented on a min-lot account is a materially expensive risk control, and the backtest has never
     charged the strategy for it.** This deserves its own pre-registered experiment (and, if the sign holds, an
     engine change so `cost_model_complete`-style honesty flags cover it) — it is NOT decided here, and it is NOT a
     licence to switch the control off: mode C is "no news protection at all", which is a deliberate risk-control
     removal and was carried here only as a reference bound.
 (ii) **The trigger frequency is un-measurable today and that is the biggest uncertainty in (i).** There is no
     historical calendar (D2); P1 (~60% of trades affected) is the always-fail-safe upper bound and P2 (~46%) is a
     coarse hour-mask, whereas today's live loop most likely runs a working MQL5 calendar with a far lower true rate.
     Before (i) can be decided, someone has to make the trigger rate measurable — e.g. by ACCUMULATING the MQL5
     exporter's forward-looking calendar to disk from now on (it is already running on the VPS), which after a few
     months yields a real, honest calendar for the paper window. Until then the magnitude in (i) is bracketed, not known.
 (iii) **A cheaper, mechanism-honest alternative exists and was NOT tested here** (out of scope: one experiment, one
     mechanism): if the goal is to stop a min-lot account from banking every winner at +0.5R, the surgical lever is
     the TRIGGER, not the fallback action — e.g. raising `news_profit_threshold_r`, or narrowing
     `news_window_minutes`, or making the min-lot fallback SKIP protection instead of closing all (the current code
     comment explicitly chose "close whole" over "skip" as the safe direction, and that choice is worth re-testing on
     evidence rather than on instinct). Each is a single pre-registered parameter with a live-observable meaning.
     Recommended order if the user wants to continue: (1) make the trigger measurable per (ii); (2) then test the
     threshold/window; (3) never the fallback action again — this experiment has answered that one.

HONEST CAVEAT ON EXIT COSTS (applies to both arms, does not change the verdict): per the engine's documented cost
convention, spread/slippage is baked into the ENTRY fill only; exits fill nominally. So mode A's market close at the
trigger price and mode B's stop-out at the locked level are both modelled ~1 spread too favourably (~$0.35 on a
$10-30 stop distance, i.e. ~0.01-0.03R). The effect is near-symmetric between the two arms and an order of magnitude
smaller than the measured B-vs-A gap (0.15-0.24R under the live-faithful convention), so it cannot flip the verdict.
It is the one place where this simulation is optimistic for BOTH modes relative to live.

## NOTE (not an EXP) 2026-08-03 — Historical news-calendar collection STARTED (EXP-024 prerequisite; follows EXP-023 §"escalation")
EXP-023's verdict escalated (did NOT decide) the bigger question: live runs news-protection mode A while every
backtest baseline EXP-001..023 is effectively mode C, and settling that (model it? redesign it? remove it?) needs the
REAL trigger rate — which nothing was recording, because `mql5/NewsCalendarExporter.mq5` only ever overwrites a
rolling [-2h, +48h] snapshot every 5 minutes. As of this note the snapshot is now folded into an append-only archive:

- Collector: `council/calendar_archive.py` (`archive_export_file()`), called once per heartbeat cycle by
  `scripts/run_health_check.py` (Task Scheduler, ~10 min) — passive observation only, no live-loop change, no restart
  needed, inherits the heartbeat's own monitoring. 10 unit tests (`tests/unit/council/test_calendar_archive.py`).
- Archive: `data/db/news_calendar_history.csv` (gitignored, VPS-local like the journal), append-only, deduped on
  (event_time, currency, importance, event_name); `first_seen_utc` is metadata (when the row first appeared in a
  snapshot), NOT comparable to `event_time` (naive server time, exporter's own convention).
- **Data start date for any future EXP-024: no archived calendar data exists before 2026-08-03.** The archive
  accumulates ALL currencies/importances (filtering to high-impact USD is the consumer's job, mirroring
  `MQL5CalendarProvider`), so trigger-window reconstruction is possible for any symbol later.
- EXP-024 itself is NOT pre-registered here — per EXP-023's escalation it should only be designed once enough weeks
  of real trigger data exist to say how often mode A actually fires (the piece both EXP-023 proxies had to assume).

## EXP-025 2026-08-04 — News-protection TRIGGER LEVEL `watchman.news_profit_threshold_r` (NEW family "news-protection trigger threshold"; sibling of EXP-023's "news-protection min-lot fallback")
Status: REJECTED (no T beats 0.5; `news_profit_threshold_r` stays 0.5, no config change) - see the RESULTS
section below. The block that follows this line was written and COMMITTED (d3089d5) BEFORE any deciding window
was run; only the `### EXP-025 RESULTS` section and this Status line were added afterwards.
ID NOTE: **EXP-024 is deliberately SKIPPED/RESERVED** for the future real-calendar experiment escalated by EXP-023
§6(ii) and set up by the 2026-08-03 NOTE (`data/db/news_calendar_history.csv` started accumulating on 2026-08-03; that
experiment cannot be designed until several weeks of real trigger data exist). This experiment therefore takes the next
free number, 025, rather than pre-empting 024.
Scope: Train (y1/y2/y3) + Val (y4) ONLY. **Test year (2025-07-22 → 2026-07-21) NOT touched under ANY outcome** — this
family's one-touch budget is unspent and stays unspent here. NO change to `config/base.yaml` and NO change to anything
under `src/` will be made by this experiment whatever the verdict.

### 0. WHY THIS PARAMETER, WHY NOW (direct descendant of EXP-023 §6(iii))
EXP-023 rejected changing the min-lot fallback ACTION (lock-the-stop) and closed that door permanently: a stop parked
inside gold's own hourly noise deletes the 2R upside (48.7% → 1.9% TP rate on Val) instead of preserving it. Its §6(iii)
named the remaining, cheaper lever explicitly: *"if the goal is to stop a min-lot account from banking every winner at
+0.5R, the surgical lever is the TRIGGER, not the fallback action — e.g. raising `news_profit_threshold_r`"*. That is
this experiment, and nothing else: same mechanism (mode A, close-all at trigger — the live behavior at min-lot per
`watchman/loop.py::_half_volume_rounded` → CLOSE_ALL), same engine, same account context, ONE parameter moved.
MECHANISM, stated so the causal role is explicit before any number is read: `news_profit_threshold_r` is the profit
gate in `watchman/news_protection.check_news_protection` — `profit_r = (price − entry)/initial_stop_distance` measured
against the ORIGINAL stop distance, and if `profit_r < threshold` the function short-circuits to NO_ACTION and the news
check is never even consulted. So the threshold decides **which trades ever become eligible for protection at all**, and
(at min-lot) therefore which trades get force-closed instead of being allowed to run to SL/TP/structure/time-stop.
Raising it is a strictly monotone shrink of the treated set: `affected(T) ⊆ affected(0.5)` for every `T ≥ 0.5`.
THE TRADE-OFF BEING PRICED, in one line: raising T re-exposes trades that sit between +0.5R and +T·R to their full
downside (they can now come back and hit the original SL for −1R) in exchange for letting the survivors reach 2R. This
is not obviously good — EXP-023 measured A − C = −0.066R (Train) / −0.132R (Val) per affected trade, i.e. protection is
expensive but the sign was NOT individually significant. The experiment must therefore be able to conclude "0.5 is
already on the plateau" and that is an entirely expected outcome.

### 1. INHERITED DESIGN DEVIATIONS (EXP-023 D1/D2/D3 — still true, still binding, restated so this block stands alone)
D1 (inherited). `backtest/engine.py` does NOT model news protection at all; every baseline in EXP-001..EXP-022 is a
   mode-C run. Mode A is simulated here by the same copied bar loop, not by production code.
D2 (inherited). There is no historical high-impact-news calendar, so the trigger TIME is proxied, declared up front:
   **P1 "always-eligible"** (PRIMARY, used for selection) = every bar trigger-eligible → protection fires at the first
   touch of +T·R; this is exactly the fail-safe regime `news_protection.py` documents when the calendar is unavailable,
   and it is the maximum-exposure bound (largest affected subset, largest legitimate sample).
   **P2 "US-macro hours"** (ROBUSTNESS ONLY, never selection) = Mon–Fri server hours {14,15,16,20,21}.
   P1 overstates trigger FREQUENCY vs a live terminal with a working MQL5 calendar; it does not distort the conditional
   comparison, which is the deciding one. Making the true rate measurable is EXP-024's job, not this one's.
D3 (inherited). Primary comparison is TRADE-MATCHED and CONDITIONAL, sequence held FIXED (same entries, same lots,
   same bars, only the management rule differs), because `max_positions_per_symbol: 1` means any earlier/later exit
   reshuffles which signal is taken next and that reshuffling noise dominates portfolio deltas (EXP-017/020/021).
   Full-sequence portfolio runs are reported too, but explicitly as VETO-ONLY evidence.

### 2. NEW DESIGN DECISIONS SPECIFIC TO EXP-025 (declared before results)
E1. **The pairing subtlety, and how it is resolved.** Because `affected(T) ⊆ affected(0.5)`, the treated subsets are
    not the same set across arms, so a naive "avgR on each arm's own affected subset" comparison would compare
    different trades and be meaningless (it would also mechanically favour high T by selection). RESOLUTION, fixed
    here: the DECIDING metric is the **paired per-trade R difference on the affected(0.5) subset** — the trades the
    CURRENT LIVE RULE touches — i.e. `A(T) − A(0.5)`, same trades, same entry bars, same lots, SE of the DIFFERENCE.
    For a trade in affected(0.5) that does NOT reach +T·R, the `A(T)` outcome is simply whatever the untreated
    management produces (stop_loss / take_profit / structure / time_stop) — which is precisely the point of raising T
    and must be counted, good or bad. Pooled over Train (y1+y2+y3) and separately over Val (y4).
E2. **The EXP-023 same-bar/next-bar addendum does NOT arise here, and this is why.** That addendum existed only
    because mode B places a RESTING BROKER STOP, so "is the fresh stop live for the rest of the triggering bar?" was a
    real, outcome-changing intrabar-path question. Mode A is a MARKET CLOSE executed at the moment of the trigger: the
    position is gone within the same poll cycle, there is no later level to be touched, and no convention choice
    exists. Every arm in this experiment is mode A. Hence ONE convention, no `@samebar` variants, and the arm count
    stays small.
E3. **Trigger convention, unchanged from EXP-023** (so the two experiments' numbers are comparable): per bar the
    priority is SL/TP `check_exit` (engine convention, SL wins a double-touch) > news-protection intrabar trigger >
    Watchman CLOSE at the bar's close. Trigger price = the bar's OPEN if the position is already ≥ T·R at the open,
    else the exact +T·R level. Exits fill nominally (engine convention; symmetric across all arms — see EXP-023's
    HONEST CAVEAT ON EXIT COSTS, which applies here identically and is an order of magnitude smaller than any effect
    that could change a verdict).
E4. **No lock-SL variants.** EXP-023 answered that question; re-testing it would be exactly the "shopping for a past
    result" the protocol forbids. Mode C (no protection) is carried as a REFERENCE BOUND ONLY and is never a
    candidate: removing a deliberate risk control is out of scope here and belongs to EXP-024's question.

### 3. GRID (one parameter — `watchman.news_profit_threshold_r`)
| id | mode | T | note |
|----|------|---|------|
| A050 | close_all | 0.50 | **BASELINE = current config, current live behavior** |
| A075 | close_all | 0.75 | |
| A100 | close_all | 1.00 | **PRIMARY CANDIDATE, declared up front** |
| A125 | close_all | 1.25 | |
| A150 | close_all | 1.50 | |
| C    | none      | n.a. | REFERENCE BOUND ONLY (= T → ∞ / no protection). NOT a candidate. |

PRIMARY-CANDIDATE MECHANISM STORY (declared before results, so it cannot be back-fitted): T = 1.0 is the midpoint of
the grid and the only value with an a-priori story — it is one full initial-stop-distance of profit (above gold's
hourly retrace noise, which EXP-023 §2 showed swallows anything parked at +0.3R) and exactly halfway to the 2R
take-profit, so a trade that reaches it has already earned the right to be *considered* worth protecting, while trades
that merely poke +0.5R and fold are left to the normal SL/TP machinery.
Coarse grid only, 5 points across the plausible range, ONE pass — no refinement pass is pre-authorised. If the pooled
curve turns out to be flat (the null this experiment fully expects), refinement would be fitting noise.
Configs evaluated (this exp / cumulative for THIS family): 5 T × 2 proxies = **10 / 10** (first-ever in this family).
Sibling-family history for multiple-testing honesty: EXP-023 burned 20 configs on the adjacent "fallback action"
question using the SAME windows and the SAME affected subsets. These are different hypotheses, but the DATA is the
same and the searcher is the same, so the bar is NOT reset to zero: the required edge is set at **1.7 SE**, the level
EXP-023 itself used at N=20, rather than the nominal 1.6 SE for N=10.

### 4. SPEC BOUNDS
Appendix A §4.5 states the rule as "ไม้กำไรอยู่ ≥ 0.5×R" with the item tagged `[adjustable]`, and `config/base.yaml`'s
watchman block plus `news_protection.NewsProtectionConfig`'s docstring both say "all values [adjustable]". No hard
numeric ceiling is specified. Self-imposed bound, declared here: **T is tested only in [0.5, 1.5], strictly below
`order.tp_r_multiple` = 2.0.** At T ≥ 2.0 the trigger can never fire before the take-profit and mode A degenerates
into mode C, i.e. it would be a covert removal of the risk control rather than a tuning of it — out of bounds for this
experiment by construction, not by result. Nothing else in `config/base.yaml` moves: `news_window_minutes` stays 30,
`news_close_mode` stays `half` (its min-lot degeneration into CLOSE_ALL is the mechanism under test, not a knob here).

### 5. WINDOWS, ACCOUNT CONTEXT, COST MODEL (identical to EXP-023 so the two are directly comparable)
Train y1 2021-07-22→2022-07-21, y2 2022-07-22→2023-07-21, y3 2023-07-22→2024-07-21; Val y4 2024-07-22→2025-07-21.
$3,000 starting equity per-year anchored, `min_lot_risk_cap_pct` 1.5, `risk_per_trade_pct` 1.0, all-24h session,
be/trail OFF, tp 2.0, pivot 3. COST MODEL COMPLETE: slippage = min-1-spread (`slippage_points=None`), swap modelled
(EXP-018 rates long −53.2 / short +36.8, 3× Wed), commission $0.00 (IC Markets Standard, the real account).
Harness: `experiments/exp025_news_threshold_harness.py`; raw outputs `experiments/exp025_cond_out.txt` (conditional /
deciding) and `experiments/exp025_port_out.txt` (portfolio / veto-only).
FIDELITY GATES, run and reported BEFORE any candidate number is read (STOP if any fails):
 1. `--mode fidelity`: the copied bar loop with the news mechanism OFF must equal `backtest.engine.run_backtest`
    trade-for-trade, field-for-field, both with and without EXP-022's fast-path memoisation shim.
 2. Conditional-replay self-check, asserted in code on every window: replaying each mode-C trade one-by-one must
    reproduce the full-sequence mode-C trade list EXACTLY.
 3. EXTERNAL anchor against a previously RECORDED number: mode C on y4/VAL at $3,000 must reproduce
    **254 trades, PF 1.0961, net +$352.60, maxDD 9.99%** (EXP-022's cap-1.5 y4 cell, re-confirmed by EXP-023).
 4. INTERNAL anchor against EXP-023: at T = 0.5 the A arm must reproduce EXP-023 §1's affected counts
    (P1 162/152/149/154, P2 124/116/116/124) and its affected-subset avgR (P1 0.501/0.497/0.494/0.495).
 5. Monotonicity assert: for every T ≥ 0.5, every trade that triggers under T must also have triggered under 0.5.

### 6. METRIC THAT DECIDES + ACCEPTANCE CRITERION (ALL required to RECOMMEND a T over 0.5; set before any result)
 (a) TREATMENT EFFECT: pooled paired mean of `R(A(T)) − R(A(0.5))` on the affected(0.5) subset under P1 must exceed
     **+1.7 SE** of that paired difference on Train (y1+y2+y3) **AND** on Val (y4). Both, not either.
 (b) PER-YEAR SIGN CONSISTENCY: the paired mean difference must have the SAME SIGN in y1, y2, y3 and y4 — no flips.
     (EXP-020 / EXP-022(a) precedent: a Train/Val ranking inversion is the overfit signature.)
 (c) PLATEAU: the winner's ±1 grid-step neighbours must land within ~15% of it on Val. A lone spike with collapsing
     neighbours is noise → REJECT even if it is the sweep's best number.
 (d) PORTFOLIO VETO (veto-only, never selection): full-sequence PF and max DD under the winning T within ~15% of
     A(0.5) on Train and on Val.
 (e) SAMPLE: ≥100 trades in affected(0.5) per evaluation window. EXP-023 measured 149–162 under P1 and 116–124 under
     P2, so this should pass; actuals are reported per window regardless.
 (f) P2 ROBUSTNESS: the SIGN of the effect must survive restriction to news-adjacent hours. An edge that exists only
     in quiet hours is not an edge on the mechanism being changed.
 (g) TAIL REPORT (mandatory disclosure, and a veto if it is ugly): raising T re-exposes trades between +0.5R and
     +T·R to full SL risk — an outcome A(0.5) makes ARITHMETICALLY IMPOSSIBLE (its floor is ~+0.26R). Report, per T
     and per window: worst single affected-trade R, and the COUNT/FRACTION of affected(0.5) trades that end NEGATIVE.
     A T that buys its mean with a materially fatter left tail is rejected on this criterion alone.
 Walk-forward: the 4 windows ARE the rolling confirmation for criterion (b); no separate tooling exists in this repo.
 Gate-integrity note: nothing in this experiment touches a promotion/demotion gate, an Auditor threshold or a
 circuit-breaker limit, and no such change will be proposed as a way to make any result pass. If the answer is "0.5 is
 already on the plateau", that is logged as the result — negative results are results.

### EXP-025 RESULTS (run 2026-08-04) — VERDICT: REJECT every raised T (0.75/1.0/1.25/1.5) under BOTH proxies. `watchman.news_profit_threshold_r` STAYS 0.5. NO config change, NO src/ change, Test year NOT touched.
Raw output: `experiments/exp025_cond_out.txt` (conditional/deciding), `experiments/exp025_pool_out.txt` (pooled paired
statistics), `experiments/exp025_port_out.txt` (portfolio/veto-only), `experiments/exp025_fidelity_out.txt`.
Harness: `experiments/exp025_news_threshold_harness.py`. All runs on the dev PC, `.venv` (Python 3.12.10, pandas 3.0.5).

FIDELITY (all five pre-registered gates passed BEFORE any candidate number below was read):
 1. `--mode fidelity`, 4,000-bar window, 197 trades: the copied bar loop with the news mechanism OFF is IDENTICAL to
    the real `backtest.engine.run_backtest`, field-for-field, both with and without EXP-022's fast-path shim
    (`copy_off_identical=True copy_off_fastpath_identical=True`).
 2. Conditional-replay self-check (asserted in code, every window): replaying each mode-C trade one-by-one reproduces
    the full-sequence mode-C trade list EXACTLY.
 3. EXTERNAL anchor: mode C on y4/VAL at $3,000 = **254 trades, PF 1.0961, net +$352.60, maxDD 9.99%** — matches
    EXP-022's cap-1.5 y4 cell and EXP-023's re-confirmation to the last digit (`--mode anchor`, `match: true`).
 4. INTERNAL anchor vs EXP-023: affected(0.5) counts P1 **162/152/149/154**, P2 **124/116/116/124**, and A(0.5)
    affected-subset avgR P1 **0.501/0.497/0.494/0.495**, C-on-affected avgR P1 **0.496/0.526/0.674/0.627** — every
    number is EXP-023 §1's, unchanged. The two experiments are measuring the same subsets on the same data.
 5. Monotonicity probe (asserted in code, per window per proxy): replaying every NOT-affected(0.5) trade at the
    smallest raised T (0.75) produced **0 leaks** in all 8 window×proxy cells (unaffected n = 84–142). Since triggering
    at a higher T implies triggering at every lower one, `affected(T) ⊆ affected(0.5)` holds empirically, as designed.

### 1. DECIDING EVIDENCE — paired per-trade R difference on the affected(0.5) subset, sequence held FIXED
avgR on the affected(0.5) subset (the trades the CURRENT LIVE RULE force-closes at +0.5R):
```
P1 (always-eligible)      tot  aff |  C(none)  A0.5   A0.75  A1.0   A1.25  A1.5
y1 2021-22                266  162 |  0.496    0.501  0.520  0.549  0.538  0.545
y2 2022-23                254  152 |  0.526    0.497  0.481  0.507  0.512  0.548
y3 2023-24                233  149 |  0.674    0.494  0.610  0.623  0.602  0.715
y4 VAL 2024-25            254  154 |  0.627    0.495  0.500  0.619  0.602  0.646
P2 (US-macro hours)       tot  aff |  C(none)  A0.5   A0.75  A1.0   A1.25  A1.5
y1 2021-22                266  124 |  0.597    0.606  0.565  0.654  0.664  0.634
y2 2022-23                254  116 |  0.645    0.619  0.642  0.586  0.560  0.594
y3 2023-24                233  116 |  0.816    0.616  0.705  0.784  0.744  0.801
y4 VAL 2024-25            254  124 |  0.835    0.669  0.687  0.752  0.782  0.786
```
POOLED paired treatment effect **A(T) − A(0.5)** on affected(0.5) (same trades, SE is of the DIFFERENCE;
pre-registered bar: > +1.7 SE on Train AND Val):
```
                  TRAIN y1+y2+y3 (n=463)              VAL y4 (n=154)
P1  T=0.75        +0.0390 +-0.0288 (t +1.35)          +0.0046 +-0.0489 (t +0.09)
P1  T=1.00        +0.0615 +-0.0403 (t +1.53)          +0.1236 +-0.0643 (t +1.92)   <- PRIMARY CANDIDATE
P1  T=1.25        +0.0527 +-0.0494 (t +1.07)          +0.1071 +-0.0822 (t +1.30)
P1  T=1.50        +0.1033 +-0.0558 (t +1.85)          +0.1510 +-0.0942 (t +1.60)
P1  [C bound]     +0.0655 +-0.0638 (t +1.03)          +0.1320 +-0.1095 (t +1.21)   <- C - A(0.5), reference only
                  TRAIN (n=356)                       VAL (n=124)
P2  T=0.75        +0.0224 +-0.0337 (t +0.66)          +0.0185 +-0.0554 (t +0.33)
P2  T=1.00        +0.0608 +-0.0461 (t +1.32)          +0.0832 +-0.0753 (t +1.11)
P2  T=1.25        +0.0427 +-0.0558 (t +0.77)          +0.1137 +-0.0903 (t +1.26)
P2  T=1.50        +0.0618 +-0.0631 (t +0.98)          +0.1169 +-0.1000 (t +1.17)
P2  [C bound]     +0.0707 +-0.0688 (t +1.03)          +0.1658 +-0.1092 (t +1.52)
```
**Not one T clears the pre-registered +1.7 SE bar on Train AND Val.** The primary candidate T=1.0 passes on Val
(t +1.92) and misses on Train (t +1.53, bar +0.068 vs mean +0.062). T=1.5 is the mirror image: Train passes
(t +1.85), Val misses (t +1.60). Every effect in the table is positive-leaning and none is individually significant.
Per-year paired differences, P1 (the per-year consistency evidence for criterion (b)):
```
                  T=0.75            T=1.00            T=1.25            T=1.50
y1 2021-22        +0.0191 (+0.38)   +0.0478 (+0.70)   +0.0368 (+0.44)   +0.0439 (+0.46)
y2 2022-23        -0.0155 (-0.29)   +0.0096 (+0.13)   +0.0154 (+0.17)   +0.0515 (+0.52)
y3 2023-24        +0.1163 (+2.57)   +0.1293 (+1.89)   +0.1081 (+1.26)   +0.2207 (+2.31)
y4 VAL 2024-25    +0.0046 (+0.09)   +0.1236 (+1.92)   +0.1071 (+1.30)   +0.1510 (+1.60)
```
Read honestly: the whole family's apparent signal is concentrated in **y3 2023-24**, the one window where it is
individually significant at any T, and y2 contributes essentially nothing at every T. That is the EXP-020/EXP-022(a)
"one regime carries it" shape, not a stable effect.

### 2. WHY NO T WORKS — the mechanism (this is the real content of the experiment)
Exit mix of the affected(0.5) subset on y4/VAL, P1 (n=154) — the cleanest statement, in the same form as EXP-023 §2:
```
  C  (no protection)  : 75 take_profit (48.7%), 49 stop_loss, 27 time_stop, 2 structure, 1 eod  -> avgR 0.627, medR 0.313, worstR -1.346, 62 negative
  A  (T=0.50, LIVE)   : 154 news_protection (100%)                                              -> avgR 0.495, medR 0.500, worstR +0.264,  0 negative
  A  (T=0.75)         : 132 news_protection, 21 stop_loss, 1 time_stop, 0 take_profit (0.0%)    -> avgR 0.500, medR 0.750, worstR -1.115, 21 negative
  A  (T=1.00)         : 115 news_protection, 26 stop_loss, 7 time_stop, 4 take_profit (2.6%)    -> avgR 0.619, medR 1.000, worstR -1.115, 30 negative
  A  (T=1.25)         :  96 news_protection, 38 stop_loss, 11 time_stop, 7 take_profit (4.5%)   -> avgR 0.602, medR 1.223, worstR -1.346, 45 negative
  A  (T=1.50)         :  80 news_protection, 43 stop_loss, 15 time_stop, 13 take_profit (8.4%)  -> avgR 0.646, medR 1.457, worstR -1.346, 52 negative
  (P2/y4, n=124, same shape: C 68 TP (54.8%); T=0.5 0; T=0.75 5; T=1.0 13; T=1.25 20; T=1.5 25)
```
**Raising T does NOT restore the 2R upside — it just moves the guillotine up the ladder.** The premise that started
this whole line of inquiry (live banks every winner at +0.5R while TP is never reached) is only *nominally* addressed
by a higher trigger: on y4/P1 the take-profit rate on the affected subset goes 48.7% (no protection) → 0% (T=0.5) →
2.6% (T=1.0) → 8.4% (T=1.5). Even at the top of the pre-registered range, 92% of these trades still never see 2R,
because the trigger is *unconditional on price path* — any T < `tp_r_multiple` simply relabels which rung the certain
exit happens on. The modal outcome under every arm is still `news_protection` at exactly +T·R (note the median is
exactly T in every column). What actually changes with T is the SHAPE of the distribution, from "certain +0.5R" to a
bimodal "+T·R or −1R": under P1 the count of affected trades ending NEGATIVE goes 0 → 15–25 (T=0.75) → 30–42 (T=1.0)
→ 44–54 (T=1.25) → 46–60 (T=1.5) per window, and the worst single affected trade goes from a guaranteed floor of
+0.26R..+0.33R to −1.11R..−1.84R. That trade-off is a real one to consider, but it is NOT "letting winners run".
**And the sweep has NO INTERIOR OPTIMUM.** The pooled effect rises (raggedly, with a dip at 1.25) all the way to the
edge of the pre-registered range, and the mode-C reference bound (T → ∞, no protection at all) sits right inside the
same band: +0.066R Train / +0.132R Val, statistically indistinguishable from T=1.0 and T=1.5. In other words the
entire measurable "signal" across the sweep is one and the same effect — **less protection is (weakly, insignificantly)
better** — and it has no preferred level. That is a statement about whether this risk control pays for itself, which
is EXP-023 §6(i)'s escalated question and EXP-024's job with a real calendar, NOT something a trigger-level grid can
answer. There is no plateau here because there is no peak to be on a plateau around.

### 3. PORTFOLIO (full-sequence; pre-registered VETO-ONLY, never selection evidence)
$3,000/window, complete cost model. PF | net$ | maxDD% | trades:
```
P1                y1                        y2                        y3                        y4 VAL
A@T0.50     1.004 |   +16 | 17.1 | 510   0.964 |  -175 | 20.0 | 516   1.076 |  +350 | 15.8 | 486   0.980 |   -86 | 12.5 | 554
A@T0.75     1.014 |   +64 | 15.8 | 410   1.023 |  +114 | 15.7 | 417   1.135 |  +688 | 15.4 | 407   1.002 |    +9 | 12.4 | 434
A@T1.00     1.066 |  +298 | 13.8 | 359   1.023 |  +116 | 21.9 | 355   1.121 |  +534 | 14.5 | 333   1.115 |  +509 |  9.2 | 369
A@T1.25     1.071 |  +300 | 13.1 | 319   1.035 |  +161 | 21.9 | 311   1.091 |  +415 | 13.5 | 312   1.158 |  +684 | 11.7 | 317
A@T1.50     1.064 |  +271 | 14.7 | 295   0.955 |  -211 | 26.9 | 293   1.203 |  +977 | 14.4 | 289   1.159 |  +652 | 10.4 | 289
C_none      1.016 |   +64 | 14.5 | 266   0.995 |   -22 | 26.1 | 254   1.202 |  +773 | 12.3 | 233   1.096 |  +353 | 10.0 | 254
P2 (news-hours)
A@T0.50     1.030 |  +127 | 12.9 | 416   0.897 |  -456 | 23.8 | 396   1.115 |  +497 | 11.2 | 385   1.010 |   +39 | 14.0 | 400
A@T0.75     1.015 |   +59 | 12.9 | 349   0.913 |  -402 | 25.0 | 358   1.213 | +1006 | 12.1 | 344   1.040 |  +153 | 11.5 | 354
A@T1.00     1.087 |  +373 | 11.5 | 326   0.893 |  -471 | 27.8 | 313   1.182 |  +782 | 13.0 | 300   1.135 |  +547 | 10.4 | 316
A@T1.25     1.004 |   +18 | 13.0 | 295   0.978 |   -95 | 22.7 | 285   1.140 |  +590 | 14.5 | 279   1.167 |  +650 | 10.3 | 291
A@T1.50     1.026 |  +104 | 14.3 | 277   0.962 |  -170 | 25.7 | 273   1.108 |  +468 | 13.2 | 274   1.123 |  +468 | 11.4 | 270
```
This table does NOT veto T=1.0 — it is better than A@T0.50 on PF in all four windows (+4% to +14%) and its max DD is
lower in three of four (y2 is +10%, inside the ~15% tolerance). Stated plainly because honesty cuts both ways: the
veto-only evidence would have LET a raised T through. It did not carry it, and by pre-registration it cannot: these
full-sequence runs differ from the baseline by 100–200 trades per year (e.g. y4 554 → 369), i.e. they are dominated by
which signals the re-sequenced engine happens to take next, which is exactly the non-causal reshuffling EXP-017/020/021
established as unusable for selection. Note also that the trade count falls with T while `news_protection` exits get
scarcer — the portfolio arms are not comparable populations at all.

### 4. ROBUSTNESS vs THE PRE-REGISTERED BARS (criterion by criterion)
 (a) paired effect > +1.7 SE on Train AND Val (P1) — **FAIL, all four raised T.** T=1.0: Train t +1.53 / Val t +1.92.
     T=1.5: Train t +1.85 / Val t +1.60. T=0.75 and T=1.25: fail both. No T passes both windows.
 (b) per-year sign consistency y1–y4 — **PASS for T=1.0, T=1.25, T=1.5 under P1** (all four years positive);
     **FAIL for T=0.75** (y2 −0.0155). Logged as a pass where it is one — but see §1: the magnitude is carried almost
     entirely by y3, so sign-consistency here is a weak form of consistency, not a strong one.
 (c) plateau, winner's ±1 grid neighbours within ~15% on Val — **FAIL, decisively, for every candidate.** T=1.0
     (+0.1236) has neighbours +0.0046 (T=0.75, **−96%**) and +0.1071 (T=1.25, −13%): one neighbour collapses to zero.
     T=1.5 (+0.1510) has lower neighbour +0.1071 (−29%, outside 15%) and NO upper neighbour — 1.75 is outside the
     pre-registered bound and untested by design, so its plateau cannot even be established. The Train curve is
     non-monotone (+0.039 / +0.062 / +0.053 / +0.103), dipping at 1.25 and jumping at 1.5. This is a jagged field of
     noise, exactly the shape rule 5 exists to reject.
 (d) portfolio non-degradation (veto-only) — **PASS / no veto** for T=1.0 (see §3). Cannot carry the candidate.
 (e) sample >=100 affected(0.5) per window — **PASS**: P1 162/152/149/154, P2 124/116/116/124.
 (f) P2 sign robustness — **PASS on sign, weak in substance**: pooled P2 effects are positive at every T on both
     Train and Val (T=1.0: +0.061 Train / +0.083 Val), so the sign survives restriction to news-adjacent hours, but
     every t-statistic falls (max +1.32) and y2 flips negative at T=1.0/1.25/1.5. Nothing here rescues (a).
 (g) tail — **FAIL by the pre-registered wording.** A(0.5) has a hard arithmetic floor (worst affected trade
     +0.26R..+0.33R, ZERO negative outcomes by construction). Every raised T destroys that floor: negatives per window
     go 0 → 15–25 (T=0.75) → 30–42 (T=1.0) → 44–54 (T=1.25) → 46–60 (T=1.5) out of ~150, i.e. **19–28% of these
     trades end in the red at T=1.0**, worst single trade −1.11R to −1.84R. Counter-evidence recorded for fairness:
     portfolio max DD did NOT worsen at T=1.0 (§3), and the affected-subset PF stays high (4.45 on y4/P1), so the tail
     is a genuine redistribution rather than a hidden blow-up. But the pre-registered test was "a T that buys its mean
     with a materially fatter left tail is rejected on this criterion alone", the mean gain is not established, and
     the tail cost is certain — so it fails as written.
 Walk-forward: the 4 windows served as the rolling confirmation (criterion (b)); no separate tooling exists.
 Multiple testing: 10 configs this experiment (5 T × 2 proxies), 10 cumulative for this family, 20 more in EXP-023's
 sibling family on the same data and the same subsets.
 **DISCLOSURE — the pre-registered bar is NOT what killed the candidate, and the record must say so.** At the nominal
 1.6 SE (the level a fresh N=10 family would have earned) T=1.5 would have passed criterion (a) by a hair on both
 windows (Train t +1.85, Val t **+1.602** vs a 1.600 bar). It was pre-registered at 1.7 BEFORE any number was read,
 precisely so this call could not be made after the fact. And it changes nothing: T=1.5 fails the plateau check (c)
 independently and by a wide margin, is the EDGE of the pre-registered range with an untestable upper neighbour, and
 sits statistically on top of the out-of-scope mode-C bound. A candidate that only exists at t=1.602 on one window, at
 the edge of its own grid, with a neighbour 29% away and no interior optimum, is the textbook false peak.

### 5. VERDICT
**REJECT every raised T. `config/base.yaml` UNCHANGED — `watchman.news_profit_threshold_r` stays 0.5** (and
`news_window_minutes` 30, `news_close_mode` half). `src/` UNCHANGED. No promotion/demotion gate, Auditor threshold or
circuit-breaker limit was touched or proposed for change. **Test year 2025-07-22 → 2026-07-21 NOT touched**; this
family's one-touch budget is UNSPENT, because nothing earned a Test confirmation.
The honest one-line summary: *the trigger LEVEL is not the problem.* Every value from 0.5 to 1.5 produces the same
weak, insignificant, y3-concentrated drift in the same direction, the reference bound at "no protection at all" sits
in the same band, and no level restores the 2R upside the exercise was meant to recover. EXP-023 §6(iii) proposed this
as "the surgical lever"; measured, it turns out not to be a lever with a setting — it is the same single question
(does min-lot news protection pay for itself?) reappearing at every grid point.

### 6. WHAT THIS CHANGES FOR THE ROADMAP (escalated, not decided here)
 (i) EXP-023 §6(iii)'s recommended order is now partly SPENT: item (2) "then test the threshold/window" is half done
     and came back negative for the threshold. `news_window_minutes` (the other half) is NOT worth a separate
     experiment on the current evidence: with no historical calendar it is unobservable by construction — both proxies
     here are hour-masks, and the window width only matters once real event times exist.
 (ii) The remaining live question is unchanged and is now better bracketed by two independent experiments: the
     pooled C − A(0.5) gap re-measured here (+0.066R Train / +0.132R Val per affected trade under P1, +0.071R /
     +0.166R under P2) reproduces EXP-023 §6(i) EXACTLY, and the T-sweep adds that no intermediate setting recovers
     it. Whether news protection at min-lot is worth its cost still requires the REAL trigger rate, i.e. EXP-024 on
     `data/db/news_calendar_history.csv` once enough weeks have accumulated (started 2026-08-03). Nothing in this
     experiment is a licence to disable the control: mode C remains a reference bound, never a candidate.
 (iii) A genuinely different mechanism NOT tested by EXP-023 or EXP-025, recorded for whoever designs next: make the
     protection CONDITIONAL ON THE POSITION SIZE rather than on the profit level — i.e. at min-lot, where "close
     half" is impossible, choose SKIP over CLOSE_ALL (`watchman/loop.py`'s comment says that direction was chosen on
     instinct, not evidence). That is a change to the fallback ACTION's fail-direction, which is distinct from
     EXP-023's lock-SL question, and it would need its own pre-registration — and, more importantly, it is really the
     same "does the control pay for itself" question again, so it should wait for EXP-024 too rather than becoming a
     third grid search on the same 4 windows.

HONEST CAVEAT ON EXIT COSTS (unchanged from EXP-023, applies to every arm, does not change the verdict): the engine
bakes spread/slippage into the ENTRY fill only; exits fill nominally. Mode A's market close at +T·R is therefore
modelled ~1 spread too favourably (~$0.35, ≈0.01–0.03R) at EVERY T, so the bias is near-identical across arms and an
order of magnitude below the paired differences discussed above. It is the one place this simulation is optimistic for
all arms relative to live.

## NOTE (not an EXP) 2026-08-04 — Historical calendar DUMP obtained from MT5 itself; depth VERIFIED to 2021-07; one timezone quirk found and characterised (EXP-024 unblocked early)
The wait-for-accumulation plan (2026-08-03 NOTE above) assumed history had to be collected forward. It does not:
`CalendarValueHistory()` — the same call `mql5/NewsCalendarExporter.mq5` already uses — accepts arbitrary historical
windows. `mql5/CalendarHistoryDump.mq5` (one-off Script, this commit) dumps 2021-07 → now+48h in the exporter's exact
CSV format; run 2026-08-04 on the dev PC's IC Markets demo terminal ("MetaTrader 5-2") → 73,699 rows, 5.5 MB, to the
terminal's Common Files as `AutoTradeNewsCalendarHistory.csv` (dev-PC local, not committed; regenerable by re-running
the Script).

Validation (5 checks, scratchpad `validate_calendar_dump.py`, results 2026-08-04):
- DEPTH: earliest event exactly 2021-07-01; high-impact-USD present in all 62 months, no gap months (min/med/max
  9/43/51 per month) — **the full Train+Val window is covered; EXP-024 need not wait for accumulation.**
- FOMC: 17/17 known Fed decision dates (2021-09→2025-06) have a high-USD event.
- OVERLAP vs the live archive: 115/116 keys match; the 1 miss is a reschedule (AUD low event moved 08-04→08-05),
  i.e. the dump reflects the current schedule while the archive recorded what live SAW — exactly the distinction the
  archive exists to capture. Keep accumulating it; it stays the cross-check and the reschedule/fail-safe ground truth.
- HYGIENE: 0 unparseable rows, dup-key rate 0.47% (consumers dedup by key), importance values within the expected set.
- **TIMEZONE QUIRK (the one real finding): dump event times are UTC + the CURRENT server offset (+3) applied to ALL
  history, not the per-date server clock.** Symptom: headline NFP prints 15:30 in summer months but 16:30 in every
  Dec–Mar across 2021-2026 (US-DST-off months). Proof against the very H1 bars the backtests run on
  (`data/historical/XAUUSD_H1.csv`): on the 20 winter NFP days the max-range bar of {14,15,16,17}:00 is the **15:00
  bar 12/20** times vs 16:00 only 5/20 (≈ the noise floor — summer control: 15:00 wins 22/37 with the calendar itself
  saying 15:30). So winter events are stamped 1h LATE relative to the bar clock. **EXP-024 rule: normalise dump times
  by −1h wherever the server ran UTC+2 (US-DST winter), then require post-normalisation NFP = 15:30 server year-round
  as a built-in self-check.** The live archive (collected with the then-current offset each cycle) will not show this
  skew for rows collected in the season they occur — a second reason to keep it running.
- Standing limitation (unchanged from the dump script's header): importance/name reflect MetaQuotes' CURRENT
  classification — declare in EXP-024's pre-registration.

## EXP-024 2026-08-04 — News protection measured against the REAL historical calendar: trigger rate + parity cost of live's mode A (NEW family "news-protection backtest/live parity", the family EXP-023 §6(i) opened; the reserved 024 slot)
Status: MEASURED — real trigger rate 25.6% (Train) / 28.0% (Val) of trades vs the P1 ~61% / P2 ~47% brackets; real
parity gap A@real − C = −0.157R ± 0.090 (Train, n=193) / −0.154R ± 0.142 (Val, n=71) per affected trade; NO config
change, NO src/ change, Test year NOT touched. See `### EXP-024 RESULTS` below. Originally: PRE-REGISTERED (results pending). Everything from this line down to `### EXP-024 RESULTS` was written and
COMMITTED, together with a results-free `experiments/exp024_real_calendar_harness.py`, BEFORE any A@real number was
produced. Only the RESULTS section and this Status line are added afterwards.
**This is a MEASUREMENT experiment, not a selection experiment.** There is no grid, no candidate, no winner, and no
`config/base.yaml` or `src/` change can follow from it directly. Its whole job is to replace EXP-023's P1 (~60% of
trades affected) / P2 (~46%) *proxy brackets* with the real number, and to price the parity gap EXP-023 §6(i)
escalated and EXP-025 §6(ii) re-confirmed as the one open question in this whole line of work.
Scope: Train (y1/y2/y3) + Val (y4) ONLY. **The Test year 2025-07-22 → 2026-07-21 is NOT touched under any outcome —
pre-registered here, and enforced in the harness (`--window y5*` returns REFUSED).** The obvious next question ("what
do the promotion-gate numbers look like on Test under real mode A, i.e. what is the honest Test baseline for the
config we actually run live?") is deliberately LEFT OPEN by this experiment so that the Test year stays clean for it.
That is a user-level decision about how to spend this family's one-touch budget, not a decision this experiment may
make.

### 0. THE QUESTION, AND WHY IT IS ANSWERABLE TODAY AND WAS NOT ON 2026-08-03
EXP-023 D1 established that `backtest/engine.py` does not model news protection at all: every number this project has
ever produced (EXP-001..EXP-025, including the Gate-1 arithmetic EXP-008 was adopted on) describes **mode C — no news
protection** — while the live system runs **mode A — close the whole position at the trigger**, because at min-lot
`watchman/loop.py::_half_volume_rounded` returns `None` and CLOSE_HALF_AND_BREAKEVEN recurses into CLOSE_ALL. EXP-023
priced that gap only inside two proxy brackets because no historical calendar existed (its D2). EXP-025 swept the
trigger LEVEL and concluded the level is not the lever — the same single question reappears at every grid point.
The 2026-08-04 NOTE removed the blocker: `mql5/CalendarHistoryDump.mq5` pulled MetaQuotes' own
`CalendarValueHistory()` back to 2021-07-01 (73,699 rows), validated for depth, FOMC coverage, hygiene and one
timezone quirk. So the trigger rate is now MEASURABLE over the whole Train+Val history, not bracketed.

### 1. INHERITED DESIGN DEVIATIONS (EXP-023 D1/D3 — still binding; D2 is what this experiment RETIRES)
D1 (inherited, unchanged). The engine does not model news protection; mode A is simulated by EXP-023's VERBATIM copy
   of the engine bar loop (re-used through `exp025_news_threshold_harness`), never by production code. Nothing under
   `src/` or `config/` is modified by this experiment.
D2 (**RETIRED, and that is the point of EXP-024**). The trigger time is no longer proxied. P1/P2 survive here only as
   CONTINUITY ANCHORS whose job is to reproduce EXP-023/EXP-025's published numbers digit-for-digit and thereby prove
   this harness is the same instrument. They are anchors, never candidates and never the deciding measurement.
D3 (inherited, unchanged). The deciding comparison is TRADE-MATCHED and CONDITIONAL with the trade sequence held
   FIXED (same entries, same fills, same lots, same bars; only the management rule differs), because
   `max_positions_per_symbol: 1` means an earlier exit reshuffles which signal is taken next, and EXP-017/020/021
   established that reshuffling noise dominates portfolio deltas. Full-sequence portfolio runs ARE reported here —
   and unlike EXP-023/025 they are not "veto-only evidence", because there is no candidate to veto: they are the
   portfolio-level *statement of the parity gap* ("by how much does every historical PF/net$/maxDD in this log
   overstate the config that actually runs live?"), and they are read as such, with the reshuffling caveat attached.

### 2. DATA CONTRACT AND THE MANDATORY TIMEZONE NORMALISATION (from the 2026-08-04 NOTE; implemented, not re-derived)
SOURCE: `C:\Users\Varintha\AppData\Roaming\MetaQuotes\Terminal\Common\Files\AutoTradeNewsCalendarHistory.csv`
(73,699 rows, 2021-07-01 → 2026-08-05, exporter CSV format, first line a `#` comment). **Read IN PLACE, not copied
into `data/db/`** — it is a 5.5 MB regenerable, gitignored, dev-PC-local artifact and a second copy would only create
a second thing that can go stale; the path is a `--calendar` argument so the file can move without touching code.
PARSER: the PRODUCTION parser `council/mql5_calendar_provider.parse_export_csv` is called directly, so the harness
cannot drift from what live reads. Rows are then deduped on `(event_time, currency, importance, event_name)` exactly
like `council/calendar_archive.py`'s `_KEY_COLUMNS` (the dump's own dup-key rate is 0.47%).
NORMALISATION (mandatory, from the NOTE): the dump stamps ALL history as UTC + the CURRENT server offset (+3), while
the H1 bars the backtest runs on are stamped in the true per-date server clock (UTC+2 in US-DST-off, UTC+3 in
US-DST-on). Therefore **subtract 1h from every dump event time whose date falls in a US-DST-OFF period**, with the US
rule implemented properly: DST is ON for `second Sunday of March <= date < first Sunday of November`, computed per
year, NOT at month granularity. Transition-day granularity is the date, not the 2:00-ET instant; the residual error
is confined to at most two Sundays a year, on which the US high-impact release calendar is empty.

### 3. ARMS (two, plus two continuity anchors — no parameter is varied anywhere in this experiment)
| id | what | role |
|----|------|------|
| **C** | no news protection | REFERENCE ARM = what every backtest in this log has silently measured |
| **A@real** | mode A (close-all at +0.5R) triggered from the REAL normalised calendar under live semantics | **THE MEASUREMENT** |
| A@P1 | mode A, every bar eligible | CONTINUITY ANCHOR (EXP-023/025's primary proxy) — must reproduce their published numbers |
| A@P2 | mode A, Mon–Fri server hours {14,15,16,20,21} | CONTINUITY ANCHOR (EXP-023/025's robustness proxy) |

`news_profit_threshold_r` stays 0.5, `news_window_minutes` stays 30, `news_close_mode` stays `half` (degenerating to
CLOSE_ALL at min-lot) — i.e. exactly `config/base.yaml` as adopted. Nothing is swept. Multiple-testing inflation
(rule 7) is therefore essentially nil: 4 arms, 0 free parameters, 0 selection decisions, and no threshold anywhere in
this experiment can be "chosen" by its result. Cumulative for this NEW family: 4 / 4.

### 4. LIVE TRIGGER SEMANTICS THIS HARNESS MUST REPRODUCE (exact functions, cited before any number)
 (a) **Which currencies.** `watchman/news_protection._news_incoming` calls
     `council/risk_voice.get_symbol_currencies(symbol)`; `_SYMBOL_CURRENCIES["XAUUSD"] = ("USD",)`. So for XAUUSD the
     calendar is filtered to **USD only** — EUR/GBP/JPY high-impact events are irrelevant to this symbol by design.
 (b) **What "high impact" means.** `council/mql5_calendar_provider.MQL5CalendarProvider.get_high_impact_events` keeps
     a row iff `event.impact.lower() == "high"` (`_HIGH_IMPACT = "high"`). The dump's importance vocabulary is
     none/low/moderate/high, so "high" is a literal string match on the same field the live provider matches on.
 (c) **The window, and its exact bounds.** `_news_incoming` builds `window_end = now + timedelta(minutes=30)` and
     calls `get_high_impact_events(currency, now, window_end)`, which admits an event iff
     `window_start <= event_time <= window_end` — **inclusive at both ends, and FORWARD-LOOKING ONLY.** Protection is
     therefore active at instant `now` iff some high-impact USD event `e` satisfies `now <= e <= now + 30min`, i.e.
     `now` lies in `[e - 30min, e]`. **There is no post-event protection window at all** — the moment the event
     prints, protection stops firing. (This differs from `risk_voice`'s trade-ENTRY blackout, which is two-sided:
     `news_blackout_before_min: 45` / `news_blackout_after_min: 30`. Different mechanism, not touched here.)
 (d) **The profit gate.** `check_news_protection` computes `profit_r = (price - entry)/initial_stop_distance` against
     the ORIGINAL stop distance and short-circuits to NO_ACTION below `profit_threshold_r = 0.5`; the news check is
     not even consulted below that.
 (e) **BAR-RESOLUTION CONVENTION (declared, because it is a real methodological choice).** Live polls every ~5 s
     (`run_shadow_loop.py --poll-interval-sec` default 5.0); the backtest sees H1 bars. An H1 bar with open time `t`
     spans `[t, t+60min)`. The bar is declared TRIGGER-ELIGIBLE iff the set of live poll instants inside it that
     would see an active window is non-empty *with positive duration*, i.e. iff there exists an event `e` with
     `t < e < t + 60min + 30min`. Inside an eligible bar the EXP-023/025 price convention is used unchanged (trigger
     price = the bar's OPEN if the position is already at or above +0.5R at the open, else the exact +0.5R level;
     per-bar priority SL/TP `check_exit` > news trigger > Watchman CLOSE at the bar's close; exits fill nominally).
     This is bar-granular in both directions: it can fire on a +0.5R touch that really happened in the eligible bar's
     non-eligible minutes, and it cannot fire twice inside an hour. It is the same approximation P1/P2 already made,
     it is unavoidable at H1, and it is why A@real is a MEASUREMENT WITH A STATED RESOLUTION, not a claim about
     individual seconds.

### 5. WINDOWS, ACCOUNT CONTEXT, COST MODEL (identical to EXP-022/023/025 so all four are directly comparable)
Train y1 2021-07-22→2022-07-21, y2 2022-07-22→2023-07-21, y3 2023-07-22→2024-07-21; Val y4 2024-07-22→2025-07-21.
$3,000 starting equity per-year anchored, `min_lot_risk_cap_pct` 1.5, `risk_per_trade_pct` 1.0, all-24h session,
be/trail OFF, tp 2.0, pivot 3. COST MODEL COMPLETE: slippage = min-1-spread (`slippage_points=None`), swap modelled
(EXP-018 long -53.2 / short +36.8, 3x Wed), commission $0.00 (IC Markets Standard, the real account).
Harness `experiments/exp024_real_calendar_harness.py`; raw outputs `experiments/exp024_*.txt` (gitignored).

### 6. THE DECIDING MEASUREMENTS (three; each is a number to be REPORTED, not a test to be passed)
 (1) **REAL TRIGGER RATE.** Per window and pooled: (i) the % of trades AFFECTED, i.e. that reach +0.5R while a real
     news window is active, stated against the P1 (~60%) and P2 (~46%) brackets EXP-023/025 had to assume; (ii) the %
     of BAR-HOURS in the window that are trigger-eligible at all — the calendar-side density, independent of any
     trade. Also reported: the distribution of high-impact-USD event times by server hour (a direct audit of how good
     or bad P2's `{14,15,16,20,21}` hour mask was).
 (2) **REAL PARITY GAP.** Paired per-trade `R(A@real) - R(C)` on the affected@real subset, sequence held FIXED
     (EXP-023 D3 / EXP-025 E1 methodology, unchanged), per year and pooled over Train (y1+y2+y3) and, separately, Val
     (y4), with the SE **of the paired difference**. Restated as portfolio deltas (trades / PF / net$ / maxDD per
     window, C vs A@real) — that is the number that says how much every historical promotion-gate figure in this log
     overstates the configuration that is actually running live.
 (3) **EXIT-MIX TABLE** on the affected@real subset, C vs A@real, in the same form as EXP-023 §2 and EXP-025 §2 —
     y4/VAL at minimum, all four windows if the counts support it.

### 7. FIDELITY GATES — ALL run and reported BEFORE any A@real number is read; any failure STOPS the experiment
 G1. `--mode fidelity`: the copied bar loop with the news mechanism OFF must equal `backtest.engine.run_backtest`
     trade-for-trade, field-for-field, with and without EXP-022's fast-path memoisation shim.
 G2. `--mode anchor`: EXTERNAL cross-check against a previously RECORDED number — mode C on y4/VAL at $3,000 must be
     **254 trades / PF 1.0961 / net +$352.60 / maxDD 9.99%** (EXP-022's cap-1.5 y4 cell, re-confirmed by EXP-023 and
     EXP-025).
 G3. **NFP normalisation self-check.** After normalisation every high-impact USD `Nonfarm Payrolls` row **whose
     normalised time falls inside this experiment's data range (2021-07-22 → 2025-07-21)** must land at exactly
     **15:30 server** — hard abort otherwise. Disclosed up front, because it was found while building the check and
     hiding it would be dishonest: across the FULL dump (60 NFP rows, 2021-07 → 2026-08) 59 normalise to 15:30 and
     ONE does not — `2025-11-20` normalises to 14:30 (raw 15:30, where "+3 for all history" predicts 16:30). That
     date is inside the untouched TEST year, i.e. outside every window this experiment reads, so it cannot affect a
     single measured number here. The gate is therefore pre-registered as: abort on ANY in-range violation (in-range
     NFP count is 48); report, but do not abort on, an out-of-range one. It is logged for whoever eventually runs the
     honest-Test-baseline experiment, for whom it IS in range.
 G4. **CONTINUITY ANCHORS vs EXP-023/EXP-025.** A@P1 must reproduce affected counts **162/152/149/154** and
     affected-subset avgR **0.501/0.497/0.494/0.495**; A@P2 must reproduce **124/116/116/124**. These run through
     literally the same code path as EXP-025 (the real-calendar arm only adds a pre-filter in front of it), so any
     mismatch means the instrument changed and every EXP-023/025 number would be in doubt too.
 G5. Conditional-replay self-check, asserted in code on every window (inherited): replaying each mode-C trade
     one-by-one must reproduce the full-sequence mode-C trade list EXACTLY.

### 8. LIVE CROSS-VALIDATION GATE (the powerful, cheap one — and a FULL DISCLOSURE of what was already inspected)
The paper journal covers 2026-07-22 → 2026-08-03 and the dump covers those dates, so the reconstruction can be checked
against what live actually did. **DISCLOSURE, stated plainly and before the gate wording, because the order matters:
the four live `news_protection` exits and the +1.272R trade were checked against the dump BY HAND while designing this
gate, i.e. the gate's directional asymmetry below was written AFTER seeing that 3 of the 4 exits reconcile and 1 does
not.** Nothing in this gate feeds a deciding measurement — it can only STOP the experiment — but the reader is
entitled to know it was not written blind. Source of the live trades: `trade_journal_paper_vps_latest.sqlite` in the
repo root (11 closed trades, the freshest copy; `data/db/trade_journal_paper.sqlite` holds only 3 and is stale, and
the scratchpad VPS snapshot holds 10 — it predates trade #11).
The gate is DIRECTIONAL, because the two possible failures are not symmetric:
 L1 (**DANGEROUS direction — hard STOP**). The reconstruction must NOT be active where live demonstrably did not fire.
    Test case: trade #1, BUY 2026-07-22 12:00:39 @ 4119.48, closed 17:05:04 @ 4162.69 for +1.272R. Its +0.5R level is
    4136.47; M1 bars pulled from the terminal put the FIRST touch at **16:30 server**, and live did not protect it
    for the following ~30 minutes (about 360 poll cycles) at or above +0.5R. The reconstruction must be INACTIVE
    across [16:30, 17:00) on 2026-07-22. (The only high-impact USD event that day is EIA Crude Oil Stocks 17:30 →
    window [17:00, 17:30]; the position was then closed at 17:05:04 tagged `reconciled_system_close`, which
    `execution/demo_adapter._classify_expert_closed_reason` defines as *this system's own close whose acknowledgment
    was lost* — so that close is itself consistent with protection firing once the window opened at 17:00. This is
    also a correction to EXP-023's reading of that trade: it did not "pass +0.5R with protection never firing"; it
    passed +0.5R during a window that was genuinely INACTIVE, and was closed shortly after the window opened.)
 L2 (**BENIGN direction — report, do not stop, unless the majority miss**). At each live `news_protection` exit the
    reconstruction should be ACTIVE. Known result of the by-hand check: #7 2026-07-29 17:00:03 (EIA 17:30) ok,
    #9 2026-07-30 15:05:40 (Core PCE / GDP / claims 15:30) ok, #11 2026-08-03 16:15:04 (S&P Global Mfg PMI 16:45) ok,
    and **#6 2026-07-28 14:51:15 — NO reconstructed event within 30 minutes, MISS** (that day's only high-impact USD
    event is CB Consumer Confidence 17:00). A miss in this direction means live fired when the reconstruction says it
    should not have, which is exactly the documented fail-safe channel (`news_protection.py`'s module docstring: a
    calendar that cannot be read TRIGGERS protection) and makes the measured rate a LOWER bound — the direction
    already declared in §10. It is therefore reported, not fatal, unless 2 or more of the 4 miss, which would mean
    the reconstruction is simply wrong. Corroborating evidence that the fail-safe channel is real and not rare on
    this account: `blocked_signal_records` in the same journal contains **17 hourly signal evaluations vetoed with
    "economic calendar unavailable for USD -- fail-safe veto"** between 2026-07-23 and 2026-07-27, including one
    unbroken 14-hour outage on 2026-07-27.

### 9. SAMPLE-SIZE HONESTY (rule 6) AND MULTIPLE TESTING (rule 7) — pre-registered, not decided after the fact
The real trigger rate is expected to be **far below** P1's ~60%, so affected@real per window may fall well under the
100-trade floor. Pre-registered handling: **per-year numbers are REPORTED but no deciding statement rests on a single
year**; the deciding statements pool Train (y1+y2+y3) and, separately, Val (y4). If pooled Train affected@real < 100,
this experiment reports **"MEASUREMENT WITH WIDE ERROR BARS"** in exactly those words, prints the SE and the implied
interval, and refuses to convert it into a significance claim. A measurement whose error bars are honestly drawn is
still a result; a manufactured t-statistic is not. Rule 7: nothing is being selected, no threshold can be chosen by
its result, and the two anchor arms are pinned to previously published numbers — so no multiple-testing correction is
warranted and none is applied; that is stated so it cannot later look like an omission.

### 10. DECLARED LIMITATIONS (all known to bias in a stated direction; from the 2026-08-04 NOTE + this design)
 (i) **Current-classification look-ahead.** The dump's `importance` and `event_name` are MetaQuotes' CURRENT
     classification of each event, not what the terminal would have said in 2021. An event reclassified
     moderate→high since then is treated as high for its whole history, and vice versa. Direction unknown; it is a
     genuine (small) look-ahead and is not removable from this data source.
 (ii) **Reschedules erased.** The dump reflects the schedule as it now stands; the live archive
     (`data/db/news_calendar_history.csv`) records what live actually SAW. The one overlap check available measured a
     1/116 divergence, all of it a reschedule. Direction unknown, magnitude about 1%.
 (iii) **Fail-safe episodes are NOT modelled.** Live fires protection whenever the calendar cannot be read (stale
     export file, exporter Service not running, unreadable file) — §8's 17 vetoed evaluations and the unreconciled
     live exit #6 are direct evidence this happens. The reconstruction has no fail-safe channel, so **the measured
     trigger rate is a LOWER BOUND on live's true rate**, and the measured parity cost is a lower bound in magnitude
     too. Quantifying that residual is the accumulating archive's job, not this experiment's.
 (iv) **H1 bar resolution** (§4(e)) — eligibility is bar-granular, live is 5-second granular.
 OBSERVATION recorded for the main session, outside this experiment's scope and NOT used by it: the local copy of the
 live archive `data/db/news_calendar_history.csv` (246 rows, pulled 2026-08-04) contains rows from three collection
 cycles, and the first two (`first_seen_utc` 16:54:27 and 17:32:21) carry event times exactly **3 hours behind** the
 third cycle's, and behind both the dump and the dev PC's own current export file — i.e. they look like UTC rather
 than `TimeTradeServer()`. The plausible mechanism is an MQL5 terminal reporting a zero server offset before its
 first server sync after a restart. If that ever happens while the shadow loop is running, live's news windows would
 be wrong by 3 hours for that period. This is a live-correctness question for `mql5/NewsCalendarExporter.mq5`, not a
 tuning question, and it is flagged rather than chased here.

### 11. WHAT CANNOT FOLLOW FROM THIS EXPERIMENT, AND WHAT IS ESCALATED
Nothing here may change `config/base.yaml` or `src/`. In particular, a large measured parity cost is NOT a licence to
disable news protection: mode C is a REFERENCE ARM describing what the backtest has been measuring, never a candidate,
because switching the control off is the removal of a deliberate risk control. Rule 8 restated: no promotion/demotion
gate, Auditor threshold or circuit-breaker limit is touched, and none will be proposed as a way to make any number
look better. The RESULTS section will close with a recommended ORDER for the three possible follow-ups (model news
protection in `backtest/engine.py`; re-open the min-lot fallback's SKIP-vs-CLOSE_ALL fail-direction, EXP-025 §6(iii),
now that it is measurable; or accept the cost) — as a recommendation with reasons, to be decided by the user, not
here.

### EXP-024 RESULTS (run 2026-08-04) — OUTCOME: MEASURED. Real trigger rate = **25.6% of trades on Train / 28.0% on Val** (vs the P1 ~61% and P2 ~47% brackets EXP-023/025 had to assume). Real parity gap A@real − C = **−0.157R ± 0.090 per affected trade on Train (n=193), −0.154R ± 0.142 on Val (n=71)**. NO config change, NO `src/` change, Test year NOT touched.
Raw output: `experiments/exp024_calendar_out.txt` (calendar + G3), `experiments/exp024_fidelity_out.txt` (G1/G2),
`experiments/exp024_livecv_out.txt` (L1/L2), `experiments/exp024_cond_out.txt` (deciding conditional),
`experiments/exp024_pool_out.txt` (pooled), `experiments/exp024_port_out.txt` (portfolio).
Harness: `experiments/exp024_real_calendar_harness.py`. Dev PC, `.venv` (Python 3.12.10, pandas 3.0.5).
Calendar read IN PLACE from the dump as pre-registered: 73,699 raw rows → 73,353 after dedup (dup rate 0.469%, matching
the NOTE's 0.47%) → 2,617 high-impact USD rows → **1,660 unique normalised event timestamps**, 1,321 of them inside
the Train+Val range.

FIDELITY / CROSS-VALIDATION GATES — all run and reported BEFORE any A@real number was read:
 G1 `--mode fidelity`, 4,000-bar window, 197 trades: the copied bar loop with the news mechanism OFF is IDENTICAL to
    `backtest.engine.run_backtest`, field-for-field, with and without EXP-022's fast-path shim
    (`copy_off_identical=True copy_off_fastpath_identical=True`). **PASS.**
 G2 EXTERNAL anchor: mode C on y4/VAL at $3,000 = **254 trades, PF 1.0961, net +$352.60, maxDD 9.9895%** — EXP-022's
    cap-1.5 y4 cell, re-confirmed by EXP-023 and EXP-025, to the last digit (`match: true`). **PASS.**
 G3 NFP normalisation: **48/48 in-range** high-impact-USD `Nonfarm Payrolls` rows normalise to exactly **15:30 server**,
    zero violations. The single out-of-range violation disclosed in the pre-registration reproduced exactly as
    predicted (`2025-11-20` raw 15:30 → 14:30), and it is in the untouched Test year. **PASS.** Independent
    corroboration that the normalisation is right, not merely self-consistent: the 1,321 in-range event timestamps
    land on the US release grid — 15:30 (419 = 08:30 ET), 17:00 (267 = 10:00 ET), 16:45 (174 = 09:45 ET), 17:30
    (171 = 10:30 ET EIA), 20:00 (99 = 13:00 ET auctions), 21:00 (34 = 14:00 ET FOMC), 21:30 (32 = FOMC presser).
 G4 CONTINUITY ANCHORS vs EXP-023/EXP-025 — the single most important gate, because it proves this is the same
    instrument. **PASS, exactly.** A@P1 affected counts **162/152/149/154** and affected-subset avgR
    **0.501/0.497/0.494/0.495**; A@P2 affected counts **124/116/116/124**. Pooled A@P1 − C = **−0.0655 ± 0.0638
    (Train) / −0.1320 ± 0.1095 (Val)** — EXP-023 §6(i)'s published "−0.066R ± 0.064 / −0.132R ± 0.110" to three
    decimals; pooled A@P2 − C = −0.0707 / −0.1658, the mirror of EXP-025 §6(ii)'s "+0.071R / +0.166R". The A@P1/A@P2
    portfolio cells also reproduce EXP-023 §3 / EXP-025 §3 (y4: 554 trades / PF 0.980 / −$86 / 12.5% and 400 / 1.010 /
    +$39 / 14.0%).
 G5 Conditional-replay self-check (asserted in code, every window): replaying each mode-C trade one-by-one reproduces
    the full-sequence mode-C trade list EXACTLY. **PASS.**
 L1 LIVE, dangerous direction — **PASS, and it is the strongest single piece of evidence in this experiment.** Trade #1
    (BUY 2026-07-22 12:00:39 @ 4119.48, +1.272R) first touched its +0.5R level 4136.47 at **16:30:00** server per the
    terminal's own M1 bars. The reconstruction is INACTIVE for exactly the next **30 minutes** and turns ACTIVE at
    **17:00:00** (EIA Crude Oil Stocks 17:30, window [17:00, 17:30]). Live left that position alone for those same 30
    minutes — roughly 360 five-second poll cycles at ≥ +0.5R — and then it was closed at 17:05:04 by this system's own
    magic (`reconciled_system_close`). The reconstructed window boundary and live's behavioural boundary coincide to
    the minute, from opposite sides. (Corollary, recorded as a CORRECTION: EXP-023's reading of this trade — "it passed
    +0.5R without protection firing, so live must be running a working calendar" — was right about the conclusion and
    wrong about the evidence. It passed +0.5R during a genuinely inactive window and was closed ~5 minutes after that
    window opened; the close is itself most plausibly a news-protection close whose acknowledgment was lost.)
 L2 LIVE, benign direction — **3 of 4, PASS by the pre-registered bar (≥ half).** #7 2026-07-29 17:00:03 → EIA 17:30 ✓;
    #9 2026-07-30 15:05:40 → Core PCE / GDP / Initial Claims 15:30 ✓; #11 2026-08-03 16:15:04 → S&P Global Mfg PMI
    16:45 ✓; **#6 2026-07-28 14:51:15 → nothing within 30 minutes, MISS** (that day's only high-impact USD event is CB
    Consumer Confidence 17:00). Exactly the pre-registered benign direction: live fired where the reconstruction says
    it should not have, which is the documented calendar-unavailable fail-safe, and which makes every rate below a
    LOWER bound. Not a stop.

### 1. MEASUREMENT (1) — THE REAL TRIGGER RATE (this is the number EXP-023 §6(ii) said had to exist before anything else)
```
window            bars  eligible  elig%  | trades  affected@real  rate%  | P1 rate%  P2 rate%
y1 2021-22        5926       322   5.43  |    266             64  24.06  |    60.90     46.62
y2 2022-23        5917       306   5.17  |    254             62  24.41  |    59.84     45.67
y3 2023-24        5894       310   5.26  |    233             67  28.76  |    63.95     49.79
y4 VAL 2024-25    5913       302   5.11  |    254             71  27.95  |    60.63     48.82
POOLED TRAIN      17737      938   5.29  |    753            193  25.63  |    61.49     47.28
POOLED VAL         5913      302   5.11  |    254             71  27.95  |    60.63     48.82
```
Read plainly: **only ~5.2% of all bar-hours are trigger-eligible at all**, and protection reaches **one trade in four**,
not the three in five P1 assumed nor the one in two P2 assumed. The real rate is **0.42× the P1 bracket and 0.55× the
P2 bracket**, and it is remarkably stable across four years (24.1 / 24.4 / 28.8 / 28.0%), with the calendar-side density
stable too (5.11–5.43% of bars). P1 was never meant to be a frequency estimate — it is the fail-safe bound — and this
confirms it overstated frequency by ~2.4×, exactly as EXP-023 D2 warned it would.
AUDIT OF P2's HOUR MASK, now that the real event clock is visible (event timestamps by server hour, in-range):
`{15: 468, 17: 440, 16: 184, 20: 105, 21: 67, 18: 33, 19: 10, 5: 7, 22: 3, 14: 2, 9: 1, 23: 1}`. P2's mask
`{14,15,16,20,21}` captured **62.5%** of real event timestamps while marking ~3× as many bars eligible as the real
calendar does — and it missed hour **17** entirely, which is the second-busiest hour of the entire US release day
(10:00 ET: ISM, JOLTS, Consumer Confidence, New Home Sales). Hour 14 — one of the five hours P2 spent — contains **2**
events in four years. P2 was a reasonable guess and it was substantially wrong; that is worth recording, because P2 was
carried as "robustness" evidence in two experiments.

### 2. MEASUREMENT (2) — THE REAL PARITY GAP (paired, sequence held FIXED, on the affected@real subset)
avgR on each window's affected subset, and the paired per-trade difference A − C (same trades, SE of the DIFFERENCE):
```
                    tot   aff   aff% | C avgR  A avgR |   A - C      SE       t
y1 2021-22          266    64  24.06 |  0.700   0.687 |  -0.0124  0.1598   -0.08
y2 2022-23          254    62  24.41 |  0.876   0.666 |  -0.2106  0.1544   -1.36
y3 2023-24          233    67  28.76 |  0.927   0.680 |  -0.2467  0.1550   -1.59
y4 VAL 2024-25      254    71  27.95 |  0.892   0.738 |  -0.1544  0.1422   -1.09
POOLED TRAIN (n=193)                                  |  -0.1574  0.0902   -1.745   95% CI [-0.334, +0.019]
POOLED VAL   (n= 71)                                  |  -0.1544  0.1422   -1.086   95% CI [-0.433, +0.124]
   for comparison, the same statistic on the PROXY subsets (continuity anchors, EXP-023/025's own numbers):
POOLED TRAIN A@P1 (n=463)                             |  -0.0655  0.0638   -1.026
POOLED VAL   A@P1 (n=154)                             |  -0.1320  0.1095   -1.205
POOLED TRAIN A@P2 (n=356)                             |  -0.0707  0.0688   -1.027
POOLED VAL   A@P2 (n=124)                             |  -0.1658  0.1092   -1.518
```
**The headline, and it is the opposite of what "a much lower trigger rate" would naively suggest: per affected trade the
real cost is ~2.4× the P1 bracket on Train (−0.157R vs −0.066R) and about the same on Val (−0.154R vs −0.132R).** The
sign is negative in all four windows and the magnitude is stable in the three most recent (−0.21 / −0.25 / −0.15R);
y1 is the one flat window. Protection fires four times less often than P1 assumed, and each firing costs about 2.4
times more.
WHY — and this is the real finding of EXP-024. The real-calendar trigger does not select a random 26% of trades; it
selects a **materially better** 26%. Under mode C the affected@real subset earns avgR **0.700 / 0.876 / 0.927 / 0.892**
and reaches the 2R take-profit **48.4 / 54.8 / 56.7 / 56.3%** of the time, versus **0.496 / 0.526 / 0.674 / 0.627** and
~48.7% (y4) for the affected@P1 subset. A +0.5R touch that happens in the 30 minutes *before* a high-impact USD release
is disproportionately part of a directional pre-news move that keeps going. So news protection, as implemented at
min-lot, is not skimming average trades — **it is cashing out the pre-news runners at +0.5R specifically, and those are
the trades most likely to have reached 2R.** Both brackets EXP-023 used missed this, because both were blind to *which*
trades a real calendar picks.
Scaled to every trade rather than every affected trade: −0.157R × 25.6% = **−0.040R per trade on Train**, −0.154R ×
28.0% = **−0.043R per trade on Val**. For context, mode C's own realised avgR on y4/VAL is **+0.0505R per trade**. On a
sequence-held-fixed basis the news-protection control therefore consumes on the order of **80–85% of the strategy's
entire measured per-trade edge on the validation year**. That number is the honest statement of EXP-023 §6(i)'s
"the backtest has never charged the strategy for it", and it is much larger than EXP-023's own bracket implied.

### 3. MEASUREMENT (2b) — PORTFOLIO DELTAS (full sequence; the reshuffling caveat is essential here, see below)
$3,000/window, complete cost model. trades | PF | net$ | maxDD% | PF excl. top-5:
```
window            C_none                              A_real                              A_P1 (anchor)            A_P2 (anchor)
y1 2021-22        266 | 1.016 |   +64 | 14.49 | 0.941  325 | 1.040 |  +167 | 15.11 | 0.968  510 | 1.004 |   +16 | 17.11  416 | 1.030 |  +127 | 12.88
y2 2022-23        254 | 0.995 |   -22 | 26.12 | 0.917  322 | 0.917 |  -387 | 29.04 | 0.846  516 | 0.964 |  -175 | 19.95  396 | 0.897 |  -456 | 23.78
y3 2023-24        233 | 1.202 |  +773 | 12.27 | 1.103  296 | 1.093 |  +370 | 14.27 | 1.012  486 | 1.075 |  +350 | 15.85  385 | 1.115 |  +497 | 11.15
y4 VAL 2024-25    254 | 1.096 |  +353 |  9.99 | 0.987  314 | 1.091 |  +352 | 10.08 | 0.995  554 | 0.980 |   -86 | 12.48  400 | 1.010 |   +39 | 13.97
POOLED TRAIN      753 |       |  +815 |                943 |       |  +151 |                1512 |      |  +192          1197 |      |  +168
POOLED VAL        254 |       |  +353 |                314 |       |  +352 |                 554 |      |   -86           400 |      |   +39
```
Stated with the same honesty EXP-023 §3 used, in both directions:
 * On **Train** the parity gap is material: three years of $3,000 accounts go from **+$815 to +$151** (−$664, −81% of
   the profit), PF falls in y2 (0.995→0.917) and y3 (1.202→1.093), and max DD worsens in every Train window
   (14.5→15.1, 26.1→29.0, 12.3→14.3). PF-excluding-top-5 falls in y2/y3 too.
 * On **Val the portfolio-level gap is essentially ZERO**: PF 1.096 → 1.091, net +$352.6 → +$352.0, maxDD 9.99 → 10.08.
   That is not a contradiction of §2 and it must not be reported as one. The mechanism is visible in the trade counts:
   A@real takes **314 trades where C takes 254** (and 943 vs 753 on Train), because every early news exit frees the
   single position slot and the engine takes the next signal. The per-trade edge is cut, and the reshuffled extra
   trades happen to pay for it on y4 but not on y2/y3. Win rate rises everywhere (0.374→0.478 on y4) precisely because
   many trades that would have been −1R are now banked at +0.5R.
 * This is exactly the non-causal reshuffling EXP-017/020/021 established as unusable for SELECTION. Nothing is being
   selected here, so these numbers are reported as what they are: the portfolio-level statement of the parity gap,
   dominated on any single window by which signals the re-sequenced engine happens to take next. **The deciding
   measurement remains §2's paired, sequence-fixed number**; §3 is the same effect seen through a noisy lens, and the
   honest summary of §3 is "materially negative on Train, neutral on Val, with large sequencing noise either way".

### 4. MEASUREMENT (3) — EXIT MIX ON THE AFFECTED@REAL SUBSET (all four windows; y4/VAL was the pre-registered minimum)
```
y1 (n=64)   C: 31 take_profit (48.4%), 16 stop_loss, 17 time_stop        -> avgR 0.700, medR 0.287, worstR -1.116, 25 negative, PF 3.56
            A: 64 news_protection (100%)                                 -> avgR 0.687, medR 0.551, worstR +0.331,  0 negative, PF inf
y2 (n=62)   C: 34 take_profit (54.8%), 11 stop_loss, 16 time_stop, 1 str -> avgR 0.876, medR 1.794, worstR -1.186, 21 negative, PF 4.89
            A: 62 news_protection (100%)                                 -> avgR 0.666, medR 0.516, worstR +0.313,  0 negative, PF inf
y3 (n=67)   C: 38 take_profit (56.7%), 13 stop_loss, 16 time_stop        -> avgR 0.927, medR 1.802, worstR -1.253, 19 negative, PF 5.36
            A: 67 news_protection (100%)                                 -> avgR 0.680, medR 0.502, worstR +0.322,  0 negative, PF inf
y4 VAL      C: 40 take_profit (56.3%), 14 stop_loss, 17 time_stop        -> avgR 0.892, medR 1.857, worstR -1.134, 21 negative, PF 4.91
   (n=71)   A: 71 news_protection (100%)                                 -> avgR 0.738, medR 0.542, worstR +0.379,  0 negative, PF inf
```
The trade being made is now completely explicit, and it is a genuine risk/return trade, not a mistake: mode A converts
a distribution with a **56% 2R-hit rate and a −1.13R worst case (21 of 71 losers)** into a distribution with **zero
losers and a +0.38R floor**, at a cost of **−0.154R of mean**. The median moves the other way (1.857 → 0.542) — the
median affected trade is a big winner under C and a scratch-plus under A. Compare EXP-023 §2's y4/P1 row (C 48.7% TP,
avgR 0.627): the real-calendar subset is the higher-quality subset on every axis, which is why the same control costs
more when it is aimed by a real calendar than when it is aimed by "every bar".

### 5. SAMPLE-SIZE HONESTY (rule 6) AND MULTIPLE TESTING (rule 7) — per §9 of the pre-registration, applied as written
Per-window affected@real is **64 / 62 / 67 / 71**, i.e. **every individual window is below the 100-trade floor** and no
statement above rests on a single year. Pooled **Train n=193 clears the floor**; pooled **Val n=71 does not**, so, in
the pre-registered words: **the Val figure is a MEASUREMENT WITH WIDE ERROR BARS** — −0.154R with SE 0.142 and a 95%
interval of **[−0.433, +0.124]** that comfortably contains zero. The Train figure's interval is **[−0.334, +0.019]**,
also containing zero at t = −1.745. **Neither pooled estimate is statistically significant at any conventional level,
and this section will not pretend otherwise.** What the data supports is: a consistently negative sign in 4 of 4
windows and in both proxy subsets, a stable magnitude of roughly −0.15R to −0.25R per affected trade in the three most
recent windows, and a Train point estimate 2.4× the previously published bracket. That is a measurement with a
direction and a rough size, not a proof. Rule 7: nothing was selected, no threshold could be chosen by its result, and
the two anchor arms were pinned to previously published numbers before the run — so no multiple-testing correction is
warranted and none was applied. Configs evaluated: 4 / 4 for this new family.

### 6. LIMITATIONS, RE-STATED AGAINST THE ACTUAL RESULT (all four were pre-registered; three bias the same way)
 (i) Current-classification look-ahead: importance/name are MetaQuotes' CURRENT judgement applied to 2021 history.
     Direction unknown. Unremovable from this source.
 (ii) Reschedules erased (~1% observed against the live archive). Direction unknown.
 (iii) **Fail-safe episodes are NOT modelled, and L2's one miss is a live example of one.** Live fires protection
     whenever the calendar cannot be read; the same journal shows 17 hourly evaluations vetoed for
     "economic calendar unavailable for USD" in 13 days, including a 14-hour outage on 2026-07-27. So the true live
     trigger rate is **above** the 25.6% / 28.0% measured here, somewhere between it and P1's 61% fail-safe bound
     depending on exporter uptime, and the true parity cost is correspondingly larger in magnitude. Measuring that
     residual is the accumulating archive's job (`data/db/news_calendar_history.csv`, running since 2026-08-03).
 (iv) H1 bar resolution: eligibility is bar-granular (5.2% of bars), live is 5-second granular. Both directions.
 (v) Unchanged from EXP-023/025 and applying identically to BOTH arms: the engine bakes spread/slippage into the ENTRY
     fill only, so mode A's market close at +0.5R is modelled ~1 spread (~$0.35, ≈0.01–0.03R) too favourably. It is an
     order of magnitude below the effect measured in §2 and it makes A look BETTER than it is, i.e. §2 is if anything
     a slight under-statement of the cost.

### 7. WHAT THIS EXPERIMENT DID NOT DO, AND WHY THAT WAS DELIBERATE
The **Test year 2025-07-22 → 2026-07-21 was NOT touched**; the harness refuses `--window y5*`. This family's one-touch
budget is UNSPENT. The obvious and important question — *what do the promotion-gate numbers (Appendix A §5.2) look like
on Test under real mode A, i.e. what is the honest Test baseline for the configuration actually running live?* — is
therefore still open, by design, and it is a **user-level decision**, not this experiment's to make: it should be spent
once, on the final agreed model of news protection, not on a bespoke harness. Rule 8 restated: no promotion/demotion
gate, Auditor threshold or circuit-breaker limit was touched, and none is proposed for change as a way to make any
number here look better. `config/base.yaml` UNCHANGED (`news_profit_threshold_r` 0.5, `news_window_minutes` 30,
`news_close_mode` half); `src/` UNCHANGED.

### 8. ESCALATION — recommended ORDER with reasons (a recommendation only; nothing is decided or changed here)
 **(1) FIRST: model news protection in `backtest/engine.py` permanently (engine change, separate task, user decision).**
 It is the only one of the three that is a pure HONESTY change rather than a strategy change: it alters no live
 behaviour and adds no risk, and it retires a whole class of error this project has already been bitten by once
 (EXP-018's swap gap, same shape — a real cost the backtest simply did not charge). Until it lands, every future
 experiment keeps measuring mode C by default, which is now known to overstate per-trade edge by ~0.04R on both Train
 and Val. It also has to land BEFORE any honest Test baseline is produced, so that the one Test touch is spent on
 production code rather than on an experiment harness. Concretely it needs: an optional `NewsCalendarProvider` on
 `BacktestConfig`, the eligibility rule of §4(e) of the pre-registration, and a `cost_model_complete`-style honesty
 flag that says whether the run modelled news protection.
 **(2) SECOND, and only after (1): re-open the min-lot fallback fail-direction, SKIP vs CLOSE_ALL (EXP-025 §6(iii)).**
 It is now measurable for the first time, and §2/§4 say exactly why it is the only remaining mechanism with a plausible
 upside: the control is cashing out a subset that hits 2R 56% of the time, so the choice between "close the whole
 position" and "skip protection when half-volume is impossible" is worth roughly 0.15R on 26% of trades ≈ 0.04R per
 trade. But it is a risk-control WEAKENING direction — it removes the only reason the affected subset currently has a
 zero-loser, +0.33R floor — so it needs its own pre-registration, an explicit tail criterion, and a user decision about
 risk appetite, not just a favourable mean. It must NOT be run as another grid on the same four windows without that.
 **(3) THIRD / DEFAULT: accept the cost.** This is a legitimate outcome and deserves to be stated as such rather than
 treated as a failure: on the validation year the portfolio-level cost is nil (PF 1.096 → 1.091, +$353 → +$352), the
 control converts a 30% loser rate on the affected subset into zero losers, and the whole Train-side gap is at
 t = −1.75 — real-looking, not proven. "We know what it costs, we priced it, we keep it" is a defensible end state, and
 it is strictly better than today's state, in which the cost was neither known nor charged.
 Explicitly NOT recommended: switching news protection off. Mode C was carried here as the reference arm describing
 what the backtest has always measured, never as a candidate.
 Also escalated, separately, as a LIVE-CORRECTNESS item rather than a tuning one: §10's observation that the live
 archive's first two collection cycles carry event times 3 hours behind server time (UTC-looking), which if it happens
 while the shadow loop is running would put live's news windows 3 hours out. Worth one look at
 `mql5/NewsCalendarExporter.mq5`'s behaviour immediately after a terminal restart.

## NOTE (not an EXP) 2026-08-04 — News protection is now MODELABLE in backtest/engine.py (mode A from the real calendar); one fidelity refinement over EXP-023/024/025's harnesses
Implements EXP-024's escalation (i). `BacktestConfig` gains `news_protection_cfg` + `news_calendar` (default `None` =
"not modeled", the engine's standard honesty convention — verified bit-for-bit against the mode-C y4 anchor
254 / 1.0961 / +$352.60 / 9.99%), `scripts/run_backtest.py` gains `--model-news-protection` /
`--news-calendar-path` and a `news_protection_modeled` envelope flag (NOT folded into `cost_model_complete` — it is
not a cost-model item). `scripts/build_backtest_calendar.py` builds the canonical normalised calendar
(`data/historical/news_calendar_backtest.csv`, gitignored/regenerable) from the MT5 dump + live archive with the
US-DST −1h rule, the NFP=15:30 self-check, and quarantine of the archive's UTC-skewed restart rows. The engine calls
`check_news_protection()` verbatim (never reimplements) with the EXP-024 per-bar priority and trigger-price
conventions. +43 tests (1531 total); reviewed.

**Fidelity refinement, disclosed loudly:** EXP-023/024/025's harnesses all simulated the min-lot CLOSE_ALL
degeneration ONLY — they never modeled the genuine CLOSE_HALF_AND_BREAKEVEN branch that live executes whenever
half-lot ≥ volume_min (which does occur: lots are not always 0.01 — the live journal itself has a 0.02-lot trade).
The engine models the genuine partial close (half closed at the trigger price, remainder's stop to breakeven,
re-trigger suppression window honoured). Consequence on y4/VAL with the real calendar: engine A@real =
**350 trades / PF 1.0667 / +$259.67 / maxDD 11.42%** vs EXP-024's harness A@real 314 / 1.091 / +$352 / 10.08%;
forcing the engine to full-close-always reproduces ~the harness numbers (320 trades), isolating the difference to the
partial-close branch, not a bug. Read: EXP-024's measured parity cost is a mild UNDER-estimate for the
larger-than-min-lot subset; its conditional A−C treatment estimates and trigger-rate measurements are unaffected
(trigger logic identical). Portfolio-level numbers remain reshuffling-dominated (veto-only evidence, per EXP-023 D3).
The honest-Test-baseline question EXP-024 deferred to the user should be run with THIS engine path when taken up.

## MEASUREMENT (not an EXP) 2026-08-04 — HONEST TEST BASELINE: the deployed config on the Test year WITH news protection modeled (user-authorised ONE touch, for measurement)
Status: MEASURED -- Gate 1 FAILED in both arms (mode C PF 1.1903; honest mode A@real PF 1.0845 and PF_ex_top5 0.9866); see `### TASK-1 RESULTS`. Originally: PRE-REGISTERED (results pending). Everything from this line down to `### TASK-1 RESULTS` was written and
COMMITTED **before any Test-year number was produced**; only the RESULTS section and this Status line are added
afterwards.

### 0. WHAT THIS IS, AND WHAT IT IS NOT (framed exactly as EXP-006 §1 framed its own "where we stand" baseline)
This is a **MEASUREMENT of the CURRENTLY DEPLOYED configuration**, not a parameter candidate. Nothing is swept, there
is no grid, no arm can "win", and no `config/base.yaml` or `src/` change can follow from it. Its whole job is to answer
the question EXP-024 §7 deliberately left open and escalated to the user: *what do the promotion-gate numbers look like
on the held-out Test year once the strategy is charged for the news-protection cost it actually pays live?* Every
Gate-1 figure this project has ever produced — including the arithmetic EXP-008 was adopted on and EXP-022's y5 cell —
is a **mode C** run (news protection genuinely unmodeled, EXP-023 D1), while live runs **mode A**.
**Therefore, and stated explicitly because it is the whole reason this is legitimate: this touch does NOT consume any
tuning family's one-touch Test budget.** Same precedent and same wording as EXP-006 §1 ("a 'where we stand'
measurement, NOT a parameter search, so it does NOT spend the family's one-touch Test budget"). In particular
EXP-023's family ("news-protection min-lot fallback") keeps its UNSPENT one-touch budget for EXP-026 below, and
EXP-025's and EXP-024's families are likewise untouched by this. The user explicitly authorised this measurement.

### 1. EXACTLY TWO RUNS, PRE-REGISTERED (no third run, no re-run "with a tweak")
| id | what | role |
|----|------|------|
| **C** | news protection NOT modeled (`news_protection_cfg=None`) | ANCHOR — must reproduce the recorded Test figures for this exact context |
| **A@real** | news protection modeled from the built historical calendar, live semantics | **THE HONEST BASELINE** |

Both via the REAL production path: `experiments/task1_test_baseline_driver.py`, a thin driver over
`scripts/run_backtest.py`'s own `run_and_persist`/`build_envelope` -> `backtest.engine.run_backtest` ->
`backtest/report.py` -> the JSON envelope `auditor/promotion.py` reads. **NOT the exp023/024/025 harness family**: per
the 2026-08-04 engine NOTE, only the engine models the genuine `CLOSE_HALF_AND_BREAKEVEN` partial-close branch live
executes whenever half-lot >= `volume_min`, so the engine path is the live-faithful one and is the only defensible
basis for a promotion-gate number. Declared deviation from the CLI, the only one: `SymbolSpec` is the hardcoded IC
Markets XAUUSD spec `experiments/exp022_minlot_harness.py` already uses (identical to what the CLI resolves from a
live MT5 session), so no terminal is required.

### 2. WINDOW, ACCOUNT CONTEXT, COST MODEL (the EXP-022+ context, unchanged, so the anchor is meaningful)
Test year **2025-07-22 -> 2026-07-21** (`y5_TEST_2025-26`, the same window EXP-022 §2 used). $3,000 starting equity,
per-year anchored; `cfo.min_lot_risk_cap_pct` 1.5; `cfo.risk_per_trade_pct` 1.0; all-24h session; `breakeven_enabled`/
`trail_enabled` false; `order.tp_r_multiple` 2.0; `global.swing_pivot_bars` 3. COST MODEL COMPLETE: slippage =
min-1-spread (`slippage_points=None`) AND swap modelled (EXP-018 rates long -53.2 / short +36.8, 3x Wed); commission
$0.00 (IC Markets Standard, the real account). `watchman.news_profit_threshold_r` 0.5, `news_window_minutes` 30,
`news_close_mode` half — i.e. `config/base.yaml` exactly as adopted, nothing overridden.

### 3. THE ANCHOR NUMBER THIS MUST REPRODUCE, CITED BEFORE THE RUN (an anchor is only an anchor if it is named first)
The only Test-year cell on record at this exact context is **EXP-022 §2's y5 cap-1.5 column**: **228 trades, PF 1.1903,
PF_ex_top5 1.067, net +$845.3, maxDD 12.39%** (worst single loss 1.94% of then-equity). Mode C must reproduce it. Two
further external anchors were run FIRST, on the already-consumed y4/VAL window (never on Test), and both reproduced
exactly before this pre-registration was written — disclosed here in full, including the ordering:
 * y4 mode C = **254 / PF 1.0961 / +$352.60 / maxDD 9.9895%** (EXP-022's cap-1.5 y4 cell, re-confirmed by EXP-023/024/025).
 * y4 A@real via the ENGINE = **350 / PF 1.0667 / +$259.67 / maxDD 11.4154%** (the 2026-08-04 engine NOTE's own
   published engine figure, which differs from EXP-024's harness 314/1.091/+$352/10.08% precisely because the engine
   models the genuine partial close).
 * FIDELITY: EXP-022's fast-path memoisation shim re-proved trade-for-trade identical to the unshimmed engine on a
   4,000-bar window, with news OFF (176 trades) **and** with news ON (242 trades) — `--mode fidelity`, `identical: true`.

### 4. WHAT IS REPORTED (fixed now, so nothing can be selected after the fact)
For BOTH arms: trades / PF / net$ / maxDD% / avgR / pf_ex5 / win rate / count of `news_protection` exits; the delta;
and the Gate-1 reading computed by the REAL `auditor/promotion.evaluate_backtest_to_paper_gate` against
`config/base.yaml`'s own `auditor.promotion` block (`backtest_min_profit_factor` 1.3, `backtest_max_drawdown_pct` 15.0,
`backtest_min_trade_count` **200** — note: 200, not 100; 100 is Gate 2's paper trade-count floor —
`backtest_min_profit_factor_excluding_top_5` > 1.0, plus the hard-fail flags `is_out_of_sample`/`cost_model_complete`/
`risk_voice_modeled`/`watchman_exits_modeled`/`shield_modeled`). The verdict sentence is stated plainly either way:
**whether the deployed strategy passes or fails its own promotion floor once its real news-protection cost is charged.**
**Rule 8, restated and binding: no gate, Auditor threshold or circuit-breaker limit is touched, and none will be
proposed for change as a way to make either number pass.** A FAIL is a publishable result, not a problem to be fixed by
moving the bar.

### 5. DECLARED LIMITATIONS, ALL KNOWN BEFORE THE RUN (three inherited from EXP-024 §6, one NEW and specific to Test)
 (i) **NEW, and in range for the first time: the one bad NFP normalisation is INSIDE this window.** EXP-024's G3
     disclosed that across the full dump exactly one high-impact-USD `Nonfarm Payrolls` row does not normalise to
     15:30 server: **2025-11-20 -> 14:30**, i.e. stamped 1h EARLY relative to the bar clock. That date sits inside the
     Test year, so for THIS measurement it is in range. `scripts/build_backtest_calendar.py`'s self-check is
     pre-registered over 2021-07-22..2025-07-22 (Train+Val) only and therefore reports it as non-fatal. Magnitude
     bound, stated before the run: 1 event out of **303 unique high-impact-USD event timestamps inside the Test year**
     (472 rows), affecting at most the ~1-2 H1 bars whose eligibility that single 30-minute window decides. Reported,
     not silently accepted; it cannot plausibly move a year-level PF.
 (ii) Fail-safe episodes are NOT modelled (EXP-024 §6(iii)): live fires protection whenever its calendar read fails,
     so A@real is a **LOWER BOUND** on live's true trigger rate, and the measured cost is a lower bound in magnitude.
 (iii) Current-classification look-ahead and erased reschedules (EXP-024 §6(i)/(ii)): importance/name are MetaQuotes'
     CURRENT judgement applied to history; ~1% divergence observed against the live archive. Direction unknown.
 (iv) H1 bar resolution: eligibility is bar-granular, live is 5-second granular; exits fill nominally (spread/slippage
     is baked into the ENTRY only), which makes mode A's market close ~1 spread too favourable in BOTH arms.
 (v) The live archive `data/db/news_calendar_history.csv` on this dev PC is a stale local copy (VPS-owned) and every
     row it contributes is dated 2026-08, i.e. **outside the Test window entirely** — it cannot affect either number.

### TASK-1 RESULTS (run 2026-08-04) — OUTCOME: MEASURED. The deployed config **FAILS its own Gate-1 promotion floor on the Test year in BOTH arms**, and charging the real news-protection cost makes the failure WIDER, not narrower: PF **1.1903 -> 1.0845** (floor 1.30), net **+$845.29 -> +$429.27** (-49%), and `profit_factor_excluding_top_5` **1.0666 -> 0.9866**, i.e. a SECOND Gate-1 criterion flips from pass to fail. NO config change, NO `src/` change, NO gate touched.
Raw output: `experiments/task1_test_out.txt` (gitignored). Driver: `experiments/task1_test_baseline_driver.py`.
Envelopes (the real `auditor/promotion.py` input): `data/db/backtest_reports/XAUUSD_TASK1_y5_TEST_2025-26_{C,A}.json`.
Dev PC, `.venv` (Python 3.12.10). Calendar built this session by `scripts/build_backtest_calendar.py`:
dump **73,699** rows + live archive **247** rows (**109 quarantined** for the 3h UTC-skew restart bug, 138 kept) ->
**73,356** rows written to `data/historical/news_calendar_backtest.csv`; **2,617** high-impact-USD rows / **1,660**
unique event timestamps overall, of which **472 rows / 303 unique timestamps** fall inside the Test year. NFP
self-check: **0 in-range violations** (in-range = the script's pre-registered Train+Val range), **1 out-of-range**
reported non-fatal — `2025-11-20 -> 14:30`, which is limitation §5(i) above and IS inside this window.

ANCHOR / FIDELITY (all passed BEFORE the A@real number was read; the y4 pair passed before the pre-registration was even written):
 * **Mode C on Test reproduces EXP-022 §2's y5 cap-1.5 cell to the cent: 228 trades / PF 1.1903 / +$845.29 / maxDD
   12.3886% / PF_ex_top5 1.0666** vs the recorded 228 / 1.1903 / +$845.3 / 12.39% / 1.067. The whole stack (engine,
   cost model, swap, sizing, Shield cooldown, account context) reproduces this log's own Test-year history exactly.
 * y4/VAL mode C = 254 / 1.0961 / +$352.60 / 9.9895% (EXP-022/023/024/025's shared anchor). PASS.
 * y4/VAL A@real via the ENGINE = 350 / 1.0667 / +$259.67 / 11.4154% — the 2026-08-04 engine NOTE's published figure,
   to the cent. PASS.
 * Fast-path shim identical to the unshimmed engine trade-for-trade, news OFF (176 trades) and news ON (242). PASS.

### 1. THE TWO ROWS (Test year 2025-07-22 -> 2026-07-21, $3,000 per-year anchored, complete cost model, commission $0)
```
arm                        trades    PF     net$    maxDD%   avgR    pf_ex5   winRate  news_protection exits
C  (news OFF, the anchor)     228  1.1903  +845.29  12.3886  0.1227  1.0666    0.4211    0
A@real (news ON, HONEST)      292  1.0845  +429.27  13.8509  0.0686  0.9866    0.4692   59
delta A - C                   +64  -0.1058  -416.02  +1.4623 -0.0541 -0.0800  +0.0481  +59
```
Reading the trade-count row honestly: the engine records a genuine partial close as its own `ClosedTrade`, so A@real's
292 records = positions + partial-close records, and the number of POSITIONS is therefore between **233 and 292**
(292 minus at most all 59 protection actions). Both bounds clear Gate-1's 200-trade floor, so the gate reading is
robust to that ambiguity. **The A-arm subset was deliberately NOT decomposed into min-lot full closes vs genuine
partial closes, and the affected trades' outcomes were deliberately NOT extracted** — that decomposition is exactly
EXP-026's treated subset, and computing it here, before EXP-026 is pre-registered, would contaminate the design of a
criterion that may later spend that family's one-touch Test confirmation. Frequency sanity check only: 59 protection
actions against >=233 positions is <=25%, consistent with EXP-024's measured 25.6% (Train) / 28.0% (Val).

### 2. GATE 1 (Backtest -> Paper, Appendix A §5.2), computed by the REAL `auditor/promotion.evaluate_backtest_to_paper_gate`
Thresholds injected from `config/base.yaml`'s own `auditor.promotion` block, unmodified: PF >= **1.3**, maxDD <=
**15.0%**, trades >= **200** (not 100 — 100 is Gate 2's PAPER trade-count floor), PF_ex_top5 > **1.0**, plus the
hard-fail flags. Both runs cleared every hard-fail flag (`is_out_of_sample` true — the Test year is genuinely
held-out; `cost_model_complete` true; `risk_voice_modeled`/`watchman_exits_modeled`/`shield_modeled` all true), so
both were evaluated on the four real criteria:
```
criterion                        C (news OFF)              A@real (HONEST)
profit_factor          >= 1.3    1.1903   FAIL             1.0845   FAIL
max_drawdown_pct       <= 15.0   12.3886  pass             13.8509  pass
trade_count            >= 200    228      pass             292      pass
profit_factor_ex_top5  >  1.0    1.0666   pass             0.9866   FAIL
GATE 1                           FAILED                    FAILED (on TWO criteria)
```
**THE VERDICT SENTENCE, stated plainly as pre-registered: the currently deployed strategy DOES NOT pass its own
Backtest -> Paper promotion floor on the held-out Test year — not before charging the news-protection cost, and by a
wider margin after.** The distance to the PF floor roughly doubles (-0.110 -> -0.216), and the top-5 dependency test
flips: with the 5 best trades removed the honest run is **no longer profitable** (0.9866 < 1.0), which is precisely the
"does this edge survive without its luckiest handful of trades" question Appendix A §5.2 asks. Rule 8: no threshold was
touched, and none is proposed for change. The gate does not move; the strategy would have to.

### 3. HOW THIS RELATES TO THE PREVIOUSLY MEASURED PARITY GAP (consistency check, no new claim)
EXP-024 measured the parity cost at **-0.157R +- 0.090 per affected trade (Train)** / **-0.154R +- 0.142 (Val)**, and
scaled it to ~-0.040R / -0.043R per trade, i.e. ~80-85% of mode C's own per-trade edge on the validation year. The
Test year lands in the same place from an independent window and through the live-faithful ENGINE path (which EXP-024's
harness could not use): per-trade edge **0.1227R -> 0.0686R, i.e. 44% of the strategy's measured per-trade expectancy
is consumed by the news-protection control**, and total net profit halves. The portfolio-level direction that was
ambiguous on y4 (where the reshuffled extra trades happened to pay for the cost exactly: +$352.6 -> +$352.0) is
**unambiguously negative on Test**. Win rate rises (42.1% -> 46.9%) for the same mechanical reason EXP-024 §3
identified: trades that would have run to -1R are banked at +0.5R instead. This is a consistency check across
independent windows, not a new significance claim — and note the two arms differ by 64 trade records, so it carries
EXP-017/020/021's reshuffling caveat exactly as EXP-024 §3 did.

### 4. WHAT MAY AND MAY NOT FOLLOW FROM THIS
May: it is now known, and on the record, what the deployed configuration actually scores out-of-sample when charged for
the risk control it really runs. May NOT: (a) no threshold moves (rule 8); (b) this is NOT an argument for switching
news protection off — mode C is the reference arm describing what the backtest has always measured, never a candidate;
(c) no `config/base.yaml` or `src/` change follows; (d) the Test year is now measured for THIS purpose only — the
budget statement is unchanged: **EXP-023's "news-protection min-lot fallback" family one-touch Test budget remains
UNSPENT** (see EXP-026 below), because this measurement selected nothing.

## EXP-026 2026-08-04 — News-protection min-lot fallback: **SKIP vs CLOSE_ALL** (CONTINUATION of EXP-023's family "news-protection min-lot fallback"; the fail-direction EXP-025 §6(iii) and EXP-024 §8(2) both deferred)
Status: NOT RECOMMENDED -- REJECTED as a recommendation (paired effect flat-to-negative, per-year signs flip) with an honest sub-verdict of INSUFFICIENT on the mean (every window below rule 6's floor; pooled Train n=92). No config change, no src/ change, Test year NOT touched, family one-touch budget still UNSPENT. See `### EXP-026 RESULTS`. Originally: PRE-REGISTERED (results pending). Everything from this line down to `### EXP-026 RESULTS` was written and
COMMITTED, together with a results-free `experiments/exp026_minlot_skip_harness.py`, **before any B_skip outcome number
existed**; only the RESULTS section and this Status line are added afterwards.
Scope: Train (y1/y2/y3) + Val (y4). **The Test year 2025-07-22 -> 2026-07-21 is NOT touched unless every acceptance bar
in §6 clears first** — the harness refuses `--window y5*` without an explicit `--allow-test`. This family's one-touch
Test budget is **UNSPENT** (EXP-023 §5, re-confirmed by EXP-025 §5 and EXP-024 §7; the 2026-08-04 honest-Test-baseline
MEASUREMENT above did not spend it, because it selected nothing). **Whatever the verdict, ADOPTION is a separate
user-level risk-appetite decision: this experiment ends at RECOMMEND / REJECT / INSUFFICIENT, and changes no
`config/base.yaml` value and nothing under `src/`.**

### 0. THE MECHANISM, AND WHY IT IS THE LAST UNTESTED ONE IN THIS LINE
Live's `watchman/loop.py::_act_on_news_decision`, on a `CLOSE_HALF_AND_BREAKEVEN` decision, calls
`_half_volume_rounded(position.volume, spec)`; when half the volume rounds below the broker's `volume_min` it returns
`None` and the branch **recurses into `CLOSE_ALL`** — the whole position is closed at ~+0.5R. The code comment states
the reason in full: *"closing the WHOLE position instead (still risk-reducing) rather than skipping protection
entirely"* — a fail-DIRECTION chosen on instinct, never measured. At a $3,000 account in the current gold regime this
is not a corner case: it is the norm for the smallest-lot trades (EXP-022 §1).
**Candidate B_skip:** when, and ONLY when, half-lot < `volume_min`, SKIP the protection action entirely — position
untouched, stop unmoved, no `news_protected_until` suppression recorded, re-checked on the next trigger exactly as
live's ~5s loop would. Trades whose half-lot IS a valid lot are **identical in both arms** (the engine's genuine
partial-close branch runs untouched), so the treatment is confined by construction to min-lot trades.
This is a distinct question from EXP-023's (which changed the ACTION to a locked stop and was rejected) and from
EXP-025's (which changed the trigger LEVEL and was rejected). It is the one EXP-024 §8(2) ranked as "the only remaining
mechanism with a plausible upside", and it is explicitly flagged there as a **risk-control WEAKENING direction**: it
removes the only reason the affected subset currently has a zero-loser, +0.33R floor. Hence §6(c)'s mandatory,
independently-decisive tail criterion.

### 1. ARMS (three; one mechanism, no parameter is swept anywhere in this experiment)
| id | what | role |
|----|------|------|
| **A_live** | current behavior: min-lot -> CLOSE_ALL | BASELINE (= what runs live today) |
| **B_skip** | min-lot -> SKIP protection; everything else identical | **THE CANDIDATE** |
| C | news protection not modeled | REFERENCE BOUND ONLY — not a candidate (removing a deliberate risk control is out of scope, EXP-023/024/025 precedent) |
Configs evaluated (this exp / cumulative for THIS family): **3 / 23** (EXP-023 burned 20 on the sibling lock-SL
question, same windows, same data, same searcher). Rule 7 at N=23: required edge **1.7 SE**, the same level EXP-023 and
EXP-025 used. No threshold anywhere in this experiment can be chosen by its result.

### 2. THE ENGINE PATH, AND WHY IT WAS CHOSEN OVER ADAPTING THE exp024 HARNESS (declared before results, as required)
This experiment drives the REAL `backtest.engine.run_backtest`, with B_skip installed as a **monkeypatched decision
seam** over `engine._act_on_news_decision` (`experiments/exp026_minlot_skip_harness.py`'s `MinLotSeam`). Nothing under
`src/` or `config/` is modified. Reason: per the 2026-08-04 engine NOTE, EXP-023/024/025's harnesses model the min-lot
CLOSE_ALL degeneration ONLY and never the genuine partial close, so their arm-level sequences diverge from live for
every larger-than-min-lot trade (the NOTE measured the difference on y4: engine 350 trades / PF 1.0667 vs harness 314 /
1.091). Because those non-min-lot trades are exactly the ones that behave IDENTICALLY in both arms here, a full-close-
only harness would still give the right *treatment* estimate — but it would give the wrong *portfolio* rows for
criterion (d) and the wrong denominator for the tail comparison. The engine path has no such defect, so it is used.
The conditional replay calls the engine's own `check_exit` / `_step_news_protection` / `evaluate_watchman` /
`_close_trade` per bar; only the bar-loop skeleton (which contains no decision) is local.

### 3. THE DECIDING METRIC (trade-matched, conditional, sequence held FIXED — EXP-023 D3 / EXP-025 E1, unchanged)
`max_positions_per_symbol: 1` means any later exit reshuffles which signal is taken next, and EXP-017/020/021
established that reshuffling noise dominates portfolio deltas. So:
 * TREATED SUBSET = positions on which arm A_live's replay performs **at least one min-lot degeneration**, i.e.
   precisely the positions on which the two arms can differ at all. Identified identically in both arms by the seam.
 * DECIDING METRIC = the **paired per-position R difference `R(B_skip) − R(A_live)`** on that subset, same entry bar,
   same fill, same lot, same metadata, same subsequent bars; SE of the DIFFERENCE. Position R = the sum of the net P&L
   of all of that position's `ClosedTrade` records (a genuine partial close emits two) over its ORIGINAL risk amount.
 * Full-sequence portfolio runs are reported as **VETO-ONLY** evidence (criterion (d)), never as selection evidence.
 * Structural assertion, checked in code every window: positions with ZERO min-lot degenerations must produce
   BIT-IDENTICAL outcomes in both arms (`untouched_identical`). If that ever fails, the seam is not confined to the
   mechanism it claims to change and the experiment stops.

### 4. WINDOWS, ACCOUNT CONTEXT, COST MODEL (identical to EXP-022/023/024/025 and to the Task-1 measurement above)
Train y1 2021-07-22→2022-07-21, y2 2022-07-22→2023-07-21, y3 2023-07-22→2024-07-21; Val y4 2024-07-22→2025-07-21.
$3,000 per-year anchored, `min_lot_risk_cap_pct` 1.5, `risk_per_trade_pct` 1.0, all-24h, be/trail OFF, tp 2.0, pivot 3.
COST MODEL COMPLETE: slippage = min-1-spread (`slippage_points=None`), swap modelled (EXP-018 long −53.2 / short
+36.8, 3× Wed), commission $0.00. News protection from the calendar built this session
(`data/historical/news_calendar_backtest.csv`), `news_profit_threshold_r` 0.5, `news_window_minutes` 30,
`news_close_mode` half — `config/base.yaml` exactly as adopted.

### 5. FIDELITY GATES — ALL RUN AND REPORTED BEFORE THIS PRE-REGISTRATION WAS WRITTEN (full disclosure of ordering)
 G1 fast-path shim identical to the unshimmed engine trade-for-trade, news OFF (176 trades) and news ON (242). **PASS.**
 G2 y4/VAL mode C = **254 / PF 1.0961 / +$352.60 / maxDD 9.9895%** — EXP-022's cap-1.5 y4 cell, re-confirmed by
    EXP-023/024/025. **PASS.**
 G3 y4/VAL A_live = **350 / PF 1.0667 / +$259.67 / maxDD 11.4154%** — the 2026-08-04 engine NOTE's published engine
    figure, to the cent. **PASS.**
 G4 conditional-replay self-check on y4/VAL: replaying every A_live position one-by-one reproduces the full-sequence
    A_live trade list **field-for-field (350 == 350, identical)**. **PASS.**
 **DISCLOSURE, because the reader is entitled to know what was visible before the bars below were set:** G3/G4 also
 printed y4's seam counts — **65 min-lot degenerations and 40 genuine partial closes** out of 105 protection actions.
 So the y4 treated-subset SIZE (<= 65, i.e. below rule 6's 100-trade floor) was known before §6 was written. No
 OUTCOME number (no R, no PF, no paired difference for either arm on the treated subset) was computed or seen. That a
 treated subset would be small was in any case predictable from EXP-024 (affected@real 64/62/67/71) and from EXP-022 §3
 (the min-lot regime barely exists before 2024) — which is exactly why §6(e) is written as an asymmetric rule that can
 block a RECOMMEND but cannot manufacture one.

### 6. ACCEPTANCE CRITERIA — ALL must hold to RECOMMEND B_skip (set before any B_skip outcome number existed)
 (a) **TREATMENT EFFECT.** Pooled paired mean `R(B_skip) − R(A_live)` on the treated subset must exceed **+1.7 SE** of
     that paired difference on **Train (y1+y2+y3) AND on Val (y4)**. Both, not either.
 (b) **PER-YEAR SIGN CONSISTENCY.** The paired mean difference must have the same sign in every window that contains
     any treated position — no flips. (Windows with zero treated positions are reported as empty, not as agreement.)
 (c) **TAIL — MANDATORY, AND DECISIVE ON ITS OWN.** B_skip re-exposes treated positions to full SL risk, an outcome
     A_live makes arithmetically impossible (its floor is the trigger price, ~+0.3R; A's loser count is expected to be
     0 by construction and will be MEASURED, not assumed). Reported per window and pooled: loser count and loser RATE
     under both arms, worst single R under both arms, count below −1.0R, and the paired downside distribution (count
     of negative differences, 5th percentile of the difference, worst single difference).
     **"Materially fat" is defined numerically, here, before results — any ONE breach REJECTS regardless of the mean:**
      * **c1** B's loser rate on the treated subset exceeds the SAME window's full-portfolio loser rate under A_live by
        more than **5 percentage points**. (Rationale: every treated position was at ≥+0.5R at some point, so it should
        lose LESS often than an ordinary trade, never materially more. y4's A_live portfolio loser rate is 0.480 and
        mode C's is 0.626 — both already on record from G2/G3.)
      * **c2** B's worst single treated-position R is worse than the SAME window's worst single R under A_live's full
        sequence (y4: −1.3456). This tests for a NEW KIND of loss, not merely a nonzero one: B may hand a treated trade
        the ordinary −1R stop-out the strategy already takes everywhere else, but it may not produce something worse
        than the book's own worst case.
      * **c3** More than **5%** of treated positions under B end below **−1.0R** (i.e. worse than a clean stop-out —
        gap/slippage beyond the stop, the genuinely new risk A_live's certain exit removes).
 (d) **PORTFOLIO NON-DEGRADATION (veto-only, never selection).** Full-sequence PF and max DD under B_skip within
     **~15% relative** of A_live on every Train window and on Val.
 (e) **SAMPLE HONESTY (rule 6), stated asymmetrically and deliberately.** Treated-subset sizes are reported per window.
     Deciding statements pool Train and, separately, Val. **If pooled Train treated < 100, this experiment may NOT
     issue a RECOMMEND at all** — the verdict is then at most INSUFFICIENT, the numbers are reported in the
     pre-registered words **"MEASUREMENT WITH WIDE ERROR BARS"** with SE and interval printed, and no significance
     claim is made. A REJECT remains available on (c) or (d), because the status quo needs no new evidence and a change
     does. A Train+Val pooled figure may be printed as DESCRIPTION only; it can never be the basis of a decision
     (that would break the split).
 (f) **TEST CONFIRMATION — only if (a)–(e) ALL clear, else the Test year is NOT touched** (EXP-008 §6.4 pattern).
     Pre-registered now so it cannot be invented later: ONE run of A_live and B_skip on `y5_TEST_2025-26`, same harness,
     same context, `--allow-test`, no re-runs and no variants. Confirmation requires ALL of: (i) the paired mean
     difference on the Test treated subset is POSITIVE, with the same sign as Train and Val; (ii) all three tail bars
     c1/c2/c3 hold on Test; (iii) portfolio PF and max DD within ~15% of A_live on Test. Anything else = NOT confirmed,
     reported as such. The result is reported whatever it is; a spent touch is never un-spent.
 Walk-forward: the 4 windows ARE the rolling confirmation for (b); no separate tooling exists in this repo.
 **Rule 8:** no promotion/demotion gate, Auditor threshold or circuit-breaker limit is touched, and none will be
 proposed for change as a way to make any result pass — including the Gate-1 floors the Task-1 measurement above just
 showed the deployed config failing.

### 7. INHERITED LIMITATIONS (unchanged, all pre-declared)
 (i) Fail-safe episodes are not modelled: live fires protection whenever its calendar read fails, so the treated subset
     measured here is a LOWER BOUND on the set live actually treats (EXP-024 §6(iii)). Direction: B_skip's real-world
     footprint is LARGER than measured here.
 (ii) Current-classification look-ahead + erased reschedules in the dump (~1%); direction unknown.
 (iii) H1 bar resolution: eligibility is bar-granular, live is 5-second granular.
 (iv) Exits fill nominally (spread/slippage baked into the ENTRY only), so A_live's market close at ~+0.5R is modelled
     ~1 spread (≈0.01–0.03R) too favourably — a bias that flatters the BASELINE, i.e. it works against the candidate.
 (v) The 2025-11-20 NFP normalisation defect is inside the TEST year only; it is irrelevant to Train/Val and would be
     re-declared if (f) is ever earned.

### EXP-026 RESULTS (run 2026-08-04) — VERDICT: **NOT RECOMMENDED.** The paired effect is flat-to-negative on Train (−0.029R ± 0.077, t −0.38) and indistinguishable from zero on Val (+0.027R ± 0.129, t +0.21); per-year signs FLIP (−/+/−/+); and every window's treated subset is below rule 6's floor (32/36/24/65, pooled Train **n=92 < 100**), so a RECOMMEND was unavailable by pre-registration §6(e) even before the numbers. **B_skip is REJECTED as a recommendation; the honest sub-verdict on its mean effect is INSUFFICIENT.** `config/base.yaml` UNCHANGED, `src/` UNCHANGED, no gate touched (rule 8). **Test year NOT touched — this family's one-touch budget remains UNSPENT.**
Raw output: `experiments/exp026_cond_out.txt` (conditional/deciding + one `TRT` line per treated position),
`experiments/exp026_pool_out.txt` (pooled), `experiments/exp026_port_out.txt` (portfolio/veto-only).
Harness: `experiments/exp026_minlot_skip_harness.py`. Dev PC, `.venv` (Python 3.12.10).

FIDELITY (§5's four gates, all PASSED before this section existed): fast-path identity news OFF/ON; y4 mode C
254/1.0961/+$352.60/9.9895%; y4 A_live 350/1.0667/+$259.67/11.4154%; conditional-replay self-check identical
(350 == 350). Two further in-code assertions passed on EVERY window: `replay_selfcheck: true` (arm-A replay
reproduces the full sequence field-for-field) and **`untouched_identical: true`** — positions with zero min-lot
degenerations produce bit-identical outcomes in both arms, i.e. the seam provably changes nothing except the
mechanism under test.

### 1. WHAT THE TREATED SUBSET ACTUALLY IS (a finding in its own right, and the reason the engine path was required)
```
window            positions  protection actions  min-lot degenerations  genuine partial closes  treated%
y1 2021-22            307           116                    32                    84             10.42%
y2 2022-23            299           136                    36                   100             12.04%
y3 2023-24            279           103                    24                    79              8.60%
y4 VAL 2024-25        310           105                    65                    40             20.97%
```
Two things follow, and neither was visible to EXP-023/024/025:
 * **The min-lot degeneration is a MINORITY of protection actions on Train (28%, 26%, 23%) and still under half on Val
   (62% — the modern high-price regime).** Pooled, 157 treated positions out of 264 protection-affected ones. EXP-024's
   ~0.04R-per-trade estimate for this lever (its §8(2)) was scaled off the whole affected@real subset and therefore
   **overstates the reach of this particular fail-direction by roughly 2x**.
 * **84 of the 157 treated positions (54%) entered at MORE than the minimum lot and only became min-lot after an
   earlier genuine partial close had halved them** (entry lots 0.02/0.03/0.04; 73 entered at 0.01). Those 84 cases
   cannot exist at all in the exp023/024/025 harnesses, which never modeled the partial-close branch. That is the
   concrete vindication of §2's choice to run the real engine rather than adapt the older harness.

### 2. DECIDING EVIDENCE — paired per-position `R(B_skip) − R(A_live)` on the treated subset, sequence held FIXED
```
window          treated |  A_live: avgR   medR   worstR  losers |  B_skip: avgR   medR   worstR  losers | paired mean_d +- SE (t)
y1 2021-22          32  |         0.8435 0.7870 +0.4058    0/32 |        0.6861 0.4707 -1.0040    3/32 | -0.1574 +- 0.1242 (-1.27)
y2 2022-23          36  |         0.7634 0.6972 +0.3959    0/36 |        0.9063 1.2743 -1.1204    3/36 | +0.1429 +- 0.1205 (+1.19)
y3 2023-24          24  |         0.7739 0.6866 +0.4160    0/24 |        0.6576 0.5086 -1.1168    4/24 | -0.1163 +- 0.1588 (-0.73)
y4 VAL 2024-25      65  |         0.8170 0.7369 +0.4061    0/65 |        0.8438 1.3091 -1.1337   15/65 | +0.0268 +- 0.1286 (+0.21)
POOLED TRAIN (n=92)                                                                                   | -0.0292 +- 0.0767 (-0.38)  95% CI [-0.180, +0.121]; 1.7-SE bar +0.130
POOLED VAL   (n=65)                                                                                   | +0.0268 +- 0.1286 (+0.21)  95% CI [-0.225, +0.279]; 1.7-SE bar +0.219
```
**MEASUREMENT WITH WIDE ERROR BARS** — the pre-registered words, used exactly as §6(e) requires: every window and both
pooled windows are below rule 6's 100-trade floor, both intervals comfortably contain zero, and no significance claim
is made in either direction. What the data supports is a **flat** effect: pooling all four windows purely
DESCRIPTIVELY (never a basis for a decision — that would break the split) gives **−0.006R ± 0.070 over 157
positions**, i.e. the fail-direction `watchman/loop.py` chose on instinct is, as far as this data can see, worth
approximately **nothing** on average.
Exit mix of the treated subset, pooled (the mechanism in one line): A_live = **157 `news_protection` closes at avgR
+0.80** (the trigger price is the bar's own open when the position gapped past +0.5R, which is why the realised floor
is +0.40R and not +0.50R); B_skip = **78 take_profit (49.7%) / 51 stop_loss (32.5%) / 28 time_stop (17.8%), avgR
+0.80**. B trades a CERTAIN +0.80R for a bimodal +2R-or-−1R lottery **with the same mean**.

### 3. TAIL (criterion (c) — pre-registered as mandatory and decisive on its own)
```
window   A_live losers | B_skip losers (rate) | B below -1.0R (rate) | A worstR | B worstR | A_live PORTFOLIO loser rate | A_live PORTFOLIO worstR | (context) book's own below-1R rate
y1            0/32     |     3/32 (9.4%)      |      1 (3.1%)        | +0.4058  | -1.0040  |          43.99%             |        -1.8405          |        30.7%
y2            0/36     |     3/36 (8.3%)      |      1 (2.8%)        | +0.3959  | -1.1204  |          43.36%             |        -2.6306          |        27.1%
y3            0/24     |     4/24 (16.7%)     |      1 (4.2%)        | +0.4160  | -1.1168  |          42.74%             |        -1.4741          |        29.6%
y4 VAL        0/65     |    15/65 (23.1%)     |     10 (15.4%)       | +0.4061  | -1.1337  |          48.00%             |        -1.3456          |        30.9%
paired downside distribution: negative differences 20 of 32, 16 of 36, 14 of 24, 29 of 65;
5th-percentile difference -1.5329 / -1.5163 / -1.7501 / -1.6661; worst single difference -1.5329 / -1.5163 / -1.7501 / -1.8935.
```
**A_live's zero-loser, positive floor is CONFIRMED BY MEASUREMENT, not assumed: 0 losers in 157 treated positions,
worst realised outcome +0.3959R.** B_skip reintroduces losers at 8–23% per window (25 of 157 pooled, 15.9%) with a
worst case of −1.13R, and its worst single paired difference is −1.89R (a position that banked +0.76R under A and
stopped out under B).
Against the three pre-registered numeric bars:
 * **c1 (loser rate vs the book's own) — PASS in all four windows, comfortably.** B's treated loser rate (9.4/8.3/16.7/
   23.1%) is far BELOW the same window's full-portfolio loser rate under A_live (44.0/43.4/42.7/48.0%), let alone
   5 points above it. Treated trades still lose much less often than ordinary ones, exactly as the rationale predicted.
 * **c2 (no new KIND of loss) — PASS in all four windows.** B's worst treated outcome (−1.00/−1.12/−1.12/−1.13R) is
   never worse than the same window's worst single trade under A_live's own full sequence (−1.84/−2.63/−1.47/−1.35R).
 * **c3 (<=5% of treated positions below −1.0R) — FAIL on y4/VAL (15.4%), pass on y1/y2/y3 (3.1/2.8/4.2%).**
 **DISCLOSURE, because the record must say what the bar actually caught: c3 as written is MIS-CALIBRATED, and it was
 my error, made before the run.** It assumed "worse than a clean stop-out" is exceptional. In this engine's R
 accounting it is not: spread/slippage is baked into the ENTRY fill while R is measured against the PLANNED stop
 distance, so an ordinary stop-out lands slightly beyond −1.0R and **27–39% of ALL trades in the book already finish
 below −1.0R** (context column above; mode C's own rate is 38.6–44.7%). On the bar's INTENDED meaning — "does B
 produce a tail abnormal for this strategy?" — B passes: 15.4% is half the book's own 30.9%. c3 is therefore recorded
 as **FAIL AS WRITTEN, PASS ON INTENT**, and — this is the important part — **the verdict does not rest on it**:
 criteria (a), (b) and (e) each fail independently and were decided on the deciding metric, not on the tail.

### 4. PORTFOLIO (full sequence; pre-registered VETO-ONLY, never selection evidence)
$3,000/window, complete cost model. trades | PF | net$ | maxDD% | PF_ex_top5:
```
window          C_none                                    A_live                                    B_skip
y1 2021-22      266 | 1.0159 |   +64 | 14.49 | 0.941     391 | 1.0256 |  +104 | 13.87 | 0.952     352 | 1.0112 |   +42 | 12.99 | 0.932
y2 2022-23      254 | 0.9949 |   -22 | 26.12 | 0.917     399 | 0.8974 |  -413 | 29.11 | 0.822     355 | 0.8920 |  -396 | 25.90 | 0.810
y3 2023-24      233 | 1.2020 |  +773 | 12.27 | 1.103     358 | 1.1328 |  +534 | 15.83 | 1.048     339 | 1.1485 |  +573 | 15.62 | 1.059
y4 VAL 2024-25  254 | 1.0961 |  +353 |  9.99 | 0.987     350 | 1.0667 |  +260 | 11.42 | 0.973     297 | 1.0772 |  +258 |  9.58 | 0.958
```
**Criterion (d): PASS / no veto.** B_skip's PF is within 1.4% of A_live in every window (−1.4%, −0.6%, +1.4%, +1.0%)
and its max DD is LOWER in three of four (−6.3%, −11.0%, −1.4%, −16.1% relative). Stated plainly because honesty cuts
both ways: the veto-only evidence would have let B through, and on Val it looks mildly favourable (PF 1.0667 → 1.0772,
maxDD 11.42% → 9.58%). By pre-registration it cannot carry the candidate, and the reason is visible in the row: the
arms differ by 39–53 trade RECORDS per window, i.e. they are dominated by which signals the re-sequenced single-slot
engine happens to take next — the non-causal reshuffling EXP-017/020/021 established as unusable for selection.

### 5. CRITERION-BY-CRITERION (§6, as written)
 (a) paired effect > +1.7 SE on Train AND Val — **FAIL.** Train **−0.0292 ± 0.0767 (t −0.38)**, i.e. the wrong sign
     entirely, against a +0.130 bar; Val **+0.0268 ± 0.1286 (t +0.21)** against a +0.219 bar. Neither window is close.
 (b) per-year sign consistency — **FAIL, textbook.** −0.157 / +0.143 / −0.116 / +0.027: the sign alternates every
     single year. This is the EXP-020 / EXP-022(a) overfit signature in its purest form, and it is what a genuinely
     null effect looks like when it is sliced into four small samples.
 (c) tail — **c1 PASS, c2 PASS, c3 FAIL-as-written / PASS-on-intent** (see §3, including the disclosure that the c3
     bar was mis-calibrated by me before the run). Reported in full as pre-registered; not the basis of the verdict.
 (d) portfolio non-degradation (veto-only) — **PASS / no veto** (§4). Cannot carry the candidate.
 (e) sample honesty (rule 6) — **BLOCKS A RECOMMEND, as pre-registered.** Treated n = 32/36/24/65; pooled Train
     **92 < 100**, pooled Val **65 < 100**. Both reported in the pre-registered words "MEASUREMENT WITH WIDE ERROR
     BARS" with SE and 95% intervals printed. No significance claim is made.
 (f) Test confirmation — **NOT EARNED, NOT SPENT.** (a), (b) and (e) all fail, so the Test year 2025-07-22 →
     2026-07-21 was NOT touched by this experiment (the harness refuses `--window y5*` without `--allow-test`, which
     was never passed). **This family's one-touch Test budget remains UNSPENT.**
 Walk-forward: the 4 windows served as the rolling confirmation for (b) — and they disagree.
 Multiple testing (rule 7): 3 configs this experiment (A_live, B_skip, C-reference), **23 cumulative for this family**
 (EXP-023's 20 on the sibling lock-SL question, same windows, same data). Required edge 1.7 SE; the best window's
 measured edge is +0.21 SE.
 POST-HOC DISCLOSURE (descriptive only, supports nothing): splitting the treated subset by entry lot gives
 −0.090 ± 0.139 (n=73, entered at min lot) and +0.067 ± 0.048 (n=84, degenerated after an earlier partial close).
 This split was computed AFTER the deciding numbers, was not pre-registered, is a subgroup analysis on n<100 halves,
 and is recorded only so the reader can see that neither half carries a signal either.

### 6. VERDICT AND WHAT IT MEANS
**B_skip is NOT RECOMMENDED.** `config/base.yaml` UNCHANGED, `src/` UNCHANGED — in particular
`watchman/loop.py::_act_on_news_decision`'s min-lot CLOSE_ALL fallback is NOT modified, for the second time (EXP-023
rejected changing its ACTION; this rejects changing its FAIL-DIRECTION). No promotion/demotion gate, Auditor threshold
or circuit-breaker limit was touched or proposed for change, including the Gate-1 floors the Task-1 measurement above
showed the deployed config failing. **Adoption was in any case never this experiment's to decide** — it was
pre-registered as a user-level risk-appetite decision — but there is nothing here to put in front of the user as an
improvement: the measured mean gain is zero, and the certain cost is the loss of a zero-loser floor on 8–21% of
positions.
The honest one-line summary: **the instinct in the code comment was neither a mistake nor an opportunity.** Closing
the whole position when half a lot is impossible banks a certain ~+0.80R; skipping protection instead converts the
same trades into a 50%-take-profit / 32%-stop-loss lottery whose mean is the same to within a rounding error
(−0.006R ± 0.070 pooled over all four windows, descriptive). Choosing between them is a pure risk-appetite preference,
not an expectancy question, and the risk-averse side of that preference is the one currently deployed.

### 7. WHAT THIS CLOSES, AND WHAT REMAINS OPEN (escalated, not decided here)
 (i) **This closes the min-lot fallback question in both of its dimensions.** EXP-023 answered the ACTION (lock-SL:
     rejected, decisively). EXP-026 answers the FAIL-DIRECTION (skip: no measurable gain, and blocked by rule 6
     anyway). EXP-025 answered the trigger LEVEL (no level helps). Recommended: do not open a fourth grid on this
     mechanism against these same four windows — it would be the same null re-sliced.
 (ii) **EXP-024 §8(2)'s sizing of this lever needs correcting on the record, and this experiment is the correction.**
     Its "~0.15R on 26% of trades ≈ 0.04R per trade" applied the whole affected@real subset's parity cost to a lever
     that only reaches the min-lot slice of it — 157 of 264 affected positions, and on that slice the measured effect
     is ~0.00R, not 0.15R. The parity COST measured by EXP-024 and re-measured on Test in the Task-1 MEASUREMENT above
     is real; what is now excluded is that THIS lever recovers it.
 (iii) **What remains open is EXP-024 §8(3), unchanged and now better evidenced: accept the cost, knowingly.** The
     Task-1 measurement priced it on the held-out year (PF 1.1903 → 1.0845, net +$845 → +$429, PF_ex_top5 1.0666 →
     0.9866), and EXP-026 has now excluded the last cheap mechanism that might have recovered it. The remaining
     honest options are strategy-level, not parameter-level, and belong to the user: live with a control that costs
     ~44% of per-trade expectancy on a min-lot account, or change the thing that makes min-lot the norm (EXP-022 §7's
     escalation (ii): $3,000 equity against XAUUSD's 0.01-lot contract at gold >$4,000). Neither is a knob this
     experiment may turn.

## EXP-027 2026-08-04 — Risk Voice's news ENTRY BLACKOUT measured against the REAL calendar, and the first FULL news-parity 2×2 (NEW family "news-entry-blackout parity"; sibling of EXP-024's "news-protection backtest/live parity")
Status: MEASURED -- veto rate 13.3% (Train) / 14.2% (Val) of entries; vetoed-population counterfactual +0.055R +- 0.131 (Train, n=100) / -0.367R +- 0.188 (Val, n=36), sign flips per year; EP-P portfolio effect +$5/-$132/+$19/+$8. Found and root-caused an engine fidelity defect in the PROTECTION path (see RESULTS §5). NO config change, NO src/ change, Test year NOT touched. See `### EXP-027 RESULTS` below. Originally: PRE-REGISTERED (results pending). Everything from this line down to `### EXP-027 RESULTS` was written and
COMMITTED, together with a results-free `experiments/exp027_entry_blackout_harness.py`, **before any E, EP or vetoed-
population number existed**; only the RESULTS section and this Status line are added afterwards.
**This is a MEASUREMENT experiment, not a selection experiment** — EXP-024's framing, deliberately reused. There is no
grid over any parameter, no candidate, no winner, and no `config/base.yaml` or `src/` change can follow from it
directly. Its whole job is to price the LAST unmodeled news mechanism in this project: Risk Voice's entry blackout
(`news_blackout_before_min: 45` / `news_blackout_after_min: 30`, condition 2 of 6), which live enforces on every bar and
which — until commit `3ec55ee` this session — no backtest in this log had ever applied. `risk_voice.news_blackout_*`
is NOT swept, proposed for sweeping, or implicitly chosen by anything here.
Scope: **Train (y1/y2/y3) + Val (y4) ONLY. The Test year 2025-07-22 → 2026-07-21 is NOT touched under any outcome** —
`load_window` refuses `y5*` unconditionally and this harness has no `--allow-test` escape hatch, deliberately. The Test
year's ONE authorised measurement touch was spent this session on the honest Test baseline (MEASUREMENT 2026-08-04),
which modeled protection but NOT the entry blackout. **A full-parity (EP) Test re-measurement is therefore explicitly
DEFERRED to a future user decision** — it is the obvious next question, it is a user-level call about how to spend a
second measurement touch on the held-out year, and this experiment may not make it. Recorded here so it cannot later
look like an oversight.

### 0. WHY THIS IS ANSWERABLE TODAY AND WAS NOT YESTERDAY
`backtest/engine.py` passed `backtest/news_stub.NoHistoricalNewsDataProvider` (always `[]` = "no event") to
`check_risk_voice`, so Risk Voice condition 2 never fired in ANY backtest this project has run — while the live journal
proves it fires (3 blocked signals for "1 high-impact USD news event(s) within -45/+30 min of now" plus 17
calendar-unavailable fail-safe vetoes in 13 days of paper trading). Commit `3ec55ee` added
`BacktestConfig.model_risk_voice_news` (default `False` = bit-for-bit prior behaviour), routing the SAME
`HistoricalNewsCalendarProvider` Watchman's protection already uses into `check_risk_voice`. That commit published one
informational y4 row (241 / PF 1.1432 / +$490.82 / DD 8.82%) and explicitly disclaimed measuring it: "measurement of
the delta is EXP-027's job, not this commit's claim". This is that job.
Motivating evidence: `experiments/entry_diagnostic_2026-08-04.md` §5 (EXPLORATORY, multiple-comparisons-unsafe, and
treated here as a hypothesis to be measured, never as a result to be confirmed) — pre-event ≤45 min clean signals
PF 0.867 Train / 0.828 Val vs 1.053 / 1.125 outside; negative in 3 of 4 years; y3 flips positive; every executed
subset below rule 6's floor. Its §5.4 also states the constraint this design obeys: the entry blackout and the exit
protection are defined against the SAME event list 30–45 min apart, so **they cannot be measured one at a time** —
hence a 2×2, not a single before/after.

### 1. THE FOUR CELLS (two independent, separately-wired mechanisms; nothing else varies)
| id | blackout (`model_risk_voice_news`) | protection (`news_protection_cfg`) | role |
|----|----|----|----|
| **C0** | OFF | OFF | REFERENCE = what EVERY historical row in this log measured. Must reproduce them exactly. |
| **E** | **ON** | OFF | the entry mechanism ALONE |
| **P** | OFF | **ON** | = EXP-024/026's engine "A@real"/"A_live". Must reproduce EXP-026 §4 exactly. |
| **EP** | **ON** | **ON** | **the first FULL news-parity numbers this project has ever had** — closest to live |

Configs evaluated (this exp / cumulative for this NEW family): **4 / 4**. Zero free parameters, zero selection
decisions, and both anchor cells are pinned to previously published numbers, so no multiple-testing correction is
warranted (rule 7) and none is applied — stated so it cannot later look like an omission.

### 2. ACCOUNT CONTEXT, WINDOWS, COST MODEL (identical to EXP-022/023/024/025/026 and to the honest Test baseline)
Train y1 2021-07-22→2022-07-21, y2 2022-07-22→2023-07-21, y3 2023-07-22→2024-07-21; Val y4 2024-07-22→2025-07-21.
$3,000 starting equity per-year anchored, `min_lot_risk_cap_pct` 1.5, `risk_per_trade_pct` 1.0, all-24h session,
be/trail OFF, `tp_r_multiple` 2.0, `swing_pivot_bars` 3. COST MODEL COMPLETE: slippage = min-1-spread
(`slippage_points=None`), swap modelled (EXP-018 long −53.2 / short +36.8, 3× Wed), commission $0.00 (IC Markets
Standard, the real account). News data: `data/historical/news_calendar_backtest.csv` as built this session by
`scripts/build_backtest_calendar.py`; `news_profit_threshold_r` 0.5, `news_window_minutes` 30, `news_close_mode` half;
`news_blackout_before_min` 45, `news_blackout_after_min` 30 — i.e. `config/base.yaml` exactly as adopted, nothing
overridden. Production path only: the real `backtest.engine.run_backtest`; the sequence-fixed replay calls the engine's
own `check_exit`/`_step_news_protection`/`evaluate_watchman`/`_close_trade` (EXP-026's fidelity-proven `replay_one`,
imported, not re-written). Harness `experiments/exp027_entry_blackout_harness.py`; raw output `experiments/exp027_*.txt`.

### 3. THE BLACKOUT'S EXACT SEMANTICS, AND THE CLOCK-CONVENTION DISCLOSURE (read from code BEFORE any run)
(a) **Window.** `check_risk_voice` condition 2 builds `window_start = now − news_blackout_after_min` and `window_end =
now + news_blackout_before_min` and vetoes iff any high-impact event for a symbol currency falls inside — i.e. the
blackout is active at instant `now` iff some high-impact **USD** (`_SYMBOL_CURRENCIES["XAUUSD"] = ("USD",)`) event `e`
satisfies **`now − 30min ≤ e ≤ now + 45min`**. Two-sided, unlike Watchman's protection window, which is forward-only
(`now ≤ e ≤ now + 30min`, EXP-024 §4(c)).
(b) **DECLARED DEVIATION — the one-bar clock skew, disclosed before any number because it is a real methodological
choice and it is NOT mine to fix here.** `run_backtest` sets its `SimulatedClock` to the SIGNAL bar's OPEN time `t`,
so the modeled veto condition on a signal is `t − 30min ≤ e ≤ t + 45min`. Live (`orchestrator/shadow_loop.py`)
evaluates `check_risk_voice` when the H1 bar CLOSES, i.e. at `≈ t + 60min` — which is also the instant the backtest
fills at (the next bar's open) — so live's condition on the same signal is `t + 30min ≤ e ≤ t + 105min`. The two
conventions select overlapping-but-different bars (for an event at 15:30 the engine vetoes the 15:00 and 16:00 bars;
live vetoes the 14:00 and 15:00 bars). This affects only time-of-day-sensitive conditions and is a PRE-EXISTING engine
convention (it applies equally to Risk Voice's session and Friday-close conditions), not something `3ec55ee`
introduced. **Handling, fixed now:** the four portfolio cells use the engine's own convention unchanged — they are the
production path and must stay comparable to every anchor in this log — and the vetoed-population measurement (a)
reports the veto set under **BOTH** conventions ("signal-open" = the engine's, deciding; "fill-time" = live's,
sensitivity), including its overlap and its counterfactual outcomes. A full live-convention portfolio arm is NOT run:
it would need a src-level or seam-level change to the production path and is a code-fidelity question for the main
session, not a tuning question this experiment may settle. EXP-027 will therefore state plainly, in RESULTS, that
"EP = closest to live" is true of the MECHANISM SET but carries a one-bar timing skew in the blackout.
(c) `blackout_active()` in the harness mirrors `risk_voice.py`'s four lines verbatim (same `get_symbol_currencies`,
same window arithmetic, same provider) rather than re-deriving them.

### 4. THE MEASUREMENTS (each is a number to be REPORTED, not a test to be passed)
 (a) **ENTRY-BLACKOUT HIT RATE + the vetoed population's COUNTERFACTUAL — the DECIDING DESCRIPTIVE.** On the C0 trade
     population (sequence FIXED, EXP-023 D3 / EXP-024 §6(2) methodology): how many of C0's actual entries the blackout
     would veto (count, % of C0 trades), under both clock conventions; plus the calendar-side bar density (% of all
     bar-hours in blackout, the analogue of EXP-024's 5.2% "elig%"). Then **what the vetoed entries actually did under
     C0** — avgR ± SE, median, PF, win rate, worst, exit mix — against the KEPT complement, with the difference of
     means and its SE. The diagnostic claims the blocked population is net-negative; this is where that claim is
     confirmed or refuted on the executed population.
 (b) **PORTFOLIO DELTAS.** All four cells per window: records, positions (unique entry times — a genuine partial close
     emits a second record, so records ≠ positions in P/EP), PF, net$, maxDD%, avgR, PF-ex-top-5, win rate, news
     exits. Deltas **E − C0** (the blackout's incremental effect WITHOUT protection) and **EP − P** (WITH it) — the
     pre-registered pair — plus P − C0 and EP − E, which are free and prevent selective reporting.
 (c) **INTERACTION HONESTY / OVERLAP.** Does the blackout mostly remove trades that protection would have truncated
     anyway? Each C0 position is replayed with protection ON, sequence held FIXED, and flagged "affected" iff ≥1
     protection action fires. Reported: |vetoed ∩ affected| as a % of vetoed and as a % of affected, per window and
     pooled. Also `R(P) − R(C0)` on the vetoed subset — what the blackout removes that protection was already
     modifying.
 (d) **RECONCILIATION of the diagnostic's 10–13% predicted trade-count reduction vs the engine's ~5.1%** (y4:
     254 → 241). Pre-registered decomposition, to be quantified rather than asserted: (i) ARM/DENOMINATOR — the
     diagnostic's 10–13% is blocked entries as a fraction of **mode-A@real trade RECORDS** (36/42/37/51 against
     391/399/358/350), while its own mode-C count is 16/20/22/17 (6.0/7.9/9.4/6.7%); (ii) CLOCK CONVENTION — the
     diagnostic classified trades by their **entry (fill) time**, i.e. live's convention, while the engine vetoes on
     the signal bar's open (§3(b)); (iii) SLOT DYNAMICS — with `max_positions_per_symbol: 1`, a vetoed entry FREES the
     slot, so a later signal that C0 never had room for is taken instead: net change = −(gross vetoes) + (new entries
     E takes that C0 never took). Measured by set-differencing C0's and E's entry timestamps, with the arithmetic
     identity asserted in code.

### 5. FIDELITY GATES — all run and reported BEFORE any E/EP number is read; any failure STOPS the experiment
 G1 **Fast-path identity.** EXP-022's memoisation shim must be trade-for-trade, field-for-field identical to the
    unshimmed engine on a 4,000-bar slice **in all four cells** (the shim has never been proven against
    `model_risk_voice_news=True`).
 G2 **C0 anchors, all four windows** (this log's universal baseline — EXP-022 cap-1.5, re-confirmed by
    EXP-023/024/025/026 §4): **266 / 254 / 233 / 254** trades, PF **1.0159 / 0.9949 / 1.2020 / 1.0961**, maxDD
    **14.49 / 26.12 / 12.27 / 9.9895%**; y4 net **+$352.60**.
 G3 **P anchors, all four windows** (EXP-026 §4's `A_live` = the 2026-08-04 engine NOTE's A@real): **391 / 399 / 358 /
    350** records, PF **1.0256 / 0.8974 / 1.1328 / 1.0667**, maxDD **13.87 / 29.11 / 15.83 / 11.4154%**; y4 net
    **+$259.67**.
 G4 **Conditional-replay self-check** (inherited, asserted in code): replaying each C0 position one-by-one must
    reproduce the full-sequence C0 trade list field-for-field — without it the (c) overlap replay is untrustworthy.
 G5 **INFORMATIONAL cross-check, not a stop:** the protection-affected count computed by (c)'s replay on the C0
    sequence should equal EXP-024 §1's affected@real **64 / 62 / 67 / 71**, because the first-trigger logic and the
    sequence are identical. A mismatch does not invalidate (a)/(b) — it would mean the engine's partial-close branch
    changes which positions trigger at all — but it must be explained in RESULTS, not glossed.
 Cell E's y4 row (241 / 1.1432 / +$490.82 / 8.82%) from `3ec55ee`'s commit message is likewise INFORMATIONAL: it is
 this experiment's own subject matter, so reproducing it is expected, and a mismatch would mean the harness differs
 from the CLI path and must be explained.

### 6. SAMPLE-SIZE HONESTY (rule 6) AND WHAT MAY NOT BE CONCLUDED
The vetoed population is expected to be SMALL — the diagnostic's mode-C counts imply roughly 16–22 per window, i.e.
**every window far below rule 6's 100-trade floor, and pooled Val almost certainly below it too**. Pre-registered
handling, identical to EXP-024 §9 and EXP-026 §6(e): per-window numbers are REPORTED but **no statement rests on a
single window**; deciding statements pool Train (y1+y2+y3) and, separately, Val (y4); any pooled figure under 100 is
reported in the pre-registered words **"MEASUREMENT WITH WIDE ERROR BARS"** with the SE and the 95% interval printed
and **no significance claim made in either direction**. A Train+Val pooled figure may be printed as DESCRIPTION only.
Explicitly forbidden here, before results exist: sub-band shopping inside the blackout window (the diagnostic §5.3's
30–45 min cell, PF 0.558/0.031 on n=50/16, is a warning, not a lead), any statement of the form "the blackout should
be widened/narrowed to X", and any use of a favourable EP row to argue that a control is free.

### 7. DECLARED LIMITATIONS (the four inherited from EXP-024 §10/§6, plus two specific to the entry side)
 (i) **Current-classification look-ahead.** The dump's `importance`/`event_name` are MetaQuotes' CURRENT judgement
     applied to 2021–2025 history; an event reclassified since then is treated as high (or not) for its whole history.
     Direction unknown, unremovable from this source.
 (ii) **Reschedules erased** (~1% divergence measured against the live archive). Direction unknown.
 (iii) **Live fail-safe episodes are NOT modelled, and on the ENTRY side this is bigger than on the exit side.**
     `HistoricalNewsCalendarProvider` never returns `None`, so `check_risk_voice`'s fail-safe branch ("economic
     calendar unavailable for USD — fail-safe veto") is structurally unreachable in this backtest, while live vetoes
     **every** entry during such an outage — the paper journal shows 17 such vetoes in 13 days including one unbroken
     14-hour outage. **The modeled veto rate is therefore a LOWER BOUND on live's, and by a larger margin than
     EXP-024's protection-side lower bound.**
 (iv) **One-bar clock skew** (§3(b)): the engine evaluates the blackout at the signal bar's open, live at that bar's
     close. Measured, both conventions reported, not fixed here.
 (v) **The engine checks once per signal; live checks twice** (signal-time + immediately-before-send). Per
     `backtest/engine.py`'s own docstring both live calls read the SAME already-closed bar, so conditions 1/3/4/5 are
     structurally near-identical between them — but condition 2 (news) IS re-queried fresh and can flip pass→veto
     between the two calls, which the single modeled check cannot reproduce. Direction: live vetoes at least as often
     as modeled. Same direction as (iii).
 (vi) **H1 bar resolution.** Blackout eligibility is evaluated at one instant per bar; live polls every ~5 s. Both
     directions.

### 8. WHAT CANNOT FOLLOW FROM THIS EXPERIMENT (rule 8, restated and binding)
Nothing here may change `config/base.yaml` or `src/`. C0 is a REFERENCE ARM describing what the backtest has always
measured, never a candidate: "the blackout costs trades" is not a licence to disable a deliberate risk control, exactly
as EXP-024 §11 established for mode C. No promotion/demotion gate, Auditor threshold or circuit-breaker limit is
touched, and none will be proposed for change as a way to make any number look better — **including** the Gate-1 floors
the honest Test baseline already showed the deployed config failing (PF 1.0845 vs 1.30, PF-ex-top-5 0.9866 vs 1.0). If
EP looks better than P, that is a MEASUREMENT of a control's incidental effect, not evidence for a parameter change;
any actual tuning of `news_blackout_before_min`/`after_min` would need its own pre-registration with a > 1.7 SE bar on
Train AND Val plus per-year sign consistency — which §6's sample sizes are very unlikely to be able to deliver, and
that is said here, before the numbers, rather than discovered afterwards.

### EXP-027 RESULTS (run 2026-08-04) — OUTCOME: MEASURED. The entry blackout vetoes **13.3% of Train / 14.2% of Val entries** (vs Watchman protection's 25.9% / 28.0%), and the vetoed population is **NOT reliably net-negative**: pooled Train **+0.055R ± 0.131** (n=100) vs kept +0.035R, pooled Val **−0.367R ± 0.188** (n=36, below floor) vs kept +0.119R, with the per-year sign **flipping** (−0.21 / −0.13 / **+0.44** / −0.37). Portfolio-wise the blackout is **noise-dominated**: E−C0 net **+$199 / −$154 / −$204 / +$138**, EP−P net **+$5 / −$132 / +$19 / +$8**. Overlap with protection's population is **only 30% (Train) / 22% (Val)** of the vetoed set — the two controls hit largely DIFFERENT trades. NO config change, NO `src/` change, **Test year NOT touched**.
Raw output: `experiments/exp027_fidelity_out.txt` (G1–G4), `experiments/exp027_port_out.txt` (the 2×2 grid),
`experiments/exp027_veto_out.txt` (per-position rows + measurement (a)/(c)), `experiments/exp027_pool_out.txt` (pooled),
`experiments/exp027_recon_out.txt` (measurement (d)), `experiments/exp027_g5diag_out.txt` (the G5 root-cause
decomposition). Harness `experiments/exp027_entry_blackout_harness.py`; G5 diagnostic
`experiments/exp027_g5_diagnostic.py`. Dev PC, `.venv` (Python 3.12.10).

FIDELITY GATES — all run and reported BEFORE any E/EP number was read:
 G1 **PASS in all four cells.** Fast-path shim identical to the unshimmed engine trade-for-trade, field-for-field on a
    4,000-bar slice: C0 176 trades, E 165, P 242, EP 232, `identical: true` throughout. (C0's 176 and P's 242 also
    match the honest-Test-baseline measurement's own fast-path fidelity rows.) This is the first time the shim has been
    proven against `model_risk_voice_news=True`.
 G2 **C0 anchors PASS exactly, all four windows**: 266 / 254 / 233 / 254 trades, PF 1.0159 / 0.9949 / 1.2020 / 1.0961,
    maxDD 14.4879 / 26.12 / 12.2659 / 9.9895%, y4 net **+$352.60** — this log's universal baseline, to the cent.
 G3 **P anchors PASS exactly, all four windows**: 391 / 399 / 358 / 350 records, PF 1.0256 / 0.8974 / 1.1328 / 1.0667,
    maxDD 13.8664 / 29.1098 / 15.8294 / 11.4154%, y4 net **+$259.67** — EXP-026 §4's `A_live` row / the engine NOTE's
    A@real, to the cent.
 G4 **Conditional-replay self-check PASS on all four windows**: replaying each C0 position one-by-one reproduces the
    full-sequence C0 trade list field-for-field (266/254/233/254 == 266/254/233/254, `identical: true`).
 INFORMATIONAL, cell E's y4 row: **241 / PF 1.1432 / +$490.82 / maxDD 8.8235%** — commit `3ec55ee`'s published
    informational figure reproduced to the cent through this harness.
 G5 **INFORMATIONAL cross-check — MISMATCH, and it was worth chasing.** Protection-affected on the C0 sequence measured
    here is **63 / 70 / 62 / 71** against EXP-024 §1's **64 / 62 / 67 / 71**. Root-caused, not glossed — see §5. It does
    not touch (a), (b)'s C0/E cells or (d); it slightly under-states protection's reach in the P/EP cells.

### 1. MEASUREMENT (b) — THE 2×2 GRID (the first full news-parity numbers this project has had)
$3,000/window, complete cost model, commission $0. `records` = `ClosedTrade` rows (a genuine partial close emits a
second one, so P/EP records > positions); `positions` = unique entries.
```
window          cell  records  positions     PF      net$    maxDD%   avgR    pf_ex5  winRate  news_exits
y1 2021-22      C0        266        266   1.0159    +64.07  14.4879  0.0153  0.9408  0.3722       0
                E         259        259   1.0680   +263.07  12.9481  0.0428  0.9898  0.3784       0
                P         391        307   1.0256   +103.51  13.8664  0.1680  0.9516  0.5269     116
                EP        366        290   1.0289   +108.53  15.6417  0.1656  0.9495  0.5137     104
y2 2022-23      C0        254        254   0.9949    -22.38  26.1200 -0.0254  0.9166  0.3386       0
                E         235        235   0.9555   -176.75  24.9050 -0.0347  0.8704  0.3362       0
                P         399        299   0.8974   -413.32  29.1098  0.1413  0.8215  0.5138     136
                EP        353        276   0.8467   -544.82  27.0516  0.0984  0.7638  0.4901     115
y3 2023-24      C0        233        233   1.2020   +773.25  12.2659  0.1305  1.1030  0.4077       0
                E         231        231   1.1585   +569.75  12.5290  0.1068  1.0648  0.3983       0
                P         358        279   1.1328   +533.73  15.8294  0.2408  1.0484  0.5391     103
                EP        345        273   1.1451   +552.69  13.8067  0.2330  1.0574  0.5304      94
y4 VAL 2024-25  C0        254        254   1.0961   +352.60   9.9895  0.0505  0.9873  0.3740       0
                E         241        241   1.1432   +490.82   8.8235  0.0776  1.0272  0.3900       0
                P         350        310   1.0667   +259.67  11.4154  0.1347  0.9728  0.4971     105
                EP        335        298   1.0692   +268.00  10.8867  0.1354  0.9749  0.4925      96
POOLED TRAIN    C0        753        753          +814.94 |  E  725 pos  +656.07 |  P  885 pos  +223.92 |  EP 839 pos  +116.40
POOLED VAL      C0        254        254          +352.60 |  E  241 pos  +490.82 |  P  310 pos  +259.67 |  EP 298 pos  +268.00
```
The two pre-registered deltas:
```
E − C0  (blackout alone)          y1 PF +0.0521 net +199.00 DD -1.54 | y2 PF -0.0394 net -154.37 DD -1.22
                                  y3 PF -0.0435 net -203.50 DD +0.26 | y4 PF +0.0471 net +138.22 DD -1.17
                                  TRAIN net -158.87   VAL net +138.22
EP − P  (blackout given protection) y1 PF +0.0033 net   +5.02 DD +1.78 | y2 PF -0.0507 net -131.50 DD -2.06
                                  y3 PF +0.0123 net  +18.96 DD -2.02 | y4 PF +0.0025 net   +8.33 DD -0.53
                                  TRAIN net -107.52   VAL net   +8.33
(free, reported so nothing is selectively omitted)
P − C0  (protection alone)        y1 +39.44 | y2 -390.94 | y3 -239.52 | y4 -92.93   TRAIN -591.02  VAL -92.93
EP − E                            y1 -154.54 | y2 -368.07 | y3 -17.06 | y4 -222.82  TRAIN -539.67  VAL -222.82
```
**Read honestly, in both directions.** (1) The blackout's per-window sign FLIPS in both deltas (E−C0: +/−/−/+; EP−P:
+/−/+/+), and the three windows where it "helps" and the two where it hurts are the same size. (2) Its Val row looks
favourable in isolation — E lifts y4 PF 1.0961 → 1.1432 and pushes `pf_ex5` across 1.0 (0.9873 → 1.0272) — and this is
exactly the kind of single-window number this log has repeatedly shown to be reshuffling, not causation (EXP-017/020/
021; EXP-024 §3; EXP-026 §4): the E arm differs from C0 by **54 removed and 41 added entries on y4** (§4), so the two
arms are not the same book. (3) With protection already on, the blackout's incremental portfolio effect is
**essentially nil on three of four windows** (+$5 / +$19 / +$8) and clearly negative on one (−$132, y2). (4) Max DD
improves under the blackout in 3 of 4 windows in both deltas — the one genuinely coherent directional read here, and
still on four observations.

### 2. MEASUREMENT (a) — THE VETO RATE AND THE VETOED POPULATION'S COUNTERFACTUAL (the deciding descriptive)
Sequence held FIXED on the C0 trade population, engine clock convention (signal bar's open):
```
window          bars  blackout-bars  %    | C0 trades  vetoed  rate%  | vetoed avgR ± SE   medR    PF    win%  worstR
y1 2021-22      5926        482     8.13  |    266        26    9.77  | -0.2066 ± 0.2316 -1.0071 0.6634 30.8  -1.1103
y2 2022-23      5917        464     7.84  |    254        38   14.96  | -0.1297 ± 0.2083 -0.9675 0.7987 31.6  -1.1302
y3 2023-24      5894        458     7.77  |    233        36   15.45  | +0.4394 ± 0.2276 +0.1461 2.0925 55.6  -1.0553
y4 VAL 2024-25  5913        447     7.56  |    254        36   14.17  | -0.3667 ± 0.1879 -0.9889 0.4858 25.0  -1.1337
POOLED TRAIN                                753       100   13.28  | +0.0552 ± 0.1309, t +0.42, 95% CI [-0.201, +0.312]
POOLED VAL                                  254        36   14.17  | -0.3667 ± 0.1879, t -1.95, 95% CI [-0.735, +0.002]
   the KEPT complement, same windows:
POOLED TRAIN  n=653  avgR +0.0345 ± 0.0523   |  vetoed − kept = **+0.0207 ± 0.1410 (t +0.15)**, CI [-0.256, +0.297]
POOLED VAL    n=218  avgR +0.1194 ± 0.0900   |  vetoed − kept = **-0.4861 ± 0.2083 (t -2.33)**, CI [-0.895, -0.078]
```
Exit mix, vetoed vs kept: y4 vetoed **25 stop_loss / 6 take_profit / 5 time_stop** (69% stop-outs) against kept
107/71/31/8/1 (49% stop-outs); y3 vetoed **15 take_profit / 14 stop_loss / 7 time_stop** — the mirror image.
**What this does and does not support.** The entry diagnostic §5.2's claim was that pre-event entries are net-negative
(unconditional PF 0.867 Train / 0.828 Val). On the EXECUTED population, with both blackout halves counted as live
counts them: **Val reproduces the claim strongly (−0.367R, PF 0.486, 69% stop-outs) and Train does not reproduce it at
all (+0.055R, PF ~1.0 on the pooled subset)** — because y3's vetoed subset is a +0.44R, PF 2.09 population. The sign
alternates −/−/+/− across the four years. Both pooled figures are dominated by the same y3 vs y4 disagreement the
diagnostic itself flagged ("negative in 3 of 4 years, y3 flips positive"), and the Val subset (n=36) is far below
rule 6's floor. **This is a MEASUREMENT WITH WIDE ERROR BARS in both directions, and it does not establish that the
blackout removes a net-negative population.**
CONVENTION SENSITIVITY (pre-registered §3(b); it matters, and it cuts against the strongest-looking number):
```
convention                       vetoed n (y1/y2/y3/y4)   pooled TRAIN avgR ± SE      pooled VAL avgR ± SE
signal-open (engine, deciding)      26 / 38 / 36 / 36      +0.0552 ± 0.1309 (n=100)   -0.3667 ± 0.1879 (n=36)
fill-time   (live's instant)        25 / 38 / 29 / 29      -0.0540 ± 0.1377 (n= 92)   -0.1400 ± 0.2367 (n=29)
set overlap (both / sig-only / fill-only):  y1 16/10/9   y2 27/11/11   y3 15/21/14   y4 17/19/12
```
The two conventions agree on the *rate* (12–14%) but disagree on *which trades* — only **42–71% of each veto set is
shared** — and the Val point estimate moves from −0.37R to −0.14R depending on which one is used. **A subset whose
measured mean moves by 0.23R under a one-bar timing convention is not a stable effect.**

### 3. MEASUREMENT (c) — OVERLAP WITH WATCHMAN'S NEWS PROTECTION (the interaction-honesty check)
```
window          C0 trades  vetoed  protection-affected  BOTH  | both as % of vetoed  as % of affected
y1 2021-22          266       26           63             9   |       34.6%              14.3%
y2 2022-23          254       38           70            12   |       31.6%              17.1%
y3 2023-24          233       36           62             9   |       25.0%              14.5%
y4 VAL 2024-25      254       36           71             8   |       22.2%              11.3%
POOLED TRAIN        753      100          195            30   |       30.0%              15.4%
POOLED VAL          254       36           71             8   |       22.2%              11.3%
```
**Answer: NO — the blackout does not mostly remove trades protection would have truncated anyway.** 70% of the vetoed
population on Train (78% on Val) is never touched by protection, and 85–89% of the protection-affected population is
never vetoed. The mechanism is visible in the exit mixes: protection can only reach a trade that gets to +0.5R (a
better-than-average population, EXP-024 §4), while the blackout's population is stop-out-heavy. On the small
intersection, `R(P) − R(C0)` over the vetoed subset is +0.176 ± 0.134 (y1) / −0.006 ± 0.106 (y2) / −0.123 ± 0.100 (y3)
/ +0.061 ± 0.097 (y4) — i.e. the two controls are close to independent, and the 2×2's near-additivity in §1 is a
consequence of that, not a coincidence.

### 4. MEASUREMENT (d) — RECONCILING "10–13%" WITH "~5.1%" (quantified, not asserted)
```
window          C0 pos  E pos  net change   gross vetoed  C0 entries   E entries     shared
                                (pct)       (engine conv)  absent in E  absent in C0  entries
y1 2021-22        266    259   -7  (-2.63%)      26            34           27          232
y2 2022-23        254    235  -19  (-7.48%)      38            54           35          200
y3 2023-24        233    231   -2  (-0.86%)      36            45           43          188
y4 VAL 2024-25    254    241  -13  (-5.12%)      36            54           41          200
(identity asserted in code every window: |C0| − |C0\E| + |E\C0| = |E|, true 4/4)
```
The "10–13% vs 5%" gap decomposes into **three separate things, none of which is a discrepancy once named:**
 **(i) The two numbers measure different quantities. This is the dominant term.** The engine's ~5.1% is a NET
 portfolio position-count change; the diagnostic's 10–13% was a GROSS blocked-entry count. The gross veto rate measured
 here is **9.8 / 15.0 / 15.5 / 14.2%** — i.e. the diagnostic's magnitude was broadly RIGHT as a gross rate, and it was
 quoted as a trade-count cost, which is the net figure. On y4: **36 entries vetoed, 13 net positions lost.**
 **(ii) Slot dynamics, quantified.** With `max_positions_per_symbol: 1` a vetoed entry frees the slot, so the E arm
 takes signals C0 never had room for. On y4 the E sequence is missing **54** of C0's entries (the 36 direct vetoes plus
 18 downstream re-sequencing casualties) and contains **41** entries C0 never took — a **76% replacement rate** — for a
 net of −13. Pooled Train: 133 removed, 105 added, net −28 (−3.7%). **Roughly three quarters of every blocked entry is
 refilled by a later signal**, which is why a 14% veto rate shows up as a 1–7% trade-count change.
 **(iii) Arm/denominator and the missing half of the window.** The diagnostic's 36/42/37/51 blocked entries were
 **pre-event (≤45 min) only** — its own §5.2 splits pre- and post-event and the pooled Train pre-event mode-A n is 115
 = 36+42+37 — and were divided by mode-A@real **records** (391/399/358/350), which include partial-close rows. Live's
 blackout blocks BOTH halves. Counting both halves on C0 positions at live's own instant gives 25/38/29/29, of which
 the pre-event half is the diagnostic's 16/20/22/17. So the diagnostic understated the blocked SET (one half omitted)
 while overstating the RATE (records denominator); the two errors partly cancelled, which is why its 10–13% happened to
 land near the true 12–14% gross rate.
 Residual, stated plainly: after (i)–(iii) there is nothing left to explain. The engine's y4 E cell (241) is exactly
 what a 36-entry gross veto plus 76% slot refill produces.

### 5. THE G5 ROOT CAUSE — AN ENGINE FIDELITY DEFECT IN THE PROTECTION PATH, FOUND BY THIS EXPERIMENT (escalated, NOT fixed here)
G5's mismatch (63/70/62/71 vs EXP-024's 64/62/67/71) decomposes EXACTLY into two independent effects, both measured
(`experiments/exp027_g5diag_out.txt`, `exp027_g5_diagnostic.py`):
```
window          EXP-024 harness rule   engine eligibility rule   lost to profit-gate   engine ACTUAL
                (t < e < t+90, strict)  (t <= e <= t+90)          float round-trip      (= this log's P cell)
y1 2021-22              64                     78                      -15                   63
y2 2022-23              62                     76                       -6                   70
y3 2023-24              67                     74                      -12                   62
y4 VAL 2024-25          71                     76                       -5                   71
```
 (i) **Boundary inclusivity (+14 / +14 / +7 / +5).** `check_news_protection`'s window is inclusive at both ends, so the
     engine counts an event landing exactly on the bar's open (very common: 267 in-range events at 17:00, 99 at 20:00)
     or exactly at `t+90`; EXP-024's harness used strict inequalities. The engine's rule is the one that matches
     `MQL5CalendarProvider.get_high_impact_events`; the harness's was a deliberate positive-duration approximation
     (EXP-024 §4(e)). The engine is right here.
 (ii) **A FLOAT ROUND-TRIP IN THE PROFIT GATE silently drops intrabar first-touch triggers (−15 / −6 / −12 / −5
     positions; 86 bar-level rejections across the four windows, ALL of them the same boundary case).**
     `engine._news_trigger_candidate_price` returns the exact `entry ± profit_threshold_r × initial_stop_distance`
     level when the bar touches it intrabar, and `check_news_protection` then RE-DERIVES
     `profit_r = (price − entry) / initial_stop_distance`. In IEEE-754 that round-trip is not exact: it lands on
     0.49999999999999994 in a large minority of cases, and the gate's `profit_r < threshold` test then rejects the
     trigger with the literal reason **"profit 0.50R below protection threshold 0.5R"**. Worked example, y1 entry
     2021-08-02 05:00 SELL: entry 1810.91, stop distance 9.45775508394141, candidate 1806.1811224580294, window
     16:00→17:30 containing three high-impact USD events — and the decision is NO_ACTION.
     Direction and size: the engine **under-fires** news protection relative to both its own rule and live (live reads
     a real tick price and never performs this round-trip), by **5–15 positions per year ≈ 7–19% of the affected
     population**. Every engine-path protection number in this log inherits it: the P/EP cells above, EXP-026's
     `A_live` rows, the 2026-08-04 engine NOTE, and the honest Test baseline's A@real arm (whose measured
     news-protection cost is therefore, by this channel, a slight UNDER-estimate — the same direction as its other
     declared limitations).
     **NOT fixed here (a measurement experiment changes no `src/`), and deliberately not worked around in the harness**
     — the P cell must remain the same instrument EXP-026 and the Test baseline used, or the anchors G2/G3 stop
     meaning anything. **Escalated to the main session as a one-line-fix candidate** (compare with a tolerance, or have
     the candidate-price path hand `check_news_protection` the profit_r it already computed). Anyone who fixes it must
     expect every engine-path A@real/P/EP row in this log to move slightly, and should re-anchor deliberately.

### 6. SAMPLE HONESTY (rule 6) AND MULTIPLE TESTING (rule 7) — as pre-registered in §6, applied as written
Vetoed subsets are **26 / 38 / 36 / 36**: every individual window is far below rule 6's 100-trade floor and no
statement above rests on one window. Pooled **Train n=100 just clears the floor**; pooled **Val n=36 does not**, so, in
the pre-registered words, **the Val figure is a MEASUREMENT WITH WIDE ERROR BARS** — −0.367R with SE 0.188 and a 95%
interval of **[−0.735, +0.002]** that touches zero; the vetoed-minus-kept contrast is −0.486 ± 0.208, and it is
reported as a description, not a significance claim, because its subset is below the floor, its sign disagrees with
Train, and it moves to −0.14R under the fill-time convention. Train's own contrast (+0.021 ± 0.141, t = 0.15) is
indistinguishable from zero. **Neither pooled estimate supports a claim that the blackout removes a net-negative
population, and this section will not pretend otherwise.** Rule 7: nothing was selected, no threshold could be chosen
by any result, both anchor cells were pinned to published numbers before the run — no correction is warranted and none
is applied. Configs evaluated: **4 / 4** for this new family.

### 7. LIMITATIONS, RE-STATED AGAINST THE ACTUAL RESULT (all six were pre-declared in §7; two now have measured sizes)
 (i) Current-classification look-ahead; (ii) reschedules erased (~1%) — direction unknown, unchanged.
 (iii) **Live fail-safe episodes are NOT modelled — the biggest one on the entry side.** `HistoricalNewsCalendarProvider`
     cannot return `None`, so the "calendar unavailable → veto every entry" channel is structurally absent, while the
     paper journal recorded 17 such vetoes in 13 days including a 14-hour outage. **The 13.3% / 14.2% veto rate is a
     LOWER BOUND on live's**, and by a wider margin than EXP-024's protection-side bound (protection's fail-safe only
     reaches positions already at +0.5R; the entry fail-safe reaches EVERY signal).
 (iv) **One-bar clock skew — now measured, and material** (§2's convention table): the two conventions share only
     42–71% of their veto sets and disagree by 0.23R on the Val subset mean. **"EP = closest to live" is true of the
     MECHANISM SET, not of the blackout's exact timing.**
 (v) Single check per signal vs live's two — same direction as (iii).
 (vi) H1 bar resolution — unchanged.
 (vii) **NEW, discovered by this experiment:** the profit-gate float round-trip of §5, which makes the P and EP cells
     under-fire protection by 7–19% of the affected population.

### 8. WHAT THIS ESTABLISHES, AND WHAT MAY NOT FOLLOW (rule 8 restated)
 (1) **The last unmodeled news mechanism now has a price, and the price is "roughly nothing, with wide error bars".**
 The blackout vetoes ~13–14% of entries, ~76% of which are refilled by later signals; its incremental portfolio effect
 given protection (EP−P) is +$5 / −$132 / +$19 / +$8 across four $3,000 years; its vetoed population's counterfactual
 mean is +0.055R on Train and −0.367R on Val with the sign flipping per year and moving 0.23R under a timing
 convention. That is a control that is close to free in expectancy terms, not a cost like protection's (EXP-024:
 −0.157R per affected trade, ~44% of per-trade expectancy on the honest Test baseline) and not a rescue either.
 (2) **The strategic conclusion from today's honest Test baseline does NOT change.** Val's full-parity cell EP is
 PF 1.0692 / +$268 / DD 10.89% against P's 1.0667 / +$259.67 / 11.42% — a difference of $8 on a $3,000 account. Nothing
 here moves a PF-1.08 Test-year strategy toward the 1.30 Gate-1 floor, and nothing here is a licence to disable a
 deliberate risk control (C0 is the reference arm, never a candidate — EXP-024 §11's precedent).
 (3) **NOT earned, and explicitly not proposed:** any tuning of `news_blackout_before_min` / `news_blackout_after_min`.
 The pre-registration (§8) required > 1.7 SE on Train AND Val plus per-year sign consistency for any such follow-up;
 the measured Train contrast is +0.15 SE, the signs alternate −/−/+/−, and the subsets are 26–38 per window. The
 diagnostic's own §7 item 2 (`news_blackout_after_min` 30 → 0/15) is therefore **NOT unblocked by this measurement** —
 it is blocked by the same sample sizes, and that should be recorded before someone reads §2's Val row alone. The
 30–45 min sub-band remains explicitly forbidden (§6).
 (4) **Escalated to the user / main session, in priority order:**
   **(a)** the §5 profit-gate float round-trip — a genuine `src/` fidelity defect this experiment found, cheap to fix,
   but it moves every engine-path protection row in this log, so fix + deliberate re-anchor, not fix alone;
   **(b)** the §3(b)/(iv) one-bar clock skew — the engine evaluates Risk Voice at the signal bar's OPEN while live
   evaluates at its CLOSE (= the fill instant). It affects news, session and Friday-close conditions alike, so it is
   older and wider than this experiment; deciding whether the engine should evaluate at `t + bar_span` is a
   production-code question, and every session-window experiment in this log (EXP-001/003/004) was measured under the
   current convention;
   **(c)** the deferred **full-parity (EP) Test re-measurement** — the Test year now has a modeled-protection-only
   baseline (PF 1.0845) but no full-parity one. Whether to spend a second measurement touch on it is a **user
   decision**, and this experiment does not make it. On this evidence the expected difference is small (EP−P on Val was
   +$8), which is itself an argument for NOT spending the touch yet.
 `config/base.yaml` UNCHANGED; `src/` UNCHANGED; no promotion/demotion gate, Auditor threshold or circuit-breaker limit
 touched or proposed for change. **Test year 2025-07-22 → 2026-07-21 NOT touched by any run in this experiment.**

## NOTE (not an EXP) 2026-08-04 — EXP-027's two escalated engine defects FIXED; which logged anchors are superseded and which numbers carry a small known bias
Fixes (engine-only; `watchman/`/`council/` untouched; C0 re-verified bit-for-bit 266/254/233/254, y4 1.0961/+$352.60/9.9895 after both):
1. **Float round-trip at the +0.5R gate**: the intrabar news-trigger candidate price is now nudged one symbol point past
   the threshold in the profit direction (clamped to the bar's own high/low) so `check_news_protection`'s recomputed
   profit_r cannot land at 0.4999...9 and skip a fire live would have taken (live triggers off ticks moving PAST the
   level). Regression test pins the exact EXP-027 worked example.
2. **Entry-blackout clock**: with `model_risk_voice_news=True` the risk-voice check now receives the signal bar's CLOSE
   time (live's decision/order-send moment), not its open. Strict no-op for every `model_risk_voice_news=False` run.
   Known, documented side effect: `check_risk_voice` computes one shared `now` for conditions 4 (session — inert at
   all-24h) and 5 (Friday-close), so Friday-close also evaluates at close time in E/EP cells — which is in fact the
   live-faithful timing as well; impact limited to Friday 19:00-23:00 signals, not separately quantified.

ANCHOR STATUS after the fix (post-fix rows, `exp027` harness conventions):
- P (protection-only): y1-y4 = 421/416/382/369 records, PF 1.0414/0.8856/1.1335/1.0830, net +166/−471/+512/+330 —
  **supersedes** the 2026-08-04 engine NOTE's y4 A@real row (350/1.0667/+259.67/11.4154) and EXP-027's P column.
  news_exits rose 116→146 / 136→145 / 103→124 / 105→118 (the 7-19% under-fire, recovered).
- EP (full parity): y1-y4 = 380/371/360/324, PF 0.9830/0.8905/1.1315/1.0724, net −60/−392/+476/+253.
- E (blackout-only): y1-y4 = 246/240/222/243, PF 1.0361/0.9093/1.1975/1.1473 — EXP-027's E column superseded.
- EXP-027's MEASUREMENT conclusions survive the fix qualitatively (blackout ≈ expectancy-free; EP−P still small; the
  vetoed-population sign-flip finding unchanged in kind), but its exact E/P/EP figures are the PRE-fix engine's.
- KNOWN SMALL BIAS, not re-run here: EXP-026's arms and the 2026-08-04 HONEST TEST BASELINE were measured with defect 1
  present (protection under-fires ~7-19% of affected trades), so the honest Test PF 1.0845 modestly UNDER-charges mode
  A's true cost. Re-measuring the Test baseline with the fixed engine is a USER decision (it would be a correction of
  the same authorised measurement touch, not a new one) — deliberately not taken unilaterally.

### AMENDMENT 2026-08-04 (same day) to the HONEST TEST BASELINE — re-measured with the post-defect-fix engine (user-authorised correction of the SAME measurement touch, not a new touch)
The original Task-1 A@real row was produced with EXP-027's defect 1 present (the +0.5R float boundary under-firing
protection). Re-run with the fixed engine (`2b0b109`), same driver, same pre-registration, C arm first as control:
- C (anchor): 228 / PF 1.1903 / +$845.29 / DD 12.39 / pf_ex5 1.0666 — IDENTICAL to the original (C is untouched by
  both fixes, as required).
- **A@real CORRECTED: 309 trades / PF 1.1116 / +$574.44 / DD 14.57 / avgR 0.0802 / pf_ex5 1.0095 / 77 news exits**
  (was 292 / 1.0845 / +$429.27 / 13.85 / 0.9866 / 59 — the missing fires were exactly the under-fire the fix
  recovered, 59→77).
Reading, honestly: the correction SOFTENS but does not reverse the original verdict. The deployed config still FAILS
Gate-1 on the Test year (PF 1.1116 < 1.3; the gap vs mode C's own miss widens −0.110 → −0.188), but the top-5
criterion flips BACK to passing (pf_ex5 1.0095 > 1.0 — the original "fails ex-top-5" statement is retracted), DD stays
under the 15% ceiling by 0.43pp, and news protection's Test-year cost is −$271 (~32% of mode-C net), not the −$416
(~49%) first reported. Mechanism: on this Test year the recovered fires mostly banked +0.5R on trades that would
otherwise have retraced — firing MORE was net-positive here, unlike the Train-side picture. Gate thresholds untouched
(rule 8). Driver got a one-line compat fix (`risk_voice_news_modeled=False` in its envelope call — the flag postdates
the driver; both Task-1 arms are defined as protection-only).

---

## EXP-028 2026-08-04 — `trend_alignment` PARTIAL tier as a direct entry VETO (ABLATION; NEW family "council-entry-tier"; menu item #4 of the 2026-08-04 entry diagnostic)
Status: PRE-REGISTERED (results pending). Everything from this line down to `### EXP-028 RESULTS` was written and
COMMITTED, together with a results-free `experiments/exp028_partial_tier_veto_harness.py`, **before any V-cell,
removed-population or overlay number existed**; only the RESULTS section and this Status line are added afterwards.

### 0. WHAT IS BEING TESTED, AND WHY IT IS NOT EXP-016 RE-RUN
`council/scoring.py` awards `trend_alignment` = **30** for full EMA alignment (EMA20>EMA50>EMA200 for Bull, reversed
for Bear), **15** for PARTIAL alignment (EMA20>EMA50 only, EMA200 not yet crossed) and 0 otherwise. The candidate is a
**direct entry VETO of the partial tier**: no entry may be taken when the admitted direction's own voice scored 15 on
`trend_alignment`, regardless of that voice's total score.
EXP-016 (REJECTED, 2026-07-23) changed the partial tier's **WEIGHT** (15 -> 0 / 7). That only removes partial bars whose
total then falls below 70; a partial bar that also has RSI + MACD + structure + confluence scores 85 and still fires at
70 after the cut. A veto at the accept step removes **all** partial-trend entries. That is a materially different and
strictly larger treatment set, never measured. **EXP-016 constrains this candidate but does not close it** — and its
rejection is also the single strongest prior AGAINST it: cutting the tier's weight made Train worse (y1 flipped
negative, y3 roughly halved, DD rose), through re-sequencing that an unconditional read cannot see. A broader veto
removes more, so the same mechanism can bite harder. This is stated before the run, not after.

### 1. HYPOTHESIS / MECHANISM (mechanism-first, like EXP-008's be/trail cut — a REMOVAL of a measured-negative population)
Partial EMA alignment is the "trend is turning but not established" state. The 2026-08-04 entry diagnostic §6 — whose
census scores EVERY bar and forward-walks it, so it carries **no fired-set selection confound**, unlike the 2026-07-23
scoring NOTE's read — measures the partial tier as **PF < 1.0 in all four years** (0.925 / 0.775 / 0.587 / 0.888),
Train avgR **−0.148 ± 0.030 (t = −4.9)**, Val −0.071 ± 0.051 (t = −1.4, same sign), on ~1,871 (Train) / 647 (Val)
unconditional signal bars ≈ 24% of clean supply. It is the most per-year-consistent negative in that document.
If that read survives as an actual trade-set change, removing the population should raise portfolio PF and net$.
**Pre-registered failure mode** (the one EXP-015/EXP-016 both hit on this exact component): with
`max_positions_per_symbol: 1`, removing entries frees the slot and lets DIFFERENT, later signals in, so the arms are
not the same book — the removal can be net-harmful even when the removed population is genuinely negative. That is a
real outcome, not an excuse, and it decides the verdict against the candidate if it happens.
**The diagnostic is EXPLORATORY and multiple-comparisons-unsafe**; it is treated here as a hypothesis to be measured,
never as a result to be confirmed.

### 2. THE ABLATION SEAM — DECLARED EXACTLY, BEFORE ANY RESULT
There is **no config knob**: the 30/15/0 tiering is hard-coded in `council/scoring.py`. The veto is installed as a
**signal-level seam** — a wrapper passed through `BacktestConfig.signal_fn` (a public, injectable field). Nothing under
`src/` or `config/` is modified by this experiment.
```
plan = engine._council_signal_fn(df, i, ...)        # the REAL Council + the REAL Risk Voice
if plan is not None and veto:
    tier = score_<leading>_voice(df, i, ...).trend_alignment   # bull for a BUY plan, bear for a SELL plan
    if tier == 15:  return None                     # <-- the ablation
```
Properties of this seam, and why it is the honest one:
 (a) The veto fires **before** Shield's cooldown check, **before** CFO sizing and **before** `_PendingOrder` creation,
     so a vetoed signal never occupies the single position slot — **the sequence is replayed honestly and the slot is
     freed for later signals**, exactly as EXP-027's blackout seam behaves. No row is deleted from a finished trade
     list. Slot-refill accounting is measurement (c).
 (b) `<leading>` is the voice whose direction the Decision Matrix admitted — precisely the population the diagnostic
     section 6 measured ("winning voice's own component only, clean signals").
 (c) No clock convention is involved (unlike EXP-027's blackout): `trend_alignment` is a pure function of closed bars
     up to the signal bar `i`.
 (d) The wrapper with `veto=False` must be a strict no-op — proven trade-for-trade by fidelity gate G3 **and** by every
     C0 anchor below, all of which are produced THROUGH the wrapper.

### 3. CELLS, WINDOWS, ACCOUNT CONTEXT, COST MODEL
| id | veto | blackout | protection | role |
|----|----|----|----|----|
| **C0** | OFF | OFF | OFF | REFERENCE = every historical row in this log. Must reproduce the anchors exactly. |
| **V** | **ON** | OFF | OFF | **THE CANDIDATE** (news mechanisms OFF = the C0 convention, and the PRIMARY comparison) |
| P | OFF | OFF | ON | post-defect-fix protection-only anchor — **gate only**, y4 |
| EP | OFF | ON | ON | full news parity, y4 only — **INFORMATIONAL overlay** |
| EPV | **ON** | ON | ON | full news parity + veto, y4 only — **INFORMATIONAL overlay** |

Train y1 2021-07-22->2022-07-21, y2 2022-07-22->2023-07-21, y3 2023-07-22->2024-07-21; Val y4 2024-07-22->2025-07-21.
$3,000 per-year anchored equity, `min_lot_risk_cap_pct` 1.5, `risk_per_trade_pct` 1.0, all-24h session, be/trail OFF,
`tp_r_multiple` 2.0, `swing_pivot_bars` 3, complete cost model (slippage = min-1-spread, swap EXP-018 long −53.2 /
short +36.8 3x Wed, commission $0.00 IC Markets Standard). Production path only: the real
`backtest.engine.run_backtest`, the real `council.decision_matrix.evaluate_council`, the real `check_risk_voice`, the
real `Shield`. EXP-022's validated fast-path memoisation shim for speed (re-proven against the new seam, gate G1).
Harness `experiments/exp028_partial_tier_veto_harness.py`; raw output `experiments/exp028_*.txt`.
**SCOPE: Train + Val ONLY. The Test year 2025-07-22 -> 2026-07-21 is NOT touched by this experiment under ANY
outcome** — this family has no authorised Test budget, `load_window` refuses `y5*` unconditionally, and the harness has
no `--allow-test` escape hatch, deliberately. **A RECOMMEND verdict ENDS AT THE RECOMMENDATION**: there is no knob to
change, so adoption would require a `src/` change to `council/` plus a spec conversation (rule 10), which is a user
decision and not this experiment's to take.

### 4. MEASUREMENTS (pre-registered; each is reported whatever it says)
 (a) **PORTFOLIO, C0 vs V**, per window and pooled Train / Val: records, positions, PF, net$, maxDD%, avgR, PF-ex-top-5,
     win rate, exit mix. Deltas V − C0.
 (b) **THE REMOVED POPULATION, per year**, sequence held FIXED on the C0 trade list (EXP-023 D3 / EXP-024 section 6(2) /
     EXP-027 (a) method): which of C0's own entries came from a partial-tier signal bar, and what those trades actually
     did — n, avgR ± SE, median, PF, win%, worst, exit mix, net$ — against the KEPT complement, with the
     difference-of-means and its SE. The tier-30 and tier-0 subsets are reported alongside, free, so nothing is
     selectively omitted.
 (c) **SLOT-REFILL ACCOUNTING** (EXP-027 (d)'s method): gross vetoed signals in the V run, C0 entries absent from V,
     V entries absent from C0, shared entries, net position delta, replacement rate, with the arithmetic identity
     `|C0| − |C0\V| + |V\C0| == |V|` asserted in code every window.
 (d) **TRADE-COUNT HONESTY**: post-veto trade counts per window, and the **Gate-1 200-trade floor** implication stated
     explicitly. A candidate that guts the sample fails even if PF rises.
 (e) **ONE INFORMATIONAL OVERLAY ROW ON VAL ONLY**: EPV − EP under the post-defect-fix engine, to sanity-check that the
     conclusion survives full news parity. Informational, descriptive, never deciding.

### 5. ACCEPTANCE CRITERIA — ALL of (a)-(e) must hold, or the verdict is REJECT
This is a REMOVAL of a measured-negative population, mechanism-first like EXP-008's be/trail cut, so the deciding
evidence is **the removal's consistency**, not a fitted peak — there is no grid, no plateau to defend and nothing to
tune (rule 5's neighbourhood check is n.a. by construction, and is recorded as n.a. rather than silently skipped).
 (a) **Portfolio improvement on Train AND Val** vs C0, in **both** PF and net$. Either split failing = REJECT.
 (b) **Per-year:** no year flips from profitable to losing, AND the removed population's negativity holds per-year
     (each year's removed-subset avgR/PF reported; a year where the removed subset is clearly POSITIVE is evidence
     against the mechanism and must be reported as such).
 (c) **Trade-count honesty:** ~24% of supply is removed. Post-veto counts are reported per window against the Gate-1
     200-trade floor. A candidate that pushes any window below the floor **fails on that ground alone**, whatever PF
     does; a candidate whose Val count falls below rule 6's 100-trade floor cannot support a conclusion at all.
 (d) **Slot-refill accounting** must be published, and the identity must hold 4/4 windows. If the V arm's book differs
     from C0 by a large add/remove churn (EXP-027 y4: 54 removed / 41 added), the portfolio delta is
     reshuffling-dominated and is **not** evidence of causation — this is stated now so a favourable number cannot be
     read as one later.
 (e) The informational EP overlay must not CONTRADICT the primary conclusion. It cannot rescue a failing candidate.

**Rule 7 (multiple testing).** Family "council-entry-tier" is NEW. Configs evaluated: **2 primary** (C0 baseline + V
candidate) **/ 2 cumulative**. There is exactly one candidate, no grid, and no value is chosen by any result, so no
edge inflation applies and no correction is warranted. The three overlay/anchor cells (P, EP, EPV) are DESCRIPTIVE and
are counted separately as such — they are not candidates and nothing is selected among them. Adjacent-but-separate
families whose counts are NOT merged into this one: "scoring-formula" (EXP-015/016, 7 configs, closed) and
"news-entry-blackout parity" (EXP-027, 4).
**Rule 6.** ~180 (Train) / ~60 (Val) executed removals are expected per the diagnostic. **The Val removed subset is
expected to be BELOW the 100-trade floor**, so per the standing convention every pooled figure under 100 is reported in
the pre-registered words **"MEASUREMENT WITH WIDE ERROR BARS"** with SE and 95% interval printed and no significance
claim made in either direction. Deciding statements pool Train (y1+y2+y3) and, separately, Val (y4); no statement rests
on a single window.

### 6. FIDELITY GATES — all run and reported BEFORE any deciding number is read; any failure STOPS the experiment
 G1 **Fast-path identity** for cells C0 and V on a 4,000-bar slice, trade-for-trade / field-for-field (the shim has
    never been proven against this seam).
 G2 **C0 anchors, all four windows**: **266 / 254 / 233 / 254** trades, PF **1.0159 / 0.9949 / 1.2020 / 1.0961**, maxDD
    **14.4879 / 26.12 / 12.2659 / 9.9895%**, y4 net **+$352.60** — this log's universal baseline (EXP-022 cap-1.5,
    re-confirmed EXP-023/024/025/026 section 4 and EXP-027 G2, and re-verified bit-for-bit after the 2026-08-04 engine
    defect fixes). Because C0 is produced THROUGH the veto wrapper (veto=False), G2 doubles as a full-window no-op proof.
 G3 **Seam no-op**: the wrapper with `veto=False` must equal the engine's own default `signal_fn` trade-for-trade.
 G4 **POST-FIX P anchor, y4 only, and only if the informational overlay is run**: **369 records / PF 1.0830 /
    +$329.94** (2026-08-04 defect-fix NOTE — this SUPERSEDES EXP-027's pre-fix P column 350 / 1.0667 / +$259.67).

### 7. WHAT CANNOT FOLLOW FROM THIS EXPERIMENT (rule 8, binding)
No promotion/demotion gate, Auditor threshold or circuit-breaker limit is touched, and none will be proposed for change
as a way to make any number pass — **including** the Gate-1 200-trade floor and the PF 1.30 floor the honest Test
baseline already showed the deployed config failing. If the veto cuts the sample below the floor, the candidate fails;
the floor does not move. No `config/base.yaml` change can follow from this experiment because no config key is
involved; no `src/` change is made here under any outcome. The Test year is not touched.

---

## EXP-029 2026-08-04 — SLOT ALLOCATION: (A) the missed-signal population & occupancy attribution, (B) `shield.duplicate_signal_cooldown_hours` (NEW family "shield-slot-allocation"; menu item #3 of the 2026-08-04 entry diagnostic)
Status: PRE-REGISTERED (results pending). Everything from this line down to `### EXP-029 RESULTS` was written and
COMMITTED, together with a results-free `experiments/exp029_slot_allocation_harness.py`, **before any census,
counterfactual or sweep number existed**; only the RESULTS section and this Status line are added afterwards.

### 0. WHY THIS, AND WHAT KIND OF THING IT IS
The 2026-08-04 entry diagnostic §3.4 reports a STRUCTURAL finding: only ~47% of distinct Council signal episodes ever
become trades (560/530/538/543 episodes vs 266/254/233/254 executed), because `max_positions_per_symbol: 1` keeps the
single slot busy. If true, **slot occupancy — not the Council gate — is the binding constraint on this strategy's trade
population**, and it is the mechanical reason every filter this log has tested produced reshuffling noise rather than a
clean effect (EXP-017/020/021, EXP-024 §3, EXP-026 §4, EXP-027 §4, EXP-028 §3).
This experiment is therefore deliberately split:
 * **PART A is a MEASUREMENT.** No candidate, no grid, nothing selected, no config or `src/` change can follow from it.
   Its job is to price the missed population and to attribute the blocking to KINDS OF HOLDS.
 * **PART B is a small pre-registered sweep** over the ONE existing `[adjustable]` knob in this area,
   `shield.duplicate_signal_cooldown_hours` (4.0, never tested).
 * **PART C is SCOPING PROSE ONLY — explicitly NOT simulated**, and it invents no numbers. `max_positions_per_symbol`
   is bounded by the spec's own "[adjustable: เพิ่มเป็น 2 ได้หลัง live 3 เดือน]" (raise to 2 only after 3 months
   live), so per rule 10 it may NOT be swept here and is not. Part C only enumerates options for a future USER
   decision.
**SCOPE: Train (y1/y2/y3) + Val (y4) ONLY. The Test year 2025-07-22 → 2026-07-21 is NOT touched by this experiment
under ANY outcome** — this family has no authorised Test budget, `load_window` refuses `y5*` unconditionally, and the
harness has no `--allow-test` escape hatch, deliberately.

### 1. ACCOUNT CONTEXT, WINDOWS, COST MODEL (identical to EXP-022..028)
Train y1 2021-07-22→2022-07-21, y2 2022-07-22→2023-07-21, y3 2023-07-22→2024-07-21; Val y4 2024-07-22→2025-07-21.
$3,000 per-year anchored equity, `min_lot_risk_cap_pct` 1.5, `risk_per_trade_pct` 1.0, all-24h session, be/trail OFF,
`tp_r_multiple` 2.0, `swing_pivot_bars` 3, complete cost model (slippage = min-1-spread, swap EXP-018, commission
$0.00). **News mechanisms OFF in every cell = the C0 convention**, so every row here is directly comparable to this
log's universal baseline. Production path only: the real `run_backtest`, the real `evaluate_council`, the real
`check_risk_voice`, the real `Shield`. EXP-022's fast-path shim for speed. Harness
`experiments/exp029_slot_allocation_harness.py`; raw output `experiments/exp029_*.txt`.

### 2. PART A — DEFINITIONS FIXED BEFORE ANY NUMBER EXISTS
 * **signal bar**: a bar at which the REAL `_council_signal_fn` (real Council + real Risk Voice) returns an
   `OrderPlan`, evaluated UNCONDITIONALLY at every bar with a `SimulatedClock` ticked to that bar's OPEN time — exactly
   the engine's own convention. This is the SUPPLY.
 * **signal episode**: a maximal run of CONSECUTIVE signal bars with the SAME direction; a gap bar or a direction
   change starts a new one. (The diagnostic §3.4 used the same construction; its published counts 560 / 530 / 538 /
   543 are cited HERE, before the run, as the number the census must land near — see gate G2.)
 * **admitted / missed**: an episode is ADMITTED iff some bar of it is the signal bar of an actual C0 trade; otherwise
   MISSED.
 * **counterfactual quality**: `backtest.forward_walk.simulate_order_forward` on the episode's FIRST bar's plan,
   started at the next bar, entry = the plan's own entry (signal bar's close), spread = the signal bar's spread,
   `time_stop_bars = 48`, Appendix A §5.4 cost convention (spread + commission, **no slippage**, no Watchman, no
   Shield, no news) — the SAME machinery and convention the entry diagnostic used, so the two are comparable.
   **ONE observation per EPISODE, not per bar**, which deliberately avoids the ~5x overlap inflation the diagnostic's
   own §1.1 caveat 1 warns about. Absolute levels under this convention run hotter than the engine's; only
   admitted-vs-missed comparison INSIDE the convention is meaningful, and that is the only comparison made.
 * **blocking attribution**: for a missed episode, the reason at its FIRST bar — `slot_busy` (the engine never
   evaluated that bar: a position was open), `shield_cooldown` (evaluated, plan returned, Shield rule 6 blocked),
   `sizing_or_no_next_bar` (evaluated, plan returned, Shield passed, still no trade: the min-lot floor, or no next bar
   to fill on). For `slot_busy`, the HOLDER is identified: its age in bars at that instant, its total holding length,
   and its eventual exit reason — which is the "which kinds of holds block the most supply" question, reported both as
   missed EPISODES and as blocked BARS per holder exit reason.
Reported: episode census per window; admit rate; missed count; cause mix; admitted-vs-missed forward-walk quality with
SE and the difference-of-means; quality by cause; the occupancy attribution above; and the C0 trades' own engine R for
scale. **Rule 6:** every subset under 100 observations is reported in the pre-registered words **"MEASUREMENT WITH
WIDE ERROR BARS"** with SE and 95% interval, and no significance claim is made in either direction.
**What Part A may NOT do (binding):** it selects nothing, so it cannot justify any config or `src/` change by itself,
and it may not be used to argue for a specific replacement policy — that is Part C's scoping question and a user
decision.

### 3. PART B — THE ONE KNOB: what `duplicate_signal_cooldown_hours` ACTUALLY gates (read from code first)
`shield/checkpoint.py` rule 6, verbatim: a new signal is blocked iff **(same symbol AND same direction as the last
trade Shield approved and that was actually FILLED)** AND **(the `swing_index` re-derived at signal time EQUALS the one
recorded at that trade)** AND **(elapsed < cooldown)**. A genuinely NEW confirmed swing bypasses it entirely, whatever
the elapsed time. In this single-position engine it is the ONLY one of Shield's six rules with any effect
(`open_positions` is always `[]`, so rules 2/3/4/5 can never fire; `min_rr` always passes at `tp_r_multiple` 2.0) —
therefore **`cooldown = 0` must be behaviourally identical to `shield_cfg = None`**, which is fidelity gate G3.
So the knob does NOT gate "how long to wait after any trade"; it gates **same-direction re-entry on the SAME confirmed
swing**. Raising it suppresses same-swing re-entries for longer; lowering it toward 0 restores the pre-2026-07-23
behaviour every EXP before that date was measured under.
**PRIOR EVIDENCE, stated before the run and against the candidate:**
 (i) The 2026-07-23 NOTE "MEASURED impact of the now-wired Shield cooldown" is effectively the {4} vs {0} contrast
     already, at $10k equity on the full history: net −2.4% trades (104 blocked entries, ~73 refilled by later signals
     — the same slot-refill dynamic), |ΔPF| ≤ 0.031 per window, **sign non-systematic** (Train +0.024, Val −0.031,
     Test +0.002, full-history +0.008). Its own conclusion was "noise-level, not a directional bias".
 (ii) The 2026-07-23 martingale NOTE's **variant B** measured the ADJACENT structural question — unlocking a second
     independent slot on a fresh signal — as **neutral-to-slightly-worse** (Train PF 1.092 vs baseline 1.102) with
     lower MTM drawdown (23.1% vs 25.5%), per-year consistent, and concluded "safe but delivers no clear edge".
 **Expectation, recorded before the run: REJECT / neutral.** A clean negative result is a valid and expected outcome
 (rule 9), and this pre-registration exists so that a lucky cell cannot be promoted after the fact.
**Grid: {0, 2, 4 (baseline), 8} hours. Nothing else is swept.** No refinement pass is authorised: with the expectation
above, a second pass around a "best" cell would be exactly the shopping this protocol forbids.

### 4. PART B — ACCEPTANCE CRITERIA (standard bars, set HARDER than usual, and why)
 (a) **Portfolio improvement on Train AND Val**, in PF and net$, vs the 4.0 baseline. Either split failing = REJECT.
 (b) **> 1.6–1.7 SE** on the avgR difference vs baseline, on Train AND on Val. **DECLARED IN ADVANCE: no PAIRED test
     is available for this knob.** By construction, an entry that exists in both arms is the SAME trade with the same
     exit and the same R (nothing but the cooldown differs), so the paired difference on shared entries is exactly
     ZERO and 100% of any effect lives in the composition change. The reported SE is therefore the **unpaired**
     `sqrt(se_a² + se_b²)`, which OVERSTATES the true SE for two heavily-overlapping trade sets and is thus
     conservative for a REJECT and demanding for an ADOPT — deliberately, per (e).
 (c) **Per-year consistency**: the candidate must beat 4.0 in the majority of Train years and not flip any year from
     profitable to losing.
 (d) **Plateau (rule 5)**: the candidate's neighbours in the grid must perform within ~15% of it. An isolated peak at
     one grid point is rejected as noise even if it is the best number in the sweep.
 (e) **The anti-reshuffling clause (the diagnostic's own requirement, adopted verbatim).** This knob's whole mechanism
     IS re-sequencing, so the usual "portfolio deltas are reshuffling noise" veto cannot be invoked to dismiss the
     result — which means **the plateau and per-year bars carry the entire weight and are set harder than usual**.
     Concretely: the shared-entry fraction between each arm and the baseline is reported, and if it is low the result
     is declared composition-dominated and the candidate must clear (c) and (d) unambiguously, not marginally.
 (f) **Rule 6 sample floor**: every window must stay ≥ 100 trades for a conclusion, and any window's Gate-1 200-trade
     status is reported (rule 8: the floor is not negotiable and is not touched).
**Rule 7 (multiple testing).** Family "shield-slot-allocation" is NEW. Configs evaluated: **4 / 4** (cooldown 0, 2, 4,
8; the baseline 4.0 is one of them). N = 4 ≪ 20, so no edge-inflation correction is triggered; the honest count is
recorded so a future session continues from 4, not from 0. Part A's census selects nothing and adds no configs.
`max_positions_per_symbol` is NOT swept and NOT counted (rule 10, spec-bounded).

### 5. FIDELITY GATES — all run and reported BEFORE any deciding number is read; any failure STOPS the experiment
 G1 **C0 anchors, all four windows, with the cooldown at its live 4.0 and the measurement probe installed**:
    **266 / 254 / 233 / 254** trades, PF **1.0159 / 0.9949 / 1.2020 / 1.0961**, maxDD **14.4879 / 26.12 / 12.2659 /
    9.9895%**, y4 net **+$352.60**. Because the probe wraps `signal_fn`, G1 doubles as proof that the instrumentation
    is behaviour-neutral.
 G2 **PART A self-consistency, asserted IN CODE (the mode aborts on failure)**: (i) the set of bars the engine did NOT
    evaluate must EQUAL the union of [entry_index, exit_index) over the C0 trades (the occupancy map is either exact or
    the attribution is meaningless); (ii) at every bar the engine DID evaluate, the unconditional census must agree
    with the engine on whether a plan exists; (iii) the episode count is reported against the diagnostic's published
    560 / 530 / 538 / 543 — a material divergence must be explained in RESULTS, not glossed.
 G3 **`cooldown = 0` == `shield_cfg = None`**, trade-for-trade / field-for-field on a full window — the structural
    claim in §3 that rule 6 is the only live rule in this engine.

### 6. WHAT CANNOT FOLLOW FROM THIS EXPERIMENT (rule 8, binding)
No promotion/demotion gate, Auditor threshold or circuit-breaker limit is touched, and none will be proposed for
change — including the Gate-1 200-trade floor and PF floor. Part C proposes no numbers and simulates nothing;
`max_positions_per_symbol` stays at 1 and is not swept (rule 10). If Part B rejects, `config/base.yaml` keeps
`duplicate_signal_cooldown_hours: 4.0` and that is recorded as a "default is already fine" result (rule 9). The Test
year is not touched under any outcome.
