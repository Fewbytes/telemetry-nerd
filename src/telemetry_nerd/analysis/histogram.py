"""Histogram query results -> per-step distribution rows (spec §3.2, §4.1).

Counts come from increase() over a window equal to the step, so columns tile time and
are additive: summing over time or over adjacent buckets is valid. Quantiles are never
an input here.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence

import pyarrow as pa

from telemetry_nerd.model.distribution import (
    COLUMN_SCHEMA,
    DIST_SCHEMA,
    VM_PER_DECADE,
    BucketScheme,
    DistResult,
)
from telemetry_nerd.model.series import SERIES_SCHEMA, labels_json, series_id
from telemetry_nerd.model.time import format_duration
from telemetry_nerd.sources.base import SourceError

GROUPING = ("le", "vmrange")
_LABEL = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_TOL = 1e-9

Bucket = tuple[float, float, float]  # (lo, hi, count)


def histogram_expr(selector: str, by: Sequence[str], step_ms: int) -> str:
    """One expression for all forms: classic keeps `le`, VictoriaMetrics keeps `vmrange`,
    native histograms have neither and come back in a `histograms` field."""
    for label in by:
        if not _LABEL.match(label) or label in GROUPING:
            raise ValueError(f"invalid group-by label {label!r}")
    labels = ", ".join([*GROUPING, *by])
    return f"sum by ({labels}) (increase({selector.strip()}[{format_duration(step_ms)}]))"


def cumulative_to_buckets(
    cum: dict[float, float | None],
) -> tuple[list[Bucket], float, set[str]] | None:
    """One series at one step: {le: cumulative count} -> non-zero buckets, n, problems.

    Cumulative counts must not decrease with le, but they can (each le series is
    extrapolated and reset-corrected independently by increase()). Like
    histogram_quantile we carry the running maximum forward and report
    `non_monotonic`. Without a +Inf bucket, n is a lower bound: `missing_inf`.
    """
    points = sorted((le, v) for le, v in cum.items() if v is not None and math.isfinite(v))
    if not points:
        return None
    problems: set[str] = set()
    if points[-1][0] != math.inf:
        problems.add("missing_inf")
    out: list[Bucket] = []
    prev_le, prev_c = -math.inf, 0.0
    for le, c in points:
        if c < prev_c:
            if prev_c - c > _TOL * max(1.0, prev_c):
                problems.add("non_monotonic")
            c = prev_c
        if c > prev_c:
            out.append((prev_le, le, c - prev_c))
        prev_le, prev_c = le, c
    return out, prev_c, problems


def native_buckets(h: dict) -> tuple[list[Bucket], float]:
    """One native histogram sample {count, sum, buckets: [[rule, lo, hi, count], ...]}.
    n is the histogram's own count (histogram_count): it can differ from the bucket sum
    (Play: 8.0008 vs 8.0). Boundary rule 0 = (lo, hi]; the zero bucket is [-z, z]."""
    n = float(h["count"])
    out: list[Bucket] = []
    for _rule, lo, hi, c in h.get("buckets") or []:
        c = float(c)
        if math.isfinite(c) and c > 0:
            out.append((float(lo), float(hi), c))
    return out, n


def native_schema(pairs) -> int | None:
    """Coarsest exponential schema s (bucket growth 2^(2^-s)) with every bucket on the
    2^(i * 2^-s) grid; None for custom bounds (NHCB). The zero bucket is ignored."""
    schemas: set[int] = set()
    for lo, hi in pairs:
        if not (math.isfinite(lo) and math.isfinite(hi)):
            return None
        a, b = sorted((abs(lo), abs(hi)))
        if a == 0 or a == b:
            continue
        s = -math.log2(math.log2(b / a))
        if abs(s - round(s)) > 1e-6:
            return None
        idx = math.log2(b) * 2 ** round(s)
        if abs(idx - round(idx)) > 1e-6:
            return None
        schemas.add(round(s))
    return min(schemas) if schemas else None


def parse_vmrange(text: str) -> tuple[float, float]:
    """VictoriaMetrics bucket label "lo...hi" ("%.3e", "+Inf" allowed)."""
    lo, sep, hi = text.partition("...")
    if not sep:
        raise ValueError(f"malformed vmrange {text!r}")
    a, b = float(lo), float(hi)
    if not a <= b:
        raise ValueError(f"malformed vmrange {text!r}")
    return a, b


def _ts(value: object) -> int:
    return round(float(value) * 1000)  # type: ignore[arg-type]


def _fractional(x: float) -> bool:
    return abs(x - round(x)) > 1e-6


def from_matrix(source: str, result: list[dict], expr: str = "") -> DistResult:
    """Parse a query_range matrix of histogram_expr(...). Raises ValueError when the
    result is malformed or not a histogram."""
    labels_by_sid: dict[str, dict[str, str]] = {}
    classic: dict[tuple[str, int], dict[float, float | None]] = {}
    les_by_sid: dict[str, set[float]] = {}
    cols: dict[tuple[str, int], float] = {}
    rows: list[tuple[int, str, float, float, float]] = []
    forms: set[str] = set()
    pairs: set[tuple[float, float]] = set()
    named: dict[tuple[str, str | None, str | None], str | None] = {}  # (sid, le, vmrange) -> name
    for item in result:
        try:
            labels = dict(item["metric"])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"malformed series {item!r} in query result") from e
        name = labels.pop("__name__", None)
        le, vmrange = labels.pop("le", None), labels.pop("vmrange", None)
        sid = series_id(source, labels)
        key = (sid, le, vmrange)
        if key in named and named[key] != name:  # one series and bucket under two metric names
            raise SourceError(
                f"series {named[key]!r} and {name!r} differ only by __name__ "
                f"(shared labels {labels}): they cannot be told apart in one dataset",
                hint="select one metric name, or aggregate them apart, e.g. sum by (le, ...) (...)",
            )
        named[key] = name
        labels_by_sid[sid] = labels
        if "histograms" in item and item.get("values"):
            raise ValueError(
                "a series mixes float and native histogram samples (migration?); narrow the selector or time range"
            )
        for sample in item.get("histograms") or []:
            forms.add("native")
            t = _ts(sample[0])
            buckets, n = native_buckets(sample[1])
            if not math.isfinite(n):
                continue
            cols[(sid, t)] = n
            for lo, hi, c in buckets:
                rows.append((t, sid, lo, hi, c))
                pairs.add((lo, hi))
        for sample in item.get("values") or []:
            t, x = _ts(sample[0]), float(sample[1])
            if le is not None:
                forms.add("classic")
                edge = float(le)
                les_by_sid.setdefault(sid, set()).add(edge)
                classic.setdefault((sid, t), {})[edge] = x
            elif vmrange is not None:
                forms.add("vmrange")
                lo, hi = parse_vmrange(vmrange)
                if not math.isfinite(x):
                    continue
                cols[(sid, t)] = cols.get((sid, t), 0.0) + x
                if x > 0:
                    rows.append((t, sid, lo, hi, x))
            else:
                raise ValueError("not a histogram: a series has neither an le nor a vmrange label")
    caveats: set[str] = set()
    for (sid, t), cum in classic.items():
        conv = cumulative_to_buckets(cum)
        if conv is None:
            continue
        buckets, n, problems = conv
        caveats |= problems
        cols[(sid, t)] = n
        rows.extend((t, sid, lo, hi, c) for lo, hi, c in buckets)
    if len(forms) > 1:
        raise ValueError(f"selector matched several histogram forms: {', '.join(sorted(forms))}")
    if any(_fractional(n) for n in cols.values()) or any(_fractional(r[4]) for r in rows):
        caveats.add("estimated_counts")
    return _tables(rows, cols, labels_by_sid, _scheme(forms, les_by_sid, pairs), expr, caveats)


def _scheme(
    forms: set[str], les_by_sid: dict[str, set[float]], pairs: set[tuple[float, float]]
) -> BucketScheme:
    if not forms:
        return BucketScheme("none")
    form = next(iter(forms))
    if form == "classic":
        common = set.intersection(*les_by_sid.values()) if les_by_sid else set()
        return BucketScheme("classic", edges=tuple(sorted(e for e in common if math.isfinite(e))))
    if form == "native":
        schema = native_schema(pairs)
        if schema is not None:
            return BucketScheme("native", schema=schema)
        return BucketScheme(
            "custom", edges=tuple(sorted({e for p in pairs for e in p if math.isfinite(e)}))
        )
    if form == "vmrange":
        return BucketScheme("vmrange", per_decade=VM_PER_DECADE)
    raise ValueError(f"unsupported histogram form {form}")


def _tables(rows, cols, labels_by_sid, scheme, expr, caveats) -> DistResult:
    rows.sort(key=lambda r: (r[1], r[0], r[2]))
    keys = sorted(cols, key=lambda k: (k[0], k[1]))
    dist = pa.table(
        {
            "ts_ms": [r[0] for r in rows],
            "series_id": [r[1] for r in rows],
            "bucket_lo": [r[2] for r in rows],
            "bucket_hi": [r[3] for r in rows],
            "count": [r[4] for r in rows],
        },
        schema=DIST_SCHEMA,
    )
    columns = pa.table(
        {
            "ts_ms": [k[1] for k in keys],
            "series_id": [k[0] for k in keys],
            "n": [cols[k] for k in keys],
        },
        schema=COLUMN_SCHEMA,
    )
    sids = sorted(labels_by_sid)
    series = pa.table(
        {"series_id": sids, "labels": [labels_json(labels_by_sid[s]) for s in sids]},
        schema=SERIES_SCHEMA,
    )
    return DistResult(dist, columns, series, scheme, expr, tuple(sorted(caveats)))
