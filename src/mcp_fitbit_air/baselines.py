"""Trailing baselines.

"HRV 42ms" is noise; "HRV 42ms against a 58ms baseline" is a finding. Every
baseline carries its sample size, so that a 4-day mean from a new account is
not mistaken for a settled 30-day one.
"""

from __future__ import annotations

from dataclasses import dataclass

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
    present = [v for v in values if v is not None]
    if not present:
        return Baseline(mean=None, n=0, window_days=window_days)
    return Baseline(
        mean=round(sum(present) / len(present), 1),
        n=len(present),
        window_days=window_days,
    )
