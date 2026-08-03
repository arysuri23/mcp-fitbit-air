import math
from datetime import date

import pytest

from mcp_fitbit_air.mapping import (
    METRICS,
    SUMMARY_METRICS,
    UnknownMetricError,
    coerce_number,
    get_metric,
)


def test_get_metric_returns_known_metric():
    metric = get_metric("steps")
    assert metric.data_type == "steps"
    assert metric.method == "dailyRollUp"


def test_unknown_metric_error_lists_valid_options():
    with pytest.raises(UnknownMetricError) as exc:
        get_metric("bogus")

    message = str(exc.value)
    assert "bogus" in message
    assert "steps" in message
    assert "hrv" in message


def test_all_summary_metrics_are_registered():
    for name in SUMMARY_METRICS:
        assert name in METRICS, f"{name} in SUMMARY_METRICS but not in METRICS"


def test_summary_covers_the_seven_spec_metrics():
    assert set(SUMMARY_METRICS) == {
        "sleep_duration",
        "resting_heart_rate",
        "hrv",
        "steps",
        "active_zone_minutes",
        "spo2",
        "skin_temperature_deviation",
    }


def test_every_metric_declares_a_supported_method():
    for name, metric in METRICS.items():
        assert metric.method in {"list", "dailyRollUp"}, name


def test_list_metrics_declare_a_filter_member_and_dialect():
    for name, metric in METRICS.items():
        if metric.method == "list":
            assert metric.filter_member, f"{name} uses list but declares no filter_member"
            assert metric.filter_dialect in {"civil_date", "physical"}, name


def test_heart_rate_range_limit_is_fourteen_days():
    """The API caps heart-rate queries at 14 days, unlike the 90-day default."""
    assert get_metric("heart_rate").max_range_days == 14


def test_daily_metrics_cap_at_ninety_days():
    assert get_metric("sleep_duration").max_range_days == 90
    assert get_metric("hrv").max_range_days == 90


def test_only_heart_rate_and_steps_support_intraday():
    intraday = {name for name, m in METRICS.items() if m.supports_intraday}
    assert intraday == {"heart_rate", "steps"}


# --- The Phase 0 spike proved every value below. These tests encode the real
# --- response shapes; a regression here means the API contract moved.


def test_coerce_number_handles_json_strings():
    """Integers arrive as JSON strings throughout the API."""
    assert coerce_number("8630") == 8630.0
    assert coerce_number(42) == 42.0
    assert coerce_number(35.5) == 35.5
    assert coerce_number(None) is None
    assert coerce_number("not a number") is None


def test_coerce_number_rejects_the_string_nan():
    """The API returns the literal JSON string "NaN" for
    baselineTemperatureCelsius before Fitbit has established a baseline —
    this is a real captured value (spike/raw/
    list_daily-sleep-temperature-derivations_nofilter.json), not a
    hypothetical. float("NaN") parses successfully in Python but is not
    valid JSON, so it must be rejected here rather than allowed to poison
    downstream averages or reach json.dumps."""
    assert coerce_number("NaN") is None


def test_coerce_number_rejects_infinity_strings():
    assert coerce_number("Infinity") is None
    assert coerce_number("-Infinity") is None


def test_coerce_number_rejects_a_raw_nan_float():
    assert coerce_number(float("nan")) is None


def test_sleep_duration_reads_minutes_asleep_as_a_number():
    point = {"sleep": {"summary": {"minutesAsleep": "385", "minutesAwake": "5"}}}
    assert get_metric("sleep_duration").extract(point) == 385.0


def test_sleep_duration_unit_is_minutes_not_seconds():
    assert get_metric("sleep_duration").unit == "minutes"


def test_resting_heart_rate_coerces_its_string_value():
    point = {"dailyRestingHeartRate": {"beatsPerMinute": "58"}}
    assert get_metric("resting_heart_rate").extract(point) == 58.0


def test_hrv_reads_the_average_millisecond_field():
    point = {
        "dailyHeartRateVariability": {
            "averageHeartRateVariabilityMilliseconds": 42.5,
            "entropy": 1.0,
        }
    }
    assert get_metric("hrv").extract(point) == 42.5


def test_spo2_reads_average_percentage():
    point = {"dailyOxygenSaturation": {"averagePercentage": 95.8, "lowerBoundPercentage": 93.7}}
    assert get_metric("spo2").extract(point) == 95.8


def test_skin_temperature_deviation_is_nightly_minus_baseline():
    """The API returns no deviation field; it is derived."""
    point = {
        "dailySleepTemperatureDerivations": {
            "nightlyTemperatureCelsius": 34.5,
            "baselineTemperatureCelsius": 34.0,
        }
    }
    assert get_metric("skin_temperature_deviation").extract(point) == pytest.approx(0.5)


def test_skin_temperature_deviation_needs_both_halves():
    point = {"dailySleepTemperatureDerivations": {"nightlyTemperatureCelsius": 34.5}}
    assert get_metric("skin_temperature_deviation").extract(point) is None


def test_skin_temperature_deviation_is_none_when_baseline_is_the_string_nan():
    """Real captured shape (spike/raw/
    list_daily-sleep-temperature-derivations_nofilter.json, 2026-07-30):
    before a baseline is established, baselineTemperatureCelsius arrives as
    the JSON string "NaN" rather than being omitted. Must resolve to None,
    not a NaN float."""
    point = {
        "dailySleepTemperatureDerivations": {
            "date": {"year": 2026, "month": 7, "day": 30},
            "nightlyTemperatureCelsius": 33.25283400809718,
            "baselineTemperatureCelsius": "NaN",
        }
    }
    assert get_metric("skin_temperature_deviation").extract(point) is None


