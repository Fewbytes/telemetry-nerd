"""Uncertainty policy (spec §5.3): unknown is citable but flagged; aggregation is maximalist.

Three layers use this module:

* **Dataset status.** A dataset carries at most one uncertainty-status caveat
  (`exchange.fmt.UNCERTAINTY_STATUS`; a fit may also carry a per-parameter `no_uncertainty`): `no_uncertainty` (unknown), `input_uncertainty_unknown`
  (own interval, an input's unknown: a lower bound) or `uncertainty_not_propagated` (own
  interval, inputs' intervals not folded in: a lower bound). Tier-2 ingest assigns it
  (`exchange.fmt.output_uncertainty_status`); `dataset_status` reads it, and walks parents for
  derived datasets that did not get one.
* **Tier-1 statistics.** An op computes its own interval for each `evidence` statistic. When
  the data it ran over is not clean, `mark_statistics(out, datasets, [inputs])` marks every
  statistic in the result with `params.input_uncertainty` ("unknown" | "not_propagated") and
  adds the matching caveat to `out["caveats"]`. New ops (e.g. check_littles_law) call it with
  all their input dataset ids after building their result::

      out = {"caveats": [...], "series": [{..., "evidence": wire.statistic(...)}]}
      return mark_statistics(out, self.datasets, [lam_id, w_id, l_id])

  Pass the datasets whose error the op did *not* fold into its interval: an op that
  propagates an input's declared interval (maximalist: the wider of propagated and its own)
  leaves that input out. A statistic for which no interval can be derived is still evidence:
  `wire.statistic(..., interval=None, ...)` emits it with `uncertainty_unknown: true`.
  `iter_statistics(out)` yields every evidence statistic in a result.

* **Findings.** `evidence_flags` derives, per cited evidence item, the flags a finding stores
  and shows (`uncertainty_unknown`, `input_uncertainty_unknown`, `uncertainty_not_propagated`).
  They are server-derived from the datasets, plus whatever an op put in
  `params.input_uncertainty` (a statistic passed on as is keeps it). Flags never refuse a
  finding; only fabrication does (`core.code_outputs.evidence_problem`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

from telemetry_nerd.analysis.sources import measurement_items
from telemetry_nerd.datasets.store import DatasetMeta, DatasetStore
from telemetry_nerd.exchange.fmt import (
    ESTIMATE,
    INPUT_UNCERTAINTY_UNKNOWN,
    NO_UNCERTAINTY,
    UNCERTAINTY_NOT_PROPAGATED,
    UNCERTAINTY_STATUS,
)
from telemetry_nerd.model.errors import NotFound

#: evidence flag: the cited value itself has no interval (and is not exact)
UNCERTAINTY_UNKNOWN = "uncertainty_unknown"
EVIDENCE_FLAGS = (UNCERTAINTY_UNKNOWN, INPUT_UNCERTAINTY_UNKNOWN, UNCERTAINTY_NOT_PROPAGATED)

#: key in an evidence statistic's `params` an op sets when its inputs are not clean
PARAM_KEY = "input_uncertainty"
INPUT_UNKNOWN, INPUT_NOT_PROPAGATED = "unknown", "not_propagated"
_PARAM_FLAG = {
    INPUT_UNKNOWN: INPUT_UNCERTAINTY_UNKNOWN,
    INPUT_NOT_PROPAGATED: UNCERTAINTY_NOT_PROPAGATED,
}

_MESSAGES = {
    UNCERTAINTY_UNKNOWN: "uncertainty unknown: the value has no interval and is not exact "
    "(unknown, not zero)",
    INPUT_UNCERTAINTY_UNKNOWN: "input uncertainty unknown: the interval covers this step only; "
    "the error of what it was computed from is unknown, so it is a lower bound",
    UNCERTAINTY_NOT_PROPAGATED: "uncertainty not propagated: the interval leaves out the "
    "inputs' declared intervals, so it is a lower bound",
}


def message(flag: str) -> str:
    """Human wording of an evidence flag or dataset uncertainty status."""
    return _MESSAGES.get(UNCERTAINTY_UNKNOWN if flag == NO_UNCERTAINTY else flag, flag)


def own_status(meta: DatasetMeta) -> str | None:
    """The uncertainty-status caveat a dataset carries itself, or None. For a fit, the
    `no_uncertainty` caveat says some parameter lacks an interval: a per-parameter matter,
    so it is not the fit's status (see `evidence_flags`)."""
    for c in meta.source_caveats:
        if c in UNCERTAINTY_STATUS and not (
            meta.representation == ESTIMATE and c == NO_UNCERTAINTY
        ):
            return c
    return None


def dataset_status(datasets: DatasetStore, dataset: str | DatasetMeta) -> str | None:
    """A dataset's uncertainty status: its own caveat, else `input_uncertainty_unknown` when an
    ancestor is not clean (a derived dataset that did not get a status of its own), else None."""
    meta = datasets.meta(dataset) if isinstance(dataset, str) else dataset
    seen: set[str] = set()

    def walk(m: DatasetMeta, top: bool) -> str | None:
        if m.id in seen:
            return None
        seen.add(m.id)
        if st := own_status(m):
            return st
        if not top and m.representation == ESTIMATE and NO_UNCERTAINTY in m.source_caveats:
            return NO_UNCERTAINTY  # data derived from a fit with a parameter of unknown error
        for pid in m.parents:
            try:
                pm = datasets.meta(pid)
            except NotFound:
                continue
            if walk(pm, False):
                return INPUT_UNCERTAINTY_UNKNOWN
        return None

    return walk(meta, True)


