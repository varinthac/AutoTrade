# AutoTrade VPS Deployment Runbook

This is an execution checklist for moving AutoTrade from the home PC to an
unattended Windows VPS. It covers **infrastructure migration only**.

**This move does NOT change trading status.** The system stays exactly where
it is today: `auditor.current_stage: backtest` in `config/base.yaml`,
pre-promotion-gate, demo-money paper trading (`--adapter demo --mode paper`).
An out-of-sample promotion-gate run already failed
(`profit_factor: actual=1.234`, threshold `>= 1.3`). Moving to a VPS is an
ops change; going live with real money is a separate, Auditor-gated decision
that this document does not touch or advance.

---

## 0. Before you start

- [ ] Code review / security-auditor / acceptance-verifier passes are done
      (they are, as of this doc -- no code changes required by this runbook).
- [ ] Decide a cutover window when the account is flat (no open
      XAUUSD positions) -- check the dashboard or MT5 terminal first. This
      matters for state-file migration (Section 4) and avoids any chance of the
      same account being traded from two machines at once.
- [ ] Have a private git remote ready (see Section 3) -- this repo currently has
      no remote configured (git remote -v is empty, local-only repo).

---

## 1. VPS selection

This workload is light: one MT5 terminal (GUI app, but idle most of the
time), a handful of Python processes polling on H1 bar closes (~hourly, not
HFT), and a small Flask dev-server dashboard. Per the small-account
philosophy, do not over-buy.

- **OS**: Windows Server 2019/2022 Standard is fine and is what most budget
  VPS providers offer by default -- no need to seek out a Windows 10/11
  desktop-OS VPS specifically. The customtkinter desktop GUI
  (AutoTrade_GUI.bat) is optional on the VPS (Telegram + the dashboard
  already give you remote control); skip installing/running it there unless
  you want it.
- **Specs**: 2 vCPU / 4 GB RAM / ~60-80 GB SSD is comfortably enough
  (MT5 terminal + Python + SQLite + Flask). Don't pay for more "because
  trading" -- this system trades on H1 bar closes, not tick-scalping.
- **Datacenter proximity to IC Markets' servers**: not a meaningful
  factor here. Proximity/latency matters for HFT/scalping; at H1
  bar-close cadence, tens or even a couple hundred ms of extra latency is
  irrelevant. Pick a region based on convenient RDP latency for you
  (for admin sessions), not execution speed.
- Any mainstream VPS provider works (dedicated "Forex VPS" providers exist
  and pre-tune for MT4/MT5, but are not required -- a generic Windows VPS
  from any reputable provider is fine and usually cheaper).

## 2. Initial provisioning

1. [ ] Provision the VPS, RDP in as Administrator.
2. [ ] Windows Update fully, reboot.
3. [ ] Install Python 3.12.x (match the home PC's tested version --
       `.venv\Scripts\python.exe --version` on the home PC currently reports
       Python 3.12.10; pyproject.toml only requires >=3.11 but match
       the exact minor version that's actually been running, not just the
       floor). Get it from python.org, 64-bit installer -- the
       MetaTrader5 package requires 64-bit Python to match the MT5
       terminal's own 64-bit build. Check "Add to PATH" during install.
4. [ ] Install the MT5 terminal (download from IC Markets or from within
       an existing installer). Launch it once interactively over RDP, log
       into the demo account by hand, confirm the server name matches what
       you'll put in `.env` (MT5_SERVER), and dismiss any first-run
       dialogs (EULA, "add symbol" popups, update prompts). Do this manual
       run once before relying on automated startup -- `mt5_session()` in
       `src/autotrade/common/mt5_connection.py` calls `mt5.initialize()`
       with login/password/server and will auto-launch/log in
       terminal64.exe on subsequent runs, but a first-run dialog blocking
       in the background could hang that auto-launch the first time.
5. [ ] Create a dedicated local Windows user account for running AutoTrade
       (not strictly required, but keeps things tidy -- a plain
       Administrator-equivalent account is fine for a single-user personal
       system; don't over-engineer this with enterprise-style least-privilege
       tiers).

## 3. Getting the code onto the VPS

