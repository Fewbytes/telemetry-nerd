"""Tier-2 data exchange through run directories: daemon export/ingest + kernel tn (b98.1/.2)."""

from __future__ import annotations

import itertools
import json
import math
import subprocess
import sys

import polars as pl
import pyarrow as pa
import pytest

from telemetry_nerd import tn
from telemetry_nerd.analysis.histogram import from_matrix
from telemetry_nerd.core.code_outputs import evidence_problem
from telemetry_nerd.datasets.db import open_duckdb
from telemetry_nerd.datasets.store import DatasetStore, Lineage
from telemetry_nerd.exchange import fmt
from telemetry_nerd.exchange.run import RunExchange, runs_root
from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult, labels_json
from telemetry_nerd.model.time import TimeRange

STEP = 60_000


@pytest.fixture
def store(tmp_path):
    counter = itertools.count(1)
    return DatasetStore(
        open_duckdb(tmp_path / "s.duckdb"), lambda p: f"{p}{next(counter)}", clock=lambda: 7
    )


@pytest.fixture
def rx(store, tmp_path):
    return RunExchange(store, runs_root(tmp_path), unit_of=lambda m: m.unit or "s")


@pytest.fixture
def kernel(rx, monkeypatch):
    """prepare a run and point tn at it, like the kernel manager will."""

    def start(node: str, inputs: list[str]):
        run = rx.prepare_run(node, inputs)
        monkeypatch.setenv(fmt.ENV_RUN_DIR, str(run))
        return run

    return start


def _series(*labels: dict) -> pa.Table:
    return pa.table(
        {"series_id": [f"s{i}" for i in range(len(labels))], "labels": [labels_json(lab) for lab in labels]},
        schema=SERIES_SCHEMA,
    )  # fmt: skip


def put_ts(store, **kw) -> str:
    buckets = pa.table(
        {
            "ts_ms": [STEP, 2 * STEP, STEP, 2 * STEP],
            "series_id": ["s0", "s0", "s1", "s1"],
            "avg": [1.5, None, 3.0, 4.0],
            "min": [1.0, None, 2.0, 3.5],
            "max": [2.0, None, 4.0, 4.5],
            "count": [4, 0, 4, 4],
        },
        schema=BUCKET_SCHEMA,
    )
    res = FetchResult(buckets, _series({"pod": "a"}, {"pod": "b"}), partial=1)
    meta = store.put(
        source="vm", expr="rate(x[1m])", rng=TimeRange(0, 2 * STEP), step_ms=STEP,
        resolution_ms=15_000, result=res, **kw,
    )  # fmt: skip
    return meta.id


def put_dist(store) -> str:
    dist = from_matrix(
        "vm",
        [
            {"metric": {"le": "1"}, "values": [[60, "3"]]},
            {"metric": {"le": "+Inf"}, "values": [[60, "5"], [120, "0"]]},
            {"metric": {"le": "1"}, "values": [[120, "0"]]},
        ],
        expr="lat_bucket",
    )
    meta = store.put_distribution(
        source="vm", rng=TimeRange(0, 2 * STEP), step_ms=STEP, resolution_ms=15_000, dist=dist,
        histogram={"selector": "lat_bucket", "by": []}, n_min=20,
    )  # fmt: skip
    return meta.id


def _by_labels(res: FetchResult) -> list[tuple]:
    labels = dict(
        zip(res.series["series_id"].to_pylist(), res.series["labels"].to_pylist(), strict=True)
    )
    return sorted(
        (labels[r.pop("series_id")], *r.values()) for r in res.buckets.to_pylist()
    )  # (labels, ts_ms, avg, min, max, count)


# --- export --------------------------------------------------------------------------------


