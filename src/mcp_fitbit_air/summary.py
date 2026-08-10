"""Build the per-day summary table.

Two things here are less obvious than they look.

Metrics are fetched concurrently: seven sequential round trips would make the
most common tool call the slowest one.

And the fetch window reaches back past the requested range. A baseline is only
worth reading if it is *trailing* — "9,000 steps against a 30-day mean of
11,200" is a finding, while "9,000 steps against the mean of the same three
days you asked about" is nearly circular. The lookback costs no extra requests,
only a wider start on the ones already being made, and it is truncated so the
widened window never exceeds what the API will accept for the narrowest metric
in the set.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from .baselines import BASELINE_WINDOW_DAYS, compute_baseline
from .fetch import MetricSeries, fetch_metric
from .mapping import get_metric
from .results import ResultState

logger = logging.getLogger(__name__)

MAX_SUMMARY_DAYS = 90
BASELINE_LOOKBACK_DAYS = BASELINE_WINDOW_DAYS


def _days_in(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _fetch_start(start: date, end: date, metric_names: list[str]) -> date:
    """How far back to reach for baseline history.

    Every metric has its own per-request range cap, and the whole set is fetched
    over one shared window, so the budget is the narrowest cap in the set. What
    is left after the requested range is what the lookback may use.
    """
    span = (end - start).days + 1
    budget = min(
        (get_metric(name).max_range_days for name in metric_names),
        default=MAX_SUMMARY_DAYS,
    )
    lookback = max(0, min(BASELINE_LOOKBACK_DAYS, budget - span))
    return start - timedelta(days=lookback)


def build_summary(
    client, start: date, end: date, metric_names: list[str], tz: ZoneInfo
) -> dict:
    if end < start:
        raise ValueError(
            f"End date {end.isoformat()} is before start date {start.isoformat()}."
        )

    span = (end - start).days + 1
    if span > MAX_SUMMARY_DAYS:
        raise ValueError(
            f"Range of {span} days exceeds the {MAX_SUMMARY_DAYS}-day maximum for a "
            "daily summary. Request a narrower range, or use get_metric_series for a "
            "single metric."
        )

    fetch_start = _fetch_start(start, end, metric_names)
    window_days = (end - fetch_start).days + 1

    with ThreadPoolExecutor(max_workers=len(metric_names) or 1) as pool:
        series_list: list[MetricSeries] = list(
            pool.map(
                lambda name: fetch_metric(client, name, fetch_start, end, tz),
                metric_names,
            )
        )
    series_by_name = {s.metric.name: s for s in series_list}

    # Computed over the widened window, including the lookback days that are
    # never emitted as rows.
    baselines = {
        name: compute_baseline(list(s.by_day.values()), window_days=window_days)
        for name, s in series_by_name.items()
    }

    days = []
    for day in _days_in(start, end):
        row: dict = {"date": day.isoformat(), "metrics": {}}
        for name in metric_names:
            series = series_by_name[name]
            metric = series.metric

            if series.state is ResultState.ERROR:
                row["metrics"][name] = {"state": "error", "message": series.message}
                continue

            value = series.by_day.get(day)
            if value is None:
                if series.state is ResultState.WARMING_UP:
                    row["metrics"][name] = {
                        "state": "warming_up",
                        "message": series.message,
                    }
                else:
                    row["metrics"][name] = {
                        "state": "no_data",
                        "message": f"No {name} recorded for {day.isoformat()}.",
                    }
                continue

            row["metrics"][name] = {
                "state": "ok",
                "value": value,
                "unit": metric.unit,
                "baseline": baselines[name].to_dict(),
            }
        days.append(row)

    metric_status = {
        name: {"state": s.state.value, "message": s.message}
        for name, s in series_by_name.items()
    }

    return {
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "baseline_window": {
            "start": fetch_start.isoformat(),
            "end": end.isoformat(),
            "days": window_days,
        },
        "days": days,
        "metric_status": metric_status,
    }
