"""PromQL / MetricsQL adapter returning min/max/avg/count buckets (never point samples)."""

from __future__ import annotations

import asyncio
import itertools
import math
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from importlib.metadata import PackageNotFoundError, version
from typing import Literal, NamedTuple

import httpx
import pyarrow as pa

from telemetry_nerd.analysis.histogram import from_matrix, histogram_expr
from telemetry_nerd.model.discovery import Discovery, MetricInfo
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
from telemetry_nerd.sources.observed import observed_count_query
from telemetry_nerd.sources.semantics import (
    MissingDataSemantics,
    classify_limit_error,
    semantics_for,
)
from telemetry_nerd.sources.spec import SourceSpec

MAX_STEPS_PER_QUERY = 11_000
#: resolution assumed until the source's scrape spacing is learned (and when it cannot be)
DEFAULT_RESOLUTION_MS = 15_000
#: series per selector whose raw samples the resolution probe reads
SPACING_SERIES = 50
SPACING_WINDOW = "10m"
_SELECTOR = re.compile(r"^\s*[a-zA-Z_:][a-zA-Z0-9_:]*\s*(\{[^{}]*\})?\s*$")
_DEFAULT_LIMITS = Limits()
_FIELDS = ("avg", "min", "max", "count")
_VALUES = frozenset(_FIELDS[:3])


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


def _name_collision(
    name_a: str | None, name_b: str | None, labels: Mapping[str, str]
) -> SourceError:
    """Two series of one result that differ only by `__name__`, which the dataset drops (series
    identity is the label set without it): they would share a series id and bucket timestamps."""
    return SourceError(
        f"series {name_a!r} and {name_b!r} differ only by __name__ (shared labels {dict(labels)}): "
        "they cannot be told apart in one dataset",
        hint="select one metric name, or aggregate/relabel them apart, e.g. sum by (...) (...) or "
        "label_replace(...) to put the name in a label",
    )


_LIMIT_HINTS = {
    "max_points": "use a coarser step or a shorter range",
    "max_samples": "narrow the selector, shorten the range, or use a coarser step",
    "max_series": "narrow the selector with label filters or aggregate, e.g. sum by (service) (...)",
    "range_too_long": "shorten the range",
    "timeout": "narrow the selector, shorten the range, or use a coarser step",
}


def _raise_if_limit(message: str) -> None:
    """Server-side query limits (documented per backend in sources/semantics.py) are
    LimitExceeded, never a generic failure: the data exists but was not served."""
    if (kind := classify_limit_error(message)) is not None:
        raise LimitExceeded(
            f"source refused the query: {message.strip()[:300]}", hint=_LIMIT_HINTS[kind]
        )


# Prometheus-engine annotations (Prometheus 3 / Thanos / Mimir) that describe the expression,
# not the data's completeness: "PromQL info: ..." and "PromQL warning: ...". Anything else in
# `warnings` (store unavailable, partial response, timeouts, ...) may mean data is missing, and
# a message we cannot classify is treated as partial: can't tell => unknown (spec section 6).
_INFORMATIONAL = re.compile(r"^\s*PromQL (info|warning):", re.IGNORECASE)
_VM_PARTIAL = (
    "VictoriaMetrics returned isPartial=true: some storage nodes did not answer, "
    "so the result may be incomplete"
)


def classify_warnings(warnings: object) -> tuple[str | None, list[str]]:
    """Split a response's `warnings` into (partial message or None, informational notes)."""
    if not isinstance(warnings, list):
        return None, []
    partial: list[str] = []
    notes: list[str] = []
    for w in warnings:
        text = str(w)
        (notes if _INFORMATIONAL.match(text) else partial).append(text)
    return ("; ".join(partial) if partial else None), notes


class _Matrix(NamedTuple):
    """A range query's series plus what the response said about its own completeness."""

    result: list[dict]
    partial: str | None = None  # source message when the response is partial
    notes: tuple[str, ...] = ()  # informational warnings


