"""Series identity and the bucket representation shared by every layer."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import pyarrow as pa

BUCKET_SCHEMA = pa.schema(
    [
        ("ts_ms", pa.int64()),
        ("series_id", pa.string()),
        ("avg", pa.float64()),
        ("min", pa.float64()),
        ("max", pa.float64()),
        ("count", pa.int64()),
    ]
)

SERIES_SCHEMA = pa.schema([("series_id", pa.string()), ("labels", pa.string())])


def labels_json(labels: dict[str, str]) -> str:
    return json.dumps(dict(sorted(labels.items())), separators=(",", ":"))


def series_id(source: str, labels: dict[str, str]) -> str:
    canonical = json.dumps(
        {"source": source, "labels": dict(sorted(labels.items()))}, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class FetchResult:
    buckets: pa.Table  # BUCKET_SCHEMA
    series: pa.Table  # SERIES_SCHEMA


def empty_result() -> FetchResult:
    return FetchResult(BUCKET_SCHEMA.empty_table(), SERIES_SCHEMA.empty_table())
