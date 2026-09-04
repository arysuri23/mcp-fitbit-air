"""Process-wide server state.

ServerContext is a module-level singleton, so anything it caches it caches for
the life of the server. That makes a cached failure permanent, which is why the
timezone fallback needs its own test.
"""

from unittest.mock import Mock
from zoneinfo import ZoneInfo

from mcp_fitbit_air.client import ApiError
from mcp_fitbit_air.context import DEFAULT_TIMEZONE, ServerContext


def context_with(client) -> ServerContext:
    ctx = ServerContext()
    ctx._client = client
    return ctx


def test_timezone_comes_from_settings():
    client = Mock()
    client.get_settings.return_value = {"timeZone": "America/New_York"}

    assert context_with(client).timezone == ZoneInfo("America/New_York")


def test_timezone_falls_back_to_utc_when_settings_fail():
    client = Mock()
    client.get_settings.side_effect = ApiError("boom", status=500)

    assert context_with(client).timezone == DEFAULT_TIMEZONE


def test_a_failed_lookup_is_not_cached_forever():
    """Found in review. The fallback was cached in the except branch of a
    process-wide singleton, so one 500 on the first tool call pinned every later
    date resolution and every physical-time filter to UTC until restart -
    silently, and off by the user's whole offset."""
    client = Mock()
    client.get_settings.side_effect = [
        ApiError("transient", status=500),
        {"timeZone": "America/New_York"},
    ]
    ctx = context_with(client)

    assert ctx.timezone == DEFAULT_TIMEZONE
    assert ctx.timezone == ZoneInfo("America/New_York")


def test_a_successful_lookup_is_cached():
    """The retry must not cost a settings round trip on every single call."""
    client = Mock()
    client.get_settings.return_value = {"timeZone": "America/New_York"}
    ctx = context_with(client)

    ctx.timezone
    ctx.timezone

    assert client.get_settings.call_count == 1


def test_an_unknown_timezone_name_falls_back_without_caching():
    client = Mock()
    client.get_settings.side_effect = [
        {"timeZone": "Mars/Olympus_Mons"},
        {"timeZone": "America/New_York"},
    ]
    ctx = context_with(client)

    assert ctx.timezone == DEFAULT_TIMEZONE
    assert ctx.timezone == ZoneInfo("America/New_York")