def test_prepare_run_layout_and_meta(store, rx):
    d = put_ts(store)
    run = rx.prepare_run("n1", [d, d])
    assert json.loads((run / "run.json").read_text()) == {
        "version": 1, "code_node": "n1", "inputs": [d],
    }  # fmt: skip
    meta = json.loads((run / "inputs" / f"{d}.meta.json").read_text())
    assert meta["handle"] == d and meta["unit"] == "s" and meta["expr"] == "rate(x[1m])"
    assert meta["step_ms"] == STEP and meta["representation"] == "bucket_agg"
    assert meta["caveats"] == ["partial"]
    assert meta["tables"] == {"rows": f"{d}.arrow", "series": f"{d}.series.arrow"}
    assert sorted(p.name for p in (run / "inputs").iterdir()) == sorted(
        [f"{d}.arrow", f"{d}.series.arrow", f"{d}.meta.json"]
    )
    # Arrow IPC *file* format (random access)
    assert (run / "inputs" / f"{d}.arrow").read_bytes()[:6] == b"ARROW1"


def test_prepare_run_rejects_unsupported_representation_and_bad_ids(store, rx, tmp_path):
    odd = store.put(
        source="vm", expr="x", rng=TimeRange(0, STEP), step_ms=STEP, resolution_ms=STEP,
        result=FetchResult(BUCKET_SCHEMA.empty_table(), SERIES_SCHEMA.empty_table()),
        representation="spectrum",
    ).id  # fmt: skip
    with pytest.raises(fmt.ExchangeError, match="spectrum dataset"):
        rx.prepare_run("n1", [odd])
    assert not (runs_root(tmp_path) / "n1").exists()  # no half-prepared run left behind
    with pytest.raises(fmt.ExchangeError, match="not a valid id"):
        rx.prepare_run("../etc", [])


def test_rerun_replaces_inputs_and_outputs(store, rx, kernel):
    d = put_ts(store)
    kernel("n1", [d])
    tn.put(tn.dataset(d), like=d)
    run = rx.prepare_run("n1", [d])
    assert list((run / "outputs").iterdir()) == []


# --- kernel side ---------------------------------------------------------------------------


def test_tn_inputs_meta_and_undeclared(store, kernel):
    d, other = put_ts(store), put_ts(store)
    kernel("n1", [d])
    assert tn.inputs == (d,)
    assert tn.meta(d)["unit"] == "s"
    with pytest.raises(tn.TnError, match=rf"'{other}' is not a declared input.*declared: {d}"):
        tn.dataset(other)
    with pytest.raises(tn.TnError, match="no table 'columns'"):
        tn.dataset(d, "columns")


def test_tn_without_run_dir(monkeypatch):
    monkeypatch.delenv(fmt.ENV_RUN_DIR, raising=False)
    with pytest.raises(tn.TnError, match="TN_RUN_DIR is not set"):
        tn.inputs  # noqa: B018


def test_dataset_is_memory_mapped_zero_copy(store, kernel):
    d = put_ts(store)
    kernel("n1", [d])
    before = pa.total_allocated_bytes()
    t = tn.dataset(d, arrow=True)
    assert pa.total_allocated_bytes() == before  # buffers live in the mapped file
    assert t.num_rows == 4 and t.schema.names == BUCKET_SCHEMA.names
    df = tn.dataset(d)
    assert isinstance(df, pl.DataFrame) and df["avg"].to_list() == [1.5, None, 3.0, 4.0]
    labels = tn.dataset(d, "series")
    assert labels["labels"].to_list() == ['{"pod":"a"}', '{"pod":"b"}']


