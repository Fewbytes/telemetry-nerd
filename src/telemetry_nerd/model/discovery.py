"""What a source can tell us about its metrics (spec §4.1).

Everything here is a claim from one origin, the source's own metadata/APIs. Other origins
(source code, collector docs, knowledge packs) feed the catalog separately and may disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

MetricType = Literal[
    "counter", "gauge", "histogram", "summary", "gaugehistogram", "info", "stateset"
]
HistogramKind = Literal["classic", "native"]

ORIGIN = "source-metadata"


@dataclass(frozen=True)
class MetricInfo:
    name: str
    #: None = the source gave no metadata for this name (not "untyped")
    type: MetricType | None = None
    help: str | None = None
    unit: str | None = None


@dataclass(frozen=True)
class Discovery:
    metrics: tuple[MetricInfo, ...]
    label_names: tuple[str, ...]
    #: histogram base name -> how it is exposed; native has no _bucket/_sum/_count series
    histograms: dict[str, HistogramKind]
    #: series count per metric for the largest metrics only; absent = unknown, not zero
    cardinality: dict[str, int] | None
    #: fraction of metric names that have metadata (0..1)
    metadata_coverage: float
    caveats: tuple[str, ...]
    #: True when any step was cut short or failed (see caveats)
    partial: bool
    origin: str = ORIGIN


def with_histogram_bases(d: Discovery) -> Discovery:
    """`d` with an entry for every classic histogram base name it lacks (telemetry-nerd-6gp).

    Listing `__name__` values only yields X_bucket/X_sum/X_count, so without this the
    histogram X itself has no catalog entry and bindings, suggestions and catalog_bind cannot
    name it. A source that knows the base's metadata already lists it (with that metadata)."""
    have = {m.name for m in d.metrics}
    missing = [b for b, k in sorted(d.histograms.items()) if k == "classic" and b not in have]
    if not missing:
        return d
    return replace(d, metrics=(*d.metrics, *(MetricInfo(b) for b in missing)))
