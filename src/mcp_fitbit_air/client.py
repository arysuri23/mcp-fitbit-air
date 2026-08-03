"""All HTTP against the Google Health API.

This module is the single seam through which API access flows. Adding a local
cache or sync layer later means changing this file only — no tool signature
anywhere else needs to change.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any

import requests
from google.auth.transport.requests import AuthorizedSession

logger = logging.getLogger(__name__)


def _civil_date(value: date) -> dict[str, int]:
    return {"year": value.year, "month": value.month, "day": value.day}

BASE_URL = "https://health.googleapis.com/v4"
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0
TIMEOUT_SECONDS = 30
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

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


class HealthClient:
    def __init__(self, credentials, session=None) -> None:
        # `session` is injectable so tests can drive a plain requests.Session
        # without real credential machinery.
        self._session = session or AuthorizedSession(credentials)
        self._credentials = credentials

    # -- internals ---------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs) -> dict[str, Any]:
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

    # -- public API --------------------------------------------------------

    def get_profile(self) -> dict[str, Any]:
        return self._request("GET", f"{BASE_URL}/users/me/profile")

    def get_settings(self) -> dict[str, Any]:
        """Settings, not profile, is where `timeZone` and `utcOffset` live."""
        return self._request("GET", f"{BASE_URL}/users/me/settings")

    def get_paired_devices(self) -> list[dict[str, Any]]:
        payload = self._request("GET", f"{BASE_URL}/users/me/pairedDevices")
        return payload.get("pairedDevices", []) or payload.get("devices", [])

    def list_data_points(
        self,
        data_type: str,
        filter_expr: str | None = None,
        page_size: int = 1440,
    ) -> list[dict[str, Any]]:
        """List data points, following pagination to completion."""
        url = f"{BASE_URL}/users/me/dataTypes/{data_type}/dataPoints"
        params: dict[str, Any] = {"pageSize": page_size}
        if filter_expr:
            params["filter"] = filter_expr

        collected: list[dict[str, Any]] = []
        while True:
            payload = self._request("GET", url, params=params)
            collected.extend(payload.get("dataPoints", []))
            token = payload.get("nextPageToken")
            if not token:
                return collected
            params = dict(params, pageToken=token)

    def daily_rollup(
        self,
        data_type: str,
        start: date,
        end: date,
        window_size_days: int = 1,
    ) -> list[dict[str, Any]]:
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

        collected: list[dict[str, Any]] = []
        while True:
            payload = self._request("POST", url, json=body)
            collected.extend(payload.get("rollupDataPoints", []))
            token = payload.get("nextPageToken")
            if not token:
                return collected
            body = dict(body, pageToken=token)
