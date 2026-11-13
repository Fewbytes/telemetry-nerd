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
    partial: int = 0  # incomplete source cells dropped (a bucket the source only half-returned)
    # chunks that failed: inclusive bucket-ts span and "ErrorClass: message" (bucket_state UNKNOWN)
    failed: tuple[tuple[int, int, str], ...] = ()
    # informational source warnings (not missing data): shown as `source_warning` caveats
    notes: tuple[str, ...] = ()
    # ids of series whose end a staleness marker proves (they stopped before their last sample
    # plus the lookback): the end is not trustworthy as "gone" (SUSPECT, 1h9.14).
    # None = the source cannot tell, which is not the same as none.
    stale_ended: tuple[str, ...] | None = None


def empty_result() -> FetchResult:
    return FetchResult(BUCKET_SCHEMA.empty_table(), SERIES_SCHEMA.empty_table())
