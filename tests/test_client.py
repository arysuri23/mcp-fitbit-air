import json
from datetime import date

import pytest
import requests
import responses

from mcp_fitbit_air.client import MAX_PAGES, ApiError, HealthClient, RateLimitError

BASE = "https://health.googleapis.com/v4"


@pytest.fixture
def client(fake_credentials):
    return HealthClient(fake_credentials, session=requests.Session())


@responses.activate
def test_get_profile_returns_body(client):
    responses.add(
        responses.GET,
        f"{BASE}/users/me/profile",
        json={"displayName": "Test", "timezone": "America/New_York"},
        status=200,
    )

    profile = client.get_profile()

    assert profile["timezone"] == "America/New_York"


@responses.activate
def test_list_data_points_follows_pagination(client):
    responses.add(
        responses.GET,
        f"{BASE}/users/me/dataTypes/sleep/dataPoints",
        json={"dataPoints": [{"id": 1}], "nextPageToken": "page2"},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{BASE}/users/me/dataTypes/sleep/dataPoints",
        json={"dataPoints": [{"id": 2}]},
        status=200,
    )

    points = client.list_data_points("sleep")

    assert [p["id"] for p in points] == [1, 2]
    assert len(responses.calls) == 2


@responses.activate
def test_list_data_points_sends_filter_expression(client):
    responses.add(
        responses.GET,
        f"{BASE}/users/me/dataTypes/sleep/dataPoints",
        json={"dataPoints": []},
        status=200,
    )

    client.list_data_points("sleep", filter_expr='sleep.interval.civil_end_time >= "2026-08-01"')

    assert "filter=" in responses.calls[0].request.url


@responses.activate
def test_empty_data_points_returns_empty_list_not_error(client):
    responses.add(
        responses.GET,
        f"{BASE}/users/me/dataTypes/daily-heart-rate-variability/dataPoints",
        json={},
        status=200,
    )

    assert client.list_data_points("daily-heart-rate-variability") == []


@responses.activate
def test_daily_rollup_posts_range_and_returns_points(client):
    responses.add(
        responses.POST,
        f"{BASE}/users/me/dataTypes/steps/dataPoints:dailyRollUp",
        json={"rollupDataPoints": [{"steps": {"count": 900}}]},
        status=200,
    )

    points = client.daily_rollup("steps", date(2026, 8, 1), date(2026, 8, 2))

    assert points[0]["steps"]["count"] == 900
    body = json.loads(responses.calls[0].request.body.decode())
    # Nested CivilDateTime. Phase 0 proved ISO strings and bare year/month/day
    # are both rejected with HTTP 400.
    assert body["range"]["start"] == {"date": {"year": 2026, "month": 8, "day": 1}}
    # Range is closed-open, so the inclusive end of Aug 2 is sent as Aug 3.
    assert body["range"]["end"] == {"date": {"year": 2026, "month": 8, "day": 3}}
    assert body["windowSizeDays"] == 1


@responses.activate
def test_get_settings_returns_timezone(client):
    """Timezone lives in settings; profile has no timezone field at all."""
    responses.add(
        responses.GET,
        f"{BASE}/users/me/settings",
        json={"timeZone": "America/New_York", "utcOffset": "-14400s"},
        status=200,
    )

    assert client.get_settings()["timeZone"] == "America/New_York"


@responses.activate
def test_401_raises_api_error_with_auth_remedy(client):
    responses.add(
        responses.GET,
        f"{BASE}/users/me/profile",
        json={"error": {"message": "Invalid Credentials"}},
        status=401,
    )

    with pytest.raises(ApiError) as exc:
        client.get_profile()

    assert exc.value.status == 401
    assert "mcp-fitbit-air auth" in (exc.value.remedy or "")


