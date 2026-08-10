import asyncio
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

    def fake(client, name, start, end, tz):
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


def test_intraday_returns_readings_with_times_and_values(fake_context):
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
    assert result["data"]["readings"] == [
        {"time": "2026-08-01T09:00:00Z", "value": 62.0},
        {"time": "2026-08-01T09:01:00Z", "value": 71.0},
    ]


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
        "2026-08-01T09:00:00Z",
        "2026-08-01T09:05:00Z",
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
        {"time": "2026-08-01T09:00:00Z", "value": 120.0}
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
    assert payload["data"]["readings"][0]["value"] == 62.0
