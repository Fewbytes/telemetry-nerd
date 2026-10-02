"""Chunked series cache: fetch only missing or stale chunks from the source."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable

import duckdb

from telemetry_nerd.datasets.db import fetch_arrow, upsert_series
from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult
from telemetry_nerd.model.time import TimeRange, now_ms
from telemetry_nerd.sources.base import LimitExceeded, SourceError

MAX_CONCURRENT_FETCHES = 8

Fetcher = Callable[[TimeRange], Awaitable[FetchResult]]


class SeriesCache:
    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        *,
        chunk_buckets: int = 720,
        settle_ms: int = 300_000,
        recent_ttl_ms: int = 30_000,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        if chunk_buckets < 2:
            raise ValueError("chunk_buckets must be >= 2")
        self._con = con
        self.chunk_buckets = chunk_buckets
        self.settle_ms = settle_ms
        self.recent_ttl_ms = recent_ttl_ms
        self._clock = clock
        # M1: one lock serializes cache access; fine for a single analyst.
        self._lock = asyncio.Lock()
        # Bound in-flight chunk fetches so one wide query cannot flood the source.
        self._fetch_slots = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)

    @staticmethod
    def query_key(source_identity: str, expr: str, step_ms: int) -> str:
        # Exact expression text: whitespace inside quoted label values is significant.
        return hashlib.sha256(f"{source_identity}\0{expr.strip()}\0{step_ms}".encode()).hexdigest()[
            :16
        ]

    def chunk_starts(self, rng: TimeRange, step_ms: int) -> list[int]:
        span = step_ms * self.chunk_buckets
        first = rng.start_ms // span * span
        return list(range(first, rng.end_ms + 1, span))

    async def get(
        self, source_identity: str, expr: str, rng: TimeRange, step_ms: int, fetch: Fetcher
    ) -> FetchResult:
        qkey = self.query_key(source_identity, expr, step_ms)
        span = step_ms * self.chunk_buckets
        async with self._lock:
            now = self._clock()
            state = {
                row[0]: (row[1], row[2])
                for row in self._con.execute(
                    "SELECT chunk_start, fetched_at, immutable FROM cache_chunks WHERE qkey = $q",
                    {"q": qkey},
                ).fetchall()
            }
            missing = [
                cs for cs in self.chunk_starts(rng, step_ms) if not self._fresh(state.get(cs), now)
            ]

            async def bounded(cs: int) -> FetchResult:
                async with self._fetch_slots:
                    return await fetch(TimeRange(cs, cs + span - step_ms))

            results = await asyncio.gather(*(bounded(cs) for cs in missing), return_exceptions=True)
            failed: list[tuple[int, int, str]] = []
            for cs, result in zip(missing, results, strict=True):
                if isinstance(result, BaseException):
                    if not isinstance(result, SourceError) or isinstance(result, LimitExceeded):
                        raise result
                    failed.append((cs, cs + span - step_ms, f"{type(result).__name__}: {result}"))
                    continue
                immutable = cs + span <= now - self.settle_ms
                self._store(qkey, cs, cs + span - step_ms, result, now, immutable)
            starts = self.chunk_starts(rng, step_ms)
            if failed and len(failed) == len(starts):
                raise next(r for r in results if isinstance(r, BaseException))
            partial = self._con.execute(
                """SELECT COALESCE(SUM(partial), 0) FROM cache_chunks
                   WHERE qkey = $q AND chunk_start BETWEEN $a AND $b""",
                {"q": qkey, "a": starts[0], "b": starts[-1]},
            ).fetchone()
            read = self._read(qkey, rng)
            return FetchResult(
                read.buckets,
                read.series,
                partial=int(partial[0]) if partial else 0,
                failed=tuple(failed),
            )

    def _fresh(self, state: tuple[int, bool] | None, now: int) -> bool:
        if state is None:
            return False
        fetched_at, immutable = state
        return immutable or now - fetched_at < self.recent_ttl_ms

    def _store(
        self, qkey: str, cs: int, ce: int, result: FetchResult, now: int, immutable: bool
    ) -> None:
        con = self._con
        con.begin()
        try:
            params = {"q": qkey, "c": cs}
            con.execute("DELETE FROM cache_buckets WHERE qkey = $q AND chunk_start = $c", params)
            con.execute("DELETE FROM cache_chunks WHERE qkey = $q AND chunk_start = $c", params)
            con.register("_tn_in", result.buckets)
            try:
                con.execute(
                    """INSERT INTO cache_buckets
                       SELECT $q, $c, ts_ms, series_id, avg, min, max, count FROM _tn_in
                       WHERE ts_ms BETWEEN $c AND $e""",
                    {**params, "e": ce},
                )
            finally:
                con.unregister("_tn_in")
            upsert_series(con, result.series)
            con.execute(
                """INSERT INTO cache_chunks (qkey, chunk_start, fetched_at, immutable, partial)
                   VALUES ($q, $c, $f, $i, $p)""",
                {**params, "f": now, "i": immutable, "p": result.partial},
            )
            con.commit()
        except Exception:
            con.rollback()
            raise

    def _read(self, qkey: str, rng: TimeRange) -> FetchResult:
        params = {"q": qkey, "s": rng.start_ms, "e": rng.end_ms}
        buckets = fetch_arrow(
            self._con,
            """SELECT ts_ms, series_id, avg, min, max, count FROM cache_buckets
               WHERE qkey = $q AND ts_ms BETWEEN $s AND $e ORDER BY series_id, ts_ms""",
            params,
            BUCKET_SCHEMA,
        )
        series = fetch_arrow(
            self._con,
            """SELECT series_id, labels FROM series WHERE series_id IN (
                 SELECT DISTINCT series_id FROM cache_buckets
                 WHERE qkey = $q AND ts_ms BETWEEN $s AND $e)
               ORDER BY series_id""",
            params,
            SERIES_SCHEMA,
        )
        return FetchResult(buckets, series)
