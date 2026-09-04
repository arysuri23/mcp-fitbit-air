from datetime import date
from zoneinfo import ZoneInfo
from unittest.mock import Mock

from mcp_fitbit_air.client import ApiError
from mcp_fitbit_air.fetch import build_filter, fetch_metric, point_date
from mcp_fitbit_air.mapping import get_metric
from mcp_fitbit_air.results import ResultState

START = date(2026, 8, 1)
END = date(2026, 8, 3)
TZ = ZoneInfo("America/New_York")


def daily_point(payload_key, fields, day=(2026, 8, 1)):
    y, m, d = day
    return {payload_key: {"date": {"year": y, "month": m, "day": d}, **fields}}


def test_list_metric_maps_points_onto_days():
    client = Mock()
    client.list_data_points.return_value = [
        daily_point("dailyRestingHeartRate", {"beatsPerMinute": "58"}, (2026, 8, 1)),
        daily_point("dailyRestingHeartRate", {"beatsPerMinute": "60"}, (2026, 8, 2)),
    ]

    series = fetch_metric(client, "resting_heart_rate", START, END, TZ)

    assert series.state is ResultState.OK
    assert series.by_day[date(2026, 8, 1)] == 58.0
    assert series.by_day[date(2026, 8, 2)] == 60.0


def test_rollup_metric_uses_daily_rollup_call():
    client = Mock()
    client.daily_rollup.return_value = [
        {
            "civilStartTime": {"date": {"year": 2026, "month": 8, "day": 1}},
            "steps": {"countSum": "9000"},
        }
    ]

    series = fetch_metric(client, "steps", START, END, TZ)

    client.daily_rollup.assert_called_once()
    client.list_data_points.assert_not_called()
    assert series.by_day[date(2026, 8, 1)] == 9000.0


def test_empty_response_for_zero_warmup_metric_is_no_data():
    client = Mock()
    client.daily_rollup.return_value = []

    series = fetch_metric(client, "steps", START, END, TZ)

    assert series.state is ResultState.NO_DATA
    assert series.by_day == {}


def test_empty_response_for_warmup_metric_is_warming_up():
    """HRV needs several nights. An empty result over a short window means the
    metric has not been computed yet, not that anything is broken."""
    client = Mock()
    client.list_data_points.return_value = []

    series = fetch_metric(client, "hrv", date(2026, 8, 1), date(2026, 8, 2), TZ)

    assert series.state is ResultState.WARMING_UP
    assert "3" in series.message


def test_empty_response_over_a_long_window_is_no_data_not_warming_up():
    client = Mock()
    client.list_data_points.return_value = []

    series = fetch_metric(client, "hrv", date(2026, 6, 1), date(2026, 8, 1), TZ)

    assert series.state is ResultState.NO_DATA


def test_api_error_becomes_error_state_rather_than_raising():
    client = Mock()
    client.list_data_points.side_effect = ApiError("boom", status=500)

    series = fetch_metric(client, "hrv", START, END, TZ)

    assert series.state is ResultState.ERROR
    assert "boom" in series.message


def test_sleep_sums_multiple_sessions_in_one_day():
    """A nap and a night's sleep on the same day must combine, not overwrite."""
    client = Mock()
    point = lambda mins: {
        "sleep": {
            "interval": {"endTime": "2026-08-01T12:00:00Z", "endUtcOffset": "-14400s"},
            "summary": {"minutesAsleep": mins},
        }
    }
    client.list_data_points.return_value = [point("385"), point("45")]

    series = fetch_metric(client, "sleep_duration", START, END, TZ)

    assert series.by_day[date(2026, 8, 1)] == 430.0


def test_non_additive_metric_keeps_one_value_per_day():
    client = Mock()
    client.list_data_points.return_value = [
        daily_point("dailyRestingHeartRate", {"beatsPerMinute": "58"}, (2026, 8, 1)),
        daily_point("dailyRestingHeartRate", {"beatsPerMinute": "62"}, (2026, 8, 1)),
    ]

    series = fetch_metric(client, "resting_heart_rate", START, END, TZ)

    assert series.by_day[date(2026, 8, 1)] == 62.0


