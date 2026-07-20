"""Tests for shield/correlation.py -- the static, config-driven correlation
table (spec.md §9 Open Decision #5)."""
from __future__ import annotations

from autotrade.shield.correlation import get_correlation


def test_same_symbol_correlation_is_always_one():
    assert get_correlation("XAUUSD", "XAUUSD") == 1.0
    assert get_correlation("EURUSD", "EURUSD") == 1.0


def test_known_pair_is_symmetric():
    assert get_correlation("EURUSD", "GBPUSD") == get_correlation("GBPUSD", "EURUSD")


def test_known_correlated_pair_returns_configured_value():
    assert get_correlation("EURUSD", "GBPUSD") == 0.85


def test_unlisted_pair_defaults_to_zero_uncorrelated():
    assert get_correlation("XAUUSD", "NOTACONFIGUREDSYMBOL") == 0.0
