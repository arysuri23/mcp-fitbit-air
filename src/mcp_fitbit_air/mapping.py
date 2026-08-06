"""Metric registry: friendly name -> Google Health API specifics.

Single source of truth. Every value here was verified against the live API by
the Phase 0 spike; see docs/superpowers/plans/phase0-findings.md.

Three things about this API make a naive mapping wrong:

1. Filters come in per-type dialects, and the WRONG ONE FAILS SILENTLY —
   HTTP 200 with zero data points, indistinguishable from "no data". The
   `daily-*` types filter on a `date` member; sleep filters on
   `civil_end_time`; steps and heart-rate only work with physical
   (RFC-3339) time.
2. Integers arrive as JSON strings ("8630", "61"), so every value needs
   coercion.
3. Two metrics are not returned at all and must be derived: skin temperature
   deviation (nightly minus baseline) and Active Zone Minutes (Fitbit weights
   cardio and peak double).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any


class UnknownMetricError(Exception):
    """Raised when a caller names a metric that is not registered."""


def coerce_number(value: Any) -> float | None:
    """Coerce an API value to a float. Integers arrive as JSON strings.

    Before Fitbit has established a baseline (e.g. skin temperature in the
    first few nights of wear), the API returns the literal JSON string
    "NaN" rather than omitting the field. `float("NaN")` and
    `float("Infinity")` both parse successfully in Python but are not valid
    JSON numbers — a bare NaN/Infinity would corrupt the MCP stdio JSON
    stream if it ever reached `json.dumps`. Treat all non-finite results as
    "no value" rather than let them propagate.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def dig(point: dict, *path: str) -> Any:
    """Walk a nested dict, returning None if any step is missing."""
    node: Any = point
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _scalar(*path: str) -> Callable[[dict], float | None]:
    return lambda point: coerce_number(dig(point, *path))


def _civil_date_at(*path: str) -> Callable[[dict], date | None]:
    """Read a {year, month, day} object at `path`."""

    def read(point: dict) -> date | None:
        node = dig(point, *path)
        if not isinstance(node, dict):
            return None
        try:
            return date(int(node["year"]), int(node["month"]), int(node["day"]))
        except (KeyError, TypeError, ValueError):
            return None

    return read


def _offset_date_at(
    time_path: tuple[str, ...], offset_path: tuple[str, ...]
) -> Callable[[dict], date | None]:
    """Derive the local calendar date from an RFC-3339 instant plus a UTC
    offset like "-14400s".

    Sleep intervals carry no civil times at all — only startTime/endTime and
    their offsets — so the local date has to be reconstructed.
    """

    def read(point: dict) -> date | None:
        stamp = dig(point, *time_path)
        if not isinstance(stamp, str):
            return None
        try:
            moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return None
        raw_offset = dig(point, *offset_path)
        seconds = 0
        if isinstance(raw_offset, str) and raw_offset.endswith("s"):
            try:
                seconds = int(raw_offset[:-1])
            except ValueError:
                seconds = 0
        return (moment + timedelta(seconds=seconds)).date()

    return read


def _first(*readers: Callable[[dict], Any]) -> Callable[[dict], Any]:
    """Try each reader in turn; return the first non-None result.

    Several Google Health data types return DIFFERENT payload shapes
    depending on whether they were fetched via `dailyRollUp` or `list` (see
    module docstring). The key names never collide between the two shapes
    for any metric we support, so trying each reader in order and taking
    the first hit is safe: at most one of them will ever find its keys.
    """

    def read(point: dict) -> Any:
        for reader in readers:
            result = reader(point)
            if result is not None:
                return result
        return None

    return read


def _skin_temperature_deviation(point: dict) -> float | None:
    """Derived: the API returns nightly and baseline, never the deviation."""
    payload = point.get("dailySleepTemperatureDerivations") or {}
    nightly = coerce_number(payload.get("nightlyTemperatureCelsius"))
    baseline = coerce_number(payload.get("baselineTemperatureCelsius"))
    if nightly is None or baseline is None:
        return None
    return round(nightly - baseline, 3)


_AZM_ROLLUP_KEYS = (
    "sumInFatBurnHeartZone",
    "sumInCardioHeartZone",
    "sumInPeakHeartZone",
)


