import pyarrow as pa
import pytest

from telemetry_nerd.datasets.cache import SeriesCache
from telemetry_nerd.datasets.db import open_duckdb
from telemetry_nerd.model.series import (
    BUCKET_SCHEMA,
    SERIES_SCHEMA,
    FetchResult,
    labels_json,
    series_id,
)
from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.base import LimitExceeded, SourceUnavailable

STEP = 60_000
SPAN = STEP * 10
NOW = 6_000_000_000  # multiple of SPAN


class FakeFetcher:
    def __init__(self):
        self.calls: list[TimeRange] = []

    async def __call__(self, rng: TimeRange) -> FetchResult:
        self.calls.append(rng)
        ts = list(range(rng.start_ms, rng.end_ms + 1, STEP))
        sid = series_id("src", {"i": "a"})
        buckets = pa.table(
            {
                "ts_ms": ts,
                "series_id": [sid] * len(ts),
                "avg": [float(t) for t in ts],
                "min": [float(t) - 1 for t in ts],
                "max": [float(t) + 1 for t in ts],
                "count": [4] * len(ts),
            },
            schema=BUCKET_SCHEMA,
        )
        series = pa.table(
            {"series_id": [sid], "labels": [labels_json({"i": "a"})]}, schema=SERIES_SCHEMA
        )
        return FetchResult(buckets, series)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def cache(tmp_path, clock):
    return SeriesCache(open_duckdb(tmp_path / "s.duckdb"), chunk_buckets=10, clock=clock)


async def test_first_get_fetches_each_overlapping_chunk(cache):
    f = FakeFetcher()
    res = await cache.get("src", "up", TimeRange(1_200_000, 2_400_000), STEP, f)
    assert [c.start_ms for c in f.calls] == [1_200_000, 1_800_000, 2_400_000]
    assert all(c.end_ms == c.start_ms + SPAN - STEP for c in f.calls)
    assert res.buckets.num_rows == 21
    assert res.buckets.schema == BUCKET_SCHEMA
    assert res.series.num_rows == 1


async def test_second_get_is_served_from_cache(cache):
    f = FakeFetcher()
    await cache.get("src", "up", TimeRange(1_200_000, 2_400_000), STEP, f)
    await cache.get("src", "up", TimeRange(1_200_000, 2_400_000), STEP, f)
    assert len(f.calls) == 3


async def test_extended_range_fetches_only_new_chunks(cache):
    f = FakeFetcher()
    await cache.get("src", "up", TimeRange(1_200_000, 2_400_000), STEP, f)
    await cache.get("src", "up", TimeRange(1_200_000, 3_000_000), STEP, f)
    assert [c.start_ms for c in f.calls[3:]] == [3_000_000]


async def test_recent_chunks_refetched_after_ttl(cache, clock):
    f = FakeFetcher()
    rng = TimeRange(NOW - 120_000, NOW)
    await cache.get("src", "up", rng, STEP, f)
    assert len(f.calls) == 2
    await cache.get("src", "up", rng, STEP, f)
    assert len(f.calls) == 2
    clock.t += 31_000
    await cache.get("src", "up", rng, STEP, f)
    assert len(f.calls) == 4


async def test_settled_chunks_never_refetched(cache, clock):
    f = FakeFetcher()
    rng = TimeRange(1_200_000, 2_400_000)
    await cache.get("src", "up", rng, STEP, f)
    clock.t += 30 * 86_400_000
    await cache.get("src", "up", rng, STEP, f)
    assert len(f.calls) == 3


async def test_result_is_filtered_to_range(cache):
    res = await cache.get("src", "up", TimeRange(1_260_000, 1_380_000), STEP, FakeFetcher())
    assert res.buckets.column("ts_ms").to_pylist() == [1_260_000, 1_320_000, 1_380_000]


async def test_step_and_expr_are_separate_keys(cache):
    f = FakeFetcher()
    await cache.get("src", "up", TimeRange(1_200_000, 1_380_000), STEP, f)
    await cache.get("src", "down", TimeRange(1_200_000, 1_380_000), STEP, f)
    assert len(f.calls) == 2


def test_query_key_is_exact_modulo_outer_whitespace():
    key = SeriesCache.query_key
    assert key("s", "  up\n", 60_000) == key("s", "up", 60_000)
    assert key("s", "up", 60_000) != key("s", "up", 30_000)
    # whitespace inside quoted label values is significant
    assert key("s", '{a="x  y"}', 60_000) != key("s", '{a="x y"}', 60_000)


