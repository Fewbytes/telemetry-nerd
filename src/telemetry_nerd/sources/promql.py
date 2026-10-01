"""PromQL / MetricsQL adapter returning min/max/avg/count buckets (never point samples)."""

from __future__ import annotations

import asyncio
import math
import os
import re
import time
from collections.abc import Mapping, Sequence
from importlib.metadata import PackageNotFoundError, version
from typing import Literal

import httpx
import pyarrow as pa

from telemetry_nerd.analysis.histogram import from_matrix, histogram_expr
from telemetry_nerd.model.distribution import DistResult
from telemetry_nerd.model.series import (
    BUCKET_SCHEMA,
    SERIES_SCHEMA,
    FetchResult,
    labels_json,
    series_id,
)
from telemetry_nerd.model.time import TimeRange, format_duration
from telemetry_nerd.sources.base import LimitExceeded, Limits, SourceError, SourceUnavailable
from telemetry_nerd.sources.gate import Gate
from telemetry_nerd.sources.spec import SourceSpec

MAX_STEPS_PER_QUERY = 11_000
_SELECTOR = re.compile(r"^\s*[a-zA-Z_:][a-zA-Z0-9_:]*\s*(\{[^{}]*\})?\s*$")
_DEFAULT_LIMITS = Limits()
_FIELDS = ("avg", "min", "max", "count")


def _user_agent() -> str:
    try:
        ver = version("telemetry-nerd")
    except PackageNotFoundError:
        ver = "dev"
    return f"telemetry-nerd/{ver} (+https://github.com/Fewbytes/telemtry-nerd)"


USER_AGENT = _user_agent()


_HIST_HINT = (
    "select the histogram itself: x_bucket{...} for classic (le) or VictoriaMetrics "
    "(vmrange) histograms, or the native histogram metric x{...}; group with `by`"
)


def _native_histograms() -> SourceError:
    return SourceError(
        "the expression returns native histograms, not numbers",
        hint=(
            "use query_distribution(selector=...) for the distribution, or wrap it in "
            "histogram_count / histogram_sum / histogram_fraction / histogram_quantile"
        ),
    )


def _malformed(message: str) -> SourceError:
    return SourceError(
        message, hint="retry the query; if it persists, check the source version and URL"
    )


def is_selector(expr: str) -> bool:
    return bool(_SELECTOR.match(expr))


