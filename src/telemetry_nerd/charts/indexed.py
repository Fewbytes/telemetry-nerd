"""Indexed view: each series as a ratio to a common baseline, log axis, 1 centred (bead 4ok.14;
guide §1, §3). The sanctioned alternative to dual y-axes. Pure.

A ratio needs a baseline > 0: series without one are not indexed and are named. Percentile
series are never indexed to a window mean (that averages percentiles), only pointwise with
n >= n_min on both sides."""

from __future__ import annotations

import polars as pl
import pyarrow as pa
import pyarrow.compute as pc


def _drawn(t: pa.Table) -> pl.DataFrame:
    df = pl.from_arrow(t)
    assert isinstance(df, pl.DataFrame)
    return df.filter(pl.col("avg").is_not_null() & pl.col("avg").is_finite())


def window_baselines(buckets: pa.Table) -> dict[str, float | None]:
    """Count-weighted mean of bucket means = the mean of every sample in the window (mergeable)."""
    df = _drawn(buckets).with_columns(pl.col("count").fill_null(0))
    n = pl.col("count").sum()
    g = df.group_by("series_id").agg(
        pl.when(n > 0).then((pl.col("avg") * pl.col("count")).sum() / n).otherwise(None).alias("b")
    )
    return dict(zip(g["series_id"].to_list(), g["b"].to_list(), strict=True))


def shifted(ref: pa.Table, shift_ms: int) -> pa.Table:
    """Reference buckets moved onto the panel's time grid."""
    i = ref.schema.get_field_index("ts_ms")
    return ref.set_column(i, ref.schema.field(i), pc.add(ref["ts_ms"], shift_ms))


def check_index(
    baseline: str,
    representation: str,
    n_min: int | None,
    cur: pa.Table,
    ref_on_grid: pa.Table | None,
    names: dict[str, str],
) -> list[str]:
    quantile = representation == "quantile"
    ids = sorted(set(cur["series_id"].to_pylist()))
    hidden = 0
    if baseline == "window":
        if quantile:
            raise ValueError(
                "a percentile series cannot be indexed to its mean over the window: that "
                "would average percentiles; use baseline=previous or week (pointwise, n-gated)"
            )
        b = window_baselines(cur)
        bad = [s for s in ids if (b.get(s) or 0) <= 0]
    else:
        if ref_on_grid is None:
            raise ValueError(f"no {baseline} reference fetched for this panel yet")
        c = _drawn(cur).select("series_id", "ts_ms")
        r = _drawn(ref_on_grid).select(
            "series_id", "ts_ms", pl.col("avg").alias("base"), pl.col("count").alias("bn")
        )
        ok = pl.col("base").fill_null(0) > 0
        if quantile and n_min is not None:
            ok = ok & (pl.col("bn").fill_null(0) >= n_min)
        j = c.join(r, on=["series_id", "ts_ms"], how="left").with_columns(ok.alias("ok"))
        per = dict(j.group_by("series_id").agg(pl.col("ok").sum()).iter_rows())
        bad = [s for s in ids if per.get(s, 0) == 0]
        hidden = int(j.filter(~pl.col("series_id").is_in(bad) & ~pl.col("ok")).height)
    if len(bad) == len(ids):
        raise ValueError(
            "no series has a baseline > 0 (missing, zero or negative): nothing to index"
        )
    out = []
    if bad:
        out.append(
            f"not indexed (baseline missing or ≤ 0): {', '.join(names.get(s, s) for s in bad)}"
        )
    if hidden:
        out.append(f"{hidden} step(s) without a usable baseline are not drawn")
    return out
