import json
import math

from mcp_fitbit_air.baselines import Baseline, compute_baseline


def test_mean_of_values():
    baseline = compute_baseline([10.0, 20.0, 30.0])
    assert baseline.mean == 20.0
    assert baseline.n == 3


def test_none_values_are_excluded_from_the_mean():
    baseline = compute_baseline([10.0, None, 30.0])
    assert baseline.mean == 20.0
    assert baseline.n == 2


def test_empty_input_yields_no_mean():
    baseline = compute_baseline([])
    assert baseline.mean is None
    assert baseline.n == 0


def test_all_none_yields_no_mean():
    baseline = compute_baseline([None, None])
    assert baseline.mean is None
    assert baseline.n == 0


def test_sample_size_is_reported_so_small_samples_are_not_over_read():
    """With an account days old, a 4-day mean must not look like a 30-day one."""
    baseline = compute_baseline([50.0, 52.0, 48.0, 51.0])
    payload = baseline.to_dict()
    assert payload["n"] == 4
    assert payload["window_days"] == 30


def test_mean_is_rounded_to_one_decimal():
    baseline = compute_baseline([1.0, 2.0])
    assert baseline.mean == 1.5
    baseline = compute_baseline([1.0, 1.0, 2.0])
    assert baseline.mean == 1.3


def test_nan_values_are_excluded_from_the_mean():
    """NaN values must be filtered out like None, not allowed to poison the mean."""
    baseline = compute_baseline([10.0, float('nan'), 30.0])
    assert baseline.mean == 20.0
    assert baseline.n == 2


def test_positive_infinity_values_are_excluded_from_the_mean():
    """Positive infinity values must be filtered out, not included in averaging."""
    baseline = compute_baseline([10.0, float('inf')])
    assert baseline.mean == 10.0
    assert baseline.n == 1


def test_negative_infinity_and_nan_yield_no_mean_when_alone():
    """All non-finite values should result in empty baseline like all-None input."""
    baseline = compute_baseline([float('nan'), float('-inf')])
    assert baseline.mean is None
    assert baseline.n == 0


def test_computed_baseline_is_json_serialisable():
    """Computed baseline must be JSON-serialisable with allow_nan=False.

    This is critical: baselines are embedded in MCP stdio protocol stream.
    A single NaN or Infinity would produce invalid JSON and break the message.
    """
    baseline = compute_baseline([1.0, float('nan')])
    # This should not raise; the baseline must be JSON-serialisable
    result = json.dumps(baseline.to_dict(), allow_nan=False)
    assert isinstance(result, str)
    assert "null" in result or "1" in result  # mean is either None or 1.0
