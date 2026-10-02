"""Behaviour groups of a heterogeneous fleet (bead lkn.10). Pure numpy.

When many members of a fleet are named (`many_outliers`), the members may differ systematically:
two instance sizes, roles, zones. Members are then split into behaviour groups by their deviation
from the fleet (log ratio or difference to the per-step median of the others), summarised as
trimmed means over B time blocks: level and shape of the deviation.

k-selection (stated in the result): recursive 2-means splits, each accepted only when it is
significant against a single Gaussian with the group's own covariance (SigClust: Liu, Hayes,
Nobel & Marron 2008, JASA 103:1281). The statistic is the 2-means cluster index
CI = within-cluster SS / total SS; its null distribution is simulated (seeded) from N(0, S) with S
the group's sample covariance, which includes any between-group spread, so the null is wide and
the test conservative. Each test runs at 1% / (2 MAX_CLUSTERS - 1), Bonferroni over the most
tests a fleet can need, so inventing any group anywhere in the recursion stays under 1%. A split must leave both parts with >= MIN_CLUSTER members (a lone deviant
member is an outlier, not a group). Each accepted group is then analysed as its own fleet
(analysis/fleet.py) at alpha / k, so the family-wise error over groups stays 1%.

Clusters are explained by labels: the label whose values best reproduce the clusters (adjusted
Rand index, which gives ~0 to labels that name every member uniquely).
Design: docs/superpowers/specs/2026-10-02-fleet-analysis-design.md (Heterogeneous fleets).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from telemetry_nerd.analysis.fleet import ALPHA, MIN_MEMBERS, TRIM, Fleet, analyse, trimmed_mean

BLOCKS = 8  # time blocks per member feature vector
MIN_BLOCK = 4  # steps per block at least
MIN_CLUSTER = MIN_MEMBERS  # a group must be a fleet on its own
MAX_CLUSTERS = 6
SPLIT_ALPHA = 0.01  # family-wise over a fleet's split tests (at most 2 MAX_CLUSTERS - 1)
TEST_ALPHA = SPLIT_ALPHA / (2 * MAX_CLUSTERS - 1)
# simulated null cluster indices: a split is accepted only if none of them is as small as the
# data's (p = 1 / (N_NULL + 1) < TEST_ALPHA); the first one that is stops the simulation
N_NULL = math.ceil(1 / TEST_ALPHA)
RESTARTS = 4
EXPLAIN_ARI = 0.5  # a label "explains" the clusters at or above this adjusted Rand index
ASSIGN_ARI = 0.8  # ... and decides the group of members whose behaviour is ambiguous above this
ASSIGN_SHARE = 0.8  # a label value maps to a group when this share of its members are in it


@dataclass
class SplitTest:
    sizes: tuple[int, int]
    ci: float  # 2-means within SS / total SS of the data
    null_ci_median: float
    p: float  # Monte Carlo (1 + hits) / (1 + simulations); early stop at the first hit
    accepted: bool
    simulations: int = 0


@dataclass
class Clustering:
    labels: np.ndarray  # cluster index per member, -1 = not assigned (no data)
    k: int
    centres: np.ndarray  # k x B feature centres (median of members)
    tests: list[SplitTest] = field(default_factory=list)
    blocks: int = BLOCKS


def features(d: np.ndarray, blocks: int = BLOCKS) -> np.ndarray:
    """members x blocks: 20% trimmed mean of each member's deviation per time block (NaN when a
    block has fewer than half its steps observed)."""
    m_, t_ = d.shape
    b = max(1, min(blocks, t_ // MIN_BLOCK))
    edges = np.linspace(0, t_, b + 1).astype(int)
    out = np.full((m_, b), np.nan)
    for j in range(b):
        seg = d[:, edges[j] : edges[j + 1]]
        for i in range(m_):
            v = np.sort(seg[i][np.isfinite(seg[i])])
            if v.size >= max(1, seg.shape[1] // 2):
                c = int(v.size * TRIM)
                out[i, j] = float(v[c : v.size - c].mean())
    return out


def _two_means(x: np.ndarray, rng: np.random.Generator, restarts: int = RESTARTS):
    """Best of `restarts` Lloyd runs (k-means++ seeding). Returns (labels, within SS)."""
    n = x.shape[0]
    best_lab, best_ss = np.zeros(n, int), math.inf
    for _ in range(restarts):
        c0 = x[rng.integers(n)]
        d0 = np.sum((x - c0) ** 2, axis=1)
        tot = d0.sum()
        c1 = x[rng.choice(n, p=d0 / tot)] if tot > 0 else x[rng.integers(n)]
        cent = np.vstack([c0, c1])
        lab = np.zeros(n, int)
        for _it in range(50):
            dist = np.sum((x[:, None, :] - cent[None, :, :]) ** 2, axis=2)
            new = np.argmin(dist, axis=1)
            if _it and np.array_equal(new, lab):
                break
            lab = new
            for k in (0, 1):
                if np.any(lab == k):
                    cent[k] = x[lab == k].mean(axis=0)
        ss = sum(
            float(np.sum((x[lab == k] - x[lab == k].mean(axis=0)) ** 2))
            for k in (0, 1)
            if np.any(lab == k)
        )
        if ss < best_ss:
            best_lab, best_ss = lab.copy(), ss
    return best_lab, best_ss


def split_test(x: np.ndarray, rng: np.random.Generator) -> tuple[np.ndarray, SplitTest]:
    """2-means split of x and its SigClust p-value against one Gaussian with x's covariance."""
    n = x.shape[0]
    xc = x - x.mean(axis=0)
    total = float(np.sum(xc**2))
    lab, within = _two_means(x, rng)
    sizes = (int(np.sum(lab == 0)), int(np.sum(lab == 1)))
    if total <= 0:
        return lab, SplitTest(sizes, 1.0, 1.0, 1.0, False)
    ci = within / total
    cov = np.cov(xc, rowvar=False).reshape(x.shape[1], x.shape[1])
    w, v = np.linalg.eigh(cov)
    root = v * np.sqrt(np.clip(w, 0, None))
    null: list[float] = []
    hits = 0
    while len(null) < N_NULL and hits == 0:  # sequential Monte Carlo (Besag & Clifford 1991)
        z = rng.standard_normal((n, x.shape[1])) @ root.T
        _, wn = _two_means(z, rng, restarts=2)
        tz = float(np.sum((z - z.mean(axis=0)) ** 2))
        null.append(wn / tz if tz > 0 else 1.0)
        hits += null[-1] <= ci
    p = (1 + hits) / (1 + len(null))
    ok = hits == 0 and p < TEST_ALPHA and min(sizes) >= MIN_CLUSTER
    return lab, SplitTest(sizes, ci, float(np.median(null)), p, ok, len(null))


