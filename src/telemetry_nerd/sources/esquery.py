"""Elasticsearch / OpenSearch `expr`: parse, validate, build the request (pure, no I/O).

`expr` is the caller's native request body: a `query` clause plus at most one metric
aggregation, optionally inside one `terms` grouping (one field) or one `composite` grouping (2+
fields), plus an optional top-level `runtime_mappings`. Time never appears in it: the adapter
wraps the aggregation in a `date_histogram` built from the time range and query step, as the
PromQL adapter adds start/end/step. Design:
docs/superpowers/specs/2026-10-07-elasticsearch-opensearch-adapter-design.md.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.base import SourceError

Form = Literal["rate", "field_rate", "sum", "stats", "percentile", "histogram", "range"]
Need = Literal["numeric", "aggregatable"]
GroupKind = Literal["terms", "composite"]

RESERVED_PREFIX = "__tn_"
TIME_AGG = "__tn_time"  # the adapter's date_histogram
COUNT_AGG = "__tn_n"  # the adapter's value_count beside a percentiles aggregation
GROUP_AGG = "__tn_by"  # the adapter's terms/composite built from query_distribution's `by`
_METRIC_FORMS: dict[str, Form] = {
    "value_count": "field_rate",
    "sum": "sum",
    "stats": "stats",
    "percentiles": "percentile",
    "histogram": "histogram",
}
_DIST_KINDS = frozenset({"histogram", "range"})  # query_distribution forms: no grouping by terms
_KINDS = frozenset({"terms", "composite", "range", *_METRIC_FORMS})
_TOP_KEYS = frozenset({"query", "aggs", "aggregations", "runtime_mappings"})

FORMS_HINT = (
    'accepted: {"query": {...}} alone (documents per second); or "aggs" with ONE of '
    "value_count (a field's values per second), sum (a numeric field's values summed per "
    "second), stats (mean/min/max/count of a numeric field), percentiles with exactly one "
    '"percents" value; optionally inside one terms aggregation (one series per term) or one '
    "composite aggregation of 2+ terms sources (one series per combination). histogram and "
    "range only in query_distribution. an optional top-level runtime_mappings defines scripted "
    "fields usable anywhere in query/aggs. No time range or date_histogram in expr: time is "
    "start/end/step"
)


def _refuse(message: str) -> SourceError:
    return SourceError(f"unsupported Elasticsearch expr: {message}", hint=FORMS_HINT)


def _one(aggs: object) -> tuple[str, dict]:
    if not isinstance(aggs, dict) or len(aggs) != 1:
        raise _refuse("aggs must hold exactly one aggregation")
    [(name, body)] = aggs.items()
    if str(name).startswith(RESERVED_PREFIX):
        raise _refuse(f"aggregation names starting with {RESERVED_PREFIX} are reserved ({name})")
    if not isinstance(body, dict):
        raise _refuse(f"{name}: an aggregation is a JSON object")
    return str(name), body


def _agg(name: str, body: dict) -> tuple[str, dict, object | None]:
    """(aggregation type, its params, its sub-aggregations or None)."""
    if "aggs" in body and "aggregations" in body:
        raise _refuse(f"{name}: give aggs or aggregations, not both")
    sub = body.get("aggs", body.get("aggregations"))
    kinds = [k for k in body if k not in ("aggs", "aggregations", "meta")]
    if len(kinds) != 1:
        raise _refuse(f"{name}: expected one aggregation type, got {kinds}")
    kind = kinds[0]
    if kind not in _KINDS:
        raise _refuse(f"{name}: {kind} is not supported in v1")
    params = body[kind]
    if not isinstance(params, dict):
        raise _refuse(f"{name}: {kind} takes a JSON object")
    if "script" in params:
        raise _refuse(f"{name}: scripts are not supported")
    return kind, params, sub


def _field(name: str, kind: str, params: dict) -> str:
    field = params.get("field")
    if not isinstance(field, str) or not field:
        raise _refuse(f"{name}: {kind} needs a field")
    return field


def _composite_fields(name: str, params: dict) -> tuple[str, ...]:
    """The fields of a `composite` grouping's `sources`, validated to the narrow shape this
    adapter supports: 2+ sources, each exactly one `{name: {"terms": {"field": ...}}}` entry."""
    extra = sorted(set(params) - {"sources", "size"})
    if extra:
        raise _refuse(f"{name}: composite takes sources and size only, not {extra}")
    sources = params.get("sources")
    if not isinstance(sources, list) or len(sources) < 2:
        raise _refuse(f"{name}: composite needs 2+ sources; group by one field with terms instead")
    fields = []
    for src in sources:
        if not isinstance(src, dict) or len(src) != 1:
            raise _refuse(
                f"{name}: each composite source is one "
                '{"<source name>": {"terms": {"field": ...}}}'
            )
        [(src_name, src_body)] = src.items()
        if not isinstance(src_body, dict) or set(src_body) != {"terms"}:
            raise _refuse(
                f"{name}: composite source {src_name} must be a plain terms source (no nested "
                "aggs, no date_histogram/histogram sources)"
            )
        fields.append(_field(f"{name}.{src_name}", "terms", src_body["terms"]))
    return tuple(fields)


def _number(x: object) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool)


def _range_edges(name: str, params: dict) -> tuple[float, ...]:
    """Validate a `range` aggregation's `ranges` and return the finite boundaries it names,
    ascending (the open ends of the first/last range are -inf/+inf, carried separately)."""
    extra = sorted(set(params) - {"field", "ranges", "keyed"})
    if extra:
        raise _refuse(f"{name}: range takes field and ranges only, not {extra}")
    if params.get("keyed") is True:
        raise _refuse(f"{name}: keyed ranges are not supported")
    ranges = params.get("ranges")
    if not isinstance(ranges, list) or not ranges:
        raise _refuse(f"{name}: range needs a non-empty ranges list")
    edges: set[float] = set()
    for i, r in enumerate(ranges):
        if not isinstance(r, dict) or not r or not set(r) <= {"from", "to"}:
            raise _refuse(f'{name}: ranges[{i}] must be {{"from": ..., "to": ...}} (at least one)')
        for k in ("from", "to"):
            if k in r:
                if not _number(r[k]):
                    raise _refuse(f"{name}: ranges[{i}].{k} must be a number")
                edges.add(float(r[k]))
    return tuple(sorted(edges))


def _runtime_mappings(doc: dict) -> dict | None:
    runtime_mappings = doc.get("runtime_mappings")
    if runtime_mappings is None:
        return None
    if not isinstance(runtime_mappings, dict) or not runtime_mappings:
        raise _refuse("runtime_mappings must be a non-empty JSON object")
    for fname, fbody in runtime_mappings.items():
        if not isinstance(fbody, dict) or "type" not in fbody:
            raise _refuse(f'runtime_mappings.{fname} must be {{"type": ..., ...}}')
    return runtime_mappings


@dataclass(frozen=True)
class EsQuery:
    query: dict
    form: Form
    metric_name: str | None = None
    metric: dict | None = None  # {kind: params}, sent as written
    field: str | None = None
    group_name: str | None = None
    group: dict | None = None  # the terms/composite params, sent as written
    group_kind: GroupKind | None = None
    group_fields: tuple[str, ...] = ()  # 1 field (terms) or 2+ (composite)
    q: float | None = None  # percentile form: percents[0] / 100
    interval: float | None = None  # histogram form
    offset: float = 0.0  # histogram form
    edges: tuple[float, ...] = ()  # range form: finite boundaries named by ranges, ascending
    runtime_mappings: dict | None = None  # top-level, sent as written

    @classmethod
    def parse(cls, expr: str) -> EsQuery:
        try:
            doc = json.loads(expr)
        except (TypeError, ValueError) as e:
            raise _refuse(f"expr is not JSON ({e})") from e
        if not isinstance(doc, dict):
            raise _refuse("expr must be a JSON object with query and/or aggs")
        extra = sorted(set(doc) - _TOP_KEYS)
        if extra:
            raise _refuse(f"unexpected keys {extra}: expr holds only query, aggs and "
                          "runtime_mappings")  # fmt: skip
        if "aggs" in doc and "aggregations" in doc:
            raise _refuse("give aggs or aggregations, not both")
        query = doc.get("query", {"match_all": {}})
        if not isinstance(query, dict):
            raise _refuse("query must be a JSON object (Query DSL)")
        runtime_mappings = _runtime_mappings(doc)
        if "aggs" not in doc and "aggregations" not in doc:
            return cls(query=query, form="rate", runtime_mappings=runtime_mappings)
        name, body = _one(doc.get("aggs", doc.get("aggregations")))
        kind, params, sub = _agg(name, body)
        if kind not in ("terms", "composite"):
            if sub is not None:
                raise _refuse(f"{name}: a {kind} aggregation takes no sub-aggregations")
            return cls._metric(query, name, kind, params, None, runtime_mappings)
        fields = _field(name, kind, params) if kind == "terms" else _composite_fields(name, params)
        group = (name, params, kind, (fields,) if kind == "terms" else fields)
        if sub is None:
            return cls(query=query, form="rate", group_name=name, group=params,
                       group_kind=kind, group_fields=group[3],
                       runtime_mappings=runtime_mappings)  # fmt: skip
        mname, mbody = _one(sub)
        mkind, mparams, msub = _agg(mname, mbody)
        if mkind in ("terms", "composite"):
            raise _refuse(
                f"{mname}: two-level nesting isn't supported; use one composite aggregation "
                "for multi-field grouping"
            )
        if mkind in _DIST_KINDS:
            raise _refuse(f"{mname}: group a {mkind} with query_distribution's by, not terms")
        if msub is not None:
            raise _refuse(f"{mname}: aggregations nest at most terms -> metric")
        return cls._metric(query, mname, mkind, mparams, group, runtime_mappings)

    @classmethod
    def _metric(
        cls,
        query: dict,
        name: str,
        kind: str,
        params: dict,
        group: tuple[str, dict, GroupKind, tuple[str, ...]] | None,
        runtime_mappings: dict | None = None,
    ) -> EsQuery:
        field = _field(name, kind, params)
        grouping = (
            {"group_name": group[0], "group": group[1], "group_kind": group[2],
             "group_fields": group[3]} if group else {}
        )  # fmt: skip
        base: dict[str, Any] = {"query": query, "metric_name": name, "metric": {kind: params},
                                "field": field, "runtime_mappings": runtime_mappings,
                                **grouping}  # fmt: skip
        if kind == "percentiles":
            p = params.get("percents")
            if not (isinstance(p, list) and len(p) == 1 and _number(p[0]) and 0 < p[0] < 100):
                raise _refuse(
                    f"{name}: percentiles needs exactly one percents value in (0, 100), e.g. "
                    "[99]; several percentiles are several queries"
                )
            return cls(form="percentile", q=float(p[0]) / 100, **base)
        if kind == "histogram":
            interval = params.get("interval")
            if not _number(interval) or interval <= 0:
                raise _refuse(f"{name}: histogram needs a positive numeric interval")
            if params.get("keyed") is True:
                raise _refuse(f"{name}: keyed histograms are not supported")
            offset = params.get("offset", 0)
            if not _number(offset):
                raise _refuse(f"{name}: histogram offset must be a number")
            return cls(form="histogram", interval=float(interval), offset=float(offset), **base)
        if kind == "range":
            return cls(form="range", edges=_range_edges(name, params), **base)
        return cls(form=_METRIC_FORMS[kind], **base)

    def with_group(self, fields: Sequence[str], size: int) -> EsQuery:
        """This query grouped by `fields` (query_distribution's `by`): one adapter terms level
        for a single field, or one composite level for 2+ fields."""
        fields = tuple(fields)
        if len(fields) == 1:
            group: dict[str, Any] = {"field": fields[0], "size": size}
            kind: GroupKind = "terms"
        else:
            group = {"sources": [{f: {"terms": {"field": f}}} for f in fields], "size": size}
            kind = "composite"
        return replace(
            self, group_name=GROUP_AGG, group=group, group_kind=kind, group_fields=fields
        )

    def fields(self) -> list[tuple[str, Need]]:
        """Every field an aggregation names, with what the field check requires of it. A field
        defined by `runtime_mappings` isn't a real mapped field, so it is exempt: the mapping
        check would otherwise refuse it as absent."""
        runtime = frozenset(self.runtime_mappings or {})
        out: list[tuple[str, Need]] = []
        if self.field is not None and self.field not in runtime:
            out.append((self.field, "aggregatable" if self.form == "field_rate" else "numeric"))
        out.extend((f, "aggregatable") for f in self.group_fields if f not in runtime)
        return out

    def body(self, rng: TimeRange, step_ms: int, time_field: str, timeout_s: float) -> dict:
        """The request sent: the caller's query and aggregation under a date_histogram of
        `step_ms` buckets whose END times run from rng.start_ms to rng.end_ms."""
        metrics: dict[str, Any] = {}
        if self.metric is not None and self.metric_name is not None:
            metrics[self.metric_name] = self.metric
        if self.form == "percentile":
            metrics[COUNT_AGG] = {"value_count": {"field": self.field}}
        inner: dict[str, Any] = metrics
        if self.group is not None and self.group_name is not None:
            grouping: dict[str, Any] = {self.group_kind or "terms": self.group}
            if metrics:
                grouping["aggs"] = metrics
            inner = {self.group_name: grouping}
        time_agg: dict[str, Any] = {
            "date_histogram": {
                "field": time_field,
                "fixed_interval": f"{step_ms}ms",
                "offset": f"+{rng.start_ms % step_ms}ms",
                "min_doc_count": 0,
            }
        }
        if inner:
            time_agg["aggs"] = inner
        window = {"gte": rng.start_ms - step_ms, "lt": rng.end_ms, "format": "epoch_millis"}
        req: dict[str, Any] = {
            "size": 0,
            "track_total_hits": False,
            "timeout": f"{round(timeout_s * 1000)}ms",
            "query": {"bool": {"filter": [self.query, {"range": {time_field: window}}]}},
            "aggs": {TIME_AGG: time_agg},
        }
        if self.runtime_mappings:
            req["runtime_mappings"] = self.runtime_mappings
        return req
