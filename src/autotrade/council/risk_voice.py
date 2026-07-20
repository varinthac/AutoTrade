"""Risk Voice -- Council's own veto gate, per trading_system_summary_v2.md
Appendix A §1.5. Six independent conditions; ANY one triggering vetoes the
trade outright (it is a gate, not a scored voice like Bull/Bear -- Appendix
A §1: "Risk-Manager persona is a hard veto/gate, not a third directional
voter", spec.md §3.3):

    1. Spread too wide: current spread > `max_spread_multiple` x the 20-day
       average spread, OR (XAUUSD only) > `max_spread_points_xauusd` points
       -- either condition alone vetoes.
    2. High-impact news for a currency involved in the trade, within
       `-news_blackout_before_min` to `+news_blackout_after_min` minutes of
       the event. See `council/news_calendar.py` for the fetch-failure
       fail-safe (`None` -> treated as "there IS news" -> veto) -- **this
       currently means every trade is vetoed**, see that module's docstring.
       A symbol missing from `_SYMBOL_CURRENCIES` (below) gets the same
       fail-safe veto rather than silently skipping this condition.
    3. The order's stop-loss > `max_stop_atr_multiple` x ATR(14). This
       DUPLICATES the clamp `council/order_construction.py`'s
       `build_order_plan` already enforces (`stop_distance` capped at
       `sl_max_atr * atr`) -- Risk Voice re-checks it independently as
       defense-in-depth, reading `order_plan.stop_distance` and the ATR
       value fresh rather than trusting the clamp already handled it
       (Appendix A §1.4's note explicitly calls this ceiling out as "the
       Risk Voice's own ceiling, duplicated in Council's own construction
       clamp").
    4. Outside the allowed trading session.
    5. Friday after `friday_close_hour` server time -- no new positions
       before the weekend close.
    6. ATR(14) > `max_atr_panic_multiple` x its 20-day average (abnormal/
       panic market conditions).

Pure function aside from the injected `NewsCalendarProvider` and `Clock`
(spec.md §2.3's dependency-direction/no-direct-clock-reads invariants) --
`avg_spread_points_20d` / `avg_atr_20d` are parameters the caller computes
and passes in, same "don't do your own rolling-window I/O" convention as
`risk/sizing.py`'s volatility check.

**Re-check at order-send time (Appendix A §1.5's explicit requirement):**
`check_risk_voice` is cheap enough to call twice per trade attempt -- once
when Council first evaluates the signal, and again immediately before
`execution/`'s `place_order()` is called. If the second call vetoes when
the first one didn't, the caller must cancel the trade and log
`stale_signal` rather than place the order -- see `orchestrator/shadow_loop.py`
for the concrete two-call wiring.

**What the re-check actually covers, honestly:** in `orchestrator/shadow_loop.py`'s
current wiring, both calls recompute spread/ATR/stop-distance from the SAME
already-closed bar/history, and the two calls happen close enough in wall-clock
time that session/Friday-close almost never differ either -- so conditions 1
(spread), 3 (stop-distance), 4 (session) and 5 (Friday-close) are structurally
near-identical between the two calls in this pipeline, NOT independently
re-verified against fresh market state. The re-check's genuine, currently-real
value is condition 2 (news): `news_provider` IS re-queried fresh on each call
and can flip pass -> veto between the two. A session/Friday-close boundary
(condition 4/5) can also flip if enough real wall-clock time elapses between
the two calls, but that's rare. Do not assume this re-check catches, e.g.,
spread widening between signal-time and send-time -- it currently doesn't, in
this pipeline.

**Session-hour choice (server time) and its DST caveat:** the London + New
York session overlap, in UTC, is roughly 12:00-16:00 (13:00-17:00 during UK
daylight saving). Most MT5 brokers run their server clock on a fixed
EET-like offset (commonly UTC+2 in northern-hemisphere winter, UTC+3 in
summer) that does not track UK/US daylight-saving transitions in lockstep
with UTC or with each other -- so the true server-time overlap window
drifts by roughly an hour, twice a year, and is broker-specific.
`session_start_hour=14`, `session_end_hour=18` (server time, in
`RiskVoiceConfig`'s defaults below) is a reasonable year-round placeholder
centered on the overlap, not a precise boundary -- Appendix A §1.5 itself
tags this `[adjustable]` and says "expand later" (อนุญาตเฉพาะ London + New
York overlap ก่อน แล้วขยายทีหลัง). This needs periodic manual
re-verification against the actual broker's current server-time offset (or
a real session calendar, later) -- a known, documented limitation, not a
bug.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from autotrade.common.clock import Clock
from autotrade.council.news_calendar import NewsCalendarProvider
from autotrade.council.order_construction import OrderPlan

# News-relevant currencies per canonical symbol (Appendix A §1.5's news
# condition needs to know which currencies to query for each trade). Unlike
# `shield/correlation.py`'s correlation table, an unlisted symbol is NOT
# treated as "nothing to check" -- `check_risk_voice` fail-safe-vetoes any
# symbol missing from this table (same fail-safe posture as a calendar fetch
# failure), rather than silently skipping the news condition for it. Add new
# symbols here as they're onboarded to `config/base.yaml`'s `symbols:` block
# to get real news-currency coverage instead of the fail-safe veto.
_SYMBOL_CURRENCIES: dict[str, tuple[str, ...]] = {
    "XAUUSD": ("USD",),
    "EURUSD": ("EUR", "USD"),
    "GBPUSD": ("GBP", "USD"),
    "USDJPY": ("USD", "JPY"),
}


def get_symbol_currencies(symbol: str) -> tuple[str, ...]:
    """News-relevant currencies for `symbol`. An unlisted symbol returns an
    empty tuple rather than raising -- `check_risk_voice` treats an empty
    tuple as fail-safe-veto territory (see its news-condition handling), not
    as "nothing to check". Add new symbols to `_SYMBOL_CURRENCIES` as
    they're onboarded."""
    return _SYMBOL_CURRENCIES.get(symbol, ())


