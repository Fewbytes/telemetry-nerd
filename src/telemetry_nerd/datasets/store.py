"""Immutable datasets: a snapshot of buckets for (source, expr, range, step)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

import duckdb

from telemetry_nerd.datasets.db import fetch_arrow, upsert_series
from telemetry_nerd.model.distribution import COLUMN_SCHEMA, DIST_SCHEMA, BucketScheme, DistResult
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
    scheme: dict | None = None  # bucket scheme of a distribution dataset
    histogram: dict | None = None  # {"selector", "by"}: the histogram a distribution came from
    source_caveats: list[str] = field(default_factory=list)  # conversion caveats from the source
    derived: dict | None = None  # filter() output: {op, from, label, reason, period_ms, ...}
    failed_spans: list[list] = field(default_factory=list)  # [[a, b, reason]] fetch failures

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
        histogram: dict | None = None,
        derived: dict | None = None,
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
            histogram=histogram,
            derived=derived,
            failed_spans=[list(f) for f in result.failed],
        )
        con = self._con
        con.begin()
        try:
            self._insert_meta(meta)
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

    def _insert_meta(self, meta: DatasetMeta) -> None:
        self._con.execute(
            "INSERT INTO datasets VALUES ($id, $m)",
            {"id": meta.id, "m": json.dumps(meta.to_dict())},
        )

    def meta(self, dataset_id: str) -> DatasetMeta:
        row = self._con.execute(
            "SELECT meta FROM datasets WHERE id = $id", {"id": dataset_id}
        ).fetchone()
        if row is None:
            raise NotFound(f"dataset {dataset_id} not found")
        return DatasetMeta(**json.loads(row[0]))

    def list_metas(self) -> list[DatasetMeta]:
        rows = self._con.execute("SELECT meta FROM datasets").fetchall()
        return [DatasetMeta(**json.loads(r[0])) for r in rows]

    def exists(self, dataset_id: str) -> bool:
        return (
            self._con.execute(
                "SELECT 1 FROM datasets WHERE id = $id", {"id": dataset_id}
            ).fetchone()
            is not None
        )

    def get(self, dataset_id: str) -> tuple[DatasetMeta, FetchResult]:
        meta = self.meta(dataset_id)
        if meta.representation == "distribution":
            raise ValueError(f"dataset {dataset_id} is a distribution; use get_distribution")
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

    def put_distribution(
        self,
        *,
        source: str,
        rng: TimeRange,
        step_ms: int,
        resolution_ms: int,
        dist: DistResult,
        histogram: dict,
        n_min: int,
    ) -> DatasetMeta:
        meta = DatasetMeta(
            id=self._new_id("d"),
            source=source,
            expr=dist.expr,
            start_ms=rng.start_ms,
            end_ms=rng.end_ms,
            step_ms=step_ms,
            resolution_ms=resolution_ms,
            representation="distribution",
            created_at_ms=self._clock(),
            n_min=n_min,
            scheme=dist.scheme.to_dict(),
            histogram=histogram,
            source_caveats=list(dist.caveats),
        )
        con = self._con
        con.begin()
        try:
            self._insert_meta(meta)
            for name, table, sql in (
                ("_tn_dr", dist.rows,
                 "INSERT INTO dist_rows SELECT $id, ts_ms, series_id, bucket_lo, bucket_hi, count FROM _tn_dr"),
                ("_tn_dc", dist.columns,
                 "INSERT INTO dist_columns SELECT $id, ts_ms, series_id, n FROM _tn_dc"),
            ):  # fmt: skip
                con.register(name, table)
                try:
                    con.execute(sql, {"id": meta.id})
                finally:
                    con.unregister(name)
            upsert_series(con, dist.series)
            con.commit()
        except Exception:
            con.rollback()
            raise
        return meta

    def get_distribution(self, dataset_id: str) -> tuple[DatasetMeta, DistResult]:
        meta = self.meta(dataset_id)
        if meta.representation != "distribution":
            raise ValueError(f"dataset {dataset_id} is {meta.representation}, not a distribution")
        params = {"id": dataset_id}
        rows = fetch_arrow(
            self._con,
            """SELECT ts_ms, series_id, bucket_lo, bucket_hi, count FROM dist_rows
               WHERE dataset_id = $id ORDER BY series_id, ts_ms, bucket_lo, bucket_hi""",
            params,
            DIST_SCHEMA,
        )
        columns = fetch_arrow(
            self._con,
            "SELECT ts_ms, series_id, n FROM dist_columns WHERE dataset_id = $id ORDER BY series_id, ts_ms",
            params,
            COLUMN_SCHEMA,
        )
        series = fetch_arrow(
            self._con,
            """SELECT series_id, labels FROM series WHERE series_id IN (
                 SELECT DISTINCT series_id FROM dist_columns WHERE dataset_id = $id)
               ORDER BY series_id""",
            params,
            SERIES_SCHEMA,
        )
        scheme = BucketScheme.from_dict(meta.scheme)
        return meta, DistResult(
            rows, columns, series, scheme, meta.expr, tuple(meta.source_caveats)
        )

    def series_count(self, dataset_id: str) -> int:
        table = (
            "dist_columns"
            if self.meta(dataset_id).representation == "distribution"
            else "dataset_rows"
        )
        row = self._con.execute(
            f"SELECT COUNT(DISTINCT series_id) FROM {table} WHERE dataset_id = $id",
            {"id": dataset_id},
        ).fetchone()
        return int(row[0]) if row else 0