def _active_zone_minutes(point: dict) -> float | None:
    """dailyRollUp shape: per-zone sums. Fitbit's headline AZM counts cardio
    and peak double, fat burn single.

    Must return None (not 0.0) when none of the rollup sum keys are present,
    so `_first` correctly falls through to the list-shape reader instead of
    stopping here with a false zero — the list shape's "activeZoneMinutes"
    payload has no sum* keys at all, only a single per-minute value.
    """
    payload = point.get("activeZoneMinutes")
    if not isinstance(payload, dict):
        return None
    if not any(key in payload for key in _AZM_ROLLUP_KEYS):
        return None
    fat = coerce_number(payload.get("sumInFatBurnHeartZone")) or 0.0
    cardio = coerce_number(payload.get("sumInCardioHeartZone")) or 0.0
    peak = coerce_number(payload.get("sumInPeakHeartZone")) or 0.0
    return fat + 2 * (cardio + peak)


# list shape: one per-minute record, weighted the same way Fitbit weights the
# rollup sums, so a summed series matches the rollup's headline number.
_AZM_ZONE_WEIGHT = {"FAT_BURN": 1.0, "CARDIO": 2.0, "PEAK": 2.0}


def _active_zone_minutes_list(point: dict) -> float | None:
    """list shape: a single per-minute record with a zone label, e.g.
    {"activeZoneMinutes": {"heartRateZone": "FAT_BURN",
                            "activeZoneMinutes": "1", "interval": {...}}}
    rather than the rollup's pre-summed-per-zone totals."""
    payload = point.get("activeZoneMinutes")
    if not isinstance(payload, dict):
        return None
    minutes = coerce_number(payload.get("activeZoneMinutes"))
    if minutes is None:
        return None
    weight = _AZM_ZONE_WEIGHT.get(payload.get("heartRateZone"))
    if weight is None:
        return None
    return minutes * weight


@dataclass(frozen=True)
class Metric:
    name: str
    data_type: str
    method: str  # "list" | "dailyRollUp"
    unit: str
    # AIP-160 member used for date filtering, and which literal format it needs.
    filter_member: str
    filter_dialect: str  # "civil_date" (bare YYYY-MM-DD) | "physical" (RFC-3339 Z)
    warmup_nights: int  # nights of wear before Fitbit computes this at all
    max_range_days: int
    supports_intraday: bool = False
    extract: Callable[[dict], float | None] = field(
        default=lambda _point: None, compare=False, repr=False
    )
    # Which local calendar day a data point belongs to. Not a single dotted
    # path: daily-* types carry a civil {year,month,day}, rollups carry one at
    # the top level, and sleep carries only an instant plus a UTC offset.
    date_of: Callable[[dict], "date | None"] = field(
        default=lambda _point: None, compare=False, repr=False
    )


