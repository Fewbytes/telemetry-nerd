"""Argument shapes Claude tries for the workspace tools, read into the canonical ones (e0k).

Live evals lost most finding_create calls to argument shape, never to meaning: bare ids
(["d9", "p1", "a1"]), one dict holding several refs ({"dataset": "d9", "panel": "p1"}),
{"kind": "annotation", "id": "a1"}, scope {"range": [start, end]}, a scope without aggregation.
This module reads every such shape that means exactly one thing, and refuses the rest with a
minimal valid example for the field that failed. It never guesses: an id that names no evidence
kind, a dataset drawn by several panels, or a dict mixing a statistic with a panel is refused.

What the evidence *means* is still checked downstream (claim scope, coverage, uncertainty,
sources): only the spelling changes here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from telemetry_nerd.workspace.models import FindingIn

#: minimal valid evidence items, one per kind (shown in errors)
EVIDENCE_EXAMPLES = {
    "panel": {"kind": "panel", "panel": "p1"},
    "annotation": {"kind": "annotation", "annotation": "a1"},
    "statistic": {
        "kind": "statistic", "dataset": "d5", "name": "mean", "value": 0.42,
        "method": "analyze", "interval": [0.40, 0.44],
    },
    "claim": {
        "kind": "claim", "source": "default", "metric": "http_requests_total", "field": "unit",
        "origins": ["context", "pack"],
    },
}  # fmt: skip
SCOPE_EXAMPLE = {
    "source": "default",
    "selector": 'sum by (code) (rate(http_requests_total{service="checkout"}[1m]))',
    "start": "2026-10-03T13:10:00Z",
    "end": "2026-10-03T13:30:00Z",
    "step": "30s",
    "aggregation": "sum by (code) of rate[1m]",
}
SCOPE_FIELDS = ("source", "selector", "start", "end", "step", "aggregation",
                "baseline_start", "baseline_end")  # fmt: skip
#: recorded when scope.aggregation is omitted: the selector as written is the aggregation
AGGREGATION_AS_WRITTEN = "as written in scope.selector"

_ID = re.compile(r"^([a-z])(\d+)$")
_REF_KINDS = {"p": "panel", "a": "annotation"}
_STAT_KEYS = frozenset({"name", "value", "interval", "exact", "uncertainty_unknown", "method",
                        "params", "source"})  # fmt: skip
_CLAIM_KEYS = frozenset({"metric", "field", "origins", "note"})
_REF_KEYS = ("panel", "annotation", "dataset")


def example(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _json_text(raw: Any) -> Any:
    """An object or list sent as JSON text (some clients stringify nested arguments)."""
    if isinstance(raw, str) and raw.strip()[:1] in ("{", "["):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


class ShapeError(ValueError):
    """An argument shape that does not read as exactly one thing; the message shows a fix."""


@dataclass
class EvidenceContext:
    """What the workspace knows, for reading ids: the panels drawing a dataset (as their main
    dataset), the methods ops recorded for a (dataset, statistic name), and the labelled
    statistics ops returned for a dataset (citable as is, special cause first)."""

    panels_of: Callable[[str], list[str]] = lambda _d: []
    methods_of: Callable[[str, str], set[str]] = lambda _d, _n: set()
    statistics_of: Callable[[str], list[dict]] = lambda _d: []


@dataclass
class Read:
    value: Any
    notes: list[str] = field(default_factory=list)


# --- evidence -------------------------------------------------------------------------------


def _ex(kind: str, **kw: Any) -> str:
    return example({**EVIDENCE_EXAMPLES[kind], **kw})


def _from_dataset(did: str, ctx: EvidenceContext, where: str, notes: list[str]) -> dict:
    """A dataset id as evidence: the one panel that draws it, else refused with the two ways
    to cite it (a dataset alone is not evidence: a panel or a statistic is)."""
    panels = ctx.panels_of(did)
    if len(panels) == 1:
        notes.append(f"{where}: dataset {did} read as panel {panels[0]} (the panel drawing it)")
        return {"kind": "panel", "panel": panels[0]}
    if panels:
        raise ShapeError(
            f"{where}: dataset {did} is drawn by several panels ({', '.join(panels)}); cite the "
            f"one the claim rests on, e.g. {_ex('panel', panel=panels[0])}"
        )
    # hk2r: the op statistic first (it carries the variation source), the panel second
    if stats := ctx.statistics_of(did):
        names = ", ".join(f"{st['name']} ({st['source']})" for st in stats[:3])
        raise ShapeError(
            f"{where}: dataset {did} is not evidence by itself. Ops returned labelled "
            f"statistics for it ({names}): cite one as returned, its source kept, e.g. "
            f"{example(stats[0])}; or draw it and cite the panel "
            f'(show(dataset="{did}", question=...) then {_ex("panel")}), which carries no source'
        )
    raise ShapeError(
        f"{where}: dataset {did} is not evidence by itself and no panel draws it. Cite a "
        f"statistic an op returned for it, as returned (it carries the variation source): "
        f"{_ex('statistic', dataset=did)}; or draw it and cite the panel "
        f'(show(dataset="{did}", question=...) then {_ex("panel")}), which carries no source'
    )


def _from_id(ref: str, ctx: EvidenceContext, where: str, notes: list[str]) -> dict:
    m = _ID.match(ref.strip())
    if m is None:
        raise ShapeError(
            f"{where}: {ref!r} is not an evidence id; cite e.g. {_ex('panel')} or "
            f"{_ex('annotation')}"
        )
    p, rid = m.group(1), ref.strip()
    if p in _REF_KINDS:
        return {"kind": _REF_KINDS[p], _REF_KINDS[p]: rid}
    if p == "d":
        return _from_dataset(rid, ctx, where, notes)
    why = {
        "c": f"{rid} is a code node: cite a statistic from its outputs "
        f"({_ex('statistic')}) or show an output dataset and cite the panel ({_ex('panel')})",
        "h": f"{rid} is a hypothesis: pass hypothesis={rid!r} with stance='for'|'against'",
        "f": f"{rid} is a finding: cite the evidence it rests on ({_ex('panel')})",
        "g": f"{rid} is a gap (a missing signal), not evidence",
        "t": f"{rid} is a thread: a reply cannot be evidence",
    }.get(p, f"{rid} names no evidence kind; cite e.g. {_ex('panel')}")
    raise ShapeError(f"{where}: {why}")


def _fill_method(item: dict, ctx: EvidenceContext, where: str, notes: list[str]) -> dict:
    """A statistic without `method`: the op's own method when exactly one op recorded this
    (dataset, name); otherwise left for validation to refuse."""
    if item.get("method") or not isinstance(item.get("dataset"), str):
        return item
    if not isinstance(item.get("name"), str):
        return item
    methods = ctx.methods_of(item["dataset"], item["name"])
    if len(methods) == 1:
        (m,) = methods
        notes.append(f"{where}: method {m!r} taken from the op that emitted {item['name']}")
        return {**item, "method": m}
    return item


def _from_dict(d: dict, ctx: EvidenceContext, where: str, notes: list[str]) -> list[dict]:
    kind = d.get("kind")
    keys = set(d) - {"kind"}
    if isinstance(kind, str):
        k = kind.strip().lower()
        if k in ("panel", "annotation"):
            if k not in d and isinstance(d.get("id"), str) and keys == {"id"}:
                rid = d["id"].strip()
                m = _ID.match(rid)
                if m is None or _REF_KINDS.get(m.group(1)) != k:
                    raise ShapeError(f"{where}: {rid!r} is not an {k} id; e.g. {_ex(k)}")
                return [{"kind": k, k: rid}]
            return [{**d, "kind": k}]
        if k == "dataset":
            did = d.get("dataset", d.get("id"))
            if keys - {"dataset", "id"} or not isinstance(did, str):
                raise ShapeError(
                    f"{where}: 'dataset' is not an evidence kind. A number computed from a "
                    f"dataset is a statistic: {_ex('statistic')}; a drawn dataset is a panel: "
                    f"{_ex('panel')}"
                )
            return [_from_dataset(did.strip(), ctx, where, notes)]
        if k in ("code", "code_node"):
            return [_from_id(str(d.get("code_node") or d.get("id") or "c"), ctx, where, notes)]
        if k == "statistic":
            return [_fill_method({**d, "kind": k}, ctx, where, notes)]
        return [{**d, "kind": k}]
    if kind is not None:
        raise ShapeError(f"{where}: kind must be a string; e.g. {_ex('panel')}")
    # `source` is both a statistic's variation source and a claim's metric source: not a cue
    stat, claim = keys & (_STAT_KEYS - {"source"}), keys & _CLAIM_KEYS
    refs = [k for k in _REF_KEYS if k in keys]
    if stat and not claim and set(refs) <= {"dataset"}:
        return [_fill_method({"kind": "statistic", **d}, ctx, where, notes)]
    if claim and not stat and not refs:
        return [{"kind": "claim", **d}]
    if keys == {"id"} and isinstance(d["id"], str):
        return [_from_id(d["id"], ctx, where, notes)]
    unknown = keys - set(_REF_KEYS)
    if not refs or unknown:
        raise ShapeError(
            f"{where}: cannot tell which evidence kind {example(d)} is "
            f"({', '.join(sorted(unknown or keys))} read as no single kind); give kind, e.g. "
            f"{_ex('panel')}, {_ex('annotation')} or {_ex('statistic')}"
        )
    out: list[dict] = []
    for k in refs:  # one dict naming several refs: one item each
        vals = d[k] if isinstance(d[k], list) else [d[k]]
        for v in vals:
            if not isinstance(v, str):
                raise ShapeError(f"{where}: {k} must be an id string; e.g. {_ex('panel')}")
            if k == "dataset":
                out.append(_from_dataset(v.strip(), ctx, where, notes))
            else:
                out.append({"kind": k, k: v.strip()})
    return out


def read_evidence(raw: Any, ctx: EvidenceContext) -> Read:
    """Evidence as a list of canonical items. Accepts one item or a list; an item is an id
    ("p1", "a1", "d3": the one panel drawing it), a dict with `kind` (`id` for the ref, kind
    "dataset" as a dataset id), or a dict without kind read by its keys (statistic fields, claim
    fields, or panel/annotation/dataset refs, one item per ref). Duplicates are dropped."""
    notes: list[str] = []
    raw = _json_text(raw)
    if isinstance(raw, str | dict):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise ShapeError(f"evidence: give a list of evidence items, e.g. [{_ex('panel')}]")
    items: list[dict] = []
    for i, it in enumerate(raw):
        where = f"evidence.{i}"
        if isinstance(it, str):
            parts = [p for p in re.split(r"[\s,]+", it) if p]
            if len(parts) > 1 and all(_ID.match(p) for p in parts):
                items += [_from_id(p, ctx, where, notes) for p in parts]
            else:
                items.append(_from_id(it, ctx, where, notes))
        elif isinstance(it, dict):
            items += _from_dict(it, ctx, where, notes)
        else:
            raise ShapeError(f"{where}: an evidence item is an object, e.g. {_ex('panel')}")
    seen: set[str] = set()
    out = []
    for it in items:
        key = json.dumps(it, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            out.append(it)
    return Read(out, notes)


# --- scope ----------------------------------------------------------------------------------


def _span(v: Any, name: str) -> tuple[Any, Any]:
    if isinstance(v, list | tuple) and len(v) == 2:
        return v[0], v[1]
    if isinstance(v, dict):
        for a, b in (("start", "end"), ("start_ms", "end_ms"), ("from", "to")):
            if a in v and b in v and len(v) == 2:
                return v[a], v[b]
    raise ShapeError(
        f'scope.{name}: give [start, end] or {{"start": ..., "end": ...}}, e.g. '
        f'"{name}": ["{SCOPE_EXAMPLE["start"]}", "{SCOPE_EXAMPLE["end"]}"]'
    )


FINDING_REQUIRED = ("source", "selector", "start", "end", "step")
#: a hypothesis' scope: where it applies (source, step and aggregation are optional there)
HYPOTHESIS_REQUIRED = ("selector", "start", "end")


def read_scope(
    scope: Any, flat: Mapping[str, Any], required: tuple[str, ...] = FINDING_REQUIRED
) -> Read:
    """The scope as {source, selector, start, end, step, aggregation, baseline_start?,
    baseline_end?}: from the `scope` dict, flat arguments, or both (a field given twice with
    different values is refused). `range`/`time_range` as [start, end] or {start, end} spell
    start/end, `baseline` the baseline. For a finding (the default `required`), a missing
    aggregation is recorded as written in the selector (said in the notes); every other missing
    field is refused with an example."""
    notes: list[str] = []
    scope = _json_text(scope)
    if scope is None:
        scope = {}
    if not isinstance(scope, dict):
        raise ShapeError(f"scope: give an object, e.g. {example(SCOPE_EXAMPLE)}")
    sc: dict[str, Any] = {}
    passed = {k: v for k, v in flat.items() if v is not None}
    for d in (dict(scope), passed):
        for alias, (a, b) in (("range", ("start", "end")), ("time_range", ("start", "end")),
                              ("baseline", ("baseline_start", "baseline_end")),
                              ("baseline_range", ("baseline_start", "baseline_end"))):  # fmt: skip
            if alias in d and d[alias] is None:  # a dumped scope: "baseline_range": null
                del d[alias]
            if alias in d:
                lo, hi = _span(d.pop(alias), alias)
                for k, v in ((a, lo), (b, hi)):
                    if k in d and d[k] != v:
                        raise ShapeError(f"scope.{k} is given twice with different values "
                                         f"({d[k]!r} and {alias} {v!r}); give it once")  # fmt: skip
                    d[k] = v
        unknown = set(d) - set(SCOPE_FIELDS)
        if unknown:
            raise ShapeError(
                f"scope: unknown field(s) {', '.join(sorted(unknown))}; scope takes "
                f"{', '.join(SCOPE_FIELDS)}, e.g. {example(SCOPE_EXAMPLE)}"
            )
        for k, v in d.items():
            if k in sc and sc[k] != v:
                raise ShapeError(f"scope.{k} is given twice with different values ({sc[k]!r} "
                                 f"and {v!r}); give it once")  # fmt: skip
            sc[k] = v
    missing = [k for k in required if sc.get(k) in (None, "")]
    if missing:
        hint = ", ".join(f'"{k}": {example(SCOPE_EXAMPLE[k])}' for k in missing)
        raise ShapeError(
            f"scope.{missing[0]}: field required ({', '.join('scope.' + k for k in missing)} "
            f"missing; add {hint}). A full scope: {example(SCOPE_EXAMPLE)}"
        )
    if sc.get("aggregation") in (None, "") and required == FINDING_REQUIRED:
        sc["aggregation"] = AGGREGATION_AS_WRITTEN
        notes.append(f"scope.aggregation not given: recorded as {AGGREGATION_AS_WRITTEN!r}")
    return Read(sc, notes)


# --- explaining what is left ----------------------------------------------------------------


def explain(e: ValidationError, evidence: list[dict]) -> str:
    """Pydantic errors of FindingIn, each with a minimal valid example for its field."""
    parts: list[str] = []
    for err in e.errors():
        loc = [str(x) for x in err["loc"]]
        path = ".".join(loc) or "<root>"
        msg = err["msg"]
        hint = ""
        if loc[:1] == ["evidence"] and len(loc) > 1 and loc[1].isdigit():
            i = int(loc[1])
            kind = loc[2] if len(loc) > 2 and loc[2] in EVIDENCE_EXAMPLES else None
            if kind is None and i < len(evidence):
                k = evidence[i].get("kind")
                kind = k if k in EVIDENCE_EXAMPLES else None
            path = ".".join([loc[0], loc[1], *loc[3:]]) if kind and len(loc) > 2 else path
            if kind == "statistic" and loc[3:] == ["method"]:
                hint = ("name the op that computed it (pass the op's evidence statistic as "
                        f"returned), e.g. {_ex('statistic')}")  # fmt: skip
            elif kind:
                hint = f"e.g. {_ex(kind)}"
            else:
                hint = (f"kinds are panel, annotation, statistic, claim: e.g. {_ex('panel')} or "
                        f"{_ex('statistic')}")  # fmt: skip
        elif loc[:1] == ["scope"]:
            field_ = loc[1] if len(loc) > 1 else None
            if field_ in ("time_range", "baseline_range"):
                pre = "" if field_ == "time_range" else "baseline_"
                path = f"scope.{pre}start/{pre}end"
                hint = (f'e.g. "{pre}start": "{SCOPE_EXAMPLE["start"]}", "{pre}end": '
                        f'"{SCOPE_EXAMPLE["end"]}"')  # fmt: skip
            elif field_ in SCOPE_EXAMPLE:
                hint = f'e.g. "{field_}": {example(SCOPE_EXAMPLE[field_])}'
            else:
                hint = f"e.g. {example(SCOPE_EXAMPLE)}"
        elif loc[:1] in (["stance"], ["hypotheses"]) or "stance" in msg:
            hint = f"e.g. hypotheses={example(HYPOTHESES_EXAMPLE)}"
        parts.append(f"{path}: {msg}" + (f" ({hint})" if hint else ""))
    return "; ".join(parts)


_FINDING_FIELDS = ("claim", "caveats", "hypothesis", "stance", "answers_panel", "scope_note")

# --- hypothesis links (aiy) -----------------------------------------------------------------

HYPOTHESES_EXAMPLE = [{"id": "h1", "stance": "for"}, {"id": "h2", "stance": "against"}]
_STANCES = ("for", "against")


def read_hypotheses(raw: Any) -> list[dict]:
    """A finding's hypothesis links as [{id, stance}]: a list of {id, stance} (or {hypothesis,
    stance}), one such dict, or a mapping {"h1": "for", "h2": "against"}. Anything else (an id
    without its stance) is refused with an example: a link always says which way it points."""
    raw = _json_text(raw)
    if raw is None:
        return []
    bad = ShapeError(
        f"hypotheses: give a list of {{id, stance}} links, e.g. {example(HYPOTHESES_EXAMPLE)} "
        "(stance for or against, one per hypothesis)"
    )
    if (
        isinstance(raw, dict)
        and raw
        and all(isinstance(k, str) and _ID.match(k) and k.startswith("h") for k in raw)
    ):
        raw = [{"id": k, "stance": v} for k, v in raw.items()]
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise bad
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise bad
        d = dict(item)
        if "hypothesis" in d and "id" not in d:
            d["id"] = d.pop("hypothesis")
        stance = d.get("stance")
        if set(d) != {"id", "stance"} or not isinstance(d["id"], str):
            raise bad
        if not isinstance(stance, str) or stance.strip().lower() not in _STANCES:
            raise bad
        out.append({"id": d["id"].strip(), "stance": stance.strip().lower()})
    return out


def hypothesis_scope(raw: Any, to_ms: Callable[[Any], int | None]) -> tuple[dict | None, list[str]]:
    """hypothesis_create's `scope` (qy7q) in the finding scope's shapes (dict, flat-free):
    {selector, start, end, source?, step?, aggregation?}, or prose. Prose is stored as text and
    said so (it is shown, not checked); a dict that does not read is refused with an example."""
    raw = _json_text(raw)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, []
    if isinstance(raw, str):
        note = ("scope stored as text: shown with the hypothesis, not checked; pass {selector, "
                "start, end, source?} to make it checkable")  # fmt: skip
        return {"text": raw.strip()}, [note]
    sc_read = read_scope(raw, {}, HYPOTHESIS_REQUIRED)
    sc = {k: v for k, v in sc_read.value.items() if not k.startswith("baseline")}
    sc["time_range"] = {"start_ms": to_ms(sc.pop("start")), "end_ms": to_ms(sc.pop("end"))}
    return sc, sc_read.notes


def finding_in(
    args: Mapping[str, Any], ctx: EvidenceContext, to_ms: Callable[[Any], int | None]
) -> tuple[FindingIn, list[str]]:
    """finding_create's arguments (scope as a dict and/or flat fields, evidence in any form
    `read_evidence` reads) as a FindingIn, and how short forms were read. Raises ShapeError
    with a minimal valid example for whatever does not read."""
    flat = {k: args.get(k) for k in (*SCOPE_FIELDS, "range")}
    sc_read = read_scope(args.get("scope"), flat)
    ev_read = read_evidence(args.get("evidence"), ctx)
    sc = dict(sc_read.value)
    start, end = sc.pop("start"), sc.pop("end")
    bs, be = sc.pop("baseline_start", None), sc.pop("baseline_end", None)
    sc["time_range"] = {"start_ms": to_ms(start), "end_ms": to_ms(end)}
    if bs is not None or be is not None:
        sc["baseline_range"] = {"start_ms": to_ms(bs), "end_ms": to_ms(be)}
    payload = {k: args.get(k) for k in _FINDING_FIELDS}
    payload["caveats"] = payload["caveats"] or []
    payload["hypotheses"] = read_hypotheses(args.get("hypotheses"))
    if isinstance(payload["stance"], str):
        payload["stance"] = payload["stance"].strip().lower()
    try:
        data = FindingIn.model_validate({**payload, "scope": sc, "evidence": ev_read.value})
    except ValidationError as e:
        raise ShapeError(explain(e, ev_read.value)) from e
    return data, [*sc_read.notes, *ev_read.notes]
