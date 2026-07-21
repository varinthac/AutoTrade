# Multi-Account Support — Forward-Looking Implementation Plan

**Status:** Design only. Nothing here is to be implemented in the current phase. This document is the reference for when multi-account work is scheduled.

**Scope confirmed against the code** at the paths/line numbers cited below (verified 2026-07-21, branch `main`). Three design decisions are already fixed by the user and are treated as requirements, not open questions:

1. **Genuine concurrency** — N accounts trade simultaneously as N separate OS processes (not sequential switching).
2. **Full per-account config files** — `config/<account>.yaml`, each self-contained, following the existing `--config` precedent in `scripts/run_auditor.py` (default `"base"`, loads `config/<name>.yaml`).
3. **One shared Telegram chat/bot** — account name prefixed into every message; `notify()`'s own signature does not change, only call-site message text does.

---

## 1. Architecture summary

The target shape is **N independent OS processes, one per account**. Each process runs `scripts/run_shadow_loop.py --config <account>` (mirroring `run_auditor.py`'s existing `--config` flag), loads its own self-contained `config/<account>.yaml`, and connects to its own **separately-installed MT5 terminal** (a distinct `terminal_path` / data folder) via `mt5_session()`. This is a hard requirement of the `MetaTrader5` Python package: it holds exactly one IPC connection to one running terminal per OS process, so true concurrency requires process-level isolation, never multithreading within one process. All per-account mutable state (the 8 state files enumerated below, plus the SQLite journals) is namespaced under a per-account directory such as `data/db/<account>/…`, so two concurrently-running accounts never read or write the same file. The `autotrade_control.py` CLI and the `.bat` launchers gain an account dimension: `start`/`stop`/`emergency-stop` act on one named account, while `status` enumerates all configured accounts. The single Telegram bot/chat is shared; each account prefixes its name into the message text (`[AutoTrade:<account>] …`) so the operator can tell whose event it is.

---

## 2. Config schema

### Move to `config/<account>.yaml` (per-account)

`config/base.yaml` today (verified) holds these blocks, all of which are legitimately per-account tunables and should live in each account's own file: `global` (timeframe/timezone), `symbols` (canonical→broker map — a different account/broker may use different suffixes), `historical`, `shield`, `cfo`, `council`, `order`, `risk_voice`, `watchman`, `auditor` (`current_stage` + `promotion`/`demotion`/`borderline` sub-blocks), and `notifications` (`enabled`, `timeout_sec`).

The simplest path, honoring decision #2 (self-contained files, not base+override), is to make each `config/<account>.yaml` a **full copy** of the current `base.yaml` structure with per-account values. Keep `config/base.yaml` as the template / default account (see §4).

### Credentials — the one thing that must NOT go into `config/<account>.yaml`

MT5 credentials come from `.env` today via `load_mt5_credentials()` (`common/config.py:37`), reading `MT5_LOGIN`/`MT5_PASSWORD`/`MT5_SERVER`/`MT5_TERMINAL_PATH`. `config/` is git-tracked, so secrets cannot move into the YAML. The clean fit is a **per-account git-ignored env file** (e.g. `.env.<account>`), because `load_mt5_credentials(env_file=...)` *already accepts an `env_file` parameter* — no signature change needed. The account config (or the loader, from the account name) resolves which env file to load. `MT5_TERMINAL_PATH` per account is essential here: each account must point at its own installed terminal, otherwise two processes fight over one terminal.

### Stays genuinely global / shared

- **Telegram bot token + chat id** (`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`, loaded by `load_telegram_credentials()` at `common/config.py:165`) — decision #3: one shared chat for all accounts. Stays in the main `.env`.
- **News-calendar API keys** (Finnhub/FMP/EODHD/RapidAPI/AlphaVantage loaders, `common/config.py:57-162`) — these are provider account keys, not trading accounts; keep global in `.env` unless a future need arises.
- **`notifications.enabled` caveat:** `notify()` itself reads `load_yaml_config("base")` (`notify/telegram.py:55`) to check `notifications.enabled`. Because decision #3 freezes `notify()`'s contract, this toggle effectively remains read from `base.yaml` regardless of which account fired. That's acceptable (a global mute switch), but it means a *per-account* `notifications.enabled:false` will not be honored unless we also thread a config name into `notify()` — which decision #3 says we should not do. Documented as an accepted limitation; see §6.

---

## 3. File-by-file change list

Ground rule that makes this tractable: **every state-file module already accepts an optional path-override parameter** (`state_path` / `flag_path` / `pid_path` / `db_path` / `log_path`, all defaulting to a module-level `DEFAULT_*` constant). The cross-cutting change is *not* rewriting those modules — it is **resolving an account-derived path once at process startup and threading it through every call site**, plus adding the `--config`/`--account` plumbing.

### 3.1 `src/autotrade/common/config.py`
- `load_mt5_credentials(env_file=...)` already supports per-account env files — no signature change. Add a small helper to resolve an account name → its env file path and → its `config/<account>.yaml` (there's already `load_yaml_config(name)` at line 180). Consider a single `load_account_config(account)` returning both the dict and resolved credentials, so scripts don't re-implement the resolution.
- Add a helper to derive the **per-account data directory** (e.g. `REPO_ROOT / "data" / "db" / account`) so every state-path resolution below is consistent.

### 3.2 `src/autotrade/common/mt5_connection.py`
- **Docstring + comment update only** (lines 26-31 and 3-6). The module-level `_lock`/`_depth` reentrancy counter is *correct as-is* for the new model — it guards one connection within one process. Update the comment that says multi-account is "intentionally out of scope" to state the new reality: "one connection per process; N accounts = N processes, each with its own terminal_path." No functional change to `mt5_session()` — it already keys off the passed `creds` (including `creds.terminal_path`).

### 3.3 The 8 state-file modules — no code change required, only call-site path threading
Each already has an override param; the gap is that callers pass the `DEFAULT_*` constant. Namespace per account by passing an account-derived path:

| Module | Constant (verified) | Override param |
|---|---|---|
| `risk/circuit_breaker.py:28` | `DEFAULT_STATE_PATH` (`circuit_breaker_state.json`) | `state_path=` on `CircuitBreaker` |
| `watchman/position_metadata.py` | `DEFAULT_STATE_PATH` (`position_metadata.json`) | `state_path=` |
| `common/kill_switch_flag.py:18` | `DEFAULT_FLAG_PATH` (`kill_switch.flag`) | `flag_path=` |
| `common/stop_request_flag.py:21` | `DEFAULT_FLAG_PATH` (`stop_request.flag`) | `flag_path=` |
| `common/pid_file.py:23` | `DEFAULT_PID_PATH` (`shadow_loop.pid`) | `pid_path=` |
| `store/models.py:45-47` | `DEFAULT_DB_PATH` / `DEFAULT_PAPER_DB_PATH` / `DEFAULT_LIVE_DB_PATH` | `db_path=` on `get_engine()` |
| `orchestrator/shadow_loop.py:118` | `DEFAULT_BORDERLINE_LOG_PATH` (`borderline_log.jsonl`) | `borderline_log_path=` ctor arg |
| `notify/gate_state.py:24` | `DEFAULT_STATE_PATH` (`notify_gate_state.json`) | `state_path=` |

Note the DB path scheme must compose with the existing paper/live split (`store/models.py:45-47`): the per-account namespace becomes a directory level, so `data/db/<account>/trade_journal_paper.sqlite` etc. The `_ENGINE_CACHE` (keyed by resolved path, `store/models.py:144`) already handles multiple distinct DB files correctly.

### 3.4 `scripts/run_shadow_loop.py`
The most-touched file. Currently (verified) it hardcodes `load_yaml_config("base")` (line 180), `load_mt5_credentials()` (line 159), `DEFAULT_STATE_PATH` (line 205), `DEFAULT_POSITION_METADATA_PATH` (lines 304, 319), and `pid_file.read()/write()/remove()` with no path (lines 167, 254, 329).
- Add `--config`/`--account` flag (default `"base"`, matching `run_auditor.py`'s convention).
- Resolve credentials from the per-account env file; load `config/<account>.yaml`.
- Thread the account-derived paths into: `CircuitBreaker(state_path=…)`, `WatchmanLoop(state_path=…)`, `ShadowLoop(position_metadata_path=…, borderline_log_path=…)`, the journal `db_path` (already threaded via the adapter/loop — confirm it picks up the per-account DB dir), and **all three `pid_file` calls** (`read`, `write`, `remove`) so the double-launch guard (lines 167-178, 253-261) becomes **per-account**. This is the critical bit: without a per-account PID path, launching account B fails closed because account A "is already running."
- Prefix the account name into the `notify(...)` calls at lines 173, 260, 269 (start/already-running/started messages).

### 3.5 `scripts/autotrade_control.py`
Currently (verified) has zero account concept and uses default (single) PID/flag paths.
- Add `--account` to `start`, `stop`, `emergency-stop`, and (optionally) `status`.
- `do_start()` (line 43): pass `--config <account>` through to the `subprocess.Popen([... run_shadow_loop.py ...])` invocation (line 62), and check the **per-account** kill-switch flag (`kill_switch_flag.get_status(flag_path=…)`).
- `do_stop()` (line 73): per-account `pid_file.is_running(pid_path=…)` and `stop_request_flag.request(reason, flag_path=…)`.
- `do_emergency_stop()` (line 95): pass `--account`/`--config` through to the `kill_switch.py` subprocess (line 108).
- `do_status()` (line 112): **enumerate all configured accounts** (glob `config/*.yaml`, excluding `base` per the migration decision) and print RUNNING/NOT-running + kill-switch + stop-flag per account, each read from that account's namespaced paths. This is a behavior expansion, not just a param addition.

### 3.6 `scripts/kill_switch.py`
Currently (verified) uses default flag path and default credentials.
- Add `--account`/`--config`. Thread into `kill_switch_flag.activate/deactivate/get_status(flag_path=…)` (lines 113, 160, 177, 188) and `load_mt5_credentials(env_file=…)` (line 118) so it halts and closes positions on **that account's terminal**. `close_all_open_positions()` (line 48) operates on the connected terminal, so correct-terminal selection is purely a credentials/`terminal_path` matter — no change to the close logic itself.
- Prefix account name into its `notify(...)` calls (lines 114, 126, 148).

### 3.7 `scripts/run_auditor.py`
Already has `--config` (line 344) and `_resolve_db_path()` (line 88). Two gaps for per-account:
- `_MODE_DB_PATHS` (line 83) and the default DB paths (`DEFAULT_DB_PATH` etc., imported line 73) are the **global** `data/db/` paths, not per-account. `_resolve_db_path()` must resolve to the account's DB directory when a non-`base` config is used. The cleanest approach: derive the mode→path map from the account (via the same helper in §3.1) rather than the module-level constants.
- `DEFAULT_BORDERLINE_LOG_PATH` (imported line 70, used line 297) and `DEFAULT_NOTIFY_DAILY_STATE_PATH` (line 85) / `gate_state` default path likewise need per-account resolution.
- `_server_today()` (line 100) already uses `cfg["symbols"]` + `load_mt5_credentials()` — must load the account's credentials/env file so it opens the right terminal.
- Prefix account name into the `--notify` message text (lines 154, 180, 283).

### 3.8 `.bat` launchers (repo root)
`AutoTrade_Start.bat`, `AutoTrade_Stop.bat`, `AutoTrade_EmergencyStop.bat` (all verified) call `autotrade_control.py <verb>` with no argument. Two options:

| Option | Pros | Cons |
|---|---|---|
| **A. Per-account .bat files** (`AutoTrade_Start_<account>.bat`) | Double-clickable, matches current UX, no arg-passing in cmd | File proliferation as accounts grow |
| **B. One .bat taking an account arg** (prompt or `%1`) | Single set of files | Double-click UX needs a `choice`/prompt to pick account |

**Recommendation: Option A** — generate one small `.bat` trio per account. It preserves the existing "double-click a file" operator workflow (consistent with the codebase's simple file-based operational design) and each file is a one-liner passing `start --account <name>`. A tiny generator script (or manual copy) creates them per account.

### 3.9 Notify call sites — account-name prefixing (decision #3)
`notify()`'s signature is frozen; the account label must reach each call site's message-building code. Since it's one account per process, thread an `account_label` string into the relevant constructors/functions (alongside the paths already being threaded) and interpolate it into the `[AutoTrade]` prefix → `[AutoTrade:<account>]`. Every call site (all verified):

- `scripts/kill_switch.py` — lines 114, 126, 148 (label from `--account`).
- `scripts/run_shadow_loop.py` — lines 173, 260, 269 (label from `--config`).
- `scripts/run_auditor.py` — lines 154, 180, 283 (label from `--config`).
- `src/autotrade/watchman/loop.py:311` — Trade CLOSED (reconciliation path). Needs `account_label` on `WatchmanLoop`.
- `src/autotrade/orchestrator/shadow_loop.py` — lines 329/334/336 (graceful-stop variants) and line 563 (Trade OPENED). Needs `account_label` on `ShadowLoop`.
- `src/autotrade/execution/demo_adapter.py:241` — Trade CLOSED (abnormal-slippage close). Needs `account_label` on `ThrottledDemoAdapter`.
- `src/autotrade/store/journal.py:173` — anomaly notify inside `record_anomaly_event()`. This is a free function; add an optional `account_label` param (defaulting to `None` for backward compatibility) that callers (`circuit_breaker`, `connectivity_watchdog`) pass through.

---

## 4. Rollout / migration plan

Introduce per-account without breaking the currently-running single-account IC Markets demo:

1. **Phase 0 (no behavior change):** Update `mt5_connection.py`'s docstring/comment (§3.2) and add the account-resolution helpers in `common/config.py` (§3.1). Nothing calls them yet. Ship + verify green.
2. **Treat `base` as the implicit "default account" during transition.** The existing `config/base.yaml` and the existing flat `data/db/*` files *are* the default account. When `--config`/`--account` is omitted, everything resolves exactly as today (same paths, same `base.yaml`, same `.env`), so a plain `AutoTrade_Start.bat` keeps working unchanged. This is a natural extension of `run_auditor.py`'s existing `default="base"`.
3. **Add path resolution that is a no-op for `base`.** The account-derived path helper should return the *current* flat paths when the account is the default, and `data/db/<account>/…` only for named accounts. This means zero migration of existing on-disk state for the current account — no file moves, no data loss risk.
4. **Onboard the second account additively.** Create `config/<account2>.yaml` + `.env.<account2>` + its own installed MT5 terminal + its `.bat` trio. It writes under `data/db/<account2>/` from day one. The default account is untouched.
5. **Optional later cleanup:** if desired, migrate the default account from flat `data/db/*` to `data/db/base/*` as an explicit, one-time, opt-in step (stop the loop, move files, switch to `--account base`). Not required for the feature to work.

This "default account = today's setup" strategy means the risky cutover never has to happen: multi-account is purely additive.

---

## 5. Testing strategy

New coverage that does not exist today:

1. **State isolation between two accounts.** Given two account configs, assert that circuit-breaker state, position metadata, kill-switch flag, stop-request flag, PID file, journal DBs, borderline log, and notify-gate state all resolve to **distinct paths** and that writing account A's state does not appear when reading account B's. (Unit-level: feed two account names to the resolver; property is "no path collision".)
2. **Per-account PID / double-launch semantics.** The current double-launch guard (`run_shadow_loop.py:167-178`, `pid_file.write`'s exclusive-create at `pid_file.py:68`) must be re-scoped per account. Tests:
   - `start --account X` while X is already running → refused (existing behavior, now per-account PID path).
   - `start --account X` while **Y is running fine** → **succeeds** (the regression this feature must not reintroduce). This is the key new test — today a second launch fails closed regardless of account.
   - Stale per-account PID file self-heals (existing `pid_file` logic, now with an account path).
3. **Per-account kill switch / stop.** `kill_switch.py --account X --activate` sets only X's flag; account Y's `status` still shows not-halted. `autotrade_control.py stop --account X` writes only X's stop-request flag.
4. **`status` enumeration.** `autotrade_control.py status` with two configs present reports both accounts' running/kill-switch/stop state independently.
5. **Notify prefixing.** Assert each call site produces `[AutoTrade:<account>] …` (parametrize over the account label; mock `notify`). Cover at minimum: trade opened/closed (shadow_loop, watchman, demo_adapter), anomaly (journal), start/stop (run_shadow_loop), auditor daily/promotion/demotion.
6. **Auditor per-account DB resolution.** `run_auditor.py daily --config X --mode live` reads `data/db/X/trade_journal_live.sqlite`, not the global one.
7. **Backward-compat / default account.** With no `--account`/`--config`, every resolver returns exactly today's flat paths and `base.yaml` — a regression guard for the migration strategy in §4.

Existing tests already parametrize on injected paths (e.g. `test_pid_file.py`, `test_journal.py`, `test_kill_switch_script.py`), so most new tests extend established patterns rather than inventing harness machinery.

---

## 6. Open risks / questions

Things the three resolved decisions do **not** settle:

1. **One installed MT5 terminal per account is mandatory, and is an operational/setup burden, not just code.** The `MetaTrader5` package binds one process to one running terminal; genuine concurrency needs a separate terminal *installation* (distinct `terminal_path`/data folder) per account, each already logged into that account. The plan assumes the operator installs and configures these. Worth an explicit setup checklist doc. **Open:** confirm each account's exact `terminal_path` and that the broker permits multiple simultaneous terminal instances.
2. **Cross-account exposure awareness — intentionally isolated, or shared?** Shield (`shield/checkpoint.py`) enforces `max_positions_total`, `total_risk_ceiling_pct`, and `max_correlation` **within one process/account only**. If two accounts trade XAUUSD simultaneously, there is *no* cross-account correlation or aggregate-exposure check — each account is fully blind to the other. For separate broker accounts with separate capital this may be exactly right (isolated risk). But if the accounts share an economic reality (same symbol, correlated), the operator may be taking 2× the intended portfolio risk without any component knowing. **Decision needed:** is per-account isolation the deliberate model, or is a shared cross-account exposure ledger a future requirement? The current plan assumes **intentional isolation** (simplest, YAGNI) and flags this explicitly.
3. **Auditor promotion/demotion gates per-account *and* per-mode.** Today paper/live is a DB-file split and `live_ramp` vs `full` is `auditor.current_stage` in `base.yaml`. Per account, each has its own `current_stage` and its own paper/live DBs — so gates become per-account-and-per-mode. The plan handles the DB paths; **open question:** is the promotion/demotion *state* (`notify/gate_state.py`) and the `current_stage` marker meant to be tracked fully independently per account (assumed yes), and does the operator run the daily/promotion scheduled task once per account (yes — each needs its own `schtasks` entry)?
4. **`notifications.enabled` reads `base.yaml` regardless of account** (§2, `notify/telegram.py:55`). With `notify()`'s contract frozen (decision #3), a per-account mute is not possible without touching `notify()`. **Open:** accept a global mute only, or carve out a narrow exception to thread a config name into `notify()`? Plan currently accepts global-only.
5. **Secrets layout.** Plan proposes per-account git-ignored `.env.<account>` (reusing `load_mt5_credentials(env_file=...)`). **Confirm** this over any alternative (e.g. a single `.env` with `MT5_LOGIN__<account>`-style namespaced keys), and confirm `.gitignore` covers the chosen pattern so credentials never get committed.
6. **News-provider concurrency.** `MQL5CalendarProvider` reads MT5's calendar export from each terminal's `commondata_path` (`run_shadow_loop.py:119`). With N terminals, each account resolves its own `commondata_path` — fine — but the `NewsCalendarExporter.mq5` Service must be started **in each terminal**. Operational note, not a code change; worth adding to the setup checklist.

---

**Files that would change (all absolute):**
- `D:\AutoTrade\src\autotrade\common\config.py`
- `D:\AutoTrade\src\autotrade\common\mt5_connection.py` (docstring/comment only)
- `D:\AutoTrade\scripts\run_shadow_loop.py`
- `D:\AutoTrade\scripts\autotrade_control.py`
- `D:\AutoTrade\scripts\kill_switch.py`
- `D:\AutoTrade\scripts\run_auditor.py`
- `D:\AutoTrade\src\autotrade\store\journal.py` (add `account_label` to `record_anomaly_event`)
- `D:\AutoTrade\src\autotrade\watchman\loop.py`, `D:\AutoTrade\src\autotrade\orchestrator\shadow_loop.py`, `D:\AutoTrade\src\autotrade\execution\demo_adapter.py` (add `account_label`, notify prefixing)
- `D:\AutoTrade\AutoTrade_Start.bat`, `D:\AutoTrade\AutoTrade_Stop.bat`, `D:\AutoTrade\AutoTrade_EmergencyStop.bat` (→ per-account trio)
- New: `D:\AutoTrade\config\<account>.yaml` + git-ignored `.env.<account>` per account.

**Files needing no code change (override params already exist), only per-account paths threaded in by callers:** `risk\circuit_breaker.py`, `watchman\position_metadata.py`, `common\kill_switch_flag.py`, `common\stop_request_flag.py`, `common\pid_file.py`, `store\models.py`, `notify\gate_state.py`.