**Recommendation: git clone from a private remote**, not a manual zip/file
copy. Rationale: `.gitignore` already correctly excludes everything that
must never leave the home PC via a bulk copy (`.env`, `data/db/*`,
`data/historical/*`, `logs/`, `.venv/`) -- a `git clone` naturally respects
that; a manual folder copy (e.g. dragging the whole D:\AutoTrade folder)
would NOT, and could silently drag your live `.env` credentials and trade
journal DB along with it over whatever transport you used.

Steps:
1. [ ] Since there is currently no remote, create one -- a private
       GitHub/GitLab repo is simplest and is fine here: the security audit
       found nothing in the codebase that's sensitive (no secrets are ever
       committed; `.env` is git-ignored regardless of host), so the choice
       of host is not itself a security-sensitive decision.
       `git remote add origin <url>` then `git push -u origin main`.
2. [ ] On the VPS: `git clone <url> C:\AutoTrade` (or wherever you want it).
3. [ ] Create the venv fresh on the VPS -- never copy `.venv/` across
       machines (it's platform/path-specific and byte-compiled):
       `python -m venv .venv`
4. [ ] Install dependencies from the lockfile (next section), not directly
       from `pyproject.toml`'s loose ranges.
5. [ ] Copy `.env.example` to `.env` on the VPS and fill in the rotated
       credentials from Section 5 below -- never copy the home PC's actual
       `.env` file itself, even though it's git-ignored (transit of a real
       secrets file is exactly the "potentially exposed in transit" case
       the security-auditor flagged -- type/paste the new rotated values in
       directly over RDP instead).

### What NOT to copy, and the one deliberate exception

- `.env` -- never copy this file itself. Rotate credentials (Section 5)
  and create a fresh `.env` on the VPS with the new values.
- `data/db/shadow_loop.pid`, `kill_switch.flag`, `stop_request.flag` --
  pure runtime process state, machine-specific. Let these start fresh
  (self-initializing) on the VPS.
- `data/db/notify_gate_state.json`, `notify_last_daily.json` -- dedupe
  bookkeeping for Telegram notifications. Safe to start fresh; worst case
  is one duplicate daily-report message on the VPS's first day.
