from datetime import datetime, timezone

from autotrade.feed.historical import _is_weekend_gap


def _dt(y, m, d, h=0):
    return datetime(y, m, d, h, tzinfo=timezone.utc)


def test_friday_to_sunday_is_weekend_gap():
    assert _is_weekend_gap(_dt(2026, 7, 17, 21), _dt(2026, 7, 19, 22)) is True


def test_friday_to_monday_is_weekend_gap():
    assert _is_weekend_gap(_dt(2026, 7, 17, 21), _dt(2026, 7, 20, 0)) is True


def test_midweek_gap_is_not_weekend_gap():
    assert _is_weekend_gap(_dt(2026, 7, 15, 10), _dt(2026, 7, 15, 14)) is False


def test_gap_longer_than_typical_weekend_is_not_classified_as_weekend():
    # e.g. a real multi-day connectivity/data hole starting on a Friday
    assert _is_weekend_gap(_dt(2026, 7, 17, 21), _dt(2026, 7, 22, 0)) is False
