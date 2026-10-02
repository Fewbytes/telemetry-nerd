"""Verify a unit an agent asserts for a panel's y axis (telemetry-nerd-lei). Pure: no I/O.

An asserted unit overrides inference, so it must not silently relabel the axis. It is checked
against what the catalog (or a derived-expression rule) says the expression returns and against
the data's own range:

* a SCALE conflict inside one dimension (fraction vs percent, s vs ms, bytes vs bits, KiB vs B)
  is refused: the numbers would be read wrong by a constant factor (a 0-1 fraction labelled "%"
  reads as almost nothing). The message says how to make the label true (rescale the
  expression, or use the expected unit).
* a different known dimension (s vs B) is refused too: one of the two is wrong.
* a unit we cannot interpret, or no expectation to check against, is accepted but flagged
  "unverified"; for percent vs fraction the data range is then checked (all values within [0,1]
  under "%", values above 1 under a fraction) and a warning names what the data suggest.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

# unit -> (dimension, size of one unit in the dimension's base)
_SCALES: dict[str, tuple[str, float]] = {
    **{u: ("fraction", 1.0) for u in ("ratio", "fraction", "percentunit", "1", "s/s")},
    **{u: ("fraction", 0.01) for u in ("%", "percent", "pct", "percentage")},
    **{
        u: ("count", 1.0)
        for u in (
            "count",
            "req",
            "reqs",
            "requests",
            "ops",
            "operations",
            "items",
            "events",
            "calls",
            "errors",
            "packets",
            "messages",
        )
    },
    **{u: ("time", 1e-9) for u in ("ns", "nanoseconds")},
    **{u: ("time", 1e-6) for u in ("us", "µs", "microseconds")},
    **{u: ("time", 1e-3) for u in ("ms", "milliseconds")},
    **{u: ("time", 1.0) for u in ("s", "sec", "seconds")},
    **{u: ("time", 60.0) for u in ("min", "minutes")},
    **{u: ("time", 3600.0) for u in ("h", "hours")},
    **{u: ("information", 8.0) for u in ("B", "bytes", "byte")},
    **{u: ("information", 1.0) for u in ("b", "bit", "bits")},
    **{u: ("information", 8e3) for u in ("kB", "KB")},
    **{u: ("information", 8e6) for u in ("MB",)},
    **{u: ("information", 8e9) for u in ("GB",)},
    **{u: ("information", 8 * 1024.0) for u in ("KiB",)},
    **{u: ("information", 8 * 1024.0**2) for u in ("MiB",)},
    **{u: ("information", 8 * 1024.0**3) for u in ("GiB",)},
    **{u: ("information", 1e3) for u in ("kb", "Kb", "kbit")},
    **{u: ("information", 1e6) for u in ("Mb", "Mbit")},
    **{u: ("information", 1e9) for u in ("Gb", "Gbit")},
}
_PER_SECOND = re.compile(r"^(?P<num>.+?)\s*(?:/\s*s|/\s*sec|ps)$")
FRACTION_TOLERANCE = 1e-9


def _parse(unit: str) -> tuple[str, float, bool] | None:
    """(dimension, scale, per_second) or None when the unit is not one we can interpret."""
    u = unit.strip()
    if u in _SCALES:
        dim, scale = _SCALES[u]
        return dim, scale, False
    m = _PER_SECOND.match(u)
    if m and m.group("num").strip() in _SCALES:
        dim, scale = _SCALES[m.group("num").strip()]
        return dim, scale, True
    low = u.lower()
    if low in _SCALES and _SCALES[low][0] in ("fraction", "time"):  # case only matters for bits
        dim, scale = _SCALES[low]
        return dim, scale, False
    return None


def _factor(a: float, b: float) -> str:
    r = a / b
    return f"{r:g}" if r >= 1 else f"1/{1 / r:g}"


@dataclass(frozen=True)
class UnitCheck:
    #: the refusal message (the asserted unit contradicts the evidence), else None
    problem: str | None = None
    #: a warning to return with the panel (accepted but unverified, data look otherwise)
    warning: str | None = None
    #: appended to "provided by <actor>" so the badge shows how far the unit was checked
    provenance_note: str | None = None


def check_unit(
    provided: str,
    expected: str | None,
    expected_why: str | None,
    data_range: tuple[float, float] | None,
) -> UnitCheck:
    """Check `provided` against the `expected` unit (catalog / derived rule, with its basis)
    and the finite (min, max) of the data, if known."""
    p = _parse(provided)
    e = _parse(expected) if expected else None
    basis = f" ({expected_why})" if expected_why else ""
    if p is not None and e is not None:
        (pd, ps, pr), (ed, es, er) = p, e
        if pd == ed and pr == er and math.isclose(ps, es):
            return UnitCheck(provenance_note=f"agrees with {expected}{basis}")
        if pd == ed and pr == er:
            hint = (
                "write 100 * (...) in the expression for percent, or pass unit='ratio'"
                if pd == "fraction" and ps < es
                else f"rescale the expression by {_factor(es, ps)} or pass unit={expected!r}"
            )
            hint += (
                "; if the catalog is wrong (the metric really is in "
                f"{provided}), record that with catalog_write (unit, with a basis) and show again"
            )
            return UnitCheck(
                problem=(
                    f"unit {provided!r} contradicts what the expression returns: {expected}"
                    f"{basis}. A label does not rescale the data: to be {provided} the values would "
                    f"have to be multiplied by {_factor(es, ps)}; {hint}"
                )
            )
        return UnitCheck(
            problem=(
                f"unit {provided!r} contradicts what the expression returns: {expected}{basis}; "
                f"one of the two is wrong. Fix the catalog claim (catalog_write with a basis) if "
                f"the catalog is, else omit unit"
            )
        )
    # nothing to check against: "provided by <actor>" already says it is the actor's word
    note = f"unverified: the catalog says {expected}{basis}" if expected else None
    warning = None
    if p is not None and p[0] == "fraction" and not p[2] and data_range is not None:
        lo, hi = data_range
        if p[1] < 1 and lo >= 0 and 0 < hi <= 1 + FRACTION_TOLERANCE:
            warning = (
                f"unit {provided!r} is unverified and every value lies in [{lo:g}, {hi:g}]: if "
                "this is a fraction of a whole, it is a ratio (unit='ratio'), not percent; "
                "percent needs 100 * (...) in the expression"
            )
        elif p[1] == 1 and hi > 1 + FRACTION_TOLERANCE:
            warning = (
                f"unit {provided!r} is unverified and values reach {hi:g}: a fraction of a "
                "whole stays within [0, 1]; if this is percent, pass unit='%'"
            )
    if expected and p is None:
        warning = warning or (
            f"unit {provided!r} could not be checked against the catalog's {expected}{basis}; "
            "make sure it is the same quantity at the same scale"
        )
    return UnitCheck(warning=warning, provenance_note=note)
