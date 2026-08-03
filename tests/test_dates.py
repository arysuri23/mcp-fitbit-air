from datetime import date
from zoneinfo import ZoneInfo

import pytest

from mcp_fitbit_air.dates import DateParseError, resolve_day, resolve_range

TZ = ZoneInfo("America/New_York")
TODAY = date(2026, 8, 2)  # a Sunday


def test_iso_dates_pass_through():
    assert resolve_range("2026-07-01", "2026-07-05", TZ, TODAY) == (
        date(2026, 7, 1),
        date(2026, 7, 5),
    )


def test_yesterday():
    assert resolve_day("yesterday", TZ, TODAY) == date(2026, 8, 1)


def test_today():
    assert resolve_day("today", TZ, TODAY) == TODAY


def test_last_week_is_the_seven_days_ending_yesterday():
    start, end = resolve_range("last week", None, TZ, TODAY)
    assert (start, end) == (date(2026, 7, 26), date(2026, 8, 1))


def test_last_n_days():
    start, end = resolve_range("last 30 days", None, TZ, TODAY)
    assert (start, end) == (date(2026, 7, 3), date(2026, 8, 1))


def test_last_month():
    start, end = resolve_range("last month", None, TZ, TODAY)
    assert (start, end) == (date(2026, 7, 3), date(2026, 8, 1))


def test_range_spanning_a_month_boundary():
    assert resolve_range("2026-07-28", "2026-08-02", TZ, TODAY) == (
        date(2026, 7, 28),
        date(2026, 8, 2),
    )


def test_omitted_end_with_iso_start_runs_through_today():
    start, end = resolve_range("2026-07-28", None, TZ, TODAY)
    assert (start, end) == (date(2026, 7, 28), TODAY)


def test_reversed_range_raises():
    with pytest.raises(DateParseError) as exc:
        resolve_range("2026-08-02", "2026-07-01", TZ, TODAY)
    assert "before" in str(exc.value).lower()


def test_unparseable_input_raises_with_examples():
    with pytest.raises(DateParseError) as exc:
        resolve_day("the day before the big meeting", TZ, TODAY)
    assert "yesterday" in str(exc.value)


def test_parsing_is_case_and_whitespace_insensitive():
    assert resolve_day("  YESTERDAY ", TZ, TODAY) == date(2026, 8, 1)


def test_whole_range_expression_with_explicit_end_raises():
    with pytest.raises(DateParseError) as exc:
        resolve_range("last week", "2026-08-05", TZ, TODAY)
    assert "already describes a whole range" in str(exc.value)
