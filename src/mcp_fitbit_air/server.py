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
from .dates import DateParseError, resolve_range
from .fetch import ADDITIVE_UNITS, build_filter, fetch_metric
from .mapping import METRICS, SUMMARY_METRICS, UnknownMetricError, get_metric
from .results import ResultState, ToolResult
from .summary import build_summary

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
    if not has_any:
        return ToolResult.no_data(
            f"No health data recorded between {start.isoformat()} and {end.isoformat()}. "
            "Check that the Fitbit Air has synced recently with get_profile_and_devices.",
            **summary,
        )

    return ToolResult.ok(summary)


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


def _floor_to_bucket(moment: str, minutes: int) -> str | None:
    """Floor an RFC-3339 instant onto a bucket boundary, or None if unparseable."""
    try:
        parsed = dt.datetime.fromisoformat(moment.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    epoch_minutes = int(parsed.timestamp() // 60)
    floored = (epoch_minutes // minutes) * minutes
    return dt.datetime.fromtimestamp(floored * 60, tz=dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


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
        label = _floor_to_bucket(_reading_time(point) or "", bucket_minutes)
        if label is None:
            # A reading with no resolvable timestamp cannot be placed in time.
            # Counted rather than dropped silently, so a systematic shape change
            # shows up as a number instead of as quietly missing data.
            unplaced += 1
            continue
        buckets.setdefault(label, []).append(value)

    readings = [
        _summarise_bucket(label, values, additive)
        for label, values in sorted(buckets.items())
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
        return ToolResult.no_data(
            f"No intraday {metric.name} recorded between {start.isoformat()} and "
            f"{end.isoformat()}.",
            **meta,
        )

    data = {
        "metric": metric.name,
        "unit": metric.unit,
        "granularity": "intraday",
        "range": {"start": start.isoformat(), "end": end.isoformat()},
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
    series = fetch_metric(ctx.client, metric, fetch_start, end, ctx.timezone)

    if series.state is ResultState.ERROR:
        return ToolResult.error(series.message or "Failed to fetch metric.")
    if series.state is ResultState.WARMING_UP:
        return ToolResult.warming_up(series.message, warmup_nights=spec.warmup_nights)
    if series.state is ResultState.NO_DATA:
        return ToolResult.no_data(series.message)

    baseline = compute_baseline(
        list(series.by_day.values()), window_days=(end - fetch_start).days + 1
    )
    points = [
        {"date": day.isoformat(), "value": value}
        for day, value in sorted(series.by_day.items())
        if start <= day <= end
    ]

    meta: dict[str, Any] = {}
    if series.truncated:
        meta = {"truncated": True, "reason": series.truncation_reason}

    return ToolResult.ok(
        {
            "metric": metric,
            "unit": spec.unit,
            "granularity": "daily",
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "baseline_window": {
                "start": fetch_start.isoformat(),
                "end": end.isoformat(),
                "days": (end - fetch_start).days + 1,
            },
            "baseline": baseline.to_dict(),
            "points": points,
        },
        **meta,
    )


def run_server() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    mcp.run()
