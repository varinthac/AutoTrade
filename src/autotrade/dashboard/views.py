"""Pure display logic for the read-only trade dashboard -- no Flask import,
so it's independently testable, same separation `gui/control.py`/
`gui/env_file.py` already use for the desktop GUI (framework shell vs. pure
logic).

Every timestamp here is already MT5 broker SERVER time (`store/models.py`'s
module docstring) -- formatted verbatim, with zero timezone conversion.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import date, datetime

from autotrade.store.models import TradeRecord

# Wide-but-finite bounds for "all trades ever" range queries -- same
# convention as scripts/run_auditor.py's _EPOCH/_FAR_FUTURE (safer across
# SQLite/SQLAlchemy datetime handling than datetime.min/datetime.max).
EPOCH = datetime(2000, 1, 1)
FAR_FUTURE = datetime(2100, 1, 1)

TRADES_PER_PAGE = 50


def format_server_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


@dataclass(frozen=True)
class TradeRow:
    exit_time: str
    symbol: str
    direction: str
    entry_time: str
    entry_price: float
    exit_price: float
    lot_size: float
    cost: float
    net_pnl: float
    r_multiple: float
    exit_reason: str


# Column order/names for the exported .xlsx -- kept alongside TradeRow so an
# empty filtered result still produces a header-only sheet with real column
# names, not a headerless blank file.
EXPORT_COLUMNS = [f.name for f in fields(TradeRow)]


def to_trade_row(trade: TradeRecord) -> TradeRow:
    return TradeRow(
        exit_time=format_server_time(trade.exit_time),
        symbol=trade.symbol,
        direction=trade.direction,
        entry_time=format_server_time(trade.entry_time),
        entry_price=trade.entry_price,
        exit_price=trade.exit_price,
        lot_size=trade.lot_size,
        cost=trade.cost,
        net_pnl=trade.net_pnl,
        r_multiple=trade.r_multiple,
        exit_reason=trade.exit_reason,
    )


# Leading characters Excel/LibreOffice will interpret a cell's content as a
# formula (the standard CSV/Excel-formula-injection trigger set).
_FORMULA_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _escape_formula_injection(value: str) -> str:
    """Prefixes a leading apostrophe so Excel/LibreOffice force the cell to
    text instead of evaluating it as a formula -- defense-in-depth for the
    export path only: `symbol`/`direction`/`exit_reason` are closed-vocabulary
    today, but `TradeRecord` has no DB-level CHECK constraint enforcing that,
    same "assume a future bug could get here" posture as
    `NoHistoricalNewsDataProvider`'s explicit-placeholder pattern."""
    if value.startswith(_FORMULA_INJECTION_PREFIXES):
        return "'" + value
    return value


def trades_to_export_rows(trades: list[TradeRecord]) -> list[dict]:
    """Same fields/formatting as `to_trade_row` (server-time strings, not
    datetime objects -- a real Excel datetime cell risks the viewer's
    spreadsheet app re-interpreting it in local timezone, exactly the bug
    class this package avoids everywhere else), but as plain dicts ready for
    `pandas.DataFrame(...)` in `app.py`'s export route. String fields are
    escaped against Excel formula injection here (not in `to_trade_row`,
    which feeds the HTML table -- a different, already-safe rendering
    context via Jinja2 auto-escaping)."""
    rows = []
    for t in trades:
        row = asdict(to_trade_row(t))
        for key, value in row.items():
            if isinstance(value, str):
                row[key] = _escape_formula_injection(value)
        rows.append(row)
    return rows


def newest_first(trades: list[TradeRecord]) -> list[TradeRecord]:
    """`journal.get_trades_in_range` returns ascending by `exit_time` --
    reverse for the dashboard's newest-first display."""
    return list(reversed(trades))


@dataclass(frozen=True)
class OpenPositionData:
    """Plain, MT5-free input shape for `to_open_position_row` -- `app.py`'s
    `get_open_positions_display()` builds one of these per position returned
    by `mt5.positions_get()` (after resolving the broker symbol back to its
    canonical name), so this module never has to import MetaTrader5 or see a
    raw MT5 named tuple, same "domain object, not a raw row" boundary
    `to_trade_row`'s `TradeRecord` param already uses."""
    ticket: int
    symbol: str
    direction: str
    volume: float
    price_open: float
    price_current: float
    sl: float
    tp: float
    profit: float


@dataclass(frozen=True)
class OpenPositionRow:
    ticket: int
    symbol: str
    direction: str
    volume: float
    price_open: float
    price_current: float
    sl: float
    tp: float
    profit: float


def to_open_position_row(position: OpenPositionData) -> OpenPositionRow:
    return OpenPositionRow(
        ticket=position.ticket,
        symbol=position.symbol,
        direction=position.direction,
        volume=position.volume,
        price_open=position.price_open,
        price_current=position.price_current,
        sl=position.sl,
        tp=position.tp,
        profit=position.profit,
    )


def sort_open_positions(positions: list[OpenPositionRow]) -> list[OpenPositionRow]:
    """Sorted by ticket for a stable, deterministic display order -- unlike
    closed trades there is no natural "newest first" axis to sort by (no
    exit_time yet), and at this project's current single-symbol scale there
    are rarely more than 1-3 open positions at once, so sort order here is
    low-stakes; ticket ascension (roughly open order) is simplest and stable
    across repeated page loads."""
    return sorted(positions, key=lambda p: p.ticket)


def paginate(trades: list[TradeRecord], page: int, per_page: int = TRADES_PER_PAGE) -> list[TradeRecord]:
    """Slices an already-fetched list in Python -- `store/journal.py` has no
    `limit`/`offset` param today and this dashboard should NOT add one; if
    the paper DB ever grows large enough for this to matter, pushing
    pagination into SQL via a new `journal.py` param would be the right
    follow-up, not something to build now."""
    page = max(page, 1)
    start = (page - 1) * per_page
    return trades[start:start + per_page]


def parse_date_param(value: str | None) -> datetime | None:
    """`None` for both "not given" and "malformed" -- a query param a user
    can easily mistype by hand (or a stale/hand-edited bookmark) must
    degrade to the route's own default behavior, never a 500."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def default_daily_date(all_trades: list[TradeRecord]) -> date | None:
    """The server date to default `/daily` to when `?date=` is omitted: the
    server date of the most recent trade's `exit_time`, derived purely from
    already-fetched data -- never MT5/`date.today()` local wall-clock, same
    server-day-only convention as `scripts/run_auditor.py`'s `_server_today`.
    `None` when there are no trades at all (nothing to default to)."""
    if not all_trades:
        return None
    return max(t.exit_time for t in all_trades).date()
