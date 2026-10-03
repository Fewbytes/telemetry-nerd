"""entities: which services (instances, namespaces, nodes) a source knows, which metric families
each reports, and whether each reported recently (bead rbz).

Cheap by construction: label-value listings from the source's index (no samples are read), one
per candidate label plus one per listed value, all bounded and cached for a minute. Honest about
coverage: an entity appears only through the labels searched, in series with samples inside the
window, so the result names the labels searched, those absent, and what was cut off.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from telemetry_nerd.catalog.search import family_prefix
from telemetry_nerd.model.time import TimeRange, format_duration, iso, parse_duration, parse_time
from telemetry_nerd.sources.base import SourceError

#: identifying labels per entity kind, most specific convention first (OTel resource attributes as
#: Prometheus labels, then Prometheus/Kubernetes conventions)
KIND_LABELS: dict[str, tuple[str, ...]] = {
    "service": (
        "service_name", "service", "app", "app_kubernetes_io_name", "k8s_deployment_name",
        "k8s_statefulset_name", "k8s_daemonset_name", "container", "job",
    ),
    "instance": (
        "service_instance_id", "instance", "k8s_pod_name", "pod", "host_name", "host",
    ),
    "namespace": ("service_namespace", "k8s_namespace_name", "namespace"),
    "node": ("k8s_node_name", "node", "nodename", "host_name", "host"),
}  # fmt: skip
#: the model a kind of entity is usually judged with (binding_suggest kind)
KIND_MODEL = {"service": "RED", "instance": "USE", "node": "USE"}
MAX_VALUES = 50
MAX_METRICS_SHOWN = 25
CACHE_TTL_MS = 60_000
_HIST = re.compile(r"_(?:bucket|count|sum|created)$")
_LABEL = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class LabelIndex(Protocol):
    async def label_values(
        self,
        label: str,
        match: Sequence[str] = (),
        rng: TimeRange | None = None,
        limit: int | None = None,
    ) -> list[str]: ...


def base_name(metric: str) -> str:
    """A classic histogram's series (`x_bucket`, `x_count`, `x_sum`) stand for `x`."""
    return _HIST.sub("", metric)


