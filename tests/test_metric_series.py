import asyncio
import datetime as dt
import json
from datetime import date, timedelta
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from mcp_fitbit_air.fetch import MetricSeries
from mcp_fitbit_air.mapping import get_metric
from mcp_fitbit_air.results import ResultState

TZ = ZoneInfo("UTC")


@pytest.fixture
def fake_context(monkeypatch):
    ctx = Mock()
    ctx.timezone = TZ
    monkeypatch.setattr("mcp_fitbit_air.server.get_context", lambda: ctx)
    return ctx


def stub_fetch(monkeypatch, series, record=None):
    """Replace fetch_metric in the server's namespace, optionally recording the
    range it was called with."""
    from mcp_fitbit_air import server

    def fake(client, name, start, end, tz, **kwargs):
        if record is not None:
            record.append((start, end))
        return series

    monkeypatch.setattr(server, "fetch_metric", fake)


# Real point shapes, taken from tests/fixtures/list_heart_rate_nofilter.json and
# list_steps_nofilter.json. Using an invented shape here would let every
# extraction assertion pass vacuously on zero readings.
def hr_point(bpm: str, physical_time: str) -> dict:
    return {
        "dataSource": {"platform": "FITBIT", "recordingMethod": "PASSIVELY_MEASURED"},
        "heartRate": {
            "beatsPerMinute": bpm,
            "sampleTime": {"physicalTime": physical_time, "utcOffset": "-14400s"},
        },
    }


def step_point(count: str, start_time: str) -> dict:
    return {
        "dataSource": {"platform": "FITBIT"},
        "steps": {
            "count": count,
            "interval": {
                "startTime": start_time,
                "startUtcOffset": "-14400s",
                "endTime": start_time,
                "endUtcOffset": "-14400s",
            },
        },
    }


# -- Daily granularity -------------------------------------------------------


def test_daily_series_returns_points_with_baseline(fake_context, monkeypatch):
    from mcp_fitbit_air import server

    stub_fetch(
        monkeypatch,
        MetricSeries(
            metric=get_metric("hrv"),
            by_day={date(2026, 8, 1): 55.0, date(2026, 8, 2): 42.0},
        ),
    )

    result = server.get_metric_series("hrv", "2026-08-01", "2026-08-02")

    assert result["state"] == "ok"
    assert result["data"]["unit"] == "milliseconds"
    assert result["data"]["baseline"]["mean"] == 48.5
    assert len(result["data"]["points"]) == 2


def test_daily_points_are_sorted_by_date(fake_context, monkeypatch):
    from mcp_fitbit_air import server

    stub_fetch(
        monkeypatch,
        MetricSeries(
            metric=get_metric("hrv"),
            by_day={date(2026, 8, 3): 40.0, date(2026, 8, 1): 55.0, date(2026, 8, 2): 42.0},
        ),
    )

    result = server.get_metric_series("hrv", "2026-08-01", "2026-08-03")

    assert [p["date"] for p in result["data"]["points"]] == [
        "2026-08-01",
        "2026-08-02",
        "2026-08-03",
    ]


def test_daily_points_are_limited_to_the_requested_range(fake_context, monkeypatch):
    """The fetch reaches back for baseline history, but those lookback days are
    context for the mean - they must not appear as points the caller asked for."""
    from mcp_fitbit_air import server

    stub_fetch(
        monkeypatch,
        MetricSeries(
            metric=get_metric("hrv"),
            by_day={
                date(2026, 7, 20): 30.0,  # lookback only
                date(2026, 7, 21): 30.0,  # lookback only
                date(2026, 8, 1): 60.0,
            },
        ),
    )

    result = server.get_metric_series("hrv", "2026-08-01", "2026-08-01")

    assert [p["date"] for p in result["data"]["points"]] == ["2026-08-01"]
    # ...but all three values inform the baseline.
    assert result["data"]["baseline"]["n"] == 3
    assert result["data"]["baseline"]["mean"] == 40.0


