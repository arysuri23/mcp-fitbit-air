import asyncio
import json
from datetime import date, timedelta
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest

from mcp_fitbit_air.fetch import MetricSeries
from mcp_fitbit_air.mapping import get_metric
from mcp_fitbit_air.results import ResultState
from mcp_fitbit_air.summary import (
    BASELINE_LOOKBACK_DAYS,
    MAX_SUMMARY_DAYS,
    build_summary,
)

TZ = ZoneInfo("UTC")


def series(name, by_day, state=ResultState.OK, message=None):
    return MetricSeries(metric=get_metric(name), by_day=by_day, state=state, message=message)


def fake_fetch(fake):
    """Stand in for fetch_metric, ignoring the range it is handed."""
    return lambda client, name, start, end, tz: fake[name]


def test_summary_has_one_row_per_day_in_range():
    fake = {"steps": series("steps", {date(2026, 8, 1): 9000, date(2026, 8, 2): 11000})}
    with patch("mcp_fitbit_air.summary.fetch_metric", side_effect=fake_fetch(fake)):
        result = build_summary(Mock(), date(2026, 8, 1), date(2026, 8, 3), ["steps"], TZ)

    assert [row["date"] for row in result["days"]] == [
        "2026-08-01",
        "2026-08-02",
        "2026-08-03",
    ]


def test_values_carry_units_and_baselines():
    fake = {"steps": series("steps", {date(2026, 8, 1): 9000, date(2026, 8, 2): 11000})}
    with patch("mcp_fitbit_air.summary.fetch_metric", side_effect=fake_fetch(fake)):
        result = build_summary(Mock(), date(2026, 8, 1), date(2026, 8, 2), ["steps"], TZ)

    cell = result["days"][0]["metrics"]["steps"]
    assert cell["value"] == 9000
    assert cell["unit"] == "count"
    assert cell["baseline"]["mean"] == 10000.0
    assert cell["baseline"]["n"] == 2


def test_day_with_no_value_is_marked_no_data_not_zero():
    fake = {"steps": series("steps", {date(2026, 8, 1): 9000})}
    with patch("mcp_fitbit_air.summary.fetch_metric", side_effect=fake_fetch(fake)):
        result = build_summary(Mock(), date(2026, 8, 1), date(2026, 8, 2), ["steps"], TZ)

    cell = result["days"][1]["metrics"]["steps"]
    assert cell["state"] == "no_data"
    assert "value" not in cell


def test_one_failing_metric_does_not_sink_the_others():
    fake = {
        "steps": series("steps", {date(2026, 8, 1): 9000}),
        "hrv": series("hrv", {}, state=ResultState.ERROR, message="boom"),
    }
    with patch("mcp_fitbit_air.summary.fetch_metric", side_effect=fake_fetch(fake)):
        result = build_summary(Mock(), date(2026, 8, 1), date(2026, 8, 1), ["steps", "hrv"], TZ)

    metrics = result["days"][0]["metrics"]
    assert metrics["steps"]["value"] == 9000
    assert metrics["hrv"]["state"] == "error"


def test_warming_up_metric_is_reported_per_day():
    fake = {"hrv": series("hrv", {}, state=ResultState.WARMING_UP, message="needs 3 nights")}
    with patch("mcp_fitbit_air.summary.fetch_metric", side_effect=fake_fetch(fake)):
        result = build_summary(Mock(), date(2026, 8, 1), date(2026, 8, 1), ["hrv"], TZ)

    cell = result["days"][0]["metrics"]["hrv"]
    assert cell["state"] == "warming_up"
    assert "3 nights" in cell["message"]


def test_metric_level_problems_are_summarised_at_the_top():
    fake = {
        "steps": series("steps", {date(2026, 8, 1): 9000}),
        "hrv": series("hrv", {}, state=ResultState.ERROR, message="boom"),
    }
    with patch("mcp_fitbit_air.summary.fetch_metric", side_effect=fake_fetch(fake)):
        result = build_summary(Mock(), date(2026, 8, 1), date(2026, 8, 1), ["steps", "hrv"], TZ)

    assert result["metric_status"]["hrv"]["state"] == "error"
    assert result["metric_status"]["steps"]["state"] == "ok"


def test_range_longer_than_the_cap_is_rejected():
    with pytest.raises(ValueError) as exc:
        build_summary(Mock(), date(2026, 1, 1), date(2026, 12, 31), ["steps"], TZ)
    assert str(MAX_SUMMARY_DAYS) in str(exc.value)


def test_inverted_range_is_rejected():
    """end before start would otherwise produce a negative span, sail past the
    cap check, and emit zero rows with no explanation."""
    with pytest.raises(ValueError):
        build_summary(Mock(), date(2026, 8, 5), date(2026, 8, 1), ["steps"], TZ)


