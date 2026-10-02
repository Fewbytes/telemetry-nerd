"""TDD for the mergeability table (spec §5 [SfE]/[H]): which statistics may be merged across
time or series, and the Hartmann caveat shown when a user overrides the guard anyway."""

import pytest

from telemetry_nerd.catalog.mergeability import (
    HARTMANN_CAVEAT,
    NATIVE_OP,
    check_aggregation,
    mergeability,
    normalize_op,
)


@pytest.mark.parametrize(
    ("statistic", "expected"),
    [
        ("count", "mergeable"),
        ("count_below", "mergeable"),
        ("sum", "mergeable"),
        ("min", "mergeable"),
        ("max", "mergeable"),
        ("mean", "needs_counts"),
        ("ratio", "needs_counts"),
        ("median", "non_mergeable"),
        ("percentile", "non_mergeable"),
        ("truncated_mean", "non_mergeable"),
        ("mad", "non_mergeable"),
        ("iqr", "non_mergeable"),
    ],
)
def test_mergeability_table(statistic, expected):
    assert mergeability(statistic) == expected


def test_unknown_statistic_rejected():
    with pytest.raises(ValueError):
        mergeability("not-a-statistic")


# -- mergeable statistics: only their native combining op is allowed ------------------------


@pytest.mark.parametrize(
    ("statistic", "op"), [("count", "sum"), ("sum", "sum"), ("min", "min"), ("max", "max")]
)
def test_mergeable_native_op_allowed(statistic, op):
    result = check_aggregation(statistic, op)
    assert not result.forbidden
    assert result.reason is None


@pytest.mark.parametrize(
    ("statistic", "op"),
    [
        ("count", "avg"),
        ("sum", "avg"),
        ("min", "avg"),
        ("max", "avg"),
        ("min", "sum"),
        ("max", "min"),
    ],
)
def test_mergeable_wrong_op_forbidden(statistic, op):
    result = check_aggregation(statistic, op)
    assert result.forbidden
    assert NATIVE_OP[statistic] in result.reason


# -- needs-counts statistics: plain avg/sum is wrong without weighting ----------------------


@pytest.mark.parametrize("statistic", ["mean", "ratio"])
def test_needs_counts_rejects_naive_average(statistic):
    result = check_aggregation(statistic, "avg")
    assert result.forbidden
    assert "count" in result.reason


def test_mean_allowed_when_count_weighted():
    assert not check_aggregation("mean", "count_weighted_mean").forbidden


def test_ratio_allowed_as_ratio_of_sums():
    assert not check_aggregation("ratio", "ratio_of_sums").forbidden


# -- non-mergeable statistics: every aggregation op is forbidden, always ---------------------


@pytest.mark.parametrize("op", ["avg", "sum", "max_over_time", "avg_over_time", "merge", "mean"])
@pytest.mark.parametrize("statistic", ["median", "percentile", "truncated_mean", "mad", "iqr"])
def test_non_mergeable_always_forbidden(statistic, op):
    result = check_aggregation(statistic, op)
    assert result.forbidden
    assert result.reason


def test_non_mergeable_forbidden_without_override_has_no_caveat():
    result = check_aggregation("percentile", "avg")
    assert result.forbidden
    assert result.caveat is None


def test_non_mergeable_override_renders_hartmann_caveat():
    result = check_aggregation("percentile", "avg", override=True)
    assert result.forbidden  # still flagged, just annotated instead of raised by the caller
    assert result.caveat == HARTMANN_CAVEAT


def test_hartmann_caveat_text_matches_guide_example():
    # exact numeric example from docs/telemetry-graphing-guide.md §5 [H]: regression fixture
    assert "60.3" in HARTMANN_CAVEAT
    assert "35.8" in HARTMANN_CAVEAT
    assert "68.5%" in HARTMANN_CAVEAT


def test_needs_counts_override_also_gets_caveat():
    result = check_aggregation("mean", "avg", override=True)
    assert result.forbidden
    assert result.caveat == HARTMANN_CAVEAT


def test_mergeable_wrong_op_override_also_gets_caveat():
    result = check_aggregation("max", "avg", override=True)
    assert result.forbidden
    assert result.caveat == HARTMANN_CAVEAT


# -- *_over_time spellings normalize to their bare counterpart ------------------------------


@pytest.mark.parametrize(
    ("op", "canonical"),
    [
        ("sum_over_time", "sum"),
        ("min_over_time", "min"),
        ("max_over_time", "max"),
        ("count_over_time", "sum"),
        ("avg_over_time", "avg"),
        ("quantile_over_time", "quantile"),
        ("sum", "sum"),  # unknown/already-canonical ops pass through unchanged
    ],
)
def test_normalize_op(op, canonical):
    assert normalize_op(op) == canonical


def test_max_over_time_is_the_native_op_for_max():
    assert not check_aggregation("max", "max_over_time").forbidden


def test_avg_over_time_is_forbidden_for_max():
    result = check_aggregation("max", "avg_over_time")
    assert result.forbidden
    assert "max" in result.reason