def test_skin_temperature_deviation_is_derived_per_day():
    client = Mock()
    client.list_data_points.return_value = [
        daily_point(
            "dailySleepTemperatureDerivations",
            {"nightlyTemperatureCelsius": 34.5, "baselineTemperatureCelsius": 34.0},
            (2026, 8, 1),
        )
    ]

    series = fetch_metric(client, "skin_temperature_deviation", START, END, TZ)

    assert series.by_day[date(2026, 8, 1)] == 0.5


def test_unreadable_points_are_skipped_not_fatal():
    client = Mock()
    client.list_data_points.return_value = [
        {"garbage": True},
        daily_point("dailyRestingHeartRate", {"beatsPerMinute": "58"}, (2026, 8, 1)),
    ]

    series = fetch_metric(client, "resting_heart_rate", START, END, TZ)

    assert series.by_day == {date(2026, 8, 1): 58.0}


def test_point_date_delegates_to_the_metric_resolver():
    assert point_date(
        get_metric("hrv"),
        {"dailyHeartRateVariability": {"date": {"year": 2026, "month": 8, "day": 1}}},
    ) == date(2026, 8, 1)
    assert point_date(
        get_metric("steps"),
        {"civilStartTime": {"date": {"year": 2026, "month": 8, "day": 2}}},
    ) == date(2026, 8, 2)
    assert point_date(get_metric("hrv"), {"nothing": 1}) is None


# --- Filter construction. Getting the dialect wrong returns HTTP 200 with zero
# --- points rather than an error, so these assertions are load-bearing.


def test_civil_date_filter_uses_bare_dates_and_an_exclusive_end():
    expr = build_filter(get_metric("hrv"), START, END, TZ)
    assert expr == (
        'daily_heart_rate_variability.date >= "2026-08-01" AND '
        'daily_heart_rate_variability.date < "2026-08-04"'
    )


def test_sleep_filter_uses_civil_end_time():
    expr = build_filter(get_metric("sleep_duration"), START, END, TZ)
    assert expr.startswith('sleep.interval.civil_end_time >= "2026-08-01"')


def test_physical_filter_uses_rfc3339_utc_instants():
    """Local midnight in America/New_York is 04:00Z in August."""
    expr = build_filter(get_metric("heart_rate"), START, START, TZ)
    assert expr == (
        'heart_rate.sample_time.physical_time >= "2026-08-01T04:00:00Z" AND '
        'heart_rate.sample_time.physical_time < "2026-08-02T04:00:00Z"'
    )


def test_physical_filter_respects_a_different_timezone():
    expr = build_filter(get_metric("heart_rate"), START, START, ZoneInfo("UTC"))
    assert '"2026-08-01T00:00:00Z"' in expr


# -- Pagination truncation reaches the tools ---------------------------------
#
# fetch_metric is the only path from the client's DataPoints to a MetricSeries.
# If it drops the truncation flag here, every layer above it reports a partial
# series as complete no matter how carefully they handle the flag they never
# receive.


def test_fetch_metric_propagates_truncation_from_the_client():
    from mcp_fitbit_air.client import DataPoints

    client = Mock()
    client.daily_rollup.return_value = DataPoints(
        [],
        truncated=True,
        truncation_reason="Reached the 50-page fetch limit.",
    )

    series = fetch_metric(client, "steps", date(2026, 8, 1), date(2026, 8, 2), ZoneInfo("UTC"))

    assert series.truncated is True
    assert "50-page" in series.truncation_reason