# -- Baseline lookback -------------------------------------------------------
#
# The baseline is meant to be a TRAILING one: "9,000 steps against a 30-day
# mean of 11,200" is a finding, while "9,000 steps against a mean of the same
# three days you asked about" is close to circular. So the fetch window is
# widened backwards past `start`, and only the requested days are emitted as
# rows. Nothing about this is visible in the returned rows, which is exactly
# why it needs its own tests.


def test_baseline_draws_on_days_before_the_requested_range():
    """The mean must include the lookback days, not just the emitted rows."""
    calls = []

    def fetch(client, name, start, end, tz):
        calls.append((start, end))
        return series(
            "steps",
            {
                date(2026, 7, 30): 2000,  # before the requested range
                date(2026, 7, 31): 4000,  # before the requested range
                date(2026, 8, 1): 12000,
            },
        )

    with patch("mcp_fitbit_air.summary.fetch_metric", side_effect=fetch):
        result = build_summary(Mock(), date(2026, 8, 1), date(2026, 8, 1), ["steps"], TZ)

    assert len(result["days"]) == 1
    baseline = result["days"][0]["metrics"]["steps"]["baseline"]
    assert baseline["n"] == 3
    assert baseline["mean"] == 6000.0


def test_fetch_window_is_widened_backwards_by_the_lookback():
    calls = []

    def fetch(client, name, start, end, tz):
        calls.append((start, end))
        return series("steps", {})

    with patch("mcp_fitbit_air.summary.fetch_metric", side_effect=fetch):
        build_summary(Mock(), date(2026, 8, 1), date(2026, 8, 3), ["steps"], TZ)

    fetch_start, fetch_end = calls[0]
    assert fetch_start == date(2026, 8, 1) - timedelta(days=BASELINE_LOOKBACK_DAYS)
    assert fetch_end == date(2026, 8, 3)


def test_lookback_shrinks_so_the_fetch_never_exceeds_the_api_cap():
    """steps caps at 90 days per request. An 85-day summary leaves room for only
    5 lookback days, not 30 - overshooting would make the API reject the call."""
    calls = []

    def fetch(client, name, start, end, tz):
        calls.append((start, end))
        return series("steps", {})

    start, end = date(2026, 1, 1), date(2026, 3, 26)  # 85 days inclusive
    with patch("mcp_fitbit_air.summary.fetch_metric", side_effect=fetch):
        build_summary(Mock(), start, end, ["steps"], TZ)

    fetch_start, fetch_end = calls[0]
    span = (fetch_end - fetch_start).days + 1
    assert span == get_metric("steps").max_range_days


def test_baseline_window_days_reports_the_window_actually_used():
    """Reporting a flat 30 here would be a lie whenever the lookback was
    truncated, and Claude reads window_days as the baseline's authority."""
    fake = {"steps": series("steps", {date(2026, 1, 1): 9000})}
    start, end = date(2026, 1, 1), date(2026, 3, 26)  # 85 days, lookback truncated to 5
    with patch("mcp_fitbit_air.summary.fetch_metric", side_effect=fake_fetch(fake)):
        result = build_summary(Mock(), start, end, ["steps"], TZ)

    baseline = result["days"][0]["metrics"]["steps"]["baseline"]
    assert baseline["window_days"] == get_metric("steps").max_range_days


def test_summary_is_json_serialisable_with_nan_rejected():
    """Non-finite values invalidate the whole MCP stdio stream, so the summary
    must survive a strict dump. mapping and baselines both filter, but this is
    the layer that assembles their output."""
    fake = {"steps": series("steps", {date(2026, 8, 1): 9000})}
    with patch("mcp_fitbit_air.summary.fetch_metric", side_effect=fake_fetch(fake)):
        result = build_summary(Mock(), date(2026, 8, 1), date(2026, 8, 2), ["steps"], TZ)

    json.dumps(result, allow_nan=False)


# -- The tool ----------------------------------------------------------------


@pytest.fixture
def fake_context(monkeypatch):
    ctx = Mock()
    ctx.timezone = TZ
    monkeypatch.setattr("mcp_fitbit_air.server.get_context", lambda: ctx)
    return ctx


def test_tool_returns_ok_for_a_range_with_data(fake_context):
    from mcp_fitbit_air.server import get_daily_summary

    fake = {name: series(name, {}) for name in ["sleep_duration", "resting_heart_rate", "hrv", "spo2", "skin_temperature_deviation", "active_zone_minutes"]}
    fake["steps"] = series("steps", {date(2026, 8, 1): 9000})
    with patch("mcp_fitbit_air.summary.fetch_metric", side_effect=fake_fetch(fake)):
        result = get_daily_summary("2026-08-01", "2026-08-01")

    assert result["state"] == "ok"
    assert result["data"]["days"][0]["metrics"]["steps"]["value"] == 9000