def _quote(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"')


def metric_matcher(metric: str) -> str:
    """Series of one metric, a histogram's member series included."""
    b = re.escape(base_name(metric))
    return f'{{__name__=~"{b}(?:_bucket|_count|_sum)?"}}'


def _with(selector: str | None, label: str, value: str) -> str:
    eq = f'{label}="{_quote(value)}"'
    if selector is None:
        return f"{{{eq}}}"
    return selector[:-1] + f",{eq}}}"


class EntityOps:
    def __init__(
        self,
        source: Callable[[str], Any],
        clock: Callable[[], int],
        suggest: Callable[..., dict[str, Any]],
        record: Callable[..., None] | None = None,
    ) -> None:
        self._source = source
        self._clock = clock
        self._suggest = suggest
        #: told every non-empty listing (source, label, values, truncated, start_ms, end_ms,
        #: metric): a finding over pooled evidence may cite a single value as its witness (q1p)
        self._record = record
        self._cache: dict[tuple, tuple[int, dict]] = {}

    async def entities(
        self,
        source: str = "default",
        kind: str = "service",
        label: str | None = None,
        metric: str | None = None,
        window: str = "1h",
        recent: str = "5m",
        end: str = "now",
        limit: int = MAX_VALUES,
    ) -> dict[str, Any]:
        if kind not in KIND_LABELS:
            raise ValueError(f"kind must be one of {sorted(KIND_LABELS)}, got {kind!r}")
        if label is not None and not _LABEL.match(label):
            raise ValueError(f"{label!r} is not a label name")
        src = self._source(source)
        if not hasattr(src, "label_values"):
            raise SourceError(
                f"source {source!r} cannot list label values",
                hint="query one metric with `sum by (<label>) (...)` instead",
            )
        now = self._clock()
        end_ms = parse_time(end, now)
        win_ms, recent_ms = parse_duration(window), parse_duration(recent)
        if win_ms <= 0 or not 0 < recent_ms <= win_ms:
            raise ValueError("window must be positive and recent at most the window")
        limit = max(1, min(limit, MAX_VALUES))
        key = (source, kind, label, metric, win_ms, recent_ms, end_ms // CACHE_TTL_MS, limit)
        hit = self._cache.get(key)
        if hit is not None and now - hit[0] < CACHE_TTL_MS:
            return {**hit[1], "cached": True}
        out = await self._run(src, source, kind, label, metric, end_ms, win_ms, recent_ms, limit)
        self._cache = {k: v for k, v in self._cache.items() if now - v[0] < CACHE_TTL_MS}
        self._cache[key] = (now, out)
        return out

    async def _run(
        self,
        src: LabelIndex,
        source: str,
        kind: str,
        label: str | None,
        metric: str | None,
        end_ms: int,
        win_ms: int,
        recent_ms: int,
        limit: int,
    ) -> dict[str, Any]:
        rng = TimeRange(end_ms - win_ms, end_ms)
        sel = metric_matcher(metric) if metric else None
        match = [sel] if sel else []
        searched = [label] if label else list(KIND_LABELS[kind])
        # one listing per candidate label; `limit + 1` tells "more" from "exactly limit"
        found = await asyncio.gather(
            *(src.label_values(lb, match, rng, limit + 1) for lb in searched)
        )
        values = {lb: v for lb, v in zip(searched, found, strict=True) if v}
        if self._record is not None:
            for lb, v in values.items():
                self._record(source, lb, list(v[:limit]), len(v) > limit, rng.start_ms,
                             rng.end_ms, metric)  # fmt: skip
        labels = [
            {
                "label": lb,
                "values": len(v[:limit]),
                "truncated": len(v) > limit,
                "sample": v[:10],
            }
            for lb, v in values.items()
        ]
        base: dict[str, Any] = {
            "source": source,
            "kind": kind,
            "window": [iso(rng.start_ms), iso(rng.end_ms)],
            "recent": format_duration(recent_ms),
            **({"metric": metric} if metric else {}),
            "labels": labels,
        }
        coverage = {
            "searched_labels": searched,
            "absent_labels": [lb for lb in searched if lb not in values],
            "note": (
                "Entities are listed only through the labels searched and only from series with "
                "samples in the window. An entity named by another label, or silent for the "
                "whole window, is not listed: say 'not found under these labels in this window', "
                "never 'does not exist'."
            ),
        }
        if not values:
            where = f" of {metric!r}" if metric else ""
            return {
                **base,
                "entities": [],
                "coverage": coverage,
                "note": f"no series{where} in source {source!r} carries any of the labels "
                f"{searched} in {base['window'][0]}..{base['window'][1]}. Absence of evidence is "
                "not evidence of absence: try `label=` with this source's own identity label, a "
                "longer window, or catalog_search for the signal.",
            }
        primary = next(lb for lb in searched if lb in values)
        names = values[primary][:limit]
        active = set(
            await src.label_values(
                primary, match, TimeRange(end_ms - recent_ms, end_ms), len(values[primary]) + 1
            )
        )
        per_value = await asyncio.gather(
            *(src.label_values("__name__", [_with(sel, primary, v)], rng) for v in names)
        )
        model = KIND_MODEL.get(kind)
        bindings = self._bindings(source, model, primary) if model else []
        rows = []
        for v, metrics in zip(names, per_value, strict=True):
            bases = sorted({base_name(m) for m in metrics})
            series_names = set(metrics) | set(bases)
            row: dict[str, Any] = {
                "label": primary,
                "value": v,
                "active_recent": v in active,
                "families": sorted({family_prefix(b) for b in bases}),
                "metrics": bases[:MAX_METRICS_SHOWN],
            }
            if len(bases) > MAX_METRICS_SHOWN:
                row["metrics_more"] = len(bases) - MAX_METRICS_SHOWN
            if model:
                fits = [b for b in bindings if b["metrics"] & series_names]
                row["bindings"] = [b["id"] for b in fits]
                if fits:
                    row["next"] = f'binding_suggest(kind="{model}", key="{v}")'
            rows.append(row)
        if any(r["value"] not in active for r in rows):
            coverage["silent"] = (
                "active_recent=false: samples in the window but none in the last "
                f"{format_duration(recent_ms)}. Silent is not healthy and not gone (principle 9)."
            )
        if len(values[primary]) > limit:
            coverage["truncated"] = f"only the first {limit} values of {primary} are listed"
        return {**base, "primary_label": primary, "entities": rows, "coverage": coverage}

    def _bindings(self, source: str, model: str, label: str) -> list[dict[str, Any]]:
        """binding_suggest proposals joined on `label`, with the metrics each role uses."""
        out = []
        for s in self._suggest(source, kind=model, limit=1000)["suggestions"]:
            if label not in s.get("join_on", ()):
                continue
            ms = {m for m in s["roles"].values() if m}
            out.append({"id": s["id"], "metrics": ms})
        return out
