"""Fraction of observations above a threshold, from histogram bucket counts (spec §5.1).

Exact when the threshold is a source bucket edge; otherwise bounded by the bucket that
contains it. Never interpolated. The sampling uncertainty (Wilson interval) is separate
from the bucket bound.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

Z95 = 1.959964


def wilson(successes: float, n: float, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for a proportion (successes may be fractional: increase())."""
    if n <= 0:
        return 0.0, 1.0
    p = min(1.0, max(0.0, successes / n))
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass(frozen=True)
class Over:
    n: float
    above: float  # count in buckets entirely above x
    inside: float  # count in the bucket containing x (0 when x is a source edge)
    bucket: tuple[float, float] | None  # the containing bucket when inexact

    @property
    def exact(self) -> bool:
        return self.inside == 0

    @property
    def lo(self) -> float:
        return self.above / self.n

    @property
    def hi(self) -> float:
        return (self.above + self.inside) / self.n


def fraction_over(
    los: Sequence[float], his: Sequence[float], counts: Sequence[float], x: float
) -> Over | None:
    n = sum(counts)
    if n <= 0:
        return None
    above = inside = 0.0
    bucket: tuple[float, float] | None = None
    for lo, hi, c in zip(los, his, counts, strict=True):
        if c <= 0:
            continue
        if lo >= x:
            above += c
        elif x < hi:  # lo < x < hi: x falls strictly inside this bucket
            inside += c
            bucket = (lo, hi)
    return Over(n=n, above=above, inside=inside, bucket=bucket)
