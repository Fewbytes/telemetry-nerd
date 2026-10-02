import dataclasses
from typing import get_args

import pytest

from telemetry_nerd.analysis.filters import Kind as FilterKind
from telemetry_nerd.devtools.synthetic import periodic_buckets
from telemetry_nerd.model.bucket_state import STATE_SCHEMA, State
from telemetry_nerd.model.companions import KINDS, OPS, SOURCE_AGGREGATED, dataset_bundle, policy
from telemetry_nerd.model.time import TimeRange
from tests.unit.fakes import NOW, make_service

DATASET_OPS = {"query", "query_distribution", "lod", "dist_rebucket", *get_args(FilterKind)}


def test_every_op_declares_every_companion_kind():
    for op in DATASET_OPS:
        for kind in KINDS:
            assert kind in OPS.get(op, {}), f"op {op!r} does not declare companion {kind!r}"


def test_undeclared_op_drops():
    assert policy("some_future_op", "bucket_state") == "drop"


@pytest.mark.parametrize(
    ("expr", "agg"),
    [
        ("sum by (job) (rate(x[5m]))", True),
        ("avg(up)", True),
        ("histogram_quantile(0.9, sum by (le) (rate(x[5m])))", True),
        ("sum(a) / sum(b)", True),
        ("(sum(x))", True),
        ('label_replace(sum(x), "a", "b", "c", "d")', True),
        ("rate(x[5m])", False),
        ("up", False),
        ("count_over_time(x[5m])", False),
        ("max_over_time(x[5m])", False),
    ],
)
def test_source_aggregation_detection(expr, agg):
    assert bool(SOURCE_AGGREGATED.search(expr)) is agg


async def test_query_dataset_derives_bucket_state(tmp_path):
    svc = make_service(tmp_path)
    ds = (await svc.query("up", start="now-2h", end="now-1h", step="1m"))["dataset"]
    meta, result = svc.datasets.get(ds)
    b = dataset_bundle(svc.datasets, meta, result)
    states = b.companions["bucket_state"]
    assert states.num_rows == result.buckets.num_rows  # full grid, both series
    assert set(states["state"].to_pylist()) == {State.OK}
    assert b.caveats == []


async def test_aggregated_query_carries_member_caveat(tmp_path):
    svc = make_service(tmp_path)
    ds = (await svc.query("sum(up)", start="now-2h", end="now-1h", step="1m"))["dataset"]
    meta, result = svc.datasets.get(ds)
    codes = [c.code for c in dataset_bundle(svc.datasets, meta, result).caveats]
    assert codes == ["member_coverage_unknown"]


M, DAY = 60_000, 86_400_000


def _filtered(svc):
    r = periodic_buckets(0, 4 * DAY, M, [(5 * M, 3, None), (DAY, 5, None)], noise=1, seed=1)
    d = svc.datasets.put(
        source="default", expr="queue_depth", rng=TimeRange(0, 4 * DAY), step_ms=M,
        resolution_ms=15_000, result=r, representation="bucket_agg",
    ).id  # fmt: skip
    return d, svc.filter(d, "lowpass", "1h", "trend")["dataset"]


def test_filtered_dataset_carries_source_states(tmp_path):
    svc = make_service(tmp_path)
    _src, f = _filtered(svc)
    meta, result = svc.datasets.get(f)
    b = dataset_bundle(svc.datasets, meta, result)
    states = b.companions["bucket_state"]
    assert states.schema == STATE_SCHEMA
    ids = set(result.series["series_id"].to_pylist())
    assert states.num_rows > 0 and set(states["series_id"].to_pylist()) <= ids
    assert b.caveats == []


def test_unknown_op_drops_companion_with_caveat(tmp_path):
    svc = make_service(tmp_path)
    src, f = _filtered(svc)
    meta, result = svc.datasets.get(f)
    meta = dataclasses.replace(meta, derived={"op": "future_op", "from": src})
    b = dataset_bundle(svc.datasets, meta, result)
    assert b.companions == {} and [c.code for c in b.caveats] == ["companion_dropped"]


def test_recompute_op_on_stored_dataset_raises(tmp_path):
    svc = make_service(tmp_path)
    src, f = _filtered(svc)
    meta, result = svc.datasets.get(f)
    meta = dataclasses.replace(meta, derived={"op": "lod", "from": src})
    with pytest.raises(ValueError, match="render time"):
        dataset_bundle(svc.datasets, meta, result)


def test_filtered_dataset_summary_reports_the_source_gap(tmp_path):
    """Coverage is carried through a filter: the filtered dataset's summary names the source's gap
    (an hour of empty buckets), not a fresh judgement of its own counts."""
    svc = make_service(tmp_path)
    r = periodic_buckets(
        0, 4 * DAY, M, [(5 * M, 3, None), (DAY, 5, None)], noise=1, seed=1,
        gaps=[(DAY, DAY + 60 * M)],
    )  # fmt: skip
    src = svc.datasets.put(
        source="default", expr="queue_depth", rng=TimeRange(0, 4 * DAY), step_ms=M,
        resolution_ms=15_000, result=r, representation="bucket_agg",
    ).id  # fmt: skip
    f = svc.filter(src, "lowpass", "1h", "trend")["dataset"]
    summaries = {}
    for ds in (src, f):
        meta, result = svc.datasets.get(ds)
        summaries[ds] = svc._time_summary(meta, result, NOW)
    for out in summaries.values():
        assert "missing_data" in out["caveats"]
        [cov] = [x["coverage"] for x in out["series"]]
        assert cov["missing"] == "1h" and cov["longest_gap"] == "1h" and cov["pct"] < 1.0
    assert summaries[f]["series"][0]["coverage"] == summaries[src]["series"][0]["coverage"]


def test_carry_without_a_source_state_drops_the_companion_with_a_caveat(tmp_path):
    """A filter of a code output: the source carries no bucket_state (coverage is not tracked
    through code), so there is nothing to carry. Dropped and said so, never invented."""
    svc = make_service(tmp_path)
    src, f = _filtered(svc)
    src_meta, src_result = svc.datasets.get(src)
    code_meta = dataclasses.replace(src_meta, producer={"kind": "code", "node": "n", "output": "o"})

    class Store:
        def get(self, dataset_id):
            return code_meta, src_result

    meta, result = svc.datasets.get(f)
    b = dataset_bundle(Store(), meta, result)  # type: ignore[arg-type]
    assert b.companions == {}
    [c] = [c for c in b.caveats if c.code == "companion_dropped"]
    assert "lowpass" in c.message
