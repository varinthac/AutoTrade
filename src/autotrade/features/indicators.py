"""Pure technical-indicator functions consumed by council/ (Phase 6) per
trading_system_summary_v2.md Appendix A §0/§1.1/§1.2.

Inputs are plain pandas Series/DataFrames of **closed H1 bars only** (Appendix
A §0: "ทุก indicator ... คำนวณจาก H1 closed bars เท่านั้น") — these functions
do no I/O and never touch a partially-formed bar; the caller is responsible
for excluding the still-forming bar before calling in.

Smoothing conventions (documented here so it's checkable against a reference
calculation later, since Appendix A's various ATR-multiple thresholds assume
one specific convention):

- `ema()` is the standard exponential moving average: a recursive formula
  seeded by the first close, `alpha = 2 / (period + 1)`
  (`pandas.Series.ewm(span=period, adjust=False)`).
- `rsi()` and `atr()` both use **Wilder's smoothing implemented as an EWM with
  `alpha = 1/period`, `adjust=False`**, applied directly to the gain/loss (RSI)
  or true-range (ATR) series from the first bar onward. This is deliberately
  NOT the textbook Wilder warm-up (which seeds bar `period` with a plain SMA
  of the first `period` values before switching to the recursive step) — the
  EWM-from-the-first-bar variant converges to the same steady-state values but
  differs slightly during the first `period` bars. Both `rsi()` and `atr()`
  use this same convention consistently, so ATR-multiple thresholds elsewhere
  in Appendix A stay internally self-consistent.
"""
from __future__ import annotations

import pandas as pd


def ema(closes: pd.Series, period: int) -> pd.Series:
    """Standard exponential moving average, alpha = 2/(period+1)."""
    return closes.ewm(span=period, adjust=False).mean()


def rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI (see module docstring for the exact smoothing convention)."""
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd_histogram(
    closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.Series:
    """MACD histogram = MACD line (EMA-fast − EMA-slow) minus its own
    signal-line EMA. Standard industry-default periods (12/26/9)."""
    macd_line = ema(closes, fast) - ema(closes, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line - signal_line


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's ATR (see module docstring for the exact smoothing convention)."""
    prev_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False).mean()
