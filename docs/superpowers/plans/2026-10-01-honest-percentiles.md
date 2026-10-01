# Honest Percentiles — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Beads: `telemetry-nerd-4ok.2` (bug: pipeline aggregates percentiles over time) and `telemetry-nerd-4ok.1` (sample count per bucket + low_count), epic M4 `telemetry-nerd-4ok`.

**Goal:** A percentile in Telemetry Nerd is never aggregated (across series or over time) and never shown or used as evidence without the number of observations behind it.

**Architecture:** A pure analyzer (`analysis/exprkind.py`) classifies an expression: a quantile expression (`histogram_quantile`, `quantile_over_time`, a summary `{quantile=...}` selector) must be outermost (optionally scaled by a numeric literal); anything wrapping it is refused with a hint. For a quantile expression the analyzer derives a **count expression** from the same histogram (classic: `max without (le)(X)`, native: `histogram_count(X)`, times the rate window when `X` is a rate). The service evaluates quantile and count expressions **as plain per-step values** (`PromQLSource.fetch_values`, no `rollup`/`*_over_time`), joins them so each bucket carries `n`, and stores a dataset with `representation="quantile"`, `quantile=q`, `n_min=ceil(10/(1-q))`. Quantile datasets are never re-bucketed by LOD; summaries report n and meaningful-bucket counts instead of a mean; the UI fades buckets with `n < n_min`; a percentile statistic cannot be evidence without `params.n ≥ n_min`. `$__rate_interval` in any expression expands to `max(4×resolution, step+resolution)` so Claude can tie the rate window to the display step.

**Tech Stack:** Python ≥3.12 (venv 3.14), uv, polars, pyarrow, pydantic v2, httpx, pytest + respx; Svelte 5 + TypeScript + uPlot, vitest.

**Spec:** `docs/superpowers/specs/2026-09-30-telemetry-nerd-mvp-design.md` §1.2 (principles 3–4), §3.2 (representation `estimate`/distributions), §3.3 invariants (statistics as evidence), §6.5 (LOD). Context: Grafana Play test run (panel p3: p95 averaged over time, n≈10 per window).

## Global Constraints

- Work directly on `master`. Commit after every task; trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Close both beads after Task 6.
- Python via `uv`; tasks via `just`; search via `rg`/`fd`. After every task: `just lint` and `just test`; UI tasks also `just ui-test`, `just ui-check`, `just ui-build`. Do **not** run `just e2e` (it wipes the dev VictoriaMetrics volume).
- **NEVER AGGREGATE PERCENTILES** — not across series, not over time (rollups, `*_over_time`, LOD rebucketing). Aggregate histograms first, take the quantile once.
- **Rule of thumb:** a quantile `q` over `n` observations is meaningful only if `n ≥ ceil(10/(1−q))` (p50 20, p95 200, p99 1000, p99.9 10000).
- JSON never contains NaN/Inf (`telemetry_nerd.model.jsonsafe`); NaN values from the source become `null`.
- Existing (non-quantile) query behaviour must not change except `$__rate_interval` expansion.

## File Structure

```
src/telemetry_nerd/analysis/exprkind.py     # NEW min_samples, rate_interval_ms, expand, analyze -> ExprAnalysis
src/telemetry_nerd/analysis/quantile.py     # NEW attach_counts(values, counts) -> FetchResult
src/telemetry_nerd/sources/base.py          # MOD Source.fetch_values
src/telemetry_nerd/sources/promql.py        # MOD fetch_values (plain query_range)
src/telemetry_nerd/datasets/store.py        # MOD DatasetMeta.quantile/n_min; put(representation=, quantile=, n_min=)
src/telemetry_nerd/core/service.py          # MOD query: expand, analyze, quantile path; panel_data: no LOD for quantile
src/telemetry_nerd/core/summary.py          # MOD quantile summary (n, meaningful buckets, low_count/n_unknown)
src/telemetry_nerd/workspace/models.py      # MOD StatisticRef: percentile needs params.n >= n_min
src/telemetry_nerd/mcp/server.py            # MOD query docstring + INSTRUCTIONS
ui/src/lib/api.ts                           # MOD DatasetMeta.quantile/n_min
ui/src/chart/toUplot.ts                     # MOD quantile mode: no envelope, faded low-n segments
ui/src/Panel.svelte                         # MOD pass quantile opts; footer text
tests/unit/test_exprkind.py                 # NEW
tests/unit/test_quantile_counts.py          # NEW
tests/unit/test_promql.py                   # MOD fetch_values
tests/unit/test_service_quantile.py         # NEW
tests/unit/test_summary.py                  # MOD quantile summary
tests/unit/test_models.py                   # MOD percentile evidence
tests/unit/fakes.py                         # MOD FakeSource.fetch_values (+ per-expr values)
ui/src/chart/toUplot.test.ts                # MOD quantile mode
tests/integration/test_quantile_vm.py       # NEW classic histogram end to end
```

## Scope notes

- Distribution datasets / heatmaps / histogram charts are `4ok.3`–`4ok.5`; not here.
- Ratios (`sum(a/b)` vs `sum(a)/sum(b)`) are the same class of bug but out of scope; noted on `4ok`.
- Summary-type quantile series (`x{quantile="0.99"}`) are allowed only as a bare selector, with `n` unknown (`n_unknown` caveat); they can never back a finding (no `n`).
- Zoom-in refetch at finer steps is not implemented in the UI today; quantile datasets are simply served at their own step (never rebucketed).

---

### Task 1: Expression analyzer

**Files:**
- Create: `src/telemetry_nerd/analysis/exprkind.py`
- Test: `tests/unit/test_exprkind.py`

**Interfaces:**
- Consumes: `telemetry_nerd.model.time.format_duration`, `parse_duration`
- Produces:
  - `RATE_INTERVAL = "$__rate_interval"`
  - `min_samples(q: float) -> int`
  - `rate_interval_ms(step_ms: int, resolution_ms: int) -> int`
  - `expand(expr: str, step_ms: int, resolution_ms: int) -> str`
  - `@dataclass(frozen=True) QuantileExpr(q: float | None, func: Literal["histogram_quantile", "quantile_over_time", "summary"], count_expr: str | None)`
  - `@dataclass(frozen=True) ExprAnalysis(quantile: QuantileExpr | None = None, problem: str | None = None)`
  - `analyze(expr: str) -> ExprAnalysis`
  - `QUANTILE_HINT: str`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_exprkind.py
