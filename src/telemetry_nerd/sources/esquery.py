"""Elasticsearch / OpenSearch `expr`: parse, validate, build the request (pure, no I/O).

`expr` is the caller's native request body: a `query` clause plus at most one metric
aggregation, optionally inside one `terms` grouping. Time never appears in it: the adapter wraps
the aggregation in a `date_histogram` built from the time range and query step, as the PromQL
adapter adds start/end/step. Design:
docs/superpowers/specs/2026-10-07-elasticsearch-opensearch-adapter-design.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Literal

from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.base import SourceError

Form = Literal["rate", "field_rate", "stats", "percentile", "histogram"]
Need = Literal["numeric", "aggregatable"]

RESERVED_PREFIX = "__tn_"
TIME_AGG = "__tn_time"  # the adapter's date_histogram
COUNT_AGG = "__tn_n"  # the adapter's value_count beside a percentiles aggregation
GROUP_AGG = "__tn_by"  # the adapter's terms built from query_distribution's `by`
_METRIC_FORMS: dict[str, Form] = {
    "value_count": "field_rate",
    "stats": "stats",
    "percentiles": "percentile",
    "histogram": "histogram",
}
_KINDS = frozenset({"terms", *_METRIC_FORMS})

FORMS_HINT = (
    'accepted: {"query": {...}} alone (documents per second); or "aggs" with ONE of '
    "value_count (a field's values per second), stats (mean/min/max/count of a numeric field), "
    'percentiles with exactly one "percents" value; optionally inside one terms aggregation '
    "(one series per term). histogram only in query_distribution. No time range or "
    "date_histogram in expr: time is start/end/step"
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


def _number(x: object) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool)


@dataclass(frozen=True)
class EsQuery:
    query: dict
    form: Form
    metric_name: str | None = None
    metric: dict | None = None  # {kind: params}, sent as written
    field: str | None = None
    group_name: str | None = None
    group: dict | None = None  # the terms params, sent as written
    group_field: str | None = None
    q: float | None = None  # percentile form: percents[0] / 100
    interval: float | None = None  # histogram form
    offset: float = 0.0  # histogram form

    @classmethod
    def parse(cls, expr: str) -> EsQuery:
        try:
            doc = json.loads(expr)
        except (TypeError, ValueError) as e:
            raise _refuse(f"expr is not JSON ({e})") from e
        if not isinstance(doc, dict):
            raise _refuse("expr must be a JSON object with query and/or aggs")
        extra = sorted(set(doc) - {"query", "aggs", "aggregations"})
        if extra:
            raise _refuse(f"unexpected keys {extra}: expr holds only query and aggs")
        if "aggs" in doc and "aggregations" in doc:
            raise _refuse("give aggs or aggregations, not both")
        query = doc.get("query", {"match_all": {}})
        if not isinstance(query, dict):
            raise _refuse("query must be a JSON object (Query DSL)")
        if "aggs" not in doc and "aggregations" not in doc:
            return cls(query=query, form="rate")
        name, body = _one(doc.get("aggs", doc.get("aggregations")))
        kind, params, sub = _agg(name, body)
        if kind != "terms":
            if sub is not None:
                raise _refuse(f"{name}: a {kind} aggregation takes no sub-aggregations")
            return cls._metric(query, name, kind, params, None)
        group = (name, params, _field(name, kind, params))
        if sub is None:
            return cls(query=query, form="rate", group_name=name, group=params,
                       group_field=group[2])  # fmt: skip
        mname, mbody = _one(sub)
        mkind, mparams, msub = _agg(mname, mbody)
        if mkind == "terms":
            raise _refuse(f"{mname}: one terms level only (multi-field grouping is later work)")
        if mkind == "histogram":
            raise _refuse(f"{mname}: group a histogram with query_distribution's by, not terms")
        if msub is not None:
            raise _refuse(f"{mname}: aggregations nest at most terms -> metric")
        return cls._metric(query, mname, mkind, mparams, group)

    @classmethod
    def _metric(
        cls, query: dict, name: str, kind: str, params: dict, group: tuple[str, dict, str] | None
    ) -> EsQuery:
        field = _field(name, kind, params)
        grouping = (
            {"group_name": group[0], "group": group[1], "group_field": group[2]} if group else {}
        )
        base: dict[str, Any] = {"query": query, "metric_name": name, "metric": {kind: params},
                                "field": field, **grouping}  # fmt: skip
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
        return cls(form=_METRIC_FORMS[kind], **base)

    def with_group(self, field: str, size: int) -> EsQuery:
        """This query grouped by `field` (query_distribution's `by`): one adapter terms level."""
        return replace(
            self, group_name=GROUP_AGG, group={"field": field, "size": size}, group_field=field
        )

    def fields(self) -> list[tuple[str, Need]]:
        """Every field an aggregation names, with what the field check requires of it."""
        out: list[tuple[str, Need]] = []
        if self.field is not None:
            out.append((self.field, "aggregatable" if self.form == "field_rate" else "numeric"))
        if self.group_field is not None:
            out.append((self.group_field, "aggregatable"))
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
            terms: dict[str, Any] = {"terms": self.group}
            if metrics:
                terms["aggs"] = metrics
            inner = {self.group_name: terms}
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
        return {
            "size": 0,
            "track_total_hits": False,
            "timeout": f"{round(timeout_s * 1000)}ms",
            "query": {"bool": {"filter": [self.query, {"range": {time_field: window}}]}},
            "aggs": {TIME_AGG: time_agg},
        }
