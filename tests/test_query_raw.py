import asyncio
import json
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

TZ = ZoneInfo("UTC")


@pytest.fixture
def fake_context(monkeypatch):
    ctx = Mock()
    ctx.timezone = TZ
    monkeypatch.setattr("mcp_fitbit_air.server.get_context", lambda: ctx)
    return ctx


def test_query_raw_list_passes_data_type_through(fake_context):
    from mcp_fitbit_air.server import query_raw

    fake_context.client.list_data_points.return_value = [{"floors": {"count": 12}}]

    result = query_raw("floors", "2026-08-01", "2026-08-02")

    assert result["state"] == "ok"
    assert result["data"]["dataPoints"][0]["floors"]["count"] == 12


def test_query_raw_daily_rollup_uses_rollup_call(fake_context):
    from mcp_fitbit_air.server import query_raw

    fake_context.client.daily_rollup.return_value = [{"distance": {"meters": 5000}}]

    result = query_raw("distance", "2026-08-01", "2026-08-02", method="dailyRollUp")

    fake_context.client.daily_rollup.assert_called_once()
    assert result["data"]["dataPoints"][0]["distance"]["meters"] == 5000


def test_unknown_method_is_rejected(fake_context):
    from mcp_fitbit_air.server import query_raw

    result = query_raw("floors", "2026-08-01", method="magic")

    assert result["state"] == "error"
    assert "list" in result["message"]


def test_empty_result_is_no_data(fake_context):
    from mcp_fitbit_air.server import query_raw

    fake_context.client.list_data_points.return_value = []

    result = query_raw("floors", "2026-08-01", "2026-08-02")

    assert result["state"] == "no_data"


def test_result_is_truncated_to_protect_the_context_window(fake_context):
    from mcp_fitbit_air.server import MAX_RAW_POINTS, query_raw

    fake_context.client.list_data_points.return_value = [
        {"floors": {"count": n}} for n in range(MAX_RAW_POINTS + 50)
    ]

    result = query_raw("floors", "2026-08-01", "2026-08-02")

    assert len(result["data"]["dataPoints"]) == MAX_RAW_POINTS
    assert result["truncated"] is True


# -- Filter dialects ---------------------------------------------------------
#
# Phase 0 established three dialects, and the WRONG one returns HTTP 200 with
# zero rows rather than an error. The plan built
# "{data_type}.interval.civil_start_time" for every type, which matches none of
# the eight verified members - so query_raw as specified would have reported
# "no data" for essentially every request, indistinguishably from a genuinely
# empty range.


def test_known_data_type_reuses_the_verified_filter(fake_context):
    """sleep's dialect is verified in the registry; query_raw must not re-guess
    it and get it wrong."""
    from mcp_fitbit_air.server import query_raw

    fake_context.client.list_data_points.return_value = [{"sleep": {}}]

    query_raw("sleep", "2026-08-01", "2026-08-01")

    _, kwargs = fake_context.client.list_data_points.call_args
    assert kwargs["filter_expr"] == (
        'sleep.interval.civil_end_time >= "2026-08-01" AND '
        'sleep.interval.civil_end_time < "2026-08-02"'
    )


def test_known_physical_dialect_data_type_is_also_reused(fake_context):
    from mcp_fitbit_air.server import query_raw

    fake_context.client.list_data_points.return_value = [{"heartRate": {}}]

    query_raw("heart-rate", "2026-08-01", "2026-08-01")

    _, kwargs = fake_context.client.list_data_points.call_args
    assert kwargs["filter_expr"] == (
        'heart_rate.sample_time.physical_time >= "2026-08-01T00:00:00Z" AND '
        'heart_rate.sample_time.physical_time < "2026-08-02T00:00:00Z"'
    )


def test_unknown_daily_type_guesses_the_civil_date_dialect(fake_context):
    """Every verified daily-* type filters on <snake_type>.date."""
    from mcp_fitbit_air.server import query_raw

    fake_context.client.list_data_points.return_value = [{"x": 1}]

    query_raw("daily-vo2-max", "2026-08-01", "2026-08-01")

    _, kwargs = fake_context.client.list_data_points.call_args
    assert kwargs["filter_expr"] == (
        'daily_vo2_max.date >= "2026-08-01" AND daily_vo2_max.date < "2026-08-02"'
    )


def test_unknown_interval_type_guesses_the_physical_dialect(fake_context):
    from mcp_fitbit_air.server import query_raw

    fake_context.client.list_data_points.return_value = [{"x": 1}]

    query_raw("floors", "2026-08-01", "2026-08-01")

    _, kwargs = fake_context.client.list_data_points.call_args
    assert kwargs["filter_expr"] == (
        'floors.interval.start_time >= "2026-08-01T00:00:00Z" AND '
        'floors.interval.start_time < "2026-08-02T00:00:00Z"'
    )


