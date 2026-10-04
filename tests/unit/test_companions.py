import dataclasses
from typing import get_args

import pytest

from telemetry_nerd.analysis.filters import Kind as FilterKind
from telemetry_nerd.datasets.store import Lineage
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
    r = periodic_buckets(0, 4 * DAY, M, [(5 * M, 3, None), (DAY, 5, None)], noise=1, seed=1)
    code = svc.datasets.put(
        source="default", expr="code:n/o", rng=TimeRange(0, 4 * DAY), step_ms=M,
        resolution_ms=15_000, result=r, representation="bucket_agg",
        lineage=Lineage(producer={"kind": "code", "node": "n", "output": "o"}),
    ).id  # fmt: skip
    f = svc.filter(code, "lowpass", "1h", "trend")["dataset"]
    meta, result = svc.datasets.get(f)
    assert meta.derived and meta.derived["op"] == "lowpass"
    assert (
        dataset_bundle(
            svc.datasets, svc.datasets.get(code)[0], svc.datasets.get(code)[1]
        ).companions
        == {}
    )
    b = dataset_bundle(svc.datasets, meta, result)  # carries through the real store.get path
    assert b.companions == {}
    [c] = [c for c in b.caveats if c.code == "companion_dropped"]
    assert "lowpass" in c.message


@pytest.mark.parametrize(
    ("expr", "holds"),
    [
        # increase() of a tile: 0 where no sample landed, the next tile carries the change
        ("sum(increase(x[15s]))", True),
        ("sum by (i) (increase(x[5s]))", True),
        ("delta(x[15s])", True),
        # a window reaching past the bucket: computed from the samples inside it
        ("sum(rate(x[1m]))", True),
        ("increase(x[1m])", True),
        # a rate of a window holding no sample is the backend's default (VictoriaMetrics: 0)
        ("sum(rate(x[15s]))", False),
        ("increase(x[15s]) / rate(y[15s])", False),
        ("sum_over_time(x[15s])", False),
        # an instant reading is lookback fill
        ("sum(x)", False),
        ("x", False),
    ],
)
def test_which_values_without_a_sample_are_the_expressions_own(expr, holds):
    from telemetry_nerd.model.companions import unobserved_values_hold

    assert unobserved_values_hold(expr, 15_000) is holds


def _tiles(counts: list[int], fill: float) -> tuple:
    """One series, 15 s buckets from 0: `counts` samples per bucket; a bucket without samples
    carries the source's value `fill` with count 0 (as PromQLSource.fetch returns it)."""
    import pyarrow as pa

    from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult

    rows = [
        {"ts_ms": i * 15_000, "series_id": "a", "avg": fill if c == 0 else 150.0 * c,
         "min": fill if c == 0 else 150.0 * c, "max": fill if c == 0 else 150.0 * c, "count": c}
        for i, c in enumerate(counts)
    ]  # fmt: skip
    series = pa.table({"series_id": ["a"], "labels": ['{"i":"a"}']}, schema=SERIES_SCHEMA)
    return FetchResult(pa.Table.from_pylist(rows, schema=BUCKET_SCHEMA), series), len(counts)


def _settle(expr: str, counts: list[int], fill: float = 0.0) -> dict[int, tuple]:
    from telemetry_nerd.model.companions import settle_unobserved

    res, n = _tiles(counts, fill)
    out = settle_unobserved(
        res, expr=expr, start_ms=0, end_ms=(n - 1) * 15_000, step_ms=15_000,
        resolution_ms=15_000,
    )  # fmt: skip
    return {r["ts_ms"] // 15_000: (r["count"], r["avg"]) for r in out.buckets.to_pylist()}


# scrapes on the tile edges: 1 in 4 lands just past its edge (a 0 tile, then a 2 one); then a
# 3 min outage (12 tiles), then steady again
_SPILLED = [1, 1, 1, 0, 2, 1, 1, 0, 2, 1] * 4 + [0] * 12 + [1] * 20


def test_a_spilled_scrape_tile_survives_and_a_gap_does_not():
    got = _settle("sum(increase(x[15s]))", _SPILLED)
    spilled = [i for i, c in enumerate(_SPILLED[:40]) if c == 0]
    gap = range(40, 52)
    # the tile a scrape spilled out of keeps the source's value, marked by count 0
    assert all(got[i] == (0, 0.0) for i in spilled)
    # an outage is missing data, never a run of zeros
    assert not any(i in got for i in gap)
    # observed tiles are untouched
    assert all(got[i] == (c, 150.0 * c) for i, c in enumerate(_SPILLED) if c)


def test_values_that_are_not_the_expressions_own_are_dropped_even_in_spilled_tiles():
    got = _settle("sum(rate(x[15s]))", _SPILLED)
    assert not any(c == 0 for c, _ in got.values())
    assert len(got) == sum(1 for c in _SPILLED if c)
