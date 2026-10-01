"""Where a quantile lies in a histogram: the SOURCE bucket holding it (spec §5.1).

Never an interpolated value, and only where the column holds enough observations for q
to mean anything (n >= min_samples(q), rule from 4ok.1). Inputs are counts, so columns
may be summed over time first (additive); they must NOT be value-merged.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import polars as pl

from telemetry_nerd.analysis.exprkind import min_samples

_EPS = 1e-12
_KEYS = ["series_id", "ts_ms"]

Bucket = tuple[float, float, float]


def q_key(q: float) -> str:
    """JSON key for q; equals JS String(q) for QUANTILE_CHOICES ("0.5", "0.999")."""
    return f"{q:g}"


def quantile_bucket(buckets: Iterable[Bucket], q: float) -> tuple[float, float] | None:
    ordered = sorted(buckets, key=lambda b: (b[1], b[0]))
    total = sum(c for _, _, c in ordered)
    if total <= 0:
        return None
    acc = 0.0
    for lo, hi, c in ordered:
        acc += c
        if acc >= q * total * (1 - _EPS):
            return lo, hi
    return None


def column_quantiles(
    rows: pl.DataFrame, cols: pl.DataFrame, qs: Sequence[float]
) -> dict[str, dict[str, dict[str, list]]]:
    """{series_id: {q_key: {ts, lo, hi}}}: per column, the bucket holding q, n-gated."""
    walk = (
        rows.filter(pl.col("count") > 0)
        .sort([*_KEYS, "bucket_hi", "bucket_lo"])
        .with_columns(
            pl.col("count").cum_sum().over(_KEYS).alias("acc"),
            pl.col("count").sum().over(_KEYS).alias("total"),
        )
        .join(cols.select([*_KEYS, "n"]), on=_KEYS, how="inner")
    )
    out: dict[str, dict[str, dict[str, list]]] = {}
    for q in qs:
        hit = (
            walk.filter(
                (pl.col("n") >= min_samples(q))
                & (pl.col("acc") >= q * pl.col("total") * (1 - _EPS))
            )
            .group_by(_KEYS, maintain_order=True)
            .first()
            .sort(_KEYS)
        )
        for sid in hit["series_id"].unique(maintain_order=True).to_list():
            g = hit.filter(pl.col("series_id") == sid)
            out.setdefault(sid, {})[q_key(q)] = {
                "ts": g["ts_ms"].to_list(),
                "lo": g["bucket_lo"].to_list(),
                "hi": g["bucket_hi"].to_list(),
            }
    return out
