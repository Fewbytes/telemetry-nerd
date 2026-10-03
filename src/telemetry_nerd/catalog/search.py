"""Compact, Claude-facing views over the catalog: search rows and family overview."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Collection, Iterable

from telemetry_nerd.catalog.models import CatalogEntry

#: fields shown per row (with the origin that wins each)
KEY_FIELDS = ("type", "unit", "role", "bounds")
#: origins that have actually interpreted a metric (rule/metadata only restate its name/HELP)
INTERPRETING = frozenset({"pack", "claude", "user"})


def family_prefix(metric: str) -> str:
    """`node_cpu_seconds_total` -> `node_cpu`; `up` -> `up`."""
    parts = metric.split("_")
    return "_".join(parts[:2]) if len(parts) > 2 else metric


def reviewed(entry: CatalogEntry) -> bool:
    """Someone (pack, Claude, user) has said what this metric is for, and nothing disagrees."""
    role = entry.fields.get("role")
    return role is not None and role.origin in INTERPRETING and not entry.conflicts()


def row(entry: CatalogEntry, hot: bool) -> dict:
    out: dict = {"metric": entry.metric}
    for f in KEY_FIELDS:
        if f in entry.fields:
            out[f] = entry.fields[f].value
    out["origins"] = {f: entry.fields[f].origin for f in KEY_FIELDS if f in entry.fields}
    if conflicts := entry.conflicts():
        out["conflicts"] = sorted(conflicts)
    out["reviewed"] = reviewed(entry)
    if hot:
        out["hot"] = True
    return out


#: words that carry no meaning in a metric search ("rate of http server requests")
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "the",
        "of",
        "for",
        "per",
        "in",
        "on",
        "by",
        "to",
        "or",
        "with",
        "from",
        "at",
        "is",
        "are",
        "metric",
        "metrics",
        "series",
    ]
)
#: words that name the same quantity in metric names, OTel semconv and HELP texts
SYNONYMS: tuple[frozenset[str], ...] = tuple(
    frozenset(g.split())
    for g in (
        "duration latency elapsed",
        "request call",
        "error failure fail failed fault",
        "memory mem",
        "utilization usage util",
        "connection conn",
        "transaction tx",
        "size byte",
        "count total number",
        "rpc grpc",
        "db database sql",
        "service svc",
        "server srv",
    )
)
_SYN: dict[str, frozenset[str]] = {w: g for g in SYNONYMS for w in g}
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_SPLIT = re.compile(r"[^a-z0-9]+")


def _stem(word: str) -> str:
    """Plural to singular, enough for metric words (requests, errors, bytes, latencies)."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _words(text: str) -> list[str]:
    return [t for t in _SPLIT.split(_CAMEL.sub(r"\1 \2", text).lower()) if t]


def tokens(text: str) -> list[str]:
    """Words of a metric name, a dotted semconv name or a HELP text: split on `_`, `.`, spaces,
    other punctuation and camelCase; lower-cased and singular."""
    return [_stem(t) for t in _words(text)]


def query_terms(query: str) -> list[str]:
    terms = [_stem(t) for t in _words(query) if t not in STOPWORDS]
    return list(dict.fromkeys(terms)) or tokens(query)


def _hits(term: str, words: set[str]) -> bool:
    if term in words or (_SYN.get(term, frozenset()) & words):
        return True
    # a prefix of a word ("dur" -> duration), never the other way round
    return len(term) >= 3 and any(w.startswith(term) for w in words)


def _score(entry: CatalogEntry, q: str, terms: list[str]) -> tuple[int, int]:
    """(terms matched, weight): a term in the name weighs 2, only in a description 1; the
    whole query as a substring of the name or a description adds 3."""
    name = set(tokens(entry.metric))
    # every description claim (pack, HELP, repo context), not only the winner: each wording
    # is a way someone may ask for the metric
    descs = [str(c.value) for c in entry.claims.get("description", [])] or (
        [str(entry.fields["description"].value)] if "description" in entry.fields else []
    )
    desc = " ".join(descs)
    words = set(tokens(desc)) if desc else set()
    matched = weight = 0
    for t in terms:
        if _hits(t, name):
            matched, weight = matched + 1, weight + 2
        elif _hits(t, words):
            matched, weight = matched + 1, weight + 1
    if q in entry.metric.lower() or (desc and q in desc.lower()):
        matched, weight = len(terms), weight + 3
    return matched, weight


def search(
    entries: Iterable[CatalogEntry],
    hot: Collection[str],
    *,
    query: str | None = None,
    prefix: str | None = None,
    needs_review: bool = False,
    limit: int = 50,
    source: str | None = None,
) -> dict:
    """Catalog rows matching `query` word by word: every word must appear in the name (split on
    `_`/`.`/camelCase) or the description (pack, HELP, semconv text), allowing plurals, synonyms
    (latency~duration, error~failure, ...) and prefixes. When no metric matches every word, the
    best partial matches are returned and marked so. Ranked: hot, then match weight, then name."""
    q = query.strip().lower() if query else None
    terms = query_terms(q) if q else []
    pool, scored = 0, []
    for e in entries:
        if prefix and not e.metric.startswith(prefix):
            continue
        if needs_review and reviewed(e):
            continue
        pool += 1
        scored.append((e, *(_score(e, q, terms) if q else (0, 0))))
    full = [x for x in scored if not q or x[1] == len(terms)]
    partial = False
    if q and not full and len(terms) > 1:
        need = max(1, -(-len(terms) // 2))  # at least half of the words
        full, partial = [x for x in scored if x[1] >= need], True
    full.sort(key=lambda x: (x[0].metric not in hot, -x[1], -x[2], x[0].metric))
    shown = full[:limit]
    out: dict = {
        "total": len(full),
        "returned": len(shown),
        "results": [
            row(e, e.metric in hot) | ({"matched": f"{m}/{len(terms)}"} if partial else {})
            for e, m, _ in shown
        ],
    }
    if q:
        out["terms"] = terms
    if partial and shown:
        out["match"] = "partial"
        out["note"] = (
            f"no catalogued metric matches every word of {query!r}; these match at least half "
            "(see `matched`). Check the names before using them."
        )
    if not full:
        where = f" in source {source!r}" if source else ""
        what = " and ".join(
            x
            for x in (
                f"words {terms}" if q else "",
                f"prefix {prefix!r}" if prefix else "",
                "needs_review" if needs_review else "",
            )
            if x
        )
        out["note"] = (
            f"no catalogued metric matches {what or 'the filters'}{where} "
            f"(searched names and descriptions of {pool} metrics, with plurals, synonyms and "
            "prefixes). This says what the catalog holds, not what the source emits: an absent "
            "name is not evidence that a service or signal is absent. Try other words or a "
            "prefix, `entities` for which services report which metric families, or "
            "source_learn if the catalog may be stale."
        )
    return out


def overview(entries: Iterable[CatalogEntry], top: int = 30) -> list[dict]:
    """Metric families (by name prefix), the least-reviewed and largest first."""
    fam: dict[str, list[bool]] = defaultdict(list)
    for e in entries:
        fam[family_prefix(e.metric)].append(reviewed(e))
    rows = [{"family": f, "metrics": len(r), "reviewed": sum(r)} for f, r in fam.items()]
    rows.sort(key=lambda x: (-(x["metrics"] - x["reviewed"]), x["family"]))
    return rows[:top]
