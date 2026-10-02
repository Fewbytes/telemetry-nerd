"""Chunked series cache: fetch only missing or stale chunks from the source."""

from __future__ import annotations

import asyncio
import hashlib
import json
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
                row[0]: (row[1], row[2], row[3])
                for row in self._con.execute(
                    """SELECT chunk_start, fetched_at, immutable, failed IS NULL
                       FROM cache_chunks WHERE qkey = $q""",
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
            errored = 0
            for cs, result in zip(missing, results, strict=True):
                if isinstance(result, BaseException):
                    if not isinstance(result, SourceError) or isinstance(result, LimitExceeded):
                        raise result
                    failed.append((cs, cs + span - step_ms, f"{type(result).__name__}: {result}"))
                    errored += 1
                    continue
                immutable = cs + span <= now - self.settle_ms
                fetched_at = now
                if result.failed:
                    # The source answered only partially (1h9.12): store the chunk as already
                    # stale so the next read asks again rather than serving it as complete.
                    immutable, fetched_at = False, now - self.recent_ttl_ms
                    old = state.get(cs)
                    if old is not None and old[2]:
                        # 3o0: we hold a complete earlier copy of this chunk. Keep it (a partial
                        # answer is worse data than a complete one) and report unknown only the
                        # buckets after the moment that copy was fetched, which it cannot cover.
                        failed += self._keep_complete(qkey, cs, step_ms, old[0], result, fetched_at)
                        continue
                    failed += result.failed
                self._store(qkey, cs, cs + span - step_ms, result, fetched_at, immutable)
            starts = self.chunk_starts(rng, step_ms)
            if errored and errored == len(starts):
                raise next(r for r in results if isinstance(r, BaseException))
            read = self._read(qkey, rng, step_ms)
            # failed: what this call learned (incl. chunks that errored and were not stored);
            # notes: every chunk's, so a pure cache hit still carries them
            return FetchResult(
                read.buckets, read.series, partial=read.partial,
                failed=tuple(failed), notes=read.notes,
            )  # fmt: skip

    def peek(self, source_identity: str, expr: str, rng: TimeRange, step_ms: int) -> FetchResult:
        """What the cache holds for the range, without fetching. Missing chunks are simply
        absent, but a chunk stored from a partial source answer reports its unknown spans in
        `failed` (3o0): a caller must treat that like a failed fetch, not read it as complete."""
        return self._read(self.query_key(source_identity, expr, step_ms), rng, step_ms)

    def _keep_complete(
        self, qkey: str, cs: int, step_ms: int, old_fetched_at: int, result: FetchResult,
        fetched_at: int,
    ) -> list[tuple[int, int, str]]:  # fmt: skip
        """Rule (3o0): a refetch that came back partial never replaces a complete stored chunk.
        The stored rows stay and the chunk stays eligible for refresh (stale at once, still
        mutable). Buckets at or before the old fetch time are covered by the complete copy; the
        unknown spans are the refetch's failed spans clipped to the buckets after it."""
        first_uncovered = cs + ((old_fetched_at - cs) // step_ms + 1) * step_ms
        spans = [
            (max(a, first_uncovered), b, why) for a, b, why in result.failed if b >= first_uncovered
        ]
        con = self._con
        row = con.execute(
            "SELECT notes FROM cache_chunks WHERE qkey = $q AND chunk_start = $c",
            {"q": qkey, "c": cs},
        ).fetchone()
        old_notes = json.loads(row[0]) if row and row[0] else []
        notes = list(dict.fromkeys([*old_notes, *result.notes]))
        con.execute(
            """UPDATE cache_chunks SET fetched_at = $f, immutable = FALSE, notes = $n
               WHERE qkey = $q AND chunk_start = $c""",
            {"q": qkey, "c": cs, "f": fetched_at, "n": json.dumps(notes) if notes else None},
        )
        return spans

    def _fresh(self, state: tuple[int, bool, bool] | None, now: int) -> bool:
        if state is None:
            return False
        fetched_at, immutable, _ = state
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
                """INSERT INTO cache_chunks
                       (qkey, chunk_start, fetched_at, immutable, partial, failed, notes)
                   VALUES ($q, $c, $f, $i, $p, $x, $n)""",
                {
                    **params,
                    "f": now,
                    "i": immutable,
                    "p": result.partial,
                    "x": json.dumps(result.failed) if result.failed else None,
                    "n": json.dumps(result.notes) if result.notes else None,
                },
            )
            con.commit()
        except Exception:
            con.rollback()
            raise

    def _read(self, qkey: str, rng: TimeRange, step_ms: int) -> FetchResult:
        params = {"q": qkey, "s": rng.start_ms, "e": rng.end_ms}
        starts = self.chunk_starts(rng, step_ms)
        partial = 0
        failed: list[tuple[int, int, str]] = []
        notes: list[str] = []
        for p, f, n in self._con.execute(
            """SELECT COALESCE(partial, 0), failed, notes FROM cache_chunks
               WHERE qkey = $q AND chunk_start BETWEEN $a AND $b ORDER BY chunk_start""",
            {"q": qkey, "a": starts[0], "b": starts[-1]},
        ).fetchall():
            partial += int(p)
            failed += [
                (a, b, why)
                for a, b, why in (json.loads(f) if f else [])
                if b >= rng.start_ms and a <= rng.end_ms
            ]
            notes += json.loads(n) if n else []
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
        return FetchResult(
            buckets, series, partial=partial, failed=tuple(failed),
            notes=tuple(dict.fromkeys(notes)),
        )  # fmt: skip
