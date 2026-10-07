"""Distribution datasets (spec §3.2): per-step histogram counts, never quantiles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pyarrow as pa

DIST_SCHEMA = pa.schema(
    [
        ("ts_ms", pa.int64()),  # end of the step window (ts - step, ts]
        ("series_id", pa.string()),
        ("bucket_lo", pa.float64()),  # -inf allowed (classic first bucket)
        ("bucket_hi", pa.float64()),  # +inf allowed (overflow bucket)
        ("count", pa.float64()),  # increase() over one step; fractional when extrapolated
    ]
)
COLUMN_SCHEMA = pa.schema([("ts_ms", pa.int64()), ("series_id", pa.string()), ("n", pa.float64())])

# min_samples(0.5): with fewer observations a column's shape is noise (4ok.1 rule)
DIST_N_MIN = 20
VM_PER_DECADE = 18  # VictoriaMetrics vmrange buckets: log-uniform, 18 per decade
_LIST_EDGES = 16

SchemeKind = Literal["none", "classic", "native", "vmrange", "custom", "linear"]


@dataclass(frozen=True)
class BucketScheme:
    kind: SchemeKind
    edges: tuple[float, ...] = ()  # classic/custom: finite edges, ascending
    schema: int | None = None  # native exponential schema: growth 2^(2^-schema)
    per_decade: int | None = None  # vmrange
    width: float | None = None  # linear: bucket width chosen by the query (edges unbounded)
    offset: float | None = None  # linear: edges are offset + k * width

    @property
    def growth(self) -> float | None:
        if self.schema is not None:
            return 2.0 ** (2.0**-self.schema)
        if self.per_decade:
            return 10.0 ** (1 / self.per_decade)
        return None

    @property
    def lower_inclusive(self) -> bool:
        """[lo, hi) buckets (Elasticsearch histogram) vs Prometheus' (lo, hi]: a bucket starting
        at x holds values >= x, so counts at an edge read P(X >= x), not P(X > x)."""
        return self.kind == "linear"

    def describe(self) -> str:
        if self.kind == "linear" and self.width is not None:
            return (
                f"fixed-width buckets of {self.width:g} (offset {self.offset or 0:g}), chosen by "
                "the query; each [lo, hi)"
            )
        if self.kind == "native" and self.schema is not None:
            return f"native exponential, schema {self.schema} (each bucket x{self.growth:.3g})"
        if self.kind == "vmrange":
            return (
                f"VictoriaMetrics vmrange, {self.per_decade} per decade "
                f"(each bucket x{self.growth:.3g})"
            )
        if self.kind in ("classic", "custom") and self.edges:
            name = "classic le" if self.kind == "classic" else "custom"
            if len(self.edges) <= _LIST_EDGES:
                return f"{name} buckets: {', '.join(f'{e:g}' for e in self.edges)}"
            return f"{name} buckets: {len(self.edges)} edges {self.edges[0]:g}..{self.edges[-1]:g}"
        return "no buckets"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "edges": list(self.edges),
            "schema": self.schema,
            "per_decade": self.per_decade,
            "width": self.width,
            "offset": self.offset,
            "description": self.describe(),
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> BucketScheme:
        if not d:
            return cls("none")
        return cls(
            d["kind"], tuple(d.get("edges") or ()), d.get("schema"), d.get("per_decade"),
            d.get("width"), d.get("offset"),
        )  # fmt: skip


@dataclass(frozen=True)
class DistResult:
    rows: pa.Table  # DIST_SCHEMA; only buckets with count > 0
    columns: pa.Table  # COLUMN_SCHEMA; one row per (series, step) with data; n may be 0
    series: pa.Table  # SERIES_SCHEMA
    scheme: BucketScheme
    expr: str = ""
    caveats: tuple[str, ...] = ()
    # fetch spans the source answered only partially: inclusive step-ts span, "Class: message"
    failed: tuple[tuple[int, int, str], ...] = ()


QUANTILE_CHOICES = (0.5, 0.9, 0.95, 0.99, 0.999)  # selectable percentile bands
DEFAULT_QUANTILES = (0.5, 0.9, 0.99)
