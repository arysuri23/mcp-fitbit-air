"""All HTTP against the Google Health API.

This module is the single seam through which API access flows. Adding a local
cache or sync layer later means changing this file only — no tool signature
anywhere else needs to change.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, timedelta
from typing import Any

import requests
from google.auth.transport.requests import AuthorizedSession, Request

logger = logging.getLogger(__name__)


def _civil_date(value: date) -> dict[str, int]:
    return {"year": value.year, "month": value.month, "day": value.day}

BASE_URL = "https://health.googleapis.com/v4"
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0
TIMEOUT_SECONDS = 30
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
# Cap on pages followed via `nextPageToken`. Without this, a server that keeps
# returning a non-empty token (or repeats the same one) would spin the loop
# forever — and since this client is the single seam every tool call goes
# through, that hangs the whole MCP server. Truncated data beats a hung server.
MAX_PAGES = 50
# One day of `heart-rate` is ~38,000 points, so page size decides both latency
# and how much of a range survives MAX_PAGES. Measured against the live API on
# the same day's data: 1,440/page took 9.4s, 5,000/page took 3.5s, and 10,000
# was no better than 5,000. The larger page also lifts the truncation ceiling
# from 72,000 records to 250,000, which is the difference between a week of
# intraday heart rate arriving whole or arriving cut.
DEFAULT_PAGE_SIZE = 5000

AUTH_REMEDY = "Run `mcp-fitbit-air auth` to re-authenticate."


class ApiError(Exception):
    def __init__(
        self, message: str, status: int | None = None, remedy: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.remedy = remedy


class RateLimitError(ApiError):
    """Raised when the API keeps returning 429 after the retry budget."""


class DataPoints(list):
    """Data points, plus whether the fetch actually reached the end.

    Pagination stops early in two bounded-but-incomplete cases: the page cap
    and a repeated-token cycle. Both were logged to stderr and nowhere else,
    which means a caller — ultimately Claude — could not tell a partial series
    from a whole one and would state conclusions about incomplete data as fact.
    This is not hypothetical: one day of `heart-rate` is ~38,000 points at a
    1,440 page size, so anything past a single day hits the cap.

    Subclassing `list` keeps every existing call site and every isinstance
    check working while carrying that one extra bit. The flag is per-result
    rather than per-client on purpose: summary.py fans metrics out across a
    thread pool, so client-level state would race.
    """

    def __init__(
        self,
        items: Any = (),
        truncated: bool = False,
        truncation_reason: str | None = None,
    ) -> None:
        super().__init__(items)
        self.truncated = truncated
        self.truncation_reason = truncation_reason


class HealthClient:
    def __init__(self, credentials, session=None) -> None:
        # `session` is injectable so tests can drive a plain requests.Session
        # without real credential machinery.
        self._injected_session = session
        self._credentials = credentials
        # summary.py fans seven metrics out across a thread pool sharing this
        # one client. requests.Session is not documented as thread-safe, so each
        # thread gets its own; the credentials underneath are shared, and the
        # lock below keeps their refresh from happening seven times at once.
        self._thread_local = threading.local()
        self._refresh_lock = threading.Lock()

    # -- internals ---------------------------------------------------------

    @property
    def _session(self):
        if self._injected_session is not None:
            return self._injected_session
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = AuthorizedSession(self._credentials)
            self._thread_local.session = session
        return session

    def _ensure_credentials_fresh(self) -> None:
        """Refresh an expired token once, not once per thread.

        google-auth's own `before_request` takes no lock, so without this the
        fan-out could have every thread refreshing the same credentials
        concurrently the moment an access token expires.
        """
        credentials = self._credentials
        if credentials is None or getattr(credentials, "valid", True):
            return
        with self._refresh_lock:
            if not credentials.valid:
                credentials.refresh(Request())

    def _request(self, method: str, url: str, **kwargs) -> dict[str, Any]:
        self._ensure_credentials_fresh()
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                response = self._session.request(
                    method, url, timeout=TIMEOUT_SECONDS, **kwargs
                )
            except requests.RequestException as exc:
                last_error = ApiError(f"Network error contacting the Health API: {exc}")
                if attempt < MAX_RETRIES:
                    time.sleep(BACKOFF_BASE_SECONDS * (2**attempt))
                    continue
                raise last_error from exc

            if response.status_code in RETRYABLE_STATUSES and attempt < MAX_RETRIES:
                delay = BACKOFF_BASE_SECONDS * (2**attempt)
                logger.warning(
                    "HTTP %s from %s; retrying in %.1fs", response.status_code, url, delay
                )
                time.sleep(delay)
                continue

            return self._handle(response)

        raise last_error or ApiError("Request failed after retries")

    def _handle(self, response) -> dict[str, Any]:
        if response.ok:
            return response.json() if response.content else {}

        message = self._extract_message(response)
        status = response.status_code

        if status in (401, 403):
            raise ApiError(
                f"The Health API rejected our credentials ({status}): {message}",
                status=status,
                remedy=AUTH_REMEDY,
            )
        if status == 429:
            raise RateLimitError(
                f"Rate limited by the Health API: {message}",
                status=status,
                remedy="Wait a few minutes and try again.",
            )
        raise ApiError(f"Health API error ({status}): {message}", status=status)

    @staticmethod
    def _extract_message(response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return response.text[:200] or "no response body"
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                return error.get("message", str(error))
            if error:
                return str(error)
        return str(payload)[:200]

    def _paginate(
        self,
        method: str,
        url: str,
        items_key: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> DataPoints:
        """Follow `nextPageToken` to completion, guarding against the two ways
        a pagination loop can hang forever: a server that repeats the same
        token (cycle) and a server that keeps minting fresh tokens
        indefinitely (unbounded growth). Since this helper is the single seam
        every paginated tool call goes through, either failure mode would
        otherwise hang the whole MCP server.

        Exactly one of `params` (GET query params) / `json_body` (POST body)
        should be provided, matching `method`. The token is threaded back
        into whichever one was given, under the key `pageToken`.
        """
        collected: list[dict[str, Any]] = []
        token: str | None = None

        for page in range(MAX_PAGES):
            request_kwargs: dict[str, Any] = {}
            if params is not None:
                request_kwargs["params"] = params
            if json_body is not None:
                request_kwargs["json"] = json_body

            payload = self._request(method, url, **request_kwargs)
            collected.extend(payload.get(items_key, []))

            next_token = payload.get("nextPageToken")
            if not next_token:
                return DataPoints(collected)
            if next_token == token:
                reason = (
                    f"The API repeated the same page token after {page + 1} page(s); "
                    f"stopped with {len(collected)} record(s), which may be incomplete."
                )
                logger.warning("Pagination cycle detected for %s. %s", url, reason)
                return DataPoints(collected, truncated=True, truncation_reason=reason)

            token = next_token
            if params is not None:
                params = dict(params, pageToken=token)
            if json_body is not None:
                json_body = dict(json_body, pageToken=token)

        reason = (
            f"Reached the {MAX_PAGES}-page fetch limit with {len(collected)} record(s); "
            "the range holds more data than one request can return. Ask for a "
            "shorter range to see all of it."
        )
        logger.warning("Hit MAX_PAGES while paginating %s. %s", url, reason)
        return DataPoints(collected, truncated=True, truncation_reason=reason)

    # -- public API --------------------------------------------------------

    def get_profile(self) -> dict[str, Any]:
        return self._request("GET", f"{BASE_URL}/users/me/profile")

    def get_settings(self) -> dict[str, Any]:
        """Settings, not profile, is where `timeZone` and `utcOffset` live."""
        return self._request("GET", f"{BASE_URL}/users/me/settings")

    def get_paired_devices(self) -> list[dict[str, Any]]:
        payload = self._request("GET", f"{BASE_URL}/users/me/pairedDevices")
        # Presence, not truthiness: `devices` is the fallback for an OLD payload
        # that lacks `pairedDevices` entirely. An empty `pairedDevices` is a real
        # answer - this account has no devices - and must not fall through to a
        # stale sibling key.
        if "pairedDevices" in payload:
            return payload["pairedDevices"]
        return payload.get("devices", [])

    def list_data_points(
        self,
        data_type: str,
        filter_expr: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> DataPoints:
        """List data points, following pagination to completion."""
        url = f"{BASE_URL}/users/me/dataTypes/{data_type}/dataPoints"
        params: dict[str, Any] = {"pageSize": page_size}
        if filter_expr:
            params["filter"] = filter_expr

        return self._paginate("GET", url, "dataPoints", params=params)

    def daily_rollup(
        self,
        data_type: str,
        start: date,
        end: date,
        window_size_days: int = 1,
    ) -> DataPoints:
        """Roll data up into per-day buckets. `end` is inclusive here; the API
        range is closed-open, so we send end + 1 day.

        `range.start` / `range.end` are nested CivilDateTime objects. Phase 0
        verified that ISO `startTime`/`endTime` strings and bare
        `{year, month, day}` are both rejected with HTTP 400.
        """
        url = f"{BASE_URL}/users/me/dataTypes/{data_type}/dataPoints:dailyRollUp"
        exclusive_end = end + timedelta(days=1)
        body = {
            "range": {
                "start": {"date": _civil_date(start)},
                "end": {"date": _civil_date(exclusive_end)},
            },
            "windowSizeDays": window_size_days,
        }

        return self._paginate("POST", url, "rollupDataPoints", json_body=body)
