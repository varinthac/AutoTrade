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
