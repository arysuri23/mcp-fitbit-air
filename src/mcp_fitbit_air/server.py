"""MCP server exposing Fitbit Air data.

Never write to stdout from this process: stdout is the MCP protocol stream.
Diagnostics go to stderr via logging.
"""

from __future__ import annotations

import datetime as dt
import functools
import inspect
import logging
import sys
from datetime import timedelta
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer

from .auth import AuthError
from .baselines import compute_baseline, lookback_start
from .client import ApiError
from .config import ConfigError
from .context import get_context
from .dates import DateParseError, resolve_day, resolve_range
from .fetch import ADDITIVE_UNITS, build_filter, fetch_metric, utc_instant
from .mapping import (
    METRICS,
    SUMMARY_METRICS,
    UnknownMetricError,
    coerce_number,
    get_metric,
)
from .results import ResultState, ToolResult
from .summary import baseline_window, build_summary

logger = logging.getLogger(__name__)

mcp = MCPServer("fitbit-air")


def tool_guard(fn: Callable[..., ToolResult]) -> Callable[..., dict[str, Any]]:
    """Translate exceptions into structured error results.

    A tool must never raise out of its boundary: a structured error result tells
    Claude what went wrong and how to fix it, where a traceback does not.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs).to_dict()
        except AuthError as exc:
            return ToolResult.error(str(exc), remedy=exc.remedy).to_dict()
        except ConfigError as exc:
            return ToolResult.error(
                str(exc), remedy="Set the required environment variables; see the README."
            ).to_dict()
        except ApiError as exc:
            return ToolResult.error(str(exc), remedy=exc.remedy).to_dict()
        except (UnknownMetricError, DateParseError) as exc:
            return ToolResult.error(str(exc)).to_dict()
        except Exception as exc:  # noqa: BLE001 - deliberate boundary
            logger.exception("Unexpected error in %s", fn.__name__)
            return ToolResult.error(f"Unexpected error in {fn.__name__}: {exc}").to_dict()

    # `functools.wraps` copies `fn`'s `__annotations__` (so `-> ToolResult`
    # silently overwrites `wrapper`'s own `-> dict[str, Any]`) AND sets
    # `wrapper.__wrapped__ = fn`. That second part is the one that actually
    # bites: the MCP SDK builds each tool's schema by calling
    # `inspect.signature(tool_fn, eval_str=True)`, and `inspect.signature`
    # follows `__wrapped__` by default whenever the object has no
    # `__signature__` of its own — so it skips straight past `wrapper` and
    # inspects `fn` instead, recovering `fn`'s original `-> ToolResult`
    # return annotation no matter what `wrapper.__annotations__` says.
    # (Confirmed directly: overwriting `wrapper.__annotations__` alone does
    # NOT change what `inspect.signature(wrapper)` reports, because it never
    # looks at `wrapper.__annotations__` once it has unwrapped past it.) The
    # SDK then builds the output schema from the `ToolResult` dataclass
    # (requires `state` AND a nested `meta` key) and validates `wrapper`'s
    # actual return value — a dict flattened by `ToolResult.to_dict()`, with
    # no `meta` key by design — against that schema. Every call to every
    # `tool_guard`-wrapped tool then fails SDK output validation, even though
    # the tool itself succeeded. This is invisible to tests that call the
    # wrapped function directly in Python, since that path never touches the
    # SDK's schema/signature machinery at all.
    #
    # Setting `__signature__` explicitly makes `wrapper` self-describing
    # again: `inspect.signature` stops unwrapping as soon as it finds an
    # object that already carries `__signature__`, so it uses this signature
    # instead of following `__wrapped__` to `fn`. Parameters are copied
    # unchanged from `fn` (via `eval_str=True`, to resolve the string
    # annotations `from __future__ import annotations` produces) since later
    # tools take real arguments (metric name, date range, ...) that must
    # still appear in the input schema — only the return annotation changes.
    original_signature = inspect.signature(fn, eval_str=True)
    wrapper.__signature__ = original_signature.replace(return_annotation=dict[str, Any])
    wrapper.__annotations__ = {
        **getattr(fn, "__annotations__", {}),
        "return": dict[str, Any],
    }

    return wrapper


@mcp.tool()
@tool_guard
def get_profile_and_devices() -> ToolResult:
    """Get the user's profile, settings, and paired Fitbit devices.

    Returns age and membership date, unit and timezone settings, and each paired
    device's battery level and last sync time. Useful on its own for "is my
    Fitbit synced?", and a good first call when other tools return no data — a
    stale lastSyncTime explains missing data better than any other signal.
    """
    ctx = get_context()
    profile = ctx.client.get_profile()
    settings = ctx.client.get_settings()
    devices = ctx.client.get_paired_devices()

    if not devices:
        return ToolResult.no_data(
            "No paired devices found on this account. If the Fitbit Air was set up "
            "recently, open the Google Health app and confirm it has synced at least once.",
            profile=profile,
            settings=settings,
        )

    return ToolResult.ok({"profile": profile, "settings": settings, "devices": devices})


@mcp.tool()
@tool_guard
def get_daily_summary(start_date: str, end_date: str | None = None) -> ToolResult:
    """Get a day-by-day health summary: sleep, resting heart rate, HRV, steps,
    active zone minutes, SpO2, and skin temperature deviation.

    This is the tool to reach for first — most questions about recent health
    can be answered from a single call.

    Dates accept natural language: "last week", "yesterday", "last 30 days",
    "2026-07-28". Give a whole-range expression as start_date on its own
    ("last week"), or a start and end pair. Maximum range is 90 days.

    Every value carries its unit and a trailing baseline computed over a window
    reaching up to 30 days before the requested range, reported alongside the
    sample size it came from — a baseline with a small n is not a settled norm.
    Days with no data are marked no_data rather than zero, and metrics Fitbit
    has not computed yet are marked warming_up.
    """
    ctx = get_context()
    start, end = resolve_range(start_date, end_date, ctx.timezone)
    try:
        summary = build_summary(ctx.client, start, end, SUMMARY_METRICS, ctx.timezone)
    except ValueError as exc:
        return ToolResult.error(str(exc))

    has_any = any(
        cell.get("state") == "ok"
        for day in summary["days"]
        for cell in day["metrics"].values()
    )
    if has_any:
        return ToolResult.ok(summary)

    # An empty table has to say which kind of empty it is. Reporting no_data for
    # all three sends the user to check their band's sync when the real cause
    # was an outage, an expired token, or simply a device too new to have
    # computed anything yet.
    statuses = summary["metric_status"]
    failures = sorted(
        {
            status["message"]
            for status in statuses.values()
            if status["state"] == "error" and status.get("message")
        }
    )
    if failures:
        # When every metric failed for the same reason, that reason's fix is the
        # answer — not something to be dug out of seven per-metric cells.
        remedies = {
            status["remedy"] for status in statuses.values() if status.get("remedy")
        }
        return ToolResult.error(
            f"Could not fetch health data for {start.isoformat()} to "
            f"{end.isoformat()}: {'; '.join(failures)}",
            remedy=remedies.pop() if len(remedies) == 1 else None,
            **summary,
        )

    if all(status["state"] == "warming_up" for status in statuses.values()):
        return ToolResult.warming_up(
            "; ".join(
                sorted(
                    {
                        status["message"]
                        for status in statuses.values()
                        if status.get("message")
                    }
                )
            ),
            **summary,
        )

    return ToolResult.no_data(
        f"No health data recorded between {start.isoformat()} and {end.isoformat()}. "
        "Check that the Fitbit Air has synced recently with get_profile_and_devices.",
        **summary,
    )


MAX_INTRADAY_DAYS = 7


def _reading_time(point: dict) -> str | None:
    """Best-effort physical timestamp for one intraday reading.

    The two intraday metrics do not share a shape: heart_rate carries
    `heartRate.sampleTime.physicalTime`, while steps carries
    `steps.interval.startTime`. Both are checked, and the bare point last, so a
    future type that hoists either field still resolves.
    """
    for holder in (point.get("heartRate") or {}, point.get("steps") or {}, point):
        if not isinstance(holder, dict):
            continue
        sample = holder.get("sampleTime")
        if isinstance(sample, dict) and isinstance(sample.get("physicalTime"), str):
            return sample["physicalTime"]
        interval = holder.get("interval")
        if isinstance(interval, dict) and isinstance(interval.get("startTime"), str):
            return interval["startTime"]
    return None


# Intraday readings are aggregated into fixed time buckets rather than returned
# raw. The band samples heart rate roughly every two seconds, not once a minute:
# one real day measured 38,093 points, or 1.87 MB of JSON. Seven of those would
# be ~13 MB on the stdio stream — useless to Claude and far past the page cap.
# Buckets keep the shape of the day (when it rose, how high, for how long) at a
# thousandth of the size.
BUCKET_CHOICES_MINUTES = (5, 15, 30, 60, 120)
MAX_INTRADAY_BUCKETS = 600


def _bucket_minutes(start: dt.date, end: dt.date) -> int:
    """Smallest bucket that keeps the response under MAX_INTRADAY_BUCKETS."""
    total_minutes = ((end - start).days + 1) * 24 * 60
    for size in BUCKET_CHOICES_MINUTES:
        if total_minutes / size <= MAX_INTRADAY_BUCKETS:
            return size
    return BUCKET_CHOICES_MINUTES[-1]


def _floor_to_bucket(moment: str, minutes: int, tz) -> str | None:
    """Floor an RFC-3339 instant onto a bucket boundary in the user's local time.

    The API returns physical instants in UTC, but every other date in the
    response is a local calendar date. Labelling buckets in UTC left the two
    incomparable — a request for one local day came back with buckets spanning
    two UTC dates, and nothing in the payload said what the offset was. Flooring
    on the local wall clock also keeps bucket boundaries on round local times
    for offsets that are not whole hours.
    """
    try:
        parsed = dt.datetime.fromisoformat(moment.replace("Z", "+00:00"))
    except (AttributeError, ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    local = parsed.astimezone(tz)
    minute_of_day = (local.hour * 60 + local.minute) // minutes * minutes
    floored = local.replace(
        hour=minute_of_day // 60, minute=minute_of_day % 60, second=0, microsecond=0
    )
    return floored.isoformat()


def _summarise_bucket(time_label: str, values: list[float], additive: bool) -> dict:
    """Additive metrics (steps, minutes) sum within a bucket; a rate like bpm
    would be meaningless summed, so it reports its range and mean instead."""
    if additive:
        return {"time": time_label, "total": round(sum(values), 2), "n": len(values)}
    return {
        "time": time_label,
        "min": min(values),
        "max": max(values),
        "avg": round(sum(values) / len(values), 1),
        "n": len(values),
    }


def _intraday_series(ctx, metric, start: dt.date, end: dt.date, truncated: bool) -> ToolResult:
    # build_filter owns the per-type dialect choice; the wrong one returns
    # HTTP 200 with zero points rather than an error.
    points = ctx.client.list_data_points(
        metric.data_type,
        filter_expr=build_filter(metric, start, end, ctx.timezone),
    )

    bucket_minutes = _bucket_minutes(start, end)
    additive = metric.unit in ADDITIVE_UNITS
    buckets: dict[str, list[float]] = {}
    unplaced = 0

    for point in points:
        value = metric.extract(point)
        if value is None:
            continue
        label = _floor_to_bucket(_reading_time(point) or "", bucket_minutes, ctx.timezone)
        if label is None:
            # A reading with no resolvable timestamp cannot be placed in time.
            # Counted rather than dropped silently, so a systematic shape change
            # shows up as a number instead of as quietly missing data.
            unplaced += 1
            continue
        buckets.setdefault(label, []).append(value)

    readings = [
        _summarise_bucket(label, values, additive)
        for label, values in sorted(
            buckets.items(), key=lambda item: dt.datetime.fromisoformat(item[0])
        )
    ]

    meta: dict[str, Any] = {}
    reasons = []
    if truncated:
        reasons.append(
            f"Intraday requests are capped at {MAX_INTRADAY_DAYS} days to keep "
            "responses manageable; only the most recent window was returned."
        )
    if getattr(points, "truncated", False):
        reasons.append(points.truncation_reason or "The fetch stopped before the end of the range.")
    if reasons:
        meta = {"truncated": True, "reason": " ".join(reasons)}

    if not readings:
        message = (
            f"No intraday {metric.name} recorded between {start.isoformat()} and "
            f"{end.isoformat()}."
        )
        if unplaced:
            # Readings did arrive; none carried a timestamp this code could
            # read. Reporting a bare "nothing recorded" here would hide exactly
            # the field rename the counter was added to catch.
            message += (
                f" {unplaced} readings were returned but none carried a usable "
                "timestamp, which usually means the API changed shape."
            )
            meta = {**meta, "unplaced_readings": unplaced}
        return ToolResult.no_data(message, **meta)

    data = {
        "metric": metric.name,
        "unit": metric.unit,
        "granularity": "intraday",
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "timezone": str(ctx.timezone),
        "bucket_minutes": bucket_minutes,
        "aggregation": "total" if additive else "min/max/avg",
        "readings": readings,
    }
    if unplaced:
        data["unplaced_readings"] = unplaced

    return ToolResult.ok(data, **meta)


@mcp.tool()
@tool_guard
def get_metric_series(
    metric: str,
    start_date: str,
    end_date: str | None = None,
    granularity: str = "daily",
) -> ToolResult:
    """Get one metric over time, for drilling into a trend.

    Valid metrics: sleep_duration, resting_heart_rate, hrv, steps,
    active_zone_minutes, spo2, skin_temperature_deviation, heart_rate.

    granularity is "daily" (default) or "intraday". Intraday is supported only
    for heart_rate and steps. Its readings are aggregated into time buckets
    (bucket_minutes says how wide, chosen from the range) because the band
    samples every few seconds — a raw day of heart_rate is ~38,000 points.
    Additive metrics report a bucket total; rates report min, max and avg.
    Intraday is capped at 7 days, and the response says so when it truncates.

    Dates accept natural language, as in get_daily_summary. Daily results carry
    a trailing baseline drawn from up to 30 days before the requested range,
    reported with the sample size and the window it actually used.
    """
    # Checked before get_context() so a typo fails on its own terms rather than
    # as an authentication error.
    if granularity not in {"daily", "intraday"}:
        return ToolResult.error(
            f"Unknown granularity {granularity!r}. Use \"daily\" or \"intraday\"."
        )

    spec = get_metric(metric)  # raises UnknownMetricError, handled by tool_guard
    ctx = get_context()
    start, end = resolve_range(start_date, end_date, ctx.timezone)

    if granularity == "intraday":
        if not spec.supports_intraday:
            supported = sorted(n for n, m in METRICS.items() if m.supports_intraday)
            return ToolResult.error(
                f"{metric} does not support intraday granularity. Intraday is "
                f"available for: {', '.join(supported)}."
            )
        truncated = (end - start).days + 1 > MAX_INTRADAY_DAYS
        if truncated:
            start = end - timedelta(days=MAX_INTRADAY_DAYS - 1)
        return _intraday_series(ctx, spec, start, end, truncated)

    span = (end - start).days + 1
    if span > spec.max_range_days:
        return ToolResult.error(
            f"Range of {span} days exceeds the {spec.max_range_days}-day maximum "
            f"for {metric}. Request a narrower range."
        )

    # Reach back past `start` so the baseline is trailing rather than a mean of
    # the same days being asked about. Those extra days inform the baseline and
    # are then dropped from `points`.
    fetch_start = lookback_start(start, end, [metric])
    series = fetch_metric(
        ctx.client, metric, fetch_start, end, ctx.timezone, requested_days=span
    )

    # Built before any return: a fetch that stopped early has to say so on every
    # path out of here, not just the successful one. "No data in this range" and
    # "we stopped looking before we got there" are different answers.
    meta: dict[str, Any] = {}
    if series.truncated:
        meta = {"truncated": True, "reason": series.truncation_reason}

    if series.state is ResultState.ERROR:
        return ToolResult.error(
            series.message or "Failed to fetch metric.", remedy=series.remedy
        )
    if series.state is ResultState.WARMING_UP:
        return ToolResult.warming_up(
            series.message, warmup_nights=spec.warmup_nights, **meta
        )
    if series.state is ResultState.NO_DATA:
        return ToolResult.no_data(series.message, **meta)

    baseline = compute_baseline(
        list(series.by_day.values()), window_days=(end - fetch_start).days + 1
    )
    points = [
        {"date": day.isoformat(), "value": value}
        for day, value in sorted(series.by_day.items())
        if start <= day <= end
    ]
    if not points:
        # by_day spans the widened baseline window, so it can be non-empty while
        # the requested range is not. Reporting ok with an empty list would hand
        # Claude a baseline and nothing to say about it.
        return ToolResult.no_data(
            f"No {metric} recorded between {start.isoformat()} and "
            f"{end.isoformat()}, though there are values before that range.",
            **meta,
        )

    return ToolResult.ok(
        {
            "metric": metric,
            "unit": spec.unit,
            "granularity": "daily",
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "baseline_window": baseline_window(
                fetch_start, start, end, (end - fetch_start).days + 1
            ),
            "baseline": baseline.to_dict(),
            "points": points,
        },
        **meta,
    )


MAX_RAW_POINTS = 500


def _truncation_meta(points) -> dict[str, Any]:
    """Lift a paginated fetch's truncation flag into tool-result meta."""
    if getattr(points, "truncated", False):
        return {
            "truncated": True,
            "reason": points.truncation_reason
            or "The fetch stopped before the end of the range.",
        }
    return {}