def cluster(d: np.ndarray, rows: list[int], seed: int = 0) -> Clustering:
    """Recursive SigClust-tested 2-means over the members in `rows` (those with complete
    features); other members with any observed block join the nearest group centre."""
    m_ = d.shape[0]
    feats = features(d)
    labels = np.full(m_, -1)
    rng = np.random.default_rng(seed)
    complete = [i for i in rows if np.all(np.isfinite(feats[i]))]
    tests: list[SplitTest] = []
    groups: list[list[int]] = []
    todo = [complete] if len(complete) >= 2 * MIN_CLUSTER else []
    if not todo:
        groups = [complete]
    while todo:
        g = todo.pop(0)
        if len(g) < 2 * MIN_CLUSTER or len(groups) + len(todo) + 1 >= MAX_CLUSTERS:
            groups.append(g)
            continue
        lab, t = split_test(feats[g], rng)
        tests.append(t)
        if t.accepted:
            todo += [[g[j] for j in np.flatnonzero(lab == k)] for k in (0, 1)]
        else:
            groups.append(g)
    # order groups by size (largest first) for stable ids
    groups.sort(key=lambda g: (-len(g), min(g) if g else 0))
    b = feats.shape[1]
    centres = np.vstack([np.median(feats[g], axis=0) if g else np.full(b, np.nan) for g in groups])
    for k, g in enumerate(groups):
        labels[g] = k
    if len(groups) > 1:
        for i in range(m_):
            ok = np.isfinite(feats[i])
            if labels[i] < 0 and ok.any():
                labels[i] = int(np.argmin(np.sum((centres[:, ok] - feats[i, ok]) ** 2, axis=1)))
    elif groups:
        labels[:] = np.where(np.isfinite(feats).any(axis=1), 0, -1)
    return Clustering(labels, len(groups), centres, tests, b)


def adjusted_rand(a: list, b: list) -> float:
    """Adjusted Rand index of two partitions given as equal-length label lists."""
    n = len(a)
    if n < 2:
        return 0.0
    pairs = Counter(zip(a, b, strict=True))
    ca, cb = Counter(a), Counter(b)

    def c2(x: int) -> float:
        return x * (x - 1) / 2

    s_ij = sum(c2(v) for v in pairs.values())
    s_a, s_b = sum(c2(v) for v in ca.values()), sum(c2(v) for v in cb.values())
    expected = s_a * s_b / c2(n)
    top = (s_a + s_b) / 2
    if top == expected:
        return 1.0 if s_ij == top else 0.0
    return (s_ij - expected) / (top - expected)