def test_tool_reports_no_data_when_every_metric_is_empty(fake_context):
    """A brand-new band with nothing synced is a legitimate answer, not an
    error, and the message should point at the tool that explains why."""
    from mcp_fitbit_air.server import get_daily_summary

    fake = {name: series(name, {}, state=ResultState.NO_DATA) for name in _all_summary_names()}
    with patch("mcp_fitbit_air.summary.fetch_metric", side_effect=fake_fetch(fake)):
        result = get_daily_summary("2026-08-01", "2026-08-02")

    assert result["state"] == "no_data"
    assert "get_profile_and_devices" in result["message"]
    # The empty table still rides along, so Claude can see which days were asked for.
    assert len(result["days"]) == 2


def test_tool_turns_an_oversized_range_into_an_error_result(fake_context):
    from mcp_fitbit_air.server import get_daily_summary

    result = get_daily_summary("2026-01-01", "2026-12-31")

    assert result["state"] == "error"
    assert str(MAX_SUMMARY_DAYS) in result["message"]


def test_tool_rejects_an_unparseable_date(fake_context):
    from mcp_fitbit_air.server import get_daily_summary

    result = get_daily_summary("whenever")

    assert result["state"] == "error"
    assert "whenever" in result["message"]


def _all_summary_names():
    from mcp_fitbit_air.mapping import SUMMARY_METRICS

    return SUMMARY_METRICS


# -- SDK-boundary tests ------------------------------------------------------
#
# Same rationale as tests/test_server_profile.py: calling the tool as a plain
# Python function never touches the SDK's signature/schema machinery, which is
# how every tool once failed output validation while the unit tests passed.


def test_call_tool_through_sdk_boundary_returns_ok(fake_context):
    import mcp_fitbit_air.server as server

    fake = {name: series(name, {}) for name in _all_summary_names()}
    fake["steps"] = series("steps", {date(2026, 8, 1): 9000})

    with patch("mcp_fitbit_air.summary.fetch_metric", side_effect=fake_fetch(fake)):
        result = asyncio.run(
            server.mcp.call_tool(
                "get_daily_summary", {"start_date": "2026-08-01", "end_date": "2026-08-01"}
            )
        )

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["state"] == "ok"
    assert payload["data"]["days"][0]["metrics"]["steps"]["value"] == 9000


def test_call_tool_input_schema_exposes_both_date_arguments():
    """tool_guard copies the wrapped function's parameters into the wrapper's
    __signature__ precisely so arguments survive into the input schema. If that
    ever regresses, Claude can no longer pass a date range at all."""
    import mcp_fitbit_air.server as server

    tools = asyncio.run(server.mcp.list_tools())
    tool = next(t for t in tools if t.name == "get_daily_summary")

    properties = (tool.input_schema or {}).get("properties", {})
    assert "start_date" in properties
    assert "end_date" in properties
    assert (tool.input_schema or {}).get("required") == ["start_date"]


def test_call_tool_output_schema_does_not_require_meta():
    import mcp_fitbit_air.server as server

    tools = asyncio.run(server.mcp.list_tools())
    tool = next(t for t in tools if t.name == "get_daily_summary")

    assert "meta" not in (tool.output_schema or {}).get("required", [])


def test_call_tool_with_only_a_relative_start_date(fake_context):
    """"last week" is a whole-range expression: it must work as start_date on
    its own, through the SDK, with end_date omitted entirely."""
    import mcp_fitbit_air.server as server

    fake = {name: series(name, {}) for name in _all_summary_names()}

    with patch("mcp_fitbit_air.summary.fetch_metric", side_effect=fake_fetch(fake)):
        result = asyncio.run(
            server.mcp.call_tool("get_daily_summary", {"start_date": "last week"})
        )

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["state"] in {"ok", "no_data"}


def test_metric_status_reports_a_truncated_fetch(fake_context):
    """A summary row that silently rests on a partially fetched series would let
    Claude state a baseline as settled when it was computed from a hole."""
    fake = {name: series(name, {}) for name in _all_summary_names()}
    fake["steps"] = MetricSeries(
        metric=get_metric("steps"),
        by_day={date(2026, 8, 1): 9000},
        truncated=True,
        truncation_reason="Reached the 50-page fetch limit.",
    )

    with patch("mcp_fitbit_air.summary.fetch_metric", side_effect=fake_fetch(fake)):
        result = build_summary(Mock(), date(2026, 8, 1), date(2026, 8, 1), _all_summary_names(), TZ)

    assert result["metric_status"]["steps"]["truncated"] is True
    assert "50-page" in result["metric_status"]["steps"]["reason"]
    # A complete metric stays quiet rather than carrying truncated=False noise.
    assert "truncated" not in result["metric_status"]["hrv"]
