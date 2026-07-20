"""Static correlation table -- The Shield's correlation guard, per spec.md
§9 Open Decision #5 ("Correlation matrix source for Shield (Phase 5) --
start static/config-driven") and trading_system_summary_v2.md Appendix A §2
rule 2.

PLACEHOLDER VALUES: every correlation below is an illustrative estimate
(e.g. EURUSD/GBPUSD are typically positively correlated since both trade
against USD; XAUUSD/USDJPY are typically negatively correlated since gold
tends to rise on USD weakness while USDJPY tends to fall on it), not a
value measured from real historical price data. These MUST be validated
against an actual rolling correlation calculation before being trusted for
real risk decisions -- same honestly-flagged-placeholder pattern as
`backtest/cost_model.py`'s `commission_per_lot=0.0`. A live rolling-60-day
correlation calculator is explicitly out of scope for Phase 5 (spec.md §9).
"""
from __future__ import annotations

# Symmetric pairwise correlation, one entry per unordered pair of the 4
# configured symbols (config/base.yaml's `symbols:` block). Same-symbol and
# unlisted pairs are handled in get_correlation(), not listed here.
_CORRELATION_TABLE: dict[frozenset[str], float] = {
    frozenset({"EURUSD", "GBPUSD"}): 0.85,
    frozenset({"EURUSD", "USDJPY"}): -0.30,
    frozenset({"GBPUSD", "USDJPY"}): -0.20,
    frozenset({"XAUUSD", "EURUSD"}): 0.35,
    frozenset({"XAUUSD", "GBPUSD"}): 0.25,
    frozenset({"XAUUSD", "USDJPY"}): -0.35,
}


def get_correlation(symbol_a: str, symbol_b: str) -> float:
    """Static correlation between two canonical symbols, in [-1, 1].

    Same symbol is trivially 1.0 -- note rule 3 (max positions per symbol)
    already prevents a same-symbol duplicate independent of this; the two
    checks are deliberately not conflated here. An unlisted pair (a symbol
    not yet added to `_CORRELATION_TABLE`) defaults to 0.0 (treated as
    uncorrelated) rather than raising -- the correlation guard should not
    hard-fail just because a new symbol hasn't been catalogued yet, it
    should simply not block on it.
    """
    if symbol_a == symbol_b:
        return 1.0
    return _CORRELATION_TABLE.get(frozenset({symbol_a, symbol_b}), 0.0)
