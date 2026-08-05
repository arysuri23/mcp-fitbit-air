"""Lazily built, process-wide server state.

Credentials are loaded on first use rather than at import time, so that a
missing token produces a clean error result from a tool call instead of
crashing the server before it can speak MCP.
"""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .auth import load_credentials
from .client import ApiError, HealthClient
from .config import Config

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = ZoneInfo("UTC")


class ServerContext:
    def __init__(self) -> None:
        self._client: HealthClient | None = None
        self._timezone: ZoneInfo | None = None

    @property
    def client(self) -> HealthClient:
        if self._client is None:
            config = Config.from_env()
            credentials = load_credentials(config)
            self._client = HealthClient(credentials)
        return self._client

    @property
    def timezone(self) -> ZoneInfo:
        """The user's timezone, used to resolve relative dates and to convert
        local dates into the UTC instants that physical-time filters need.

        This comes from `users/me/settings`, NOT the profile - Phase 0 confirmed
        the profile response carries no timezone field of any kind.
        """
        if self._timezone is None:
            try:
                name = self.client.get_settings().get("timeZone")
                self._timezone = ZoneInfo(name) if name else DEFAULT_TIMEZONE
            except (ApiError, KeyError, ValueError, ZoneInfoNotFoundError) as exc:
                logger.warning("Falling back to UTC; could not read settings timeZone: %s", exc)
                self._timezone = DEFAULT_TIMEZONE
        return self._timezone


_context: ServerContext | None = None


def get_context() -> ServerContext:
    global _context
    if _context is None:
        _context = ServerContext()
    return _context