import pytest

from telemetry_nerd.analysis.exprkind import (
    analyze,
    expand,
    min_samples,
    rate_interval_ms,
)

NATIVE = 'sum by (cloud_region) (rate(lat{svc="c"}[5m]))'
CLASSIC = 'sum by (le, region) (rate(lat_seconds_bucket{svc="c"}[2m]))'


@pytest.mark.parametrize(
    ("q", "n"), [(0.5, 20), (0.9, 100), (0.95, 200), (0.99, 1000), (0.999, 10000)]
)
def test_min_samples(q, n):
    assert min_samples(q) == n


@pytest.mark.parametrize("q", [0, 1, -0.1, 1.5])
def test_min_samples_rejects_out_of_range(q):
    with pytest.raises(ValueError):
        min_samples(q)


def test_rate_interval_and_expand():
    assert rate_interval_ms(30_000, 20_000) == 80_000  # 4 x scrape wins
    assert rate_interval_ms(300_000, 15_000) == 315_000  # step + scrape wins
    assert expand("rate(x[$__rate_interval])", 30_000, 20_000) == "rate(x[80s])"
    assert expand("rate(x[1m])", 30_000, 20_000) == "rate(x[1m])"


def test_plain_expression_is_not_quantile():
    a = analyze("sum by (svc) (rate(http_requests_total[5m]))")
    assert a.quantile is None and a.problem is None


def test_native_histogram_quantile_with_rate():
    a = analyze(f"histogram_quantile(0.95, {NATIVE})")
    assert a.problem is None
    assert a.quantile.func == "histogram_quantile"
    assert a.quantile.q == 0.95
    assert a.quantile.count_expr == f"(histogram_count({NATIVE})) * 300"


def test_classic_histogram_quantile_with_rate():
    a = analyze(f"histogram_quantile(0.99, {CLASSIC})")
    assert a.quantile.q == 0.99
    assert a.quantile.count_expr == f"(max without (le) ({CLASSIC})) * 120"


def test_increase_needs_no_window_multiplier():
    inner = "sum by (le) (increase(lat_bucket[10m]))"
    assert analyze(f"histogram_quantile(0.5, {inner})").quantile.count_expr == (
        f"max without (le) ({inner})"
    )


def test_mixed_windows_leave_n_unknown():
    inner = "sum by (le) (rate(a_bucket[1m])) + sum by (le) (rate(b_bucket[5m]))"
    a = analyze(f"histogram_quantile(0.9, {inner})")
    assert a.problem is None
    assert a.quantile.count_expr is None


def test_quantile_over_time_counts_samples():
    a = analyze("quantile_over_time(0.9, queue_depth{q=\"x\"}[10m])")
    assert a.quantile.func == "quantile_over_time"
    assert a.quantile.count_expr == 'count_over_time(queue_depth{q="x"}[10m])'


def test_scaling_by_a_literal_is_allowed():
    a = analyze(f"histogram_quantile(0.95, {NATIVE}) * 1000")
    assert a.problem is None and a.quantile.q == 0.95


@pytest.mark.parametrize(
    "expr",
    [
        f"sum(histogram_quantile(0.95, {NATIVE}))",
        f"avg by (x) (histogram_quantile(0.95, {NATIVE}))",
        f"max_over_time(histogram_quantile(0.95, {NATIVE})[1h:])",
        f"histogram_quantile(0.95, {NATIVE}) + histogram_quantile(0.5, {NATIVE})",
        f"histogram_quantile(0.95, {NATIVE}) / on() group_left sum(up)",
        'avg(rpc_latency{quantile="0.99"})',
    ],
)
def test_aggregated_percentiles_are_refused(expr):
    a = analyze(expr)
    assert a.quantile is None
    assert a.problem is not None and "percentile" in a.problem


def test_summary_quantile_selector_allowed_with_unknown_n():
    a = analyze('rpc_latency{quantile="0.99", job="api"}')
    assert a.problem is None
    assert a.quantile.func == "summary"
    assert a.quantile.count_expr is None


def test_quoted_text_does_not_fool_the_scanner():
    a = analyze('sum(rate(x{path="histogram_quantile(0.9, y)"}[5m]))')
    assert a.quantile is None and a.problem is None


def test_non_literal_quantile_is_kept_without_q():
    a = analyze(f"histogram_quantile(scalar(foo), {NATIVE})")
    assert a.quantile.q is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_exprkind.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'telemetry_nerd.analysis.exprkind'`

- [ ] **Step 3: Implement**

```python
# src/telemetry_nerd/analysis/exprkind.py
"""Classify PromQL so percentiles are never aggregated (spec §1.2, principles 3-4).

A quantile expression must be outermost; we derive the number of observations
behind each value from the same histogram so a percentile always travels with n.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

from telemetry_nerd.model.time import format_duration, parse_duration

RATE_INTERVAL = "$__rate_interval"
QUANTILE_HINT = (
    "never aggregate percentiles: aggregate the histogram first and take the quantile "
    "once, e.g. histogram_quantile(0.95, sum by (region) (rate(x[$__rate_interval]))); "
    "for summaries query one {quantile=...} selector as is"
)
_AGGREGATED = (
    "percentile aggregation refused: a quantile must be the outermost expression "
    "(optionally scaled by a number); wrapping it in sum/avg/max/over_time/arithmetic "
    "aggregates percentiles"
)
_QCALL = re.compile(r"\b(histogram_quantile|quantile_over_time)\s*\(")
_SCALE = re.compile(r"^\s*[*/]\s*[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\s*$")
_WINDOW = re.compile(r"\[([0-9]+(?:ms|s|m|h|d|w))(?::[^\]]*)?\]")
_RATE = re.compile(r"\b(?:rate|irate)\s*\(")
_INCREASE = re.compile(r"\bincrease\s*\(")
_CLASSIC = re.compile(r"_bucket\b|\ble\b")
_SUMMARY_Q = re.compile(r"\bquantile\s*=~?\s*[\"'`]")
_SELECTOR = re.compile(r"^\s*[a-zA-Z_:][a-zA-Z0-9_:]*\s*\{[^{}]*\}\s*$")


def min_samples(q: float) -> int:
    """Observations needed for a meaningful quantile: ~10 beyond it, n >= 10/(1-q)."""
    if not 0 < q < 1:
        raise ValueError(f"quantile must be in (0, 1), got {q}")
    return math.ceil(round(10 / (1 - q), 6))


def rate_interval_ms(step_ms: int, resolution_ms: int) -> int:
    """Grafana's $__rate_interval: at least 4 scrapes, and never shorter than a step."""
    return max(4 * resolution_ms, step_ms + resolution_ms)


