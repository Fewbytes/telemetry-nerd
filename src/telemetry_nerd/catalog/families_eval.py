"""Precision and recall of name-template detection against a hand-labelled sample."""

from __future__ import annotations

from dataclasses import dataclass

from telemetry_nerd.catalog.families import Detection


@dataclass(frozen=True)
class Scores:
    labelled: int
    positives: int  # names labelled as name-encoded dimensions
    clustered: int  # labelled names the detector put in a family
    true_positive: int  # clustered and labelled positive
    precision: float
    recall: float
    dimension_agreement: float  # of true positives, the labelled entity is inside the dimension
    false_positives: list[str]
    missed: list[str]


def evaluate(detection: Detection, labels: list[dict]) -> Scores:
    """A name counts as clustered when it is in a family. For correctly clustered names the
    extracted dimension must contain the labelled entity (a finer or coarser dimension is fine as
    long as the identifier is in it)."""
    pos = [x for x in labels if x["family"]]
    clustered = [x for x in labels if x["name"] in detection.assignment]
    tp = [x for x in clustered if x["family"]]
    agree = [x for x in tp if x["entity"] in detection.assignment[x["name"]][1]]
    return Scores(
        labelled=len(labels),
        positives=len(pos),
        clustered=len(clustered),
        true_positive=len(tp),
        precision=len(tp) / len(clustered) if clustered else 1.0,
        recall=len(tp) / len(pos) if pos else 1.0,
        dimension_agreement=len(agree) / len(tp) if tp else 1.0,
        false_positives=[x["name"] for x in clustered if not x["family"]],
        missed=[x["name"] for x in pos if x["name"] not in detection.assignment],
    )