@responses.activate
def test_429_retries_then_raises_rate_limit_error(client, monkeypatch):
    monkeypatch.setattr("mcp_fitbit_air.client.time.sleep", lambda _: None)
    for _ in range(4):
        responses.add(
            responses.GET,
            f"{BASE}/users/me/profile",
            json={"error": {"message": "Too many requests"}},
            status=429,
        )

    with pytest.raises(RateLimitError):
        client.get_profile()

    assert len(responses.calls) == 4  # initial attempt plus 3 retries


@responses.activate
def test_429_then_success_succeeds(client, monkeypatch):
    monkeypatch.setattr("mcp_fitbit_air.client.time.sleep", lambda _: None)
    responses.add(responses.GET, f"{BASE}/users/me/profile", json={}, status=429)
    responses.add(
        responses.GET, f"{BASE}/users/me/profile", json={"timezone": "UTC"}, status=200
    )

    assert client.get_profile()["timezone"] == "UTC"


@responses.activate
def test_500_surfaces_server_message(client, monkeypatch):
    monkeypatch.setattr("mcp_fitbit_air.client.time.sleep", lambda _: None)
    for _ in range(4):
        responses.add(
            responses.GET,
            f"{BASE}/users/me/profile",
            json={"error": {"message": "Backend error"}},
            status=500,
        )

    with pytest.raises(ApiError) as exc:
        client.get_profile()

    assert "Backend error" in str(exc.value)


# -- Additional coverage: every except/status branch exercised at least once --
#
# The brief's own test list left several branches in the reference client.py
# implementation untested (403 vs 401, the network-exception retry path, the
# `pairedDevices`/`devices` fallback keys, the non-JSON and keyless error
# bodies in `_extract_message`, the empty-body-on-200 shortcut, and rollup
# pagination). Added here per the task's coverage expectation.


@responses.activate
def test_403_raises_api_error_with_auth_remedy(client):
    """403 (forbidden/insufficient scope) shares the auth-remedy branch with 401."""
    responses.add(
        responses.GET,
        f"{BASE}/users/me/profile",
        json={"error": {"message": "Insufficient scope"}},
        status=403,
    )

    with pytest.raises(ApiError) as exc:
        client.get_profile()

    assert exc.value.status == 403
    assert "mcp-fitbit-air auth" in (exc.value.remedy or "")


@responses.activate
def test_network_error_retries_then_raises_api_error(client, monkeypatch):
    """A transient connection failure (requests.RequestException) should retry
    up to the budget and then surface as a plain ApiError, not crash raw."""
    monkeypatch.setattr("mcp_fitbit_air.client.time.sleep", lambda _: None)
    for _ in range(4):
        responses.add(
            responses.GET,
            f"{BASE}/users/me/profile",
            body=requests.exceptions.ConnectionError("connection refused"),
        )

    with pytest.raises(ApiError) as exc:
        client.get_profile()

    assert not isinstance(exc.value, RateLimitError)
    assert len(responses.calls) == 4  # initial attempt plus 3 retries


@responses.activate
def test_network_error_then_success_succeeds(client, monkeypatch):
    """A retried network error that then succeeds should not raise at all."""
    monkeypatch.setattr("mcp_fitbit_air.client.time.sleep", lambda _: None)
    responses.add(
        responses.GET,
        f"{BASE}/users/me/profile",
        body=requests.exceptions.ConnectionError("connection refused"),
    )
    responses.add(
        responses.GET, f"{BASE}/users/me/profile", json={"timezone": "UTC"}, status=200
    )

    assert client.get_profile()["timezone"] == "UTC"


@responses.activate
def test_get_paired_devices_returns_pairedDevices_key(client):
    """The primary (non-fallback) key path: `pairedDevices` present and
    non-empty is returned as-is."""
    responses.add(
        responses.GET,
        f"{BASE}/users/me/pairedDevices",
        json={"pairedDevices": [{"deviceType": "TRACKER", "batteryLevel": 80}]},
        status=200,
    )

    devices = client.get_paired_devices()

    assert devices == [{"deviceType": "TRACKER", "batteryLevel": 80}]


