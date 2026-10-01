"""Zero-phase Gaussian filters for bucket series (bead 4ok.9). Pure numpy/polars, no I/O.

Cutoff = the period at half power. Gaps split a series into segments: no kernel crosses a gap
and nothing is interpolated. Points within 3 sigma of a segment edge see a truncated
(renormalised) kernel and are flagged `edge` (filter warm-up/tail: unreliable)."""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
import polars as pl
import pyarrow as pa

from telemetry_nerd.model.series import BUCKET_SCHEMA
from telemetry_nerd.model.time import format_duration as fmt

Kind = Literal["lowpass", "highpass", "bandpass"]
LOW_SIGMA = math.sqrt(math.log(2)) / (2 * math.pi)  # H(1/P) = 1/sqrt2
HIGH_SIGMA = math.sqrt(-math.log(1 - 2**-0.5) / 2) / math.pi  # 1 - H(1/P) = 1/sqrt2
RADIUS, WEAK_STEPS = 3.0, 8
_NAMES = {"lowpass": "low-pass", "highpass": "high-pass", "bandpass": "band-pass"}


@dataclass(frozen=True)
class FilterSpec:
    kind: Kind
    period_ms: int
    period_hi_ms: int | None = None

    def label(self) -> str:
        cut = fmt(self.period_ms) + (f"–{fmt(self.period_hi_ms)}" if self.period_hi_ms else "")
        return f"{cut} {_NAMES[self.kind]} (Gaussian, zero-phase)"

    def check(self, step_ms: int, span_ms: int) -> list[str]:
        if self.period_ms < 2 * step_ms:
            raise ValueError(
                f"cutoff {fmt(self.period_ms)} is below 2 x step ({fmt(2 * step_ms)}): "
                "shorter periods do not exist at this step (Nyquist)"
            )
        if self.kind == "bandpass":
            if self.period_hi_ms is None or self.period_hi_ms < 4 * self.period_ms:
                raise ValueError(
                    "band-pass needs period_hi >= 4 x period so both edges stay at half power"
                )
        elif self.period_hi_ms is not None:
            raise ValueError("period_hi is only for band-pass")
        longest = self.period_hi_ms or self.period_ms
        if longest > span_ms // 2:
            raise ValueError(
                f"cutoff {fmt(longest)} is longer than half the range ({fmt(span_ms // 2)}); "
                "query a longer range"
            )
        return (
            ["weak_filter"]
            if self.kind != "highpass" and self.period_ms < WEAK_STEPS * step_ms
            else []
        )


def _conv_same(x: np.ndarray, k: np.ndarray) -> np.ndarray:
    n = x.size + k.size - 1
    m = 1 << (n - 1).bit_length()
    full = np.fft.irfft(np.fft.rfft(x, m) * np.fft.rfft(k, m), m)[:n]
    r = (k.size - 1) // 2
    return full[r : r + x.size]


def _gauss(y: np.ndarray, sigma_steps: float) -> tuple[np.ndarray, int]:
    r = max(1, math.ceil(RADIUS * sigma_steps))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / max(sigma_steps, 1e-9)) ** 2)
    return _conv_same(y, k) / _conv_same(np.ones_like(y), k), r


def segments(ts: np.ndarray, step_ms: int) -> list[tuple[int, int]]:
    cuts = (np.flatnonzero(np.diff(ts) != step_ms) + 1).tolist()
    b = [0, *cuts, ts.size] if ts.size else []
    return list(itertools.pairwise(b))


@dataclass(frozen=True)
class Filtered:
    values: np.ndarray
    edge: np.ndarray


def apply(spec: FilterSpec, ts: np.ndarray, y: np.ndarray, step_ms: int) -> Filtered:
    out, edge = np.empty(y.size), np.zeros(y.size, bool)
    lo_s, hi_s = LOW_SIGMA * spec.period_ms / step_ms, HIGH_SIGMA * spec.period_ms / step_ms
    for a, b in segments(ts, step_ms):
        seg = y[a:b]
        if spec.kind == "lowpass":
            v, r = _gauss(seg, lo_s)
        elif spec.kind == "highpass":
            base, r = _gauss(seg, hi_s)
            v = seg - base
        else:
            short, _ = _gauss(seg, lo_s)
            long_, r = _gauss(seg, HIGH_SIGMA * spec.period_hi_ms / step_ms)  # type: ignore[operator]
            v = short - long_
        out[a:b] = v
        edge[a : min(b, a + r)] = True
        edge[max(a, b - r) : b] = True
    return Filtered(out, edge)


@dataclass(frozen=True)
class FilterOutput:
    buckets: pa.Table
    edges: dict[str, list[list[int]]]  # series_id -> [[t0, t1], ...] unreliable spans
    removed_share: dict[str, float]  # var(raw - filtered) / var(raw), interior points
    edge_share: float


def _spans(ts: np.ndarray, mask: np.ndarray) -> list[list[int]]:
    out: list[list[int]] = []
    for i in np.flatnonzero(mask):
        if out and ts[i - 1] == out[-1][1] and mask[i - 1]:
            out[-1][1] = int(ts[i])
        else:
            out.append([int(ts[i]), int(ts[i])])
    return out


def filter_buckets(spec: FilterSpec, buckets: pa.Table, step_ms: int) -> FilterOutput:
    df = pl.from_arrow(buckets).with_columns(pl.col("avg").fill_nan(None)).drop_nulls("avg")
    parts, edges, removed, n_edge = [], {}, {}, 0
    for (sid,), g in df.sort("ts_ms").group_by("series_id", maintain_order=True):
        ts, y = g["ts_ms"].to_numpy(), g["avg"].to_numpy()
        f = apply(spec, ts, y, step_ms)
        inner = ~f.edge
        var = float(np.var(y[inner])) if inner.sum() > 1 else 0.0
        removed[sid] = float(np.var((y - f.values)[inner]) / var) if var > 0 else 0.0
        edges[sid], n_edge = _spans(ts, f.edge), n_edge + int(f.edge.sum())
        parts.append(g.with_columns(*(pl.Series(c, f.values) for c in ("avg", "min", "max"))))
    out = (
        pl.concat(parts).select(BUCKET_SCHEMA.names)
        if parts
        else pl.from_arrow(BUCKET_SCHEMA.empty_table())
    )
    return FilterOutput(
        out.to_arrow().cast(BUCKET_SCHEMA), edges, removed, n_edge / max(df.height, 1)
    )


def removed_table(raw: pa.Table, filtered: pa.Table) -> pa.Table:
    """raw - filtered on the same grid: the part a high/band-pass took out (drawn dashed over raw)."""
    r = pl.from_arrow(raw).select("ts_ms", "series_id", "avg", "count")
    f = pl.from_arrow(filtered).select("ts_ms", "series_id", pl.col("avg").alias("f"))
    d = r.join(f, on=["series_id", "ts_ms"]).with_columns(
        (pl.col("avg") - pl.col("f")).alias("avg")
    )
    return (
        d.with_columns(pl.col("avg").alias("min"), pl.col("avg").alias("max"))
        .select(BUCKET_SCHEMA.names)
        .sort(["series_id", "ts_ms"])
        .to_arrow()
        .cast(BUCKET_SCHEMA)
    )