@dataclass(frozen=True)
class RiskVoiceConfig:
    """All 6 conditions' thresholds, per `config/base.yaml`'s `risk_voice:`
    block (trading_system_summary_v2.md Appendix A §1.5 / §6). Every value
    here is `[adjustable]` per the spec."""

    max_spread_multiple: float = 1.5
    max_spread_points_xauusd: float = 35.0
    news_blackout_before_min: float = 45.0
    news_blackout_after_min: float = 30.0
    max_stop_atr_multiple: float = 2.5
    session_start_hour: int = 14
    session_end_hour: int = 18
    friday_close_hour: int = 20
    max_atr_panic_multiple: float = 3.0


@dataclass(frozen=True)
class RiskVoiceDecision:
    """Mirrors `shield.checkpoint.ShieldDecision`'s shape: one
    blocked+reason pair per condition, plus a `.vetoed` summary and a
    `.reasons` list for logging."""

    spread_blocked: bool
    spread_reason: str | None
    news_blocked: bool
    news_reason: str | None
    stop_distance_blocked: bool
    stop_distance_reason: str | None
    session_blocked: bool
    session_reason: str | None
    friday_close_blocked: bool
    friday_close_reason: str | None
    atr_panic_blocked: bool
    atr_panic_reason: str | None

    @property
    def vetoed(self) -> bool:
        """True if any of the 6 conditions vetoes this trade."""
        return (
            self.spread_blocked
            or self.news_blocked
            or self.stop_distance_blocked
            or self.session_blocked
            or self.friday_close_blocked
            or self.atr_panic_blocked
        )

    @property
    def reasons(self) -> list[str]:
        """Every triggered condition's reason, for logging (empty if not
        `vetoed`)."""
        return [
            reason
            for reason in (
                self.spread_reason, self.news_reason, self.stop_distance_reason,
                self.session_reason, self.friday_close_reason, self.atr_panic_reason,
            )
            if reason is not None
        ]