def input_status(datasets: DatasetStore, dataset_ids: Iterable[str]) -> str | None:
    """What a tier-1 statistic computed over these datasets inherits: "unknown" when one's
    uncertainty is unknown or only a lower bound; "not_propagated" when one carries a declared
    interval the op does not propagate; None when all are clean measurements or exact."""
    out = None
    for did in dict.fromkeys(dataset_ids):
        meta = datasets.meta(did)
        if dataset_status(datasets, meta):
            return INPUT_UNKNOWN
        u = meta.uncertainty or {}
        if u and not u.get("exact"):
            out = INPUT_NOT_PROPAGATED
    return out


def iter_statistics(obj: Any) -> Iterable[dict]:
    """Every evidence statistic dict inside an op result."""
    if isinstance(obj, dict):
        if obj.get("kind") == "statistic" and "dataset" in obj and "value" in obj:
            yield obj
            return
        for v in obj.values():
            yield from iter_statistics(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_statistics(v)


def mark_statistics(out: dict, datasets: DatasetStore, dataset_ids: Sequence[str]) -> dict:
    """Mark a tier-1 op result computed over `dataset_ids` (in place; returns `out`): every
    evidence statistic gets `params.input_uncertainty` and `out["caveats"]` the matching
    caveat when the inputs are not clean. The op's own intervals stay as they are: they are
    still evidence (spec §5.3), with the flag saying they are a lower bound. Every statistic is
    also recorded (`DatasetStore.record_statistics`) so a finding citing one without its
    variation `source` gets it back (spec §5.4)."""
    datasets.record_statistics(list(iter_statistics(out)))
    status = input_status(datasets, dataset_ids)
    if status is None:
        return out
    for st in iter_statistics(out):
        params = st.setdefault("params", {})
        if params.get(PARAM_KEY) != INPUT_UNKNOWN:  # unknown wins over not_propagated
            params[PARAM_KEY] = status
    caveats = out.setdefault("caveats", [])
    if isinstance(caveats, list) and (c := _PARAM_FLAG[status]) not in caveats:
        caveats.append(c)
        # spec §5.4: unknown or unpropagated input error is the measurement system's
        if isinstance(out.get("variation"), list):
            out["variation"] += measurement_items([c])
    return out


def _status_flag(status: str | None) -> str | None:
    """Evidence flag for a value with an interval (or exact) over data with this status."""
    if status in (NO_UNCERTAINTY, INPUT_UNCERTAINTY_UNKNOWN):
        return INPUT_UNCERTAINTY_UNKNOWN
    return status  # UNCERTAINTY_NOT_PROPAGATED or None


def statistic_flags(datasets: DatasetStore, ref: dict) -> list[str]:
    """Flags for one cited statistic (a StatisticRef as a dict)."""
    meta = datasets.meta(ref["dataset"])
    flags: list[str] = []
    if ref.get("uncertainty_unknown") or (ref.get("interval") is None and not ref.get("exact")):
        flags.append(UNCERTAINTY_UNKNOWN)
    elif meta.representation == ESTIMATE:
        # the parameter's own interval is cited as stored; only the fit's inputs can flag it
        if f := _status_flag(own_status(meta) or _parents_status(datasets, meta)):
            flags.append(f)
    elif f := _status_flag(dataset_status(datasets, meta)):
        flags.append(f)
    declared = (ref.get("params") or {}).get(PARAM_KEY)
    if declared in _PARAM_FLAG:
        flags.append(_PARAM_FLAG[declared])
    if UNCERTAINTY_UNKNOWN in flags:
        return [UNCERTAINTY_UNKNOWN]  # no interval at all: nothing to call a lower bound
    if INPUT_UNCERTAINTY_UNKNOWN in flags:
        return [INPUT_UNCERTAINTY_UNKNOWN]
    return list(dict.fromkeys(flags))


def _parents_status(datasets: DatasetStore, meta: DatasetMeta) -> str | None:
    for pid in meta.parents:
        try:
            if dataset_status(datasets, pid):
                return INPUT_UNCERTAINTY_UNKNOWN
        except NotFound:
            continue
    return None


def panel_flags(datasets: DatasetStore, dataset_ids: Iterable[str]) -> list[str]:
    """Flags for a cited panel: from each dataset it draws (values shown without a known
    interval are `uncertainty_unknown`)."""
    flags = []
    for did in dataset_ids:
        st = dataset_status(datasets, did)
        if st == NO_UNCERTAINTY:
            flags.append(UNCERTAINTY_UNKNOWN)
        elif st:
            flags.append(st)
    return list(dict.fromkeys(flags))


def evidence_flags(
    datasets: DatasetStore,
    refs: Sequence[dict],
    panel_datasets: Callable[[str], list[str]],
) -> list[dict]:
    """[{evidence: index, flag, message}] for a finding's evidence (refs as dicts, in order).
    `panel_datasets(panel_id)` lists the datasets a panel draws."""
    out = []
    for i, ref in enumerate(refs):
        kind = ref.get("kind")
        if kind == "statistic":
            flags = statistic_flags(datasets, ref)
            what = f"statistic {ref['name']!r} of {ref['dataset']}"
        elif kind == "panel":
            flags = panel_flags(datasets, panel_datasets(ref["panel"]))
            what = f"panel {ref['panel']}"
        else:
            continue
        out += [{"evidence": i, "flag": f, "message": f"{what}: {message(f)}"} for f in flags]
    return out