def test_put_validates_meta_locally(store, kernel):
    d = put_ts(store)
    kernel("n1", [d])
    df = tn.dataset(d)
    with pytest.raises(tn.TnError, match="unknown meta keys"):
        tn.put(df, {"like": d, "ci": 0.95})
    with pytest.raises(tn.TnError, match="step_ms is required"):
        tn.put(df)
    with pytest.raises(tn.TnError, match="missing column 'lo'"):
        tn.put(df, like=d, uncertainty={"method": "bootstrap", "level": 0.95})
    with pytest.raises(tn.TnError, match="uncertainty.method is required"):
        tn.put(df, like=d, uncertainty={"level": 0.95})
    with pytest.raises(tn.TnError, match="unexpected column 'stderr'"):
        tn.put(df.with_columns(stderr=pl.lit(0.1)), like=d)
    with pytest.raises(tn.TnError, match="exact=True is only for integral"):
        tn.put(df, like=d, exact=True)
    with pytest.raises(tn.TnError, match="contradictory"):
        tn.put(df, like=d, exact=True, uncertainty={"method": "x"})
    with pytest.raises(tn.TnError, match="representation 'estimate' cannot be put.*put_fit"):
        tn.put(df, like=d, representation="estimate")
    with pytest.raises(tn.TnError, match="'d99' is not a declared input"):
        tn.put(df, like=d, parents=["d99"])
    with pytest.raises(tn.TnError, match="lo > hi"):
        tn.put(
            df.with_columns(lo=pl.col("avg") + 1, hi=pl.col("avg")),
            like=d, uncertainty={"method": "m"},
        )  # fmt: skip
    assert list(tn_run_outputs()).__len__() == 0  # nothing written by failed puts


def tn_run_outputs():
    import os
    from pathlib import Path

    return list((Path(os.environ[fmt.ENV_RUN_DIR]) / "outputs").iterdir())


def test_put_writes_atomically_meta_last(store, kernel, monkeypatch):
    d = put_ts(store)
    run = kernel("n1", [d])
    name = tn.put(tn.dataset(d), like=d)
    assert name == "out1"
    names = sorted(p.name for p in (run / "outputs").iterdir())
    assert names == ["out1.arrow", "out1.meta.json"]  # no temp files left
    meta = json.loads((run / "outputs" / "out1.meta.json").read_text())
    assert meta["rows"] == 4 and meta["files"] == {"rows": "out1.arrow"}

    # a writer dying mid-table leaves no committed output
    from telemetry_nerd.exchange import tables

    def boom(f):
        f.write(b"ARROW1 half")
        raise KeyboardInterrupt

    monkeypatch.setattr(
        tables, "write_atomic", lambda path, write, mode="wb": fmt.write_atomic(path, boom)
    )
    with pytest.raises(KeyboardInterrupt):
        tn.put(tn.dataset(d), like=d, name="crashy")
    assert not (run / "outputs" / "crashy.meta.json").exists()
    assert not (run / "outputs" / "crashy.arrow").exists()


