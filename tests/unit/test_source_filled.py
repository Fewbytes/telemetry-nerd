"""bucket_state honesty: unobservable expression counts (1h9.15) and the post-gap increase
spike of previous-sample backends (1h9.13)."""

import pyarrow as pa
import pytest

from telemetry_nerd.core.coverage_check import claim_coverage
from telemetry_nerd.core.summary import summarize
from telemetry_nerd.datasets.store import DatasetMeta
from telemetry_nerd.model.bucket_state import Flag, State, coarsen
from telemetry_nerd.model.caveats import UNOBSERVABLE_MESSAGE, from_bucket_state
from telemetry_nerd.model.companions import derive_states, post_gap_buckets, previous_sample_windows
from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult
from telemetry_nerd.sources.semantics import semantics_for
from tests.unit.fakes import FakeSource, make_service

STEP = 60_000
HOLE = {3, 4, 5}  # buckets without samples (an outage)
VM = {"post_gap_increase_spike": True}


def meta(expr, flags=None):
    return DatasetMeta(
        id="d1", source="s", expr=expr, start_ms=STEP, end_ms=8 * STEP, step_ms=STEP,
        resolution_ms=15_000, semantics_flags=flags or {},
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


@pytest.mark.parametrize("expr", ["x / on(a) y", "x > 0", "topk(3, x)", "x offset 5m"])
def test_unobservable_expression_is_all_unknown_and_source_filled(expr):
    m, r = meta(expr), result(hole=())
    assert set(states_of(m, r)) == {int(State.UNKNOWN)}
    assert set(flags_of(m, r)) == {int(Flag.SOURCE_FILLED)}


def test_unobservable_expression_in_presence_mode_too():
    m = DatasetMeta(**{**meta("x / on(a) y").__dict__, "representation": "quantile"})
    assert set(states_of(m, result())) == {int(State.UNKNOWN)}


def test_observable_expression_keeps_count_based_states():
    m = meta("rate(x[5m])")
    assert states_of(m, result()) == [0, 0, 2, 2, 2, 0, 0, 0]
    assert set(flags_of(m, result())) == {0}


def test_ratio_between_observables_keeps_count_based_states():
    m = meta("sum(rate(a[5m])) / sum(rate(b[5m]))")
    assert states_of(m, result()) == [0, 0, 2, 2, 2, 0, 0, 0]
    assert set(flags_of(m, result())) == {0}


def test_unobservable_caveat_is_one_dataset_level_reason():
    m, r = meta("x / on(a) y"), result(hole=())
    [c] = from_bucket_state(derive_states(m, r), {"a": "a"}, STEP)
    assert (c.code, c.message) == ("untrusted_data", UNOBSERVABLE_MESSAGE)
    assert "subquery fills gaps" in c.message
    assert c.where.series is None  # every series: no per-series list


def test_summary_reports_one_unknown_span_for_the_window():
    m, r = meta("x / on(a) y"), result(hole=())
    out = summarize(m, r, now_ms=10**12, settle_ms=0, states=derive_states(m, r))
    assert len(out["unknown_spans"]) == 1
    assert "untrusted_data" in out["caveats"]
    assert "unobservable_counts" in out["caveats"]
    assert out["series"][0]["coverage"] == {"pct": None, "missing": None, "longest_gap": None}


def test_claims_on_unobservable_expression_are_blocked_and_say_how_to_rephrase():
    m, r = meta("x / on(a) y"), result(hole=())
    [c] = claim_coverage(derive_states(m, r), STEP, 8 * STEP, STEP)
    assert (c.code, c.severity) == ("untrusted_data", "blocks_claim")
    assert "subquery fills gaps" in c.message
    assert "instead of retrying" in c.message


# --- 1h9.13 -----------------------------------------------------------------------------------


def test_vm_post_gap_bucket_is_flagged_but_keeps_its_state():
    m = meta("increase(x[1m])", VM)
    assert states_of(m, result()) == [0, 0, 2, 2, 2, 0, 0, 0]
    assert flags_of(m, result()) == [0, 0, 0, 0, 0, int(Flag.POST_GAP), 0, 0]


def test_post_gap_caveat_is_located_on_the_bucket():
    m, r = meta("rate(x[1m])", VM), result()
    codes = {c.code: c for c in from_bucket_state(derive_states(m, r), {"a": "{n}"}, STEP)}
    spike = codes["post_gap_spike"]
    assert spike.severity == "warn"
    assert spike.where.spans == [(5 * STEP, 6 * STEP)]
    assert spike.where.series == ["a"]
    assert "not a real spike" in spike.message


def test_post_gap_after_unknown_span_is_flagged_too():
    m = meta("increase(x[1m])", VM)
    m = DatasetMeta(**{**m.__dict__, "failed_spans": [[3 * STEP, 4 * STEP, "boom"]]})
    r = result(hole=(3, 4))
    assert flags_of(m, r)[4] == int(Flag.POST_GAP)


def test_prometheus_semantics_never_flag():
    m = meta("increase(x[1m])", {})
    assert set(flags_of(m, result())) == {0}


def test_no_gap_no_flag():
    m = meta("increase(x[1m])", VM)
    assert set(flags_of(m, result(hole=()))) == {0}


def test_series_start_is_not_a_gap():
    m = meta("increase(x[1m])", VM)
    r = result(hole={1, 2, 3})  # absent until bucket 4
    assert set(flags_of(m, r)) == {0}


def test_other_expressions_are_not_flagged_on_vm():
    m = meta("max_over_time(x[1m])", VM)
    assert set(flags_of(m, result())) == {0}


@pytest.mark.parametrize(
    ("expr", "hit"),
    [
        ("increase(x[5m])", True),
        ("sum by (a) (rate(x[1m]))", True),
        ("irate(x[1m])", False),
        ("sum_rate(x)", False),
        ("up", False),
        ('up{job="rate("}', False),
    ],
)
def test_previous_sample_functions(expr, hit):
    assert bool(previous_sample_windows(expr)) is hit


@pytest.mark.parametrize(
    ("expr", "n"),
    [
        ("increase(x[1m])", 1),
        ("increase(x[5m])", 5),
        ("sum(rate(x[150s]))", 3),
        ("increase(x[1h30m])", 1),  # unparsable range: first bucket only
        ("up", 0),
    ],
)
def test_post_gap_bucket_count_follows_the_range(expr, n):
    assert post_gap_buckets(expr, STEP) == n


def test_wide_window_flags_every_bucket_that_reaches_over_the_gap_up_to_the_next_gap():
    m = meta("increase(x[3m])", VM)
    r = result(hole={3, 4, 6})  # buckets 5 and 7, 8 follow gaps
    f = flags_of(m, r)
    post = int(Flag.POST_GAP)
    assert f == [0, 0, 0, 0, post, 0, post, post]
    r = result(hole={2})  # 3 buckets after the gap, bounded by the window
    assert flags_of(m, r) == [0, 0, post, post, post, 0, 0, 0]


def test_coarsened_post_gap_with_failed_span_keeps_the_failure_reason():
    base = meta("increase(x[1m])", VM)
    failed = [(4 * STEP, 4 * STEP, "boom")]
    m = DatasetMeta(**{**base.__dict__, "failed_spans": [list(f) for f in failed]})
    # bucket 4 failed, bucket 5 is post-gap; both coarsen into the bucket ending at 6
    states = coarsen(derive_states(m, result(hole={4})), 3 * STEP)
    caveats = from_bucket_state(states, {"a": "a"}, 3 * STEP, failed)
    [untrusted] = [c for c in caveats if c.code == "untrusted_data"]
    assert "boom" in untrusted.message and "cannot be observed" not in untrusted.message
    # that coarse bucket is UNKNOWN: no spike claim for it
    assert "post_gap_spike" not in {c.code for c in caveats}
    # a post-gap bucket in a coarse bucket that is not UNKNOWN still gets its caveat
    states = coarsen(derive_states(meta("increase(x[1m])", VM), result(hole={3, 4, 5})), 3 * STEP)
    assert "post_gap_spike" in {c.code for c in from_bucket_state(states, {"a": "a"}, 3 * STEP)}


def test_failed_span_on_an_unobservable_expression_names_both():
    base = meta("x / on(a) y")
    m = DatasetMeta(**{**base.__dict__, "failed_spans": [[3 * STEP, 4 * STEP, "boom"]]})
    r = result(hole=())
    [c] = from_bucket_state(derive_states(m, r), {"a": "a"}, STEP, [(3 * STEP, 4 * STEP, "boom")])
    assert "cannot be observed" in c.message and "boom" in c.message


def test_claim_over_post_gap_value_warns():
    m, r = meta("increase(x[1m])", VM), result()
    [c] = [
        c
        for c in claim_coverage(derive_states(m, r), STEP, 8 * STEP, STEP)
        if c.code == "post_gap_spike"
    ]
    assert c.severity == "warn"
    assert c.where.spans == [(5 * STEP, 6 * STEP)]
    assert not [
        c
        for c in claim_coverage(derive_states(m, r), 6 * STEP, 8 * STEP, STEP)
        if c.code == "post_gap_spike"
    ]


def test_summary_carries_post_gap_spike_caveat():
    m, r = meta("increase(x[1m])", VM), result()
    out = summarize(m, r, now_ms=10**12, settle_ms=0, states=derive_states(m, r))
    assert "post_gap_spike" in out["caveats"]


class _Sourced(FakeSource):
    def __init__(self, backend):
        super().__init__()
        self.semantics = semantics_for(backend)


@pytest.mark.parametrize(("backend", "flags"), [("victoriametrics", VM), ("prometheus", {})])
async def test_query_records_semantics_hint_in_dataset_meta(tmp_path, backend, flags):
    svc = make_service(tmp_path, _Sourced(backend))
    ds = (await svc.query("rate(x[1m])", start="now-2h", end="now-1h", step="1m"))["dataset"]
    assert svc.datasets.meta(ds).semantics_flags == flags


def test_old_stored_meta_without_the_field_still_loads():
    old = {k: v for k, v in meta("up").to_dict().items() if k != "semantics_flags"}
    assert DatasetMeta(**old).semantics_flags == {}
