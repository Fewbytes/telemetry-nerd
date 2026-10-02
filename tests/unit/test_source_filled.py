"""bucket_state honesty: unobservable expression counts are unknown + source_filled (1h9.15)."""

import pyarrow as pa
import pytest

from telemetry_nerd.core.coverage_check import claim_coverage
from telemetry_nerd.core.summary import summarize
from telemetry_nerd.datasets.store import DatasetMeta
from telemetry_nerd.model.bucket_state import Flag, State
from telemetry_nerd.model.caveats import UNOBSERVABLE_MESSAGE, from_bucket_state
from telemetry_nerd.model.companions import derive_states
from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult

STEP = 60_000
HOLE = {3, 4, 5}  # buckets without samples (an outage)


def meta(expr):
    return DatasetMeta(
        id="d1", source="s", expr=expr, start_ms=STEP, end_ms=8 * STEP, step_ms=STEP,
        resolution_ms=15_000,
    )  # fmt: skip


def result(hole=HOLE):
    ts = [k * STEP for k in range(1, 9) if k not in hole]
    buckets = pa.table(
        {"ts_ms": ts, "series_id": ["a"] * len(ts), "avg": [15.0] * len(ts),
         "min": [15.0] * len(ts), "max": [15.0] * len(ts), "count": [4] * len(ts)},
        schema=BUCKET_SCHEMA,
    )  # fmt: skip
    series = pa.table({"series_id": ["a"], "labels": ['{"n":"a"}']}, schema=SERIES_SCHEMA)
    return FetchResult(buckets, series)


def states_of(m, r):
    return derive_states(m, r)["state"].to_pylist()


def flags_of(m, r):
    return derive_states(m, r)["flags"].to_pylist()


# --- 1h9.15 -----------------------------------------------------------------------------------


@pytest.mark.parametrize("expr", ["x / y", "x > 0", "topk(3, x)", "x offset 5m"])
def test_unobservable_expression_is_all_unknown_and_source_filled(expr):
    m, r = meta(expr), result(hole=())
    assert set(states_of(m, r)) == {int(State.UNKNOWN)}
    assert set(flags_of(m, r)) == {int(Flag.SOURCE_FILLED)}


def test_unobservable_expression_in_presence_mode_too():
    m = DatasetMeta(**{**meta("x / y").__dict__, "representation": "quantile"})
    assert set(states_of(m, result())) == {int(State.UNKNOWN)}


def test_observable_expression_keeps_count_based_states():
    m = meta("rate(x[5m])")
    assert states_of(m, result()) == [0, 0, 2, 2, 2, 0, 0, 0]
    assert set(flags_of(m, result())) == {0}


def test_unobservable_caveat_is_one_dataset_level_reason():
    m, r = meta("x / y"), result(hole=())
    [c] = from_bucket_state(derive_states(m, r), {"a": "a"}, STEP)
    assert (c.code, c.message) == ("untrusted_data", UNOBSERVABLE_MESSAGE)
    assert "subquery fills gaps" in c.message
    assert c.where.series is None  # every series: no per-series list


def test_summary_reports_one_unknown_span_for_the_window():
    m, r = meta("x / y"), result(hole=())
    out = summarize(m, r, now_ms=10**12, settle_ms=0, states=derive_states(m, r))
    assert len(out["unknown_spans"]) == 1
    assert "untrusted_data" in out["caveats"]


def test_claims_on_unobservable_expression_are_blocked_and_say_how_to_rephrase():
    m, r = meta("x / y"), result(hole=())
    [c] = claim_coverage(derive_states(m, r), STEP, 8 * STEP, STEP)
    assert (c.code, c.severity) == ("untrusted_data", "blocks_claim")
    assert "subquery fills gaps" in c.message
    assert "instead of retrying" in c.message