def test_tn_import_is_light():
    code = (
        "import sys, telemetry_nerd.tn; "
        "heavy = {'polars', 'pyarrow', 'duckdb', 'numpy', 'pydantic'} & set(sys.modules); "
        "assert not heavy, heavy"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# --- round trips ---------------------------------------------------------------------------


def test_time_series_round_trip_keeps_schema_and_meta(store, rx, kernel):
    d = put_ts(store)
    kernel("n7", [d])
    tn.put(tn.dataset(d), like=d, caveats=["smoothed"], description="identity")
    res = rx.ingest_run("n7", succeeded=True)
    assert res.issues == []
    (ing,) = res.datasets
    assert ing.name == "out1" and ing.parents == (d,) and ing.uncertainty == "no_uncertainty"
    src_meta, src = store.get(d)
    meta, got = store.get(ing.dataset_id)
    assert (meta.step_ms, meta.resolution_ms, meta.start_ms, meta.end_ms) == (
        src_meta.step_ms, src_meta.resolution_ms, src_meta.start_ms, src_meta.end_ms,
    )  # fmt: skip
    assert meta.source == "vm" and meta.expr == "code:n7/out1" and meta.unit == "s"
    assert meta.producer == {
        "kind": "code",
        "node": "n7",
        "output": "out1",
        "description": "identity",
    }
    assert meta.parents == [d]
    assert meta.source_caveats == ["partial", "smoothed", "no_uncertainty"]
    assert meta.uncertainty is None
    # values intact; series renamed to derived identity but labels kept
    assert _by_labels(got) == _by_labels(src)
    assert set(got.series.column("series_id").to_pylist()).isdisjoint(
        src.series.column("series_id").to_pylist()
    )
    assert evidence_problem(meta, None) is None  # unknown uncertainty is a flag, not a refusal


def test_interval_round_trip_and_evidence(store, rx, kernel):
    d = put_ts(store)
    kernel("n1", [d])
    df = tn.dataset(d).filter(pl.col("avg").is_not_null())
    out = df.with_columns(lo=pl.col("avg") * 0.9, hi=pl.col("avg") * 1.1)
    u = {"method": "bootstrap percentile", "level": 0.95}
    tn.put(out, like=d, uncertainty=u)
    (first,) = rx.ingest_run("n1", succeeded=True).datasets
    assert first.uncertainty is None and "no_uncertainty" not in first.caveats
    meta = store.meta(first.dataset_id)
    assert meta.uncertainty == {**u, "kind": "confidence"}
    iv = store.interval(first.dataset_id)
    assert sorted(iv.column("lo").to_pylist()) == pytest.approx([1.35, 2.7, 3.6])

    # the interval travels on: export it again, lo/hi come back
    kernel("n2", [first.dataset_id])
    again = tn.dataset(first.dataset_id)
    assert {"lo", "hi"} <= set(again.columns)
    assert tn.meta(first.dataset_id)["uncertainty"]["method"] == "bootstrap percentile"
    tn.put(again, like=first.dataset_id, uncertainty=u)
    (second,) = rx.ingest_run("n2", succeeded=True).datasets
    iv2 = store.interval(second.dataset_id)
    assert sorted(iv2.column("hi").to_pylist()) == pytest.approx(
        sorted(iv.column("hi").to_pylist())
    )


def test_exact_and_label_columns(store, rx, kernel):
    d = put_ts(store)
    kernel("n1", [d])
    out = pl.DataFrame({"ts_ms": [STEP, STEP], "avg": [3, 5], "svc": ["a", "b"]})
    tn.put(out, step_ms=STEP, labels=["svc"], exact=True, unit="1")
    (ing,) = rx.ingest_run("n1", succeeded=True).datasets
    meta, got = store.get(ing.dataset_id)
    assert meta.uncertainty == {"exact": True} and ing.uncertainty is None
    assert meta.start_ms == 0 and meta.end_ms == STEP  # range from the data
    assert sorted(got.series.column("labels").to_pylist()) == ['{"svc":"a"}', '{"svc":"b"}']
    assert got.buckets.column("min").null_count == 2  # not given: unknown, never invented


def test_distribution_round_trip(store, rx, kernel):
    d = put_dist(store)
    kernel("n1", [d])
    assert tn.meta(d)["scheme"]["kind"] == "classic"
    rows, cols = tn.dataset(d), tn.dataset(d, "columns")
    tn.put(rows, like=d, columns=cols, exact=True)
    (ing,) = rx.ingest_run("n1", succeeded=True).datasets
    src_meta, src = store.get_distribution(d)
    meta, got = store.get_distribution(ing.dataset_id)
    assert meta.representation == "distribution" and meta.scheme == src_meta.scheme
    assert meta.n_min == 20 and meta.uncertainty == {"exact": True}
    keep = ["ts_ms", "bucket_lo", "bucket_hi", "count"]
    assert got.rows.select(keep).equals(src.rows.select(keep))
    assert got.columns.select(["ts_ms", "n"]).equals(src.columns.select(["ts_ms", "n"]))
    assert got.rows.column("bucket_lo")[0].as_py() == -math.inf


def test_distribution_refuses_interval_and_fractional_exact(store, kernel):
    d = put_dist(store)
    kernel("n1", [d])
    with pytest.raises(tn.TnError, match="distribution has no lo/hi"):
        tn.put(tn.dataset(d), like=d, uncertainty={"method": "m"})
    frac = tn.dataset(d).with_columns(pl.col("count") * 0.5)
    with pytest.raises(tn.TnError, match="integral counts"):
        tn.put(frac, like=d, exact=True)


def test_fit_with_prediction(store, rx, kernel):
    d = put_ts(store)
    kernel("n1", [d])
    pred = pl.DataFrame(
        {"ts_ms": [STEP, 2 * STEP], "avg": [1.0, 2.0], "lo": [0.5, 1.0], "hi": [1.5, 3.0]}
    )
    name = tn.put_fit(
        "linear",
        {"slope": {"value": 2.1, "interval": [1.8, 2.4]}, "intercept": 0.3},
        method="OLS, HC3",
        level=0.95,
        diagnostics={"durbin_watson": 1.9},
        goodness={"r2": 0.93},
        prediction=pred,
        prediction_meta={
            "like": d,
            "uncertainty": {"method": "OLS", "level": 0.95, "kind": "prediction"},
        },
    )
    assert name == "fit1"
    res = rx.ingest_run("n1", succeeded=True)
    assert res.issues == []
    fit, prediction = res.datasets
    fmeta = store.meta(fit.dataset_id)
    assert fmeta.representation == "estimate" and fmeta.parents == [d]
    assert fmeta.fit["params"]["slope"] == {"value": 2.1, "interval": [1.8, 2.4], "exact": False}
    assert fmeta.fit["params_without_uncertainty"] == ["intercept"]
    assert (fmeta.start_ms, fmeta.end_ms) == (0, 2 * STEP)
    slope = {"name": "slope", "value": 2.1, "interval": [1.8, 2.4], "exact": False}
    assert evidence_problem(fmeta, slope) is None
    # a parameter without an interval is citable as stored (flagged unknown), not refused
    intercept = {"name": "intercept", "value": fmeta.fit["params"]["intercept"]["value"]}
    assert evidence_problem(fmeta, intercept) is None
    assert "cite it as stored" in evidence_problem(fmeta, {**intercept, "interval": [0, 1]})
    assert "no fit parameter" in evidence_problem(fmeta, {**slope, "name": "bogus"})
    assert prediction.name == "fit1_prediction" and prediction.parents == (fit.dataset_id, d)
    assert store.meta(prediction.dataset_id).uncertainty["kind"] == "prediction"
    # the band and the fit are one computation: no propagation to declare; but the intercept's
    # error is unknown, so the band is a lower bound (spec §5.3)
    assert prediction.uncertainty == "input_uncertainty_unknown"


def test_fit_needs_diagnostics(store, kernel):
    d = put_ts(store)
    kernel("n1", [d])
    with pytest.raises(tn.TnError, match="diagnostics are mandatory"):
        tn.put_fit(
            "linear", {"slope": {"value": 1.0, "interval": [0, 2]}}, method="OLS", diagnostics={}
        )


# --- failure modes -------------------------------------------------------------------------


def test_failed_run_ingests_nothing(store, rx, kernel):
    d = put_ts(store)
    kernel("n1", [d])
    tn.put(tn.dataset(d), like=d)
    res = rx.ingest_run("n1", succeeded=False)
    assert res.datasets == [] and [i.code for i in res.issues] == ["run_failed"]
    assert len(store.list_metas()) == 1


def test_partial_and_corrupt_outputs_are_not_ingested(store, rx, kernel):
    d = put_ts(store)
    run = kernel("n1", [d])
    tn.put(tn.dataset(d), like=d, name="good")
    tn.put(tn.dataset(d), like=d, name="short")
    out = run / "outputs"
    (out / ".tmp-x.arrow.abc").write_bytes(b"ARROW1")  # a writer that died
    (out / "orphan.arrow").write_bytes((out / "good.arrow").read_bytes())  # meta never written
    data = (out / "short.arrow").read_bytes()
    (out / "short.arrow").write_bytes(data[: len(data) // 2])  # truncated behind tn's back
    res = rx.ingest_run("n1", succeeded=True)
    assert [i.name for i in res.datasets] == ["good"]
    assert sorted((i.name, i.code) for i in res.issues) == [
        (".tmp-x.arrow.abc", "partial_write"),
        ("orphan", "uncommitted"),
        ("short", "corrupt"),
    ]


def test_ingest_validates_independently_of_tn(store, rx, kernel):
    d = put_ts(store)
    run = kernel("n1", [d])
    tn.put(tn.dataset(d), like=d)
    mpath = run / "outputs" / "out1.meta.json"
    meta = json.loads(mpath.read_text())
    meta["exact"] = True  # hand-edited: avg is fractional
    mpath.write_text(json.dumps(meta))
    res = rx.ingest_run("n1", succeeded=True)
    assert res.datasets == [] and "integral" in res.issues[0].message


def test_passthrough_series_id_must_come_from_an_input(store, rx, kernel):
    d = put_ts(store)
    kernel("n1", [d])
    tn.put(tn.dataset(d).with_columns(series_id=pl.lit("zzz")).unique(["ts_ms"]), like=d)
    res = rx.ingest_run("n1", succeeded=True)
    assert "not a series of any declared input" in res.issues[0].message


def test_ingest_is_idempotent(store, rx, kernel):
    d = put_ts(store)
    run = kernel("n1", [d])
    tn.put(tn.dataset(d), like=d)
    (first,) = rx.ingest_run("n1", succeeded=True).datasets
    assert rx.ingest_run("n1", succeeded=True).datasets == []
    assert json.loads((run / "ingested.json").read_text()) == {"out1": first.dataset_id}
    assert len(store.list_metas()) == 2


def test_gc_removes_unreferenced_runs(store, rx, tmp_path):
    d = put_ts(store)
    rx.prepare_run("n1", [d])
    rx.prepare_run("n2", [d])
    assert rx.gc(keep=lambda node: node == "n2") == ["n1"]
    assert sorted(p.name for p in runs_root(tmp_path).iterdir()) == ["n2"]


def test_store_refuses_interval_mismatch(store):
    with pytest.raises(ValueError, match="lo/hi"):
        store.put(
            source="vm", expr="x", rng=TimeRange(0, STEP), step_ms=STEP, resolution_ms=STEP,
            result=FetchResult(BUCKET_SCHEMA.empty_table(), SERIES_SCHEMA.empty_table()),
            lineage=Lineage(producer={"kind": "code"}, uncertainty={"method": "m"}),
        )  # fmt: skip


# --- uncertainty status: maximalist propagation (spec §5.3, dl7) ----------------------------

CI = {"method": "moving-block bootstrap, block=2", "level": 0.95}


def _ci(df: pl.DataFrame) -> pl.DataFrame:
    df = df.filter(pl.col("avg").is_not_null())
    return df.with_columns(lo=pl.col("avg") * 0.9, hi=pl.col("avg") * 1.1)


def _child(rx, kernel, node: str, parent: str, **put) -> tuple[str, ...]:
    """Run `node` over `parent`, put one output (put kwargs), return its caveats."""
    kernel(node, [parent])
    df = tn.dataset(parent).drop(["lo", "hi"], strict=False)
    if put.get("exact"):  # e.g. a count per bucket: integral
        df = df.with_columns(pl.col("avg").round())
    tn.put(_ci(df) if "uncertainty" in put else df.filter(pl.col("avg").is_not_null()),
           like=parent, **put)  # fmt: skip
    (ing,) = rx.ingest_run(node, succeeded=True).datasets
    return ing.dataset_id, ing.caveats


def test_status_is_recomputed_not_inherited(store, rx, kernel):
    d = put_ts(store)
    bare, cav = _child(rx, kernel, "n1", d)
    assert cav == ("partial", "no_uncertainty")
    # dl7's case: a bootstrap CI over a point-estimate output is not 'no_uncertainty' (the
    # parent's tag describes the parent), but its interval excludes the parent's unknown error
    boot, cav = _child(rx, kernel, "n2", bare, uncertainty=CI)
    assert cav == ("partial", "input_uncertainty_unknown")
    # a declared propagation cannot clear an unknown input: unknown cannot be propagated
    _, cav = _child(rx, kernel, "n3", bare, uncertainty={**CI, "propagation": "delta method"})
    assert "input_uncertainty_unknown" in cav
    # nor can exactness hide it
    _, cav = _child(rx, kernel, "n4", bare, exact=True)
    assert cav == ("partial", "input_uncertainty_unknown")
    # a lower-bound input is not clean either: its child is input_uncertainty_unknown
    _, cav = _child(rx, kernel, "n5", boot, uncertainty=CI)
    assert cav == ("partial", "input_uncertainty_unknown")
    # nothing declared: just unknown, never stacked with a parent's status
    _, cav = _child(rx, kernel, "n6", boot)
    assert cav == ("partial", "no_uncertainty")


def test_interval_inputs_must_be_propagated(store, rx, kernel):
    d = put_ts(store)
    ci, cav = _child(rx, kernel, "n1", d, uncertainty=CI)
    assert "uncertainty_not_propagated" not in cav  # source data: nothing to propagate
    _, cav = _child(rx, kernel, "n2", ci, uncertainty=CI)
    assert cav == ("partial", "uncertainty_not_propagated")  # a narrower interval: lower bound
    prop = {**CI, "propagation": "Monte Carlo over input intervals"}
    out, cav = _child(rx, kernel, "n3", ci, uncertainty=prop)
    assert cav == ("partial",)
    assert store.meta(out).uncertainty["propagation"] == "Monte Carlo over input intervals"
    _, cav = _child(rx, kernel, "n4", ci, exact=True)  # exact asserts no error at all
    assert cav == ("partial",)


def test_unused_unknown_input_can_be_dropped_from_lineage(store, rx, kernel):
    d = put_ts(store)
    bare, _ = _child(rx, kernel, "n1", d)
    kernel("n2", [d, bare])
    tn.put(_ci(tn.dataset(d)), like=d, uncertainty=CI, parents=[d])
    (ing,) = rx.ingest_run("n2", succeeded=True).datasets
    assert ing.uncertainty is None


def test_propagation_must_name_a_method(store, kernel):
    d = put_ts(store)
    kernel("n1", [d])
    with pytest.raises(tn.TnError, match="propagation names how"):
        tn.put(_ci(tn.dataset(d)), like=d, uncertainty={**CI, "propagation": " "})


def test_fit_status_over_inputs(store, rx, kernel):
    d = put_ts(store)
    ci, _ = _child(rx, kernel, "n1", d, uncertainty=CI)
    slope = {"slope": {"value": 2.0, "interval": [1.5, 2.5]}}

    def fit(node, **kw):
        kernel(node, [ci])
        tn.put_fit("linear", kw.pop("params", slope), method="OLS", diagnostics={"dw": 2}, **kw)
        (ing,) = rx.ingest_run(node, succeeded=True).datasets
        return ing.caveats

    assert fit("n2") == ("partial", "uncertainty_not_propagated")
    # a prediction of a clean fit is clean (the fit's intervals are not an input to propagate)
    kernel("n5", [d])
    pred = pl.DataFrame({"ts_ms": [STEP], "avg": [1.0], "lo": [0.5], "hi": [1.5]})
    tn.put_fit("linear", slope, method="OLS", diagnostics={"dw": 2}, prediction=pred,
               prediction_meta={"like": d, "uncertainty": {**CI, "kind": "prediction"}})  # fmt: skip
    assert [i.uncertainty for i in rx.ingest_run("n5", succeeded=True).datasets] == [None, None]
    assert fit("n3", propagation="weighted least squares on input intervals") == ("partial",)
    # a parameter without an interval is no_uncertainty on its own, beside the inputs' status
    cav = fit("n4", params={**slope, "icept": 0.3})
    assert set(cav) == {"partial", "no_uncertainty", "uncertainty_not_propagated"}
