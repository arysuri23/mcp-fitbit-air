"""Cross-tool invariants: whatever happens, every tool honours the contract.

The per-tool test files check what each tool means. This file checks what they
all have to agree on, and is parametrised over the tools the SDK actually
advertises so a sixth tool cannot quietly opt out.
"""

import asyncio
import json
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from mcp_fitbit_air.auth import AuthError
from mcp_fitbit_air.client import ApiError

VALID_STATES = {"ok", "no_data", "warming_up", "error"}


@pytest.fixture
def fake_context(monkeypatch):
    ctx = Mock()
    ctx.timezone = ZoneInfo("UTC")
    monkeypatch.setattr("mcp_fitbit_air.server.get_context", lambda: ctx)
    return ctx


def all_tool_calls():
    from mcp_fitbit_air import server

    return [
        ("get_profile_and_devices", lambda: server.get_profile_and_devices()),
        ("get_daily_summary", lambda: server.get_daily_summary("last week")),
        ("get_metric_series", lambda: server.get_metric_series("hrv", "last week")),
        ("get_sleep_detail", lambda: server.get_sleep_detail("yesterday")),
        ("query_raw", lambda: server.query_raw("sleep", "last week")),
    ]


TOOL_CALLS = all_tool_calls()


def _break_every_client_method(client, **kwargs) -> None:
    for name in (
        "get_profile",
        "get_settings",
        "get_paired_devices",
        "list_data_points",
        "daily_rollup",
    ):
        for attr, value in kwargs.items():
            setattr(getattr(client, name), attr, value)


@pytest.mark.parametrize("name,call", TOOL_CALLS)
def test_tool_returns_valid_state_on_empty_data(fake_context, name, call):
    fake_context.client.list_data_points.return_value = []
    fake_context.client.daily_rollup.return_value = []
    fake_context.client.get_profile.return_value = {}
    fake_context.client.get_settings.return_value = {}
    fake_context.client.get_paired_devices.return_value = []

    result = call()

    assert result["state"] in VALID_STATES, f"{name} returned {result}"


@pytest.mark.parametrize("name,call", TOOL_CALLS)
def test_tool_returns_error_state_on_api_failure(fake_context, name, call):
    _break_every_client_method(
        fake_context.client, side_effect=ApiError("upstream exploded", status=500)
    )

    result = call()

    assert result["state"] == "error", f"{name} returned {result}"


@pytest.mark.parametrize("name,call", TOOL_CALLS)
def test_tool_returns_auth_remedy_when_unauthenticated(monkeypatch, name, call):
    """An expired refresh token is the single most likely failure in normal use,
    because restricted scopes in Testing status expire it every 7 days. Every
    tool has to name the command that fixes it."""
    from mcp_fitbit_air import server

    class Unauthenticated:
        timezone = ZoneInfo("UTC")

        @property
        def client(self):
            raise AuthError("no credentials on disk")

    monkeypatch.setattr(server, "get_context", lambda: Unauthenticated())

    result = call()

    assert result["state"] == "error"
    assert "mcp-fitbit-air auth" in result["remedy"], f"{name} returned {result}"


@pytest.mark.parametrize("name,call", TOOL_CALLS)
def test_tool_never_raises(fake_context, name, call):
    _break_every_client_method(fake_context.client, side_effect=RuntimeError("chaos"))

    result = call()  # must not raise

    assert result["state"] == "error", f"{name} returned {result}"


@pytest.mark.parametrize("name,call", TOOL_CALLS)
def test_tool_result_is_json_serialisable(fake_context, name, call):
    """Results cross a stdio JSON boundary. NaN and Infinity are valid Python
    floats and valid json.dumps output by default, but not valid JSON — they
    would be emitted and then rejected at the far end."""
    fake_context.client.list_data_points.return_value = []
    fake_context.client.daily_rollup.return_value = []
    fake_context.client.get_profile.return_value = {}
    fake_context.client.get_settings.return_value = {}
    fake_context.client.get_paired_devices.return_value = []

    json.dumps(call(), allow_nan=False)


@pytest.mark.parametrize("name,call", TOOL_CALLS)
def test_tool_writes_nothing_to_stdout(fake_context, capsys, name, call):
    """stdout IS the MCP transport. One stray print corrupts the protocol for
    the rest of the session, and the symptom is a client that goes silent
    rather than an error anyone can trace back here."""
    fake_context.client.list_data_points.return_value = []
    fake_context.client.daily_rollup.return_value = []
    fake_context.client.get_profile.return_value = {}
    fake_context.client.get_settings.return_value = {}
    fake_context.client.get_paired_devices.return_value = []

    call()
    _break_every_client_method(fake_context.client, side_effect=RuntimeError("chaos"))
    call()

    assert capsys.readouterr().out == "", f"{name} wrote to stdout"


def test_every_advertised_tool_is_covered_by_this_contract():
    """Without this, adding a sixth tool silently exempts it from every
    invariant above — the tests would still pass and prove less."""
    from mcp_fitbit_air import server

    advertised = {t.name for t in asyncio.run(server.mcp.list_tools())}

    assert advertised == {name for name, _ in TOOL_CALLS}
