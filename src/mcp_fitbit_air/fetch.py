"""Fetch one metric over a date range and reduce it to one value per day.

Every shape here was verified against the live API during Phase 0. Two details
matter more than they look:

- Filter dialects differ per data type and the wrong one FAILS SILENTLY,
  returning HTTP 200 with zero points. `build_filter` is the only place that
  decides, and its tests pin the exact strings.
- Physical-time filters compare against UTC instants, so local dates must be
  converted through the user's timezone. Getting this wrong shifts every
  intraday query by the UTC offset.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .client import ApiError
from .mapping import Metric, get_metric
from .results import ResultState

logger = logging.getLogger(__name__)

# Metrics whose same-day values should be summed rather than overwritten.
ADDITIVE_UNITS = {"minutes", "count"}


@dataclass
class MetricSeries:
    metric: Metric
    by_day: dict[date, float] = field(default_factory=dict)
    state: ResultState = ResultState.OK
    message: str | None = None
    # Set when pagination stopped before the end of the range. The data here is
    # real but incomplete, which is worse than missing if nobody says so.
    truncated: bool = False
    truncation_reason: str | None = None


def point_date(metric: Metric, point: dict) -> date | None:
    """Read this point's local calendar day.

    Delegates to the metric's own resolver: the date lives in a different place
    for every type family, and sleep has to derive it from an instant plus an
    offset because its interval carries no civil times.
    """
    return metric.date_of(point)


def utc_instant(day: date, tz: ZoneInfo) -> str:
    """Local midnight on `day`, expressed as an RFC-3339 UTC instant."""
    local = datetime.combine(day, time.min, tzinfo=tz)
    return local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_filter(metric: Metric, start: date, end: date, tz: ZoneInfo) -> str:
    """Build the AIP-160 filter for this metric. `end` is inclusive; the API
    range is closed-open, so the emitted upper bound is the following day."""
    exclusive_end = end + timedelta(days=1)
    member = metric.filter_member

    if metric.filter_dialect == "physical":
        lower = utc_instant(start, tz)
        upper = utc_instant(exclusive_end, tz)
    else:  # "civil_date"
        lower = start.isoformat()
        upper = exclusive_end.isoformat()

    return f'{member} >= "{lower}" AND {member} < "{upper}"'


def _accumulate(series: MetricSeries, points: list[dict]) -> None:
    additive = series.metric.unit in ADDITIVE_UNITS
    for point in points:
        day = point_date(series.metric, point)
        value = series.metric.extract(point)
        if day is None or value is None:
            continue
        if additive and day in series.by_day:
            series.by_day[day] += value
        else:
            series.by_day[day] = value


def fetch_metric(
    client,
    metric_name: str,
    start: date,
    end: date,
    tz: ZoneInfo,
    requested_days: int | None = None,
) -> MetricSeries:
    """Fetch one metric. Never raises for API problems — returns ERROR state.

    `requested_days` is the span the caller actually asked about, which is not
    always the span being fetched: both tools widen the window backwards to give
    the baseline something trailing to work with. Warm-up is judged against the
    question, not against the widened window — otherwise a 7-day request became
    a 37-day one and no metric could ever be reported as warming up.
    """
    metric = get_metric(metric_name)
    series = MetricSeries(metric=metric)

    try:
        if metric.method == "dailyRollUp":
            points = client.daily_rollup(metric.data_type, start, end)
        else:
            points = client.list_data_points(
                metric.data_type, filter_expr=build_filter(metric, start, end, tz)
            )
    except ApiError as exc:
        series.state = ResultState.ERROR
        series.message = str(exc)
        return series

    series.truncated = bool(getattr(points, "truncated", False))
    series.truncation_reason = getattr(points, "truncation_reason", None)

    _accumulate(series, points)

    if not series.by_day:
        window_days = (
            requested_days if requested_days is not None else (end - start).days + 1
        )
        if metric.warmup_nights and window_days <= metric.warmup_nights * 2:
            series.state = ResultState.WARMING_UP
            series.message = (
                f"{metric.name} needs about {metric.warmup_nights} nights of wear "
                "before Fitbit computes it. No values yet for this range."
            )
        else:
            series.state = ResultState.NO_DATA
            series.message = (
                f"No {metric.name} recorded between {start.isoformat()} and "
                f"{end.isoformat()}. The band may not have been worn, or may not "
                "have synced."
            )

    return series
