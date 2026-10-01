"""Filtered/raw data views of a panel drawn from filter() (bead 4ok.9). Same mechanics as y-views."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DataView = Literal["overlay", "filtered", "removed", "raw"]
VIEW_LABELS = {
    "overlay": "filtered over raw",
    "filtered": "filtered",
    "removed": "raw + removed part",
    "raw": "raw",
}


def offered_views(kind: str) -> list[DataView]:
    return ["overlay", "filtered", "raw"] if kind == "lowpass" else ["filtered", "removed", "raw"]


class SignalViews(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filter: str  # "15m low-pass (Gaussian, zero-phase)"
    kind: Literal["lowpass", "highpass", "bandpass"]
    reason: str = Field(min_length=1, max_length=160)
    offered: list[DataView]
    default: DataView
    selected: DataView | None = None

    @model_validator(mode="after")
    def _ok(self) -> SignalViews:
        if "\n" in self.reason:
            raise ValueError("reason must be one line")
        for v in (self.default, self.selected):
            if v is not None and v not in self.offered:
                raise ValueError(f"view {v!r} is not offered ({', '.join(self.offered)})")
        return self
