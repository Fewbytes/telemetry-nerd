"""T1 sample statistics: what a short window of raw values says about a metric's behaviour.

Pure numpy. A counter never decreases except by resetting toward zero, so a decrease that keeps at
least half the value ("small decrease") is evidence of a gauge, and a metric that only ever grows
is evidence of a counter. A short window can only suggest: callers treat it so.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

#: a decrease below this fraction of the previous value is a counter reset
RESET_FRACTION = 0.5
MIN_SERIES_SAMPLES = 10  # a series with fewer samples does not vote
COUNTER_MIN_INCREASES = 5
NONNEG_MIN_SAMPLES = 30


@dataclass(frozen=True)
class SeriesStats:
    n: int
    min: float | None
    max: float | None
    negatives: int
    increases: int
    decreases: int
    resets: int
    small_decreases: int
    integral: bool
    constant: bool


def scan_series(values: Sequence[float | None]) -> SeriesStats:
    v = np.array([x for x in values if x is not None and np.isfinite(x)], dtype=float)
    if v.size == 0:
        return SeriesStats(0, None, None, 0, 0, 0, 0, 0, False, False)
    d = np.diff(v)
    prev = v[:-1]
    dec = d < 0
    reset = dec & (v[1:] < prev * RESET_FRACTION)
    return SeriesStats(
        n=int(v.size),
        min=float(v.min()),
        max=float(v.max()),
        negatives=int((v < 0).sum()),
        increases=int((d > 0).sum()),
        decreases=int(dec.sum()),
        resets=int(reset.sum()),
        small_decreases=int((dec & ~reset).sum()),
        integral=bool(np.all(v == np.round(v))),
        constant=bool(v.size > 1 and np.all(v == v[0])),
    )


@dataclass(frozen=True)
class SampleStats:
    """Pooled over a metric's series."""

    series: int
    voting: int  # series with enough samples to judge
    n: int
    min: float | None
    max: float | None
    negatives: int
    increases: int
    decreases: int
    resets: int
    small_decreases: int
    gauge_voters: int  # voting series with at least one small decrease
    integral: bool
    constant: bool  # every series constant

    @property
    def counter_like(self) -> bool:
        return (
            self.voting > 0
            and self.increases >= COUNTER_MIN_INCREASES
            and self.small_decreases == 0
            and self.negatives == 0
        )

    @property
    def gauge_like(self) -> bool:
        return self.voting > 0 and self.gauge_voters * 2 >= self.voting

    @property
    def grows_only(self) -> bool:
        return self.increases >= COUNTER_MIN_INCREASES and self.decreases == 0

    @property
    def nonnegative(self) -> bool:
        return self.n >= NONNEG_MIN_SAMPLES and self.negatives == 0

    @property
    def verdict(self) -> str:
        if self.n == 0:
            return "no data"
        if self.constant:
            return "constant"
        if self.gauge_like:
            return "gauge-like"
        if self.counter_like:
            return "counter-like"
        return "inconclusive"


def pool(stats: Iterable[SeriesStats]) -> SampleStats:
    ss = [s for s in stats if s.n > 0]
    voting = [s for s in ss if s.n >= MIN_SERIES_SAMPLES]
    lows = [s.min for s in ss if s.min is not None]
    highs = [s.max for s in ss if s.max is not None]
    return SampleStats(
        series=len(ss),
        voting=len(voting),
        n=sum(s.n for s in ss),
        min=min(lows) if lows else None,
        max=max(highs) if highs else None,
        negatives=sum(s.negatives for s in ss),
        increases=sum(s.increases for s in ss),
        decreases=sum(s.decreases for s in ss),
        resets=sum(s.resets for s in ss),
        small_decreases=sum(s.small_decreases for s in ss),
        gauge_voters=sum(1 for s in voting if s.small_decreases > 0),
        integral=bool(ss) and all(s.integral for s in ss),
        constant=bool(ss) and all(s.constant for s in ss),
    )