@mcp.tool()
@tool_guard
def get_sleep_detail(date: str) -> ToolResult:
    """Get the full sleep-stage breakdown for a single night.

    Returns each sleep session's stage segments (AWAKE, LIGHT, DEEP, REM) with
    their start and end times, plus per-stage totals — more detail than
    get_daily_summary, which reports only total duration. Naps come back as
    separate sessions; is_main_sleep distinguishes the night itself.

    `date` is the date the sleep ENDED (the morning you woke up), and accepts
    natural language: "yesterday", "2026-08-01".
    """
    ctx = get_context()
    day = resolve_day(date, ctx.timezone)
    metric = get_metric("sleep_duration")
    points = ctx.client.list_data_points(
        metric.data_type,
        filter_expr=build_filter(metric, day, day, ctx.timezone),
    )

    sessions = []
    for point in points:
        payload = point.get("sleep") or {}
        summary = payload.get("summary") or {}
        # Every minute count arrives as a JSON string; coerce so Claude can
        # compare and sum them without guessing. coerce_number also rejects the
        # literal "NaN" the API emits for values it could not compute.
        sessions.append(
            {
                "minutes_asleep": coerce_number(summary.get("minutesAsleep")),
                "minutes_awake": coerce_number(summary.get("minutesAwake")),
                "minutes_to_fall_asleep": coerce_number(
                    summary.get("minutesToFallAsleep")
                ),
                "minutes_in_sleep_period": coerce_number(
                    summary.get("minutesInSleepPeriod")
                ),
                "stages_summary": [
                    {
                        "type": entry.get("type"),
                        "minutes": coerce_number(entry.get("minutes")),
                        "count": coerce_number(entry.get("count")),
                    }
                    for entry in summary.get("stagesSummary") or []
                ],
                "stages": payload.get("stages", []),
                "is_main_sleep": (payload.get("metadata") or {}).get("mainSleep"),
                "interval": payload.get("interval"),
            }
        )

    if not sessions:
        return ToolResult.no_data(
            f"No sleep recorded for {day.isoformat()}. The band may not have been "
            "worn overnight, or may not have synced."
        )

    return ToolResult.ok(
        {"date": day.isoformat(), "sessions": sessions}, **_truncation_meta(points)
    )


