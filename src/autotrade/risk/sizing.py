"""Position sizing — The CFO, per trading_system_summary_v2.md Appendix A
§3.1 "Position Sizing" and §3.2 "Volatility Adjustment".

Pure functions only: no I/O, no MT5, no Clock dependency. Broker-specific
values (point/tick value, volume min/max/step) come from
`common/symbol_spec.SymbolSpec` — the caller resolves that and passes plain
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
    min_lot_risk_cap_pct: float | None = None,
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

    **`min_lot_risk_cap_pct` -- dated, deliberate deviation from Appendix A
    §3.1 (added 2026-07-22).** `None` (the default) preserves the
    spec-exact behavior above with zero change. When set, and the
    risk-based lot rounds below `volume_min`, this checks whether trading
    `volume_min` anyway would still risk no more than `min_lot_risk_cap_pct`
    percent of `equity` (using FULL `equity`, independent of the ATR-based
    halving above -- the cap bounds the broker-minimum trade's own risk,
    not the halved risk budget that produced the sub-minimum lot in the
    first place): if so, this returns `min(volume_min, volume_max)` instead
    of `None`. Otherwise it still returns `None`, same as today. This
    exists because on a small account relative to XAUUSD's stop distances,
    the spec-exact behavior discards a large fraction of signals that pass
    every other gate; Stage 1 measurement (`experiments/experiments_log.md`,
    the 2026-07-22 NOTE "small-account sizing REFRESH + min-lot-fallback
    measurement") found the rescued trades carry a real, positive edge
    (PF 1.60 at cap=1.5 in isolation), not dead weight. `config/base.yaml`'s
    `cfo.min_lot_risk_cap_pct: 1.5` is the user's Stage 2 decision to adopt
    this live.
    """
    if equity <= 0:
        raise ValueError(f"equity must be positive, got {equity}")
    if risk_per_trade_pct <= 0:
        raise ValueError(f"risk_per_trade_pct must be positive, got {risk_per_trade_pct}")
    if point_value <= 0:
        raise ValueError(f"point_value must be positive, got {point_value}")
    if min_lot_risk_cap_pct is not None and min_lot_risk_cap_pct <= 0:
        raise ValueError(f"min_lot_risk_cap_pct must be positive, got {min_lot_risk_cap_pct}")

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
        if min_lot_risk_cap_pct is not None:
            min_lot_risk = stop_distance * point_value * volume_min
            if min_lot_risk <= (min_lot_risk_cap_pct / 100) * equity:
                return min(volume_min, volume_max)
        return None
    return lot
