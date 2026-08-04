# AutoTrade VPS Lean Architecture Plan

**Status: PROPOSAL — decision-ready, nothing implemented.** No VPS writes and no
code changes were made producing this document; every VPS command run was
read-only (`Get-*`, `dir`, `query`, `schtasks /query`).

Motivated by the 2026-08-04 incident: reboot + logon storm drove the box to
100% CPU with ~405 MB free, RDP black-screened, and recovery was fragile.

**Binding constraint (user decision, 2026-08-04): no hardware/RAM upgrade.
Zero recurring cost increase. The plan must fit the box as-is.** See Section E.

---

## 0. Measured baseline (2026-08-04 ~09:05-09:20 local, VPS 38.247.162.198)

Everything below is measured, not estimated, unless marked *(est.)*.

### 0.1 The box

| Fact | Measured value |
|---|---|
| Hypervisor | Hyper-V (`Win32_ComputerSystem`: Microsoft Corporation / Virtual Machine) |
| CPU | Intel Xeon E5-2667 v2 @ 3.30GHz, **1 core / 2 logical** |
| RAM at 08:57 (boot) | `TotalVisibleMemorySize` = **4,095 MB** |
| RAM at 09:14-09:20 | `TotalVisibleMemorySize` = **2,047 MB** (re-sampled 3x, stable) |
| Free RAM, steady state | ~685 MB |
| Committed bytes | 1,219 MB |
| Pagefile | 1,536 MB allocated, CurrentUsage 0, PeakUsage 0 |
| Disk C: | 24.01 GB used / **35.87 GB free** |
| CPU, steady state | 2-6% (sampled 5x @1s) |

**Finding B-1 (important, changes the framing): the guest's RAM is not
constant.** It reported 4,095 MB at boot and 2,047 MB fifteen minutes later.
That is Hyper-V dynamic-memory ballooning: the host hands out startup RAM then
reclaims down toward a floor. Practical consequences:

- The user's "2 GB" figure is correct and is the number to design against.
  4 GB is a boot-time transient you cannot rely on.
- **Any headroom this plan frees can be reclaimed by the host anyway.** The
  goal is therefore not "more free MB" for its own sake — it is *shrinking the
  peak*, so the reboot/logon storm no longer collides with the balloon.
- This also explains the incident's shape: the storm happens exactly when
  startup RAM is being clawed back.

**Finding B-2: CPU, not RAM, is the tighter constraint.** One physical core.
Steady state is a comfortable 2-6%, but *every* concurrent startup, scan, or
watchdog spawn serialises onto that single core. The incident's 100% CPU is
the direct cause of the black screen and of the 16-minute heartbeat run in
0.3 below. Proposals are therefore ranked on *avoided concurrent work* as much
as on megabytes.

**Finding B-3: disk is a non-issue.** 35.87 GB free. `C:\AutoTrade` totals
~232 MB, 218 MB of which is `.venv`. There is essentially nothing worth
deleting for space (see Section A.3) — the brief's hypothesis that
`data/historical` and `backtest_reports` were bloating the VPS is **not borne
out**: they are absent. Report this as closed, don't spend effort there.

### 0.2 Current runtime state — and two live defects

At the time of inspection the full stack was running **in Session 0**, with:

- `AutoAdminLogon = 0` — **auto-logon is disabled.**
- `query session`: console Session 1 `Conn` with **no username** (sitting at
  LogonUI); Session 0 = services.
- `quser`: "No User exists".

**Finding D-1 (defect, live): the runbook's central premise is currently
false, in both directions.** `docs/vps_deployment.md` Section 6a says MT5
requires an interactive Session 1 created by auto-logon. Right now auto-logon
is *off* (most likely wiped by the provider's cloudbase-init, which is observed
running `SetDNS.ps1` and `--noreset_service_password` on this boot), yet
`terminal64.exe`, the MQL5 exporter Service, the shadow loop, the dashboard
and the Telegram listener are **all running in Session 0 and working** — the
calendar export file was written at 09:17:42 against a 09:20:29 clock, i.e.
genuinely fresh.