def test_daily_fetch_reaches_back_for_baseline_history(fake_context, monkeypatch):
    from mcp_fitbit_air import server

    calls = []
    stub_fetch(
        monkeypatch,
        MetricSeries(metric=get_metric("hrv"), by_day={date(2026, 8, 1): 60.0}),
        record=calls,
    )

    server.get_metric_series("hrv", "2026-08-01", "2026-08-01")

    start, end = calls[0]
    assert start == date(2026, 8, 1) - timedelta(days=30)
    assert end == date(2026, 8, 1)


def test_lookback_respects_a_metrics_own_range_cap(fake_context, monkeypatch):
    """heart_rate caps at 14 days per request, not 90. A flat 30-day lookback
    would push the fetch past what the API accepts."""
    from mcp_fitbit_air import server

    calls = []
    stub_fetch(
        monkeypatch,
        MetricSeries(metric=get_metric("heart_rate"), by_day={date(2026, 8, 1): 60.0}),
        record=calls,
    )

    server.get_metric_series("heart_rate", "2026-08-01", "2026-08-01")

    start, end = calls[0]
    assert (end - start).days + 1 == get_metric("heart_rate").max_range_days


def test_baseline_window_days_reports_the_window_actually_used(fake_context, monkeypatch):
    from mcp_fitbit_air import server

    stub_fetch(
        monkeypatch,
        MetricSeries(metric=get_metric("heart_rate"), by_day={date(2026, 8, 1): 60.0}),
    )

    result = server.get_metric_series("heart_rate", "2026-08-01", "2026-08-01")

    assert result["data"]["baseline"]["window_days"] == 14


def test_daily_range_beyond_the_metric_cap_is_rejected(fake_context):
    """heart_rate allows 14 days; asking for a year must be a clear error rather
    than a silently truncated or API-rejected request."""
    from mcp_fitbit_air import server

    result = server.get_metric_series("heart_rate", "2026-01-01", "2026-12-31")

    assert result["state"] == "error"
    assert "14" in result["message"]


def test_unknown_metric_returns_error_listing_valid_names(fake_context):
    from mcp_fitbit_air import server

    result = server.get_metric_series("bogus", "2026-08-01")

    assert result["state"] == "error"
    assert "hrv" in result["message"]


def test_invalid_granularity_is_rejected(fake_context):
    from mcp_fitbit_air import server

    result = server.get_metric_series("hrv", "2026-08-01", granularity="hourly")

    assert result["state"] == "error"
    assert "daily" in result["message"]


def test_granularity_is_validated_before_any_credentials_are_needed(monkeypatch):
    """A typo in granularity should not fail with an auth error - the argument
    is checkable without ever touching the network."""
    from mcp_fitbit_air import server
    from mcp_fitbit_air.auth import AuthError

    def explode():
        raise AuthError("no token")

    monkeypatch.setattr(server, "get_context", explode)

    result = server.get_metric_series("hrv", "2026-08-01", granularity="hourly")

    assert result["state"] == "error"
    assert "daily" in result["message"]


def test_warming_up_state_is_propagated(fake_context, monkeypatch):
    from mcp_fitbit_air import server

    stub_fetch(
        monkeypatch,
        MetricSeries(
            metric=get_metric("hrv"),
            by_day={},
            state=ResultState.WARMING_UP,
            message="needs 3 nights",
        ),
    )

    result = server.get_metric_series("hrv", "2026-08-01", "2026-08-02")

    assert result["state"] == "warming_up"
    assert result["warmup_nights"] == 3


def test_no_data_state_is_propagated(fake_context, monkeypatch):
    from mcp_fitbit_air import server

    stub_fetch(
        monkeypatch,
        MetricSeries(
            metric=get_metric("steps"),
            by_day={},
            state=ResultState.NO_DATA,
            message="nothing recorded",
        ),
    )

    result = server.get_metric_series("steps", "2026-08-01", "2026-08-02")

    assert result["state"] == "no_data"
    assert "nothing recorded" in result["message"]


def test_fetch_error_becomes_an_error_result(fake_context, monkeypatch):
    from mcp_fitbit_air import server

    stub_fetch(
        monkeypatch,
        MetricSeries(
            metric=get_metric("hrv"), by_day={}, state=ResultState.ERROR, message="boom"
        ),
    )

    result = server.get_metric_series("hrv", "2026-08-01", "2026-08-02")

    assert result["state"] == "error"
    assert "boom" in result["message"]