def explain(groups: np.ndarray, labels: list[dict]) -> list[dict]:
    """Labels whose values reproduce the clusters, best first: [{label, ari, values: {cluster:
    [values...]}}] for those at or above EXPLAIN_ARI."""
    idx = [i for i in range(len(labels)) if groups[i] >= 0]
    keys = sorted(
        {k for i in idx for k in labels[i] if k != "__name__"}
        | ({"__name__"} if len({labels[i].get("__name__") for i in idx}) > 1 else set())
    )
    out = []
    for key in keys:
        vals = [labels[i].get(key, "") for i in idx]
        if len(set(vals)) < 2:
            continue
        ari = adjusted_rand([int(groups[i]) for i in idx], vals)
        if ari >= EXPLAIN_ARI:
            per: dict[int, Counter] = {}
            for i, v in zip(idx, vals, strict=True):
                per.setdefault(int(groups[i]), Counter())[v] += 1
            out.append({"label": key, "ari": ari,
                        "values": {k: [v for v, _ in c.most_common(5)] for k, c in sorted(per.items())}})  # fmt: skip
    out.sort(key=lambda e: -e["ari"])
    return out


def group_alpha(k: int) -> float:
    """Per-group family-wise alpha so the error over all groups stays ALPHA."""
    return ALPHA / max(k, 1)


def assign_by_label(groups: np.ndarray, labels: list[dict], key: str) -> list[int]:
    """Move members whose `key` value maps (>= ASSIGN_SHARE of its members) to another group into
    it; returns the moved members. Used only when that label explains the clusters at
    >= ASSIGN_ARI: behaviour cannot place a member that drifts from one group's level to the
    other's, but its label can (and a member behaving like the other group is then judged, and
    named, within its own)."""
    per: dict[str, Counter] = {}
    for i, lb in enumerate(labels):
        if groups[i] >= 0:
            per.setdefault(lb.get(key, ""), Counter())[int(groups[i])] += 1
    home = {}
    for v, c in per.items():
        g, n = c.most_common(1)[0]
        if n >= ASSIGN_SHARE * sum(c.values()):
            home[v] = g
    moved = []
    for i, lb in enumerate(labels):
        g = home.get(lb.get(key, ""))
        if g is not None and groups[i] >= 0 and groups[i] != g:
            groups[i] = g
            moved.append(i)
    return moved


@dataclass
class Group:
    members: list[int]  # indices into the fleet
    fleet: Fleet  # this group analysed as its own fleet (member indices are positions in members)
    level: float  # median over members of their trimmed-mean deviation from the whole fleet


@dataclass
class Groups:
    clustering: Clustering
    groups: list[Group]
    explained_by: list[dict]
    assigned_by_label: list[int]
    unassigned: list[int]


def analyse_groups(
    y: np.ndarray, f: Fleet, labels: list[dict], unknown: np.ndarray | None = None
) -> Groups | None:
    """Behaviour groups of a heterogeneous fleet (only when `many_outliers`), each analysed as
    its own fleet at alpha / k. None when the fleet is not split."""
    if "many_outliers" not in f.caveats:
        return None
    c = cluster(f.d, f.tested)
    if c.k < 2:
        return None
    groups = c.labels.copy()
    expl = explain(groups, labels)
    moved = []
    if expl and expl[0]["ari"] >= ASSIGN_ARI:
        moved = assign_by_label(groups, labels, expl[0]["label"])
        if moved:
            expl = explain(groups, labels)
    level = trimmed_mean(f.d)
    out = []
    for k in range(c.k):
        mem = [int(i) for i in np.flatnonzero(groups == k)]
        if len(mem) < MIN_MEMBERS:
            continue
        g = analyse(
            y[mem], scale=f.scale, normalise=f.normalise, alpha=group_alpha(c.k),
            unknown=None if unknown is None else unknown[mem],
        )  # fmt: skip
        lv = level[mem]
        out.append(
            Group(mem, g, float(np.median(lv[np.isfinite(lv)])) if np.isfinite(lv).any() else 0.0)
        )
    if len(out) < 2:
        return None
    unassigned = [i for i in range(len(labels)) if not any(i in g.members for g in out)]
    return Groups(c, out, expl, moved, unassigned)
