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


# Real shape, from tests/fixtures/list_sleep_nofilter.json. The plan's own
# fixture used sleep.levels[].level with a durationSeconds field, which the API
# never returns and its own reference implementation never read - so that test
# asserted against a payload that cannot occur.
def sleep_point(main: bool = True) -> dict:
    return {
        "dataSource": {"platform": "FITBIT"},
        "sleep": {
            "interval": {
                "startTime": "2026-08-01T04:20:00Z",
                "startUtcOffset": "-14400s",
                "endTime": "2026-08-01T10:50:00Z",
                "endUtcOffset": "-14400s",
            },
            "metadata": {"mainSleep": main, "stagesStatus": "SUCCEEDED"},
            "stages": [
                {
                    "type": "AWAKE",
                    "startTime": "2026-08-01T04:20:00Z",
                    "endTime": "2026-08-01T04:23:30Z",
                },
                {
                    "type": "DEEP",
                    "startTime": "2026-08-01T04:23:30Z",
                    "endTime": "2026-08-01T05:01:30Z",
                },
            ],
            "summary": {
                "minutesAsleep": "390",
                "minutesAwake": "10",
                "minutesToFallAsleep": "5",
                "minutesInSleepPeriod": "400",
                "stagesSummary": [
                    {"type": "DEEP", "minutes": "60", "count": "4"},
                    {"type": "REM", "minutes": "90", "count": "5"},
                ],
            },
        },
    }


def test_sleep_detail_returns_stage_segments(fake_context):
    from mcp_fitbit_air.server import get_sleep_detail

    fake_context.client.list_data_points.return_value = [sleep_point()]

    result = get_sleep_detail("2026-08-01")

    assert result["state"] == "ok"
    assert len(result["data"]["sessions"]) == 1
    session = result["data"]["sessions"][0]
    assert session["stages"][0]["type"] == "AWAKE"
    assert session["stages"][1]["type"] == "DEEP"


def test_sleep_summary_minutes_are_numbers_not_strings(fake_context):
    """The API returns every sleep minute count as a JSON string. Passing "390"
    through means Claude has to guess whether it can be compared or summed."""
    from mcp_fitbit_air.server import get_sleep_detail

    fake_context.client.list_data_points.return_value = [sleep_point()]

    session = get_sleep_detail("2026-08-01")["data"]["sessions"][0]

    assert session["minutes_asleep"] == 390.0
    assert session["minutes_awake"] == 10.0
    assert session["minutes_to_fall_asleep"] == 5.0


def test_stages_summary_minutes_are_numbers_too(fake_context):
    """stagesSummary is where "how much deep sleep did I get" is answered, and
    it arrives as strings just like the top-level summary."""
    from mcp_fitbit_air.server import get_sleep_detail

    fake_context.client.list_data_points.return_value = [sleep_point()]

    stages = get_sleep_detail("2026-08-01")["data"]["sessions"][0]["stages_summary"]

    assert stages[0] == {"type": "DEEP", "minutes": 60.0, "count": 4.0}


def test_main_sleep_flag_is_exposed(fake_context):
    """A night can contain naps as well as the main sleep; without this flag a
    30-minute nap is indistinguishable from the night itself."""
    from mcp_fitbit_air.server import get_sleep_detail

    fake_context.client.list_data_points.return_value = [
        sleep_point(main=True),
        sleep_point(main=False),
    ]

    sessions = get_sleep_detail("2026-08-01")["data"]["sessions"]

    assert [s["is_main_sleep"] for s in sessions] == [True, False]


def test_sleep_detail_uses_the_civil_end_time_dialect(fake_context):
    """sleep filters on civil_end_time, not a physical instant - the wrong
    dialect returns HTTP 200 with zero rows."""
    from mcp_fitbit_air.server import get_sleep_detail

    fake_context.client.list_data_points.return_value = []

    get_sleep_detail("2026-08-01")

    _, kwargs = fake_context.client.list_data_points.call_args
    assert kwargs["filter_expr"] == (
        'sleep.interval.civil_end_time >= "2026-08-01" AND '
        'sleep.interval.civil_end_time < "2026-08-02"'
    )


def test_no_sleep_recorded_is_no_data(fake_context):
    from mcp_fitbit_air.server import get_sleep_detail

    fake_context.client.list_data_points.return_value = []

    result = get_sleep_detail("2026-08-01")

    assert result["state"] == "no_data"
    assert "2026-08-01" in result["message"]


def test_relative_date_is_accepted(fake_context):
    from mcp_fitbit_air.server import get_sleep_detail

    fake_context.client.list_data_points.return_value = []

    result = get_sleep_detail("yesterday")

    assert result["state"] == "no_data"


def test_unparseable_date_is_an_error(fake_context):
    from mcp_fitbit_air.server import get_sleep_detail

    result = get_sleep_detail("whenever")

    assert result["state"] == "error"
    assert "yesterday" in result["message"]


def test_sleep_detail_surfaces_page_cap_truncation(fake_context):
    from mcp_fitbit_air.client import DataPoints
    from mcp_fitbit_air.server import get_sleep_detail

    fake_context.client.list_data_points.return_value = DataPoints(
        [sleep_point()],
        truncated=True,
        truncation_reason="Reached the 50-page fetch limit.",
    )

    result = get_sleep_detail("2026-08-01")

    assert result["truncated"] is True


def test_sleep_detail_is_json_serialisable_with_nan_rejected(fake_context):
    from mcp_fitbit_air.server import get_sleep_detail

    point = sleep_point()
    # The API emits the literal string "NaN" for values it cannot compute.
    point["sleep"]["summary"]["minutesAsleep"] = "NaN"
    fake_context.client.list_data_points.return_value = [point]

    result = get_sleep_detail("2026-08-01")

    json.dumps(result, allow_nan=False)
    assert result["data"]["sessions"][0]["minutes_asleep"] is None


# -- SDK boundary ------------------------------------------------------------


def test_call_tool_through_sdk_boundary(fake_context):
    import mcp_fitbit_air.server as server

    fake_context.client.list_data_points.return_value = [sleep_point()]

    result = asyncio.run(
        server.mcp.call_tool("get_sleep_detail", {"date": "2026-08-01"})
    )

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["state"] == "ok"
    assert payload["data"]["sessions"][0]["minutes_asleep"] == 390.0


def test_call_tool_input_schema_exposes_date():
    """The parameter is named `date`, which shadows datetime.date if server.py
    ever imports it bare - the schema is where that would show up."""
    import mcp_fitbit_air.server as server

    tools = asyncio.run(server.mcp.list_tools())
    tool = next(t for t in tools if t.name == "get_sleep_detail")

    schema = tool.input_schema or {}
    assert "date" in schema.get("properties", {})
    assert schema.get("required") == ["date"]