def _completeness(
    rng: TimeRange, *parts: _Matrix
) -> tuple[tuple[tuple[int, int, str], ...], tuple[str, ...]]:
    """(failed spans, notes) of one chunk fetch: a partial response makes the whole chunk
    unknown (we cannot tell which series or steps are missing), the data stays."""
    msgs = list(dict.fromkeys(p.partial for p in parts if p.partial))
    notes = tuple(dict.fromkeys(n for p in parts for n in p.notes))
    failed = ((rng.start_ms, rng.end_ms, f"PartialResponse: {'; '.join(msgs)}"),) if msgs else ()
    return failed, notes


def is_selector(expr: str) -> bool:
    return bool(_SELECTOR.match(expr))


class PromQLSource:
    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        flavor: Literal["victoriametrics", "prometheus"] = "victoriametrics",
        resolution_ms: int | None = None,
        limits: Limits = _DEFAULT_LIMITS,
        client: httpx.AsyncClient | None = None,
        headers: Mapping[str, str] | None = None,
        gate: Gate | None = None,
        backend: str | None = None,
    ) -> None:
        self.name = name
        self.backend = backend
        self.base_url = base_url.rstrip("/")
        self.flavor = flavor
        #: the configured resolution overrides what is learned; None: learn it (learn_resolution)
        self.configured_resolution_ms = resolution_ms
        self.resolution_ms = resolution_ms or DEFAULT_RESOLUTION_MS
        self.resolution_origin: Literal["configured", "learned", "assumed"] = (
            "configured" if resolution_ms else "assumed"
        )
        self.resolution_learned: dict | None = None  # the last probe's measurement
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
    def semantics(self) -> MissingDataSemantics:
        """What this source does at the edges of its data (profile; evidence in data-source-quirks)."""
        return semantics_for(self.backend, self.flavor)

    @property
    def identity(self) -> str:
        return f"{self.flavor}|{self.base_url}|{self.resolution_ms}"

    def _window(self, expr: str, step_ms: int) -> str:
        window = format_duration(step_ms)
        if is_selector(expr):
            return f"{expr.strip()}[{window}]"
        return f"({expr.strip()})[{window}:{format_duration(self.resolution_ms)}]"

    def _observed_count(self, expr: str, step_ms: int) -> str | None:
        """Sample count behind a non-selector expression, from its underlying selector: the
        subquery's own count counts lookback-filled evaluations, not samples (1h9.11)."""
        if is_selector(expr):
            return None
        return observed_count_query(expr, format_duration(step_ms))

    def build_queries(self, expr: str, step_ms: int) -> dict[str, str]:
        win = self._window(expr, step_ms)
        if self.flavor == "victoriametrics":
            queries = {"rollup": f"rollup({win})", "count": f"count_over_time({win})"}
        else:
            queries = {field: f"{field}_over_time({win})" for field in _FIELDS}
        if (observed := self._observed_count(expr, step_ms)) is not None:
            queries["count"] = observed
        # else (expression we cannot derive from): the count is subquery evaluations, not
        # samples; consumers learn so from sources.observed.counts_are_observed(expr)
        return queries

    async def fetch(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult:
        steps = (rng.end_ms - rng.start_ms) // step_ms + 1
        if steps > MAX_STEPS_PER_QUERY:
            raise LimitExceeded(
                f"{steps} steps exceeds {MAX_STEPS_PER_QUERY} per query",
                hint="use a coarser step or a shorter range",
            )
        queries = self.build_queries(expr, step_ms)
        derived = self._observed_count(expr, step_ms) is not None
        results = await asyncio.gather(
            *(self._query_range(q, rng, step_ms) for q in queries.values())
        )
        cells: dict[tuple[str, int], dict[str, float | None]] = {}
        labels_by_sid: dict[str, dict[str, str]] = {}
        named: dict[tuple[str, str], str | None] = {}  # (field, series id) -> its __name__
        for query_field, matrix in zip(queries, results, strict=True):
            for item in matrix.result:
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
                name = labels.pop("__name__", None)
                if field not in _FIELDS:
                    continue
                sid = series_id(self.name, labels)
                if (field, sid) in named:  # one query gave this series twice (names differ)
                    raise _name_collision(named[(field, sid)], name, labels)
                named[(field, sid)] = name
                labels_by_sid[sid] = labels
                for sample in samples:
                    try:
                        ts, value = float(sample[0]), float(sample[1])
                    except (IndexError, TypeError, ValueError) as e:
                        raise _malformed(f"malformed sample {sample!r} in query result") from e
                    # NaN / +-Inf cannot be averaged: store NaN (a positive observation, not
                    # null = no value) and keep the bucket's count; summaries flag `non_finite`.
                    cells.setdefault((sid, round(ts * 1000)), {})[field] = (
                        value if math.isfinite(value) else math.nan
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
        notes_extra: tuple[str, ...] = ()
        if derived:
            # Values and count come from different queries here. Values without a count: no
            # sample arrived in the bucket. Either a scrape near the bucket edge spilled into
            # the next bucket (the expression's value there is real: a window reaching past the
            # bucket, or increase() of a tile, 0 while the next tile carries the change), or
            # the bucket is a gap that lookback / a long range filled. Telling them apart needs
            # the series' cadence, which bucket_state reads: kept here with count 0 and settled
            # when the dataset is made (companions.settle_unobserved; uup), not dropped unseen.
            # A count without values is samples the expression has no value for (cause
            # unknown): kept with null values (null = no value, NaN = non-finite), so
            # bucket_state reads the samples that arrived (not EMPTY) and summaries flag
            # `no_value` (1h9.16).
            def no_value(c: dict) -> bool:
                return not (c.keys() & _VALUES)

            unobserved = {
                k: {**{f: c[f] for f in _VALUES}, "count": 0}
                for k, c in cells.items()
                if c.get("count") is None and _VALUES <= c.keys()
            }
            complete = {
                k: {f: c.get(f) for f in _FIELDS}
                for k, c in cells.items()
                if c.get("count") is not None and (no_value(c) or all(f in c for f in _FIELDS))
            }
            partial = sum(1 for c in cells.values() if 0 < len(c.keys() & _VALUES) < 3)
            # a series with no value anywhere is either the expression's own all-no-value
            # series or a count-query series whose labels match no expression series: telling
            # them apart is impossible, so none is invented; disclosed once
            valued = {sid for (sid, _), c in complete.items() if c["avg"] is not None}
            dropped = {sid for sid, _ in complete} - valued
            if dropped:
                complete = {k: c for k, c in complete.items() if k[0] in valued}
                named = sorted(labels_json(labels_by_sid[sid]) for sid in dropped)
                more = f" (+{len(named) - 3} more)" if len(named) > 3 else ""
                notes_extra = (
                    f"{len(dropped)} series had samples but no value anywhere in the window "
                    + "(or labels that match no value series) and are left out: "
                    + ", ".join(named[:3])
                    + more,
                )
            labels_by_sid = {sid: labels_by_sid[sid] for sid, _ in complete}
            # only series that observed samples somewhere: one never observed is no evidence
            complete |= {k: c for k, c in unobserved.items() if k[0] in valued}
        else:
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
        failed, notes = _completeness(rng, *results)
        return FetchResult(
            buckets, series, partial=partial, failed=failed, notes=(*notes, *notes_extra)
        )

    async def _get_json(
        self, path: str, params: Mapping[str, str | list[str]], timeout_s: float | None = None
    ) -> dict:
        url = f"{self.base_url}{path}"
        timeout_s = timeout_s or self.limits.timeout_s
        try:
            async with self._gate.slot():
                resp = await self._client.get(
                    url, params=params, headers=self._headers, timeout=timeout_s
                )
        except httpx.TimeoutException as e:
            raise SourceUnavailable(
                f"query timed out after {timeout_s}s",
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
            # Thanos answers some limit errors as plain text, not the JSON envelope
            _raise_if_limit(resp.text)
            raise SourceUnavailable(
                f"non-JSON response from source (HTTP {resp.status_code})",
                hint="check the source URL points at a Prometheus-compatible API, not a proxy page",
            ) from e
        if not isinstance(body, dict):
            raise _malformed(f"unexpected response body {type(body).__name__}")
        if body.get("status") != "success":
            _raise_if_limit(str(body.get("error", "")))
            raise SourceError(
                f"query failed: {body.get('error', f'HTTP {resp.status_code}')}",
                hint="check PromQL/MetricsQL syntax and metric names",
            )
        return body

    async def fetch_values(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult:
        """The expression's own value at each step: no rollup, no *_over_time.
        For quantiles and other non-additive values that must never be re-aggregated.

        Instant evaluation fills gaps, so where the observed samples can be derived
        (sources.observed) a second query keeps only values of buckets that observed any;
        otherwise values are returned as evaluated (`counts_are_observed(expr)` is False)."""
        steps = (rng.end_ms - rng.start_ms) // step_ms + 1
        if steps > MAX_STEPS_PER_QUERY:
            raise LimitExceeded(
                f"{steps} steps exceeds {MAX_STEPS_PER_QUERY} per query",
                hint="use a coarser step or a shorter range",
            )
        observed_q = observed_count_query(expr, format_duration(step_ms))
        if observed_q is None:
            matrix = await self._query_range(expr.strip(), rng, step_ms)
            count_matrix = None
        else:
            matrix, count_matrix = await asyncio.gather(
                self._query_range(expr.strip(), rng, step_ms),
                self._query_range(observed_q, rng, step_ms),
            )
        result = matrix.result
        counts = None if count_matrix is None else count_matrix.result
        rows: list[tuple[int, str, float | None]] = []
        labels_by_sid: dict[str, dict[str, str]] = {}
        named: dict[str, str | None] = {}
        for item in result:
            if isinstance(item, dict) and "histograms" in item:
                raise _native_histograms()
            try:
                labels = dict(item["metric"])
                samples = list(item["values"])
            except (KeyError, TypeError, ValueError) as e:
                raise _malformed(f"malformed series {item!r} in query result") from e
            name = labels.pop("__name__", None)
            sid = series_id(self.name, labels)
            if sid in named:
                raise _name_collision(named[sid], name, labels)
            named[sid] = name
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
        if counts is not None:
            observed = self._observed_cells(counts)
            # Instant evaluation fills (lookback, range windows longer than the step, VM's
            # previous sample): keep a value only where the bucket observed samples (1h9.11).
            rows = [r for r in rows if (r[1], r[0]) in observed]
            labels_by_sid = {sid: labels_by_sid[sid] for sid in {r[1] for r in rows}}
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
        failed, notes = _completeness(rng, matrix, *([count_matrix] if count_matrix else []))
        return FetchResult(buckets, series, failed=failed, notes=notes)

    def _observed_cells(self, result: list[dict]) -> set[tuple[str, int]]:
        """(series_id, ts_ms) of buckets with at least one observed sample."""
        cells: set[tuple[str, int]] = set()
        for item in result:
            try:
                labels = dict(item["metric"])
                samples = list(item["values"])
                labels.pop("__name__", None)
                sid = series_id(self.name, labels)
                cells.update((sid, round(float(t) * 1000)) for t, v in samples if float(v) > 0)
            except (KeyError, TypeError, ValueError) as e:
                raise _malformed(f"malformed series {item!r} in query result") from e
        return cells

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
        matrix = await self._query_range(expr, rng, step_ms)
        try:
            dist = from_matrix(self.name, matrix.result, expr=expr)
        except (ValueError, TypeError, IndexError, KeyError) as e:
            raise SourceError(str(e), hint=_HIST_HINT) from e
        if dist.scheme.kind == "classic":
            await self._check_le_layouts(selector, by, rng.end_ms)
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
        failed, notes = _completeness(rng, matrix)
        if failed or notes:
            dist = replace(
                dist,
                failed=failed,
                caveats=(*dist.caveats, *(f"source_warning:{n}" for n in notes)),
            )
        return dist

    async def _check_le_layouts(self, selector: str, by: Sequence[str], at_ms: int) -> None:
        """`sum by (le)` across series with different bucket layouts gives bogus cumulative counts
        (4ok.17). Within each `by` group every series must carry the same le set: refuse otherwise.
        Best effort: a source that cannot answer the probe is not blocked."""
        probe = f"count by (le{''.join(', ' + b for b in by)}) ({selector})"
        try:
            body = await self._get_json(
                "/api/v1/query", {"query": probe, "time": f"{at_ms / 1000:.3f}"}
            )
        except SourceError:
            return
        data = body.get("data")
        items = data.get("result") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return
        counts: dict[tuple, set[float]] = {}
        for item in items:
            try:
                m = item["metric"]
                counts.setdefault(tuple(m.get(b, "") for b in by), set()).add(
                    float(item["value"][1])
                )
            except (KeyError, TypeError, ValueError, IndexError):
                return
        bad = [g for g, c in counts.items() if len(c) > 1]
        if bad:
            raise SourceError(
                "series in the same group have different bucket layouts (le sets), so summing by le "
                "gives wrong cumulative counts",
                hint=(
                    "group by the label that distinguishes the layouts (by=[...], e.g. job or "
                    "service), or narrow the selector to one layout"
                ),
            )

    async def _query_range(self, query: str, rng: TimeRange, step_ms: int) -> _Matrix:
        params = {
            "query": query,
            "start": f"{rng.start_ms / 1000:.3f}",
            "end": f"{rng.end_ms / 1000:.3f}",
            "step": f"{step_ms / 1000:g}s",
        }
        if self.flavor == "victoriametrics":
            # Our cache owns freshness; VM's response cache would hide late samples.
            params["nocache"] = "1"
        body = await self._get_json("/api/v1/query_range", params)
        data = body.get("data")
        if not isinstance(data, dict) or "resultType" not in data or "result" not in data:
            raise _malformed("response missing data.resultType / data.result")
        if data["resultType"] != "matrix":
            raise SourceError(
                f"expected matrix result, got {data['resultType']}",
                hint="use an expression that returns a range of values, not a scalar or string",
            )
        if not isinstance(data["result"], list):
            raise _malformed("data.result is not a list")
        partial, notes = classify_warnings(body.get("warnings"))
        if body.get("isPartial") is True:  # VictoriaMetrics cluster
            partial = "; ".join(filter(None, (_VM_PARTIAL, partial)))
        return _Matrix(data["result"], partial, tuple(notes))

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

    # discovery (spec §4.1) -------------------------------------------------
    async def _listing(self, path: str, params: dict[str, str] | None = None) -> object:
        body = await self._get_json(path, params or {}, self.limits.discover_timeout_s)
        return body.get("data")

    async def discover(self) -> Discovery:
        """Names, metadata, label names and top cardinalities in a handful of cheap calls.

        Never loops per metric. Optional steps degrade to a caveat; only names are fatal.
        """
        caveats: list[str] = []
        partial = False

        names = await self._listing("/api/v1/label/__name__/values")
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise _malformed("label values for __name__ are not a list of strings")
        if len(names) > self.limits.max_metrics:
            caveats.append(f"metrics_truncated:{self.limits.max_metrics}/{len(names)}")
            names = names[: self.limits.max_metrics]
            partial = True
        name_set = set(names)

        meta = await self._discover_metadata(name_set)
        if meta is None:
            caveats.append("metadata_unavailable")
            partial = True
            meta = {}
        coverage = sum(1 for n in names if n in meta) / len(names) if names else 1.0
        if meta and coverage < 1.0:
            caveats.append(f"metadata_coverage:{coverage:.0%}")

        try:
            labels = await self._listing("/api/v1/labels")
            if not isinstance(labels, list):
                raise _malformed("labels is not a list")
            label_names = tuple(str(x) for x in labels)
        except SourceError:
            label_names = ()
            caveats.append("labels_unavailable")
            partial = True

        cardinality = await self._discover_cardinality()
        if cardinality is None:
            caveats.append("cardinality_unavailable")
            partial = True
        else:
            caveats.append("cardinality_top_only")

        histograms = _histogram_families(name_set, meta)
        # a classic histogram's base name is not a series (no __name__ value) but is the
        # metric: catalogue it too, with the metadata Prometheus keys by the base name
        bases = [b for b, k in sorted(histograms.items()) if k == "classic" and b not in name_set]
        for b in bases:  # some exporters key the metadata by X_bucket instead
            if b not in meta and f"{b}_bucket" in meta:
                meta[b] = meta[f"{b}_bucket"]
        metrics = tuple(
            MetricInfo(
                n,
                meta[n]["type"] if n in meta else None,
                meta[n]["help"] if n in meta else None,
                meta[n]["unit"] if n in meta else None,
            )
            for n in [*names, *bases]
        )
        return Discovery(
            metrics=metrics,
            label_names=label_names,
            histograms=histograms,
            cardinality=cardinality,
            metadata_coverage=coverage,
            caveats=tuple(caveats),
            partial=partial,
        )

    async def label_values(
        self,
        label: str,
        match: Sequence[str] = (),
        rng: TimeRange | None = None,
        limit: int | None = None,
    ) -> list[str]:
        """Values of one label among series matching `match` (any series when empty) that have
        samples in `rng`: the index answers it (/api/v1/label/<label>/values), no samples are
        read. Sorted; at most `limit` when given (VictoriaMetrics/Prometheus `limit`)."""
        params: dict[str, str | list[str]] = {}
        if match:
            params["match[]"] = list(match)
        if rng is not None:
            params["start"] = f"{rng.start_ms / 1000:.3f}"
            params["end"] = f"{rng.end_ms / 1000:.3f}"
        if limit is not None:
            params["limit"] = str(limit)
        data = (await self._get_json(f"/api/v1/label/{label}/values", params)).get("data")
        if not isinstance(data, list):
            raise _malformed(f"label values for {label} are not a list")
        vals = sorted(str(v) for v in data)
        return vals[:limit] if limit is not None else vals

    async def _discover_metadata(self, names: set[str]) -> dict[str, dict] | None:
        """Union of /metadata responses: Thanos returns a different subset per call, so retry
        until every name is covered or attempts run out. None if no call succeeded."""
        union: dict[str, dict] = {}
        succeeded = False
        for _ in range(_METADATA_ATTEMPTS):
            try:
                data = await self._listing("/api/v1/metadata")
            except SourceError:
                continue
            if not isinstance(data, dict):
                continue
            succeeded = True
            for name, entries in data.items():
                if name in union or not entries or not isinstance(entries, list):
                    continue
                e = entries[0]
                kind = e.get("type")
                union[name] = {
                    "type": kind if kind in _METRIC_TYPES else None,
                    "help": e.get("help") or None,
                    "unit": e.get("unit") or None,
                }
            if names <= union.keys():
                break
        return union if succeeded else None

    async def _discover_cardinality(self) -> dict[str, int] | None:
        """Top metrics by series count (Prometheus/VM tsdb status); None if unsupported."""
        try:
            data = await self._listing("/api/v1/status/tsdb", {"limit": "100", "topN": "100"})
            rows = data["seriesCountByMetricName"] if isinstance(data, dict) else None
            if not isinstance(rows, list):
                return None
            return {str(r["name"]): int(r["value"]) for r in rows}
        except (SourceError, KeyError, TypeError, ValueError):
            return None

    async def sample_spacing(self, selector: str, at_ms: int | None = None) -> dict[str, list[int]]:
        """Per job label: the median sample spacing (ms) of each series of `selector` with >= 3
        raw samples in the SPACING_WINDOW up to `at_ms` (default now), for at most SPACING_SERIES
        series. Series without a job label are keyed ''."""
        params = {"query": f"{selector.strip()}[{SPACING_WINDOW}]", "limit": str(SPACING_SERIES)}
        if at_ms is not None:
            params["time"] = f"{at_ms / 1000:.3f}"
        body = await self._get_json("/api/v1/query", params)
        result = (body.get("data") or {}).get("result")
        out: dict[str, list[int]] = {}
        for r in result if isinstance(result, list) else []:
            values = r.get("values") if isinstance(r, dict) else None
            if not isinstance(values, list) or len(values) < 3:
                continue
            gaps = sorted(float(b[0]) - float(a[0]) for a, b in itertools.pairwise(values))
            ms = round(gaps[len(gaps) // 2] * 1000)
            if ms > 0:
                job = str((r.get("metric") or {}).get("job", ""))
                out.setdefault(job, []).append(ms)
        return out

    async def learn_resolution(self, candidates: Sequence[str] = ()) -> dict:
        """Measure the scrape spacing (bead wbw): the median raw-sample spacing per series of
        `up` (one series per scrape target), else of the first candidate metric that has recent
        samples (push-based sources have no `up`); per job, the median over its series.

        The resolution becomes the COARSEST job's spacing: every job's series then hold at least
        one sample per resolution step, so windows built on it (rate intervals, increase tiles,
        sub-steps) are never empty; finer jobs are read at that coarser step (stated). A
        configured resolution is kept (it overrides) and only compared. Returns what was found."""
        by_job: dict[str, list[int]] = {}
        probed = None
        for sel in ["up", *candidates]:
            try:
                by_job = await self.sample_spacing(sel)
            except SourceError:
                continue
            if by_job:
                probed = sel
                break
        info: dict = {"probed": probed, "window": SPACING_WINDOW}
        if not by_job:
            info["note"] = (
                f"no series with >= 3 samples in the last {SPACING_WINDOW}: scrape spacing unknown"
            )
            self.resolution_learned = info
            return self.resolution_info()
        # scrape intervals are whole seconds: the median of jittered spacings (5.038 s) is
        # rounded to one, so steps and windows built on it stay round
        jobs = {
            j: max(1_000, round(sorted(v)[len(v) // 2] / 1_000) * 1_000)
            for j, v in sorted(by_job.items())
        }
        learned = min(max(max(jobs.values()), 1_000), 3_600_000)
        info |= {
            "resolution_ms": learned,
            "by_job": {j or "(no job label)": format_duration(ms) for j, ms in jobs.items()},
            "series": sum(len(v) for v in by_job.values()),
        }
        if len(set(jobs.values())) > 1:
            info["note"] = (
                "jobs are scraped at different intervals: the resolution is the coarsest, so "
                "every series has a sample per step; finer jobs are read at it"
            )
        self.resolution_learned = info
        if self.configured_resolution_ms is None:
            self.resolution_ms, self.resolution_origin = learned, "learned"
        return self.resolution_info()

    def resolution_info(self) -> dict:
        """The resolution in use, where it came from, and what the scrape spacing probe saw."""
        out: dict = {
            "resolution": format_duration(self.resolution_ms),
            "origin": self.resolution_origin,
        }
        if self.resolution_origin == "assumed":
            out["note"] = (
                f"assumed {format_duration(DEFAULT_RESOLUTION_MS)}: the scrape spacing is not "
                "learned yet (source_learn measures it; or connect with resolution=...)"
            )
        m = self.resolution_learned
        if m is not None:
            out["measured"] = {k: v for k, v in m.items() if k != "resolution_ms"}
            got = m.get("resolution_ms")
            if got is not None:
                out["measured"]["resolution"] = format_duration(got)
            if (
                self.resolution_origin == "configured"
                and got
                and abs(got - self.resolution_ms) > (self.resolution_ms // 5)
            ):
                out["mismatch"] = (
                    f"configured {format_duration(self.resolution_ms)} but series are scraped "
                    f"every {format_duration(got)} (hint: reconnect without resolution to use "
                    "the measured spacing)"
                )
        return out

    async def scrape_interval(self, selector: str, at_ms: int | None = None) -> int | None:
        """Median sample spacing of one series over the 10m up to `at_ms` (default now) (ms);
        None if < 3 samples.

        Per metric, not per source: native resolution differs per job (15/20/30/60s)."""
        params = {"query": f"{selector.strip()}[10m]", "limit": "1"}
        if at_ms is not None:
            params["time"] = f"{at_ms / 1000:.3f}"
        body = await self._get_json("/api/v1/query", params)
        result = (body.get("data") or {}).get("result")
        if not isinstance(result, list) or not result:
            return None
        values = result[0].get("values")
        if not isinstance(values, list) or len(values) < 3:
            return None
        ts = [float(v[0]) for v in values]
        gaps = sorted(b - a for a, b in itertools.pairwise(ts))
        return round(gaps[len(gaps) // 2] * 1000)


_METADATA_ATTEMPTS = 3
_METRIC_TYPES = frozenset(
    {"counter", "gauge", "histogram", "summary", "gaugehistogram", "info", "stateset"}
)


def _histogram_families(names: set[str], meta: dict[str, dict]) -> dict[str, str]:
    """Classic: _bucket with _sum and _count series. Native: typed histogram with no _bucket."""
    out: dict[str, str] = {}
    for n in names:
        if n.endswith("_bucket"):
            base = n[: -len("_bucket")]
            if f"{base}_sum" in names and f"{base}_count" in names:
                out[base] = "classic"
    for key, m in meta.items():
        if m["type"] != "histogram":
            continue
        base = key.removesuffix("_bucket")
        if base in out:
            continue
        out[base] = "classic" if f"{base}_bucket" in names else "native"
    return out
