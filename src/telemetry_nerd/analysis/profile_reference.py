"""A marginal against the operating profile (bead 2as.22). Pure.

The profile holds HOURLY values over a long window; a panel may be finer. Like is compared with
like: the panel's buckets are resampled to whole hours (count-weighted means of the scrape-sample
averages), and the profile's hours are restricted to the same seasonal buckets the window covers
when the profile found a seasonal pattern (a morning window is not judged against the night)."""

from __future__ import annotations

import numpy as np
import polars as pl
import pyarrow as pa

from telemetry_nerd.analysis.profile import HOUR_MS, Period, _bucket_index


class ProfileReferenceRefused(ValueError):
    """The panel cannot be compared with hourly profile values without misleading."""


def hourly_means(buckets: pa.Table, step_ms: int) -> tuple[pl.DataFrame, int]:
    """(series_id, hour_start_ms, avg) per COMPLETE hour, and the number of buckets dropped
    because their hour is only partly inside the window. Bucket ts is the END of its bucket."""
    if step_ms > HOUR_MS:
        raise ProfileReferenceRefused(
            f"the panel's step ({step_ms // 60_000}m) is coarser than the profile's hour; "
            "re-query at 1h or finer to compare with the profile"
        )
    if HOUR_MS % step_ms:
        raise ProfileReferenceRefused(
            "the panel's step does not divide an hour, so its buckets cannot be rolled up to hours"
        )
    per_hour = HOUR_MS // step_ms
    df = pl.from_arrow(buckets)
    assert isinstance(df, pl.DataFrame)
    df = df.filter(pl.col("avg").is_not_null() & pl.col("avg").is_finite())
    if df.height == 0:
        return pl.DataFrame(
            schema={"series_id": pl.String, "hour_ms": pl.Int64, "avg": pl.Float64}
        ), 0
    df = df.with_columns(
        ((pl.col("ts_ms") - step_ms) // HOUR_MS * HOUR_MS).alias("hour_ms"),
        pl.col("count").fill_null(1).clip(lower_bound=1).alias("w"),
    )
    g = df.group_by("series_id", "hour_ms").agg(
        pl.len().alias("n"),
        ((pl.col("avg") * pl.col("w")).sum() / pl.col("w").sum()).alias("avg"),
    )
    full = g.filter(pl.col("n") >= per_hour)
    dropped = int(g.filter(pl.col("n") < per_hour)["n"].sum() or 0)
    return full.select("series_id", "hour_ms", "avg").sort("series_id", "hour_ms"), dropped


def matched_hours(
    now_hours_ms: np.ndarray, ref_hours_ms: np.ndarray, period: Period, tz: str
) -> np.ndarray:
    """Mask over the profile's hours: those in a seasonal bucket the window also covers."""
    if period == "none" or now_hours_ms.size == 0:
        return np.ones(ref_hours_ms.size, bool)
    covered = np.unique(_bucket_index(period, now_hours_ms, tz))
    return np.isin(_bucket_index(period, ref_hours_ms, tz), covered)


def describe(period: Period, days: float, tz: str, rate_window: str | None) -> str:
    """The reference's label: what it is and how it was matched, so the reader cannot mistake it."""
    how = {
        "hour_of_week": f"same hours of the week, {tz}",
        "hour_of_day": f"same hours of the day, {tz}",
        "none": "all hours: no seasonal pattern found",
    }[period]
    rate = f"; rates over {rate_window}" if rate_window else ""
    return f"normal profile ({days:g}d hourly, {how}{rate})"
