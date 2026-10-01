"""Distribution datasets from recorded Grafana Play responses (native + classic): no network."""

from pathlib import Path

import httpx
import polars as pl
import pytest

from telemetry_nerd.analysis.distlod import rebucket_time
from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.presets import PLAY
from telemetry_nerd.sources.promql import PromQLSource
from telemetry_nerd.sources.replay import ReplayTransport

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "play"
NATIVE_SEL = 'traces_spanmetrics_latency{service="checkoutservice",span_kind="SPAN_KIND_SERVER"}'
NATIVE_RANGE = TimeRange(
    1_790_848_200_000, 1_790_850_000_000
)  # as in scripts/record_play_histograms.py
CLASSIC_SEL = 'http_server_request_duration_seconds_bucket{job="ecommerce-prod/cartservice"}'
CLASSIC_RANGE = TimeRange(1_790_856_000_000, 1_790_859_600_000)


@pytest.fixture
def play():
    return PromQLSource.from_spec(
        PLAY, client=httpx.AsyncClient(transport=ReplayTransport(FIXTURES))
    )


async def test_native_columns_equal_histogram_count(play):
    dist = await play.fetch_histogram(NATIVE_SEL, ["cloud_region"], NATIVE_RANGE, 60_000)
    assert dist.scheme.kind == "native" and dist.scheme.schema is not None
    assert dist.series.num_rows == 3
    counts = await play.fetch_values(
        f"sum by (cloud_region) (histogram_count(increase({NATIVE_SEL}[1m])))", NATIVE_RANGE, 60_000
    )
    want = {
        (r["series_id"], r["ts_ms"]): r["avg"]
        for r in counts.buckets.to_pylist()
        if r["avg"] is not None
    }
    got = {(c["series_id"], c["ts_ms"]): c["n"] for c in dist.columns.to_pylist()}
    assert got.keys() == want.keys()
    assert all(got[k] == pytest.approx(want[k]) for k in got)


async def test_spike_column_is_the_slow_cluster(play):
    from telemetry_nerd.model.series import series_id

    dist = await play.fetch_histogram(NATIVE_SEL, ["cloud_region"], NATIVE_RANGE, 60_000)
    ap = series_id("play", {"cloud_region": "ap-south-1"})
    rows = [
        r for r in dist.rows.to_pylist() if r["series_id"] == ap and r["ts_ms"] == 1_790_848_920_000
    ]
    assert rows and min(r["bucket_lo"] for r in rows) > 10  # 10:02Z: all >= ~14.7 s
    n = next(
        c["n"]
        for c in dist.columns.to_pylist()
        if c["series_id"] == ap and c["ts_ms"] == 1_790_848_920_000
    )
    assert n == pytest.approx(8, abs=0.01)


async def test_time_rebucket_sums_native_counts(play):
    dist = await play.fetch_histogram(NATIVE_SEL, ["cloud_region"], NATIVE_RANGE, 60_000)
    rows = pl.from_arrow(dist.rows)
    cols = pl.from_arrow(dist.columns).with_columns(pl.lit(1, pl.Int64).alias("cover"))
    r5, c5 = rebucket_time(rows, cols, 300_000)
    assert r5["count"].sum() == pytest.approx(rows["count"].sum())
    assert c5["n"].sum() == pytest.approx(cols["n"].sum())


async def test_classic_play_n_equals_inf_bucket_increase(play):
    dist = await play.fetch_histogram(CLASSIC_SEL, [], CLASSIC_RANGE, 300_000)
    assert dist.scheme.kind == "classic"
    inf = await play.fetch_values(
        'sum(increase(http_server_request_duration_seconds_bucket{job="ecommerce-prod/cartservice",le="+Inf"}[5m]))',
        CLASSIC_RANGE, 300_000,
    )  # fmt: skip
    want = sorted(r["avg"] for r in inf.buckets.to_pylist())
    assert sorted(c["n"] for c in dist.columns.to_pylist()) == pytest.approx(want)


async def test_percentile_bands_are_the_source_bucket_holding_q(play):
    from telemetry_nerd.analysis.exprkind import min_samples
    from telemetry_nerd.analysis.quantiles import column_quantiles, quantile_bucket
    from telemetry_nerd.model.series import series_id

    dist = await play.fetch_histogram(NATIVE_SEL, ["cloud_region"], NATIVE_RANGE, 60_000)
    rows, cols = pl.from_arrow(dist.rows), pl.from_arrow(dist.columns)
    by_col: dict = {}
    for r in rows.iter_rows(named=True):
        by_col.setdefault((r["series_id"], r["ts_ms"]), []).append(
            (r["bucket_lo"], r["bucket_hi"], r["count"])
        )
    n_at = {(c["series_id"], c["ts_ms"]): c["n"] for c in cols.iter_rows(named=True)}
    out = column_quantiles(rows, cols, (0.5, 0.9, 0.99))
    for q in (0.5, 0.9, 0.99):
        want = {k for k, n in n_at.items() if n >= min_samples(q) and k in by_col}
        got = set()
        for sid, per_q in out.items():
            b = per_q.get(f"{q:g}", {"ts": [], "lo": [], "hi": []})
            for t, lo, hi in zip(b["ts"], b["lo"], b["hi"], strict=True):
                got.add((sid, t))
                assert (lo, hi) == quantile_bucket(by_col[(sid, t)], q)
                assert (lo, hi) in {(x[0], x[1]) for x in by_col[(sid, t)]}  # a source bucket
        assert got == want
    ap = series_id("play", {"cloud_region": "ap-south-1"})
    assert 1_790_848_920_000 not in out.get(ap, {}).get("0.5", {"ts": []})["ts"]  # n ~ 8 < 20