METRICS: dict[str, Metric] = {
    "sleep_duration": Metric(
        name="sleep_duration",
        data_type="sleep",
        method="list",
        unit="minutes",  # sleep.summary.minutesAsleep is minutes, not seconds
        filter_member="sleep.interval.civil_end_time",
        filter_dialect="civil_date",
        warmup_nights=0,
        max_range_days=90,
        extract=_scalar("sleep", "summary", "minutesAsleep"),
        # Sleep intervals have no civil times; derive the day from the
        # end instant plus its offset, so a session is attributed to the
        # morning you woke up.
        date_of=_offset_date_at(
            ("sleep", "interval", "endTime"), ("sleep", "interval", "endUtcOffset")
        ),
    ),
    "resting_heart_rate": Metric(
        name="resting_heart_rate",
        data_type="daily-resting-heart-rate",
        method="list",
        unit="bpm",
        filter_member="daily_resting_heart_rate.date",
        filter_dialect="civil_date",
        warmup_nights=1,
        max_range_days=90,
        extract=_scalar("dailyRestingHeartRate", "beatsPerMinute"),
        date_of=_civil_date_at("dailyRestingHeartRate", "date"),
    ),
    "hrv": Metric(
        name="hrv",
        data_type="daily-heart-rate-variability",
        method="list",
        unit="milliseconds",
        filter_member="daily_heart_rate_variability.date",
        filter_dialect="civil_date",
        warmup_nights=3,
        max_range_days=90,
        extract=_scalar(
            "dailyHeartRateVariability", "averageHeartRateVariabilityMilliseconds"
        ),
        date_of=_civil_date_at("dailyHeartRateVariability", "date"),
    ),
    "spo2": Metric(
        name="spo2",
        data_type="daily-oxygen-saturation",
        method="list",
        unit="percent",
        filter_member="daily_oxygen_saturation.date",
        filter_dialect="civil_date",
        warmup_nights=1,
        max_range_days=90,
        extract=_scalar("dailyOxygenSaturation", "averagePercentage"),
        date_of=_civil_date_at("dailyOxygenSaturation", "date"),
    ),
    "skin_temperature_deviation": Metric(
        name="skin_temperature_deviation",
        data_type="daily-sleep-temperature-derivations",
        method="list",
        unit="celsius_deviation",
        filter_member="daily_sleep_temperature_derivations.date",
        filter_dialect="civil_date",
        warmup_nights=3,
        max_range_days=90,
        extract=_skin_temperature_deviation,
        date_of=_civil_date_at("dailySleepTemperatureDerivations", "date"),
    ),
    "steps": Metric(
        name="steps",
        data_type="steps",
        method="dailyRollUp",
        unit="count",
        filter_member="steps.interval.start_time",
        filter_dialect="physical",  # civil filters return 0 points, silently
        warmup_nights=0,
        max_range_days=90,
        supports_intraday=True,
        # dailyRollUp gives steps.countSum with civilStartTime at the top
        # level; the intraday `list` shape gives steps.count with the date
        # nested under steps.interval.civilStartTime instead. Both are live.
        extract=_first(
            _scalar("steps", "countSum"),  # dailyRollUp shape
            _scalar("steps", "count"),  # list (intraday) shape
        ),
        date_of=_first(
            _civil_date_at("civilStartTime", "date"),  # dailyRollUp: top-level
            _civil_date_at("steps", "interval", "civilStartTime", "date"),  # list
        ),
    ),
    "active_zone_minutes": Metric(
        name="active_zone_minutes",
        data_type="active-zone-minutes",
        method="dailyRollUp",
        unit="minutes",
        filter_member="active_zone_minutes.interval.start_time",
        filter_dialect="physical",
        warmup_nights=0,
        max_range_days=90,
        # dailyRollUp gives pre-summed-per-zone totals; the intraday `list`
        # shape gives one per-minute record per zone instead. Both are live.
        extract=_first(_active_zone_minutes, _active_zone_minutes_list),
        date_of=_first(
            _civil_date_at("civilStartTime", "date"),  # dailyRollUp: top-level
            _civil_date_at(
                "activeZoneMinutes", "interval", "civilStartTime", "date"
            ),  # list
        ),
    ),
    "heart_rate": Metric(
        name="heart_rate",
        data_type="heart-rate",
        method="dailyRollUp",
        unit="bpm",
        filter_member="heart_rate.sample_time.physical_time",
        filter_dialect="physical",
        warmup_nights=0,
        # The API caps heart-rate queries at 14 days, unlike the 90-day default.
        max_range_days=14,
        supports_intraday=True,
        # dailyRollUp gives heartRate.beatsPerMinuteAvg with civilStartTime
        # at the top level; the intraday `list` shape gives
        # heartRate.beatsPerMinute with the date nested under
        # heartRate.sampleTime.civilTime instead. Both are live.
        extract=_first(
            _scalar("heartRate", "beatsPerMinuteAvg"),  # dailyRollUp shape
            _scalar("heartRate", "beatsPerMinute"),  # list shape
        ),
        date_of=_first(
            _civil_date_at("civilStartTime", "date"),  # dailyRollUp: top-level
            _civil_date_at("heartRate", "sampleTime", "civilTime", "date"),  # list
        ),
    ),
}

# The columns returned by get_daily_summary, in display order.
SUMMARY_METRICS: list[str] = [
    "sleep_duration",
    "resting_heart_rate",
    "hrv",
    "steps",
    "active_zone_minutes",
    "spo2",
    "skin_temperature_deviation",
]


def get_metric(name: str) -> Metric:
    try:
        return METRICS[name]
    except KeyError:
        valid = ", ".join(sorted(METRICS))
        raise UnknownMetricError(
            f"Unknown metric {name!r}. Valid metrics are: {valid}"
        ) from None
