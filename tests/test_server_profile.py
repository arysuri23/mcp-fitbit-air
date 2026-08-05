import asyncio
import json
from unittest.mock import Mock

import pytest

from mcp_fitbit_air.auth import AuthError
from mcp_fitbit_air.client import ApiError, RateLimitError


@pytest.fixture
def fake_context(monkeypatch):
    ctx = Mock()
    monkeypatch.setattr("mcp_fitbit_air.server.get_context", lambda: ctx)
    return ctx


def test_profile_and_devices_returns_ok_with_battery(fake_context):
    from mcp_fitbit_air.server import get_profile_and_devices

    fake_context.client.get_profile.return_value = {"age": 30}
    fake_context.client.get_settings.return_value = {"timeZone": "America/New_York"}
    fake_context.client.get_paired_devices.return_value = [
        {"deviceVersion": "Fitbit Air", "batteryLevel": 82, "lastSyncTime": "2026-08-02T09:00:00Z"}
    ]

    result = get_profile_and_devices()

    assert result["state"] == "ok"
    assert result["data"]["devices"][0]["batteryLevel"] == 82
    # Timezone comes from settings; the profile response has no such field.
    assert result["data"]["settings"]["timeZone"] == "America/New_York"


def test_no_paired_devices_is_no_data_not_error(fake_context):
    from mcp_fitbit_air.server import get_profile_and_devices

    fake_context.client.get_profile.return_value = {"age": 30}
    fake_context.client.get_settings.return_value = {"timeZone": "UTC"}
    fake_context.client.get_paired_devices.return_value = []

    result = get_profile_and_devices()

    assert result["state"] == "no_data"
    assert "device" in result["message"].lower()


def test_auth_error_becomes_error_result_with_remedy(fake_context):
    from mcp_fitbit_air.server import get_profile_and_devices

    type(fake_context).client = property(
        lambda self: (_ for _ in ()).throw(AuthError("no token"))
    )

    result = get_profile_and_devices()

    assert result["state"] == "error"
    assert "mcp-fitbit-air auth" in result["remedy"]


def test_api_error_becomes_error_result(fake_context):
    from mcp_fitbit_air.server import get_profile_and_devices

    fake_context.client.get_profile.side_effect = ApiError("boom", status=500)

    result = get_profile_and_devices()

    assert result["state"] == "error"
    assert "boom" in result["message"]


def test_rate_limit_surfaces_wait_remedy(fake_context):
    from mcp_fitbit_air.server import get_profile_and_devices

    fake_context.client.get_profile.side_effect = RateLimitError(
        "slow down", status=429, remedy="Wait a few minutes and try again."
    )

    result = get_profile_and_devices()

    assert result["state"] == "error"
    assert "Wait a few minutes" in result["remedy"]


def test_tool_never_raises_out_of_the_tool_boundary(fake_context):
    """An unexpected exception must still return a structured error, because a
    raised exception inside an MCP tool is far less useful to Claude."""
    from mcp_fitbit_air.server import get_profile_and_devices

    fake_context.client.get_profile.side_effect = ValueError("unexpected")

    result = get_profile_and_devices()

    assert result["state"] == "error"


def test_config_error_becomes_error_result_with_env_remedy(fake_context, monkeypatch):
    """tool_guard's ConfigError branch is untested by the brief's list; cover it
    explicitly since every except branch should have direct coverage."""
    from mcp_fitbit_air.config import ConfigError
    from mcp_fitbit_air.server import get_profile_and_devices

    fake_context.client.get_profile.side_effect = ConfigError("missing FITBIT_MCP_CLIENT_ID")

    result = get_profile_and_devices()

    assert result["state"] == "error"
    assert "environment variables" in result["remedy"].lower()


def test_unknown_metric_error_becomes_error_result(fake_context):
    """tool_guard's UnknownMetricError branch is untested by the brief's list;
    get_profile_and_devices never raises it itself, but the guard is shared by
    later tools, so exercise it here to keep every except branch covered."""
    from mcp_fitbit_air.mapping import UnknownMetricError
    from mcp_fitbit_air.server import get_profile_and_devices

    fake_context.client.get_profile.side_effect = UnknownMetricError("unknown metric 'foo'")

    result = get_profile_and_devices()

    assert result["state"] == "error"
    assert "foo" in result["message"]


