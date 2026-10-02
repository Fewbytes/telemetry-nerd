from typing import get_args

import pytest

from telemetry_nerd.analysis.filters import Kind as FilterKind
from telemetry_nerd.model.bucket_state import State
from telemetry_nerd.model.companions import KINDS, OPS, SOURCE_AGGREGATED, dataset_bundle, policy
from tests.unit.fakes import make_service

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
        ("rate(x[5m])", False),
        ("up", False),
    ],
)
def test_source_aggregation_detection(expr, agg):
    assert bool(SOURCE_AGGREGATED.match(expr)) is agg


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