def test_steps_reads_rollup_count_sum():
    point = {"steps": {"countSum": "8630"}}
    assert get_metric("steps").extract(point) == 8630.0


def test_active_zone_minutes_uses_fitbit_weighting():
    """Fitbit counts cardio and peak double; fat burn single."""
    point = {
        "activeZoneMinutes": {
            "sumInFatBurnHeartZone": "10",
            "sumInCardioHeartZone": "5",
            "sumInPeakHeartZone": "2",
        }
    }
    assert get_metric("active_zone_minutes").extract(point) == 10 + 2 * (5 + 2)


def test_active_zone_minutes_treats_absent_zones_as_zero():
    point = {"activeZoneMinutes": {"sumInFatBurnHeartZone": "1"}}
    assert get_metric("active_zone_minutes").extract(point) == 1.0


def test_extract_returns_none_when_the_payload_is_missing():
    for name in SUMMARY_METRICS:
        assert get_metric(name).extract({}) is None, name


def test_daily_metrics_filter_on_the_date_member():
    """daily-* points carry a `date` object, not an `interval`."""
    assert get_metric("hrv").filter_member == "daily_heart_rate_variability.date"
    assert get_metric("hrv").filter_dialect == "civil_date"


def test_sleep_filters_on_civil_end_time():
    assert get_metric("sleep_duration").filter_member == "sleep.interval.civil_end_time"
    assert get_metric("sleep_duration").filter_dialect == "civil_date"


def test_heart_rate_filters_on_physical_time():
    """Civil-time filters on heart-rate return 200 with zero points — silently
    wrong. Physical time is the only reliable dialect for this type."""
    assert get_metric("heart_rate").filter_member == "heart_rate.sample_time.physical_time"
    assert get_metric("heart_rate").filter_dialect == "physical"


def test_steps_filters_on_physical_interval_time():
    assert get_metric("steps").filter_member == "steps.interval.start_time"
    assert get_metric("steps").filter_dialect == "physical"


def test_date_of_resolves_each_type_family():
    cases = {
        "hrv": {"dailyHeartRateVariability": {"date": {"year": 2026, "month": 8, "day": 1}}},
        "steps": {"civilStartTime": {"date": {"year": 2026, "month": 8, "day": 1}}},
        "heart_rate": {
            "heartRate": {"sampleTime": {"civilTime": {"date": {"year": 2026, "month": 8, "day": 1}}}}
        },
    }
    for name, point in cases.items():
        assert get_metric(name).date_of(point) == date(2026, 8, 1), name


def test_sleep_date_comes_from_the_end_instant_plus_offset():
    """Sleep intervals carry NO civil times — only instants and offsets."""
    point = {
        "sleep": {
            "interval": {"endTime": "2026-08-02T14:50:00Z", "endUtcOffset": "-14400s"}
        }
    }
    # 14:50Z minus 4h is 10:50 local on the same day.
    assert get_metric("sleep_duration").date_of(point) == date(2026, 8, 2)


def test_sleep_date_uses_local_time_not_utc():
    """A session ending 01:30Z with a -4h offset is still the previous evening
    locally, and must be attributed to that day."""
    point = {
        "sleep": {
            "interval": {"endTime": "2026-08-03T01:30:00Z", "endUtcOffset": "-14400s"}
        }
    }
    assert get_metric("sleep_duration").date_of(point) == date(2026, 8, 2)


def test_date_of_returns_none_for_unreadable_points():
    for name in SUMMARY_METRICS:
        assert get_metric(name).date_of({}) is None, name


def test_extract_never_returns_a_non_finite_float():
    """Regression guard: no metric's `extract` may return NaN or Infinity,
    which json.dumps would serialise as bare (invalid) JSON tokens onto the
    MCP stdio stream. Exercises every registered metric, including the two
    derived ones, against points shaped like real captured API payloads —
    both healthy values and the "NaN"-baseline case that triggered this
    guard."""
    points_by_name = {
        "sleep_duration": {"sleep": {"summary": {"minutesAsleep": "385"}}},
        "resting_heart_rate": {"dailyRestingHeartRate": {"beatsPerMinute": "58"}},
        "hrv": {
            "dailyHeartRateVariability": {
                "averageHeartRateVariabilityMilliseconds": 42.5
            }
        },
        "spo2": {"dailyOxygenSaturation": {"averagePercentage": 95.8}},
        "skin_temperature_deviation": {
            "dailySleepTemperatureDerivations": {
                "nightlyTemperatureCelsius": 33.25283400809718,
                "baselineTemperatureCelsius": "NaN",
            }
        },
        "steps": {"steps": {"countSum": "8630"}},
        "active_zone_minutes": {
            "activeZoneMinutes": {
                "sumInFatBurnHeartZone": "10",
                "sumInCardioHeartZone": "5",
                "sumInPeakHeartZone": "2",
            }
        },
        "heart_rate": {"heartRate": {"beatsPerMinute": "61"}},
    }
    assert set(points_by_name) == set(METRICS)
    for name, point in points_by_name.items():
        value = get_metric(name).extract(point)
        assert value is None or math.isfinite(value), name
