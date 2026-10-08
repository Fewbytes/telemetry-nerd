"""Elasticsearch / OpenSearch adapter: the caller's native Query DSL, bucketed by the adapter.

`expr` is an Elasticsearch request body (sources/esquery.py); the adapter adds the time range and a
date_histogram of query-step buckets, the way PromQLSource adds start/end/step. OpenSearch speaks
the same search API, so one adapter serves both flavors; `flavor` only changes how probe() reads
the version.

Query buckets carry their END time, but an Elasticsearch bucket is [t - step, t) where a
Prometheus one is (t - step, t]: a document stamped exactly on a boundary lands one bucket later
than a sample would. Design: docs/superpowers/specs/2026-10-07-elasticsearch-opensearch-adapter-design.md.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import httpx
import pyarrow as pa

from telemetry_nerd.model.discovery import Discovery, MetricInfo
from telemetry_nerd.model.distribution import COLUMN_SCHEMA, DIST_SCHEMA, BucketScheme, DistResult
from telemetry_nerd.model.series import (
    BUCKET_SCHEMA,
    SERIES_SCHEMA,
    FetchResult,
    labels_json,
    series_id,
)
from telemetry_nerd.model.time import TimeRange, format_duration
from telemetry_nerd.sources.base import LimitExceeded, Limits, SourceError, SourceUnavailable
from telemetry_nerd.sources.esquery import COUNT_AGG, TIME_AGG, EsQuery
from telemetry_nerd.sources.gate import Gate
from telemetry_nerd.sources.oauth import TokenProvider
from telemetry_nerd.sources.promql import MAX_STEPS_PER_QUERY, USER_AGENT
from telemetry_nerd.sources.spec import OAuthRef, SourceSpec

EsFlavor = Literal["elasticsearch", "opensearch"]
#: finest query step assumed when none is configured (documents have no series interval)
DEFAULT_RESOLUTION_MS = 1_000
_DEFAULT_LIMITS = Limits()
NUMERIC_TYPES = frozenset({"long", "integer", "short", "byte", "double", "float", "half_float",
                           "scaled_float", "unsigned_long"})  # fmt: skip
DATE_TYPES = frozenset({"date", "date_nanos"})
LABEL_TYPES = frozenset({"keyword", "constant_keyword", "ip", "boolean"})
MIN_VERSION: dict[str, tuple[int, int]] = {"elasticsearch": (7, 10), "opensearch": (1, 0)}


class _Forbidden(SourceError):
    """403: the credentials lack a privilege (probe() tolerates it on `/`)."""


def _malformed(message: str) -> SourceError:
    return SourceError(
        message,
        hint="retry; if it persists, check the url points at an Elasticsearch/OpenSearch cluster",
    )


def _no_index(pattern: str) -> SourceError:
    return SourceError(
        f"index pattern {pattern} matches no index",
        hint="check index_pattern (source_connect(..., index_pattern=..., replace=true))",
    )


_UNITS = {"micros": "us", "nanos": "ns", "byte": "B"}  # mapping meta.unit -> catalog unit


def _meta(info: dict, key: str) -> str | None:
    """A mapping `meta` value; _field_caps returns each as a list (merged across indices)."""
    v = (info.get("meta") or {}).get(key)
    if isinstance(v, list):
        v = v[0] if v else None
    return str(v) if v else None


def _field_unit(info: dict) -> str | None:
    u = _meta(info, "unit")
    return _UNITS.get(u, u) if u else None


def _field_type(info: dict) -> str | None:
    t = info.get("time_series_metric") or _meta(info, "metric_type")
    return t if t in ("gauge", "counter") else None


def _causes(err: object) -> list[tuple[str, str]]:
    """(type, reason) of an Elasticsearch error: root causes first, then the error itself and its
    caused_by chain."""
    out: list[tuple[str, str]] = []

    def walk(e: object) -> None:
        if not isinstance(e, dict):
            return
        for rc in e.get("root_cause") or []:
            walk(rc)
        if "type" in e:
            out.append((str(e["type"]), str(e.get("reason", ""))))
        walk(e.get("caused_by"))

    walk(err)
    return out


def _raise_for(status: int, body: dict, pattern: str) -> None:
    """Map an Elasticsearch error response to the typed source errors (spec: Errors)."""
    causes = _causes(body.get("error"))
    if not causes and isinstance(body.get("error"), str):
        causes = [("error", body["error"])]
    kinds = {t for t, _ in causes}
    first = f"{causes[0][0]}: {causes[0][1]}" if causes else f"HTTP {status}"
    if "too_many_buckets_exception" in kinds:
        raise LimitExceeded(
            f"too many buckets: {first}",
            hint="use a coarser step, a shorter range, a larger histogram interval or fewer groups",
        )
    if "circuit_breaking_exception" in kinds:
        raise LimitExceeded(
            f"the query needs too much memory: {first}",
            hint="narrow the query: shorter range, coarser step, fewer groups",
        )
    if status == 403:
        raise _Forbidden(
            f"permission denied: {first}",
            hint=f"the credentials need read and view_index_metadata on {pattern}",
        )
    if "index_not_found_exception" in kinds:
        raise _no_index(pattern)
    if status == 429 or status >= 500:
        raise SourceUnavailable(
            f"source returned HTTP {status}: {first}",
            hint="the cluster is overloaded or failing; retry shortly or narrow the query",
        )
    if any("fielddata" in reason for _, reason in causes):
        raise SourceError(
            f"cannot aggregate a text field: {first}",
            hint="aggregate its keyword sub-field instead (e.g. message.keyword); source_learn "
            "lists the aggregatable fields",
        )
    raise SourceError(
        f"query failed: {first}",
        hint="check the Query DSL / Lucene syntax and the field names; source_learn lists the fields",
    )


Cell = tuple[float, float, float, int]  # avg, min, max, count
Failed = tuple[tuple[int, int, str], ...]


def _time_buckets(resp: dict) -> list[dict]:
    buckets = ((resp.get("aggregations") or {}).get(TIME_AGG) or {}).get("buckets")
    if not isinstance(buckets, list):
        raise _malformed("response has no time buckets (aggregations.__tn_time.buckets)")
    return buckets


def _interior(buckets: list[dict]) -> list[dict]:
    """First..last non-empty time bucket: before the first document is the retention edge and
    after the last the future, unknown, never zero (min_doc_count 0 fills only between them)."""
    full = [i for i, b in enumerate(buckets) if (b.get("doc_count") or 0) > 0]
    return buckets[full[0] : full[-1] + 1] if full else []


def _bucket_ts(b: dict, step_ms: int) -> int:
    try:
        return int(b["key"]) + step_ms  # keys are bucket starts; a query bucket carries its end
    except (KeyError, TypeError, ValueError) as e:
        raise _malformed(f"malformed time bucket {b!r}") from e


def _groups(q: EsQuery, b: dict) -> Iterator[tuple[dict[str, str], dict]]:
    """(labels, the bucket holding the metric) per series in one time bucket."""
    if q.group_name is None or q.group_field is None:
        yield {}, b
        return
    agg = b.get(q.group_name)
    if not isinstance(agg, dict) or not isinstance(agg.get("buckets"), list):
        raise _malformed(f"time bucket without its terms aggregation {q.group_name!r}")
    other = agg.get("sum_other_doc_count") or 0
    if other > 0:
        size = (q.group or {}).get("size", 10)
        raise LimitExceeded(
            f"terms on {q.group_field} returned only the top {size} terms in a query bucket "
            f"({other} documents fell in other terms)",
            hint="raise the terms size (up to the 500-series limit) or narrow the query",
        )
    for tb in agg["buckets"]:
        key = tb.get("key_as_string", tb.get("key"))
        yield {q.group_field: str(key)}, tb


def _percentile_value(agg: dict) -> float | None:
    values = agg.get("values")
    if isinstance(values, dict):
        v = next(iter(values.values()), None)
    elif isinstance(values, list) and values:
        v = values[0].get("value")
    else:
        v = None
    return None if v is None else float(v)


def _cell(q: EsQuery, parent: dict, step_s: float) -> Cell | None:
    """One query bucket's value of the form, or None when it holds no observation."""
    try:
        if q.form == "rate":
            v = float(parent["doc_count"]) / step_s
            return v, v, v, 1
        agg = parent[q.metric_name]
        if q.form == "field_rate":
            v = float(agg.get("value") or 0) / step_s
            return v, v, v, 1
        if q.form == "stats":
            n = int(agg.get("count") or 0)
            if n == 0:
                return None
            return float(agg["avg"]), float(agg["min"]), float(agg["max"]), n
        n = int((parent.get(COUNT_AGG) or {}).get("value") or 0)
        v = _percentile_value(agg)
        if n == 0 or v is None:
            return None
        return v, v, v, n
    except (KeyError, TypeError, ValueError) as e:
        raise _malformed(f"malformed {q.form} bucket {parent!r}") from e


