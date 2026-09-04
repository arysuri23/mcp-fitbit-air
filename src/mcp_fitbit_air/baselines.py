"""Trailing baselines.

"HRV 42ms" is noise; "HRV 42ms against a 58ms baseline" is a finding. Every
baseline carries its sample size, so that a 4-day mean from a new account is
not mistaken for a settled 30-day one.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from .mapping import get_metric

BASELINE_WINDOW_DAYS = 30


@dataclass(frozen=True)
class Baseline:
    mean: float | None
    n: int
    window_days: int

    def to_dict(self) -> dict:
        return {"mean": self.mean, "n": self.n, "window_days": self.window_days}


def compute_baseline(
    values: list[float | None], window_days: int = BASELINE_WINDOW_DAYS
) -> Baseline:
    present = [v for v in values if v is not None and math.isfinite(v)]
    if not present:
        return Baseline(mean=None, n=0, window_days=window_days)
    return Baseline(
        mean=round(sum(present) / len(present), 1),
        n=len(present),
        window_days=window_days,
    )


def lookback_start(start: date, end: date, metric_names: Sequence[str]) -> date:
    """How far back to fetch so a baseline is genuinely trailing.

    A mean taken over only the days the caller asked about is close to circular
    — "Tuesday was above the average of Monday through Wednesday" says little.
    So the fetch window is widened backwards; callers still emit rows only for
    the requested range.

    This costs no extra requests, only an earlier start on the ones already
    being made, but it must respect the API's per-request range cap. Every
    metric has its own cap and a caller may fetch several over one shared
    window, so the budget is the narrowest cap in the set, and whatever it
    leaves after the requested range is what the lookback may use.
    """
    span = (end - start).days + 1
    caps = [get_metric(name).max_range_days for name in metric_names]
    budget = min(caps) if caps else span + BASELINE_WINDOW_DAYS
    lookback = max(0, min(BASELINE_WINDOW_DAYS, budget - span))
    return start - timedelta(days=lookback)
