"""Latest sample-scan observation per metric, and the contradiction findings already filed."""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel


class SampleObservation(BaseModel):
    window_ms: int
    step_ms: int
    series: int
    voting: int
    n: int
    min: float | None
    max: float | None
    negatives: int
    increases: int
    decreases: int
    resets: int
    small_decreases: int
    gauge_voters: int
    integral: bool
    constant: bool
    verdict: str
    dataset: str
    scanned_ms: int


_COLS = (
    "window_ms, step_ms, series, voting, n, min, max, negatives, increases, decreases, resets, "
    "small_decreases, gauge_voters, integral, constant, verdict, dataset, scanned_ms"
)


class SampleStore:
    def __init__(self, con: sqlite3.Connection) -> None:
        self._db = con

    def put(self, source: str, metric: str, o: SampleObservation) -> None:
        row = o.model_dump()
        row["integral"], row["constant"] = int(o.integral), int(o.constant)
        self._db.execute(
            f"INSERT OR REPLACE INTO catalog_samples (source, metric, {_COLS}) "
            f"VALUES (?, ?, {', '.join('?' * 18)})",
            (source, metric, *[row[c.strip()] for c in _COLS.split(",")]),
        )

    def get(self, source: str, metric: str) -> SampleObservation | None:
        r = self._db.execute(
            f"SELECT {_COLS} FROM catalog_samples WHERE source = ? AND metric = ?", (source, metric)
        ).fetchone()
        if r is None:
            return None
        d = dict(zip((c.strip() for c in _COLS.split(",")), r, strict=True))
        d["integral"], d["constant"] = bool(d["integral"]), bool(d["constant"])
        return SampleObservation(**d)

    def findings_for(self, source: str, metrics: list[str]) -> dict[str, list[dict]]:
        """kind/id of the contradiction findings filed for each of `metrics`."""
        out: dict[str, list[dict]] = {m: [] for m in metrics}
        if not metrics:
            return out
        marks = ",".join("?" * len(metrics))
        for m, kind, fid in self._db.execute(
            f"SELECT metric, kind, finding_id FROM catalog_findings WHERE source = ? "
            f"AND metric IN ({marks}) ORDER BY kind",
            (source, *metrics),
        ):
            out[m].append({"kind": kind, "id": fid})
        return out

    def verdicts_for(self, source: str, metrics: list[str]) -> dict[str, str]:
        if not metrics:
            return {}
        marks = ",".join("?" * len(metrics))
        rows = self._db.execute(
            f"SELECT metric, verdict FROM catalog_samples WHERE source = ? AND metric IN ({marks})",
            (source, *metrics),
        ).fetchall()
        return dict(rows)

    def finding(self, source: str, metric: str, kind: str) -> str | None:
        r = self._db.execute(
            "SELECT finding_id FROM catalog_findings WHERE source = ? AND metric = ? AND kind = ?",
            (source, metric, kind),
        ).fetchone()
        return r[0] if r else None

    def set_finding(self, source: str, metric: str, kind: str, finding_id: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO catalog_findings VALUES (?, ?, ?, ?)",
            (source, metric, kind, finding_id),
        )
