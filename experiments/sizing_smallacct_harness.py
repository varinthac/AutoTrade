#!/usr/bin/env python3
"""Small-account sizing harness (Stage 1, read-only measurement).

WHY: on the user's real $3,000 IC Markets demo at `risk_per_trade_pct: 1.0`,
`risk/sizing.py::compute_lot_size()` returns `None` (skips the signal) whenever
the risk-based lot rounds below the broker's 0.01 minimum, per spec §3.1's
"อย่าฝืนเสี่ยงเกินแผน" (don't force excess risk). An earlier NOTE in
experiments_log.md measured ~52% of otherwise-valid signals skipped this way --
but that table was taken with Watchman breakeven/trail STILL ENABLED (before
EXP-008 adopted `false`/`false`), so its trade set is now stale. This harness
re-measures under the CURRENT config, and additionally tests a config-gated
deviation from spec §3.1 -- a "min-lot fallback with a risk cap": if the
computed lot is below volume_min but the actual dollar risk of trading the
minimum lot anyway is still <= a cap % of equity, trade the minimum lot instead
of skipping. This is TESTED here, not assumed good. Stage 1 only: NO source
under src/ is modified; the fallback lives entirely as a monkeypatch wrapper
around `autotrade.backtest.engine.compute_lot_size` in THIS file.

CRITICAL: unlike the stale NOTE's `one_level.py` (which built
`WatchmanConfig(wm[...], ...)` positionally and silently defaulted
breakeven_enabled/trail_enabled to True), this harness constructs BOTH the
RiskVoiceConfig and the WatchmanConfig with EVERY field from config/base.yaml,
mirroring scripts/run_backtest.py main() exactly -- so breakeven_enabled=false /
trail_enabled=false (EXP-008) are actually in effect.

Two modes:
  --mode riskgrid : sweep risk_per_trade_pct, fallback OFF (real behavior).
  --mode fallback : fixed risk=1.0%, sweep min_lot_risk_cap_pct in {None,...}.

Machine-parseable output: one `RESULT {json}` line per cell on stdout.
"""
from __future__ import annotations

import argparse
import json
import sys

import pandas as pd

import autotrade.backtest.engine as engine_mod
from autotrade.backtest.cost_model import CostModelConfig
from autotrade.backtest.engine import BacktestConfig, run_backtest
from autotrade.backtest.report import generate_report
from autotrade.common.config import load_yaml_config
from autotrade.common.symbol_spec import SymbolSpec
from autotrade.council.risk_voice import RiskVoiceConfig
from autotrade.feed.historical import HISTORICAL_DIR
from autotrade.watchman.evaluate import WatchmanConfig

# Hardcoded XAUUSD spec so the harness never needs a live MT5 connection --
# same values run_backtest.py resolves from get_symbol_spec() for IC Markets.
_SPEC = SymbolSpec(
    canonical="XAUUSD", broker_name="XAUUSD", digits=2, point=0.01,
    tick_size=0.01, tick_value=1.0, contract_size=100.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
    trade_stops_level=0, freeze_level=0,
)


def _build_risk_voice_cfg(cfg) -> RiskVoiceConfig:
    """Mirror scripts/run_backtest.py main() exactly (all 9 fields)."""
    rv = cfg["risk_voice"]
    return RiskVoiceConfig(
        max_spread_multiple=rv["max_spread_multiple"],
        max_spread_points_xauusd=rv["max_spread_points_xauusd"],
        news_blackout_before_min=rv["news_blackout_before_min"],
        news_blackout_after_min=rv["news_blackout_after_min"],
        max_stop_atr_multiple=rv["max_stop_atr_multiple"],
        session_start_hour=rv["session_start_hour"],
        session_end_hour=rv["session_end_hour"],
        friday_close_hour=rv["friday_close_hour"],
        max_atr_panic_multiple=rv["max_atr_panic_multiple"],
    )


def _build_watchman_cfg(cfg) -> WatchmanConfig:
    """Mirror scripts/run_backtest.py main() lines 228-236 EXACTLY -- every
    field, INCLUDING breakeven_enabled/trail_enabled (EXP-008 adopted false/
    false). Do NOT build this positionally / partially: the dataclass defaults
    both flags to True, which would silently measure the pre-EXP-008 config."""
    wm = cfg["watchman"]
    return WatchmanConfig(
        breakeven_at_r=wm["breakeven_at_r"],
        trail_start_r=wm["trail_start_r"],
        trail_distance_atr=wm["trail_distance_atr"],
        time_stop_hours=wm["time_stop_hours"],
        dead_trade_r_band=wm["dead_trade_r_band"],
        breakeven_enabled=wm["breakeven_enabled"],
        trail_enabled=wm["trail_enabled"],
    )


