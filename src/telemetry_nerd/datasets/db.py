"""DuckDB holds series data. Only the server process opens the file (single writer)."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa

_SCHEMA = [
    "CREATE TABLE IF NOT EXISTS series (series_id VARCHAR PRIMARY KEY, labels VARCHAR NOT NULL)",
    """CREATE TABLE IF NOT EXISTS cache_chunks (
        qkey VARCHAR, chunk_start BIGINT, fetched_at BIGINT, immutable BOOLEAN,
        PRIMARY KEY (qkey, chunk_start))""",
    """CREATE TABLE IF NOT EXISTS cache_buckets (
        qkey VARCHAR, chunk_start BIGINT, ts_ms BIGINT, series_id VARCHAR,
        avg DOUBLE, min DOUBLE, max DOUBLE, count BIGINT)""",
    "ALTER TABLE cache_chunks ADD COLUMN IF NOT EXISTS partial BIGINT DEFAULT 0",
    "CREATE TABLE IF NOT EXISTS datasets (id VARCHAR PRIMARY KEY, meta VARCHAR NOT NULL)",
    """CREATE TABLE IF NOT EXISTS dataset_rows (
        dataset_id VARCHAR, ts_ms BIGINT, series_id VARCHAR,
        avg DOUBLE, min DOUBLE, max DOUBLE, count BIGINT)""",
    """CREATE TABLE IF NOT EXISTS dist_rows (
        dataset_id VARCHAR, ts_ms BIGINT, series_id VARCHAR,
        bucket_lo DOUBLE, bucket_hi DOUBLE, count DOUBLE)""",
    """CREATE TABLE IF NOT EXISTS dist_columns (
        dataset_id VARCHAR, ts_ms BIGINT, series_id VARCHAR, n DOUBLE)""",
    # declared uncertainty of a value (e.g. a code output's CI); NULL when none was declared
    "ALTER TABLE dataset_rows ADD COLUMN IF NOT EXISTS lo DOUBLE",
    "ALTER TABLE dataset_rows ADD COLUMN IF NOT EXISTS hi DOUBLE",
]


def open_duckdb(path: str | Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(path))
    for statement in _SCHEMA:
        con.execute(statement)
    return con


def upsert_series(con: duckdb.DuckDBPyConnection, series: pa.Table) -> None:
    con.register("_tn_series", series)
    try:
        con.execute("INSERT OR IGNORE INTO series SELECT series_id, labels FROM _tn_series")
    finally:
        con.unregister("_tn_series")


def fetch_arrow(
    con: duckdb.DuckDBPyConnection, sql: str, params: dict, schema: pa.Schema
) -> pa.Table:
    return con.execute(sql, params).to_arrow_table().cast(schema)
