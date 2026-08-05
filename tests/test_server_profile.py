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