@responses.activate
def test_get_paired_devices_falls_back_to_devices_key(client):
    """Some captured responses key the list as `devices` rather than
    `pairedDevices`; both must be handled."""
    responses.add(
        responses.GET,
        f"{BASE}/users/me/pairedDevices",
        json={"devices": [{"deviceType": "TRACKER"}]},
        status=200,
    )

    devices = client.get_paired_devices()

    assert devices == [{"deviceType": "TRACKER"}]


@responses.activate
def test_non_json_error_body_uses_raw_text(client, monkeypatch):
    """When the error body isn't JSON at all, `_extract_message` falls back to
    the raw response text instead of raising ValueError uncaught."""
    monkeypatch.setattr("mcp_fitbit_air.client.time.sleep", lambda _: None)
    for _ in range(4):
        responses.add(
            responses.GET,
            f"{BASE}/users/me/profile",
            body="<html>Internal Server Error</html>",
            status=500,
            content_type="text/html",
        )

    with pytest.raises(ApiError) as exc:
        client.get_profile()

    assert "Internal Server Error" in str(exc.value)


@responses.activate
def test_error_payload_without_error_key_uses_str_payload(client, monkeypatch):
    """A JSON error body with no `error` key at all should still produce a
    readable message rather than 'None'."""
    monkeypatch.setattr("mcp_fitbit_air.client.time.sleep", lambda _: None)
    for _ in range(4):
        responses.add(
            responses.GET,
            f"{BASE}/users/me/profile",
            json={"message": "unexpected failure"},
            status=500,
        )

    with pytest.raises(ApiError) as exc:
        client.get_profile()

    assert "unexpected failure" in str(exc.value)


@responses.activate
def test_error_field_present_but_not_dict(client, monkeypatch):
    """`error` present but a bare string (not a dict) is still surfaced."""
    monkeypatch.setattr("mcp_fitbit_air.client.time.sleep", lambda _: None)
    for _ in range(4):
        responses.add(
            responses.GET,
            f"{BASE}/users/me/profile",
            json={"error": "boom"},
            status=500,
        )

    with pytest.raises(ApiError) as exc:
        client.get_profile()

    assert "boom" in str(exc.value)


@responses.activate
def test_non_dict_json_error_body_uses_str_payload(client, monkeypatch):
    """A JSON error body that parses to something other than a dict (e.g. a
    bare list) must still fall through to a string representation rather than
    crashing on `.get`."""
    monkeypatch.setattr("mcp_fitbit_air.client.time.sleep", lambda _: None)
    for _ in range(4):
        responses.add(
            responses.GET,
            f"{BASE}/users/me/profile",
            json=["unexpected", "array", "body"],
            status=500,
        )

    with pytest.raises(ApiError) as exc:
        client.get_profile()

    assert "unexpected" in str(exc.value)


@responses.activate
def test_non_json_empty_body_uses_fallback_message(client, monkeypatch):
    """A non-JSON error response with a genuinely empty body falls back to the
    literal 'no response body' rather than an empty string."""
    monkeypatch.setattr("mcp_fitbit_air.client.time.sleep", lambda _: None)
    for _ in range(4):
        responses.add(
            responses.GET,
            f"{BASE}/users/me/profile",
            body="",
            status=500,
            content_type="text/plain",
        )

    with pytest.raises(ApiError) as exc:
        client.get_profile()

    assert "no response body" in str(exc.value)


@responses.activate
def test_empty_response_body_returns_empty_dict(client):
    """A 200 with a genuinely empty body (no content) must not raise trying to
    parse JSON from nothing."""
    responses.add(
        responses.GET,
        f"{BASE}/users/me/profile",
        body="",
        status=200,
    )

    assert client.get_profile() == {}


