"""`SymbolSpec` -- the plain, MT5-free data shape for a symbol's tradeable
properties (spec.md §2.3's dependency-direction invariant: modules like
`backtest/` must be able to import this without pulling in `MetaTrader5`).

`common/symbols.py` is the module that actually touches MT5 to populate one
of these (`get_symbol_spec`); this module only defines the shape.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolSpec:
    canonical: str
    broker_name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    trade_stops_level: int
    freeze_level: int
