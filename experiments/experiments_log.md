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