# -- Intraday granularity ----------------------------------------------------


def test_intraday_on_unsupported_metric_is_an_error(fake_context):
    from mcp_fitbit_air import server

    result = server.get_metric_series("hrv", "2026-08-01", granularity="intraday")

    assert result["state"] == "error"
    assert "intraday" in result["message"].lower()
    # The error should name what *does* work, not just what does not.
    assert "heart_rate" in result["message"]


def test_intraday_aggregates_a_rate_into_min_max_avg_buckets(fake_context):
    """Both readings fall in the same 5-minute bucket, so they collapse into one
    entry. Summing a bpm would be meaningless, hence min/max/avg."""
    from mcp_fitbit_air import server

    fake_context.client.list_data_points.return_value = [
        hr_point("62", "2026-08-01T09:00:00Z"),
        hr_point("71", "2026-08-01T09:01:00Z"),
    ]

    result = server.get_metric_series(
        "heart_rate", "2026-08-01", "2026-08-01", granularity="intraday"
    )

    assert result["state"] == "ok"
    assert result["data"]["granularity"] == "intraday"
    assert result["data"]["aggregation"] == "min/max/avg"
    assert result["data"]["readings"] == [
        {"time": "2026-08-01T09:00:00+00:00", "min": 62.0, "max": 71.0, "avg": 66.5, "n": 2}
    ]