class ElasticsearchSource:
    query_language: Literal["es_dsl"] = "es_dsl"
    #: no verified missing-data profile yet (spec: later work)
    semantics = None

    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        index_pattern: str,
        time_field: str,
        flavor: EsFlavor = "elasticsearch",
        resolution_ms: int | None = None,
        limits: Limits = _DEFAULT_LIMITS,
        client: httpx.AsyncClient | None = None,
        headers: Mapping[str, str] | None = None,
        gate: Gate | None = None,
        token_provider: TokenProvider | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.flavor = flavor
        self.index_pattern = index_pattern
        self.time_field = time_field
        #: the finest query step the source accepts; never learned
        self.resolution_ms = resolution_ms or DEFAULT_RESOLUTION_MS
        self.resolution_origin: Literal["configured", "assumed"] = (
            "configured" if resolution_ms else "assumed"
        )
        self.limits = limits
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient()
        self._headers = {"User-Agent": USER_AGENT, **(headers or {})}
        self._gate = gate or Gate()
        self._token_provider = token_provider
        self._caps: dict[str, dict[str, dict]] = {}  # field -> {type: info}, until discover()

    @classmethod
    def from_spec(
        cls,
        spec: SourceSpec,
        environ: Mapping[str, str] = os.environ,
        client: httpx.AsyncClient | None = None,
        data_dir: Path | None = None,
    ) -> ElasticsearchSource:
        """Build a live source; resolves the secret reference now (raises MissingSecret)."""
        assert spec.index_pattern is not None and spec.time_field is not None  # spec validates
        token_provider = None
        headers: Mapping[str, str] = {}
        if isinstance(spec.auth, OAuthRef):
            if data_dir is None:
                raise ValueError("OAuth sources need data_dir")
            token_provider = TokenProvider(
                spec.auth, spec.name, spec.url, data_dir, client=client, environ=dict(environ)
            )
        elif spec.auth is not None:
            headers = spec.auth.headers(environ)
        return cls(
            spec.name,
            spec.url,
            index_pattern=spec.index_pattern,
            time_field=spec.time_field,
            flavor=spec.flavor,  # type: ignore[arg-type]
            resolution_ms=spec.resolution_ms,
            limits=Limits(timeout_s=spec.politeness.timeout_s),
            client=client,
            headers=headers,
            gate=Gate(spec.politeness.max_concurrency, spec.politeness.min_interval_ms),
            token_provider=token_provider,
        )

    async def aclose(self) -> None:
        if self._token_provider is not None:
            await self._token_provider.aclose()
        if self._owns_client:
            await self._client.aclose()

    @property
    def identity(self) -> str:
        # the cache key changes when the index pattern or the time field does
        return (
            f"{self.flavor}|{self.base_url}|{self.index_pattern}|{self.time_field}|"
            f"{self.resolution_ms}"
        )

    def resolution_info(self) -> dict:
        note = "documents have no series interval: this is the finest query step the source accepts"
        if self.resolution_origin == "assumed":
            note += (
                f" (assumed {format_duration(DEFAULT_RESOLUTION_MS)}; connect with "
                "resolution=... to change it)"
            )
        return {
            "resolution": format_duration(self.resolution_ms),
            "origin": self.resolution_origin,
            "note": note,
        }

    async def scrape_interval(self, selector: str, at_ms: int | None = None) -> int | None:
        return None  # documents are events, not samples of a series

    async def _attempt(
        self,
        method: str,
        url: str,
        params: Mapping[str, str] | None,
        body: dict | None,
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> httpx.Response:
        try:
            async with self._gate.slot():
                return await self._client.request(
                    method, url, params=params, json=body, headers=headers,
                    timeout=timeout_s,
                )  # fmt: skip
        except httpx.TimeoutException as e:
            raise SourceUnavailable(
                f"query timed out after {timeout_s:g}s",
                hint="narrow the query, shorten the range, or use a coarser step",
            ) from e
        except httpx.HTTPError as e:
            raise SourceUnavailable(
                f"cannot reach {self.base_url}: {e}",
                hint="check the source url and that the cluster is up",
            ) from e

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: dict | None = None,
        timeout_s: float | None = None,
    ) -> dict:
        url = f"{self.base_url}{path}"
        timeout_s = timeout_s or self.limits.timeout_s
        headers = self._headers
        if self._token_provider is not None:
            headers = {**headers, **(await self._token_provider.headers())}
        resp = await self._attempt(method, url, params, body, headers, timeout_s)
        if resp.status_code == 401 and self._token_provider is not None:
            rejected = headers.get("Authorization", "").removeprefix("Bearer ")
            if await self._token_provider.on_401(rejected):
                headers = {**self._headers, **(await self._token_provider.headers())}
                resp = await self._attempt(method, url, params, body, headers, timeout_s)
        if resp.status_code == 401 and self._token_provider is not None:
            raise SourceError(
                f"authentication failed (HTTP 401) querying {self.base_url}",
                hint="re-run source_connect to log in again",
            )
        if resp.status_code == 401:
            raise SourceError(
                "authentication failed",
                hint="check the secret reference and auth_scheme: apikey for Elasticsearch API "
                "keys, bearer for service-account tokens and JWTs, basic for user:pass",
            )
        try:
            data = resp.json()
        except ValueError as e:
            if resp.status_code == 429 or resp.status_code >= 500:
                raise SourceUnavailable(
                    f"source returned HTTP {resp.status_code}",
                    hint="the cluster is overloaded or failing; retry shortly",
                ) from e
            raise SourceUnavailable(
                f"non-JSON response from source (HTTP {resp.status_code})",
                hint="check the url points at an Elasticsearch/OpenSearch cluster, not a proxy "
                "or login page",
            ) from e
        if not isinstance(data, dict):
            raise _malformed(f"unexpected response body {type(data).__name__}")
        if resp.status_code >= 400:
            _raise_for(resp.status_code, data, self.index_pattern)
        return data

    async def probe(self) -> dict:
        """Version (flavor-aware) and the time field: `GET /`, then `_field_caps` of time_field."""
        t0 = time.monotonic()
        out: dict[str, Any] = {"reachable": True}
        try:
            root = await self._request("GET", "/")
        except _Forbidden:
            out["version_unavailable"] = "403 on / (no monitor privilege): version not checked"
        else:
            out |= self._version(root)
        caps = await self._request(
            "GET", f"/{self.index_pattern}/_field_caps", params={"fields": self.time_field}
        )
        indices = caps.get("indices")
        if isinstance(indices, list) and not indices:
            raise _no_index(self.index_pattern)
        fields = caps.get("fields") if isinstance(caps.get("fields"), dict) else {}
        types = set(fields.get(self.time_field) or {}) - {"unmapped"}
        if not types or not types <= DATE_TYPES:
            what = (
                "is not in the mapping"
                if not types
                else f"is {', '.join(sorted(types))}, not a date"
            )
            dates = await self._date_fields()
            raise SourceError(
                f"time field {self.time_field} {what} (index pattern {self.index_pattern}; "
                f"date fields: {', '.join(dates) or 'none'})",
                hint="reconnect with time_field set to one of the date fields",
            )
        out |= {
            "latency_ms": round((time.monotonic() - t0) * 1000),
            "index_pattern": self.index_pattern,
            "time_field": self.time_field,
        }
        if isinstance(indices, list):
            out["indices"] = len(indices)
        return out

    def _version(self, root: dict) -> dict:
        v = root.get("version") if isinstance(root.get("version"), dict) else {}
        number = str(v.get("number", ""))
        found: EsFlavor = "opensearch" if v.get("distribution") == "opensearch" else "elasticsearch"
        try:
            major, minor = (int(x) for x in number.split(".")[:2])
        except ValueError:
            return {"distribution": found, "version": number or None}
        if (major, minor) < MIN_VERSION[found]:
            need = ".".join(str(x) for x in MIN_VERSION[found])
            raise SourceError(
                f"{found} {number} is not supported (needs >= {need})",
                hint="supported: Elasticsearch >= 7.10, OpenSearch >= 1.0",
            )
        out: dict[str, Any] = {"distribution": found, "version": number}
        if found != self.flavor:
            out["flavor_mismatch"] = (
                f"connected as {self.flavor} but the cluster reports {found} (the search API is "
                f"the same; reconnect with flavor={found!r} to silence this)"
            )
        return out

    async def _date_fields(self) -> list[str]:
        caps = await self._request(
            "GET", f"/{self.index_pattern}/_field_caps", params={"fields": "*"}
        )
        fields = caps.get("fields") if isinstance(caps.get("fields"), dict) else {}
        return sorted(f for f, t in fields.items() if not f.startswith("_") and set(t) & DATE_TYPES)

    async def discover(self) -> Discovery:
        """One _field_caps call: numeric aggregatable fields fill the metric role, aggregatable
        keyword/ip/boolean fields are the candidate `by` / terms fields."""
        self._caps.clear()  # field checks re-read the mapping after a discover
        caps = await self._request(
            "GET", f"/{self.index_pattern}/_field_caps",
            params={"fields": "*", "include_unmapped": "false"},
            timeout_s=self.limits.discover_timeout_s,
        )  # fmt: skip
        fields = caps.get("fields")
        if not isinstance(fields, dict):
            raise _malformed("field_caps response has no fields")
        nested = [f for f, t in fields.items() if isinstance(t, dict) and "nested" in t]
        numeric: list[MetricInfo] = []
        labels: list[str] = []
        conflicts = nested_skipped = histogram_skipped = 0
        for name, types in sorted(fields.items()):
            if name.startswith("_") or not isinstance(types, dict):
                continue  # metadata fields (_id, _index, ...)
            kinds = set(types) - {"object", "nested"}
            if not kinds:
                continue
            if any(name.startswith(p + ".") for p in nested):
                nested_skipped += 1  # needs a nested aggregation (not a v1 form)
                continue
            if len(kinds) > 1:
                conflicts += 1  # mapped with different types across the pattern's indices
                continue
            [kind] = kinds
            info = types[kind]
            if kind == "histogram":
                histogram_skipped += 1  # pre-aggregated histogram field: later work
                continue
            if not info.get("aggregatable"):
                continue
            if kind in NUMERIC_TYPES:
                numeric.append(MetricInfo(name, _field_type(info), None, _field_unit(info)))  # type: ignore[arg-type]
            elif kind in LABEL_TYPES:
                labels.append(name)
        caveats = ["cardinality_unavailable"]
        partial = False
        for count, code in ((conflicts, "mapping_conflict"), (nested_skipped, "nested_fields_skipped"),
                            (histogram_skipped, "histogram_fields_skipped")):  # fmt: skip
            if count:
                caveats.append(f"{code}:{count}")
                partial = True
        coverage = sum(1 for m in numeric if m.unit or m.type) / len(numeric) if numeric else 1.0
        if len(numeric) > self.limits.max_metrics:
            caveats.append(f"metrics_truncated:{self.limits.max_metrics}/{len(numeric)}")
            numeric = numeric[: self.limits.max_metrics]
            partial = True
        if not numeric:
            caveats.append("no_numeric_fields")  # not an error: the rate form needs no field
        return Discovery(
            metrics=tuple(numeric),
            label_names=tuple(labels),
            histograms={},
            cardinality=None,
            metadata_coverage=coverage,
            caveats=tuple(caveats),
            partial=partial,
            naming="fields",
        )

    async def fetch(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult:
        q = EsQuery.parse(expr)
        if q.form == "percentile":
            raise SourceError(
                "a percentile is read per query bucket, never rolled up",
                hint="query() routes a percentiles aggregation to fetch_values",
            )
        return await self._fetch(q, rng, step_ms)

    async def fetch_values(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult:
        """Every non-histogram form, one value per query bucket (percentiles included)."""
        return await self._fetch(EsQuery.parse(expr), rng, step_ms)

    async def fetch_histogram(
        self, selector: str, by: Sequence[str], rng: TimeRange, step_ms: int
    ) -> DistResult:
        """Document counts per value bucket per query bucket. The value buckets are chosen by the
        query (interval, offset), not by the source: a finer interval can always be asked for."""
        q = EsQuery.parse(selector)
        if q.form != "histogram":
            raise SourceError(
                "query_distribution needs a histogram aggregation",
                hint='selector = {"query": {...}, "aggs": {"lat": {"histogram": {"field": '
                '"<numeric field>", "interval": 25}}}}; group with by',
            )
        if len(by) > 1:
            raise SourceError(
                f"by takes at most one field on an Elasticsearch source, got {list(by)}",
                hint="one terms level in v1 (multi-field grouping is later work)",
            )
        if by:
            q = q.with_group(by[0], self.limits.max_series + 1)
        resp, failed = await self._search(q, rng, step_ms)
        width = float(q.interval or 0)
        rows: list[tuple[int, str, float, float, float]] = []
        cols: dict[tuple[str, int], float] = {}
        labels_by_sid: dict[str, dict[str, str]] = {}
        interior = _interior(_time_buckets(resp))
        for b in interior:
            ts = _bucket_ts(b, step_ms)
            for labels, parent in _groups(q, b):
                sid = series_id(self.name, labels)
                labels_by_sid[sid] = labels
                leaves = (parent.get(q.metric_name) or {}).get("buckets")
                if not isinstance(leaves, list):
                    raise _malformed(f"bucket without its histogram {q.metric_name!r}")
                n = 0.0
                for leaf in leaves:
                    c = float(leaf.get("doc_count") or 0)
                    if c > 0:
                        lo = float(leaf["key"])
                        rows.append((ts, sid, lo, lo + width, c))
                        n += c
                cols[(sid, ts)] = n
        if len(labels_by_sid) > self.limits.max_series:
            raise LimitExceeded(
                f"histogram has {len(labels_by_sid)} series (limit {self.limits.max_series})",
                hint="group by a field with fewer values (by=[...]) or narrow the query",
            )
        for sid in labels_by_sid:  # a group absent from an interior query bucket: zero documents
            for b in interior:
                cols.setdefault((sid, _bucket_ts(b, step_ms)), 0.0)
        if len(rows) > self.limits.max_points:
            raise LimitExceeded(
                f"histogram has {len(rows)} non-empty cells (limit {self.limits.max_points})",
                hint="use a coarser step, a shorter range or a larger interval",
            )
        rows.sort(key=lambda r: (r[1], r[0], r[2]))
        ckeys = sorted(cols)
        sids = sorted(labels_by_sid)
        return DistResult(
            rows=pa.table(
                {
                    "ts_ms": [r[0] for r in rows],
                    "series_id": [r[1] for r in rows],
                    "bucket_lo": [r[2] for r in rows],
                    "bucket_hi": [r[3] for r in rows],
                    "count": [r[4] for r in rows],
                },
                schema=DIST_SCHEMA,
            ),
            columns=pa.table(
                {
                    "ts_ms": [ts for _, ts in ckeys],
                    "series_id": [sid for sid, _ in ckeys],
                    "n": [cols[k] for k in ckeys],
                },
                schema=COLUMN_SCHEMA,
            ),
            series=pa.table(
                {"series_id": sids, "labels": [labels_json(labels_by_sid[s]) for s in sids]},
                schema=SERIES_SCHEMA,
            ),
            scheme=BucketScheme("linear", width=width, offset=q.offset),
            expr=selector,
            caveats=("query_chosen_buckets",),
            failed=failed,
        )

    async def _fetch(self, q: EsQuery, rng: TimeRange, step_ms: int) -> FetchResult:
        if q.form == "histogram":
            raise SourceError(
                "a histogram aggregation is a distribution, not a time series",
                hint="use query_distribution(selector=<this expr>, source=...) for counts per "
                "value bucket",
            )
        resp, failed = await self._search(q, rng, step_ms)
        step_s = step_ms / 1000
        cells: dict[tuple[str, int], Cell] = {}
        labels_by_sid: dict[str, dict[str, str]] = {}
        interior = _interior(_time_buckets(resp))
        for b in interior:
            ts = _bucket_ts(b, step_ms)
            for labels, parent in _groups(q, b):
                sid = series_id(self.name, labels)
                labels_by_sid[sid] = labels
                if (cell := _cell(q, parent, step_s)) is not None:
                    cells[(sid, ts)] = cell
        if len(labels_by_sid) > self.limits.max_series:
            raise LimitExceeded(
                f"query returned {len(labels_by_sid)} series (limit {self.limits.max_series})",
                hint="group by a field with fewer values, or narrow the query",
            )
        if q.form in ("rate", "field_rate") and q.group is not None:
            # a term absent from an interior query bucket had no matching documents there
            for sid in labels_by_sid:
                for b in interior:
                    cells.setdefault((sid, _bucket_ts(b, step_ms)), (0.0, 0.0, 0.0, 1))
        keys = sorted(cells)
        sids = sorted({sid for sid, _ in keys})
        buckets = pa.table(
            {
                "ts_ms": [ts for _, ts in keys],
                "series_id": [sid for sid, _ in keys],
                "avg": [cells[k][0] for k in keys],
                "min": [cells[k][1] for k in keys],
                "max": [cells[k][2] for k in keys],
                "count": [cells[k][3] for k in keys],
            },
            schema=BUCKET_SCHEMA,
        )
        series = pa.table(
            {"series_id": sids, "labels": [labels_json(labels_by_sid[s]) for s in sids]},
            schema=SERIES_SCHEMA,
        )
        return FetchResult(buckets, series, failed=failed)

    async def _search(self, q: EsQuery, rng: TimeRange, step_ms: int) -> tuple[dict, Failed]:
        steps = (rng.end_ms - rng.start_ms) // step_ms + 1
        if steps > MAX_STEPS_PER_QUERY:
            raise LimitExceeded(
                f"{steps} steps exceeds {MAX_STEPS_PER_QUERY} per query",
                hint="use a coarser step or a shorter range",
            )
        await self._check_fields(q)
        body = q.body(rng, step_ms, self.time_field, self.limits.timeout_s)
        resp = await self._request("POST", f"/{self.index_pattern}/_search", body=body)
        shards = resp.get("_shards") if isinstance(resp.get("_shards"), dict) else {}
        if shards.get("total") == 0:  # a wildcard that matches nothing answers 200
            raise _no_index(self.index_pattern)
        reasons: list[str] = []
        if resp.get("timed_out") is True:
            reasons.append(
                f"the cluster timed out after {self.limits.timeout_s:g}s and returned partial results"
            )
        if (shards.get("failed") or 0) > 0:
            fails = shards.get("failures") or []
            why = _causes(fails[0].get("reason")) if fails and isinstance(fails[0], dict) else []
            reasons.append(
                f"{shards['failed']} of {shards.get('total')} shards failed"
                + (f" ({why[0][0]}: {why[0][1]})" if why else "")
            )
        failed: Failed = (
            ((rng.start_ms, rng.end_ms, "PartialResponse: " + "; ".join(reasons)),)
            if reasons
            else ()
        )
        return resp, failed

    async def _field_types(self, field: str) -> dict[str, dict]:
        if field not in self._caps:
            caps = await self._request(
                "GET", f"/{self.index_pattern}/_field_caps", params={"fields": field}
            )
            if isinstance(caps.get("indices"), list) and not caps["indices"]:
                raise _no_index(self.index_pattern)
            fields = caps.get("fields") if isinstance(caps.get("fields"), dict) else {}
            types = fields.get(field) or {}
            self._caps[field] = {t: i for t, i in types.items() if t != "unmapped"}
        return self._caps[field]

    async def _check_fields(self, q: EsQuery) -> None:
        """Elasticsearch answers an aggregation on an unmapped field with empty results, which
        would read as "no data": every aggregated field is checked against the mapping first."""
        for field, need in q.fields():
            types = await self._field_types(field)
            if not types:
                raise SourceError(
                    f"field {field} is not in the mapping of {self.index_pattern}",
                    hint="source_learn lists the fields (then catalog_search)",
                )
            if len(types) > 1:
                raise SourceError(
                    f"field {field} is mapped as {', '.join(sorted(types))} across the indices "
                    f"of {self.index_pattern}",
                    hint="narrow index_pattern to indices that agree, or aggregate another field",
                )
            [(kind, info)] = types.items()
            if need == "numeric" and kind not in NUMERIC_TYPES:
                raise SourceError(
                    f"field {field} is {kind}, not numeric",
                    hint="stats, percentiles and histogram need a numeric field; source_learn "
                    "lists the numeric fields as metrics",
                )
            if need == "aggregatable" and not info.get("aggregatable"):
                raise SourceError(
                    f"field {field} ({kind}) is not aggregatable",
                    hint=f"use its keyword sub-field, e.g. {field}.keyword",
                )