def test_date_parse_error_becomes_error_result(fake_context):
    """Same rationale as the UnknownMetricError case above: cover the shared
    tool_guard branch even though this tool has no date arguments itself."""
    from mcp_fitbit_air.dates import DateParseError
    from mcp_fitbit_air.server import get_profile_and_devices

    fake_context.client.get_profile.side_effect = DateParseError("could not understand 'whenever'")

    result = get_profile_and_devices()

    assert result["state"] == "error"
    assert "whenever" in result["message"]


# -- SDK-boundary tests ------------------------------------------------------
#
# Every test above calls get_profile_and_devices() directly as a plain Python
# function, which never goes near the MCP SDK's schema/signature machinery.
# That is exactly how 106 passing tests coexisted with a server that failed
# output validation on every single real tool call: `functools.wraps(fn)` in
# `tool_guard` sets `wrapper.__wrapped__ = fn`, and `inspect.signature`
# (which the SDK uses, with `eval_str=True`, to build each tool's schema)
# follows `__wrapped__` by default, silently recovering `fn`'s own
# `-> ToolResult` return annotation instead of `wrapper`'s `-> dict[str, Any]`.
# The tests below go through `mcp.call_tool` / `mcp.list_tools` - the same
# entry points a real MCP client uses - so a regression here fails loudly.


def test_call_tool_through_sdk_boundary_returns_ok(fake_context):
    """Drives the tool through the real SDK dispatch path (mcp.call_tool),
    not just the bare Python function, so SDK-level output-schema validation
    is actually exercised."""
    import mcp_fitbit_air.server as server

    fake_context.client.get_profile.return_value = {"age": 30}
    fake_context.client.get_settings.return_value = {"timeZone": "UTC"}
    fake_context.client.get_paired_devices.return_value = [
        {"deviceVersion": "Fitbit Air", "batteryLevel": 82, "lastSyncTime": "2026-08-02T09:00:00Z"}
    ]

    result = asyncio.run(server.mcp.call_tool("get_profile_and_devices", {}))

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["state"] == "ok"
    assert payload["data"]["devices"][0]["batteryLevel"] == 82


def test_call_tool_output_schema_does_not_require_meta():
    """The declared output schema must reflect the wrapper's real
    `-> dict[str, Any]` return type, not the ToolResult dataclass tool_guard
    builds internally and then flattens via to_dict(). A schema requiring
    `state` and `meta` (ToolResult's own fields) is exactly the bug: to_dict()
    deliberately has no `meta` key, so every real call would fail validation."""
    import mcp_fitbit_air.server as server

    tools = asyncio.run(server.mcp.list_tools())
    tool = next(t for t in tools if t.name == "get_profile_and_devices")

    required = (tool.output_schema or {}).get("required", [])
    assert "meta" not in required


def test_call_tool_no_data_is_not_flagged_as_sdk_error(fake_context):
    """no_data is a legitimate answer (e.g. a brand-new account with no
    paired devices yet), not a failure - it must come back as a normal,
    non-error MCP tool result, not with is_error set."""
    import mcp_fitbit_air.server as server

    fake_context.client.get_profile.return_value = {"age": 30}
    fake_context.client.get_settings.return_value = {"timeZone": "UTC"}
    fake_context.client.get_paired_devices.return_value = []

    result = asyncio.run(server.mcp.call_tool("get_profile_and_devices", {}))

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["state"] == "no_data"


def test_call_tool_auth_error_returns_structured_result_not_sdk_exception(fake_context):
    """An exception raised deep inside a tool (here, credentials missing) must
    still surface as a structured error result through the real SDK dispatch
    path, not as a raised SDK-level exception or protocol-level tool error -
    tool_guard's whole point is that Claude gets a message and a remedy, not
    a crash."""
    import mcp_fitbit_air.server as server

    type(fake_context).client = property(
        lambda self: (_ for _ in ()).throw(AuthError("no token"))
    )

    result = asyncio.run(server.mcp.call_tool("get_profile_and_devices", {}))

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["state"] == "error"
    assert "mcp-fitbit-air auth" in payload["remedy"]