def check_risk_voice(
    symbol: str,
    order_plan: OrderPlan,
    current_spread_points: float,
    avg_spread_points_20d: float,
    current_atr: float,
    avg_atr_20d: float,
    news_provider: NewsCalendarProvider,
    clock: Clock,
    config: RiskVoiceConfig,
) -> RiskVoiceDecision:
    """Evaluate all 6 veto conditions (Appendix A §1.5) against the current
    market/order state. See module docstring for the re-check-twice design
    and the session-hour/DST caveat."""
    now = clock.now()

    # 1. Spread -- either sub-condition alone vetoes.
    spread_multiple_breach = (
        avg_spread_points_20d > 0
        and current_spread_points > config.max_spread_multiple * avg_spread_points_20d
    )
    spread_xauusd_breach = (
        symbol == "XAUUSD" and current_spread_points > config.max_spread_points_xauusd
    )
    spread_blocked = spread_multiple_breach or spread_xauusd_breach
    spread_reason = None
    if spread_blocked:
        parts = []
        if spread_multiple_breach:
            parts.append(
                f"spread {current_spread_points:.1f} points > {config.max_spread_multiple}x "
                f"20-day average ({avg_spread_points_20d:.1f})"
            )
        if spread_xauusd_breach:
            parts.append(
                f"spread {current_spread_points:.1f} points > XAUUSD ceiling "
                f"{config.max_spread_points_xauusd}"
            )
        spread_reason = "; ".join(parts)

    # 2. News -- window is centered on "now": an event counts if "now" falls
    # within [event_time - before_min, event_time + after_min], equivalently
    # event_time falls within [now - after_min, now + before_min].
    news_blocked = False
    news_reason = None
    symbol_currencies = get_symbol_currencies(symbol)
    if not symbol_currencies:
        news_blocked = True
        news_reason = (
            f"symbol {symbol!r} has no configured news-currency mapping in "
            "risk_voice._SYMBOL_CURRENCIES -- failing safe (veto) rather than "
            "skipping the news check entirely"
        )
    else:
        window_start = now - timedelta(minutes=config.news_blackout_after_min)
        window_end = now + timedelta(minutes=config.news_blackout_before_min)
        for currency in symbol_currencies:
            events = news_provider.get_high_impact_events(currency, window_start, window_end)
            if events is None:
                news_blocked = True
                news_reason = (
                    f"economic calendar unavailable for {currency} -- fail-safe veto "
                    "(Appendix A §1.5: a fetch failure is treated as 'there IS news')"
                )
                break
            if events:
                news_blocked = True
                news_reason = (
                    f"{len(events)} high-impact {currency} news event(s) within "
                    f"-{config.news_blackout_before_min:.0f}/+{config.news_blackout_after_min:.0f} "
                    "min of now"
                )
                break

    # 3. Stop-loss > max_stop_atr_multiple x ATR -- defense-in-depth
    # re-check of order_construction.build_order_plan's own clamp.
    max_stop_distance = config.max_stop_atr_multiple * current_atr
    stop_distance_blocked = order_plan.stop_distance > max_stop_distance
    stop_distance_reason = (
        f"stop distance {order_plan.stop_distance:.5f} > {config.max_stop_atr_multiple}x "
        f"ATR ({max_stop_distance:.5f})"
        if stop_distance_blocked else None
    )

    # 4. Session -- London+NY overlap placeholder, server time (see module
    # docstring for the DST caveat). Half-open [start, end).
    session_blocked = not (config.session_start_hour <= now.hour < config.session_end_hour)
    session_reason = (
        f"server hour {now.hour} outside allowed session "
        f"[{config.session_start_hour}, {config.session_end_hour})"
        if session_blocked else None
    )

    # 5. Friday close -- weekday(): Monday=0 ... Friday=4.
    friday_close_blocked = now.weekday() == 4 and now.hour >= config.friday_close_hour
    friday_close_reason = (
        f"Friday, server hour {now.hour} >= {config.friday_close_hour}:00 weekend-close cutoff"
        if friday_close_blocked else None
    )

    # 6. ATR panic.
    atr_panic_blocked = avg_atr_20d > 0 and current_atr > config.max_atr_panic_multiple * avg_atr_20d
    atr_panic_reason = (
        f"ATR(14) {current_atr:.5f} > {config.max_atr_panic_multiple}x 20-day average "
        f"({avg_atr_20d:.5f})"
        if atr_panic_blocked else None
    )

    return RiskVoiceDecision(
        spread_blocked=spread_blocked, spread_reason=spread_reason,
        news_blocked=news_blocked, news_reason=news_reason,
        stop_distance_blocked=stop_distance_blocked, stop_distance_reason=stop_distance_reason,
        session_blocked=session_blocked, session_reason=session_reason,
        friday_close_blocked=friday_close_blocked, friday_close_reason=friday_close_reason,
        atr_panic_blocked=atr_panic_blocked, atr_panic_reason=atr_panic_reason,
    )
