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

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from .baselines import BASELINE_WINDOW_DAYS, compute_baseline, lookback_start
from .fetch import MetricSeries, fetch_metric
from .results import ResultState

MAX_SUMMARY_DAYS = 90
BASELINE_LOOKBACK_DAYS = BASELINE_WINDOW_DAYS


def baseline_window(fetch_start: date, start: date, end: date, window_days: int) -> dict:
    """Describe the window the baseline was computed over.

    `trailing_days` is the part that precedes the requested range, and it is the
    number that decides whether the baseline is a comparison at all. It reaches
    zero whenever the requested span already fills the metric's API range cap,
    and at that point the mean is taken over the very days being displayed — so
    the payload says so rather than leaving the reader to work it out from three
    dates.
    """
    trailing = (start - fetch_start).days
    window = {
        "start": fetch_start.isoformat(),
        "end": end.isoformat(),
        "days": window_days,
        "trailing_days": trailing,
    }
    if trailing == 0:
        window["note"] = (
            "No days precede the requested range, so this baseline is the mean of "
            "the same days shown here rather than an independent comparison."
        )
    return window


def _days_in(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


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

    fetch_start = lookback_start(start, end, metric_names)
    window_days = (end - fetch_start).days + 1

    with ThreadPoolExecutor(max_workers=len(metric_names) or 1) as pool:
        series_list: list[MetricSeries] = list(
            pool.map(
                lambda name: fetch_metric(
                    client, name, fetch_start, end, tz, requested_days=span
                ),
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
        name: {
            "state": s.state.value,
            "message": s.message,
            # Present only when it happened, so a clean row stays quiet.
            **({"truncated": True, "reason": s.truncation_reason} if s.truncated else {}),
            **({"remedy": s.remedy} if s.remedy else {}),
        }
        for name, s in series_by_name.items()
    }

    return {
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "baseline_window": baseline_window(fetch_start, start, end, window_days),
        "days": days,
        "metric_status": metric_status,
    }
