"""Y-views: switchable y ranges for time-series panels (spec §6.2; bead 2as.17).

Pure: no I/O. 2as.10 adds `reference` and `semantic` to YMode through this same model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import polars as pl
import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field, model_validator

YMode = Literal["auto", "zero", "data", "meaningful", "band", "log", "indexed"]
MAX_SUGGESTIONS = 4
BUILTIN_LABELS: dict[str, str] = {
    "auto": "auto",
    "zero": "from zero",
    "data": "y zoomed to data",
    "meaningful": "meaningful buckets only",
    "band": "y band",
    "log": "log scale",
    "indexed": "indexed",
}
INDEX_LABELS = {"window": "÷ own mean", "previous": "÷ previous window", "week": "÷ last week"}


class YView(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    mode: YMode
    label: str = Field(min_length=1, max_length=40)
    reason: str | None = Field(default=None, max_length=160)
    lo: float | None = Field(default=None, allow_inf_nan=False)
    hi: float | None = Field(default=None, allow_inf_nan=False)
    baseline: Literal["window", "previous", "week"] | None = None  # indexed views only
    id: str | None = None  # "v1".. for suggestions
    author: Literal["claude", "user"] = "user"

    @model_validator(mode="after")
    def _shape(self) -> YView:
        if self.reason is not None and "\n" in self.reason:
            raise ValueError("reason must be one line")
        if self.mode == "band":
            if self.lo is None or self.hi is None or not self.lo < self.hi:
                raise ValueError("band views need finite lo < hi")
        elif self.lo is not None or self.hi is not None:
            raise ValueError("lo/hi are only for band views")
        if (self.mode == "indexed") != (self.baseline is not None):
            raise ValueError(
                "indexed views need a baseline (window, previous or week); other views take none"
            )
        if self.author == "claude" and not self.reason:
            raise ValueError("a suggested view needs a one-line reason")
        return self


@dataclass(frozen=True)
class ValueStats:
    lo: float | None
    hi: float | None
    quantile: bool
    meaningful_lo: float | None = None
    meaningful_hi: float | None = None
    low_n: int = 0


def _ext(s: pl.Series) -> tuple[float | None, float | None]:
    s = s.drop_nulls().drop_nans()
    return (None, None) if s.is_empty() else (float(s.min()), float(s.max()))  # type: ignore[arg-type]


def value_stats(buckets: pa.Table, representation: str, n_min: int | None) -> ValueStats:
    df = pl.from_arrow(buckets)
    assert isinstance(df, pl.DataFrame)
    if representation != "quantile":
        lo, hi = _ext(pl.concat([df["avg"], df["min"], df["max"]]))
        return ValueStats(lo, hi, quantile=False)
    drawn = df.filter(pl.col("avg").is_not_null() & pl.col("avg").is_not_nan())
    lo, hi = _ext(drawn["avg"])
    if n_min is None:
        return ValueStats(lo, hi, True, lo, hi, 0)
    ok = drawn["count"].fill_null(0) >= n_min
    mlo, mhi = _ext(drawn.filter(ok)["avg"])
    return ValueStats(lo, hi, True, mlo, mhi, int((~ok).sum()))


def check_view(view: YView, st: ValueStats) -> list[str]:
    """Refusals raise ValueError (they reach Claude/the user as errors); warnings are returned."""
    if view.mode in ("auto", "indexed"):  # indexed needs the data: charts.indexed.check_index
        return []
    if st.lo is None or st.hi is None:
        raise ValueError("the panel has no drawn values; only the auto view applies")
    if view.mode == "log" and st.lo <= 0:
        raise ValueError(f"log scale needs every drawn value > 0 (min is {st.lo:g})")
    if view.mode == "meaningful":
        if not st.quantile:
            raise ValueError(
                "meaningful-only applies to percentile panels (it hides buckets with n < n_min)"
            )
        if st.meaningful_lo is None:
            raise ValueError(
                "no bucket has enough observations (n ≥ n_min); nothing meaningful to range over"
            )
    if view.mode == "band":
        assert view.lo is not None and view.hi is not None
        if view.hi < st.lo or view.lo > st.hi:
            raise ValueError(
                f"band [{view.lo:g}, {view.hi:g}] contains no data (data spans {st.lo:g}–{st.hi:g})"
            )
        out = []
        if view.lo < st.lo:
            out.append(f"band extends below the data (min {st.lo:g})")
        if view.hi > st.hi:
            out.append(f"band extends above the data (max {st.hi:g})")
        return out
    return []
