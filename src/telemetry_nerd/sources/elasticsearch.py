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
from collections.abc import Mapping
from typing import Any, Literal

import httpx

from telemetry_nerd.model.time import format_duration
from telemetry_nerd.sources.base import LimitExceeded, Limits, SourceError, SourceUnavailable
from telemetry_nerd.sources.gate import Gate
from telemetry_nerd.sources.promql import USER_AGENT
from telemetry_nerd.sources.spec import SourceSpec

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
        self._caps: dict[str, dict[str, dict]] = {}  # field -> {type: info}, until discover()

    @classmethod
    def from_spec(
        cls,
        spec: SourceSpec,
        environ: Mapping[str, str] = os.environ,
        client: httpx.AsyncClient | None = None,
    ) -> ElasticsearchSource:
        """Build a live source; resolves the secret reference now (raises MissingSecret)."""
        assert spec.index_pattern is not None and spec.time_field is not None  # spec validates
        return cls(
            spec.name,
            spec.url,
            index_pattern=spec.index_pattern,
            time_field=spec.time_field,
            flavor=spec.flavor,  # type: ignore[arg-type]
            resolution_ms=spec.resolution_ms,
            limits=Limits(timeout_s=spec.politeness.timeout_s),
            client=client,
            headers=spec.auth.headers(environ) if spec.auth else {},
            gate=Gate(spec.politeness.max_concurrency, spec.politeness.min_interval_ms),
        )

    async def aclose(self) -> None:
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
        try:
            async with self._gate.slot():
                resp = await self._client.request(
                    method, url, params=params, json=body, headers=self._headers,
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
