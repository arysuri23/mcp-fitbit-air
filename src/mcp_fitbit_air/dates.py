"""Natural-language date resolution.

Claude should never have to do date arithmetic before calling a tool — that is
a reliable source of off-by-one errors, especially around "this week". All
relative expressions resolve against today in the user's profile timezone.

Relative ranges end YESTERDAY, not today: today's data is incomplete and
including a partial day would skew any comparison against it.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

EXAMPLES = 'Try "yesterday", "last week", "last 30 days", or "2026-07-28".'


class DateParseError(Exception):
    """Raised when a date expression cannot be understood."""


def _today_in(tz: ZoneInfo, today: date | None) -> date:
    return today if today is not None else datetime.now(tz).date()


def _parse_iso(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def resolve_day(value: str, tz: ZoneInfo, today: date | None = None) -> date:
    """Resolve a single-day expression to a concrete date."""
    text = value.strip().lower()
    now = _today_in(tz, today)

    if text == "today":
        return now
    if text == "yesterday":
        return now - timedelta(days=1)

    parsed = _parse_iso(text)
    if parsed is not None:
        return parsed

    match = re.fullmatch(r"(\d+) days? ago", text)
    if match:
        return now - timedelta(days=int(match.group(1)))

    raise DateParseError(f"Could not understand the date {value!r}. {EXAMPLES}")


def _relative_range(text: str, now: date) -> tuple[date, date] | None:
    """Ranges ending yesterday, since today is always partial."""
    end = now - timedelta(days=1)

    if text in {"last week", "past week", "this week"}:
        return end - timedelta(days=6), end
    if text in {"last month", "past month"}:
        return end - timedelta(days=29), end

    match = re.fullmatch(r"last (\d+) days?", text)
    if match:
        days = int(match.group(1))
        if days < 1:
            return None
        return end - timedelta(days=days - 1), end

    return None


def resolve_range(
    start: str,
    end: str | None,
    tz: ZoneInfo,
    today: date | None = None,
) -> tuple[date, date]:
    """Resolve a date range. Both bounds are inclusive.

    `start` may itself be a whole-range expression such as "last week", in which
    case `end` must be omitted.
    """
    now = _today_in(tz, today)
    text = start.strip().lower()

    relative = _relative_range(text, now)
    if relative is not None:
        if end is not None:
            raise DateParseError(
                f"{start!r} already describes a whole range, so an end date cannot "
                "also be given."
            )
        return relative

    start_date = resolve_day(start, tz, today)
    # A bare single-day start means that one day. Defaulting the end to `now`
    # instead made "yesterday" resolve to a two-day range ending on today's
    # partial data — the opposite of what the caller asked for, and enough to
    # push an intraday request past the API's page limit. "Through today" is
    # still expressible as an explicit end_date, and whole-range expressions
    # ("last week") never reach this branch.
    end_date = resolve_day(end, tz, today) if end is not None else start_date

    if end_date < start_date:
        raise DateParseError(
            f"End date {end_date.isoformat()} is before start date "
            f"{start_date.isoformat()}."
        )

    return start_date, end_date