@responses.activate
def test_daily_rollup_follows_pagination(client):
    """`daily_rollup` must page through `nextPageToken` just like
    `list_data_points`, accumulating rollupDataPoints across calls."""
    responses.add(
        responses.POST,
        f"{BASE}/users/me/dataTypes/steps/dataPoints:dailyRollUp",
        json={
            "rollupDataPoints": [{"steps": {"count": 100}}],
            "nextPageToken": "page2",
        },
        status=200,
    )
    responses.add(
        responses.POST,
        f"{BASE}/users/me/dataTypes/steps/dataPoints:dailyRollUp",
        json={"rollupDataPoints": [{"steps": {"count": 200}}]},
        status=200,
    )

    points = client.daily_rollup("steps", date(2026, 8, 1), date(2026, 8, 2))

    assert [p["steps"]["count"] for p in points] == [100, 200]
    assert len(responses.calls) == 2
    second_body = json.loads(responses.calls[1].request.body.decode())
    assert second_body["pageToken"] == "page2"


# -- Pagination hang guards: repeated (cyclic) token and unbounded fresh
# tokens must both terminate rather than looping forever. This client is the
# single seam every tool call goes through, so an unbounded pagination loop
# hangs the whole MCP server. --


@responses.activate
def test_list_data_points_stops_on_repeated_page_token(client):
    """A server that returns the same nextPageToken twice is a server bug;
    the client must stop rather than loop forever, and must still hand back
    whatever it collected."""
    responses.add(
        responses.GET,
        f"{BASE}/users/me/dataTypes/sleep/dataPoints",
        json={"dataPoints": [{"id": 1}], "nextPageToken": "cycle"},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{BASE}/users/me/dataTypes/sleep/dataPoints",
        json={"dataPoints": [{"id": 2}], "nextPageToken": "cycle"},
        status=200,
    )

    points = client.list_data_points("sleep")

    assert [p["id"] for p in points] == [1, 2]
    # Exactly 2 calls: the cycle is detected right after the second response
    # repeats the token used to fetch it, so a third (identical) request is
    # never issued.
    assert len(responses.calls) == 2


@responses.activate
def test_list_data_points_stops_at_max_pages(client):
    """A server that keeps minting fresh, non-empty tokens forever must not
    hang the client indefinitely; it should stop at MAX_PAGES and return the
    partial data rather than raising."""
    for i in range(MAX_PAGES):
        responses.add(
            responses.GET,
            f"{BASE}/users/me/dataTypes/sleep/dataPoints",
            json={"dataPoints": [{"id": i}], "nextPageToken": f"token{i}"},
            status=200,
        )

    points = client.list_data_points("sleep")

    assert len(responses.calls) == MAX_PAGES
    assert [p["id"] for p in points] == list(range(MAX_PAGES))


@responses.activate
def test_daily_rollup_stops_on_repeated_page_token(client):
    """Same cycle guard as `list_data_points`, but for the POST-based
    `daily_rollup` pagination loop."""
    responses.add(
        responses.POST,
        f"{BASE}/users/me/dataTypes/steps/dataPoints:dailyRollUp",
        json={"rollupDataPoints": [{"steps": {"count": 100}}], "nextPageToken": "cycle"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{BASE}/users/me/dataTypes/steps/dataPoints:dailyRollUp",
        json={"rollupDataPoints": [{"steps": {"count": 200}}], "nextPageToken": "cycle"},
        status=200,
    )

    points = client.daily_rollup("steps", date(2026, 8, 1), date(2026, 8, 2))

    assert [p["steps"]["count"] for p in points] == [100, 200]
    assert len(responses.calls) == 2


@responses.activate
def test_daily_rollup_stops_at_max_pages(client):
    """Same page-cap guard as `list_data_points`, but for the POST-based
    `daily_rollup` pagination loop."""
    for i in range(MAX_PAGES):
        responses.add(
            responses.POST,
            f"{BASE}/users/me/dataTypes/steps/dataPoints:dailyRollUp",
            json={
                "rollupDataPoints": [{"steps": {"count": i}}],
                "nextPageToken": f"token{i}",
            },
            status=200,
        )

    points = client.daily_rollup("steps", date(2026, 8, 1), date(2026, 8, 2))

    assert len(responses.calls) == MAX_PAGES
    assert [p["steps"]["count"] for p in points] == list(range(MAX_PAGES))
