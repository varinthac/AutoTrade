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

from dataclasses import dataclass
from datetime import datetime, timedelta

from autotrade.common.clock import Clock


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
    """

    def __init__(
        self,
        daily_loss_limit_pct: float,
        max_consecutive_losses: int,
        max_drawdown_halt_pct: float,
        live_downgrade_pct: float = 15.0,
        consecutive_loss_halt_hours: float = 24.0,
    ) -> None:
        self._daily_loss_limit_pct = daily_loss_limit_pct
        self._max_consecutive_losses = max_consecutive_losses
        self._max_drawdown_halt_pct = max_drawdown_halt_pct
        self._live_downgrade_pct = live_downgrade_pct
        self._consecutive_loss_halt_hours = consecutive_loss_halt_hours

        self._current_server_day = None
        self._realized_pnl_today = 0.0
        self._consecutive_losses = 0
        self._consecutive_loss_halt_until: datetime | None = None
        self._drawdown_halted = False

        self._latest_equity: float | None = None
        self._latest_peak_equity: float | None = None
        self._latest_live_start_equity: float | None = None
        self._latest_floating_pnl = 0.0

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
                self._drawdown_halted = True

    def clear_drawdown_halt(self) -> None:
        """Manually clear the drawdown halt. Must only ever be called from an
        explicit human action (e.g. a restart script) -- never from timed or
        automatic logic. Appendix A §3.3: "ต้อง manual restart เท่านั้น"."""
        self._drawdown_halted = False

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