def expand(expr: str, step_ms: int, resolution_ms: int) -> str:
    if RATE_INTERVAL not in expr:
        return expr
    return expr.replace(RATE_INTERVAL, format_duration(rate_interval_ms(step_ms, resolution_ms)))


@dataclass(frozen=True)
class QuantileExpr:
    q: float | None
    func: Literal["histogram_quantile", "quantile_over_time", "summary"]
    count_expr: str | None


@dataclass(frozen=True)
class ExprAnalysis:
    quantile: QuantileExpr | None = None
    problem: str | None = None


def _mask_strings(expr: str) -> str:
    """Blank out quoted text (keeping positions) so label values cannot fool the scanner."""
    out = list(expr)
    quote: str | None = None
    i = 0
    while i < len(expr):
        c = expr[i]
        if quote:
            if c == "\\" and quote != "`":
                out[i] = " "
                if i + 1 < len(expr):
                    out[i + 1] = " "
                i += 2
                continue
            if c == quote:
                quote = None
            else:
                out[i] = " "
        elif c in "\"'`":
            quote = c
        i += 1
    return "".join(out)


def _close(masked: str, open_idx: int) -> int:
    depth = 0
    for i in range(open_idx, len(masked)):
        c = masked[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("unbalanced parentheses in expression")


def _split_args(expr: str, masked: str, start: int, end: int) -> list[str]:
    args, depth, last = [], 0, start
    for i in range(start, end):
        c = masked[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            args.append(expr[last:i].strip())
            last = i + 1
    args.append(expr[last:end].strip())
    return args


def _literal(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _count_expr(func: str, inner: str) -> str | None:
    if func == "quantile_over_time":
        return f"count_over_time({inner})"
    masked = _mask_strings(inner)
    total = (
        f"max without (le) ({inner})" if _CLASSIC.search(masked) else f"histogram_count({inner})"
    )
    if _INCREASE.search(masked):
        return total
    if _RATE.search(masked):
        windows = {m.group(1) for m in _WINDOW.finditer(masked)}
        if len(windows) != 1:
            return None
        return f"({total}) * {parse_duration(windows.pop()) / 1000:g}"
    return None


def analyze(expr: str) -> ExprAnalysis:
    masked = _mask_strings(expr)
    calls = list(_QCALL.finditer(masked))
    if not calls:
        if _SUMMARY_Q.search(masked):
            if _SELECTOR.match(masked):
                return ExprAnalysis(QuantileExpr(None, "summary", None))
            return ExprAnalysis(problem=_AGGREGATED)
        return ExprAnalysis()
    first = calls[0]
    open_idx = first.end() - 1
    close_idx = _close(masked, open_idx)
    head, tail = masked[: first.start()], masked[close_idx + 1 :]
    if len(calls) > 1 or head.strip() or (tail.strip() and not _SCALE.match(tail)):
        return ExprAnalysis(problem=_AGGREGATED)
    args = _split_args(expr, masked, open_idx + 1, close_idx)
    if len(args) != 2:
        return ExprAnalysis(problem=f"{first.group(1)} takes 2 arguments, got {len(args)}")
    func = first.group(1)
    return ExprAnalysis(QuantileExpr(_literal(args[0]), func, _count_expr(func, args[1])))  # type: ignore[arg-type]
```

- [ ] **Step 4: Run tests and lint**

Run: `uv run pytest tests/unit/test_exprkind.py -q && just lint`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/analysis/exprkind.py tests/unit/test_exprkind.py
git commit -m "feat(analysis): classify quantile expressions and derive their sample count (4ok.2)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Plain per-step values from the source + count join

**Files:**
- Modify: `src/telemetry_nerd/sources/base.py`, `src/telemetry_nerd/sources/promql.py`
- Create: `src/telemetry_nerd/analysis/quantile.py`
- Modify: `tests/unit/fakes.py`
- Test: `tests/unit/test_promql.py` (append), `tests/unit/test_quantile_counts.py`

**Interfaces:**
- Consumes: `PromQLSource._query_range`, `labels_json`, `series_id`, `BUCKET_SCHEMA`, `SERIES_SCHEMA`
- Produces:
  - `Source.fetch_values(expr: str, rng: TimeRange, step_ms: int) -> FetchResult` (protocol)
  - `PromQLSource.fetch_values(...)`: one `query_range` of the expression as is; each sample → bucket with `avg=min=max=value` (non-finite → `None`) and `count=1`; same series identity rules as `fetch` (drop `__name__`); same series/points limits
  - `attach_counts(values: FetchResult, counts: FetchResult) -> FetchResult` — replaces `count` with `round(count value)` joined on `(series_id, ts_ms)`; missing or non-finite → 0
  - `FakeSource(values: dict[str, float] | None = None)` — `fetch_values` returns, per series and timestamp, the value of the first key contained in `expr` (default 1.0)

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_promql.py`:

```python
@respx.mock
async def test_fetch_values_is_a_plain_query_range():
    route = respx.get(**ROUTE).mock(
        return_value=httpx.Response(
            200,
            json=matrix(
                [
                    {
                        "metric": {"__name__": "x", "region": "a"},
                        "values": [[1_700_000_040, "0.25"], [1_700_000_100, "NaN"]],
                    }
                ]
            ),
        )
    )
    src = PromQLSource("s", BASE, flavor="victoriametrics")
    res = await src.fetch_values("histogram_quantile(0.9, x)", RNG, 60_000)
    assert route.calls.last.request.url.params["query"] == "histogram_quantile(0.9, x)"
    rows = res.buckets.to_pylist()
    assert [(r["avg"], r["min"], r["max"], r["count"]) for r in rows] == [
        (0.25, 0.25, 0.25, 1),
        (None, None, None, 1),
    ]
    assert res.series.to_pylist()[0]["series_id"] == series_id("s", {"region": "a"})
```

Create `tests/unit/test_quantile_counts.py`:

```python
import pyarrow as pa

from telemetry_nerd.analysis.quantile import attach_counts
from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult


def result(rows):
    buckets = pa.table(
        {
            "ts_ms": [r[0] for r in rows],
            "series_id": [r[1] for r in rows],
            "avg": [r[2] for r in rows],
            "min": [r[2] for r in rows],
            "max": [r[2] for r in rows],
            "count": [1] * len(rows),
        },
        schema=BUCKET_SCHEMA,
    )
    sids = sorted({r[1] for r in rows})
    series = pa.table({"series_id": sids, "labels": ["{}"] * len(sids)}, schema=SERIES_SCHEMA)
    return FetchResult(buckets, series)


def test_counts_replace_placeholder_and_missing_is_zero():
    values = result([(1, "a", 0.3), (2, "a", 0.4), (1, "b", 0.9)])
    counts = result([(1, "a", 250.4), (2, "a", None), (9, "a", 7.0)])
    out = attach_counts(values, counts).buckets.to_pylist()
    got = {(r["series_id"], r["ts_ms"]): (r["avg"], r["count"]) for r in out}
    assert got == {("a", 1): (0.3, 250), ("a", 2): (0.4, 0), ("b", 1): (0.9, 0)}


def test_series_table_comes_from_values():
    values = result([(1, "a", 0.3)])
    out = attach_counts(values, result([(1, "zzz", 5.0)]))
    assert out.series.to_pylist() == values.series.to_pylist()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_quantile_counts.py tests/unit/test_promql.py -q`
Expected: FAIL — missing `attach_counts` / `fetch_values`.

- [ ] **Step 3: Implement**

`base.py` — add to the `Source` protocol:

```python
    async def fetch_values(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult: ...
```

`promql.py` — add to `PromQLSource` (reuses `_query_range`, `labels_json`, `series_id`):

```python
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
```

`analysis/quantile.py`:

```python
"""Attach the number of observations behind each per-bucket quantile value."""

from __future__ import annotations

import polars as pl

from telemetry_nerd.model.series import BUCKET_SCHEMA, FetchResult


def attach_counts(values: FetchResult, counts: FetchResult) -> FetchResult:
    v = pl.from_arrow(values.buckets).drop("count")
    c = (
        pl.from_arrow(counts.buckets)
        .select("series_id", "ts_ms", pl.col("avg").fill_nan(None).alias("n"))
    )
    joined = (
        v.join(c, on=["series_id", "ts_ms"], how="left")
        .with_columns(pl.col("n").round(0).fill_null(0).cast(pl.Int64).alias("count"))
        .select(BUCKET_SCHEMA.names)
        .sort(["series_id", "ts_ms"])
    )
    return FetchResult(joined.to_arrow().cast(BUCKET_SCHEMA), values.series, partial=values.partial)
```

`tests/unit/fakes.py` — extend `FakeSource`:

```python
    def __init__(self, name="fake", n_series=2, resolution_ms=15_000, identity=None, values=None):
        ...  # existing assignments unchanged
        self.values = values or {}
        self.value_exprs: list[str] = []

    async def fetch_values(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult:
        self.calls += 1
        self.value_exprs.append(expr)
        v = next((val for key, val in self.values.items() if key in expr), 1.0)
        ts = list(range(rng.start_ms, rng.end_ms + 1, step_ms))
        labels = [{"instance": f"i{k}"} for k in range(self.n_series)]
        sids = [series_id(self.name, lb) for lb in labels]
        rows = [(t, sid) for sid in sids for t in ts]
        buckets = pa.table(
            {
                "ts_ms": [r[0] for r in rows],
                "series_id": [r[1] for r in rows],
                "avg": [v] * len(rows),
                "min": [v] * len(rows),
                "max": [v] * len(rows),
                "count": [1] * len(rows),
            },
            schema=BUCKET_SCHEMA,
        )
        series = pa.table(
            {"series_id": sids, "labels": [labels_json(lb) for lb in labels]}, schema=SERIES_SCHEMA
        )
        return FetchResult(buckets, series)
```

- [ ] **Step 4: Run all tests**

Run: `just test && just lint`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/sources/base.py src/telemetry_nerd/sources/promql.py src/telemetry_nerd/analysis/quantile.py tests/unit/fakes.py tests/unit/test_promql.py tests/unit/test_quantile_counts.py
git commit -m "feat(sources): plain per-step values and count join for quantiles (4ok.2)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Service quantile path, dataset metadata, no LOD for quantiles

**Files:**
- Modify: `src/telemetry_nerd/datasets/store.py`
- Modify: `src/telemetry_nerd/core/service.py` (`query`, `panel_data`)
- Test: `tests/unit/test_service_quantile.py`

**Interfaces:**
- Consumes: `expand`, `analyze`, `min_samples`, `QUANTILE_HINT` (Task 1); `fetch_values`, `attach_counts` (Task 2)
- Produces:
  - `DatasetMeta.quantile: float | None = None`, `DatasetMeta.n_min: int | None = None` (old stored metas load with defaults)
  - `DatasetStore.put(..., representation: str = "bucket_agg", quantile: float | None = None, n_min: int | None = None)`
  - `TelemetryService.query`: expands `$__rate_interval` for every expression; refuses aggregated percentiles with `SourceError(problem, hint=QUANTILE_HINT)`; quantile expressions produce `representation="quantile"` datasets with `count = n` (or `1` when n is unknown, `n_min=None`)
  - `TelemetryService.panel_data`: quantile datasets are returned at their own step (no `lod`)

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_service_quantile.py
import pytest

from telemetry_nerd.sources.base import SourceError
from tests.unit.fakes import FakeSource, make_service

Q = 'histogram_quantile(0.95, sum by (r) (rate(lat{s="c"}[5m])))'


def svc_with(tmp_path, n=250.0, q=0.42):
    src = FakeSource(values={"histogram_count": n, "histogram_quantile": q})
    return make_service(tmp_path, src), src


async def test_quantile_dataset_carries_n_and_is_not_rolled_up(tmp_path):
    svc, src = svc_with(tmp_path)
    out = await svc.query(Q, "now-2h", "now-1h", step="1m")
    meta, result = svc.datasets.get(out["dataset"])
    assert meta.representation == "quantile"
    assert meta.quantile == 0.95 and meta.n_min == 200
    rows = result.buckets.to_pylist()
    assert {r["count"] for r in rows} == {250}
    assert {r["avg"] for r in rows} == {0.42}
    assert any("histogram_count" in e for e in src.value_exprs)
    assert src.calls >= 2  # values + counts, never the rollup fetch


async def test_aggregated_percentile_is_refused_with_hint(tmp_path):
    svc, _ = svc_with(tmp_path)
    with pytest.raises(SourceError) as e:
        await svc.query(f"avg({Q})", "now-2h", "now-1h", step="1m")
    assert "percentile" in str(e.value)
    assert "histogram first" in (e.value.hint or "")


async def test_rate_interval_is_expanded_for_any_expression(tmp_path):
    src = FakeSource(resolution_ms=20_000)
    svc = make_service(tmp_path, src)
    out = await svc.query("sum(rate(x[$__rate_interval]))", "now-2h", "now-1h", step="30s")
    meta, _ = svc.datasets.get(out["dataset"])
    assert meta.expr == "sum(rate(x[80s]))"


async def test_unknown_n_keeps_values_and_flags(tmp_path):
    src = FakeSource(values={"histogram_quantile": 0.3})
    svc = make_service(tmp_path, src)
    mixed = "histogram_quantile(0.9, sum by (le) (rate(a_bucket[1m])) + sum by (le) (rate(b_bucket[5m])))"
    out = await svc.query(mixed, "now-2h", "now-1h", step="1m")
    meta, _ = svc.datasets.get(out["dataset"])
    assert meta.representation == "quantile" and meta.n_min is None
    assert "n_unknown" in out["summary"]["caveats"]


async def test_panel_data_never_rebuckets_quantiles(tmp_path):
    svc, _ = svc_with(tmp_path)
    out = await svc.query(Q, "now-6h", "now-1h", step="15s")
    panel = svc.show(out["dataset"], "p95 by r?").panel
    data = svc.panel_data(panel.id, width_px=100)  # 1201 buckets >> 100 px
    assert data["effective_step_ms"] == 15_000
    assert data["dataset"]["n_min"] == 200
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_service_quantile.py -q`
Expected: FAIL (`representation` is `bucket_agg`, no refusal, etc.)

- [ ] **Step 3: Implement**

`datasets/store.py`: add fields to `DatasetMeta` after `partial`:

```python
    quantile: float | None = None  # q of a quantile dataset
    n_min: int | None = None  # observations per bucket for a meaningful quantile
```

and extend `put` with keyword arguments `representation: str = "bucket_agg", quantile: float | None = None, n_min: int | None = None`, passing them into `DatasetMeta(...)`.

`core/service.py` — imports:

```python
from telemetry_nerd.analysis.exprkind import QUANTILE_HINT, analyze, expand, min_samples
from telemetry_nerd.analysis.quantile import attach_counts
```

In `query`, after `rng = rng.align(step_ms)` and the bucket-limit check, replace the fetch/put block with:

```python
        expr = expand(expr, step_ms, src.resolution_ms)
        info = analyze(expr)
        if info.problem:
            raise SourceError(info.problem, hint=QUANTILE_HINT)
        representation, q, n_min = "bucket_agg", None, None
        if info.quantile is None:
            result = await self.cache.get(
                src.identity, expr, rng, step_ms, lambda r: src.fetch(expr, r, step_ms)
            )
        else:
            qx = info.quantile
            representation, q = "quantile", qx.q
            result = await self.cache.get(
                src.identity, f"values|{expr}", rng, step_ms,
                lambda r: src.fetch_values(expr, r, step_ms),
            )
            if qx.count_expr is not None:
                count_expr = qx.count_expr
                counts = await self.cache.get(
                    src.identity, f"values|{count_expr}", rng, step_ms,
                    lambda r: src.fetch_values(count_expr, r, step_ms),
                )
                result = attach_counts(result, counts)
                n_min = min_samples(q) if q is not None else None
        meta = self.datasets.put(
            source=src.name,
            expr=expr,
            rng=rng,
            step_ms=step_ms,
            resolution_ms=src.resolution_ms,
            result=result,
            representation=representation,
            quantile=q,
            n_min=n_min,
        )
```

(keep the existing `summary = summarize(...)`, event append and return). Note `summarize` is extended in Task 4; until then `n_unknown` is not emitted — run only the first, second, third and fifth tests in this task:

Run: `uv run pytest tests/unit/test_service_quantile.py -q -k "not unknown_n"`

In `panel_data`, replace the `lod` call:

```python
        if meta.representation == "quantile":
            # never re-aggregate percentiles over time: serve at their own step
            table, effective_step = result.buckets, meta.step_ms
        else:
            table, effective_step = lod(
                result.buckets, meta.step_ms, TimeRange(meta.start_ms, meta.end_ms), width_px
            )
```

- [ ] **Step 4: Run tests**

Run: `just test && just lint`
Expected: PASS (except `test_unknown_n_keeps_values_and_flags`, which passes after Task 4 — mark nothing as xfail; just note it).

If `just test` must be fully green per commit, temporarily commit Task 3 and Task 4 together.

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/datasets/store.py src/telemetry_nerd/core/service.py tests/unit/test_service_quantile.py
git commit -m "fix(service): evaluate quantiles per bucket with n, never roll up or rebucket (4ok.2)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Quantile summaries: n, meaningful buckets, caveats

**Files:**
- Modify: `src/telemetry_nerd/core/summary.py`
- Test: `tests/unit/test_summary.py` (append)

**Interfaces:**
- Consumes: `DatasetMeta.representation/quantile/n_min` (Task 3)
- Produces: for `representation == "quantile"` the summary has `quantile`, `n_min`, and per series `{labels, min, max, mean: None, n_total, buckets, meaningful_buckets, gaps}`; `min`/`max` range over meaningful buckets only (all buckets with data when `n_min` is None); caveats add `low_count` (some bucket with data has `n < n_min`) and `n_unknown` (`n_min` is None). Never a count-weighted mean of percentiles.

- [ ] **Step 1: Write failing tests** (append to `tests/unit/test_summary.py`; reuse its existing imports/helpers — check them with `sed -n 1,40p tests/unit/test_summary.py`; the test below builds its own inputs)

```python
def _quantile_inputs(counts, values, n_min=200):
    import pyarrow as pa

    from telemetry_nerd.datasets.store import DatasetMeta
    from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult

    ts = [1_000 + 60_000 * i for i in range(len(values))]
    buckets = pa.table(
        {"ts_ms": ts, "series_id": ["a"] * len(ts), "avg": values, "min": values,
         "max": values, "count": counts},
        schema=BUCKET_SCHEMA,
    )
    series = pa.table({"series_id": ["a"], "labels": ['{"r":"x"}']}, schema=SERIES_SCHEMA)
    meta = DatasetMeta(
        id="d1", source="s", expr="histogram_quantile(0.95, x)", start_ms=ts[0],
        end_ms=ts[-1], step_ms=60_000, resolution_ms=15_000, representation="quantile",
        quantile=0.95, n_min=n_min,
    )
    return meta, FetchResult(buckets, series)


def test_quantile_summary_reports_n_and_never_averages():
    meta, result = _quantile_inputs([10, 300, 500, 12], [34.0, 0.6, 0.7, 0.2])
    s = summarize(meta, result, now_ms=10**13, settle_ms=0)
    [row] = s["series"]
    assert s["quantile"] == 0.95 and s["n_min"] == 200
    assert row["mean"] is None
    assert row["n_total"] == 822
    assert (row["meaningful_buckets"], row["buckets"]) == (2, 4)
    assert (row["min"], row["max"]) == (0.6, 0.7)  # the n=10 spike is not a meaningful p95
    assert "low_count" in s["caveats"]


def test_quantile_summary_without_n():
    meta, result = _quantile_inputs([1, 1], [0.3, 0.4], n_min=None)
    s = summarize(meta, result, now_ms=10**13, settle_ms=0)
    assert "n_unknown" in s["caveats"]
    assert (s["series"][0]["min"], s["series"][0]["max"]) == (0.3, 0.4)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_summary.py -q`
Expected: FAIL (`KeyError: 'quantile'`)

- [ ] **Step 3: Implement** — in `summarize`, right after the line `expected = (meta.end_ms - meta.start_ms) // meta.step_ms + 1`, add:

```python
    if meta.representation == "quantile":
        return _summarize_quantile(meta, result, base, caveats, expected, top)
```

and, right after `base = {...}` is built (before the empty-dataset early return), add `if meta.representation == "quantile": base |= {"quantile": meta.quantile, "n_min": meta.n_min}`. New function:

```python
def _summarize_quantile(
    meta: DatasetMeta, result: FetchResult, base: dict, caveats: list[str], expected: int, top: int
) -> dict:
    """Percentiles are reported per bucket with their n; never averaged (spec §1.2)."""
    labels = {r["series_id"]: json.loads(r["labels"]) for r in result.series.to_pylist()}
    df = pl.from_arrow(result.buckets).with_columns(pl.col("avg").fill_nan(None))
    has_value = pl.col("avg").is_not_null() & (pl.col("count") > 0)
    meaningful = has_value & (pl.col("count") >= (meta.n_min or 0))
    per = (
        df.group_by("series_id")
        .agg(
            pl.col("avg").filter(meaningful).min().alias("min"),
            pl.col("avg").filter(meaningful).max().alias("max"),
            pl.col("count").sum().alias("n_total"),
            has_value.sum().alias("buckets"),
            meaningful.sum().alias("meaningful_buckets"),
        )
        .with_columns(
            pl.max_horizontal(pl.lit(expected) - pl.col("buckets"), pl.lit(0)).alias("gaps")
        )
        .sort("n_total", descending=True)
    )
    if meta.n_min is None:
        caveats.append("n_unknown")
    elif (per["meaningful_buckets"] < per["buckets"]).any():
        caveats.append("low_count")
    if per["gaps"].sum() > 0:
        caveats.insert(0, "gaps")
    series = [
        {
            "labels": labels.get(r["series_id"], {}),
            "min": _round(r["min"]),
            "max": _round(r["max"]),
            "mean": None,
            "n_total": int(r["n_total"]),
            "buckets": int(r["buckets"]),
            "meaningful_buckets": int(r["meaningful_buckets"]),
            "gaps": int(r["gaps"]),
        }
        for r in per.head(top).to_dicts()
    ]
    return {
        **base,
        "series_count": per.height,
        "series": series,
        "more_series": max(0, per.height - top),
        "caveats": caveats,
    }
```

Note: when `n_min` is None (n unknown, count placeholder 1), `meaningful` equals `has_value`, so min/max cover all buckets with values.

- [ ] **Step 4: Run tests**

Run: `just test && just lint`
Expected: PASS (including `test_service_quantile.py::test_unknown_n_keeps_values_and_flags`)

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/core/summary.py tests/unit/test_summary.py
git commit -m "feat(summary): quantile summaries report n and meaningful buckets, never a mean (4ok.1)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Evidence rule + UI rendering

**Files:**
- Modify: `src/telemetry_nerd/workspace/models.py` (`StatisticRef`)
- Modify: `ui/src/lib/api.ts`, `ui/src/chart/toUplot.ts`, `ui/src/Panel.svelte`
- Test: `tests/unit/test_models.py` (append), `ui/src/chart/toUplot.test.ts` (append)

**Interfaces:**
- Consumes: `min_samples` (Task 1); `dataset.representation`, `dataset.n_min` in `/api/panels/{id}/data` (Task 3)
- Produces:
  - `StatisticRef` with a percentile name (`p50`, `p95`, `p99.9`, `median`, `quantile…`, `percentile…`) requires `params.q` when not inferable and `params.n` (int) with `n ≥ min_samples(q)`; messages start `percentile_without_n:` / `percentile_not_meaningful:`
  - `toUplot(series, grid?, opts?: { quantile?: boolean; nMin?: number | null })`: in quantile mode each series becomes a solid line where `count ≥ nMin` and a dashed faded line labelled `… (n<nMin)` where below; no min/max series or band

- [ ] **Step 1: Failing Python tests** (append to `tests/unit/test_models.py`; it already imports `pytest`, `ValidationError` and the models — check with `sed -n 1,20p tests/unit/test_models.py` and add `StatisticRef` to the import if missing)

```python
def _stat(**kw):
    base = {
        "kind": "statistic", "dataset": "d1", "name": "p95", "value": 0.7,
        "interval": [0.6, 0.8], "method": "histogram_quantile",
    }
    return StatisticRef.model_validate(base | kw)


def test_percentile_statistic_requires_n():
    with pytest.raises(ValidationError, match="percentile_without_n"):
        _stat()


def test_percentile_statistic_requires_meaningful_n():
    with pytest.raises(ValidationError, match="percentile_not_meaningful"):
        _stat(params={"n": 13})
    assert _stat(params={"n": 2328}).params["n"] == 2328


def test_percentile_q_from_name_or_params():
    with pytest.raises(ValidationError, match="percentile_not_meaningful"):
        _stat(name="p99.9", params={"n": 2000})  # needs 10000
    with pytest.raises(ValidationError, match="params.q"):
        _stat(name="quantile", params={"n": 5000})
    assert _stat(name="quantile", params={"n": 5000, "q": 0.99})


def test_non_percentile_statistics_unaffected():
    assert _stat(name="mean_latency", params={})
```

- [ ] **Step 2: Implement** — in `workspace/models.py` add `import re` and `from telemetry_nerd.analysis.exprkind import min_samples`, then at module level:

```python
_PERCENTILE = re.compile(r"^(?:p(\d+(?:\.\d+)?)|(median)|quantile|percentile)\b", re.IGNORECASE)
```

and append to `StatisticRef._uncertainty` before `return self`:

```python
        m = _PERCENTILE.match(self.name.strip())
        if m:
            if m.group(1):
                q = float(m.group(1)) / 100
            elif m.group(2):
                q = 0.5
            else:
                q = self.params.get("q")
            if not isinstance(q, int | float) or not 0 < q < 1:
                raise ValueError("a quantile statistic needs params.q in (0, 1), e.g. 0.95")
            n = self.params.get("n")
            if not isinstance(n, int) or isinstance(n, bool):
                raise ValueError(
                    "percentile_without_n: a percentile needs params.n, the number of "
                    "observations behind it (e.g. histogram_count over the same window)"
                )
            need = min_samples(float(q))
            if n < need:
                raise ValueError(
                    f"percentile_not_meaningful: n={n} < {need} needed for q={q}; "
                    "report the count or the mean instead"
                )
```

Run: `uv run pytest tests/unit/test_models.py -q && just test`
Expected: PASS. If an existing test elsewhere builds a percentile-named statistic without `n` (check: `rg -n '"name": "p[0-9]' tests`), add `params={"n": <large>}` to it.

- [ ] **Step 3: Failing UI test** (append inside `describe("toUplot", ...)` in `ui/src/chart/toUplot.test.ts`)

```ts
  it("draws quantiles without envelope and fades low-n buckets", () => {
    const q: SeriesData = {
      id: "a", labels: { r: "x" }, ts: [1000, 2000, 3000],
      avg: [34, 0.6, 0.7], min: [34, 0.6, 0.7], max: [34, 0.6, 0.7], count: [13, 2328, 400],
    };
    const m = toUplot([q], undefined, { quantile: true, nMin: 200 });
    expect(m.series.length).toBe(3); // x + solid + faded
    expect(m.bands).toEqual([]);
    expect(m.data[1]).toEqual([null, 0.6, 0.7]);
    expect(m.data[2]).toEqual([34, null, null]);
    expect(String(m.series[2].label)).toContain("n<200");
  });

  it("quantile mode without nMin draws every value solid", () => {
    const q: SeriesData = {
      id: "a", labels: {}, ts: [1000], avg: [1], min: [1], max: [1], count: [1],
    };
    const m = toUplot([q], undefined, { quantile: true, nMin: null });
    expect(m.series.length).toBe(2);
    expect(m.data[1]).toEqual([1]);
  });
```

- [ ] **Step 4: Implement UI**

`ui/src/lib/api.ts` — extend `DatasetMeta` with `quantile?: number | null; n_min?: number | null;`.

`ui/src/chart/toUplot.ts` — change the signature and branch inside the per-series loop:

```ts
export interface ToUplotOpts { quantile?: boolean; nMin?: number | null }

export function toUplot(series: SeriesData[], grid?: Grid, opts: ToUplotOpts = {}): UplotModel {
  // ...existing setup unchanged...
  series.forEach((s, k) => {
    const color = PALETTE[k % PALETTE.length];
    const column = /* existing helper */;
    const name = seriesName(s.labels);
    if (opts.quantile) {
      // Percentiles are never aggregated: no envelope. Buckets with too few
      // observations are drawn faded so they are not read as real percentiles.
      const nMin = opts.nMin ?? null;
      const ok = s.avg.map((v, i) => (nMin === null || (s.count[i] ?? 0) >= nMin ? v : null));
      data.push(column(ok));
      uSeries.push({ label: name, stroke: color, width: 1.5, spanGaps: false });
      if (nMin !== null) {
        const low = s.avg.map((v, i) => ((s.count[i] ?? 0) < nMin ? v : null));
        data.push(column(low));
        uSeries.push({
          label: `${name} (n<${nMin}, not meaningful)`, stroke: rgba(color, 0.35), width: 1,
          dash: [4, 4], spanGaps: false,
        });
      }
      return;
    }
    // ...existing avg/min/max + band code unchanged...
  });
```

`ui/src/Panel.svelte` — pass options and adjust the footer:

```svelte
    const quantile = data.dataset.representation === "quantile";
    const model = toUplot(data.series, {
      start: data.dataset.start_ms, end: data.dataset.end_ms, step: data.effective_step_ms,
    }, { quantile, nMin: data.dataset.n_min ?? null });
```

and in the footer replace `{data.dataset.representation}, min/max envelope` with:

```svelte
      {#if data.dataset.representation === "quantile"}
        quantile per {fmtStep(data.effective_step_ms)} window (never aggregated){#if data.dataset.n_min}; faded: n &lt; {data.dataset.n_min}, not meaningful{:else}; n unknown{/if}
      {:else}
        {data.dataset.representation}, min/max envelope
      {/if}
```

- [ ] **Step 5: Run everything**

Run: `just test && just lint && just ui-test && just ui-check && just ui-build`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/telemetry_nerd/workspace/models.py tests/unit/test_models.py ui/src/lib/api.ts ui/src/chart/toUplot.ts ui/src/chart/toUplot.test.ts ui/src/Panel.svelte
git commit -m "feat: percentile evidence needs n; UI fades low-n quantile buckets (4ok.1)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Claude guidance, integration test, live check

**Files:**
- Modify: `src/telemetry_nerd/mcp/server.py` (`query` docstring, `INSTRUCTIONS`)
- Create: `tests/integration/test_quantile_vm.py`

- [ ] **Step 1: Guidance** — in `query`'s docstring add:

```
        Quantiles: write histogram_quantile(q, sum by (...) (rate(x[$__rate_interval])))
        as the WHOLE expression. It is evaluated per step (never rolled up) and each bucket
        carries n, the observations behind it; buckets with n < 10/(1-q) are flagged
        low_count. Wrapping a quantile in sum/avg/max/*_over_time is refused.
        $__rate_interval expands to max(4 x scrape interval, step + scrape interval).
```

and in `INSTRUCTIONS` under the percentile bullets add: `Write $__rate_interval as the rate window for quantiles so each value covers one display step.`

- [ ] **Step 2: Integration test** (classic histogram through VictoriaMetrics)

```python
# tests/integration/test_quantile_vm.py
import pytest

from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.devtools.synthetic import exposition, push
from telemetry_nerd.model.time import now_ms
from telemetry_nerd.sources.spec import SourceSpec

pytestmark = pytest.mark.integration

# 10 obs/s: 90% <= 0.1s, 9% in (0.1, 1], 1% in (1, 10]
CUMULATIVE = {"0.1": 9.0, "1": 9.9, "10": 10.0, "+Inf": 10.0}


async def test_classic_histogram_p95_with_n(vm_url, tmp_path):
    t0 = (now_ms() - 2 * 3_600_000) // 60_000 * 60_000
    text = "".join(
        exposition(
            "tn_it_lat_seconds_bucket",
            {"le": le, "job": "it"},
            [(t0 + i * 15_000, rate * 15 * i) for i in range(480)],
        )
        for le, rate in CUMULATIVE.items()
    )
    push(vm_url, text)
    svc = build_service(Settings(data_dir=tmp_path / "d", source_url="http://127.0.0.1:9"))
    await svc.source_connect(SourceSpec(name="vm", url=vm_url, flavor="victoriametrics"))

    expr = "histogram_quantile(0.95, sum by (le) (rate(tn_it_lat_seconds_bucket[$__rate_interval])))"
    out = await svc.query(expr, "now-100m", "now-40m", step="1m", source="vm")
    s = out["summary"]
    assert s["representation"] == "quantile" and s["n_min"] == 200
    [row] = s["series"]
    # rate window = max(4 x 15s, 60s + 15s) = 75s -> n ~ 10/s x 75s = 750 per bucket
    assert row["meaningful_buckets"] == row["buckets"] > 0
    assert 600 <= row["n_total"] / row["buckets"] <= 900
    assert 0.1 < row["min"] <= row["max"] <= 1.0  # p95 lies in the (0.1, 1] bucket

    with pytest.raises(Exception, match="percentile"):
        await svc.query(f"avg({expr})", "now-100m", "now-40m", step="1m", source="vm")
```

Run: `uv run pytest -m integration tests/integration/test_quantile_vm.py -q`
Expected: PASS. If the `n` band is off because VictoriaMetrics extrapolates `rate` differently, widen it with a comment explaining why — never relax the "meaningful"/range assertions.

- [ ] **Step 3: Full suite**

Run: `just test && just lint && uv run pytest -m integration -q`
Expected: PASS

- [ ] **Step 4: Live check against Grafana Play (polite, through the running daemon)**

Restart the dev daemon once to load the code, then via MCP (`scripts/mcp_call.py --url http://127.0.0.1:7070/mcp`):

1. `query(expr='histogram_quantile(0.95, sum by (cloud_region) (rate(traces_spanmetrics_latency{service="checkoutservice", span_kind="SPAN_KIND_SERVER"}[$__rate_interval])))', start="2026-10-01T09:20:00Z", end="2026-10-01T11:00:00Z", step="1m", source="play")` → `representation: quantile`, `n_min: 200`, ap-south-1 `meaningful_buckets` only around 10:04–10:10, `low_count` caveat, max over meaningful buckets ≈ 1 s (not 34 s).
2. `query(expr="avg(" + <same> + ")")` → refused with the hint.
3. `show(...)` → panel draws ap-south-1's 10:02 point faded/dashed, footer says "faded: n < 200".

- [ ] **Step 5: Commit and close**

```bash
git add src/telemetry_nerd/mcp/server.py tests/integration/test_quantile_vm.py
git commit -m "test(quantile): classic histogram p95 with n end to end; Claude guidance (4ok.1, 4ok.2)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
bd close telemetry-nerd-4ok.2 --reason "Quantiles evaluated per bucket (no rollup/LOD), aggregation refused; integration + Play check"
bd close telemetry-nerd-4ok.1 --reason "n per bucket, low_count/n_unknown caveats, faded UI, percentile evidence needs n"
```

---

## Acceptance

- `histogram_quantile` / `quantile_over_time` / summary quantile selectors are never rolled up, never `*_over_time`-wrapped by us, never rebucketed by LOD.
- Any expression that aggregates a percentile (across series or time) is refused with a hint showing the correct form.
- Every quantile bucket carries `n` when derivable; summaries report `n_total`, `meaningful_buckets/buckets`, `low_count` / `n_unknown`; no mean of percentiles anywhere.
- The UI fades buckets with `n < n_min` and says so in the footer.
- `finding_create` rejects a percentile statistic without `params.n ≥ n_min`.
- Grafana Play: the ap-south-1 34 s "p95" shows as a faded n=13 point; meaningful p95 during the burst ≈ 0.6–1.1 s.