class PromQLSource:
    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        flavor: Literal["victoriametrics", "prometheus"] = "victoriametrics",
        resolution_ms: int = 15_000,
        limits: Limits = _DEFAULT_LIMITS,
        client: httpx.AsyncClient | None = None,
        headers: Mapping[str, str] | None = None,
        gate: Gate | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.flavor = flavor
        self.resolution_ms = resolution_ms
        self.limits = limits
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient()
        self._headers = {"User-Agent": USER_AGENT, **(headers or {})}
        self._gate = gate or Gate()

    @classmethod
    def from_spec(
        cls,
        spec: SourceSpec,
        environ: Mapping[str, str] = os.environ,
        client: httpx.AsyncClient | None = None,
    ) -> PromQLSource:
        """Build a live source; resolves the secret reference now (raises MissingSecret)."""
        headers = spec.auth.headers(environ) if spec.auth else {}
        return cls(
            spec.name,
            spec.url,
            flavor=spec.flavor,
            resolution_ms=spec.resolution_ms,
            limits=Limits(timeout_s=spec.politeness.timeout_s),
            client=client,
            headers=headers,
            gate=Gate(spec.politeness.max_concurrency, spec.politeness.min_interval_ms),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def identity(self) -> str:
        return f"{self.flavor}|{self.base_url}|{self.resolution_ms}"

    def _window(self, expr: str, step_ms: int) -> str:
        window = format_duration(step_ms)
        if is_selector(expr):
            return f"{expr.strip()}[{window}]"
        return f"({expr.strip()})[{window}:{format_duration(self.resolution_ms)}]"

    def build_queries(self, expr: str, step_ms: int) -> dict[str, str]:
        win = self._window(expr, step_ms)
        if self.flavor == "victoriametrics":
            return {"rollup": f"rollup({win})", "count": f"count_over_time({win})"}
        return {field: f"{field}_over_time({win})" for field in _FIELDS}

    async def fetch(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult:
        steps = (rng.end_ms - rng.start_ms) // step_ms + 1
        if steps > MAX_STEPS_PER_QUERY:
            raise LimitExceeded(
                f"{steps} steps exceeds {MAX_STEPS_PER_QUERY} per query",
                hint="use a coarser step or a shorter range",
            )
        queries = self.build_queries(expr, step_ms)
        results = await asyncio.gather(
            *(self._query_range(q, rng, step_ms) for q in queries.values())
        )
        cells: dict[tuple[str, int], dict[str, float | None]] = {}
        labels_by_sid: dict[str, dict[str, str]] = {}
        for query_field, result in zip(queries, results, strict=True):
            for item in result:
                if isinstance(item, dict) and "histograms" in item:
                    raise _native_histograms()
                try:
                    labels = dict(item["metric"])
                    samples = list(item["values"])
                except (KeyError, TypeError, ValueError) as e:
                    raise _malformed(f"malformed series {item!r} in query result") from e
                field = labels.pop("rollup", None) if query_field == "rollup" else query_field
                # VictoriaMetrics semantics: MetricsQL rollup() keeps __name__ (plus the
                # rollup label) while *_over_time() drops it, per Prometheus function
                # rules. Series identity is the label set without __name__: the metric
                # name is the expression being queried, and keeping it would split
                # avg/min/max and count of the same series across different buckets.
                labels.pop("__name__", None)
                if field not in _FIELDS:
                    continue
                sid = series_id(self.name, labels)
                labels_by_sid[sid] = labels
                for sample in samples:
                    try:
                        ts, value = float(sample[0]), float(sample[1])
                    except (IndexError, TypeError, ValueError) as e:
                        raise _malformed(f"malformed sample {sample!r} in query result") from e
                    # NaN / +-Inf cannot be represented in JSON (or averaged): store null
                    # and keep the bucket's count; summaries flag it as `non_finite`.
                    cells.setdefault((sid, round(ts * 1000)), {})[field] = (
                        value if math.isfinite(value) else None
                    )
        if len(labels_by_sid) > self.limits.max_series:
            raise LimitExceeded(
                f"query returned {len(labels_by_sid)} series (limit {self.limits.max_series})",
                hint="narrow the selector with label filters or aggregate, e.g. sum by (service) (...)",
            )
        if len(cells) > self.limits.max_points:
            raise LimitExceeded(
                f"query returned {len(cells)} buckets (limit {self.limits.max_points})",
                hint="use a coarser step, a shorter range, or narrow the selector",
            )
        # A bucket needs all of avg/min/max/count from the source; a half-returned cell
        # would otherwise become a bucket with data but no mean. Drop it, count it.
        complete = {k: c for k, c in cells.items() if all(f in c for f in _FIELDS)}
        complete = {k: c for k, c in complete.items() if c["count"] is not None}
        partial = len(cells) - len(complete)
        cells = complete
        keys = sorted(cells)
        buckets = pa.table(
            {
                "ts_ms": [ts for _, ts in keys],
                "series_id": [sid for sid, _ in keys],
                "avg": [cells[k].get("avg") for k in keys],
                "min": [cells[k].get("min") for k in keys],
                "max": [cells[k].get("max") for k in keys],
                # counts are integral by construction (count_over_time); round() is a
                # no-op cast float -> int that cannot raise on malformed data
                "count": [round(cells[k]["count"]) for k in keys],
            },
            schema=BUCKET_SCHEMA,
        )
        sids = sorted(labels_by_sid)
        series = pa.table(
            {"series_id": sids, "labels": [labels_json(labels_by_sid[s]) for s in sids]},
            schema=SERIES_SCHEMA,
        )
        return FetchResult(buckets, series, partial=partial)

    async def _get_json(self, path: str, params: dict[str, str]) -> dict:
        url = f"{self.base_url}{path}"
        try:
            async with self._gate.slot():
                resp = await self._client.get(
                    url, params=params, headers=self._headers, timeout=self.limits.timeout_s
                )
        except httpx.TimeoutException as e:
            raise SourceUnavailable(
                f"query timed out after {self.limits.timeout_s}s",
                hint="narrow the selector, shorten the range, or use a coarser step",
            ) from e
        except httpx.HTTPError as e:
            raise SourceUnavailable(
                f"cannot reach {self.base_url}: {e}",
                hint="check the source URL and that the server is running",
            ) from e
        if resp.status_code == 429 or resp.status_code >= 500:
            raise SourceUnavailable(
                f"source returned HTTP {resp.status_code}",
                hint="the source is overloaded or failing; retry shortly or narrow the query",
            )
        try:
            body = resp.json()
        except ValueError as e:
            raise SourceUnavailable(
                f"non-JSON response from source (HTTP {resp.status_code})",
                hint="check the source URL points at a Prometheus-compatible API, not a proxy page",
            ) from e
        if not isinstance(body, dict):
            raise _malformed(f"unexpected response body {type(body).__name__}")
        if body.get("status") != "success":
            raise SourceError(
                f"query failed: {body.get('error', f'HTTP {resp.status_code}')}",
                hint="check PromQL/MetricsQL syntax and metric names",
            )
        return body

    async def fetch_values(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult:
        """The expression's own value at each step: no rollup, no *_over_time.
        For quantiles and other non-additive values that must never be re-aggregated."""
        steps = (rng.end_ms - rng.start_ms) // step_ms + 1
        if steps > MAX_STEPS_PER_QUERY:
            raise LimitExceeded(
                f"{steps} steps exceeds {MAX_STEPS_PER_QUERY} per query",
                hint="use a coarser step or a shorter range",
            )
        result = await self._query_range(expr.strip(), rng, step_ms)
        rows: list[tuple[int, str, float | None]] = []
        labels_by_sid: dict[str, dict[str, str]] = {}
        for item in result:
            if isinstance(item, dict) and "histograms" in item:
                raise _native_histograms()
            try:
                labels = dict(item["metric"])
                samples = list(item["values"])
            except (KeyError, TypeError, ValueError) as e:
                raise _malformed(f"malformed series {item!r} in query result") from e
            labels.pop("__name__", None)
            sid = series_id(self.name, labels)
            labels_by_sid[sid] = labels
            for sample in samples:
                try:
                    ts, value = float(sample[0]), float(sample[1])
                except (IndexError, TypeError, ValueError) as e:
                    raise _malformed(f"malformed sample {sample!r} in query result") from e
                rows.append((round(ts * 1000), sid, value if math.isfinite(value) else None))
        if len(labels_by_sid) > self.limits.max_series:
            raise LimitExceeded(
                f"query returned {len(labels_by_sid)} series (limit {self.limits.max_series})",
                hint="narrow the selector with label filters or aggregate the histogram by fewer labels",
            )
        rows.sort(key=lambda r: (r[1], r[0]))
        vals = [r[2] for r in rows]
        buckets = pa.table(
            {
                "ts_ms": [r[0] for r in rows],
                "series_id": [r[1] for r in rows],
                "avg": vals,
                "min": vals,
                "max": vals,
                "count": [1] * len(rows),
            },
            schema=BUCKET_SCHEMA,
        )
        sids = sorted(labels_by_sid)
        series = pa.table(
            {"series_id": sids, "labels": [labels_json(labels_by_sid[s]) for s in sids]},
            schema=SERIES_SCHEMA,
        )
        return FetchResult(buckets, series)

    async def fetch_histogram(
        self, selector: str, by: Sequence[str], rng: TimeRange, step_ms: int
    ) -> DistResult:
        """Per-step bucket counts: increase() over a window equal to the step (spec §4.1)."""
        if not is_selector(selector):
            raise SourceError(f"not a metric selector: {selector!r}", hint=_HIST_HINT)
        steps = (rng.end_ms - rng.start_ms) // step_ms + 1
        if steps > MAX_STEPS_PER_QUERY:
            raise LimitExceeded(
                f"{steps} steps exceeds {MAX_STEPS_PER_QUERY} per query",
                hint="use a coarser step or a shorter range",
            )
        try:
            expr = histogram_expr(selector, by, step_ms)
        except ValueError as e:
            raise SourceError(
                str(e), hint="`by` takes plain label names other than le/vmrange"
            ) from e
        result = await self._query_range(expr, rng, step_ms)
        try:
            dist = from_matrix(self.name, result, expr=expr)
        except (ValueError, TypeError, IndexError, KeyError) as e:
            raise SourceError(str(e), hint=_HIST_HINT) from e
        if dist.series.num_rows > self.limits.max_series:
            raise LimitExceeded(
                f"histogram has {dist.series.num_rows} series (limit {self.limits.max_series})",
                hint="group by fewer labels (by=[...]) or narrow the selector",
            )
        if dist.rows.num_rows > self.limits.max_points:
            raise LimitExceeded(
                f"histogram has {dist.rows.num_rows} non-empty cells (limit {self.limits.max_points})",
                hint="use a coarser step or a shorter range",
            )
        return dist

    async def _query_range(self, query: str, rng: TimeRange, step_ms: int) -> list[dict]:
        params = {
            "query": query,
            "start": f"{rng.start_ms / 1000:.3f}",
            "end": f"{rng.end_ms / 1000:.3f}",
            "step": f"{step_ms / 1000:g}s",
        }
        if self.flavor == "victoriametrics":
            # Our cache owns freshness; VM's response cache would hide late samples.
            params["nocache"] = "1"
        data = (await self._get_json("/api/v1/query_range", params)).get("data")
        if not isinstance(data, dict) or "resultType" not in data or "result" not in data:
            raise _malformed("response missing data.resultType / data.result")
        if data["resultType"] != "matrix":
            raise SourceError(
                f"expected matrix result, got {data['resultType']}",
                hint="use an expression that returns a range of values, not a scalar or string",
            )
        if not isinstance(data["result"], list):
            raise _malformed("data.result is not a list")
        return data["result"]

    async def probe(self) -> dict:
        """Cheap reachability check: buildinfo, else a trivial instant query
        (some proxies hide status endpoints)."""
        t0 = time.monotonic()
        info: dict[str, str] = {}
        try:
            data = (await self._get_json("/api/v1/status/buildinfo", {})).get("data")
            if isinstance(data, dict):
                info = {k: str(data[k]) for k in ("application", "version") if k in data}
        except SourceError:
            await self._get_json("/api/v1/query", {"query": "1"})
        return {"reachable": True, "latency_ms": round((time.monotonic() - t0) * 1000), **info}
