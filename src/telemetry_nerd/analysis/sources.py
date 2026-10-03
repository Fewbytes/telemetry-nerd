"""Sources of variation (SPC vocabulary; docs/principles.md principle 8; beads 60j, gkk).

Every op that reports variation or a deviation labels each item with its source:

- COMMON cause: the system's inherent variability as it is (a small system's fluctuation,
  cycles, strata). Limits and bands are its envelope; act by changing the system, never chase a
  point inside it.
- SPECIAL cause: assignable (out-of-limit points, run rules, shifts, drift, transients, leaving
  steady state, an unusual window or member). Investigate.
- MEASUREMENT system: instrumentation error or bias (sampling, edges, unmeasured segments, units,
  missing members, partial / untrusted / missing data, unknown input uncertainty).
- UNDETERMINED: the data cannot tell the sources apart. Said, never guessed.

Pure: constants, wording and small helpers shared by analysis/ and core/ ops.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

COMMON = "common_cause"
SPECIAL = "special_cause"
MEASUREMENT = "measurement_system"
UNDETERMINED = "undetermined"
SOURCES = (COMMON, SPECIAL, MEASUREMENT, UNDETERMINED)
Source = Literal["common_cause", "special_cause", "measurement_system", "undetermined"]

TEXT = {
    COMMON: "common cause",
    SPECIAL: "special cause",
    MEASUREMENT: "measurement system",
    UNDETERMINED: "source undetermined",
}

MEANING = {
    COMMON: "inherent variability of the system as it is (the envelope): act by changing the "
    "system, never chase points inside it",
    SPECIAL: "assignable: investigate the cause",
    MEASUREMENT: "instrumentation error or bias: qualify or fix the measurement before reading "
    "the process",
    UNDETERMINED: "the data cannot tell common cause, special cause and measurement apart",
}

#: caveat codes that describe the measurement system (instrumentation, coverage, data trust),
#: not the process: each becomes a measurement-system item of an op's `variation` list
MEASUREMENT_CAVEATS: dict[str, str] = {
    "gaps": "gaps in the series (steps without data)",
    "partial": "partial buckets (fewer samples than expected)",
    "missing_data": "missing or partial samples",
    "untrusted_data": "data unknown for part of the range (failed fetch or unobservable)",
    "unobservable_counts": "sample counts unobservable",
    "post_gap_spike": "a value right after a gap carries the whole gap's change",
    "interval_change": "the scrape interval changed within the window",
    "interval_differs": "a series sampled at another rate than configured",
    "absent_part": "a series first seen after the window starts (membership change is normal "
    "lifecycle, not a fault; it moves n and so the aggregates)",
    "coarsened": "coarsened to a wider step than requested",
    "fake_resolution": "step finer than the scrape interval",
    "sampling_artifact": "a spectral peak at the sampling interval",
    "extrapolated": "rate extrapolated at the window edges",
    "resets": "counter resets",
    "members_missing": "members not reporting while alive",
    "members_skipped": "members with too little data to test",
    "member_coverage_unknown": "member coverage unknown",
    "members_partial": "members with partial data",
    "skipped_series": "series skipped",
    "latency_unit_assumed": "latency unit assumed",
    "estimated_counts": "counts estimated",
    "counts_unknown": "counts unknown",
    "n_unknown": "sample count unknown",
    "no_uncertainty": "input uncertainty unknown",
    "input_uncertainty_unknown": "input uncertainty unknown: intervals are lower bounds",
    "uncertainty_not_propagated": "input intervals not propagated: intervals are lower bounds",
}


def text(source: str | None) -> str:
    return TEXT.get(source or UNDETERMINED, TEXT[UNDETERMINED])


def item(source: str, finding: str, **extra) -> dict:
    """One labelled finding of an op's `variation` list."""
    if source not in SOURCES:
        raise ValueError(f"unknown variation source {source!r}: use {', '.join(SOURCES)}")
    return {"source": source, "finding": finding, **extra}


def measurement_items(caveats: Iterable[str]) -> list[dict]:
    """Measurement-system items for the caveats that describe the instruments (in order)."""
    return [
        item(MEASUREMENT, MEASUREMENT_CAVEATS[c], caveat=c)
        for c in dict.fromkeys(caveats)
        if c in MEASUREMENT_CAVEATS
    ]


def measurement_caveats(caveats: Iterable[str]) -> list[str]:
    return [c for c in dict.fromkeys(caveats) if c in MEASUREMENT_CAVEATS]
