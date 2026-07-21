"""Account-level circuit breakers — The CFO, per
trading_system_summary_v2.md Appendix A §3.3 "Circuit Breakers".

All "now"/"day" comparisons go through an injected `Clock` (spec.md §2.3 --
no direct OS clock reads in decision code). Per Appendix A §0 ("เวลาและ 'วัน'
= MT5 server time ทั้งระบบ"), this component expects to be fed **MT5 server
time** -- both the `closed_at`/`as_of` timestamps passed into the `record_*`
methods and the `clock` passed into `check()` must all come from the same
server-time source (e.g. `common/mt5_time.server_now()` in live/sandbox, a
`SimulatedClock` fed server-time bars in backtest). "Day" is `datetime.date()`
of that server-time value -- mixing UTC/local time in here would silently
break the daily-loss reset boundary.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from autotrade.common.clock import Clock
from autotrade.common.config import REPO_ROOT
from autotrade.store import journal

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = REPO_ROOT / "data" / "db" / "circuit_breaker_state.json"

HEAVY_CB_MARKER = "drawdown halt"
"""Substring written at the start of `record_equity`'s `AnomalyEventRecord.
details` when the heaviest ("ระดับหนัก") circuit-breaker tier -- the
drawdown halt -- fires. `AnomalyEventRecord.event_type` is generically
`"circuit_breaker_trigger"` for every tier (daily-loss/consecutive-loss/
drawdown/downgrade), so this free-text marker is the only way a reader
(Appendix A §5.2's Live ramp -> Full size gate, `scripts/run_auditor.py`)
can tell which tier actually fired. Exported as a constant specifically so
that CLI/consumer and this module can't silently drift apart -- if this
message ever changes, update it here and every importer picks it up."""


@dataclass(frozen=True)
class CircuitBreakerState:
    daily_loss_halted: bool
    daily_loss_reason: str | None
    consecutive_loss_halted: bool
    consecutive_loss_reason: str | None
    drawdown_halted: bool
    drawdown_reason: str | None
    should_downgrade_to_paper: bool
    downgrade_reason: str | None

    @property
    def blocks_new_entries(self) -> bool:
        """True if any gate currently blocks opening new trades. Existing
        positions are unaffected -- per Appendix A §3.3 they stay with the
        Watchman."""
        return self.daily_loss_halted or self.consecutive_loss_halted or self.drawdown_halted


class CircuitBreaker:
    """Fed trade-close events and periodic equity snapshots; call `check()`
    to find out which gates (if any) are currently tripped.

    Gates, lightest to heaviest (Appendix A §3.3):
      1. Daily loss limit -- blocks new entries until the next server day.
      2. 3 consecutive losses -- blocks new entries for 24 hours.
      3. Drawdown from equity peak -- halts entirely, one-way, requires a
         manual call to `clear_drawdown_halt()` (never auto-clears).
      4. Equity far below the live-start baseline -- signals
         `should_downgrade_to_paper`; switching adapters is the
         orchestrator's job, this module only exposes the signal.

    If `state_path` is given, state is persisted to that JSON file (same
    plain-file-I/O pattern as `common/kill_switch_flag.py`) and reloaded on
    construction -- critically so the drawdown halt (gate 3) survives a
    process restart rather than silently clearing, which would defeat its
    whole "requires manual restart" point.
    """

    def __init__(
        self,
        daily_loss_limit_pct: float,
        max_consecutive_losses: int,
        max_drawdown_halt_pct: float,
        live_downgrade_pct: float = 15.0,
        consecutive_loss_halt_hours: float = 24.0,
        state_path: Path | None = None,
        journal_db_path: Path | None = None,
    ) -> None:
        self._daily_loss_limit_pct = daily_loss_limit_pct
        self._max_consecutive_losses = max_consecutive_losses
        self._max_drawdown_halt_pct = max_drawdown_halt_pct
        self._live_downgrade_pct = live_downgrade_pct
        self._consecutive_loss_halt_hours = consecutive_loss_halt_hours
        self._state_path = state_path
        self._journal_db_path = journal_db_path

        self._current_server_day = None
        self._realized_pnl_today = 0.0
        self._consecutive_losses = 0
        self._consecutive_loss_halt_until: datetime | None = None
        self._drawdown_halted = False

        self._latest_equity: float | None = None
        self._latest_peak_equity: float | None = None
        self._latest_live_start_equity: float | None = None
        self._latest_floating_pnl = 0.0

        # Edge-trigger tracking for anomaly-event recording only (see
        # `check()`'s "gate-trip anomaly events" note) -- NOT persisted
        # across restarts, unlike `_drawdown_halted` itself, so a restart
        # during an already-active daily-loss/consecutive-loss/downgrade
        # condition can re-fire one anomaly event on the first `check()`
        # call afterwards. Acceptable for a daily-report trigger COUNT
        # (rare, restart-adjacent double-count) rather than worth the extra
        # persisted-state machinery `_drawdown_halted` has for its
        # genuinely one-way, manual-clear-only latch.
        self._daily_loss_reported = False
        self._consecutive_loss_reported = False
        self._downgrade_reported = False

        self._load_state()

    def _load_state(self) -> None:
        """Restore persisted state (if `state_path` is set and the file
        exists) -- critically, `_drawdown_halted`, whose whole point is that
        a crash/restart must NOT be how it gets cleared (Appendix A §3.3:
        "ต้อง manual restart เท่านั้น").

        A present-but-corrupt/unreadable state file fails CLOSED: rather
        than silently keeping the constructor default of `_drawdown_halted =
        False` (which would un-halt an active drawdown halt -- exactly the
        scenario this persistence exists to prevent), it is treated as
        `_drawdown_halted = True` and logged loudly, same fail-safe
        philosophy as `common/kill_switch_flag.get_status()`. The other
        fields aren't safety-critical one-way latches, so they simply reset
        to their defaults."""
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error(
                "circuit breaker state file %s is corrupt/unreadable (%s); "
                "conservatively treating drawdown halt as ACTIVE pending manual "
                "investigation, per Appendix A §3.3's fail-closed philosophy",
                self._state_path,
                exc,
            )
            self._drawdown_halted = True
            return

        self._drawdown_halted = payload.get("drawdown_halted", False)
        self._latest_peak_equity = payload.get("latest_peak_equity")
        self._latest_live_start_equity = payload.get("latest_live_start_equity")
        day = payload.get("current_server_day")
        self._current_server_day = date.fromisoformat(day) if day else None
        self._realized_pnl_today = payload.get("realized_pnl_today", 0.0)
        self._consecutive_losses = payload.get("consecutive_losses", 0)
        halt_until = payload.get("consecutive_loss_halt_until")
        self._consecutive_loss_halt_until = (
            datetime.fromisoformat(halt_until) if halt_until else None
        )

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        payload = {
            "drawdown_halted": self._drawdown_halted,
            "latest_peak_equity": self._latest_peak_equity,
            "latest_live_start_equity": self._latest_live_start_equity,
            "current_server_day": (
                self._current_server_day.isoformat() if self._current_server_day else None
            ),
            "realized_pnl_today": self._realized_pnl_today,
            "consecutive_losses": self._consecutive_losses,
            "consecutive_loss_halt_until": (
                self._consecutive_loss_halt_until.isoformat()
                if self._consecutive_loss_halt_until
                else None
            ),
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def record_trade_close(self, pnl: float, closed_at: datetime) -> None:
        """Feed a closed trade's realized P&L. `closed_at` must be server
        time (see module docstring)."""
        day = closed_at.date()
        if self._current_server_day != day:
            self._current_server_day = day
            self._realized_pnl_today = 0.0
        self._realized_pnl_today += pnl

        if pnl < 0:
            self._consecutive_losses += 1
            if self._consecutive_losses >= self._max_consecutive_losses:
                self._consecutive_loss_halt_until = closed_at + timedelta(
                    hours=self._consecutive_loss_halt_hours
                )
        else:
            self._consecutive_losses = 0

        self._save_state()

    def record_equity(
        self,
        equity: float,
        peak_equity: float,
        live_start_equity: float,
        as_of: datetime,
        floating_pnl: float = 0.0,
    ) -> None:
        """Feed a periodic equity snapshot. `as_of` must be server time (see
        module docstring). `floating_pnl` is the unrealized P&L of currently
        open positions, used for the daily-loss check."""
        self._latest_equity = equity
        self._latest_peak_equity = peak_equity
        self._latest_live_start_equity = live_start_equity
        self._latest_floating_pnl = floating_pnl

        if peak_equity > 0:
            drawdown_pct = (peak_equity - equity) / peak_equity * 100
            if drawdown_pct >= self._max_drawdown_halt_pct:
                if not self._drawdown_halted:
                    journal.record_anomaly_event(
                        timestamp=as_of, event_type="circuit_breaker_trigger",
                        details=(
                            f"{HEAVY_CB_MARKER}: equity drawdown from peak {drawdown_pct:.2f}% >= "
                            f"{self._max_drawdown_halt_pct}%; halted, requires manual restart"
                        ),
                        db_path=self._journal_db_path,
                    )
                self._drawdown_halted = True

        self._save_state()

    def clear_drawdown_halt(self) -> None:
        """Manually clear the drawdown halt. Must only ever be called from an
        explicit human action (e.g. a restart script) -- never from timed or
        automatic logic. Appendix A §3.3: "ต้อง manual restart เท่านั้น"."""
        self._drawdown_halted = False
        self._save_state()

    def _record_gate_edge(
        self, now: datetime, tripped: bool, already_reported_attr: str, event_type: str, reason: str | None,
    ) -> None:
        """Fires one `record_anomaly_event` the moment a gate transitions
        from not-tripped to tripped, and re-arms once it clears -- same
        "log once per event, not once per polling cycle" pattern
        `watchman/connectivity_watchdog.py`'s `_alerted_since_last_good`
        already uses, applied here to `check()`'s per-call-recomputed daily-
        loss/consecutive-loss/downgrade gates."""
        already_reported = getattr(self, already_reported_attr)
        if tripped and not already_reported:
            journal.record_anomaly_event(
                timestamp=now, event_type="circuit_breaker_trigger",
                details=f"{event_type}: {reason}", db_path=self._journal_db_path,
            )
            setattr(self, already_reported_attr, True)
        elif not tripped and already_reported:
            setattr(self, already_reported_attr, False)

    def check(self, clock: Clock) -> CircuitBreakerState:
        now = clock.now()

        daily_loss_halted = False
        daily_loss_reason = None
        if self._latest_equity is not None and self._latest_equity > 0:
            realized_today = (
                self._realized_pnl_today if self._current_server_day == now.date() else 0.0
            )
            today_pnl = realized_today + self._latest_floating_pnl
            if today_pnl < 0:
                loss_pct = -today_pnl / self._latest_equity * 100
                if loss_pct >= self._daily_loss_limit_pct:
                    daily_loss_halted = True
                    daily_loss_reason = (
                        f"today's loss {loss_pct:.2f}% of equity >= limit "
                        f"{self._daily_loss_limit_pct}% (realized {realized_today:.2f} "
                        f"+ floating {self._latest_floating_pnl:.2f}); blocked until next server day"
                    )

        consecutive_loss_halted = False
        consecutive_loss_reason = None
        if self._consecutive_loss_halt_until is not None and now < self._consecutive_loss_halt_until:
            consecutive_loss_halted = True
            consecutive_loss_reason = (
                f"{self._consecutive_losses} consecutive losses; "
                f"halted until {self._consecutive_loss_halt_until.isoformat()}"
            )

        drawdown_reason = None
        if self._drawdown_halted:
            drawdown_reason = (
                f"equity drawdown from peak >= {self._max_drawdown_halt_pct}%; "
                "halted, requires manual restart"
            )

        should_downgrade = False
        downgrade_reason = None
        if (
            self._latest_live_start_equity is not None
            and self._latest_live_start_equity > 0
            and self._latest_equity is not None
        ):
            drop_pct = (
                (self._latest_live_start_equity - self._latest_equity)
                / self._latest_live_start_equity
                * 100
            )
            if drop_pct >= self._live_downgrade_pct:
                should_downgrade = True
                downgrade_reason = (
                    f"equity {drop_pct:.2f}% below live-start baseline >= "
                    f"{self._live_downgrade_pct}%; downgrade to paper trading"
                )

        self._record_gate_edge(now, daily_loss_halted, "_daily_loss_reported", "daily_loss_halt", daily_loss_reason)
        self._record_gate_edge(
            now, consecutive_loss_halted, "_consecutive_loss_reported",
            "consecutive_loss_halt", consecutive_loss_reason,
        )
        self._record_gate_edge(now, should_downgrade, "_downgrade_reported", "downgrade_to_paper", downgrade_reason)

        return CircuitBreakerState(
            daily_loss_halted=daily_loss_halted,
            daily_loss_reason=daily_loss_reason,
            consecutive_loss_halted=consecutive_loss_halted,
            consecutive_loss_reason=consecutive_loss_reason,
            drawdown_halted=self._drawdown_halted,
            drawdown_reason=drawdown_reason,
            should_downgrade_to_paper=should_downgrade,
            downgrade_reason=downgrade_reason,
        )
