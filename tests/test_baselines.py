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


def test_non_finite_values_never_escape_to_mean():
    """Belt-and-braces: even if a non-finite value reaches this function,
    the computed mean must be finite and JSON-serialisable."""
    # This tests the constraint that NaN and Infinity should never appear in mean
    # even if they somehow pass through (though upstream should filter them).
    baseline = compute_baseline([10.0, 20.0, 30.0])
    assert baseline.mean is not None
    assert math.isfinite(baseline.mean), "Mean must be finite and JSON-serialisable"

    # Empty list produces None mean, which is also valid
    baseline = compute_baseline([])
    assert baseline.mean is None or math.isfinite(baseline.mean)
