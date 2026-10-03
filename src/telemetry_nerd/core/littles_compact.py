"""check_littles_law as the MCP returns it by default: compact (zroj).

The full result (`LittlesOps.summary`) carries every window of every group and each group's
evidence with its parameters: 54k characters for 3 pods at 40 s windows over 12 min in eval
round 4, more than the agent could read. Bulk data never enters Claude's context, so the
default view keeps what answers the question and drops only what repeats or is unflagged:

* everything at the top (summary, discrepancy, verdict, classification, warnings, variation,
  hints...) as is; assumptions as name + status, with their text where flagged;
* the total and every *flagged* group (verdict not consistent, a flagged or transient window, a
  promoted load peak, a growing L) in full but without the per-window rows of consistent
  windows: their flagged windows, classification and evidence are never cut;
* the other groups as one row each in a table, at most `MAX_GROUP_ROWS` (flagged first, then by
  |L/(λW) − 1|), and the rest counted with their verdicts and ratio range;
* evidence statistics as finding_create takes them, with a one-line method (`SHORT_METHODS`,
  recorded like the op's own so their variation source is found) and only the parameters that
  say where (group, window, phase); the values they repeat are in the group.

`detail=true` returns the full result; `group="pod=x"` one group in full.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from telemetry_nerd.core.uncertainty import PARAM_KEY as INPUT_PARAM

MAX_GROUP_ROWS = 10
GROUP_COLUMNS = ["group", "verdict", "ratio", "ci95_lo", "ci95_hi", "L", "lambda_W",
                 "flagged_windows", "transient_or_promoted"]  # fmt: skip
#: evidence params kept in the compact view: where and what (the values repeat the group's)
_KEPT_PARAMS = ("group", "window", "phase", "promoted_from", "reference", INPUT_PARAM)
#: the compact view's method per statistic: the op and what was computed, in one line (the full
#: text, `detail=true`, explains the error terms); recorded like the op's own (`compact`)
SHORT_METHODS = {
    "littles_law_ratio": "R = L / (lambda W) over the range, measurement interval",
    "littles_law_discrepancy": "L / (lambda W) - 1 over the range, measurement interval",
    "mean_concurrency_L": "time average of the in-flight gauge, sampling interval",
    "lambda_times_W": "throughput x mean latency (rate(_sum)/rate(_count)), timing interval",
    "littles_law_systematic_offset": "L / (lambda W) shared by the non-transient windows",
    "littles_law_transient": "L / (lambda W) - 1 of a transient window, measurement interval",
    "littles_law_backlog_growth": "backlog change over a promoted load-peak window",
    "littles_law_latency_rise": "mean latency rise into a promoted load peak",
    "littles_law_peak_growth": "growth of the discrepancy across repeated load peaks",
}
_GROUP_DISCREPANCY = ("difference", "difference_ci95", "relative", "relative_ci95", "per_window")
#: group keys dropped in the compact view (per-window rows and the error budget's terms)
_BULK = ("windows", "window_columns", "sd_terms", "bias", "arrivals", "gauge_samples")
DETAIL_NOTE = (
    "compact: consistent windows' rows, method texts and unflagged groups' evidence left out "
    '(flagged ones never). detail=true: all; group="<label>=<value>": one group in full'
)


def group_name(labels: dict | None) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted((labels or {}).items())) or "total"


def flagged(g: dict) -> bool:
    """Does this group carry anything beyond common-cause noise around L = λW?"""
    c = g.get("classification") or {}
    return bool(
        g.get("verdict") != "consistent"
        or g.get("flagged_windows")
        or c.get("transient")
        or c.get("promoted")
        or (g.get("growing") or {}).get("growing")
    )


def _special(g: dict) -> int:
    c = g.get("classification") or {}
    return len(c.get("transient") or []) + len(c.get("promoted") or [])


def _rel(g: dict) -> float:
    r = g.get("ratio")
    return abs(r - 1) if isinstance(r, int | float) else -1.0


def short_method(name: str) -> str:
    return f"check_littles_law: {SHORT_METHODS.get(name, name.replace('_', ' '))}"


def _evidence(ev: list[dict], whole: list | None) -> list[dict]:
    """Compact evidence: one-line method, where-params only (a window equal to the whole range
    is the default and left out), `exact` only when true."""
    out = []
    for st in ev:
        params = {k: v for k, v in (st.get("params") or {}).items()
                  if k in _KEPT_PARAMS and not (k == "window" and v == whole)}  # fmt: skip
        item = {k: v for k, v in st.items() if not (k == "exact" and v is False)}
        out.append({**item, "method": short_method(st["name"]), "params": params})
    return out


def _group(g: dict, top_level: bool, whole: list | None) -> dict:
    out = {k: v for k, v in g.items() if k not in _BULK}
    if top_level:  # the top of the result (summary, discrepancy...) holds these for the total
        for k in ("discrepancy", "classification"):
            out.pop(k, None)
    else:  # the group's own numbers stay; the tests' settings and noise model are the total's
        out.pop("common_cause", None)
        d = out.get("discrepancy") or {}
        out["discrepancy"] = {k: d[k] for k in _GROUP_DISCREPANCY if k in d}
        c = out.get("classification") or {}
        out["classification"] = {k: v for k, v in c.items() if k != "promotion"}
    if "evidence" in out:
        out["evidence"] = _evidence(out["evidence"], whole)
    return out


def _row(g: dict) -> list[Any]:
    ci = g.get("ci95") or [None, None]
    return [group_name(g.get("labels")), g.get("verdict"), g.get("ratio"), ci[0], ci[1],
            g.get("L"), g.get("lambda_W"), len(g.get("flagged_windows") or []),
            _special(g)]  # fmt: skip


def compact(out: dict, group: str | None = None) -> dict:
    """The compact view of a check_littles_law result (see the module doc). `group`: one
    group's name (`group_name`), returned in full beside the compact rest."""
    groups: list[dict] = out.get("groups") or []
    res: dict[str, Any] = {
        k: v for k, v in out.items() if k not in ("total", "groups", "method", "assumptions")
    }
    res["assumptions"] = [
        {"name": a["name"], "status": a["status"],
         **({"detail": a["detail"]} if a.get("status") == "flagged" and "detail" in a else {})}
        for a in out.get("assumptions") or []
    ]  # fmt: skip
    if isinstance(res.get("classification"), dict):  # the promotion tests' settings: detail
        res["classification"] = {k: v for k, v in res["classification"].items() if k != "promotion"}
    warnings = out.get("warnings") or []
    if warnings and all(w in (out.get("summary") or "") for w in warnings):
        # the summary quotes them word for word: said once
        res["warnings"] = f"{len(warnings)} warning(s), quoted in full at the end of summary"
    # the total's own span (its first statistic's window): a group statistic over it says so
    whole = next((e.get("params", {}).get("window") for e in out["total"].get("evidence") or []),
                 None)  # fmt: skip
    res["total"] = _group(out["total"], top_level=True, whole=whole)
    if group is not None:
        hit = [g for g in groups if group_name(g.get("labels")) == group]
        if not hit:
            names = ", ".join(group_name(g.get("labels")) for g in groups[:MAX_GROUP_ROWS])
            raise ValueError(f"no group {group!r} in this result (groups: {names or 'none'})")
        res["group"] = hit[0]
    if groups:
        ranked = sorted(groups, key=lambda g: (not flagged(g), -_rel(g), group_name(g["labels"])))
        marked = [g for g in ranked if flagged(g)]
        listed = ranked[: max(MAX_GROUP_ROWS, len(marked))]  # flagged groups are never cut
        res["group_columns"] = GROUP_COLUMNS
        res["groups"] = [_row(g) for g in listed]
        if marked:
            res["flagged_groups"] = [_group(g, top_level=False, whole=whole) for g in marked]
        rest = ranked[len(listed) :]
        if rest:
            ratios = [g["ratio"] for g in rest if isinstance(g.get("ratio"), int | float)]
            res["other_groups"] = {
                "count": len(rest),
                "verdicts": dict(Counter(str(g.get("verdict")) for g in rest)),
                "ratio_range": [min(ratios), max(ratios)] if ratios else None,
                "note": "none flagged; not listed one by one (group=... or detail=true)",
            }
    res["detail"] = DETAIL_NOTE
    return res


def statistics(res: dict) -> list[dict]:
    """The compact view's evidence statistics (to record beside the op's own, so a finding
    citing one as given gets its variation source back like the full one)."""
    out = list(res["total"].get("evidence") or [])
    for g in res.get("flagged_groups") or []:
        out += g.get("evidence") or []
    return out
