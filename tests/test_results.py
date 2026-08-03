import json

from mcp_fitbit_air.results import ResultState, ToolResult


def test_ok_result_serialises_state_and_data():
    result = ToolResult.ok({"steps": 1000})
    assert result.to_dict() == {"state": "ok", "data": {"steps": 1000}}


def test_no_data_is_not_an_error():
    result = ToolResult.no_data("No data recorded for 2026-08-01.")
    payload = result.to_dict()
    assert payload["state"] == "no_data"
    assert payload["message"].startswith("No data")
    assert "data" not in payload


def test_warming_up_carries_nights_so_far():
    result = ToolResult.warming_up(
        "HRV needs 3 nights of wear before Fitbit computes it.", nights_so_far=1
    )
    payload = result.to_dict()
    assert payload["state"] == "warming_up"
    assert payload["nights_so_far"] == 1


def test_error_includes_remedy_when_given():
    result = ToolResult.error("Not authenticated.", remedy="Run `mcp-fitbit-air auth`.")
    payload = result.to_dict()
    assert payload["state"] == "error"
    assert payload["remedy"] == "Run `mcp-fitbit-air auth`."


def test_meta_keys_are_merged_into_the_payload():
    result = ToolResult.ok([1, 2, 3], truncated=True, reason="7-day intraday cap")
    payload = result.to_dict()
    assert payload["truncated"] is True
    assert payload["reason"] == "7-day intraday cap"


def test_states_are_plain_strings_for_json_serialisation():
    assert ResultState.OK == "ok"
    assert ResultState.WARMING_UP.value == "warming_up"


def test_ok_result_is_json_serialisable():
    result = ToolResult.ok({"steps": 1000})
    payload = result.to_dict()
    json_str = json.dumps(payload, allow_nan=False)
    parsed = json.loads(json_str)
    assert parsed["state"] == "ok"


def test_no_data_result_is_json_serialisable():
    result = ToolResult.no_data("No data recorded.")
    payload = result.to_dict()
    json_str = json.dumps(payload, allow_nan=False)
    parsed = json.loads(json_str)
    assert parsed["state"] == "no_data"


def test_warming_up_result_is_json_serialisable():
    result = ToolResult.warming_up("Warming up.", nights_so_far=1)
    payload = result.to_dict()
    json_str = json.dumps(payload, allow_nan=False)
    parsed = json.loads(json_str)
    assert parsed["state"] == "warming_up"


def test_error_result_is_json_serialisable():
    result = ToolResult.error("Error occurred.", remedy="Try again.")
    payload = result.to_dict()
    json_str = json.dumps(payload, allow_nan=False)
    parsed = json.loads(json_str)
    assert parsed["state"] == "error"


def test_meta_kwarg_cannot_overwrite_state():
    result = ToolResult.no_data("No data.", state="ok")
    payload = result.to_dict()
    assert payload["state"] == "no_data", "state must not be overridable by meta kwargs"


def test_meta_kwarg_cannot_overwrite_message():
    result = ToolResult(
        state=ResultState.NO_DATA,
        message="Original message",
        meta={"message": "Overridden message"}
    )
    payload = result.to_dict()
    assert payload["message"] == "Original message", "message must not be overridable by meta"


def test_meta_direct_assignment_cannot_overwrite_remedy():
    result = ToolResult(
        state=ResultState.ERROR,
        message="Error",
        remedy="Original remedy",
        meta={"remedy": "Overridden remedy"}
    )
    payload = result.to_dict()
    assert payload["remedy"] == "Original remedy", "remedy must not be overridable by meta"
