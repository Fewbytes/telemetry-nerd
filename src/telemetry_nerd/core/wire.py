"""Shaping analysis results for the wire, shared by the analyze / compare_seasonal / fleet ops
(epic lkn): significant-digit rounding, caveat lists, `evidence` statistics, a small memo."""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Hashable, Iterable
from typing import Any

from telemetry_nerd.analysis.sources import CITE_RANK, MEASUREMENT_CAVEATS, SOURCES


def sig(v: float | None, digits: int = 4) -> float | None:
    """v to `digits` significant digits; None for None, NaN and +-inf."""
    if v is None or not math.isfinite(v):
        return None
    return float(f"{v:.{digits}g}")


def sig_pair(pair) -> list[float | None]:
    return [sig(pair[0]), sig(pair[1])]


def sig_list(a: Iterable | None) -> list[float | None]:
    """An array to 5 significant digits (non-finite -> None); [] for None."""
    if a is None:
        return []
    return [sig(float(v), 5) for v in a]


def add_caveats(caveats: list[str], more: Iterable[str]) -> None:
    """Append, in place, the caveats in `more` that `caveats` does not hold yet."""
    caveats += [c for c in more if c not in caveats]


def statistic(
    dataset: str,
    name: str,
    value: float | None,
    interval: list | None,
    method: str,
    params: dict,
    source: str | None = None,
) -> dict:
    """An `evidence` statistic for finding_create (value and interval already rounded).

    No interval (None, or a bound that is not finite) is unknown uncertainty, not zero: the
    statistic is still evidence, stated `uncertainty_unknown: true` (spec §5.3). Derive one
    where possible first (bootstrap, effective n, bucket bounds, propagation).

    `source`: what the statistic's variation or deviation is attributed to (spec §5.4,
    `analysis.sources`): common_cause | special_cause | measurement_system | undetermined.
    Left out when the statistic reports no variation (a level, a count)."""
    if source is not None and source not in SOURCES:
        raise ValueError(f"unknown variation source {source!r}")
    out: dict = {"kind": "statistic", "dataset": dataset, "name": name, "value": value}
    if interval is None or any(v is None for v in interval):
        out |= {"interval": None, "exact": False, "uncertainty_unknown": True}
    else:
        out |= {"interval": interval, "exact": False}
    out |= {"method": method, "params": params}
    if source is not None:
        out["source"] = source
    return out


def measurement_caveats(meta) -> list[str]:
    """Measurement-system caveat codes a dataset carries itself (spec §5.4): a partial fetch,
    failed spans (data unknown there), and source caveats about the instruments."""
    out = ["partial"] if getattr(meta, "partial", False) else []
    if getattr(meta, "failed_spans", None):
        out.append("untrusted_data")
    return out + [c for c in getattr(meta, "source_caveats", ()) if c in MEASUREMENT_CAVEATS]


class Memo[V]:
    """Least-recently-used results of the latest `size` analyses."""

    def __init__(self, size: int = 16) -> None:
        self._size = size
        self._items: OrderedDict[Hashable, V] = OrderedDict()

    def get(self, key: Hashable) -> V | None:
        if key not in self._items:
            return None
        self._items.move_to_end(key)
        return self._items[key]

    def put(self, key: Hashable, value: V) -> None:
        self._items[key] = value
        if len(self._items) > self._size:
            self._items.popitem(last=False)


def _statistic_paths(obj: Any, path: str = "") -> Iterable[tuple[str, dict]]:
    if isinstance(obj, dict):
        if obj.get("kind") == "statistic" and "dataset" in obj and "value" in obj:
            yield path, obj
            return
        for k, v in obj.items():
            yield from _statistic_paths(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _statistic_paths(v, f"{path}[{i}]")


def cite_line(out: dict, limit: int = 3) -> str | None:
    """A one-line pointer to an op result's citable `evidence` statistics (hk2r): where they
    are, labelled ones first (special cause first), and that they go to finding_create as is.
    None when the result carries no statistic."""
    found = list(_statistic_paths(out))
    if not found:
        return None
    labelled = sorted(
        (x for x in found if x[1].get("source")), key=lambda x: CITE_RANK.get(x[1]["source"], 9)
    )
    picks = labelled[:limit] or found[:1]
    where = ", ".join(
        f"{p} ({st['name']}" + (f", {st['source']})" if st.get("source") else ")")
        for p, st in picks
    )
    more = len(labelled or found) - len(picks)
    return (
        f"cite: evidence {where}"
        + (f" (+{more} more)" if more > 0 else "")
        + ": pass as is to finding_create(evidence=[...]); its source is kept, a panel has none"
    )


def with_cite(out: Any) -> Any:
    """`out` with its `cite` line last (a dict result carrying evidence statistics)."""
    if isinstance(out, dict) and (line := cite_line(out)):
        out = {k: v for k, v in out.items() if k != "cite"} | {"cite": line}
    return out