- `data/db/position_metadata.json`, `circuit_breaker_state.json` -- these
  track in-flight state (breakeven/trail progress per open position,
  today's accrued daily-loss). This is exactly why the cutover window
  should be a flat-book moment (Section 0) -- with no open positions,
  there is nothing in-flight to lose track of, so these can also safely
  start fresh on the VPS. (If you cannot arrange a flat-book cutover,
  these two files would need deliberate manual migration alongside the DB
  below -- more fragile, avoid it if at all possible.)
- **`data/db/trade_journal_paper.sqlite` -- deliberately MIGRATE this
  one**, via a one-time manual copy (RDP clipboard file copy, or `scp`/
  secure file transfer), done during the flat-book cutover window,
  immediately after stopping the loop on the home PC and before starting
  it on the VPS. Justification: this file is the paper-trading track
  record that `run_auditor.py promotion --gate paper --weeks-elapsed N`
  measures elapsed time and results against. Starting fresh would reset
  that clock for no reason connected to any strategy change -- the
  acceptance-verifier was explicit that this move changes nothing about
  promotion status, which implies continuity, not a reset. Bring
  `data/db/borderline_log.jsonl` along at the same time for the same
  reason (small file, harmless, preserves expectancy-tracking continuity).
- Do this copy manually and deliberately, outside of git, in one step
  during the cutover window -- not as an ongoing sync mechanism.

## 4. Dependency lockfile (none currently exists)

`pyproject.toml` uses wide ranges (`pandas>=2.2,<4.0`, etc.) with no pin --
a fresh `pip install` on the VPS could resolve different versions than
what's actually been tested on the home PC.

Concrete steps (no new tooling needed -- plain `pip freeze`, proportionate
to this project's size):

1. [ ] On the home PC, build a clean venv containing only runtime deps
       (not the existing `.venv`, which may have extra dev/security-audit
       tooling like pip-audit/cyclonedx-python-lib mixed in that doesn't
       belong in a runtime lock):
       ```
       python -m venv .venv-lock
       .venv-lock\Scripts\pip install .
       .venv-lock\Scripts\pip freeze > requirements-lock.txt
       ```
2. [ ] Commit `requirements-lock.txt` to the repo.
3. [ ] On the VPS, after cloning and creating `.venv`, install from the
       lock instead of from `pyproject.toml` directly:
       ```
       .venv\Scripts\pip install -r requirements-lock.txt
       .venv\Scripts\pip install -e . --no-deps
       ```
       (`--no-deps` on the editable install so it registers the autotrade
       package/console-scripts without re-resolving dependencies against
       the loose pyproject.toml ranges a second time.)
4. [ ] Regenerate `requirements-lock.txt` deliberately (repeat step 1)
       whenever you intentionally upgrade a dependency and re-test -- not
       automatically, not on every install.

## 5. Credential rotation (do this as part of the move, not optional)

Treat anything that existed in the home PC's `.env` as potentially exposed
in transit once you start setting up a new machine.

**MT5 demo account password:**
1. [ ] Log into the MT5 terminal (or the IC Markets client portal) on the
       home PC with the current credentials.
2. [ ] Use the terminal's "Change Password" (or the broker portal) to set a
       new password for the demo account login.
3. [ ] Update MT5_PASSWORD in a fresh `.env` on the VPS with the new
       value (typed directly over RDP, not pasted from a copied file).
4. [ ] Do not update the home PC's `.env` to match unless you intend to
       keep using the home PC as a live fallback (Section 9) -- if so,
       update both; if the home PC is being fully retired, its old `.env`
       becomes harmless once the password itself is changed.

**Telegram bot token:**
1. [ ] Open a chat with @BotFather on Telegram.
2. [ ] Send `/revoke` (or `/token` then choose to regenerate) for the
       existing AutoTrade bot to invalidate the old token immediately.
3. [ ] BotFather returns a new token -- put it in TELEGRAM_BOT_TOKEN in
       the VPS's fresh `.env`.
4. [ ] TELEGRAM_CHAT_ID does not need rotating (it's your own chat id,
       not a secret credential) -- copy it over as-is.
5. [ ] Confirm `scripts/run_telegram_control.py` and `notify()` both work
       with the new token before decommissioning anything on the home PC
       (send /status from Telegram once the VPS is up, Section 10).

**Other API keys** (FINNHUB_API_KEY, FMP_API_KEY, etc.): these are all
currently unused (every real economic-calendar provider candidate found so
far is paywalled -- see `council/news_calendar.py`'s docstring; the system
runs on StubNewsCalendarProvider/MQL5CalendarProvider instead). Leave
blank on the VPS unless/until one of these is actually wired up; no
rotation needed for keys that were never functionally in use.

## 6. Unattended reliability

### 6a. Auto-start on boot (shadow loop, dashboard, Telegram bot)

The core wrinkle: `terminal64.exe` (launched by `mt5.initialize()` inside
`mt5_session()`) is a real Win32 GUI app -- it needs an interactive desktop
session to render/operate correctly, not a Session-0 service context. A
Task Scheduler task set to "Run whether user is logged on or not" runs in
Session 0 with no interactive window station, and MT5 will not work
reliably there. The standard, well-established pattern for this exact
situation (the same one retail forex VPS hosts use for MT4/MT5):

1. [ ] Enable Windows auto-logon for the dedicated account from Section 2,
       so an interactive desktop session (Session 1) exists automatically
       after every reboot with no human needing to RDP in. Use Sysinternals
       Autologon.exe (safer than hand-editing the registry -- it encrypts
       the stored password via LSA secrets rather than plaintext registry).
2. [ ] Create three Task Scheduler tasks, each:
       - Trigger: "At log on", specific user = the dedicated account,
         with a 1-2 minute delay ("Delay task for" under trigger settings)
         so networking is up before MT5 tries to connect.
       - Security options: "Run only when user is logged on" (NOT
         "whether user is logged on or not" -- that's the Session-0 trap
         above).
       - Actions (call the underlying python.exe directly, not the .bat
         files -- AutoTrade_Start.bat/AutoTrade_Stop.bat each end in a
         `pause`, which is harmless for interactive double-click use but
         leaves an unanswered "press any key" prompt sitting in a
         scheduled-task-launched console forever):
         - Shadow loop: `"C:\AutoTrade\.venv\Scripts\python.exe" "C:\AutoTrade\scripts\autotrade_control.py" start`
           (this itself launches run_shadow_loop.py --adapter demo detached
           in its own console and returns immediately, per
           scripts/autotrade_control.py's do_start()).
         - Dashboard: `"C:\AutoTrade\.venv\Scripts\python.exe" "C:\AutoTrade\scripts\run_dashboard.py"`
         - Telegram control: `"C:\AutoTrade\.venv\Scripts\python.exe" "C:\AutoTrade\scripts\run_telegram_control.py"`
3. [ ] Never click "Sign out"/"Log off" on the VPS. Always disconnect the
       RDP session (the X button, or Start > Disconnect) instead --
       disconnecting keeps the Session-1 desktop and everything running in
       it alive; logging off tears the whole session down and kills MT5,
       the shadow loop, dashboard, and Telegram bot with it.
4. [ ] Test by rebooting the VPS once (during the flat-book cutover
       window, before it's the live/only copy) and confirming, without
       RDP-ing in, that a Telegram "AutoTrade started" message arrives on
       its own (the shadow loop sends this via notify() right after
       connecting -- see run_shadow_loop.py's main()).

### 6b. Daily Auditor report

`scripts/run_auditor.py`'s own module docstring already documents the
exact schtasks invocation to use -- reuse it, with one correction: the
docstring's example uses `--mode live`, but this system currently records
into the paper journal (`run_shadow_loop.py --mode paper`, the default),
so the scheduled task must say `--mode paper` to match what's actually
being recorded, or the report will read an empty/wrong DB:

```
schtasks /Create /TN "AutoTrade Daily Report" /SC DAILY /ST 00:15 ^
    /TR "C:\AutoTrade\.venv\Scripts\python.exe C:\AutoTrade\scripts\run_auditor.py daily --mode paper --notify"
```

Run this once (as the dedicated user, "run whether user is logged on or
not" is fine here since this task has no GUI-app dependency -- it's a
one-shot Python script that reads the DB and exits) via an elevated
schtasks command or Task Scheduler UI.

### 6c. External heartbeat + auto-restart ("the whole VPS went dark" / "the loop died and nothing said so")

Telegram alerts from AutoTrade obviously can't fire if the VPS itself, or
Windows, or the Python process, is what died -- that needs a check
external to this system. Recommended: a free dead-man's-switch service
(e.g. healthchecks.io, free tier) that expects a periodic "I'm alive" ping
and alerts you itself (email/Telegram/etc., independent of this codebase)
if the ping doesn't show up within a grace period.

**2026-07-24 incident:** the shadow loop died silently (~13:00 server time)
and `scripts/run_loop_watchdog.py` -- the console-window process meant to
alert on exactly that -- was ALSO found not running, so zero alert fired.
A process that only (re)launches "at logon" cannot recover from its own
silent death mid-session. This section's Task Scheduler task, because it
has its own *repeating* trigger, doesn't have that problem: Task Scheduler
dispatches a fresh one-shot process every cycle, so there's no long-lived
watchdog process here that can silently die. It is now the primary
mechanism for both detecting AND auto-recovering from the loop dying;
`run_loop_watchdog.py`/`AutoTrade_Watchdog_Start.bat` remain as a secondary,
alert-only (no restart) belt-and-suspenders check when manually launched.

1. [ ] Create a free healthchecks.io check with a period matching your
       Task Scheduler interval (e.g. every 10 minutes, grace period 15
       minutes) and connect its own Telegram/email integration.
2. [ ] `C:\AutoTrade\ops\heartbeat.ps1` (a deployment ops artifact, not
       part of the Python package) calls `scripts/run_health_check.py`,
       which does two things every cycle: (a) Telegram-alerts on a
       DOWN<->UP transition (`common/loop_watchdog.py`'s existing
       transition-only logic, unchanged), and (b) attempts to relaunch the
       loop (`autotrade_control.py start`) on every cycle it's found down,
       not just the first -- safe to retry unconditionally because
       `run_shadow_loop.py`'s own PID-file double-launch guard already
       refuses a second instance, so a retry racing an in-progress startup
       just harmlessly no-ops:
       ```powershell
       $status = & "C:\AutoTrade\.venv\Scripts\python.exe" `
           "C:\AutoTrade\scripts\run_health_check.py"
       if ($status -match "RUNNING") {
           Invoke-WebRequest -Uri "https://hc-ping.com/<your-check-uuid>" -UseBasicParsing | Out-Null
       }
       ```
       This only pings healthchecks.io when the loop is actually RUNNING --
       so the external alarm fires both if the VPS/scheduler itself is dead
       (no ping arrives at all) and if Windows is up but the shadow loop
       process itself has crashed (ping is skipped because it isn't
       RUNNING).
3. [ ] Task Scheduler: run this script every 10 minutes. Unlike a plain
       read-only status check, this one's restart attempt launches
       `run_shadow_loop.py`, which has the exact same interactive-desktop
       requirement Section 6a explains (MT5's GUI needs a real Session 1,
       not Session 0) -- so this task MUST use the same settings as 6a's
       three tasks: trigger "At log on" + repeat every 10 minutes, security
       option **"Run only when user is logged on"** (NOT "whether user is
       logged on or not" -- that would silently break the auto-restart's
       MT5 connection even though the heartbeat ping itself would still
       look fine). Action:
       `powershell.exe -ExecutionPolicy Bypass -File C:\AutoTrade\ops\heartbeat.ps1`

## 7. Remote access to the dashboard

The dashboard (`scripts/run_dashboard.py`) binds `127.0.0.1:8765` only, by
design, with no auth -- this must never change on the VPS. Two ways to
view it remotely, both keep that binding intact:

- **Simplest (zero extra setup)**: RDP into the VPS and open a browser
  inside that session pointed at `http://127.0.0.1:8765` -- you already
  have a full remote desktop, no tunnel needed.
- **Lighter-weight, from your home PC's own browser** (no full RDP
  session): enable the "OpenSSH Server" optional Windows feature on the
  VPS (Settings > Apps > Optional Features > OpenSSH Server, or
  `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0`), start
  the sshd service, then from the home PC:
  ```
  ssh -L 8765:127.0.0.1:8765 <user>@<vps-ip> -N
  ```
  and browse to `http://127.0.0.1:8765` locally -- traffic tunnels over
  SSH, the dashboard's port is never exposed directly to the internet.

## 8. Backup of data/db/*.sqlite

The DB is SQLite in WAL mode -- a raw file copy while the WAL is active can
be inconsistent. Use SQLite's own online-backup mechanism instead of a
plain file copy; Python's stdlib sqlite3 module has this built in
(Connection.backup()), so no extra tool/binary is needed:

1. [ ] Create `C:\AutoTrade\ops\backup_db.py`:
       ```python
       import sqlite3, datetime
       from pathlib import Path

       SRC = Path(r"C:\AutoTrade\data\db\trade_journal_paper.sqlite")
       DEST_DIR = Path(r"C:\AutoTrade\backups")
       DEST_DIR.mkdir(exist_ok=True)
       stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
       dest = DEST_DIR / f"trade_journal_paper_{stamp}.sqlite"

       src_conn = sqlite3.connect(str(SRC))
       dest_conn = sqlite3.connect(str(dest))
       with dest_conn:
           src_conn.backup(dest_conn)
       src_conn.close()
       dest_conn.close()
       ```
2. [ ] Schedule it nightly (Task Scheduler, e.g. 00:30, "whether user is
       logged on or not" is fine -- pure file I/O, no MT5/GUI dependency).
3. [ ] Get copies off the VPS, not just into a local backups\ folder (a
       backup that lives on the same disk as the original doesn't protect
       you if the VPS itself is lost/corrupted): the simplest option for a
       solo user is a synced cloud folder (OneDrive/Google Drive desktop
       client installed on the VPS, DEST_DIR pointed at the synced
       folder) -- no extra script needed, the sync client handles off-box
       transfer on its own. robocopy/rclone to remote storage is an
       equally valid alternative if you'd rather not run a sync client on
       the VPS.
4. [ ] Prune old backups periodically (keep, say, the last 30 days) -- a
       one-line Get-ChildItem/Where-Object delete-if-older-than in the
       same or a small companion script; not critical for v1, just don't
       let it grow unbounded forever.

## 9. Rollback / abort plan

Kept deliberately simple -- proportionate to a single-user personal
system, not enterprise blue-green:

- Do not decommission the home PC's setup until the VPS has run cleanly
  through at least one full unattended reboot cycle (Section 10's
  go/no-go). Nothing about this migration requires deleting anything on
  the home PC.
- Only ever run the loop on one machine at a time. Running
  `--adapter demo` on both the home PC and the VPS simultaneously against
  the same MT5 account is a real double-execution risk (the PID-file
  double-launch guard in run_shadow_loop.py is per-machine, not shared
  across machines -- it would not stop this). The cutover is: stop the
  home PC loop (AutoTrade_Stop.bat, confirm the Telegram "stopped" message
  and `autotrade_control.py status` shows NOT running) then start the VPS
  loop -- never both at once.
- If the VPS run misbehaves (crashes repeatedly, MT5 connection issues,
  Task Scheduler auto-start not working, etc.) during the trial window:
  1. `python scripts/autotrade_control.py emergency-stop --confirm` (or
     graceful `stop` if no positions are open) on the VPS.
  2. Resume on the home PC exactly as before -- its `.env`, `.venv`, and
     `data/db/` were never touched, so this is close to zero-effort: just
     double-click AutoTrade_Start.bat again.
  3. If credentials were rotated (Section 5) before the failed VPS
     attempt, make sure the home PC's `.env` has the same rotated
     MT5_PASSWORD/TELEGRAM_BOT_TOKEN before restarting there.
- No DB migration is undone by a rollback -- the home PC's own copy of
  trade_journal_paper.sqlite was never deleted (only copied to the VPS,
  Section 3), so reverting loses nothing.

## 10. Go/no-go checklist (VPS infrastructure -- NOT the Auditor's live-trading gate)

This checklist is about "is it safe to consider the VPS the one running
copy of the existing demo/paper setup" -- it is explicitly not the
Auditor's own promotion gate, which is separate, already evaluated
(currently FAIL), and untouched by this move.

- [ ] Fresh git clone on the VPS builds and installs cleanly from
      requirements-lock.txt with no manual patching.
- [ ] MT5 terminal logs in successfully via mt5_session() using the
      rotated demo credentials.
- [ ] Telegram bot responds to /status using the rotated bot token.
- [ ] Dashboard loads at 127.0.0.1:8765 inside an RDP session (and via
      SSH tunnel from the home PC, if you set that up).
- [ ] trade_journal_paper.sqlite (and borderline_log.jsonl) were migrated
      during a confirmed flat-book cutover window, and
      `run_auditor.py daily --mode paper` on the VPS reports sane,
      continuous-looking history (not an empty DB, not a gap).
- [ ] Shadow loop, dashboard, and Telegram control bot all auto-started
      correctly after a real VPS reboot, with nobody RDP-ing in -- the
      Telegram "AutoTrade started" message arrived unattended.
- [ ] The scheduled daily Auditor report (schtasks, Section 6b) fired once
      on its own and produced a Telegram message.
- [ ] The external heartbeat (Section 6c) is receiving pings on schedule,
      and you have manually killed the shadow loop process once as a test
      to confirm the heartbeat alarm actually fires when it should.
- [ ] The nightly DB backup (Section 8) ran at least once and a copy
      exists off-box (visible in the synced cloud folder / remote target,
      not just locally on the VPS).
- [ ] Home PC's copy of the setup is left intact and untouched as a
      fallback (Section 9), not deleted.
- [ ] Both the home PC's shadow loop is confirmed STOPPED and only the
      VPS's is running, before calling this "live" (no double-running).

Once every box above is checked, the VPS is a safe, unattended replacement
for the home PC -- running the exact same demo/paper setup, at the exact
same pre-promotion-gate trading status as before.