# data_type -> the registry entry that already knows its verified filter.
_METRIC_BY_DATA_TYPE = {metric.data_type: metric for metric in METRICS.values()}


def _raw_filter(data_type: str, start: dt.date, end: dt.date, tz) -> tuple[str, bool]:
    """Build a list filter for an arbitrary data type.

    Returns the expression and whether it was guessed. This matters because the
    wrong dialect does not error — it returns HTTP 200 with zero rows, which
    reads exactly like "no data". Registry-backed types reuse their verified
    member; anything else follows the pattern its family used in Phase 0, and
    the caller is told the filter was a guess if nothing comes back.
    """
    known = _METRIC_BY_DATA_TYPE.get(data_type)
    if known is not None:
        return build_filter(known, start, end, tz), False

    member_root = data_type.replace("-", "_")
    exclusive_end = end + timedelta(days=1)
    if data_type.startswith("daily-"):
        # Every verified daily-* type filters on <snake_type>.date with civil dates.
        member = f"{member_root}.date"
        lower, upper = start.isoformat(), exclusive_end.isoformat()
    else:
        member = f"{member_root}.interval.start_time"
        lower = utc_instant(start, tz)
        upper = utc_instant(exclusive_end, tz)
    return f'{member} >= "{lower}" AND {member} < "{upper}"', True


