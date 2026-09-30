"""PromQL / MetricsQL adapter returning min/max/avg/count buckets (never point samples)."""

from __future__ import annotations

import asyncio
import re
from typing import Literal

import httpx
import pyarrow as pa

from telemetry_nerd.model.series import (
    BUCKET_SCHEMA,
    SERIES_SCHEMA,
    FetchResult,
    labels_json,
    series_id,
)
from telemetry_nerd.model.time import TimeRange, format_duration
from telemetry_nerd.sources.base import LimitExceeded, Limits, SourceError, SourceUnavailable

MAX_STEPS_PER_QUERY = 11_000
_SELECTOR = re.compile(r"^\s*[a-zA-Z_:][a-zA-Z0-9_:]*\s*(\{[^{}]*\})?\s*$")
_DEFAULT_LIMITS = Limits()
_FIELDS = ("avg", "min", "max", "count")


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
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.flavor = flavor
        self.resolution_ms = resolution_ms
        self.limits = limits
        self._client = client or httpx.AsyncClient()

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
        cells: dict[tuple[str, int], dict[str, float]] = {}
        labels_by_sid: dict[str, dict[str, str]] = {}
        for query_field, result in zip(queries, results, strict=True):
            for item in result:
                labels = dict(item["metric"])
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
                for sample in item["values"]:
                    try:
                        ts, value = float(sample[0]), float(sample[1])
                    except (IndexError, TypeError, ValueError) as e:
                        raise SourceError(
                            f"malformed sample {sample!r} in query result",
                            hint="retry the query; if it persists, check the source version",
                        ) from e
                    cells.setdefault((sid, round(ts * 1000)), {})[field] = value
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
                "count": [round(cells[k]["count"]) for k in keys if "count" in cells[k]],
            },
            schema=BUCKET_SCHEMA,
        )
        sids = sorted(labels_by_sid)
        series = pa.table(
            {"series_id": sids, "labels": [labels_json(labels_by_sid[s]) for s in sids]},
            schema=SERIES_SCHEMA,
        )
        return FetchResult(buckets, series)

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
        url = f"{self.base_url}/api/v1/query_range"
        try:
            resp = await self._client.get(url, params=params, timeout=self.limits.timeout_s)
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
        try:
            body = resp.json()
        except ValueError as e:
            raise SourceError(f"non-JSON response from source (HTTP {resp.status_code})") from e
        if body.get("status") != "success":
            raise SourceError(
                f"query failed: {body.get('error', f'HTTP {resp.status_code}')}",
                hint="check PromQL/MetricsQL syntax and metric names",
            )
        data = body["data"]
        if data["resultType"] != "matrix":
            raise SourceError(f"expected matrix result, got {data['resultType']}")
        return data["result"]
