"""Helpers for tests over tests/fixtures/missing-data (recorded by scripts/record_missing_data.py
and scripts/missing_data_lab.py). Fixture ids are `<backend dir>/<source>__<claim>` relative to
tests/fixtures/missing-data/."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
Series = dict[tuple, list[tuple[float, float]]]


def _path(fixture_id: str) -> Path:
    if not fixture_id.startswith("missing-data/"):
        fixture_id = f"missing-data/{fixture_id}"
    return FIXTURES / f"{fixture_id}.json"


def fx(fixture_id: str) -> dict:
    return json.loads(_path(fixture_id).read_text())


def exists(fixture_id: str) -> bool:
    return _path(fixture_id).exists()


def matrix(fixture_id: str) -> Series:
    """query_range matrix -> {label items: [(ts, value)]}; stale/NaN values stay as floats."""
    body = fx(fixture_id)["body"]
    assert body["status"] == "success", body
    out: Series = {}
    for s in body["data"]["result"]:
        key = tuple(sorted(s["metric"].items()))
        out[key] = [(float(t), float(v)) for t, v in s["values"]]
    return out


def only(series: Series) -> list[tuple[float, float]]:
    assert len(series) == 1, f"expected one series, got {len(series)}"
    return next(iter(series.values()))


def raw_samples(fixture_id: str) -> list[tuple[float, float]]:
    """instant range-vector response -> the single series' samples."""
    body = fx(fixture_id)["body"]
    [s] = body["data"]["result"]
    return [(float(t), float(v)) for t, v in s["values"]]


def gaps(samples: list[tuple[float, float]], min_gap_s: float) -> list[tuple[float, float]]:
    """(start, end) of consecutive samples further apart than `min_gap_s`."""
    return [(a, b) for (a, _), (b, _) in itertools.pairwise(samples) if b - a > min_gap_s]


def fill_after(evaluated: list[tuple[float, float]], gap: tuple[float, float]) -> float:
    """Seconds after the last real sample (gap start) that evaluation still returned a point."""
    a, b = gap
    inside = [t for t, _ in evaluated if a < t < b]
    return max(inside) - a if inside else 0.0
