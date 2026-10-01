"""Reference windows for "now vs reference" comparisons (spec §6.2/§6.4; bead 4ok.6).

Pure. A dataset's buckets sit at ts in [start, end] and each covers (ts - step, ts], so the
window is end - start + step long. A reference is the same grid shifted back by shift_ms."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RefMode = Literal["previous", "week"]
REF_MODES: tuple[str, ...] = ("previous", "week")
WEEK_MS = 7 * 86_400_000


@dataclass(frozen=True)
class RefWindow:
    mode: str
    start_ms: int
    end_ms: int
    shift_ms: int
    label: str


def reference_window(start_ms: int, end_ms: int, step_ms: int, mode: str) -> RefWindow:
    if step_ms <= 0 or end_ms < start_ms:
        raise ValueError("the panel window must have end >= start and a positive step")
    span = end_ms - start_ms + step_ms
    if mode == "previous":
        shift, label = span, "previous window"
    elif mode == "week":
        if WEEK_MS % step_ms:
            raise ValueError(
                "the step does not divide a week, so last week's buckets do not "
                "line up; use reference=previous or re-query at a standard step"
            )
        if span > WEEK_MS:
            raise ValueError(
                "the window is longer than a week, so last week overlaps it; use reference=previous"
            )
        shift, label = WEEK_MS, "same window last week"
    elif mode == "profile":
        raise ValueError(
            "a marginal against the operating profile is not available yet "
            "(bead 2as.22); use previous or week for now"
        )
    else:
        raise ValueError(f"unknown reference {mode!r}: use previous or week")
    return RefWindow(mode, start_ms - shift, end_ms - shift, shift, label)
