"""Shaping analysis results for the wire, shared by the analyze / compare_seasonal / fleet ops
(epic lkn): significant-digit rounding, caveat lists, `evidence` statistics, a small memo."""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Hashable, Iterable


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
    dataset: str, name: str, value: float | None, interval: list, method: str, params: dict
) -> dict:
    """An `evidence` statistic for finding_create (value and interval already rounded)."""
    return {
        "kind": "statistic", "dataset": dataset, "name": name, "value": value,
        "interval": interval, "exact": False, "method": method, "params": params,
    }  # fmt: skip


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