This cuts two ways and both matter:

- *Bad:* the documented recovery path (auto-logon then "At log on" triggers) is
  not armed. After a cloudbase-init reboot there is no guarantee anything
  starts. This is precisely why today's recovery was fragile.
- *Good:* the "MT5 needs an interactive desktop" constraint appears **not to
  be binding in practice** for headless operation. That is the single most
  useful discovery here, because it unblocks running the core as
  session-independent tasks (Phase 5) — the same way `AutoTrade Cloudflared`
  already correctly runs as SYSTEM on a boot trigger.

*Honesty caveat:* I could not fully determine which parent launched the
Session-0 processes at 09:05 (the 08:51 heartbeat run is the likely culprit,
having overrun and been killed — see 0.3). Treat Session-0 viability as
**strongly indicated but requiring one deliberate controlled test** before
Phase 5 relies on it. Do not skip that test.

**Finding D-2 (defect, live): off-box backups are probably not happening.**
`ops/backup_db.py` writes to `C:\Users\Administrator\My Drive\AutoTrade_Backups`
(11 items present, newest `trade_journal_paper_20260804_003017.sqlite`, task
`AutoTrade DB Backup` LastTaskResult = 0). But **`GoogleDriveFS.exe` is not
running**, and it is started from an *HKCU Run key* — i.e. only on interactive
logon, which per D-1 is not happening. `DEST_DIR.mkdir(parents=True)` happily
creates/uses a plain local folder whether or not Drive is mounted, so the task
reports success while the files may never leave the box.