def test_intraday_aggregation_collapses_a_realistic_sample_rate(fake_context):
    """The defect this replaced: the band samples every ~2 seconds, so one live
    day returned 38,093 raw readings (1.87 MB of JSON). Bucketing must reduce
    that by orders of magnitude, not merely relabel it."""
    from mcp_fitbit_air import server

    # 20 minutes of 2-second sampling.
    base = dt.datetime(2026, 8, 1, 9, 0, tzinfo=dt.timezone.utc)
    fake_context.client.list_data_points.return_value = [
        hr_point("60", (base + dt.timedelta(seconds=2 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        for i in range(600)
    ]

    result = server.get_metric_series(
        "heart_rate", "2026-08-01", "2026-08-01", granularity="intraday"
    )

    readings = result["data"]["readings"]
    assert len(readings) == 4  # 20 minutes / 5-minute buckets
    assert sum(r["n"] for r in readings) == 600


def test_intraday_bucket_count_stays_bounded_across_the_whole_cap(fake_context):
    """Whatever range is asked for, the response must stay small enough to be
    usable. Without an adaptive bucket, 7 days at 5 minutes is 2,016 entries."""
    from mcp_fitbit_air import server

    fake_context.client.list_data_points.return_value = [
        hr_point("62", "2026-08-01T09:00:00Z")
    ]

    for start in ("2026-08-01", "2026-07-30", "2026-07-26"):
        result = server.get_metric_series(
            "heart_rate", start, "2026-08-01", granularity="intraday"
        )
        span_minutes = (
            (date.fromisoformat("2026-08-01") - date.fromisoformat(start)).days + 1
        ) * 24 * 60
        assert span_minutes / result["data"]["bucket_minutes"] <= server.MAX_INTRADAY_BUCKETS


def test_intraday_bucket_width_widens_with_the_range(fake_context):
    from mcp_fitbit_air import server

    fake_context.client.list_data_points.return_value = [
        hr_point("62", "2026-08-01T09:00:00Z")
    ]

    one_day = server.get_metric_series(
        "heart_rate", "2026-08-01", "2026-08-01", granularity="intraday"
    )
    seven_days = server.get_metric_series(
        "heart_rate", "2026-07-26", "2026-08-01", granularity="intraday"
    )

    assert one_day["data"]["bucket_minutes"] == 5
    assert seven_days["data"]["bucket_minutes"] > one_day["data"]["bucket_minutes"]


def test_intraday_bucket_time_is_floored_to_the_boundary(fake_context):
    """A bucket labelled with the first reading's time rather than the boundary
    would make buckets look irregularly spaced."""
    from mcp_fitbit_air import server

    fake_context.client.list_data_points.return_value = [
        hr_point("62", "2026-08-01T09:07:43Z")
    ]

    result = server.get_metric_series(
        "heart_rate", "2026-08-01", "2026-08-01", granularity="intraday"
    )

    assert result["data"]["readings"][0]["time"] == "2026-08-01T09:05:00+00:00"


def test_intraday_readings_with_no_resolvable_time_are_counted_not_dropped(fake_context):
    """Silently dropping them would hide a shape change in the API behind data
    that merely looks thinner."""
    from mcp_fitbit_air import server

    fake_context.client.list_data_points.return_value = [
        hr_point("62", "2026-08-01T09:00:00Z"),
        {"heartRate": {"beatsPerMinute": "70"}},  # no sampleTime at all
    ]

    result = server.get_metric_series(
        "heart_rate", "2026-08-01", "2026-08-01", granularity="intraday"
    )

    assert result["data"]["unplaced_readings"] == 1
    assert len(result["data"]["readings"]) == 1


def test_intraday_omits_unplaced_count_when_everything_placed(fake_context):
    from mcp_fitbit_air import server

    fake_context.client.list_data_points.return_value = [
        hr_point("62", "2026-08-01T09:00:00Z")
    ]

    result = server.get_metric_series(
        "heart_rate", "2026-08-01", "2026-08-01", granularity="intraday"
    )

    assert "unplaced_readings" not in result["data"]


def test_intraday_readings_are_sorted_by_time(fake_context):
    """The API does not promise ordering, and an out-of-order series reads as
    noise to anyone plotting or summarising it."""
    from mcp_fitbit_air import server

    fake_context.client.list_data_points.return_value = [
        hr_point("71", "2026-08-01T09:05:00Z"),
        hr_point("62", "2026-08-01T09:00:00Z"),
    ]

    result = server.get_metric_series(
        "heart_rate", "2026-08-01", "2026-08-01", granularity="intraday"
    )

    assert [r["time"] for r in result["data"]["readings"]] == [
        "2026-08-01T09:00:00+00:00",
        "2026-08-01T09:05:00+00:00",
    ]


def test_intraday_steps_read_their_time_from_the_interval(fake_context):
    """steps carries its timestamp under steps.interval.startTime, while
    heart_rate uses heartRate.sampleTime.physicalTime - the two supported
    intraday metrics do not share a shape."""
    from mcp_fitbit_air import server

    fake_context.client.list_data_points.return_value = [
        step_point("120", "2026-08-01T09:00:00Z")
    ]

    result = server.get_metric_series(
        "steps", "2026-08-01", "2026-08-01", granularity="intraday"
    )

    assert result["data"]["readings"] == [
        {"time": "2026-08-01T09:00:00+00:00", "total": 120.0, "n": 1}
    ]


def test_intraday_sums_an_additive_metric_within_a_bucket(fake_context):
    """Steps are counts: two minutes of walking in one bucket is their sum, not
    their average. Reporting avg here would understate activity threefold."""
    from mcp_fitbit_air import server

    fake_context.client.list_data_points.return_value = [
        step_point("100", "2026-08-01T09:00:00Z"),
        step_point("50", "2026-08-01T09:01:00Z"),
        step_point("30", "2026-08-01T09:02:00Z"),
    ]

    result = server.get_metric_series(
        "steps", "2026-08-01", "2026-08-01", granularity="intraday"
    )

    assert result["data"]["aggregation"] == "total"
    assert result["data"]["readings"] == [
        {"time": "2026-08-01T09:00:00+00:00", "total": 180.0, "n": 3}
    ]


def test_intraday_range_is_truncated_to_seven_days(fake_context):
    from mcp_fitbit_air import server

    fake_context.client.list_data_points.return_value = [
        hr_point("62", "2026-08-01T09:00:00Z")
    ]

    result = server.get_metric_series(
        "heart_rate", "2026-07-01", "2026-08-01", granularity="intraday"
    )

    assert result["truncated"] is True
    assert "7" in result["reason"]
    # Truncation keeps the most recent window, not the oldest.
    assert result["data"]["range"] == {"start": "2026-07-26", "end": "2026-08-01"}


def test_intraday_within_the_cap_is_not_flagged_as_truncated(fake_context):
    from mcp_fitbit_air import server

    fake_context.client.list_data_points.return_value = [
        hr_point("62", "2026-08-01T09:00:00Z")
    ]

    result = server.get_metric_series(
        "heart_rate", "2026-07-30", "2026-08-01", granularity="intraday"
    )

    assert "truncated" not in result


def test_intraday_at_exactly_the_cap_is_not_truncated(fake_context):
    """The boundary case: 7 days is allowed in full. Testing only a short range
    cannot tell `> MAX_INTRADAY_DAYS` from `>=`, and the off-by-one would quietly
    drop a day off every week-long request."""
    from mcp_fitbit_air import server

    fake_context.client.list_data_points.return_value = [
        hr_point("62", "2026-08-01T09:00:00Z")
    ]

    result = server.get_metric_series(
        "heart_rate", "2026-07-26", "2026-08-01", granularity="intraday"
    )

    assert "truncated" not in result
    assert result["data"]["range"] == {"start": "2026-07-26", "end": "2026-08-01"}


def test_intraday_one_day_over_the_cap_is_truncated(fake_context):
    """The other side of the same boundary."""
    from mcp_fitbit_air import server

    fake_context.client.list_data_points.return_value = [
        hr_point("62", "2026-08-01T09:00:00Z")
    ]

    result = server.get_metric_series(
        "heart_rate", "2026-07-25", "2026-08-01", granularity="intraday"
    )

    assert result["truncated"] is True


def test_intraday_uses_the_physical_time_filter_dialect(fake_context):
    """The wrong filter dialect returns HTTP 200 with zero rows - a silent
    failure that looks exactly like "no data"."""
    from mcp_fitbit_air import server

    fake_context.client.list_data_points.return_value = []

    server.get_metric_series(
        "heart_rate", "2026-08-01", "2026-08-01", granularity="intraday"
    )

    _, kwargs = fake_context.client.list_data_points.call_args
    assert kwargs["filter_expr"] == (
        'heart_rate.sample_time.physical_time >= "2026-08-01T00:00:00Z" AND '
        'heart_rate.sample_time.physical_time < "2026-08-02T00:00:00Z"'
    )


def test_intraday_with_no_readings_is_no_data_not_an_error(fake_context):
    from mcp_fitbit_air import server

    fake_context.client.list_data_points.return_value = []

    result = server.get_metric_series(
        "heart_rate", "2026-08-01", "2026-08-01", granularity="intraday"
    )

    assert result["state"] == "no_data"


def test_intraday_no_data_still_reports_truncation(fake_context):
    """Zero readings over a truncated window is ambiguous otherwise: the caller
    cannot tell whether the missing days were never fetched."""
    from mcp_fitbit_air import server

    fake_context.client.list_data_points.return_value = []

    result = server.get_metric_series(
        "heart_rate", "2026-07-01", "2026-08-01", granularity="intraday"
    )

    assert result["state"] == "no_data"
    assert result["truncated"] is True


# -- Page-cap truncation -----------------------------------------------------
#
# Verified live: two days of heart_rate returns 71,997 of ~76,186 points because
# pagination stops at MAX_PAGES=50. Before this, the tool reported state="ok"
# with no hint, and the only warning went to stderr where Claude cannot see it -
# so an incomplete series would be analysed as though it were whole.


def test_intraday_surfaces_page_cap_truncation(fake_context):
    from mcp_fitbit_air import server
    from mcp_fitbit_air.client import DataPoints

    fake_context.client.list_data_points.return_value = DataPoints(
        [hr_point("62", "2026-08-01T09:00:00Z")],
        truncated=True,
        truncation_reason="Reached the 50-page fetch limit with 72000 record(s).",
    )

    result = server.get_metric_series(
        "heart_rate", "2026-08-01", "2026-08-01", granularity="intraday"
    )

    assert result["state"] == "ok"
    assert result["truncated"] is True
    assert "50-page" in result["reason"]


def test_intraday_reports_both_truncation_causes_together(fake_context):
    """A request can be cut twice over - once by the 7-day cap, once by the page
    limit. Reporting only one would understate how partial the answer is."""
    from mcp_fitbit_air import server
    from mcp_fitbit_air.client import DataPoints

    fake_context.client.list_data_points.return_value = DataPoints(
        [hr_point("62", "2026-08-01T09:00:00Z")],
        truncated=True,
        truncation_reason="Reached the 50-page fetch limit.",
    )

    result = server.get_metric_series(
        "heart_rate", "2026-07-01", "2026-08-01", granularity="intraday"
    )

    assert result["truncated"] is True
    assert "7 days" in result["reason"]
    assert "50-page" in result["reason"]


def test_daily_surfaces_page_cap_truncation(fake_context, monkeypatch):
    from mcp_fitbit_air import server

    stub_fetch(
        monkeypatch,
        MetricSeries(
            metric=get_metric("hrv"),
            by_day={date(2026, 8, 1): 55.0},
            truncated=True,
            truncation_reason="Reached the 50-page fetch limit.",
        ),
    )

    result = server.get_metric_series("hrv", "2026-08-01", "2026-08-01")

    assert result["state"] == "ok"
    assert result["truncated"] is True
    assert "50-page" in result["reason"]


def test_daily_stays_quiet_when_the_fetch_was_complete(fake_context, monkeypatch):
    from mcp_fitbit_air import server

    stub_fetch(
        monkeypatch,
        MetricSeries(metric=get_metric("hrv"), by_day={date(2026, 8, 1): 55.0}),
    )

    result = server.get_metric_series("hrv", "2026-08-01", "2026-08-01")

    assert "truncated" not in result


def test_intraday_result_is_json_serialisable_with_nan_rejected(fake_context):
    from mcp_fitbit_air import server

    fake_context.client.list_data_points.return_value = [
        hr_point("62", "2026-08-01T09:00:00Z"),
        # A reading the API could not compute; it must be dropped, not carried
        # through as a bare NaN that invalidates the whole stdio stream.
        hr_point("NaN", "2026-08-01T09:01:00Z"),
    ]

    result = server.get_metric_series(
        "heart_rate", "2026-08-01", "2026-08-01", granularity="intraday"
    )

    json.dumps(result, allow_nan=False)
    assert len(result["data"]["readings"]) == 1


# -- SDK-boundary tests ------------------------------------------------------


def test_call_tool_through_sdk_boundary_returns_ok(fake_context, monkeypatch):
    import mcp_fitbit_air.server as server

    stub_fetch(
        monkeypatch,
        MetricSeries(metric=get_metric("hrv"), by_day={date(2026, 8, 1): 55.0}),
    )

    result = asyncio.run(
        server.mcp.call_tool(
            "get_metric_series",
            {"metric": "hrv", "start_date": "2026-08-01", "end_date": "2026-08-01"},
        )
    )

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["state"] == "ok"
    assert payload["data"]["points"][0]["value"] == 55.0


def test_call_tool_input_schema_exposes_every_argument():
    import mcp_fitbit_air.server as server

    tools = asyncio.run(server.mcp.list_tools())
    tool = next(t for t in tools if t.name == "get_metric_series")

    schema = tool.input_schema or {}
    assert set(schema.get("properties", {})) >= {
        "metric",
        "start_date",
        "end_date",
        "granularity",
    }
    # Only the two arguments without defaults may be required.
    assert schema.get("required") == ["metric", "start_date"]


def test_call_tool_output_schema_does_not_require_meta():
    import mcp_fitbit_air.server as server

    tools = asyncio.run(server.mcp.list_tools())
    tool = next(t for t in tools if t.name == "get_metric_series")

    assert "meta" not in (tool.output_schema or {}).get("required", [])


def test_call_tool_intraday_through_sdk_boundary(fake_context):
    import mcp_fitbit_air.server as server

    fake_context.client.list_data_points.return_value = [
        hr_point("62", "2026-08-01T09:00:00Z")
    ]

    result = asyncio.run(
        server.mcp.call_tool(
            "get_metric_series",
            {
                "metric": "heart_rate",
                "start_date": "2026-08-01",
                "end_date": "2026-08-01",
                "granularity": "intraday",
            },
        )
    )

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["data"]["readings"][0]["min"] == 62.0
    assert payload["data"]["bucket_minutes"] == 5


# -- Empty ranges, and baselines that are not really trailing ------------------
#
# Both found in review.


def test_values_only_in_the_lookback_are_not_reported_as_ok(fake_context, monkeypatch):
    """The state was chosen from series.by_day, which includes the lookback days
    the baseline needs, while points were filtered to the requested range. A
    metric with values only before the range therefore returned state ok with an
    empty list - exactly the collapse results.py exists to prevent."""
    from mcp_fitbit_air.server import get_metric_series

    series = MetricSeries(metric=get_metric("hrv"), by_day={date(2026, 6, 1): 55.0})
    stub_fetch(monkeypatch, series)

    result = get_metric_series("hrv", "2026-07-01", "2026-07-05")

    assert result["state"] == "no_data"
    assert "2026-07-01" in result["message"]


def test_a_value_inside_the_range_is_still_ok(fake_context, monkeypatch):
    from mcp_fitbit_air.server import get_metric_series

    series = MetricSeries(
        metric=get_metric("hrv"),
        by_day={date(2026, 6, 1): 55.0, date(2026, 7, 3): 61.0},
    )
    stub_fetch(monkeypatch, series)

    result = get_metric_series("hrv", "2026-07-01", "2026-07-05")

    assert result["state"] == "ok"
    assert [p["date"] for p in result["data"]["points"]] == ["2026-07-03"]


def test_baseline_window_reports_how_many_days_actually_precede_the_range(
    fake_context, monkeypatch
):
    from mcp_fitbit_air.server import get_metric_series

    series = MetricSeries(metric=get_metric("hrv"), by_day={date(2026, 7, 3): 61.0})
    stub_fetch(monkeypatch, series)

    result = get_metric_series("hrv", "2026-07-01", "2026-07-05")

    assert result["data"]["baseline_window"]["trailing_days"] == 30


def test_a_baseline_with_no_trailing_days_says_it_is_not_a_comparison(
    fake_context, monkeypatch
):
    """heart_rate caps at 14 days, so a 14-day request leaves no room to reach
    back and the "baseline" becomes the mean of the very days being displayed.
    The number is still useful; presenting it as an independent norm is not."""
    from mcp_fitbit_air.server import get_metric_series

    series = MetricSeries(
        metric=get_metric("heart_rate"),
        by_day={date(2026, 7, 1) + timedelta(days=n): 60.0 + n for n in range(14)},
    )
    stub_fetch(monkeypatch, series)

    result = get_metric_series("heart_rate", "2026-07-01", "2026-07-14")

    window = result["data"]["baseline_window"]
    assert window["trailing_days"] == 0
    assert "note" in window
    assert "same days" in window["note"]


# -- Intraday timestamps must be readable against the range they came with ----
#
# Found in review, confirmed against live data: a request for 2026-09-02 in
# America/New_York returned buckets labelled 2026-09-02T04:00:00Z through
# 2026-09-03T03:55:00Z. The instants were correct but expressed in UTC while
# `range` was local calendar dates, and the payload never named the timezone -
# so nothing in the response could convert one to the other, and the last bucket
# appeared to fall on a day that was never asked for.

NY = ZoneInfo("America/New_York")


@pytest.fixture
def ny_context(monkeypatch):
    ctx = Mock()
    ctx.timezone = NY
    monkeypatch.setattr("mcp_fitbit_air.server.get_context", lambda: ctx)
    return ctx


def test_intraday_buckets_are_labelled_in_local_time(ny_context):
    from mcp_fitbit_air.server import get_metric_series

    # 01:54Z on the 3rd is 21:54 local on the 2nd - the day that was requested.
    ny_context.client.list_data_points.return_value = [
        hr_point("70", "2026-09-03T01:54:00Z")
    ]

    data = get_metric_series("heart_rate", "2026-09-02", granularity="intraday")["data"]

    assert data["readings"][0]["time"].startswith("2026-09-02T21:50")
    assert data["timezone"] == "America/New_York"


def test_no_intraday_bucket_falls_outside_the_requested_local_day(ny_context):
    from mcp_fitbit_air.server import get_metric_series

    ny_context.client.list_data_points.return_value = [
        hr_point("60", "2026-09-02T04:00:00Z"),   # 00:00 local
        hr_point("80", "2026-09-03T03:55:00Z"),   # 23:55 local, same local day
    ]

    data = get_metric_series("heart_rate", "2026-09-02", granularity="intraday")["data"]

    days = {reading["time"][:10] for reading in data["readings"]}
    assert days == {"2026-09-02"}


def test_buckets_are_ordered_by_time_not_by_string(ny_context):
    from mcp_fitbit_air.server import get_metric_series

    ny_context.client.list_data_points.return_value = [
        hr_point("80", "2026-09-03T03:55:00Z"),
        hr_point("60", "2026-09-02T04:00:00Z"),
    ]

    data = get_metric_series("heart_rate", "2026-09-02", granularity="intraday")["data"]

    times = [reading["time"] for reading in data["readings"]]
    assert times == sorted(times, key=dt.datetime.fromisoformat)
    assert times[0] < times[1]


def test_readings_with_no_usable_timestamp_are_reported_even_when_none_land(
    ny_context,
):
    """The unplaced counter exists so that a shape change surfaces as a number
    rather than as quietly missing data - but it was dropped on the very path
    that a total shape change produces."""
    from mcp_fitbit_air.server import get_metric_series

    ny_context.client.list_data_points.return_value = [
        {"heartRate": {"beatsPerMinute": "70", "sampleTime": {"renamedField": "x"}}}
        for _ in range(3)
    ]

    result = get_metric_series("heart_rate", "2026-09-02", granularity="intraday")

    assert result["state"] == "no_data"
    assert result["unplaced_readings"] == 3
    assert "timestamp" in result["message"]


def test_metric_series_reports_warming_up_through_its_widened_window(fake_context):
    """get_metric_series widens the fetch window for the baseline exactly as
    build_summary does, so it needs the same protection: warm-up is judged by
    the range the caller asked about."""
    from mcp_fitbit_air.server import get_metric_series

    fake_context.client.list_data_points.return_value = []

    result = get_metric_series("hrv", "2026-08-01", "2026-08-03")

    assert result["state"] == "warming_up"


# -- Truncation must survive every exit from the daily path -------------------
#
# Found by cloud review of the empty-range guard. The meta block was assembled
# below the early returns, so a fetch that was cut short reported its result
# authoritatively with no signal that it had been cut short - the exact collapse
# DataPoints.truncated was added to prevent.


def test_empty_range_no_data_still_reports_a_truncated_fetch(fake_context, monkeypatch):
    from mcp_fitbit_air.server import get_metric_series

    series = MetricSeries(
        metric=get_metric("hrv"),
        by_day={date(2026, 6, 15): 55.0},   # lookback only
        truncated=True,
        truncation_reason="The API repeated the same page token.",
    )
    stub_fetch(monkeypatch, series)

    result = get_metric_series("hrv", "2026-07-01", "2026-07-05")

    assert result["state"] == "no_data"
    assert result["truncated"] is True
    assert "page token" in result["reason"]


def test_a_wholly_empty_truncated_fetch_also_reports_truncation(
    fake_context, monkeypatch
):
    """The likelier trigger: the fetch is cut short before it reaches the range
    at all, so the series is empty and no_data - which reads as "you have no
    data" rather than "we stopped looking"."""
    from mcp_fitbit_air.server import get_metric_series

    series = MetricSeries(
        metric=get_metric("hrv"),
        by_day={},
        state=ResultState.NO_DATA,
        message="No hrv recorded.",
        truncated=True,
        truncation_reason="Reached the 50-page fetch limit.",
    )
    stub_fetch(monkeypatch, series)

    result = get_metric_series("hrv", "2026-07-01", "2026-07-05")

    assert result["state"] == "no_data"
    assert result["truncated"] is True


def test_warming_up_also_carries_truncation(fake_context, monkeypatch):
    from mcp_fitbit_air.server import get_metric_series

    series = MetricSeries(
        metric=get_metric("hrv"),
        by_day={},
        state=ResultState.WARMING_UP,
        message="hrv needs about 3 nights.",
        truncated=True,
        truncation_reason="Reached the 50-page fetch limit.",
    )
    stub_fetch(monkeypatch, series)

    result = get_metric_series("hrv", "2026-07-01", "2026-07-03")

    assert result["state"] == "warming_up"
    assert result["truncated"] is True