@mcp.tool()
@tool_guard
def query_raw(
    data_type: str,
    start_date: str,
    end_date: str | None = None,
    method: str = "list",
    filter_expr: str | None = None,
) -> ToolResult:
    """Escape hatch: query any Google Health API data type directly.

    Use this only when no other tool covers what is needed — the other tools
    return cleaner, better-labelled data.

    `data_type` is a Google Health API identifier, for example: distance,
    floors, total-calories, exercise, active-minutes, daily-vo2-max,
    daily-respiratory-rate, sedentary-period, weight, altitude.

    `method` is "list" (default) or "dailyRollUp" (per-day aggregation). Not
    every data type supports both: cumulative types such as floors and distance
    reject "list" outright and must be queried with "dailyRollUp". If a request
    is refused for that reason, the response says which method to use instead.

    Filters differ per data type and the wrong one returns zero rows rather
    than an error, so the filter is derived from the type. If a derived filter
    returns nothing, the response says which one it used; pass `filter_expr` to
    override it with an exact AIP-160 expression.

    Results are truncated at 500 data points.
    """
    # Checked before get_context() so a typo fails on its own terms rather than
    # as an authentication error.
    if method not in {"list", "dailyRollUp"}:
        return ToolResult.error(
            f"Unknown method {method!r}. Use \"list\" or \"dailyRollUp\"."
        )

    ctx = get_context()
    start, end = resolve_range(start_date, end_date, ctx.timezone)

    guessed = False
    used_filter = None
    if method == "dailyRollUp":
        points = ctx.client.daily_rollup(data_type, start, end)
    else:
        if filter_expr:
            used_filter = filter_expr
        else:
            used_filter, guessed = _raw_filter(data_type, start, end, ctx.timezone)
        try:
            points = ctx.client.list_data_points(data_type, filter_expr=used_filter)
        except ApiError as exc:
            # Cumulative types reject list with a 400 that names the methods they
            # do support. Left to tool_guard this arrives as a bare API error;
            # turning it into an actionable remedy saves a guessing round.
            if "dailyrollup" in str(exc).lower():
                return ToolResult.error(
                    str(exc),
                    remedy=(
                        f"{data_type} does not support list. Retry with "
                        'method="dailyRollUp".'
                    ),
                )
            raise

    if not points:
        message = (
            f"No {data_type} data between {start.isoformat()} and {end.isoformat()}."
        )
        if guessed:
            message += (
                f" Note that the filter was derived rather than verified for this "
                f"data type: {used_filter}. An unsupported filter member returns "
                "zero rows rather than an error, so this may mean the filter is "
                "wrong rather than that the range is empty. Pass filter_expr to "
                "override it."
            )
        return ToolResult.no_data(message)

    meta = _truncation_meta(points)
    if len(points) > MAX_RAW_POINTS:
        reason = f"Truncated to the first {MAX_RAW_POINTS} of {len(points)} points."
        meta = {
            "truncated": True,
            "reason": f"{meta['reason']} {reason}" if meta else reason,
        }

    return ToolResult.ok(
        {
            "data_type": data_type,
            "method": method,
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "filter": used_filter,
            "dataPoints": points[:MAX_RAW_POINTS],
        },
        **meta,
    )


def run_server() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    mcp.run()