This is the **exact failure mode the 2026-07-28 audit already fixed once**
("nightly backups reporting success while providing zero real off-box
protection"). It has silently regressed via a different mechanism. Given the
calendar archive is designated the irreplaceable dataset, this is the highest
*correctness* risk in this document, independent of any leanness work.

### 0.3 Scheduled tasks (measured)

| Task | Principal | Trigger | Last result | Reading |
|---|---|---|---|---|
| AutoTrade Cloudflared | **SYSTEM** | Boot | 267009 (running) | Correctly session-independent — the model to copy |
| AutoTrade Shadow Loop | Interactive/Admin | At logon | 0 | Launcher returns immediately |
| AutoTrade Dashboard | Interactive/Admin | At logon | **3221225786** (`0xC000013A`, terminated) | Killed |
| AutoTrade Telegram Control | Interactive/Admin | At logon | **3221225786** | Killed |
| AutoTrade Watchdog | Interactive/Admin | At logon | **3221225786** | Killed — **and redundant, see P3** |
| AutoTrade Heartbeat | Interactive/Admin | Time, **repeat PT10M** | **267014** (`SCHED_S_TASK_TERMINATED`) | Overran and was killed |
| AutoTrade Daily Report | Interactive/Admin | Daily 09:00 | 0 | Fine |
| AutoTrade DB Backup | Interactive/Admin | Daily 00:30 | 0 | "Success" — but see D-1/D-2 |
| MicrosoftEdgeUpdateTaskMachineUA | SYSTEM | Daily, **repeat PT1H** | 0 | Not needed; hourly CPU spike |
| MicrosoftEdgeUpdateTaskMachineCore | SYSTEM | Logon + daily | 0 | Not needed |

**Finding D-3: the heartbeat ran from 08:51 to ~09:07 (16 minutes) on a
10-minute repeat before Task Scheduler killed it.** On a 1-core box under the
boot storm, the recovery mechanism itself became a load source and overlapped
its own next cycle. Mid-flight it killed `terminal64.exe` at 09:04 (calendar
recovery, `calendar_export_watchdog_state.json` last_restart_attempt
`2026-08-04T14:04:24Z`). Self-healing did ultimately work — but it was racing
itself. This is a real fragility multiplier during exactly the window that
matters.

---

## A. Inventory

Memory is `WorkingSet`. **Caveat: WS double-counts shared pages, so per-process
figures are an upper bound and the column does not sum to true consumption.**
Deltas between "with X" and "without X" are the trustworthy numbers.

### A.1 Processes

| Process | Measured RAM | CPU | Why it's there | Verdict |
|---|---|---|---|---|
| `terminal64.exe` (MT5) | **157.2 MB** | idle-low | Irreplaceable: execution venue + hosts the MQL5 calendar exporter Service | **KEEP** (slim config, P6) |
| Shadow loop (`run_shadow_loop.py`) | **97.5 MB** (3.7 venv stub + 93.8 real) | H1-cadence, near-idle | Irreplaceable: the trading job | **KEEP** |
| Dashboard (`run_dashboard.py`) | **98.4 MB** (3.7 + 94.7) | idle | Convenience web UI; imports pandas + MetaTrader5 + Flask | **MAKE-ON-DEMAND** — biggest single win |
| Telegram listener | **65.3 MB** (3.7 + 61.6) | negligible (30s long-poll) | Phone-facing control + alerts; only path to recover other services remotely | **KEEP** — cheapest phone surface |
| `cloudflared.exe` | **19.7-21 MB** | negligible | Publishes dashboard at trade.kylerlink.com; Telegram Web App button target | **KEEP** (already SYSTEM/boot; cheap) |
| `GoogleDriveFS.exe` | **not running**; 150-300 MB *(est.)* across its processes when it is; 408 MB installed | sync + FS filter | Sole purpose: carry nightly backups off-box | **REMOVE** then replace (P2) |
| Heartbeat spawn (per 10 min) | `powershell.exe` ~70 MB + `python.exe` ~50 MB, transient | **overran to 16 min under load** | Watchdog/restart/ping | **KEEP function, trim shell (P5)** |
| `run_loop_watchdog.py` | 60-90 MB *(est.)* when it runs | low | Superseded alert-only watchdog | **REMOVE task (P3)** |
| `spoolsv.exe` (Print Spooler) | **22.2 MB** | idle | Windows default; no printer on a trading VPS | **REMOVE (P3)** |
| Edge Update tasks | transient | **hourly spike** | Windows default | **REMOVE (P3)** |
| cloudbase-init (2x python + powershell) | **93.8 + 50.8 + 91.2 MB** at boot | high at boot | Provider-controlled | **KEEP** (cannot remove; plan around it) |

**AutoTrade-owned steady-state total today: ~438 MB**
(157.2 + 97.5 + 98.4 + 65.3 + 20).

### A.2 Data folders on the VPS

| Path | Size | Verdict |
|---|---|---|
| `.venv` | 218.6 MB (9,532 files) | **KEEP** — required; see P7 for why *not* to trim it |
| `.git` | 6.9 MB | KEEP |
| `data/db` | ~2 MB | **KEEP** — core state |
| - `news_calendar_history.csv` | 10.6 KB, 143 lines | **KEEP — irreplaceable, never leaves** |
| - `trade_journal_paper.sqlite` | 36 KB (**stale since 7/24**) | KEEP |
| - `trade_journal_paper.sqlite-wal` | **1,862.9 KB** | KEEP — but **checkpoint it (P8)** |
| `logs/` | 0.4 MB (18 files) | KEEP — add retention (P8); one log already hit 162 KB |
| `experiments/` | 1.1 MB (32 files) | **KEEP** — trivial disk, zero runtime. Removing it fights git for no gain |
| `tests/` | 1.1 MB | KEEP — same reasoning |
| `backups/` (local) | 0.4 MB (9 files) | KEEP — pre-change safety copies |
| `src`, `docs`, `scripts`, `ops`, `config`, `mql5` | ~1.7 MB total | KEEP |
| **`data/historical/`** | **`.gitkeep` only — EMPTY** | **Nothing to do.** The VPS never generated its own CSVs |
| **`backtest_reports/`** | **ABSENT** | **Nothing to do** |

### A.3 MT5 data directory (167.7 MB) and adjacent

| Path | Size | Verdict |
|---|---|---|
| `bases/ICMarketsSC-Demo` | 82.8 MB | KEEP (re-downloadable; trim via "Max bars", P6) |
| `bases/Default` | 44.1 MB | KEEP |
| `MQL5/Services/NewsCalendarExporter.ex5` | 15.2 KB | **KEEP — irreplaceable function** |
| `MQL5/Experts/Examples`, `Free Robots`, etc. | ~26 MB of shipped samples | REMOVE (optional, disk-only, low value) |
| `Terminal/Community` (`mql5.codebase.en.dat` 90.3 MB) | **105.3 MB** | **REMOVE** — pure MQL5 marketplace/codebase cache, zero operational role |
| `C:\Program Files\Google` | **408 MB** | **REMOVE** with P2 |

**Honest summary of "data that should not be on the VPS": there is very little,
and none of it matters for RAM.** The only genuinely pointless bytes are the
105 MB MQL5 Community cache and the 408 MB Google Drive install. Since disk has
35.87 GB free, **these are worth doing only as side effects of P2/P6, not as
goals.** The VPS's problem is processes and peaks, not stored data.

---

## B. Ranked proposals

Ranked by (headroom bought at the peak) x (risk reduction) / effort.

### P1 — Dashboard on-demand instead of always-on  <== biggest single RAM win
- **Saves: 98.4 MB steady state (measured)** — 22% of the AutoTrade stack, and
  ~14% of all free RAM. Also removes a pandas+MetaTrader5+Flask import storm
  from the boot window, which on 1 core is worth more than the megabytes.
- **Keep `cloudflared` always-on** (20 MB, already SYSTEM/boot,
  session-independent): the hostname and the Telegram Web App button keep
  working, and there is no start/stop orchestration to build. Only the 98 MB
  Python process becomes on-demand.
- **Design:** add a Telegram `/dashboard` command that launches
  `run_dashboard.py` (reusing the existing `pid_file` + `service_watchdog`
  patterns), replies with the URL, and auto-stops after ~30 min idle.
- **Required change, do not miss:** `scripts/run_health_check.py` currently
  calls `check_and_restart("Dashboard", ...)` every cycle. That **must** be
  removed/gated or the heartbeat will resurrect the dashboard within 10
  minutes and silently undo this entire proposal. Same trap the
  `manual_halt_flag` work already hit once.
- **Risk: low.** Worst case the dashboard is 20-30 s slower to appear on first
  use (pandas import on 1 core). Trading is untouched.
- **Effort: small-medium** (one command handler + idle timer + one deletion).

### P2 — Fix off-box backup (Finding D-2)  <== highest correctness value
- Not primarily a leanness item, but it is the most serious live defect and it
  *also* removes the single heaviest optional process on the box.
- **Replace Google Drive for Desktop with `rclone`** invoked by the existing
  nightly backup task: a single binary that runs, copies ~70 KB, and exits.
  **Steady-state cost: zero.** Saves 150-300 MB *(est.)* whenever a logon
  occurs, plus 408 MB disk, plus removes an interactive-logon dependency from
  the backup path.
- *Alternative:* have the dev PC `scp`-pull nightly (SSH already works — this
  investigation used it). Simpler, adds nothing to the VPS, but makes backups
  depend on a machine that is explicitly **not production-managed**. Since
  backups are not latency-critical and the current state is *no off-box copy at
  all*, this is an acceptable fallback — but rclone is preferred precisely
  because it keeps the irreplaceable dataset's protection on the VPS's own
  schedule.
- **Verify first**, before deciding: confirm whether the 11 files in `My Drive`
  ever actually synced. Do not assume either way.
- **Risk: low. Effort: small.**

### P3 — Delete redundant tasks and Windows cruft (no code change)
- Remove the **`AutoTrade Watchdog`** task: `run_loop_watchdog.py` is
  explicitly documented as superseded, "secondary, alert-only (no restart)...
  when manually launched" — yet it is wired to auto-start at every logon.
  Saves 60-90 MB *(est.)* per logon and one process off the critical core.
- Disable **Print Spooler** (22.2 MB measured, no printer).
- Disable both **Edge Update** tasks (removes an hourly CPU spike on 1 core).
- Delete `Terminal/Community` cache (105 MB disk).
- Add **Defender exclusions** for `C:\AutoTrade` and the MT5 data dir — this is
  a deliberate, scoped security trade-off, justified on a single-purpose box
  where scan CPU directly threatens the trading loop.
- **Risk: very low, all trivially revertible. Effort: minimal.**

### P4 — Make the core session-independent (the real reliability fix)
- Convert the AutoTrade tasks from `LogonType=Interactive` to a
  session-independent principal, **exactly as `AutoTrade Cloudflared` already
  is** (SYSTEM + boot trigger, observed working).
- This is the fix for the actual incident: today, after a cloudbase-init
  reboot with `AutoAdminLogon=0`, *nothing is guaranteed to start*.
- **Gated on the Phase-0 test** confirming MT5 + the MQL5 exporter Service
  genuinely operate in Session 0 (D-1 indicates yes; prove it).
- If the test fails: fall back to restoring a hardened auto-logon and
  re-asserting it *after* cloudbase-init runs, rather than fighting it.
- **Saves: no RAM directly** — buys reboot survivability, which is the whole
  point of the exercise.
- **Risk: medium** (touches startup of the irreplaceable jobs) — hence its own
  phase, with an explicit revert.
- **Effort: medium.**

### P5 — Trim the heartbeat (Finding D-3)
- Drop the `powershell.exe` wrapper (~70 MB transient every 10 min) by moving
  the healthchecks.io ping into `run_health_check.py` via `urllib` — it already
  imports nothing heavier. Halves the per-cycle process spawn on a 1-core box.
- Set **"Stop the task if it runs longer than 5 minutes"** so a cycle can never
  again overlap its own successor (today: 16 min).
- **Saves ~70 MB transient x 144/day; removes a self-collision failure mode.**
- **Risk: low. Effort: small.**

### P6 — Slim the MT5 terminal
- Market Watch: **hide every symbol except XAUUSD** (fewer ticks processed —
  a genuine 1-core CPU saver).
- "Max bars in chart" to 5,000; close all unused charts (the `profiles` folder
  is already empty of chart files, so this is likely near-optimal already).
- Disable MQL5 Community / Market / Signals / News panels (stops the 105 MB
  cache regrowing and removes background network+CPU).
- **Saves 20-40 MB *(est.)* + steady CPU.**
- **Risk: low**, but requires one interactive session to apply, and Market
  Watch changes must not hide the traded symbol. **Effort: small.**

### P7 — venv/Python memory trims — **REJECTED, do not do this**
Listed because the brief asked. Options considered: `-X frozen_modules`,
stripping `.venv`, `-OO`. The 218 MB `.venv` is *disk*, not RAM, and disk is
free. Python's ~90 MB WS is dominated by pandas/MetaTrader5 which are genuinely
needed. **Expected saving: near zero; risk: breaking a working runtime for
sport.** P1 removes a whole 98 MB interpreter, which is the correct version of
this idea.

### P8 — Retention / hygiene
- **WAL checkpoint:** `trade_journal_paper.sqlite` is 36 KB and last written
  7/24, while its WAL is 1.86 MB — consistent with the known "main file can be
  weeks stale" trap. Add a periodic `PRAGMA wal_checkpoint(TRUNCATE)` so the
  main file is current and a crash risks less. (`ops/backup_db.py` uses
  `Connection.backup()` and is therefore already *correct*; this is about the
  live file, not the backup.)
- Log retention: prune `logs/*.log` beyond ~30 days. Currently 0.4 MB — a
  guardrail, not a problem.
- **Risk: low. Effort: small.**

---

## C. The minimal core — what must NEVER leave the VPS

Stated explicitly so future leanness work cannot erode it. **Anything not on
this list is negotiable; everything on it is not.**

1. **The MT5 terminal (`terminal64.exe`)** — the execution venue, and the host
   process for (2). Non-negotiable.
2. **The MQL5 calendar exporter Service** (`NewsCalendarExporter.ex5`) and its
   output `AutoTradeNewsCalendar.csv` — feeds live news checks; when it goes
   stale the system fail-safe-vetoes every USD signal.
3. **`data/db/news_calendar_history.csv`** — the append-only archive. **The one
   dataset in the entire system that cannot be reconstructed if lost.** It must
   stay on the VPS *and* be continuously copied off it (P2).
4. **The shadow loop** (`run_shadow_loop.py --adapter demo`) and its live state:
   `trade_journal_paper.sqlite` (+ WAL), `position_metadata.json`,
   `circuit_breaker_state.json`, the PID/flag files.
5. **Enough ops to keep 1-4 alive and to tell the user when they aren't:** the
   heartbeat/health-check cycle, the external healthchecks.io dead-man's
   switch, and the nightly backup.
6. **The Telegram listener** — the only remote path that can recover the other
   services when they are down, and the only alerting surface that works when
   the dashboard doesn't. Cheapest phone-facing surface at 65 MB; **do not
   trade it away for RAM.**

**Corollary rules:**
- Never move the *trading decision* off this box. Backtests, experiments and
  promotion runs stay on the dev PC, as they already do.
- The dashboard is a **convenience**, not core (hence P1). The Telegram bot is
  **core**, because it is the recovery path.
- The dev PC is **not production-managed**. It may host convenience and backup
  *destinations*; it must never become a dependency of items 1-5.

---

## D. Phased migration plan

Each phase is independently shippable and revertible. **Do not batch them** —
on a 1-core box, one change at a time is also how you keep verification honest.

### Phase 0 — Verify (read-only, no changes)
1. Determine whether the `My Drive` backups ever synced (D-2). Check Google
   Drive's own sync state / the account's web view.
2. **Controlled Session-0 test** (gates Phase 5): with no interactive logon,
   confirm MT5 stays connected and `AutoTradeNewsCalendar.csv` keeps updating
   across a full reboot.
3. Record a clean baseline: free RAM, per-process WS, CPU, during and after a
   reboot.
- **Verify:** you can state, with evidence, whether backups leave the box and
  whether Session 0 is genuinely viable.
- **Revert:** n/a.

### Phase 1 — P3 (cruft removal)
- Remove `AutoTrade Watchdog` task; disable Print Spooler + Edge Update tasks;
  delete `Terminal/Community`; add Defender exclusions.
- **Verify:** reboot; loop starts; exporter fresh within 20 min; Telegram
  `/status` answers; free RAM up ~22 MB.
- **Revert:** re-enable services/tasks (all one-liners). Cache regenerates.

### Phase 2 — P2 (backup correctness)
- Install rclone (or wire the dev-PC pull); repoint `ops/backup_db.py`'s
  `DEST_DIR`; run once manually; **confirm the file is visible off-box**;
  only then uninstall Google Drive for Desktop.
- **Verify:** next nightly run produces a new off-box copy of *both* the
  journal and `news_calendar_history.csv`, confirmed from another machine.
- **Revert:** reinstall Drive, restore `DEST_DIR`. Keep the local
  `backups/` copies throughout as a floor.

### Phase 3 — P5 (heartbeat trim)
- Fold the ping into `run_health_check.py`; drop the PowerShell wrapper; set a
  5-minute task time limit.
- **Verify:** healthchecks.io keeps receiving pings on schedule; kill the loop
  once and confirm it is detected, restarted, and alerted exactly as before.
- **Revert:** restore `ops/heartbeat.ps1` as the task action.

### Phase 4 — P1 (dashboard on-demand)  <== the headline saving
- Remove the Dashboard entry from `run_health_check.py`'s auto-restart list;
  disable the `AutoTrade Dashboard` logon task; add Telegram `/dashboard`
  start + idle auto-stop.
- **Verify:** (a) free RAM up ~98 MB; (b) **wait a full 15 min and confirm the
  heartbeat does NOT resurrect it**; (c) `/dashboard` brings it up and
  trade.kylerlink.com serves; (d) it auto-stops when idle; (e) the shadow loop
  and Telegram alerts are unaffected throughout.
- **Revert:** re-enable the logon task and restore the health-check entry.

### Phase 5 — P4 (session independence)
- **Only if Phase 0.2 passed.** Convert tasks to SYSTEM/boot triggers,
  mirroring `AutoTrade Cloudflared`. Do one task at a time, loop last.
- **Verify:** hard reboot with nobody logged on: loop trading, exporter fresh,
  Telegram "started" message arrives unattended. Repeat twice.
- **Revert:** restore Interactive principals + re-arm auto-logon.

### Phase 6 — P6 + P8 (MT5 slimming, retention)
- Market Watch to XAUUSD only; max bars 5,000; disable Community/Market/
  Signals/News. Add WAL checkpoint + log pruning.
- **Verify:** XAUUSD still quotes and trades; exporter still fresh; main
  `.sqlite` mtime now current rather than weeks stale.
- **Revert:** re-show symbols; retention changes are additive.

---

## E. The RAM-upgrade alternative

**Declined by the user (2026-08-04): no hardware/RAM upgrade, zero recurring
cost increase.** The entire recommendation above is therefore software-only.
*(Noted only for completeness: a 2-to-4 GB Windows VPS bump is typically
~$5-10/mo — but it is moot, and Finding B-1 means the host can balloon RAM away
regardless, so it was never the clean fix it appears to be.)*

### Expected post-plan steady state

| | Today (measured) | After plan | Delta |
|---|---|---|---|
| MT5 terminal | 157.2 MB | ~130 MB *(est., P6)* | -27 |
| Shadow loop | 97.5 MB | 97.5 MB | 0 |
| Dashboard | 98.4 MB | **0** (on-demand) | **-98** |
| Telegram listener | 65.3 MB | 65.3 MB | 0 |
| cloudflared | 20 MB | 20 MB | 0 |
| **AutoTrade total** | **~438 MB** | **~313 MB** | **-125** |
| Print Spooler | 22.2 MB | 0 | -22 |
| **Free RAM (of 2,047 MB)** | **~685 MB** | **~830 MB** *(est.)* | **+145** |

**Peak / reboot-window reduction — the number that actually addresses the
incident:**

| Avoided at the peak | *(est.)* |
|---|---|
| Dashboard never starts at logon (P1) | ~98 MB + a pandas import on 1 core |
| Google Drive never starts (P2) | ~150-300 MB |
| Redundant watchdog never starts (P3) | ~60-90 MB |
| PowerShell heartbeat wrapper (P5) | ~70 MB, and no 16-min self-overlap |
| **Total peak relief** | **~380-560 MB** |

Against an incident low-water mark of ~405 MB free, **removing ~380-560 MB from
the same window is the difference between a storm and a non-event** — and it
removes roughly four concurrent startups from a single CPU core, which is the
larger effect.

### Recommendation

Software-only is not merely the constrained choice here — on the evidence it is
the *better* one:

1. **P1 buys the most headroom.** *The single change that buys the most
   headroom is retiring the always-on dashboard: 98.4 MB measured, the largest
   single reclaimable block on the box, ~22% of the AutoTrade stack.*
2. **But do Phase 0 and P2 first in time.** A silently-broken off-box backup of
   the irreplaceable calendar archive (D-2) outranks any megabyte, and P4 must
   not proceed on an unverified assumption.
3. **P4 is the actual cure for the incident.** RAM was the symptom; *nothing
   being guaranteed to start after a cloudbase-init reboot* is the disease. No
   amount of RAM would have fixed `AutoAdminLogon=0`.

**Sequence: Phase 0 - 1 - 2 - 3 - 4 - 5 - 6** (as ordered in Section D; note
Phase 2 = backup correctness is the first *change*). Stop after Phase 4 and
re-measure; Phases 5-6 may prove unnecessary.