async def test_same_name_different_identity_are_separate_entries(cache):
    f = FakeFetcher()
    rng = TimeRange(1_200_000, 1_380_000)
    await cache.get("vm|http://a|15000", "up", rng, STEP, f)
    await cache.get("vm|http://b|15000", "up", rng, STEP, f)
    assert len(f.calls) == 2


async def test_partial_flag_survives_the_cache(cache):
    class Partial(FakeFetcher):
        async def __call__(self, rng):
            res = await super().__call__(rng)
            return FetchResult(res.buckets, res.series, partial=1)

    rng = TimeRange(1_200_000, 2_400_000)  # 3 chunks
    first = await cache.get("src", "up", rng, STEP, Partial())
    assert first.partial == 3
    cached = await cache.get("src", "up", rng, STEP, FakeFetcher())  # all from cache
    assert cached.partial == 3
    assert (
        await cache.get("src", "up", TimeRange(1_200_000, 1_380_000), STEP, FakeFetcher())
    ).partial == 1


async def test_concurrent_chunk_fetches_are_bounded(cache):
    import asyncio

    active = peak = 0

    async def fetch(rng):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return await FakeFetcher()(rng)

    # 30 chunks of SPAN
    await cache.get("src", "up", TimeRange(0, 30 * SPAN - STEP), STEP, fetch)
    assert 1 < peak <= 8


class FlakyFetcher(FakeFetcher):
    """Fails every chunk whose start is in `bad`."""

    def __init__(self, bad, exc=None):
        super().__init__()
        self.bad, self.exc = set(bad), exc or SourceUnavailable("store down")

    async def __call__(self, rng):
        if rng.start_ms in self.bad:
            self.calls.append(rng)
            raise self.exc
        return await super().__call__(rng)


async def test_failed_chunk_becomes_failed_span_and_is_not_cached(cache):
    rng = TimeRange(NOW - 3 * SPAN, NOW - SPAN - STEP)
    bad = NOW - 2 * SPAN
    out = await cache.get("src", "up", rng, STEP, FlakyFetcher([bad]))
    assert out.failed == ((bad, bad + SPAN - STEP, "SourceUnavailable: store down"),)
    assert all(not (bad <= t < bad + SPAN) for t in out.buckets["ts_ms"].to_pylist())
    retry = FakeFetcher()
    again = await cache.get("src", "up", rng, STEP, retry)
    assert [r.start_ms for r in retry.calls] == [bad]  # only the failed chunk is refetched
    assert again.failed == ()


async def test_all_chunks_failing_raises(cache):
    rng = TimeRange(NOW - 2 * SPAN, NOW - SPAN - STEP)
    with pytest.raises(SourceUnavailable):
        await cache.get("src", "up", rng, STEP, FlakyFetcher([NOW - 2 * SPAN]))


async def test_limit_exceeded_propagates(cache):
    rng = TimeRange(NOW - 3 * SPAN, NOW - SPAN - STEP)
    with pytest.raises(LimitExceeded):
        await cache.get(
            "src", "up", rng, STEP, FlakyFetcher([NOW - 2 * SPAN], LimitExceeded("too many"))
        )


async def test_chunk_reported_failed_by_the_fetcher_keeps_its_data_but_is_not_cached_as_complete(
    cache, clock
):
    """A partial source response (1h9.12): data returned, span unknown, refetched next read."""
    calls = []

    async def partial_fetch(rng):
        calls.append(rng)
        res = await FakeFetcher()(rng)
        return FetchResult(
            res.buckets, res.series, failed=((rng.start_ms, rng.end_ms, "PartialResponse: x"),)
        )

    rng = TimeRange(NOW - 2 * SPAN, NOW - SPAN - STEP)  # old: would be immutable if complete
    first = await cache.get("src", "up", rng, STEP, partial_fetch)
    assert first.buckets.num_rows == 10  # data kept
    assert [f[2] for f in first.failed] == ["PartialResponse: x"]
    second = await cache.get("src", "up", rng, STEP, partial_fetch)
    assert len(calls) == 2  # not served from cache
    assert [f[2] for f in second.failed] == ["PartialResponse: x"]
    ok = await cache.get("src", "up", rng, STEP, FakeFetcher())  # source recovered
    assert ok.failed == () and ok.buckets.num_rows == 10
    again = FakeFetcher()
    await cache.get("src", "up", rng, STEP, again)
    assert again.calls == []  # now cached