def test_an_explicit_filter_overrides_every_guess(fake_context):
    """The real escape hatch: when the guess is wrong, the caller can say
    exactly what the API should be asked."""
    from mcp_fitbit_air.server import query_raw

    fake_context.client.list_data_points.return_value = [{"x": 1}]

    query_raw("weird-type", "2026-08-01", filter_expr='weird_type.thing >= "2026-08-01"')

    _, kwargs = fake_context.client.list_data_points.call_args
    assert kwargs["filter_expr"] == 'weird_type.thing >= "2026-08-01"'


def test_empty_result_from_a_guessed_filter_says_the_filter_may_be_wrong(fake_context):
    """Zero rows from a guessed dialect is ambiguous. Saying so - and showing the
    filter used - is the difference between a dead end and a next step."""
    from mcp_fitbit_air.server import query_raw

    fake_context.client.list_data_points.return_value = []

    result = query_raw("floors", "2026-08-01", "2026-08-01")

    assert result["state"] == "no_data"
    assert "floors.interval.start_time" in result["message"]
    assert "filter" in result["message"].lower()


def test_empty_result_from_a_verified_filter_does_not_blame_the_filter(fake_context):
    """For a registry-backed type the dialect is known good, so an empty result
    means there is genuinely no data - saying otherwise would send the caller
    chasing a filter that is already correct."""
    from mcp_fitbit_air.server import query_raw

    fake_context.client.list_data_points.return_value = []

    result = query_raw("sleep", "2026-08-01", "2026-08-01")

    assert result["state"] == "no_data"
    assert "filter" not in result["message"].lower()


def test_rollup_empty_result_does_not_mention_filters(fake_context):
    """dailyRollUp takes no filter expression at all."""
    from mcp_fitbit_air.server import query_raw

    fake_context.client.daily_rollup.return_value = []

    result = query_raw("distance", "2026-08-01", "2026-08-02", method="dailyRollUp")

    assert result["state"] == "no_data"
    assert "filter" not in result["message"].lower()


def test_query_raw_surfaces_page_cap_truncation(fake_context):
    from mcp_fitbit_air.client import DataPoints
    from mcp_fitbit_air.server import query_raw

    fake_context.client.list_data_points.return_value = DataPoints(
        [{"floors": {"count": 1}}],
        truncated=True,
        truncation_reason="Reached the 50-page fetch limit.",
    )

    result = query_raw("floors", "2026-08-01", "2026-08-02")

    assert result["truncated"] is True
    assert "50-page" in result["reason"]


def test_method_is_validated_before_any_credentials_are_needed(monkeypatch):
    from mcp_fitbit_air import server
    from mcp_fitbit_air.auth import AuthError

    def explode():
        raise AuthError("no token")

    monkeypatch.setattr(server, "get_context", explode)

    result = server.query_raw("floors", "2026-08-01", method="magic")

    assert result["state"] == "error"
    assert "list" in result["message"]


# -- SDK boundary ------------------------------------------------------------


def test_call_tool_through_sdk_boundary(fake_context):
    import mcp_fitbit_air.server as server

    fake_context.client.list_data_points.return_value = [{"floors": {"count": 12}}]

    result = asyncio.run(
        server.mcp.call_tool(
            "query_raw", {"data_type": "floors", "start_date": "2026-08-01"}
        )
    )

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["data"]["dataPoints"][0]["floors"]["count"] == 12


def test_call_tool_input_schema_exposes_every_argument():
    import mcp_fitbit_air.server as server

    tools = asyncio.run(server.mcp.list_tools())
    tool = next(t for t in tools if t.name == "query_raw")

    schema = tool.input_schema or {}
    assert set(schema.get("properties", {})) >= {
        "data_type",
        "start_date",
        "end_date",
        "method",
        "filter_expr",
    }
    assert schema.get("required") == ["data_type", "start_date"]


# -- Methods a data type does not support ------------------------------------
#
# Verified live: query_raw("floors", ...) returns HTTP 400 "List is not
# supported for data type floors, but the following actions are supported:
# reconcile, rollup, dailyRollup". Cumulative types reject list outright, so the
# tool's own examples must not imply otherwise and the error must say what to
# do next.


def test_list_rejected_for_a_cumulative_type_suggests_daily_rollup(fake_context):
    from mcp_fitbit_air.client import ApiError
    from mcp_fitbit_air.server import query_raw

    fake_context.client.list_data_points.side_effect = ApiError(
        "Health API error (400): List is not supported for data type floors, but "
        "the following actions are supported: reconcile, rollup, dailyRollup",
        status=400,
    )

    result = query_raw("floors", "2026-08-01")

    assert result["state"] == "error"
    assert "dailyRollUp" in result["remedy"]
    assert "floors" in result["remedy"]


def test_an_unrelated_api_error_is_not_dressed_up_as_a_method_problem(fake_context):
    """Only the 'use dailyRollUp instead' case earns that remedy; a 500 must not
    be answered with advice that would not help."""
    from mcp_fitbit_air.client import ApiError
    from mcp_fitbit_air.server import query_raw

    fake_context.client.list_data_points.side_effect = ApiError(
        "Health API error (500): Backend error", status=500
    )

    result = query_raw("floors", "2026-08-01")

    assert result["state"] == "error"
    assert "dailyRollUp" not in (result.get("remedy") or "")