def _make_wrapper(cap_pct: float | None):
    """Return `(wrapper, state)`. The wrapper wraps the REAL compute_lot_size
    (never edits it). It records every call in order for fallback attribution,
    and -- when `cap_pct` is not None -- rescues a would-be-None (sub-min) lot
    to volume_min IFF the dollar risk of trading volume_min is <= cap_pct% of
    equity.

    state["calls"]    : list of dicts, one per call, in call order:
                        {lot: <float|None returned>, fallback: <bool>}
    state["raw_none"] : count of calls the REAL sizer returned None for
    state["rescued"]  : count of those rescued to volume_min by the fallback
    """
    real = engine_mod.compute_lot_size
    state = {"calls": [], "raw_none": 0, "rescued": 0}

    def wrapper(*args, **kwargs):
        # Engine calls compute_lot_size with keyword args only (see engine.py
        # run_backtest call site); read them by name.
        lot = real(*args, **kwargs)
        fallback = False
        if lot is None:
            state["raw_none"] += 1
            if cap_pct is not None:
                equity = kwargs["equity"]
                entry = kwargs["entry"]
                stop_loss = kwargs["stop_loss"]
                point_value = kwargs["point_value"]
                volume_min = kwargs["volume_min"]
                stop_distance = abs(entry - stop_loss)
                min_lot_risk = stop_distance * point_value * volume_min
                if min_lot_risk <= (cap_pct / 100.0) * equity:
                    lot = volume_min
                    fallback = True
                    state["rescued"] += 1
        state["calls"].append({"lot": lot, "fallback": fallback})
        return lot

    return wrapper, state


def _pf(trades) -> float | None:
    gp = sum(t.net_pnl for t in trades if t.net_pnl > 0)
    gl = -sum(t.net_pnl for t in trades if t.net_pnl < 0)
    if not trades:
        return None
    if gl == 0:
        return float("inf") if gp > 0 else 0.0
    return gp / gl


def _dd_usd(trades, start) -> float:
    eq = start
    peak = start
    worst = 0.0
    for t in trades:  # engine returns trades in exit order (sequential)
        eq += t.net_pnl
        peak = max(peak, eq)
        worst = min(worst, eq - peak)
    return worst


def _worst_streak_usd(trades) -> float:
    worst = 0.0
    run = 0.0
    for t in trades:
        if t.net_pnl < 0:
            run += t.net_pnl
            worst = min(worst, run)
        else:
            run = 0.0
    return worst


def _attribute_fallback_trades(state, trades):
    """Correlate each executed trade with its compute_lot_size call, in order.

    Only non-None-lot calls produce trades. The engine holds at most one open
    position at a time and processes bars sequentially, so the ordered sequence
    of non-None-lot calls maps 1:1 onto the ordered trade list -- EXCEPT a
    signal on the final bar can size a non-None lot that never fills (no next
    bar). We zip and ASSERT lot equality before trusting the mapping (gotcha
    #2); a single trailing unfilled call is tolerated (len diff of 1), anything
    else raises."""
    sized = [c for c in state["calls"] if c["lot"] is not None]
    n_sized, n_trades = len(sized), len(trades)
    if n_sized == n_trades:
        pairs = list(zip(sized, trades))
    elif n_sized == n_trades + 1:
        # last sized call was a final-bar signal that never filled -- drop it.
        pairs = list(zip(sized[:-1], trades))
    else:
        raise AssertionError(
            f"sizing-call/trade count mismatch: {n_sized} sized calls vs "
            f"{n_trades} trades (diff must be 0 or 1)"
        )
    for call, trade in pairs:
        assert abs(call["lot"] - trade.lot_size) < 1e-9, (
            f"lot mismatch in attribution: call lot {call['lot']} != trade "
            f"lot {trade.lot_size} -- ordering assumption violated"
        )
    fallback_trades = [t for c, t in pairs if c["fallback"]]
    normal_trades = [t for c, t in pairs if not c["fallback"]]
    return fallback_trades, normal_trades


