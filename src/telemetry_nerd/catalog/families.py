"""Name-template detection: collapse names that encode a dimension in the metric name.

statsd-exporter style names such as `airflow_ti_finish_<dag>_<task>_<state>` turn one logical
metric into tens of thousands of names. A family is a template with a fixed prefix and suffix and
one slot in between; the text in the slot is the extracted dimension.

Detection is recursive partitioning on token positions, walking inward from both ends: a position
with one value extends the template, a position with few values (an enumeration, such as a task
state) splits the group so each value is its own template, a position with many values is the slot.
A group becomes a family only with enough members and enough distinct slot values, and only with a
fixed structure around the slot (a lone namespace prefix like `node_*` would merge unrelated metrics
that merely share a namespace). Pure and deterministic.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from functools import partial

SEP = "_"
SLOT = "*"
MIN_SUPPORT = 20  # members per family
MIN_DISTINCT = 20  # distinct slot values per family
MAX_ENUM = 16  # at most this many frequent values: a candidate enumeration, not a slot
SMALL_ENUM = 4  # a closed set this small is always a split (kinds, sites): separate families
COVERAGE = 0.95  # frequent values must cover this share of the group, else the tail is a slot
MIN_INFO = 0.1  # a larger enumeration splits only if it tells something about the other end
MIN_SLOT_TOKENS = 2  # a slot of single tokens is a list of distinct statistics, not an identifier
MIN_FIXED = 3  # fixed tokens around the slot, namespace token included
MIN_PREFIX = 2  # at least the namespace and one more token before the slot


@dataclass(frozen=True)
class Family:
    template: str  # e.g. airflow_ti_finish_*_removed
    prefix: tuple[str, ...]
    suffix: tuple[str, ...]
    members: int
    distinct: int  # distinct dimension values


@dataclass(frozen=True)
class Detection:
    families: tuple[Family, ...]
    #: member name -> (template, dimension)
    assignment: dict[str, tuple[str, str]]


def template_of(prefix: Iterable[str], suffix: Iterable[str]) -> str:
    return SEP.join([*prefix, SLOT, *suffix])


def _info(pairs: Counter) -> float:
    """Normalised mutual information of a joint count table (0 independent, 1 determined)."""
    n = sum(pairs.values())
    if n == 0:
        return 0.0
    px: Counter = Counter()
    py: Counter = Counter()
    for (x, y), c in pairs.items():
        px[x] += c
        py[y] += c

    def h(d: Counter) -> float:
        return -sum(c / n * math.log(c / n) for c in d.values())

    hx, hy = h(px), h(py)
    if min(hx, hy) == 0:
        return 0.0
    mi = sum(c / n * math.log(c * n / (px[x] * py[y])) for (x, y), c in pairs.items())
    return mi / min(hx, hy)


def _joint(toks, deep, p: int, s: int) -> Counter:
    return Counter((toks[n][p], toks[n][-(s + 1)]) for n in deep)


def _decide(vals: Counter, other_pairs, min_support: int, max_enum: int) -> tuple[str, list[str]]:
    """What a token position is: ("const", [v]), ("split", values) or ("slot", [])."""
    total = sum(vals.values())
    frequent = [v for v, c in vals.items() if c >= min_support]
    covered = sum(vals[v] for v in frequent)
    if not frequent or covered < COVERAGE * total:
        return "slot", []
    if len(frequent) == 1:
        return "const", frequent
    if len(frequent) <= SMALL_ENUM:
        return "split", sorted(frequent)
    if len(frequent) <= max_enum and _info(other_pairs()) >= MIN_INFO:
        return "split", sorted(frequent)
    return "slot", []


def detect(
    names: Iterable[str],
    *,
    min_support: int = MIN_SUPPORT,
    min_distinct: int = MIN_DISTINCT,
    max_enum: int = MAX_ENUM,
) -> Detection:
    toks: dict[str, list[str]] = {}
    for n in names:
        t = n.split(SEP)
        if len(t) >= 3:
            toks[n] = t
    by_ns: dict[str, list[str]] = defaultdict(list)
    for n, t in toks.items():
        by_ns[t[0]].append(n)  # the first token is a namespace: never a slot of its own

    families: list[Family] = []
    assignment: dict[str, tuple[str, str]] = {}
    for ns in sorted(by_ns):
        stack = [(by_ns[ns], 1, 0)]
        while stack:
            group, p, s = stack.pop()
            while True:
                live = [n for n in group if len(toks[n]) > p + s]
                if len(live) < min_support:
                    break
                deep = [n for n in live if len(toks[n]) > p + s + 1]
                if len(deep) < len(live) * COVERAGE or len(deep) < min_support:
                    _finalize(live, toks, p, s, min_support, min_distinct, families, assignment)
                    break
                pre = Counter(toks[n][p] for n in deep)
                suf = Counter(toks[n][-(s + 1)] for n in deep)
                joint = partial(_joint, toks, deep, p, s)
                kp, vp = _decide(pre, joint, min_support, max_enum)
                ks, vs = _decide(suf, joint, min_support, max_enum)
                if kp == "const":
                    group, p = [n for n in deep if toks[n][p] == vp[0]], p + 1
                elif ks == "const":
                    group, s = [n for n in deep if toks[n][-(s + 1)] == vs[0]], s + 1
                elif kp == "split":
                    for v in vp:
                        stack.append(([n for n in deep if toks[n][p] == v], p + 1, s))
                    break
                elif ks == "split":
                    for v in vs:
                        stack.append(([n for n in deep if toks[n][-(s + 1)] == v], p, s + 1))
                    break
                else:
                    _finalize(live, toks, p, s, min_support, min_distinct, families, assignment)
                    break
    families.sort(key=lambda f: f.template)
    return Detection(tuple(families), assignment)


def _finalize(live, toks, p, s, min_support, min_distinct, families, assignment) -> None:
    if p < MIN_PREFIX or p + s < MIN_FIXED:
        return
    dims = {n: SEP.join(toks[n][p : len(toks[n]) - s]) for n in live}
    distinct = len(set(dims.values()))
    if len(live) < min_support or distinct < min_distinct:
        return
    multi = sum(1 for n in live if len(toks[n]) - p - s >= MIN_SLOT_TOKENS)
    if multi < len(live) / 2:
        return  # single-token slot values are distinct statistics (node_netstat_Tcp_*), not ids
    first = toks[live[0]]
    suffix = tuple(first[len(first) - s :]) if s else ()
    template = template_of(first[:p], suffix)
    families.append(Family(template, tuple(first[:p]), suffix, len(live), distinct))
    for n, d in dims.items():
        assignment[n] = (template, d)
