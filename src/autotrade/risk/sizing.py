"""Position sizing — The CFO, per trading_system_summary_v2.md Appendix A
§3.1 "Position Sizing" and §3.2 "Volatility Adjustment".

Pure functions only: no I/O, no MT5, no Clock dependency. Broker-specific
values (point/tick value, volume min/max/step) come from
`common/symbols.SymbolSpec` — the caller resolves that and passes plain
floats in here, so this module stays decoupled from `common/` and `features/`
per spec.md §2.3's dependency-direction invariant.

Units:
- `risk_per_trade_pct` is a percentage (e.g. `0.5` means 0.5%), matching
  `config/base.yaml`'s `cfo.risk_per_trade_pct`.
- `point_value` is the monetary value of a one-price-unit move for a single
  1.0 lot (i.e. `tick_value / tick_size` from `SymbolSpec`, not `tick_value`
  alone -- most brokers set `tick_size == point`, but this keeps the formula
  correct even when they don't).
"""
from __future__ import annotations

from decimal import ROUND_DOWN, Decimal


def _round_down_to_step(value: float, step: float) -> float:
    """Round `value` down to the nearest multiple of `step`, using Decimal to
    avoid binary-float rounding artifacts (e.g. 0.07000000000000001)."""
    if step <= 0:
        raise ValueError(f"volume_step must be positive, got {step}")
    d_value = Decimal(str(value))
    d_step = Decimal(str(step))
    whole_steps = (d_value / d_step).to_integral_value(rounding=ROUND_DOWN)
    return float(whole_steps * d_step)


def compute_lot_size(
    equity: float,
    risk_per_trade_pct: float,
    entry: float,
    stop_loss: float,
    point_value: float,
    volume_min: float,
    volume_max: float,
    volume_step: float,
    current_atr: float | None = None,
    avg_atr_20d: float | None = None,
    volatility_multiplier_threshold: float = 1.5,
) -> float | None:
    """Compute a broker-valid lot size for a single trade.

    Formula (Appendix A §3.1):
        risk_amount   = equity * risk_per_trade
        stop_distance = |entry - stop_loss|
        lot_size      = risk_amount / (stop_distance * point_value)

    Volatility adjustment (Appendix A §3.2): if `current_atr` exceeds
    `volatility_multiplier_threshold * avg_atr_20d`, `risk_per_trade` is
    halved before computing `risk_amount`. This is *in addition to* the
    natural lot-size reduction already caused by a wider stop_distance in
    high-volatility regimes -- both effects apply; this is intentional
    double dampening per the spec, not a bug to "fix" into one adjustment.

    The result is always rounded DOWN to `volume_step`, never up. If the
    rounded lot is below `volume_min`, this returns `None` -- meaning
    "do not trade" -- rather than silently trading the minimum lot, per
    Appendix A §3.1 ("อย่าฝืนเสี่ยงเกินแผน" / don't force a trade that risks
    more than planned). The result is capped at `volume_max`.
    """
    if equity <= 0:
        raise ValueError(f"equity must be positive, got {equity}")
    if risk_per_trade_pct <= 0:
        raise ValueError(f"risk_per_trade_pct must be positive, got {risk_per_trade_pct}")
    if point_value <= 0:
        raise ValueError(f"point_value must be positive, got {point_value}")

    stop_distance = abs(entry - stop_loss)
    if stop_distance <= 0:
        raise ValueError("stop_distance must be positive (entry and stop_loss cannot be equal)")

    risk_per_trade = risk_per_trade_pct / 100
    if (
        current_atr is not None
        and avg_atr_20d is not None
        and avg_atr_20d > 0
        and current_atr > volatility_multiplier_threshold * avg_atr_20d
    ):
        risk_per_trade /= 2

    risk_amount = equity * risk_per_trade
    raw_lot = risk_amount / (stop_distance * point_value)

    lot = _round_down_to_step(raw_lot, volume_step)
    lot = min(lot, volume_max)

    if lot < volume_min:
        return None
    return lot
