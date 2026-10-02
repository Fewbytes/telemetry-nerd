"""Datasets produced by tier-2 code (spec §5.2) as seen by show() and the tier-1 ops.

A code output is fixed data: its expression (`code:<node>/<name>`) names an output, not a
query, so no path may re-fetch it, widen it, profile it or look its metrics up in the catalog.
Ops that need the source (reference windows, previous cycles, the operating profile, a ghost)
refuse with the reason; ops over the stored values work. Fits (`estimate`) have parameters,
not rows: they are cited, not drawn."""

from __future__ import annotations

from telemetry_nerd.datasets.store import DatasetMeta, DatasetStore
from telemetry_nerd.exchange.fmt import ESTIMATE
from telemetry_nerd.model.caveats import Caveat

CODE_OUTPUT = "code_output"  # info caveat: where the data came from and what that rules out


def origin(meta: DatasetMeta) -> str:
    """The producer as a phrase: output 'x' of code node c3."""
    p = meta.producer or {}
    return f"output {p.get('output')!r} of code node {p.get('node')}"


def refuse_requery(meta: DatasetMeta, what: str, hint: str = "") -> None:
    """Raise when `meta` is a code output: `what` would have to fetch it again from a source."""
    if meta.code_node is None:
        return
    raise ValueError(
        f"{meta.id} is {origin(meta)}: fixed data, not a source query, so {what} cannot "
        "fetch it again"
        + (f" (hint: {hint})" if hint else " (hint: run the op on the code's input datasets, or "
           "compute it in the code over inputs that cover it)")
    )  # fmt: skip


def code_caveat(meta: DatasetMeta) -> Caveat:
    parents = ", ".join(meta.parents) or "no input datasets"
    return Caveat(
        code=CODE_OUTPUT,
        severity="info",
        message=(
            f"Produced by code node {meta.code_node} (output {(meta.producer or {}).get('output')!r}) "
            f"from {parents}. Fixed data: never re-queried; no operating profile, reference "
            "windows or catalog context."
        ),
        source=f"code:{meta.code_node}",
    )


def _citable(meta: DatasetMeta) -> list[dict]:
    """Ready-to-cite evidence statistics for a fit's parameters, as stored. One without an
    interval is cited with uncertainty_unknown=true (spec §5.3: citable, flagged)."""
    fit = meta.fit or {}
    out = []
    for name, p in (fit.get("params") or {}).items():
        unknown = p.get("interval") is None and not p.get("exact")
        out.append({
            "kind": "statistic", "dataset": meta.id, "name": name, "value": p["value"],
            "interval": p.get("interval"), "exact": bool(p.get("exact")),
            **({"uncertainty_unknown": True} if unknown else {}),
            "method": fit.get("method") or "fit",
        })  # fmt: skip
    return out


def prediction_of(datasets: DatasetStore, meta: DatasetMeta) -> str | None:
    """The `<fit>_prediction` dataset stored with a fit, if the code gave one."""
    want = f"{(meta.producer or {}).get('output')}_prediction"
    for m in datasets.list_metas():
        if (
            m.code_node == meta.code_node
            and (m.producer or {}).get("output") == want
            and m.parents[:1] == [meta.id]
        ):
            return m.id
    return None


def refuse_estimate(datasets: DatasetStore, meta: DatasetMeta, what: str = "show") -> None:
    """A fit has parameters, not rows: say what to draw or cite instead."""
    if meta.representation != ESTIMATE:
        return
    pred = prediction_of(datasets, meta)
    cite = _citable(meta)
    hints = []
    if pred:
        hints.append(f"show its prediction {pred}")
    if cite:
        names = ", ".join(c["name"] for c in cite)
        c = next((c for c in cite if not c.get("uncertainty_unknown")), cite[0])
        stated = (
            "uncertainty_unknown: true" if c.get("uncertainty_unknown") else
            f"interval: {c['interval']}, exact: {str(c['exact']).lower()}"
        )  # fmt: skip
        hints.append(
            f"cite a parameter ({names}) as evidence, e.g. "
            f"{{kind: statistic, dataset: {meta.id}, name: {c['name']!r}, value: "
            f"{c['value']}, {stated}, method: {c['method']!r}}}"
        )
    model = (meta.fit or {}).get("model", "model")
    raise ValueError(
        f"{meta.id} is a fit ({model}, {origin(meta)}): parameters, not rows, so {what} has "
        "nothing to draw"
        + (f" (hint: {'; or '.join(hints)})" if hints else "")
    )  # fmt: skip


def evidence_problem(meta: DatasetMeta, statistic: dict | None) -> str | None:
    """Why citing a code output would misstate it, or None. `statistic` is the cited
    StatisticRef as a dict; None for a panel citation.

    Only fabrication is refused: a fit cited as a panel (it has no rows to show), a fit
    parameter that does not exist or is not cited exactly as stored. Unknown uncertainty is
    never a reason (spec §5.3): `core.uncertainty.evidence_flags` flags it instead."""
    if meta.code_node is None or meta.representation != ESTIMATE:
        return None
    if statistic is None:
        return f"{meta.id} is a fit: cite one of its parameters as a statistic, not a panel"
    name = statistic["name"]
    params = (meta.fit or {}).get("params") or {}
    if name not in params:
        return f"{meta.id} has no fit parameter {name!r} (parameters: {', '.join(params)})"
    value, interval, exact = _as_cited(params[name])
    if _as_cited(statistic) != (value, interval, exact):
        if interval is not None:
            stored = f"interval={interval}"
        elif exact:
            stored = "exact=true"
        else:
            stored = "no interval (cite it with uncertainty_unknown=true)"
        return f"fit parameter {name!r} of {meta.id} is value={value}, {stored}: cite it as stored"
    return None


def _as_cited(stat: dict) -> tuple:
    """(value, interval as a list or None, exact) of a cited statistic or a stored fit param."""
    interval = stat.get("interval")
    return stat["value"], None if interval is None else list(interval), bool(stat.get("exact"))
