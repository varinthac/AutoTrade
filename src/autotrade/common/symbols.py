"""Canonical <-> broker symbol mapping (spec.md §4 "Symbol abstraction").

Strategy code (council/, shield/, risk/, watchman/) only ever sees canonical
names like "XAUUSD". This module is the one place broker-specific spellings
("XAUUSD.a", "GOLD", etc.) and SYMBOL_INFO lookups happen -- and thus the one
place `MetaTrader5` is imported for symbol resolution. The `SymbolSpec` shape
itself lives in `common/symbol_spec.py` (MT5-free) and is re-exported here
for existing callers; MT5-free modules (e.g. `backtest/`) must import it from
`common.symbol_spec` directly, not through this module (spec.md §2.3's
dependency-direction invariant).
"""
from __future__ import annotations

import MetaTrader5 as mt5

from autotrade.common.config import load_yaml_config
from autotrade.common.symbol_spec import SymbolSpec

__all__ = ["SymbolSpec", "UnknownSymbolError", "to_broker_name", "get_symbol_spec"]


class UnknownSymbolError(RuntimeError):
    pass


def _load_symbol_map() -> dict[str, str]:
    return load_yaml_config("base")["symbols"]


def to_broker_name(canonical: str, symbol_map: dict[str, str] | None = None) -> str:
    symbol_map = symbol_map or _load_symbol_map()
    try:
        return symbol_map[canonical]
    except KeyError as e:
        raise UnknownSymbolError(
            f"{canonical!r} is not in config/base.yaml symbols: {sorted(symbol_map)}"
        ) from e


def get_symbol_spec(canonical: str, symbol_map: dict[str, str] | None = None) -> SymbolSpec:
    """Resolve a canonical symbol to its broker name and pull SYMBOL_INFO.
    Requires an active mt5_session()."""
    broker_name = to_broker_name(canonical, symbol_map)

    if not mt5.symbol_select(broker_name, True):
        code, desc = mt5.last_error()
        raise UnknownSymbolError(
            f"mt5.symbol_select({broker_name!r}) failed: [{code}] {desc}. "
            "Check the symbol is visible in Market Watch and the broker mapping "
            "in config/base.yaml matches this account's symbol names."
        )

    info = mt5.symbol_info(broker_name)
    if info is None:
        code, desc = mt5.last_error()
        raise UnknownSymbolError(f"mt5.symbol_info({broker_name!r}) returned None: [{code}] {desc}")

    return SymbolSpec(
        canonical=canonical,
        broker_name=broker_name,
        digits=info.digits,
        point=info.point,
        tick_size=info.trade_tick_size,
        tick_value=info.trade_tick_value,
        contract_size=info.trade_contract_size,
        volume_min=info.volume_min,
        volume_max=info.volume_max,
        volume_step=info.volume_step,
        trade_stops_level=info.trade_stops_level,
        freeze_level=info.trade_freeze_level,
    )