def _cell_metrics(trades, start_equity) -> dict:
    rep = generate_report(trades, start_equity)
    return {
        "trades": rep.trade_count,
        "win_rate": round(rep.win_rate, 4) if rep.win_rate is not None else None,
        "PF": round(rep.profit_factor, 4) if rep.profit_factor not in (None, float("inf")) else rep.profit_factor,
        "PF_ex5": round(rep.profit_factor_excluding_top_5, 4) if rep.profit_factor_excluding_top_5 not in (None, float("inf")) else rep.profit_factor_excluding_top_5,
        "net": round(rep.total_net_pnl, 2),
        "avgR": round(rep.avg_r_multiple, 4) if rep.avg_r_multiple is not None else None,
        "maxDD_pct": round(rep.max_drawdown_pct, 2) if rep.max_drawdown_pct is not None else None,
        "maxDD_usd": round(_dd_usd(trades, start_equity), 2),
        "max_single_loss": round(min((t.net_pnl for t in trades), default=0.0), 2),
        "worst_streak_usd": round(_worst_streak_usd(trades), 2),
    }


def run_cell(df, *, risk_pct, cap_pct, cfg, commission, start_equity) -> dict:
    """Run one full-history backtest with a fallback wrapper installed
    (cap_pct=None => wrapper never rescues => identical to no fallback)."""
    rv = _build_risk_voice_cfg(cfg)
    wm = _build_watchman_cfg(cfg)
    wrapper, state = _make_wrapper(cap_pct)

    original = engine_mod.compute_lot_size
    engine_mod.compute_lot_size = wrapper
    try:
        bt = BacktestConfig(
            starting_equity=start_equity,
            risk_per_trade_pct=risk_pct,
            cost_model=CostModelConfig(commission_per_lot=commission, slippage_points=None),
            risk_voice_cfg=rv,
            watchman_cfg=wm,
            pivot_bars=cfg["global"]["swing_pivot_bars"],
        )
        trades = run_backtest(df, "XAUUSD", _SPEC, bt)
    finally:
        engine_mod.compute_lot_size = original

    calls = len(state["calls"])
    raw_none = state["raw_none"]
    rescued = state["rescued"]
    skips = raw_none - rescued  # signals still discarded after fallback

    out = {
        "mode_risk_pct": risk_pct,
        "cap_pct": cap_pct,
        "signals_to_sizing": calls,
        "skips": skips,
        "skip_pct": round(100.0 * skips / calls, 2) if calls else 0.0,
        **_cell_metrics(trades, start_equity),
    }

    if cap_pct is not None:
        fb_trades, _normal = _attribute_fallback_trades(state, trades)
        out["fallback_trades"] = len(fb_trades)
        out["fallback_frac_of_executed"] = (
            round(len(fb_trades) / len(trades), 4) if trades else 0.0
        )
        out["fallback_subset"] = {
            "trades": len(fb_trades),
            "net": round(sum(t.net_pnl for t in fb_trades), 2),
            "PF": (round(_pf(fb_trades), 4) if _pf(fb_trades) not in (None, float("inf")) else _pf(fb_trades)),
            "win_rate": (round(sum(1 for t in fb_trades if t.net_pnl > 0) / len(fb_trades), 4) if fb_trades else None),
            "max_single_loss": round(min((t.net_pnl for t in fb_trades), default=0.0), 2),
        }
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["riskgrid", "fallback"], required=True)
    p.add_argument("--starting-equity", type=float, default=3000.0)
    p.add_argument("--commission-per-lot", type=float, default=7.0)
    args = p.parse_args()

    cfg = load_yaml_config("base")
    df = pd.read_csv(HISTORICAL_DIR / "XAUUSD_H1.csv", parse_dates=["time"])

    if args.mode == "riskgrid":
        for risk in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
            res = run_cell(
                df, risk_pct=risk, cap_pct=None, cfg=cfg,
                commission=args.commission_per_lot, start_equity=args.starting_equity,
            )
            print("RESULT " + json.dumps(res), flush=True)
    else:  # fallback
        for cap in (None, 1.25, 1.5, 2.0):
            res = run_cell(
                df, risk_pct=1.0, cap_pct=cap, cfg=cfg,
                commission=args.commission_per_lot, start_equity=args.starting_equity,
            )
            print("RESULT " + json.dumps(res), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
