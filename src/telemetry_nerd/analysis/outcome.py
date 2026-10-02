"""Split a latency histogram by outcome (bead 2as.19). Pure.

Fast errors flatter latency and slow errors drag it (SRE book ch. 6): a latency distribution that
mixes successes and failures answers neither question. This finds the label that records the
outcome and classifies its values, deterministically, so a follow-up can show the two apart. Values
it cannot classify are left out and reported, never guessed."""

from __future__ import annotations

import re
from dataclasses import dataclass

from telemetry_nerd.charts.ycontext import selector_parts

#: label names that record how a request ended, in the order they are tried
OUTCOME_LABELS: tuple[str, ...] = (
    "status_code",
    "http_response_status_code",
    "http_status_code",
    "status",
    "code",
    "outcome",
    "result",
    "success",
    "error",
)
_FAIL_WORDS = {
    "error", "errors", "fail", "failed", "failure", "exception", "timeout", "timed_out",
    "unavailable", "internal", "deadline_exceeded", "aborted", "cancelled", "canceled",
    "false", "err", "5xx",
}  # fmt: skip
_OK_WORDS = {"ok", "success", "succeeded", "successful", "true", "2xx", "3xx", "none"}
_HTTP = re.compile(r"[1-5]\d\d")


@dataclass(frozen=True)
class Outcomes:
    label: str
    success: tuple[str, ...]
    failure: tuple[str, ...]
    #: values that are neither (client errors, unknown words): left out of both, said so
    excluded: tuple[str, ...]


def classify_value(label: str, value: str) -> str | None:
    """ "success", "failure" or None (unclassifiable). HTTP: 2xx/3xx ok, 5xx fail, 4xx is the
    client's doing and is left out; the `success`/`error` labels read as booleans."""
    v = value.strip().lower()
    if label in ("success",):
        return {"true": "success", "1": "success", "false": "failure", "0": "failure"}.get(v)
    if label in ("error",):
        if v in ("", "false", "0", "none"):
            return "success"
        return "failure"
    if _HTTP.fullmatch(v):
        return "failure" if v[0] == "5" else "success" if v[0] in "23" else None
    if v in _OK_WORDS or v == "0":  # gRPC code 0 = OK
        return "success"
    if v in _FAIL_WORDS:
        return "failure"
    return None


def classify(label: str, values: list[str]) -> Outcomes:
    ok, bad, left = [], [], []
    for v in sorted(set(values)):
        kind = classify_value(label, v)
        (ok if kind == "success" else bad if kind == "failure" else left).append(v)
    return Outcomes(label, tuple(ok), tuple(bad), tuple(left))


def candidate_labels(label_names: list[str] | tuple[str, ...]) -> list[str]:
    """The outcome-looking labels the source knows, in trial order."""
    known = set(label_names)
    return [name for name in OUTCOME_LABELS if name in known]


def add_matcher(selector: str, label: str, values: tuple[str, ...]) -> str:
    """`selector` restricted to `label` in `values` (an exact alternation); ValueError if the
    selector is not a plain one."""
    parts = selector_parts(selector)
    if parts is None:
        raise ValueError(f"cannot add a matcher to {selector!r}: not a plain selector")
    metric, matchers = parts
    clause = f'{label}=~"{"|".join(re.escape(v) for v in values)}"'
    inner = matchers.strip()[1:-1].strip() if matchers.strip() else ""
    return f"{metric}{{{inner + ',' if inner else ''}{clause}}}"
