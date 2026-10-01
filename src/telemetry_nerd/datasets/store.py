"""Immutable datasets: a snapshot of buckets for (source, expr, range, step)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass

import duckdb

from telemetry_nerd.datasets.db import fetch_arrow, upsert_series
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult
from telemetry_nerd.model.time import TimeRange, now_ms


@dataclass(frozen=True)
class DatasetMeta:
    id: str
    source: str
    expr: str
    start_ms: int
    end_ms: int
    step_ms: int
    resolution_ms: int
    representation: str = "bucket_agg"
    created_at_ms: int = 0
    partial: int = 0  # incomplete source cells dropped while fetching
    quantile: float | None = None  # q of a quantile dataset
    n_min: int | None = None  # observations per bucket for a meaningful quantile

    def to_dict(self) -> dict:
        return asdict(self)


class DatasetStore:
    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        new_id: Callable[[str], str],
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self._con = con
        self._new_id = new_id
        self._clock = clock

    def put(
        self,
        *,
        source: str,
        expr: str,
        rng: TimeRange,
        step_ms: int,
        resolution_ms: int,
        result: FetchResult,
        representation: str = "bucket_agg",
        quantile: float | None = None,
        n_min: int | None = None,
    ) -> DatasetMeta:
        meta = DatasetMeta(
            id=self._new_id("d"),
            source=source,
            expr=expr,
            start_ms=rng.start_ms,
            end_ms=rng.end_ms,
            step_ms=step_ms,
            resolution_ms=resolution_ms,
            created_at_ms=self._clock(),
            partial=result.partial,
            representation=representation,
            quantile=quantile,
            n_min=n_min,
        )
        con = self._con
        con.begin()
        try:
            con.execute(
                "INSERT INTO datasets VALUES ($id, $m)",
                {"id": meta.id, "m": json.dumps(meta.to_dict())},
            )
            con.register("_tn_ds", result.buckets)
            try:
                con.execute(
                    """INSERT INTO dataset_rows
                       SELECT $id, ts_ms, series_id, avg, min, max, count FROM _tn_ds""",
                    {"id": meta.id},
                )
            finally:
                con.unregister("_tn_ds")
            upsert_series(con, result.series)
            con.commit()
        except Exception:
            con.rollback()
            raise
        return meta

    def exists(self, dataset_id: str) -> bool:
        return (
            self._con.execute(
                "SELECT 1 FROM datasets WHERE id = $id", {"id": dataset_id}
            ).fetchone()
            is not None
        )

    def get(self, dataset_id: str) -> tuple[DatasetMeta, FetchResult]:
        row = self._con.execute(
            "SELECT meta FROM datasets WHERE id = $id", {"id": dataset_id}
        ).fetchone()
        if row is None:
            raise NotFound(f"dataset {dataset_id} not found")
        meta = DatasetMeta(**json.loads(row[0]))
        params = {"id": dataset_id}
        buckets = fetch_arrow(
            self._con,
            """SELECT ts_ms, series_id, avg, min, max, count FROM dataset_rows
               WHERE dataset_id = $id ORDER BY series_id, ts_ms""",
            params,
            BUCKET_SCHEMA,
        )
        series = fetch_arrow(
            self._con,
            """SELECT series_id, labels FROM series WHERE series_id IN (
                 SELECT DISTINCT series_id FROM dataset_rows WHERE dataset_id = $id)
               ORDER BY series_id""",
            params,
            SERIES_SCHEMA,
        )
        return meta, FetchResult(buckets, series)