def test_fetch_metric_propagates_truncation_from_a_list_call():
    """The list path and the rollup path are separate calls in fetch_metric;
    covering only one would leave the other free to drop the flag."""
    from mcp_fitbit_air.client import DataPoints

    client = Mock()
    client.list_data_points.return_value = DataPoints(
        [], truncated=True, truncation_reason="Reached the 50-page fetch limit."
    )

    series = fetch_metric(client, "hrv", date(2026, 8, 1), date(2026, 8, 2), ZoneInfo("UTC"))

    assert series.truncated is True
    assert "50-page" in series.truncation_reason


def test_fetch_metric_reports_a_complete_fetch_as_untruncated():
    from mcp_fitbit_air.client import DataPoints

    client = Mock()
    client.daily_rollup.return_value = DataPoints([])

    series = fetch_metric(client, "steps", date(2026, 8, 1), date(2026, 8, 2), ZoneInfo("UTC"))

    assert series.truncated is False
    assert series.truncation_reason is None


# -- Warm-up must be reachable through the tools that use a widened window ----
#
# Found in review. fetch_metric judged warm-up from the window it was handed,
# but both tools hand it a window widened by up to 30 days for the baseline. A
# 7-day request became a 37-day window, so hrv's threshold of 6 could never
# trip and a days-old account was told "no data" instead of "warming up" - the
# single scenario the state exists for.


def test_warming_up_is_judged_by_the_requested_range_not_the_fetch_window():
    from datetime import date

    from mcp_fitbit_air.fetch import fetch_metric

    client = Mock()
    client.list_data_points.return_value = []

    series = fetch_metric(
        client,
        "hrv",
        date(2026, 1, 1),   # widened start, 30 days before the request
        date(2026, 2, 6),
        TZ,
        requested_days=3,   # what the caller actually asked about
    )

    assert series.state is ResultState.WARMING_UP
    assert "nights" in series.message


def test_a_long_requested_range_is_still_no_data():
    """Asking about a month and getting nothing is absence, not warm-up - the
    band would have had ample time to compute it."""
    from datetime import date

    from mcp_fitbit_air.fetch import fetch_metric

    client = Mock()
    client.list_data_points.return_value = []

    series = fetch_metric(
        client, "hrv", date(2026, 1, 1), date(2026, 2, 6), TZ, requested_days=30
    )

    assert series.state is ResultState.NO_DATA


def test_requested_days_defaults_to_the_fetched_window():
    """Callers that do not widen anything should not have to say so twice."""
    from datetime import date

    from mcp_fitbit_air.fetch import fetch_metric

    client = Mock()
    client.list_data_points.return_value = []

    series = fetch_metric(client, "hrv", date(2026, 1, 1), date(2026, 1, 3), TZ)

    assert series.state is ResultState.WARMING_UP


# -- The remedy is the most actionable thing an error carries -----------------
#
# Found in review. fetch_metric kept ApiError's message and dropped its remedy,
# so a 401 mid-session reported "the API rejected our credentials" without the
# one line that says what to do about it - while the intraday path, which lets
# ApiError reach tool_guard, reported it correctly. Same tool, same failure,
# different answer.


def test_fetch_metric_preserves_the_remedy_from_an_api_error():
    from datetime import date

    from mcp_fitbit_air.fetch import fetch_metric

    client = Mock()
    client.list_data_points.side_effect = ApiError(
        "The Health API rejected our credentials (401): Invalid Credentials",
        status=401,
        remedy="Run `mcp-fitbit-air auth` to re-authenticate.",
    )

    series = fetch_metric(client, "hrv", date(2026, 8, 1), date(2026, 8, 2), TZ)

    assert series.state is ResultState.ERROR
    assert series.remedy == "Run `mcp-fitbit-air auth` to re-authenticate."


def test_an_error_without_a_remedy_leaves_it_unset():
    from datetime import date

    from mcp_fitbit_air.fetch import fetch_metric

    client = Mock()
    client.list_data_points.side_effect = ApiError("Backend error", status=500)

    series = fetch_metric(client, "hrv", date(2026, 8, 1), date(2026, 8, 2), TZ)

    assert series.remedy is None
