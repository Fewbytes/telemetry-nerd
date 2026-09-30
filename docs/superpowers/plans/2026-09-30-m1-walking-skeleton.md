# M1 Walking Skeleton Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task is also a beads issue (`bd ready`); the issue body points back here.

**Goal:** Prove the architecture end to end: a PromQL/MetricsQL expression is fetched from VictoriaMetrics as min/max/avg/count buckets, cached in DuckDB, stored as an immutable dataset, rendered as a `line+envelope` panel that answers an explicit question in a browser UI, reachable from Claude Code through MCP tools `query` and `show`.

**Architecture:** One Python asyncio process (`telemetry-nerd serve`) runs an MCP stdio server and a Starlette HTTP/WebSocket server side by side. Both call the same `TelemetryService`. Data flows `PromQLSource → SeriesCache (DuckDB, chunked) → DatasetStore (DuckDB) → lod() → UI`. Workspace objects (M1: panels only) live in SQLite. The UI is a Vite + React + uPlot app served by the server.

**Tech Stack:** Python ≥3.12, uv, httpx, pyarrow, polars, duckdb, pydantic v2, starlette, uvicorn, mcp (Python SDK, FastMCP), pytest, pytest-asyncio, hypothesis, respx, testcontainers; TypeScript, React, Vite, uPlot, vitest, Playwright; VictoriaMetrics; just.

**Spec:** `docs/superpowers/specs/2026-09-30-telemetry-nerd-mvp-design.md` (read §1.2 principles before any task).

## Global Constraints

- Python ≥3.12; manage everything with `uv` (`uv add`, `uv run`). Never call `pip` directly.
- Package lives in `src/telemetry_nerd/` (src layout). Tests in `tests/unit/` (no Docker) and `tests/integration/` (Docker, marked `@pytest.mark.integration`).
- All timestamps are **int64 epoch milliseconds** (`ts_ms`). Bucket timestamp = bucket **end**; a bucket at `t` with step `s` covers `(t - s, t]`.
- Bucket Arrow schema is exactly `BUCKET_SCHEMA` (`ts_ms int64, series_id string, avg float64, min float64, max float64, count int64`). Never collapse a bucket to a single value.
- Never downsample by sampling. Re-aggregation is min-of-min, max-of-max, sum-of-count, count-weighted mean.
- Errors are explicit and typed (`SourceError` + `hint`). Never truncate or drop data silently.
- Panels require a non-empty question. No chart without one.
- MCP runs on **stdio**: nothing may print to stdout except the MCP protocol. Logging goes to stderr.
- Main git branch is `master`. Commit after every task; message ends with the trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` (use the model you actually are).
- Use `rg`/`fd` instead of `grep`/`find`. Use `just` recipes, not ad-hoc scripts, where a recipe exists.
- Close the task's beads issue when done: `bd close <id> --reason "<one line>"`.

## File Structure

```
pyproject.toml                         # uv project, deps, pytest config
justfile                               # dev recipes
.claude-plugin/plugin.json             # Claude Code plugin manifest
.mcp.json                              # MCP server registration (stdio)
deploy/dev/compose.yml                 # VictoriaMetrics for dev + e2e
scripts/seed_synthetic.py              # push synthetic demo series into VM
src/telemetry_nerd/
  __init__.py
  cli.py                               # `telemetry-nerd serve`
  config.py                            # Settings
  model/time.py                        # durations, time parsing, TimeRange, now_ms
  model/series.py                      # BUCKET_SCHEMA, SERIES_SCHEMA, series_id, FetchResult
  model/errors.py                      # NotFound
  sources/base.py                      # Source protocol, Limits, SourceError family
  sources/promql.py                    # PromQL/MetricsQL adapter
  datasets/db.py                       # DuckDB schema + helpers
  datasets/cache.py                    # SeriesCache (chunked, settle/TTL)
  datasets/store.py                    # DatasetStore (immutable datasets)
  analysis/resample.py                 # rebucket(), lod()
  charts/spec.py                       # ChartSpec, validate(), auto_spec()
  workspace/store.py                   # WorkspaceStore (SQLite): ids, panels
  core/events.py                       # EventBus
  core/summary.py                      # summarize() → compact dict for Claude
  core/service.py                      # TelemetryService, auto_step, ChartRejected
  core/bootstrap.py                    # build_service(settings)
  api/app.py                           # Starlette HTTP + WS
  mcp/server.py                        # FastMCP tools: query, show
  devtools/synthetic.py                # exposition text + push to VM
tests/unit/...                         # one test file per module
tests/integration/conftest.py          # VM testcontainer fixture
tests/integration/test_promql_vm.py
ui/                                    # Vite + React + uPlot
  src/api.ts, src/App.tsx, src/Panel.tsx, src/chart/toUplot.ts, src/chart/toUplot.test.ts
  e2e/global-setup.ts, e2e/skeleton.spec.ts, playwright.config.ts
```

Scope notes (deliberate M1 simplifications, each revisited in later milestones):
- `Source.fetch` returns a whole `FetchResult` per call; streaming happens at chunk granularity through the cache. Spec §4.1's `AsyncIterator` form arrives with the sandbox (M5).
- No catalog yet (M3): y-axis uses `range_mode="data"` and the UI labels it as such. Units are unknown → validator warning.
- Only the `line+envelope` mark and the series-budget rule exist.
- No event log persistence (M2): `EventBus` is in-memory pub/sub.
- MCP over stdio only; streamable HTTP arrives in M2.

---

### Task 1: Project scaffold and core time/series model

**Files:**
- Create: `pyproject.toml`, `justfile`, `src/telemetry_nerd/__init__.py`, `src/telemetry_nerd/model/__init__.py`, `src/telemetry_nerd/model/time.py`, `src/telemetry_nerd/model/series.py`, `src/telemetry_nerd/model/errors.py`
- Modify: `.gitignore` (append)
- Test: `tests/unit/test_time.py`, `tests/unit/test_series.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `model.time`: `parse_duration(text: str) -> int`, `format_duration(ms: int) -> str`, `parse_time(text: str, now_ms: int) -> int`, `now_ms() -> int`, `iso(ms: int) -> str`, `TimeRange(start_ms: int, end_ms: int)` (frozen dataclass, `.align(step_ms) -> TimeRange`).
  - `model.series`: `BUCKET_SCHEMA`, `SERIES_SCHEMA` (pyarrow schemas), `series_id(source: str, labels: dict[str, str]) -> str`, `labels_json(labels) -> str`, `FetchResult(buckets: pa.Table, series: pa.Table)` dataclass, `empty_result() -> FetchResult`.
  - `model.errors`: `NotFound(Exception)`.

- [ ] **Step 1: Create the uv project**

```toml
# pyproject.toml
[project]
name = "telemetry-nerd"
version = "0.1.0"
description = "Evidence-first telemetry analysis workspace for Claude"
requires-python = ">=3.12"
dependencies = []

[project.scripts]
telemetry-nerd = "telemetry_nerd.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/telemetry_nerd"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
testpaths = ["tests"]
markers = ["integration: requires Docker"]
addopts = "-m 'not integration'"

[tool.ruff]
line-length = 100
target-version = "py312"
```

Then add dependencies with uv (lets uv pick current versions):

```bash
uv add httpx pyarrow polars duckdb pydantic starlette uvicorn mcp anyio
uv add --dev pytest pytest-asyncio hypothesis respx testcontainers ruff
```

Create empty `src/telemetry_nerd/__init__.py`, `src/telemetry_nerd/model/__init__.py`, `tests/__init__.py`, and `tests/unit/__init__.py` (tests import shared fakes as `tests.unit.fakes`).

Append to `.gitignore`:

```
# python
.venv/
__pycache__/
*.pyc
# telemetry-nerd runtime data
.tn-data/
# ui
ui/node_modules/
ui/dist/
ui/test-results/
ui/playwright-report/
```

- [ ] **Step 2: Create the justfile**

```just
set shell := ["bash", "-cu"]

default: test

test:
    uv run pytest

test-integration:
    uv run pytest -m integration

lint:
    uv run ruff check . && uv run ruff format --check .

fmt:
    uv run ruff format . && uv run ruff check --fix .
```

(Later tasks append recipes.)

- [ ] **Step 3: Write failing tests**

```python
# tests/unit/test_time.py
import pytest

from telemetry_nerd.model.time import (
    TimeRange,
    format_duration,
    iso,
    parse_duration,
    parse_time,
)

NOW = 1_700_000_000_000


def test_parse_duration_units():
    assert parse_duration("250ms") == 250
    assert parse_duration("15s") == 15_000
    assert parse_duration("5m") == 300_000
    assert parse_duration("2h") == 7_200_000
    assert parse_duration("1d") == 86_400_000
    assert parse_duration("1w") == 604_800_000


def test_parse_duration_rejects_garbage():
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration("5 minutes")


@pytest.mark.parametrize("text", ["250ms", "15s", "90s", "1m", "2h", "1d", "1w"])
def test_format_duration_roundtrip(text):
    assert format_duration(parse_duration(text)) == text


def test_parse_time_forms():
    assert parse_time("now", NOW) == NOW
    assert parse_time("now-1h", NOW) == NOW - 3_600_000
    assert parse_time("1699990000000", NOW) == 1_699_990_000_000
    assert parse_time("2023-11-14T22:13:20+00:00", NOW) == 1_700_000_000_000


def test_parse_time_requires_timezone():
    with pytest.raises(ValueError, match="timezone"):
        parse_time("2023-11-14T22:13:20", NOW)


def test_time_range_rejects_empty():
    with pytest.raises(ValueError):
        TimeRange(10, 10)


def test_time_range_align_expands_to_step_multiples():
    assert TimeRange(61_000, 179_000).align(60_000) == TimeRange(60_000, 180_000)
    assert TimeRange(60_000, 180_000).align(60_000) == TimeRange(60_000, 180_000)


def test_iso():
    assert iso(1_700_000_000_000) == "2023-11-14T22:13:20+00:00"
```

```python
# tests/unit/test_series.py
from telemetry_nerd.model.series import (
    BUCKET_SCHEMA,
    SERIES_SCHEMA,
    empty_result,
    labels_json,
    series_id,
)


def test_series_id_is_stable_and_order_independent():
    a = series_id("vm", {"job": "api", "instance": "a"})
    b = series_id("vm", {"instance": "a", "job": "api"})
    assert a == b
    assert len(a) == 16
    int(a, 16)  # hex


def test_series_id_depends_on_source():
    assert series_id("vm", {"x": "1"}) != series_id("prom", {"x": "1"})


def test_labels_json_is_canonical():
    assert labels_json({"b": "2", "a": "1"}) == '{"a":"1","b":"2"}'


def test_empty_result_has_schemas():
    r = empty_result()
    assert r.buckets.schema == BUCKET_SCHEMA
    assert r.series.schema == SERIES_SCHEMA
    assert r.buckets.num_rows == 0
```

- [ ] **Step 4: Run tests, verify they fail**

Run: `uv run pytest tests/unit -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'telemetry_nerd.model.time'`

- [ ] **Step 5: Implement**

```python
# src/telemetry_nerd/model/time.py
"""Time primitives. All timestamps are int64 epoch milliseconds."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

_DURATION = re.compile(r"^(\d+)(ms|s|m|h|d|w)$")
_UNIT_MS = {
    "ms": 1,
    "s": 1_000,
    "m": 60_000,
    "h": 3_600_000,
    "d": 86_400_000,
    "w": 604_800_000,
}


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def parse_duration(text: str) -> int:
    match = _DURATION.match(text.strip())
    if not match:
        raise ValueError(f"invalid duration {text!r}; use e.g. 15s, 5m, 1h, 2d")
    return int(match.group(1)) * _UNIT_MS[match.group(2)]


def format_duration(ms: int) -> str:
    for unit in ("w", "d", "h", "m", "s"):
        size = _UNIT_MS[unit]
        if ms >= size and ms % size == 0:
            return f"{ms // size}{unit}"
    return f"{ms}ms"


def parse_time(text: str, now_ms: int) -> int:
    """Parse `now`, `now-<duration>`, epoch milliseconds, or ISO-8601 with timezone."""
    value = text.strip()
    if value == "now":
        return now_ms
    if value.startswith("now-"):
        return now_ms - parse_duration(value[4:])
    if value.isdigit():
        return int(value)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp {text!r} must include a timezone, e.g. +00:00")
    return int(parsed.timestamp() * 1000)


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class TimeRange:
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.end_ms <= self.start_ms:
            raise ValueError(f"time range end ({self.end_ms}) must be after start ({self.start_ms})")

    def align(self, step_ms: int) -> TimeRange:
        """Expand outward to multiples of step."""
        start = (self.start_ms // step_ms) * step_ms
        end = -(-self.end_ms // step_ms) * step_ms
        return TimeRange(start, end)
```

```python
# src/telemetry_nerd/model/series.py
"""Series identity and the bucket representation shared by every layer."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import pyarrow as pa

BUCKET_SCHEMA = pa.schema(
    [
        ("ts_ms", pa.int64()),
        ("series_id", pa.string()),
        ("avg", pa.float64()),
        ("min", pa.float64()),
        ("max", pa.float64()),
        ("count", pa.int64()),
    ]
)

SERIES_SCHEMA = pa.schema([("series_id", pa.string()), ("labels", pa.string())])


def labels_json(labels: dict[str, str]) -> str:
    return json.dumps(dict(sorted(labels.items())), separators=(",", ":"))


def series_id(source: str, labels: dict[str, str]) -> str:
    canonical = json.dumps(
        {"source": source, "labels": dict(sorted(labels.items()))}, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class FetchResult:
    buckets: pa.Table  # BUCKET_SCHEMA
    series: pa.Table  # SERIES_SCHEMA


def empty_result() -> FetchResult:
    return FetchResult(BUCKET_SCHEMA.empty_table(), SERIES_SCHEMA.empty_table())
```

```python
# src/telemetry_nerd/model/errors.py
class NotFound(Exception):
    """A workspace object or dataset id does not exist."""
```

- [ ] **Step 6: Run tests, verify they pass**

Run: `uv run pytest tests/unit -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock justfile .gitignore src tests
git commit -m "feat: project scaffold with time and series model"
```

---

### Task 2: Source interface and PromQL/MetricsQL adapter

**Files:**
- Create: `src/telemetry_nerd/sources/__init__.py`, `src/telemetry_nerd/sources/base.py`, `src/telemetry_nerd/sources/promql.py`
- Test: `tests/unit/test_promql.py`

**Interfaces:**
- Consumes: `TimeRange`, `format_duration` (Task 1); `BUCKET_SCHEMA`, `SERIES_SCHEMA`, `series_id`, `labels_json`, `FetchResult` (Task 1).
- Produces:
  - `sources.base`: `SourceError(message, hint=None)` with `.hint`; subclasses `LimitExceeded`, `SourceUnavailable`; `Limits(max_series=500, max_points=2_000_000, timeout_s=30.0)`; `Source` Protocol with attributes `name: str`, `resolution_ms: int` and `async fetch(expr: str, rng: TimeRange, step_ms: int) -> FetchResult`.
  - `sources.promql`: `is_selector(expr) -> bool`, `PromQLSource(name, base_url, *, flavor="victoriametrics"|"prometheus", resolution_ms=15_000, limits=Limits(), client=None)` with `.build_queries(expr, step_ms) -> dict[str, str]` and `async .fetch(...)`, constant `MAX_STEPS_PER_QUERY = 11_000`.

**Why rollups:** a plain `query_range` samples one point per step and silently drops everything between. We fetch the whole bucket (min/max/avg/count), so peaks survive (spec §4.1). VictoriaMetrics `rollup(m[w])` returns three series per input series labelled `rollup="min"|"max"|"avg"`; `count_over_time` gives the count. For non-selector expressions we use a subquery `(<expr>)[<step>:<resolution>]`. Plain Prometheus uses four `*_over_time` queries.

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_promql.py
import httpx
import pytest
import respx

from telemetry_nerd.model.series import series_id
from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.base import Limits, LimitExceeded, SourceError, SourceUnavailable
from telemetry_nerd.sources.promql import PromQLSource, is_selector

BASE = "http://vm.test"
RNG = TimeRange(1_700_000_040_000, 1_700_000_160_000)
ROUTE = {"host": "vm.test", "path": "/api/v1/query_range"}


def matrix(result):
    return {"status": "success", "data": {"resultType": "matrix", "result": result}}


def test_is_selector():
    assert is_selector("up")
    assert is_selector('http_requests_total{job="api", code=~"5.."}')
    assert not is_selector("rate(x[1m])")
    assert not is_selector("sum(up)")


def test_build_queries_vm_selector_uses_raw_window():
    src = PromQLSource("vm", BASE)
    assert src.build_queries("up", 60_000) == {
        "rollup": "rollup(up[1m])",
        "count": "count_over_time(up[1m])",
    }


def test_build_queries_vm_expression_uses_subquery():
    src = PromQLSource("vm", BASE, resolution_ms=15_000)
    q = src.build_queries("sum(rate(x[1m]))", 60_000)
    assert q["rollup"] == "rollup((sum(rate(x[1m])))[1m:15s])"
    assert q["count"] == "count_over_time((sum(rate(x[1m])))[1m:15s])"


def test_build_queries_prometheus_flavor():
    src = PromQLSource("prom", BASE, flavor="prometheus")
    assert src.build_queries("up", 60_000) == {
        "avg": "avg_over_time(up[1m])",
        "min": "min_over_time(up[1m])",
        "max": "max_over_time(up[1m])",
        "count": "count_over_time(up[1m])",
    }


@respx.mock
async def test_fetch_pivots_rollup_and_count_into_buckets():
    def responder(request):
        if request.url.params["query"].startswith("rollup("):
            return httpx.Response(
                200,
                json=matrix(
                    [
                        {"metric": {"instance": "a", "rollup": "min"},
                         "values": [[1700000100, "1"], [1700000160, "2"]]},
                        {"metric": {"instance": "a", "rollup": "max"},
                         "values": [[1700000100, "5"], [1700000160, "9"]]},
                        {"metric": {"instance": "a", "rollup": "avg"},
                         "values": [[1700000100, "3"], [1700000160, "4"]]},
                    ]
                ),
            )
        return httpx.Response(
            200,
            json=matrix([{"metric": {"instance": "a"},
                          "values": [[1700000100, "4"], [1700000160, "4"]]}]),
        )

    respx.get(**ROUTE).mock(side_effect=responder)
    res = await PromQLSource("vm", BASE).fetch("up", RNG, 60_000)
    sid = series_id("vm", {"instance": "a"})
    assert res.buckets.to_pylist() == [
        {"ts_ms": 1_700_000_100_000, "series_id": sid, "avg": 3.0, "min": 1.0, "max": 5.0, "count": 4},
        {"ts_ms": 1_700_000_160_000, "series_id": sid, "avg": 4.0, "min": 2.0, "max": 9.0, "count": 4},
    ]
    assert res.series.to_pylist() == [{"series_id": sid, "labels": '{"instance":"a"}'}]


@respx.mock
async def test_fetch_sends_step_and_nocache():
    route = respx.get(**ROUTE).mock(return_value=httpx.Response(200, json=matrix([])))
    await PromQLSource("vm", BASE).fetch("up", RNG, 60_000)
    params = route.calls[0].request.url.params
    assert params["step"] == "60s"
    assert params["nocache"] == "1"
    assert params["start"] == "1700000040.000"


@respx.mock
async def test_series_limit_is_explicit_error():
    result = [{"metric": {"i": str(i), "rollup": "avg"}, "values": [[1700000100, "1"]]}
              for i in range(3)]
    respx.get(**ROUTE).mock(return_value=httpx.Response(200, json=matrix(result)))
    src = PromQLSource("vm", BASE, limits=Limits(max_series=2))
    with pytest.raises(LimitExceeded) as exc:
        await src.fetch("up", RNG, 60_000)
    assert "narrow" in exc.value.hint


async def test_too_many_steps_rejected_before_request():
    src = PromQLSource("vm", BASE)
    with pytest.raises(LimitExceeded) as exc:
        await src.fetch("up", TimeRange(0, 20_000 * 1_000), 1_000)
    assert "coarser step" in exc.value.hint


@respx.mock
async def test_query_error_is_surfaced():
    respx.get(**ROUTE).mock(
        return_value=httpx.Response(
            422, json={"status": "error", "errorType": "bad_data", "error": "parse error at 3"}
        )
    )
    with pytest.raises(SourceError, match="parse error at 3"):
        await PromQLSource("vm", BASE).fetch("up{", RNG, 60_000)


@respx.mock
async def test_connection_failure_is_unavailable():
    respx.get(**ROUTE).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(SourceUnavailable) as exc:
        await PromQLSource("vm", BASE).fetch("up", RNG, 60_000)
    assert exc.value.hint
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/unit/test_promql.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'telemetry_nerd.sources'`

- [ ] **Step 3: Implement**

Create empty `src/telemetry_nerd/sources/__init__.py`.

```python
# src/telemetry_nerd/sources/base.py
"""Source adapter contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from telemetry_nerd.model.series import FetchResult
from telemetry_nerd.model.time import TimeRange


class SourceError(Exception):
    """Typed source failure. `hint` tells Claude how to recover."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class LimitExceeded(SourceError):
    pass


class SourceUnavailable(SourceError):
    pass


@dataclass(frozen=True)
class Limits:
    max_series: int = 500
    max_points: int = 2_000_000
    timeout_s: float = 30.0


class Source(Protocol):
    name: str
    resolution_ms: int

    async def fetch(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult: ...
```

```python
# src/telemetry_nerd/sources/promql.py
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
from telemetry_nerd.sources.base import Limits, LimitExceeded, SourceError, SourceUnavailable

MAX_STEPS_PER_QUERY = 11_000
_SELECTOR = re.compile(r"^\s*[a-zA-Z_:][a-zA-Z0-9_:]*\s*(\{[^{}]*\})?\s*$")
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
        limits: Limits = Limits(),
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.flavor = flavor
        self.resolution_ms = resolution_ms
        self.limits = limits
        self._client = client or httpx.AsyncClient()

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
                if field not in _FIELDS:
                    continue
                sid = series_id(self.name, labels)
                labels_by_sid[sid] = labels
                for ts, value in item["values"]:
                    cells.setdefault((sid, round(float(ts) * 1000)), {})[field] = float(value)
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
                "count": [
                    int(cells[k]["count"]) if "count" in cells[k] else None for k in keys
                ],
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
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `uv run pytest tests/unit/test_promql.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/sources tests/unit/test_promql.py
git commit -m "feat: PromQL/MetricsQL adapter fetching rollup buckets"
```

---

### Task 3: Dev VictoriaMetrics, synthetic data, adapter integration test

**Files:**
- Create: `deploy/dev/compose.yml`, `src/telemetry_nerd/devtools/__init__.py`, `src/telemetry_nerd/devtools/synthetic.py`, `scripts/seed_synthetic.py`, `tests/integration/__init__.py`, `tests/integration/conftest.py`, `tests/integration/test_promql_vm.py`, `tests/unit/test_synthetic.py`
- Modify: `justfile` (append recipes)

**Interfaces:**
- Consumes: `PromQLSource` (Task 2), `TimeRange`, `now_ms` (Task 1).
- Produces:
  - `devtools.synthetic`: `exposition(metric: str, labels: dict[str, str], samples: Iterable[tuple[int, float]]) -> str`, `push(base_url: str, text: str) -> None`, `demo_text(start_ms: int, end_ms: int, interval_ms: int = 15_000, seed: int = 7) -> str`.
  - Constant `VM_IMAGE = "victoriametrics/victoria-metrics:v1.137.0"` in `tests/integration/conftest.py`, and fixture `vm_url` (session-scoped, str).
  - Demo metrics (used by E2E in Task 12): `tn_demo_latency_seconds{instance="a"|"b"|"c"}` (gauge; instance `c` spikes to 1.5 for 5 minutes at 2/3 of the range) and `tn_demo_requests_total{instance=...}` (counter).

- [ ] **Step 1: Compose file and recipes**

```yaml
# deploy/dev/compose.yml
services:
  victoriametrics:
    image: victoriametrics/victoria-metrics:v1.137.0
    command:
      - -retentionPeriod=100y
      - -search.latencyOffset=0s
      - -dedup.minScrapeInterval=1ms
    ports:
      - "8428:8428"
    volumes:
      - vmdata:/victoria-metrics-data
volumes:
  vmdata: {}
```

Append to `justfile`:

```just
dev-up:
    docker compose -f deploy/dev/compose.yml up -d

dev-down:
    docker compose -f deploy/dev/compose.yml down

seed hours="6":
    uv run python scripts/seed_synthetic.py --url http://127.0.0.1:8428 --hours {{hours}}
```

- [ ] **Step 2: Write failing unit test for the generator**

```python
# tests/unit/test_synthetic.py
from telemetry_nerd.devtools.synthetic import demo_text, exposition


def test_exposition_format():
    text = exposition("m", {"b": "2", "a": "1"}, [(1000, 1.5), (2000, 2.0)])
    assert text == 'm{a="1",b="2"} 1.5 1000\nm{a="1",b="2"} 2.0 2000\n'


def test_exposition_without_labels():
    assert exposition("m", {}, [(1000, 1.0)]) == "m 1.0 1000\n"


def test_demo_text_contains_spike_for_instance_c():
    start, end = 0, 6 * 3_600_000
    lines = demo_text(start, end).splitlines()
    spike = [ln for ln in lines if ln.startswith('tn_demo_latency_seconds{instance="c"} 1.5 ')]
    assert len(spike) == 20  # 5 minutes at 15s
    assert any(ln.startswith("tn_demo_requests_total{") for ln in lines)
```

Run: `uv run pytest tests/unit/test_synthetic.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement generator and seed script**

Create empty `src/telemetry_nerd/devtools/__init__.py`.

```python
# src/telemetry_nerd/devtools/synthetic.py
"""Synthetic series for development and tests (known ground truth)."""

from __future__ import annotations

import math
import random
import time
from collections.abc import Iterable

import httpx


def exposition(metric: str, labels: dict[str, str], samples: Iterable[tuple[int, float]]) -> str:
    inner = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    head = f"{metric}{{{inner}}}" if inner else metric
    return "".join(f"{head} {value!r} {ts}\n" for ts, value in samples)


def push(base_url: str, text: str, attempts: int = 40) -> None:
    """Import exposition text and flush so it is immediately queryable.
    Retries connection errors so it works right after `docker compose up`."""
    with httpx.Client(timeout=60) as client:
        for attempt in range(attempts):
            try:
                client.post(
                    f"{base_url}/api/v1/import/prometheus", content=text.encode()
                ).raise_for_status()
                break
            except httpx.TransportError:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.5)
        client.get(f"{base_url}/internal/force_flush").raise_for_status()


def demo_text(start_ms: int, end_ms: int, interval_ms: int = 15_000, seed: int = 7) -> str:
    """Latency gauge (hourly sine + noise) for instances a,b,c; c spikes to 1.5
    for 5 minutes starting at 2/3 of the range. Request counter per instance."""
    rng = random.Random(seed)
    spike_start = start_ms + (end_ms - start_ms) * 2 // 3
    spike_start -= spike_start % interval_ms
    spike_end = spike_start + 5 * 60_000
    parts: list[str] = []
    for instance in ("a", "b", "c"):
        latency: list[tuple[int, float]] = []
        requests: list[tuple[int, float]] = []
        total = 0.0
        for ts in range(start_ms, end_ms, interval_ms):
            if instance == "c" and spike_start <= ts < spike_end:
                value = 1.5
            else:
                phase = 2 * math.pi * (ts % 3_600_000) / 3_600_000
                value = round(0.05 + 0.02 * math.sin(phase) + rng.uniform(0, 0.01), 6)
            latency.append((ts, value))
            total += rng.randint(600, 900)
            requests.append((ts, total))
        parts.append(exposition("tn_demo_latency_seconds", {"instance": instance}, latency))
        parts.append(exposition("tn_demo_requests_total", {"instance": instance}, requests))
    return "".join(parts)
```

```python
# scripts/seed_synthetic.py
"""Push synthetic demo series into VictoriaMetrics."""

import argparse

from telemetry_nerd.devtools.synthetic import demo_text, push
from telemetry_nerd.model.time import now_ms


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8428")
    parser.add_argument("--hours", type=int, default=6)
    args = parser.parse_args()
    end = now_ms() // 15_000 * 15_000
    start = end - args.hours * 3_600_000
    push(args.url, demo_text(start, end))
    print(f"seeded {args.hours}h of demo series into {args.url}")


if __name__ == "__main__":
    main()
```

Run: `uv run pytest tests/unit/test_synthetic.py -v` → PASS.

- [ ] **Step 4: Write the integration test**

```python
# tests/integration/conftest.py
import time

import httpx
import pytest
from testcontainers.core.container import DockerContainer

VM_IMAGE = "victoriametrics/victoria-metrics:v1.137.0"


@pytest.fixture(scope="session")
def vm_url():
    container = (
        DockerContainer(VM_IMAGE)
        .with_exposed_ports(8428)
        .with_command("-retentionPeriod=100y -search.latencyOffset=0s -search.disableCache")
    )
    container.start()
    url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8428)}"
    deadline = time.monotonic() + 30
    while True:
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            container.stop()
            raise RuntimeError("VictoriaMetrics did not become healthy")
        time.sleep(0.3)
    yield url
    container.stop()
```

Create empty `tests/integration/__init__.py`.

```python
# tests/integration/test_promql_vm.py
import pytest

from telemetry_nerd.devtools.synthetic import exposition, push
from telemetry_nerd.model.time import TimeRange, now_ms
from telemetry_nerd.sources.promql import PromQLSource

pytestmark = pytest.mark.integration


def _hour_aligned_t0() -> int:
    return (now_ms() - 3 * 3_600_000) // 3_600_000 * 3_600_000


async def test_rollup_preserves_peak(vm_url):
    t0 = _hour_aligned_t0()
    samples = [(t0 + i * 15_000, 1000.0 if i == 42 else float(i % 4)) for i in range(240)]
    push(vm_url, exposition("tn_it_gauge", {"instance": "a"}, samples))

    src = PromQLSource("vm", vm_url)
    res = await src.fetch('tn_it_gauge{instance="a"}', TimeRange(t0 + 60_000, t0 + 3_540_000), 60_000)
    rows = res.buckets.to_pylist()

    # sample 42 is at t0+630s; the bucket covering (600s, 660s] ends at t0+660s
    spike = next(r for r in rows if r["ts_ms"] == t0 + 660_000)
    assert spike["max"] == 1000.0
    assert spike["avg"] < 1000.0
    # 4 samples per 60s bucket; VM may include the sample on the window edge
    assert all(4 <= r["count"] <= 5 for r in rows)
    assert res.series.num_rows == 1


async def test_expression_uses_subquery(vm_url):
    t0 = _hour_aligned_t0()
    for inst in ("a", "b"):
        push(vm_url, exposition("tn_it_sum", {"instance": inst},
                                [(t0 + i * 15_000, 1.0) for i in range(240)]))
    src = PromQLSource("vm", vm_url)
    res = await src.fetch("sum(tn_it_sum)", TimeRange(t0 + 120_000, t0 + 3_000_000), 60_000)
    assert res.series.num_rows == 1
    assert {r["avg"] for r in res.buckets.to_pylist()} == {2.0}
```

- [ ] **Step 5: Run integration tests**

Run: `just test-integration`
Expected: 2 PASS. If `count` bounds fail, print the rows, record VictoriaMetrics' window-edge semantics in a comment in `promql.py`, and adjust only the bound (never drop the peak assertion).

- [ ] **Step 6: Commit**

```bash
git add deploy scripts src/telemetry_nerd/devtools tests justfile
git commit -m "feat: dev VictoriaMetrics, synthetic series, adapter integration tests"
```

---

### Task 4: DuckDB schema and chunked series cache

**Files:**
- Create: `src/telemetry_nerd/datasets/__init__.py`, `src/telemetry_nerd/datasets/db.py`, `src/telemetry_nerd/datasets/cache.py`
- Test: `tests/unit/test_cache.py`

**Interfaces:**
- Consumes: `TimeRange`, `now_ms`, `BUCKET_SCHEMA`, `SERIES_SCHEMA`, `FetchResult`, `series_id`, `labels_json` (Task 1).
- Produces:
  - `datasets.db`: `open_duckdb(path: str | Path) -> duckdb.DuckDBPyConnection` (creates all tables, including those `DatasetStore` uses), `upsert_series(con, series: pa.Table) -> None`, `fetch_arrow(con, sql: str, params: dict, schema: pa.Schema) -> pa.Table`.
  - `datasets.cache`: `Fetcher = Callable[[TimeRange], Awaitable[FetchResult]]`; `SeriesCache(con, *, chunk_buckets=720, settle_ms=300_000, recent_ttl_ms=30_000, clock=now_ms)` with `.settle_ms`, `SeriesCache.query_key(source, expr, step_ms) -> str` (staticmethod), `.chunk_starts(rng, step_ms) -> list[int]`, `async .get(source, expr, rng, step_ms, fetch: Fetcher) -> FetchResult`.

**Semantics (spec §2.2):** a chunk covers `chunk_buckets` step-aligned bucket timestamps `[cs, cs + span - step]` with `span = step * chunk_buckets`. A chunk is **immutable** once `cs + span <= now - settle_ms`; mutable chunks are refetched when older than `recent_ttl_ms`. Only missing or stale chunks are fetched. Results include buckets with `start_ms <= ts_ms <= end_ms`.

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_cache.py
import pyarrow as pa
import pytest

from telemetry_nerd.datasets.cache import SeriesCache
from telemetry_nerd.datasets.db import open_duckdb
from telemetry_nerd.model.series import (
    BUCKET_SCHEMA,
    SERIES_SCHEMA,
    FetchResult,
    labels_json,
    series_id,
)
from telemetry_nerd.model.time import TimeRange

STEP = 60_000
SPAN = STEP * 10
NOW = 6_000_000_000  # multiple of SPAN


class FakeFetcher:
    def __init__(self):
        self.calls: list[TimeRange] = []

    async def __call__(self, rng: TimeRange) -> FetchResult:
        self.calls.append(rng)
        ts = list(range(rng.start_ms, rng.end_ms + 1, STEP))
        sid = series_id("src", {"i": "a"})
        buckets = pa.table(
            {
                "ts_ms": ts,
                "series_id": [sid] * len(ts),
                "avg": [float(t) for t in ts],
                "min": [float(t) - 1 for t in ts],
                "max": [float(t) + 1 for t in ts],
                "count": [4] * len(ts),
            },
            schema=BUCKET_SCHEMA,
        )
        series = pa.table({"series_id": [sid], "labels": [labels_json({"i": "a"})]},
                          schema=SERIES_SCHEMA)
        return FetchResult(buckets, series)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def cache(tmp_path, clock):
    return SeriesCache(open_duckdb(tmp_path / "s.duckdb"), chunk_buckets=10, clock=clock)


async def test_first_get_fetches_each_overlapping_chunk(cache):
    f = FakeFetcher()
    res = await cache.get("src", "up", TimeRange(1_200_000, 2_400_000), STEP, f)
    assert [c.start_ms for c in f.calls] == [1_200_000, 1_800_000, 2_400_000]
    assert all(c.end_ms == c.start_ms + SPAN - STEP for c in f.calls)
    assert res.buckets.num_rows == 21
    assert res.buckets.schema == BUCKET_SCHEMA
    assert res.series.num_rows == 1


async def test_second_get_is_served_from_cache(cache):
    f = FakeFetcher()
    await cache.get("src", "up", TimeRange(1_200_000, 2_400_000), STEP, f)
    await cache.get("src", "up", TimeRange(1_200_000, 2_400_000), STEP, f)
    assert len(f.calls) == 3


async def test_extended_range_fetches_only_new_chunks(cache):
    f = FakeFetcher()
    await cache.get("src", "up", TimeRange(1_200_000, 2_400_000), STEP, f)
    await cache.get("src", "up", TimeRange(1_200_000, 3_000_000), STEP, f)
    assert [c.start_ms for c in f.calls[3:]] == [3_000_000]


async def test_recent_chunks_refetched_after_ttl(cache, clock):
    f = FakeFetcher()
    rng = TimeRange(NOW - 120_000, NOW)
    await cache.get("src", "up", rng, STEP, f)
    assert len(f.calls) == 2
    await cache.get("src", "up", rng, STEP, f)
    assert len(f.calls) == 2
    clock.t += 31_000
    await cache.get("src", "up", rng, STEP, f)
    assert len(f.calls) == 4


async def test_settled_chunks_never_refetched(cache, clock):
    f = FakeFetcher()
    rng = TimeRange(1_200_000, 2_400_000)
    await cache.get("src", "up", rng, STEP, f)
    clock.t += 30 * 86_400_000
    await cache.get("src", "up", rng, STEP, f)
    assert len(f.calls) == 3


async def test_result_is_filtered_to_range(cache):
    res = await cache.get("src", "up", TimeRange(1_260_000, 1_380_000), STEP, FakeFetcher())
    assert res.buckets.column("ts_ms").to_pylist() == [1_260_000, 1_320_000, 1_380_000]


async def test_step_and_expr_are_separate_keys(cache):
    f = FakeFetcher()
    await cache.get("src", "up", TimeRange(1_200_000, 1_380_000), STEP, f)
    await cache.get("src", "down", TimeRange(1_200_000, 1_380_000), STEP, f)
    assert len(f.calls) == 2


def test_query_key_normalizes_whitespace():
    assert SeriesCache.query_key("s", "sum( up )", 60_000) == SeriesCache.query_key(
        "s", "sum(  up\n)", 60_000
    )
    assert SeriesCache.query_key("s", "up", 60_000) != SeriesCache.query_key("s", "up", 30_000)
```

Run: `uv run pytest tests/unit/test_cache.py -v` → FAIL (module missing).

- [ ] **Step 2: Implement**

Create empty `src/telemetry_nerd/datasets/__init__.py`.

```python
# src/telemetry_nerd/datasets/db.py
"""DuckDB holds series data. Only the server process opens the file (single writer)."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa

_SCHEMA = [
    "CREATE TABLE IF NOT EXISTS series (series_id VARCHAR PRIMARY KEY, labels VARCHAR NOT NULL)",
    """CREATE TABLE IF NOT EXISTS cache_chunks (
        qkey VARCHAR, chunk_start BIGINT, fetched_at BIGINT, immutable BOOLEAN,
        PRIMARY KEY (qkey, chunk_start))""",
    """CREATE TABLE IF NOT EXISTS cache_buckets (
        qkey VARCHAR, chunk_start BIGINT, ts_ms BIGINT, series_id VARCHAR,
        avg DOUBLE, min DOUBLE, max DOUBLE, count BIGINT)""",
    "CREATE TABLE IF NOT EXISTS datasets (id VARCHAR PRIMARY KEY, meta VARCHAR NOT NULL)",
    """CREATE TABLE IF NOT EXISTS dataset_rows (
        dataset_id VARCHAR, ts_ms BIGINT, series_id VARCHAR,
        avg DOUBLE, min DOUBLE, max DOUBLE, count BIGINT)""",
]


def open_duckdb(path: str | Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(path))
    for statement in _SCHEMA:
        con.execute(statement)
    return con


def upsert_series(con: duckdb.DuckDBPyConnection, series: pa.Table) -> None:
    con.register("_tn_series", series)
    try:
        con.execute("INSERT OR IGNORE INTO series SELECT series_id, labels FROM _tn_series")
    finally:
        con.unregister("_tn_series")


def fetch_arrow(
    con: duckdb.DuckDBPyConnection, sql: str, params: dict, schema: pa.Schema
) -> pa.Table:
    return con.execute(sql, params).fetch_arrow_table().cast(schema)
```

```python
# src/telemetry_nerd/datasets/cache.py
"""Chunked series cache: fetch only missing or stale chunks from the source."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable

import duckdb

from telemetry_nerd.datasets.db import fetch_arrow, upsert_series
from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult
from telemetry_nerd.model.time import TimeRange, now_ms

Fetcher = Callable[[TimeRange], Awaitable[FetchResult]]


class SeriesCache:
    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        *,
        chunk_buckets: int = 720,
        settle_ms: int = 300_000,
        recent_ttl_ms: int = 30_000,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        if chunk_buckets < 2:
            raise ValueError("chunk_buckets must be >= 2")
        self._con = con
        self.chunk_buckets = chunk_buckets
        self.settle_ms = settle_ms
        self.recent_ttl_ms = recent_ttl_ms
        self._clock = clock
        # M1: one lock serializes cache access; fine for a single analyst.
        self._lock = asyncio.Lock()

    @staticmethod
    def query_key(source: str, expr: str, step_ms: int) -> str:
        normalized = " ".join(expr.split()).replace("( ", "(").replace(" )", ")")
        return hashlib.sha256(f"{source}\0{normalized}\0{step_ms}".encode()).hexdigest()[:16]

    def chunk_starts(self, rng: TimeRange, step_ms: int) -> list[int]:
        span = step_ms * self.chunk_buckets
        first = rng.start_ms // span * span
        return list(range(first, rng.end_ms + 1, span))

    async def get(
        self, source: str, expr: str, rng: TimeRange, step_ms: int, fetch: Fetcher
    ) -> FetchResult:
        qkey = self.query_key(source, expr, step_ms)
        span = step_ms * self.chunk_buckets
        async with self._lock:
            now = self._clock()
            state = {
                row[0]: (row[1], row[2])
                for row in self._con.execute(
                    "SELECT chunk_start, fetched_at, immutable FROM cache_chunks WHERE qkey = $q",
                    {"q": qkey},
                ).fetchall()
            }
            missing = [
                cs for cs in self.chunk_starts(rng, step_ms) if not self._fresh(state.get(cs), now)
            ]
            results = await asyncio.gather(
                *(fetch(TimeRange(cs, cs + span - step_ms)) for cs in missing)
            )
            for cs, result in zip(missing, results, strict=True):
                immutable = cs + span <= now - self.settle_ms
                self._store(qkey, cs, cs + span - step_ms, result, now, immutable)
            return self._read(qkey, rng)

    def _fresh(self, state: tuple[int, bool] | None, now: int) -> bool:
        if state is None:
            return False
        fetched_at, immutable = state
        return immutable or now - fetched_at < self.recent_ttl_ms

    def _store(
        self, qkey: str, cs: int, ce: int, result: FetchResult, now: int, immutable: bool
    ) -> None:
        con = self._con
        con.begin()
        try:
            params = {"q": qkey, "c": cs}
            con.execute("DELETE FROM cache_buckets WHERE qkey = $q AND chunk_start = $c", params)
            con.execute("DELETE FROM cache_chunks WHERE qkey = $q AND chunk_start = $c", params)
            con.register("_tn_in", result.buckets)
            try:
                con.execute(
                    """INSERT INTO cache_buckets
                       SELECT $q, $c, ts_ms, series_id, avg, min, max, count FROM _tn_in
                       WHERE ts_ms BETWEEN $c AND $e""",
                    {**params, "e": ce},
                )
            finally:
                con.unregister("_tn_in")
            upsert_series(con, result.series)
            con.execute(
                "INSERT INTO cache_chunks VALUES ($q, $c, $f, $i)",
                {**params, "f": now, "i": immutable},
            )
            con.commit()
        except Exception:
            con.rollback()
            raise

    def _read(self, qkey: str, rng: TimeRange) -> FetchResult:
        params = {"q": qkey, "s": rng.start_ms, "e": rng.end_ms}
        buckets = fetch_arrow(
            self._con,
            """SELECT ts_ms, series_id, avg, min, max, count FROM cache_buckets
               WHERE qkey = $q AND ts_ms BETWEEN $s AND $e ORDER BY series_id, ts_ms""",
            params,
            BUCKET_SCHEMA,
        )
        series = fetch_arrow(
            self._con,
            """SELECT series_id, labels FROM series WHERE series_id IN (
                 SELECT DISTINCT series_id FROM cache_buckets
                 WHERE qkey = $q AND ts_ms BETWEEN $s AND $e)
               ORDER BY series_id""",
            params,
            SERIES_SCHEMA,
        )
        return FetchResult(buckets, series)
```

- [ ] **Step 3: Run tests, verify they pass**

Run: `uv run pytest tests/unit/test_cache.py -v`
Expected: all PASS. (If your DuckDB version deprecates `fetch_arrow_table`, change it in `fetch_arrow` only.)

- [ ] **Step 4: Commit**

```bash
git add src/telemetry_nerd/datasets tests/unit/test_cache.py
git commit -m "feat: DuckDB chunked series cache with settle window"
```

---

### Task 5: Workspace store (SQLite) and dataset store (DuckDB)

**Files:**
- Create: `src/telemetry_nerd/workspace/__init__.py`, `src/telemetry_nerd/workspace/store.py`, `src/telemetry_nerd/datasets/store.py`
- Test: `tests/unit/test_workspace_store.py`, `tests/unit/test_dataset_store.py`

**Interfaces:**
- Consumes: `open_duckdb`, `upsert_series`, `fetch_arrow` (Task 4); `FetchResult`, schemas, `TimeRange`, `now_ms`, `NotFound` (Task 1).
- Produces:
  - `workspace.store`: `Panel` dataclass (`id, question, status, spec: dict, dataset_ids: list[str], created_at_ms`) with `.to_dict()`; `WorkspaceStore(path, clock=now_ms)` with `.next_id(prefix) -> str`, `.create_panel(question, spec, dataset_ids) -> Panel` (raises `ValueError` if question is blank), `.get_panel(id) -> Panel` (raises `NotFound`), `.list_panels() -> list[Panel]` (newest first).
  - `datasets.store`: `DatasetMeta` frozen dataclass (`id, source, expr, start_ms, end_ms, step_ms, resolution_ms, representation="bucket_agg", created_at_ms=0`) with `.to_dict()`; `DatasetStore(con, new_id: Callable[[str], str], clock=now_ms)` with `.put(*, source, expr, rng, step_ms, resolution_ms, result) -> DatasetMeta` and `.get(dataset_id) -> tuple[DatasetMeta, FetchResult]` (raises `NotFound`).

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_workspace_store.py
import pytest

from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.workspace.store import WorkspaceStore


@pytest.fixture
def ws(tmp_path):
    return WorkspaceStore(tmp_path / "w.db", clock=lambda: 42)


def test_next_id_is_per_prefix_sequence(ws):
    assert [ws.next_id("d"), ws.next_id("d"), ws.next_id("p")] == ["d1", "d2", "p1"]


def test_ids_survive_reopen(tmp_path):
    WorkspaceStore(tmp_path / "w.db").next_id("d")
    assert WorkspaceStore(tmp_path / "w.db").next_id("d") == "d2"


def test_create_and_get_panel(ws):
    p = ws.create_panel("Is latency up?", {"layers": []}, ["d1"])
    assert p.id == "p1"
    assert p.status == "open"
    got = ws.get_panel("p1")
    assert got == p
    assert got.to_dict()["question"] == "Is latency up?"
    assert got.created_at_ms == 42


@pytest.mark.parametrize("question", ["", "   "])
def test_panel_requires_question(ws, question):
    with pytest.raises(ValueError, match="explicit question"):
        ws.create_panel(question, {}, ["d1"])


def test_list_panels_newest_first(ws):
    ws.create_panel("q1", {}, [])
    ws.create_panel("q2", {}, [])
    assert [p.id for p in ws.list_panels()] == ["p2", "p1"]


def test_unknown_panel(ws):
    with pytest.raises(NotFound):
        ws.get_panel("p9")
```

```python
# tests/unit/test_dataset_store.py
import itertools

import pyarrow as pa
import pytest

from telemetry_nerd.datasets.db import open_duckdb
from telemetry_nerd.datasets.store import DatasetStore
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult
from telemetry_nerd.model.time import TimeRange


@pytest.fixture
def store(tmp_path):
    counter = itertools.count(1)
    return DatasetStore(open_duckdb(tmp_path / "s.duckdb"), lambda p: f"{p}{next(counter)}",
                        clock=lambda: 7)


def result():
    buckets = pa.table(
        {"ts_ms": [60_000, 120_000], "series_id": ["s1", "s1"], "avg": [1.0, None],
         "min": [0.5, None], "max": [2.0, None], "count": [4, 0]},
        schema=BUCKET_SCHEMA,
    )
    series = pa.table({"series_id": ["s1"], "labels": ['{"i":"a"}']}, schema=SERIES_SCHEMA)
    return FetchResult(buckets, series)


def test_put_then_get_roundtrip(store):
    meta = store.put(source="vm", expr="up", rng=TimeRange(60_000, 120_000), step_ms=60_000,
                     resolution_ms=15_000, result=result())
    assert meta.id == "d1"
    assert meta.representation == "bucket_agg"
    got_meta, got = store.get("d1")
    assert got_meta == meta
    assert got.buckets.equals(result().buckets)
    assert got.series.equals(result().series)


def test_datasets_are_immutable_snapshots(store):
    store.put(source="vm", expr="up", rng=TimeRange(60_000, 120_000), step_ms=60_000,
              resolution_ms=15_000, result=result())
    store.put(source="vm", expr="up", rng=TimeRange(60_000, 120_000), step_ms=60_000,
              resolution_ms=15_000, result=result())
    assert store.get("d1")[1].buckets.num_rows == 2
    assert store.get("d2")[1].buckets.num_rows == 2


def test_unknown_dataset(store):
    with pytest.raises(NotFound):
        store.get("d404")
```

Run: `uv run pytest tests/unit/test_workspace_store.py tests/unit/test_dataset_store.py -v` → FAIL.

- [ ] **Step 2: Implement**

Create empty `src/telemetry_nerd/workspace/__init__.py`.

```python
# src/telemetry_nerd/workspace/store.py
"""Workspace objects in SQLite. M1: id allocation and panels."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.time import now_ms

_SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (prefix TEXT PRIMARY KEY, n INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS panels (
    id TEXT PRIMARY KEY,
    question TEXT NOT NULL CHECK (length(trim(question)) > 0),
    status TEXT NOT NULL DEFAULT 'open',
    spec TEXT NOT NULL,
    dataset_ids TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class Panel:
    id: str
    question: str
    status: str
    spec: dict
    dataset_ids: list[str]
    created_at_ms: int

    def to_dict(self) -> dict:
        return asdict(self)


class WorkspaceStore:
    def __init__(self, path: str | Path, clock: Callable[[], int] = now_ms) -> None:
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.executescript(_SCHEMA)
        self._clock = clock

    def next_id(self, prefix: str) -> str:
        (n,) = self._db.execute(
            """INSERT INTO counters (prefix, n) VALUES (?, 1)
               ON CONFLICT (prefix) DO UPDATE SET n = n + 1 RETURNING n""",
            (prefix,),
        ).fetchone()
        return f"{prefix}{n}"

    def create_panel(self, question: str, spec: dict, dataset_ids: list[str]) -> Panel:
        if not question or not question.strip():
            raise ValueError("every panel must answer an explicit question")
        panel = Panel(
            id=self.next_id("p"),
            question=question.strip(),
            status="open",
            spec=spec,
            dataset_ids=list(dataset_ids),
            created_at_ms=self._clock(),
        )
        self._db.execute(
            "INSERT INTO panels VALUES (?, ?, ?, ?, ?, ?)",
            (panel.id, panel.question, panel.status, json.dumps(spec),
             json.dumps(panel.dataset_ids), panel.created_at_ms),
        )
        return panel

    def get_panel(self, panel_id: str) -> Panel:
        row = self._db.execute("SELECT * FROM panels WHERE id = ?", (panel_id,)).fetchone()
        if row is None:
            raise NotFound(f"panel {panel_id} not found")
        return self._panel(row)

    def list_panels(self) -> list[Panel]:
        rows = self._db.execute(
            "SELECT * FROM panels ORDER BY created_at_ms DESC, CAST(substr(id, 2) AS INTEGER) DESC"
        ).fetchall()
        return [self._panel(r) for r in rows]

    @staticmethod
    def _panel(row: tuple) -> Panel:
        return Panel(row[0], row[1], row[2], json.loads(row[3]), json.loads(row[4]), row[5])
```

```python
# src/telemetry_nerd/datasets/store.py
"""Immutable datasets: a snapshot of buckets for (source, expr, range, step)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass

import duckdb

from telemetry_nerd.datasets.db import fetch_arrow, upsert_series
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult
from telemetry_nerd.model.time import TimeRange, now_ms


@dataclass(frozen=True)
class DatasetMeta:
    id: str
    source: str
    expr: str
    start_ms: int
    end_ms: int
    step_ms: int
    resolution_ms: int
    representation: str = "bucket_agg"
    created_at_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class DatasetStore:
    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        new_id: Callable[[str], str],
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self._con = con
        self._new_id = new_id
        self._clock = clock

    def put(
        self,
        *,
        source: str,
        expr: str,
        rng: TimeRange,
        step_ms: int,
        resolution_ms: int,
        result: FetchResult,
    ) -> DatasetMeta:
        meta = DatasetMeta(
            id=self._new_id("d"),
            source=source,
            expr=expr,
            start_ms=rng.start_ms,
            end_ms=rng.end_ms,
            step_ms=step_ms,
            resolution_ms=resolution_ms,
            created_at_ms=self._clock(),
        )
        con = self._con
        con.begin()
        try:
            con.execute("INSERT INTO datasets VALUES ($id, $m)",
                        {"id": meta.id, "m": json.dumps(meta.to_dict())})
            con.register("_tn_ds", result.buckets)
            try:
                con.execute(
                    """INSERT INTO dataset_rows
                       SELECT $id, ts_ms, series_id, avg, min, max, count FROM _tn_ds""",
                    {"id": meta.id},
                )
            finally:
                con.unregister("_tn_ds")
            upsert_series(con, result.series)
            con.commit()
        except Exception:
            con.rollback()
            raise
        return meta

    def get(self, dataset_id: str) -> tuple[DatasetMeta, FetchResult]:
        row = self._con.execute(
            "SELECT meta FROM datasets WHERE id = $id", {"id": dataset_id}
        ).fetchone()
        if row is None:
            raise NotFound(f"dataset {dataset_id} not found")
        meta = DatasetMeta(**json.loads(row[0]))
        params = {"id": dataset_id}
        buckets = fetch_arrow(
            self._con,
            """SELECT ts_ms, series_id, avg, min, max, count FROM dataset_rows
               WHERE dataset_id = $id ORDER BY series_id, ts_ms""",
            params,
            BUCKET_SCHEMA,
        )
        series = fetch_arrow(
            self._con,
            """SELECT series_id, labels FROM series WHERE series_id IN (
                 SELECT DISTINCT series_id FROM dataset_rows WHERE dataset_id = $id)
               ORDER BY series_id""",
            params,
            SERIES_SCHEMA,
        )
        return meta, FetchResult(buckets, series)
```

- [ ] **Step 3: Run tests, verify they pass**

Run: `uv run pytest tests/unit/test_workspace_store.py tests/unit/test_dataset_store.py -v`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add src/telemetry_nerd/workspace src/telemetry_nerd/datasets/store.py tests/unit
git commit -m "feat: workspace panel store and immutable dataset store"
```

---

### Task 6: Correct re-bucketing and pixel-aware level of detail

**Files:**
- Create: `src/telemetry_nerd/analysis/__init__.py`, `src/telemetry_nerd/analysis/resample.py`
- Test: `tests/unit/test_resample.py`

**Interfaces:**
- Consumes: `BUCKET_SCHEMA` (Task 1), `TimeRange`.
- Produces: `rebucket(buckets: pa.Table, new_step_ms: int) -> pa.Table` and `lod(buckets: pa.Table, step_ms: int, rng: TimeRange, width_px: int) -> tuple[pa.Table, int]` (returns the table and the effective step).

**Rules (spec §3.2, §6.5):** new bucket end = `ceil(ts / new_step) * new_step`; `min = min(min)`, `max = max(max)`, `count = sum(count)`, `avg = Σ(avg·count) / Σcount` (null when Σcount = 0). `lod` picks `new_step = step * ceil(n_buckets / width_px)` so there is at most about one bucket per pixel. Min/max per pixel is exactly M4-style peak preservation.

- [ ] **Step 1: Write failing tests (example-based + property-based)**

```python
# tests/unit/test_resample.py
import math

import pyarrow as pa
from hypothesis import given, settings
from hypothesis import strategies as st

from telemetry_nerd.analysis.resample import lod, rebucket
from telemetry_nerd.model.series import BUCKET_SCHEMA
from telemetry_nerd.model.time import TimeRange

STEP = 15_000


def table(rows):
    cols = {name: [r[i] for r in rows] for i, name in enumerate(BUCKET_SCHEMA.names)}
    return pa.table(cols, schema=BUCKET_SCHEMA)


def test_rebucket_aggregates_correctly():
    t = table([
        (15_000, "s", 1.0, 0.0, 2.0, 1),
        (30_000, "s", 4.0, 3.0, 10.0, 3),
        (45_000, "s", None, None, None, 0),
        (60_000, "s", 2.0, 1.0, 3.0, 2),
    ])
    out = rebucket(t, 60_000).to_pylist()
    assert out == [{"ts_ms": 60_000, "series_id": "s", "avg": (1 + 12 + 4) / 6,
                    "min": 0.0, "max": 10.0, "count": 6}]


def test_rebucket_all_empty_bucket_has_null_avg():
    t = table([(15_000, "s", None, None, None, 0)])
    assert rebucket(t, 60_000).to_pylist()[0]["avg"] is None


def test_rebucket_keeps_series_separate():
    t = table([(60_000, "a", 1.0, 1.0, 1.0, 1), (60_000, "b", 5.0, 5.0, 5.0, 1)])
    out = rebucket(t, 60_000).to_pylist()
    assert [r["series_id"] for r in out] == ["a", "b"]


def test_lod_noop_when_it_fits():
    t = table([(i * STEP, "s", 1.0, 1.0, 1.0, 1) for i in range(1, 11)])
    out, step = lod(t, STEP, TimeRange(STEP, 10 * STEP), width_px=800)
    assert step == STEP
    assert out.equals(t)


def test_lod_reduces_to_width_and_keeps_peak():
    rows = [(i * STEP, "s", 1.0, 1.0, 1.0, 4) for i in range(1, 1001)]
    rows[500] = (501 * STEP, "s", 1.0, 1.0, 999.0, 4)
    out, step = lod(table(rows), STEP, TimeRange(STEP, 1000 * STEP), width_px=100)
    assert step == STEP * 10
    assert out.num_rows <= 101
    assert max(out.column("max").to_pylist()) == 999.0


bucket = st.tuples(
    st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False),
    st.floats(0, 1e3, allow_nan=False, allow_infinity=False),
    st.integers(0, 50),
)


@settings(max_examples=200)
@given(st.dictionaries(st.integers(1, 200), bucket, min_size=1, max_size=200),
       st.integers(1, 20))
def test_rebucket_invariants(data, factor):
    rows = []
    for i, (avg, spread, count) in sorted(data.items()):
        if count == 0:
            rows.append((i * STEP, "s", None, None, None, 0))
        else:
            rows.append((i * STEP, "s", avg, avg - spread, avg + spread, count))
    t = table(rows)
    out = rebucket(t, STEP * factor)

    def col(tbl, name):
        return [v for v in tbl.column(name).to_pylist() if v is not None]

    assert sum(col(out, "count")) == sum(col(t, "count"))
    if col(t, "min"):
        assert min(col(out, "min")) == min(col(t, "min"))
        assert max(col(out, "max")) == max(col(t, "max"))
    total = sum(col(t, "count"))
    if total:
        before = sum(r[2] * r[5] for r in rows if r[5]) / total
        after_rows = [r for r in out.to_pylist() if r["count"]]
        after = sum(r["avg"] * r["count"] for r in after_rows) / total
        assert math.isclose(before, after, rel_tol=1e-9, abs_tol=1e-6)
```

Run: `uv run pytest tests/unit/test_resample.py -v` → FAIL.

- [ ] **Step 2: Implement**

Create empty `src/telemetry_nerd/analysis/__init__.py`.

```python
# src/telemetry_nerd/analysis/resample.py
"""Re-aggregation that never erodes peaks or biases means."""

from __future__ import annotations

import math

import polars as pl
import pyarrow as pa

from telemetry_nerd.model.series import BUCKET_SCHEMA
from telemetry_nerd.model.time import TimeRange


def rebucket(buckets: pa.Table, new_step_ms: int) -> pa.Table:
    """Merge buckets into coarser ones ending at multiples of new_step_ms."""
    if buckets.num_rows == 0:
        return buckets
    df = pl.from_arrow(buckets)
    k = new_step_ms
    total = pl.col("count").sum()
    out = (
        df.with_columns(((pl.col("ts_ms") + k - 1) // k * k).alias("ts_ms"))
        .group_by(["series_id", "ts_ms"])
        .agg(
            pl.when(total > 0)
            .then((pl.col("avg") * pl.col("count")).sum() / total)
            .otherwise(None)
            .alias("avg"),
            pl.col("min").min(),
            pl.col("max").max(),
            total.alias("count"),
        )
        .sort(["series_id", "ts_ms"])
        .select(BUCKET_SCHEMA.names)
    )
    return out.to_arrow().cast(BUCKET_SCHEMA)


def lod(buckets: pa.Table, step_ms: int, rng: TimeRange, width_px: int) -> tuple[pa.Table, int]:
    """Reduce to roughly one bucket per pixel, preserving min/max envelopes."""
    n_buckets = (rng.end_ms - rng.start_ms) // step_ms + 1
    factor = max(1, math.ceil(n_buckets / max(1, width_px)))
    if factor == 1:
        return buckets, step_ms
    new_step = step_ms * factor
    return rebucket(buckets, new_step), new_step
```

- [ ] **Step 3: Run tests, verify they pass**

Run: `uv run pytest tests/unit/test_resample.py -v`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add src/telemetry_nerd/analysis tests/unit/test_resample.py
git commit -m "feat: peak-preserving rebucket and pixel-aware LOD"
```

---

### Task 7: Chart spec, validator, auto spec

**Files:**
- Create: `src/telemetry_nerd/charts/__init__.py`, `src/telemetry_nerd/charts/spec.py`
- Test: `tests/unit/test_chart_spec.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: pydantic models `Layer(mark: Literal["line+envelope"], data: str)`, `YAxis(range_mode: Literal["data","reference","semantic"]="data", unit: str | None = None, label: str | None = None)`, `ChartSpec(layers: list[Layer] (min 1), y: YAxis = YAxis())`, `ValidationIssue(rule: str, message: str, severity: Literal["error","warning"])`; functions `validate(spec: ChartSpec, series_counts: dict[str, int]) -> list[ValidationIssue]`, `auto_spec(dataset_id: str) -> ChartSpec`; constant `LINE_SERIES_BUDGET = 5`.

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_chart_spec.py
import pytest
from pydantic import ValidationError

from telemetry_nerd.charts.spec import ChartSpec, Layer, auto_spec, validate


def test_auto_spec_is_line_envelope_scaled_to_data():
    spec = auto_spec("d3")
    assert spec.layers == [Layer(mark="line+envelope", data="d3")]
    assert spec.y.range_mode == "data"


def test_spec_requires_a_layer():
    with pytest.raises(ValidationError):
        ChartSpec(layers=[])


def test_unknown_mark_rejected():
    with pytest.raises(ValidationError):
        Layer(mark="pie", data="d1")


def test_series_budget_error_above_five():
    issues = validate(auto_spec("d1"), {"d1": 6})
    errors = [i for i in issues if i.severity == "error"]
    assert [i.rule for i in errors] == ["series_budget"]
    assert "aggregate" in errors[0].message


def test_five_series_ok_but_unknown_unit_warns():
    issues = validate(auto_spec("d1"), {"d1": 5})
    assert [(i.rule, i.severity) for i in issues] == [("units", "warning")]


def test_known_unit_no_issues():
    spec = auto_spec("d1")
    spec.y.unit = "s"
    assert validate(spec, {"d1": 2}) == []
```

Run: `uv run pytest tests/unit/test_chart_spec.py -v` → FAIL.

- [ ] **Step 2: Implement**

Create empty `src/telemetry_nerd/charts/__init__.py`.

```python
# src/telemetry_nerd/charts/spec.py
"""Declarative chart spec + validator. Rules are errors, not taste (spec §6.3)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

LINE_SERIES_BUDGET = 5


class Layer(BaseModel):
    mark: Literal["line+envelope"]
    data: str


class YAxis(BaseModel):
    range_mode: Literal["data", "reference", "semantic"] = "data"
    unit: str | None = None
    label: str | None = None


class ChartSpec(BaseModel):
    layers: list[Layer] = Field(min_length=1)
    y: YAxis = Field(default_factory=YAxis)


class ValidationIssue(BaseModel):
    rule: str
    message: str
    severity: Literal["error", "warning"]


def auto_spec(dataset_id: str) -> ChartSpec:
    # M1 has no catalog: no unit, no reference range. The UI labels "data" mode honestly.
    return ChartSpec(layers=[Layer(mark="line+envelope", data=dataset_id)])


def validate(spec: ChartSpec, series_counts: dict[str, int]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for layer in spec.layers:
        n = series_counts.get(layer.data, 0)
        if layer.mark == "line+envelope" and n > LINE_SERIES_BUDGET:
            issues.append(
                ValidationIssue(
                    rule="series_budget",
                    severity="error",
                    message=(
                        f"{layer.data} has {n} series; line charts allow at most "
                        f"{LINE_SERIES_BUDGET}. Aggregate across series (e.g. sum by / avg by "
                        "a coarser label) or filter to the series that answer the question."
                    ),
                )
            )
    if spec.y.unit is None:
        issues.append(
            ValidationIssue(
                rule="units",
                severity="warning",
                message="y-axis unit unknown; it will come from the metric catalog (M3)",
            )
        )
    return issues
```

- [ ] **Step 3: Run tests, verify they pass**

Run: `uv run pytest tests/unit/test_chart_spec.py -v` → all PASS.

- [ ] **Step 4: Commit**

```bash
git add src/telemetry_nerd/charts tests/unit/test_chart_spec.py
git commit -m "feat: chart spec with series-budget validator"
```

---

### Task 8: Telemetry service, summaries, event bus

**Files:**
- Create: `src/telemetry_nerd/core/__init__.py`, `src/telemetry_nerd/core/events.py`, `src/telemetry_nerd/core/summary.py`, `src/telemetry_nerd/core/service.py`
- Test: `tests/unit/test_service.py`, `tests/unit/fakes.py`

**Interfaces:**
- Consumes: everything from Tasks 1–7.
- Produces:
  - `core.events`: `EventBus` with `.subscribe() -> asyncio.Queue`, `.unsubscribe(q)`, `.publish(event: dict)`.
  - `core.summary`: `summarize(meta: DatasetMeta, result: FetchResult, *, now_ms: int, settle_ms: int, top: int = 5) -> dict` with keys `dataset, expr, range, step, representation, series_count, series (≤top: labels,min,max,mean,gaps), more_series, caveats`. Caveat codes: `empty`, `gaps`, `fake_resolution`, `settling`.
  - `core.service`: `auto_step(rng, resolution_ms, target_buckets=600) -> int`; `ChartRejected(issues)`; `ShowResult(panel: Panel, issues: list[ValidationIssue])`; `TelemetryService(sources, cache, datasets, workspace, events, clock=now_ms)` with `async query(expr, start="now-1h", end="now", step="auto", source="default") -> dict` (`{"dataset": id, "summary": {...}}`), `show(dataset_id, question) -> ShowResult`, `panel_data(panel_id, width_px) -> dict`.
  - `tests/unit/fakes.py`: `FakeSource(name="fake", n_series=2, resolution_ms=15_000)` and `make_service(tmp_path, source=None, clock=...) -> TelemetryService` for reuse by API/MCP tests.

- [ ] **Step 1: Write fakes and failing tests**

```python
# tests/unit/fakes.py
import pyarrow as pa

from telemetry_nerd.core.events import EventBus
from telemetry_nerd.core.service import TelemetryService
from telemetry_nerd.datasets.cache import SeriesCache
from telemetry_nerd.datasets.db import open_duckdb
from telemetry_nerd.datasets.store import DatasetStore
from telemetry_nerd.model.series import (
    BUCKET_SCHEMA,
    SERIES_SCHEMA,
    FetchResult,
    labels_json,
    series_id,
)
from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.workspace.store import WorkspaceStore

NOW = 6_000_000_000


class FakeSource:
    def __init__(self, name="fake", n_series=2, resolution_ms=15_000):
        self.name = name
        self.n_series = n_series
        self.resolution_ms = resolution_ms
        self.calls = 0

    async def fetch(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult:
        self.calls += 1
        ts = list(range(rng.start_ms, rng.end_ms + 1, step_ms))
        labels = [{"instance": f"i{k}"} for k in range(self.n_series)]
        sids = [series_id(self.name, lb) for lb in labels]
        rows = [(t, sid, float(k + 1)) for k, sid in enumerate(sids) for t in ts]
        buckets = pa.table(
            {
                "ts_ms": [r[0] for r in rows],
                "series_id": [r[1] for r in rows],
                "avg": [r[2] for r in rows],
                "min": [r[2] - 0.5 for r in rows],
                "max": [r[2] + 0.5 for r in rows],
                "count": [4] * len(rows),
            },
            schema=BUCKET_SCHEMA,
        )
        series = pa.table(
            {"series_id": sids, "labels": [labels_json(lb) for lb in labels]}, schema=SERIES_SCHEMA
        )
        return FetchResult(buckets, series)


def make_service(tmp_path, source=None, clock=lambda: NOW) -> TelemetryService:
    source = source or FakeSource()
    con = open_duckdb(tmp_path / "series.duckdb")
    workspace = WorkspaceStore(tmp_path / "workspace.db", clock=clock)
    return TelemetryService(
        sources={"default": source},
        cache=SeriesCache(con, clock=clock),
        datasets=DatasetStore(con, workspace.next_id, clock=clock),
        workspace=workspace,
        events=EventBus(),
        clock=clock,
    )
```

```python
# tests/unit/test_service.py
import pytest

from telemetry_nerd.core.service import ChartRejected, auto_step
from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.base import SourceError
from tests.unit.fakes import FakeSource, make_service


def test_auto_step_targets_about_600_buckets():
    assert auto_step(TimeRange(0, 3_600_000), 15_000) == 15_000
    assert auto_step(TimeRange(0, 6 * 3_600_000), 15_000) == 60_000
    assert auto_step(TimeRange(0, 7 * 86_400_000), 15_000) == 1_800_000


def test_auto_step_never_below_resolution():
    assert auto_step(TimeRange(0, 60_000), 30_000) == 30_000


async def test_query_returns_handle_and_compact_summary(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.query("up", start="now-2h", end="now-1h", step="1m")
    assert out["dataset"] == "d1"
    s = out["summary"]
    assert s["series_count"] == 2
    assert s["step"] == "1m"
    assert s["series"][0]["max"] == 2.5  # sorted by max, descending
    assert s["caveats"] == []
    assert len(str(out)) < 2048


async def test_query_flags_settling_and_fake_resolution(tmp_path):
    svc = make_service(tmp_path, FakeSource(resolution_ms=60_000))
    out = await svc.query("up", start="now-10m", end="now", step="15s")
    assert set(out["summary"]["caveats"]) >= {"settling", "fake_resolution"}


async def test_unknown_source_has_hint(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(SourceError) as exc:
        await svc.query("up", source="nope")
    assert "default" in exc.value.hint


async def test_show_creates_panel_and_publishes_event(tmp_path):
    svc = make_service(tmp_path)
    ds = (await svc.query("up", start="now-2h", end="now-1h"))["dataset"]
    q = svc.events.subscribe()  # after query, so dataset.created is not in the queue
    res = svc.show(ds, "Are both instances stable?")
    assert res.panel.id == "p1"
    assert res.panel.dataset_ids == [ds]
    assert [i.rule for i in res.issues] == ["units"]
    assert q.get_nowait() == {"type": "panel.created", "panel": "p1"}


async def test_show_rejects_spaghetti(tmp_path):
    svc = make_service(tmp_path, FakeSource(n_series=6))
    ds = (await svc.query("up", start="now-2h", end="now-1h"))["dataset"]
    with pytest.raises(ChartRejected) as exc:
        svc.show(ds, "Which instance is slow?")
    assert exc.value.issues[0].rule == "series_budget"


async def test_show_requires_question(tmp_path):
    svc = make_service(tmp_path)
    ds = (await svc.query("up", start="now-2h", end="now-1h"))["dataset"]
    with pytest.raises(ValueError):
        svc.show(ds, " ")


async def test_panel_data_respects_width(tmp_path):
    svc = make_service(tmp_path)
    ds = (await svc.query("up", start="now-6h", end="now-1h", step="15s"))["dataset"]
    panel = svc.show(ds, "Stable?").panel
    data = svc.panel_data(panel.id, width_px=100)
    assert len(data["series"]) == 2
    assert all(len(s["ts"]) <= 101 for s in data["series"])
    assert data["effective_step_ms"] > 15_000
    assert sorted(s["labels"]["instance"] for s in data["series"]) == ["i0", "i1"]
    assert data["dataset"]["expr"] == "up"
```

Run: `uv run pytest tests/unit/test_service.py -v` → FAIL.

- [ ] **Step 2: Implement**

Create empty `src/telemetry_nerd/core/__init__.py`.

```python
# src/telemetry_nerd/core/events.py
"""In-memory pub/sub. M2 replaces this with the persisted event log."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("event subscriber queue full; dropping %s", event.get("type"))
```

```python
# src/telemetry_nerd/core/summary.py
"""Compact dataset summaries for Claude: statistics and caveats, never raw series."""

from __future__ import annotations

import json

import polars as pl

from telemetry_nerd.datasets.store import DatasetMeta
from telemetry_nerd.model.series import FetchResult
from telemetry_nerd.model.time import format_duration, iso


def _round(x: float | None) -> float | None:
    return None if x is None else float(f"{x:.4g}")


def summarize(
    meta: DatasetMeta, result: FetchResult, *, now_ms: int, settle_ms: int, top: int = 5
) -> dict:
    caveats: list[str] = []
    if meta.step_ms < meta.resolution_ms:
        caveats.append("fake_resolution")
    if meta.end_ms > now_ms - settle_ms:
        caveats.append("settling")
    base = {
        "dataset": meta.id,
        "expr": meta.expr,
        "range": [iso(meta.start_ms), iso(meta.end_ms)],
        "step": format_duration(meta.step_ms),
        "representation": meta.representation,
    }
    if result.buckets.num_rows == 0:
        return {**base, "series_count": 0, "series": [], "more_series": 0,
                "caveats": ["empty", *caveats]}
    labels = {r["series_id"]: json.loads(r["labels"]) for r in result.series.to_pylist()}
    expected = (meta.end_ms - meta.start_ms) // meta.step_ms + 1
    total = pl.col("count").sum()
    per = (
        pl.from_arrow(result.buckets)
        .group_by("series_id")
        .agg(
            pl.col("min").min().alias("min"),
            pl.col("max").max().alias("max"),
            pl.when(total > 0)
            .then((pl.col("avg") * pl.col("count")).sum() / total)
            .otherwise(None)
            .alias("mean"),
            (pl.col("count") > 0).sum().alias("with_data"),
        )
        .with_columns((pl.lit(expected) - pl.col("with_data")).alias("gaps"))
        .sort("max", descending=True, nulls_last=True)
    )
    if per["gaps"].sum() > 0:
        caveats.insert(0, "gaps")
    series = [
        {
            "labels": labels.get(r["series_id"], {}),
            "min": _round(r["min"]),
            "max": _round(r["max"]),
            "mean": _round(r["mean"]),
            "gaps": int(r["gaps"]),
        }
        for r in per.head(top).to_dicts()
    ]
    return {**base, "series_count": per.height, "series": series,
            "more_series": max(0, per.height - top), "caveats": caveats}
```

```python
# src/telemetry_nerd/core/service.py
"""One operation layer shared by MCP, HTTP, and (later) the sandbox."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

import polars as pl

from telemetry_nerd.analysis.resample import lod
from telemetry_nerd.charts.spec import ValidationIssue, auto_spec, validate
from telemetry_nerd.core.events import EventBus
from telemetry_nerd.core.summary import summarize
from telemetry_nerd.datasets.cache import SeriesCache
from telemetry_nerd.datasets.store import DatasetStore
from telemetry_nerd.model.time import TimeRange, now_ms, parse_duration, parse_time
from telemetry_nerd.sources.base import Source, SourceError
from telemetry_nerd.workspace.store import Panel, WorkspaceStore

_NICE_STEPS = [
    parse_duration(s)
    for s in ("15s", "30s", "1m", "2m", "5m", "10m", "15m", "30m", "1h", "2h", "6h", "12h", "1d")
]


def auto_step(rng: TimeRange, resolution_ms: int, target_buckets: int = 600) -> int:
    wanted = max((rng.end_ms - rng.start_ms) / target_buckets, resolution_ms)
    return next((s for s in _NICE_STEPS if s >= wanted), _NICE_STEPS[-1])


class ChartRejected(Exception):
    def __init__(self, issues: list[ValidationIssue]) -> None:
        super().__init__("; ".join(f"[{i.rule}] {i.message}" for i in issues))
        self.issues = issues


@dataclass(frozen=True)
class ShowResult:
    panel: Panel
    issues: list[ValidationIssue]


@dataclass
class TelemetryService:
    sources: dict[str, Source]
    cache: SeriesCache
    datasets: DatasetStore
    workspace: WorkspaceStore
    events: EventBus
    clock: Callable[[], int] = now_ms

    async def query(
        self,
        expr: str,
        start: str = "now-1h",
        end: str = "now",
        step: str = "auto",
        source: str = "default",
    ) -> dict:
        src = self.sources.get(source)
        if src is None:
            raise SourceError(f"unknown source {source!r}",
                              hint=f"available sources: {', '.join(sorted(self.sources))}")
        now = self.clock()
        rng = TimeRange(parse_time(start, now), parse_time(end, now))
        step_ms = auto_step(rng, src.resolution_ms) if step == "auto" else parse_duration(step)
        rng = rng.align(step_ms)
        result = await self.cache.get(
            src.name, expr, rng, step_ms, lambda r: src.fetch(expr, r, step_ms)
        )
        meta = self.datasets.put(source=src.name, expr=expr, rng=rng, step_ms=step_ms,
                                 resolution_ms=src.resolution_ms, result=result)
        summary = summarize(meta, result, now_ms=now, settle_ms=self.cache.settle_ms)
        self.events.publish({"type": "dataset.created", "dataset": meta.id})
        return {"dataset": meta.id, "summary": summary}

    def show(self, dataset_id: str, question: str) -> ShowResult:
        _, result = self.datasets.get(dataset_id)
        spec = auto_spec(dataset_id)
        issues = validate(spec, {dataset_id: result.series.num_rows})
        errors = [i for i in issues if i.severity == "error"]
        if errors:
            raise ChartRejected(errors)
        panel = self.workspace.create_panel(question, spec.model_dump(), [dataset_id])
        self.events.publish({"type": "panel.created", "panel": panel.id})
        return ShowResult(panel, [i for i in issues if i.severity == "warning"])

    def panel_data(self, panel_id: str, width_px: int) -> dict:
        panel = self.workspace.get_panel(panel_id)
        meta, result = self.datasets.get(panel.dataset_ids[0])
        table, effective_step = lod(
            result.buckets, meta.step_ms, TimeRange(meta.start_ms, meta.end_ms), width_px
        )
        labels = {r["series_id"]: json.loads(r["labels"]) for r in result.series.to_pylist()}
        series = []
        for (sid,), group in pl.from_arrow(table).group_by("series_id", maintain_order=True):
            series.append(
                {
                    "id": sid,
                    "labels": labels.get(sid, {}),
                    **{c: group[c].to_list() for c in ("ts_ms", "avg", "min", "max", "count")},
                }
            )
        for s in series:
            s["ts"] = s.pop("ts_ms")
        caveats = summarize(meta, result, now_ms=self.clock(),
                            settle_ms=self.cache.settle_ms)["caveats"]
        return {
            "panel": panel.to_dict(),
            "dataset": meta.to_dict(),
            "effective_step_ms": effective_step,
            "series": series,
            "caveats": caveats,
        }
```

- [ ] **Step 3: Run tests, verify they pass**

Run: `uv run pytest tests/unit/test_service.py -v` → all PASS. Then `just test` → whole unit suite PASS.

- [ ] **Step 4: Commit**

```bash
git add src/telemetry_nerd/core tests
git commit -m "feat: telemetry service with compact summaries and event bus"
```

---

### Task 9: HTTP + WebSocket API

**Files:**
- Create: `src/telemetry_nerd/api/__init__.py`, `src/telemetry_nerd/api/app.py`
- Test: `tests/unit/test_api.py`

**Interfaces:**
- Consumes: `TelemetryService`, `ChartRejected` (Task 8); `SourceError` (Task 2); `NotFound` (Task 1); `tests/unit/fakes.make_service`.
- Produces: `create_app(service: TelemetryService, ui_dir: Path | None = None) -> Starlette`; constants `RENDER_BUDGET_MS = 100`, `RENDER_POINTS_PER_PX = 2`. Routes:
  - `GET /api/panels` → `[Panel.to_dict()]`
  - `GET /api/panels/{id}/data?width=N` → `service.panel_data` (width clamped to 50..4000; 404 on unknown)
  - `POST /api/query` `{expr, start?, end?, step?, source?}` → `service.query` result (400 `{error, hint}` on `SourceError`)
  - `POST /api/show` `{dataset, question}` → `{panel, issues}` (400 blank question, 404 unknown dataset, 422 `{error, issues}` if rejected)
  - `POST /api/render-report` `{panel_id, render_ms, points, width_px}` → `{budget_exceeded: bool}`; on breach logs a warning and publishes `{"type": "render.budget_exceeded", ...}`
  - `WS /ws` → streams every published event as JSON
  - `/` → static UI from `ui_dir` if `ui_dir/index.html` exists

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_api.py
import pytest
from starlette.testclient import TestClient

from telemetry_nerd.api.app import create_app
from tests.unit.fakes import FakeSource, make_service


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(make_service(tmp_path))) as c:
        yield c


def make_panel(client, question="Is it stable?"):
    ds = client.post("/api/query", json={"expr": "up", "start": "now-2h", "end": "now-1h"}).json()
    return client.post("/api/show", json={"dataset": ds["dataset"], "question": question})


def test_query_show_list_data(client):
    resp = make_panel(client)
    assert resp.status_code == 200
    panel = resp.json()["panel"]
    assert [p["id"] for p in client.get("/api/panels").json()] == [panel["id"]]
    data = client.get(f"/api/panels/{panel['id']}/data?width=200").json()
    assert len(data["series"]) == 2


def test_show_blank_question_is_400(client):
    assert make_panel(client, question="").status_code == 400


def test_unknown_panel_is_404(client):
    assert client.get("/api/panels/p99/data").status_code == 404


def test_unknown_source_is_400_with_hint(client):
    resp = client.post("/api/query", json={"expr": "up", "source": "nope"})
    assert resp.status_code == 400
    assert resp.json()["hint"]


def test_rejected_chart_is_422(tmp_path):
    with TestClient(create_app(make_service(tmp_path, FakeSource(n_series=9)))) as c:
        resp = make_panel(c)
        assert resp.status_code == 422
        assert resp.json()["issues"][0]["rule"] == "series_budget"


def test_render_report_budget(client):
    ok = client.post("/api/render-report",
                     json={"panel_id": "p1", "render_ms": 20, "points": 400, "width_px": 400})
    assert ok.json() == {"budget_exceeded": False}
    slow = client.post("/api/render-report",
                       json={"panel_id": "p1", "render_ms": 250, "points": 400, "width_px": 400})
    assert slow.json() == {"budget_exceeded": True}
    dense = client.post("/api/render-report",
                        json={"panel_id": "p1", "render_ms": 5, "points": 5000, "width_px": 400})
    assert dense.json() == {"budget_exceeded": True}


def test_websocket_receives_panel_created(client):
    with client.websocket_connect("/ws") as ws:
        make_panel(client)
        events = [ws.receive_json(), ws.receive_json()]
    assert {"type": "panel.created", "panel": "p1"} in events
```

Run: `uv run pytest tests/unit/test_api.py -v` → FAIL.

- [ ] **Step 2: Implement**

Create empty `src/telemetry_nerd/api/__init__.py`.

```python
# src/telemetry_nerd/api/app.py
"""HTTP + WebSocket API for the UI, the sandbox (M5) and future front doors."""

from __future__ import annotations

import logging
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from telemetry_nerd.core.service import ChartRejected, TelemetryService
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.sources.base import SourceError

log = logging.getLogger(__name__)

RENDER_BUDGET_MS = 100
RENDER_POINTS_PER_PX = 2


def _error(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


def create_app(service: TelemetryService, ui_dir: Path | None = None) -> Starlette:
    async def list_panels(request: Request) -> JSONResponse:
        return JSONResponse([p.to_dict() for p in service.workspace.list_panels()])

    async def panel_data(request: Request) -> JSONResponse:
        width = min(4000, max(50, int(request.query_params.get("width", "800"))))
        try:
            return JSONResponse(service.panel_data(request.path_params["id"], width))
        except NotFound as e:
            return _error(404, str(e))

    async def query(request: Request) -> JSONResponse:
        body = await request.json()
        args = {k: body[k] for k in ("expr", "start", "end", "step", "source") if k in body}
        try:
            return JSONResponse(await service.query(**args))
        except SourceError as e:
            return _error(400, str(e), hint=e.hint)
        except ValueError as e:
            return _error(400, str(e))

    async def show(request: Request) -> JSONResponse:
        body = await request.json()
        try:
            res = service.show(body.get("dataset", ""), body.get("question", ""))
        except ChartRejected as e:
            return _error(422, "chart rejected", issues=[i.model_dump() for i in e.issues])
        except NotFound as e:
            return _error(404, str(e))
        except ValueError as e:
            return _error(400, str(e))
        return JSONResponse(
            {"panel": res.panel.to_dict(), "issues": [i.model_dump() for i in res.issues]}
        )

    async def render_report(request: Request) -> JSONResponse:
        body = await request.json()
        exceeded = (
            body["render_ms"] > RENDER_BUDGET_MS
            or body["points"] > RENDER_POINTS_PER_PX * body["width_px"]
        )
        if exceeded:
            log.warning("render budget exceeded: %s", body)
            service.events.publish({"type": "render.budget_exceeded", **body})
        return JSONResponse({"budget_exceeded": exceeded})

    async def events(websocket: WebSocket) -> None:
        await websocket.accept()
        queue = service.events.subscribe()
        try:
            while True:
                await websocket.send_json(await queue.get())
        except WebSocketDisconnect:
            pass
        finally:
            service.events.unsubscribe(queue)

    routes = [
        Route("/api/panels", list_panels),
        Route("/api/panels/{id}/data", panel_data),
        Route("/api/query", query, methods=["POST"]),
        Route("/api/show", show, methods=["POST"]),
        Route("/api/render-report", render_report, methods=["POST"]),
        WebSocketRoute("/ws", events),
    ]
    if ui_dir is not None and (ui_dir / "index.html").exists():
        routes.append(Mount("/", app=StaticFiles(directory=ui_dir, html=True)))
    return Starlette(routes=routes)
```

- [ ] **Step 3: Run tests, verify they pass**

Run: `uv run pytest tests/unit/test_api.py -v` → all PASS.

- [ ] **Step 4: Commit**

```bash
git add src/telemetry_nerd/api tests/unit/test_api.py
git commit -m "feat: HTTP and WebSocket API"
```

---

### Task 10: MCP tools, CLI, bootstrap, plugin manifest

**Files:**
- Create: `src/telemetry_nerd/mcp/__init__.py`, `src/telemetry_nerd/mcp/server.py`, `src/telemetry_nerd/config.py`, `src/telemetry_nerd/core/bootstrap.py`, `src/telemetry_nerd/cli.py`, `.claude-plugin/plugin.json`, `.mcp.json`
- Modify: `justfile` (append `serve`)
- Test: `tests/unit/test_mcp.py`, `tests/unit/test_bootstrap.py`

**Interfaces:**
- Consumes: `TelemetryService`, `ChartRejected` (Task 8), `create_app` (Task 9), `PromQLSource` (Task 2), stores (Tasks 4–5).
- Produces:
  - `config.Settings` dataclass: `data_dir: Path = Path(".tn-data")`, `source_url = "http://127.0.0.1:8428"`, `source_flavor = "victoriametrics"`, `resolution_ms = 15_000`, `host = "127.0.0.1"`, `port = 7070`, `ui_dir: Path | None` (default = repo `ui/dist`). `Settings.from_env()` reads `TN_DATA_DIR`, `TN_SOURCE_URL`, `TN_SOURCE_FLAVOR`, `TN_PORT`.
  - `core.bootstrap.build_service(settings: Settings) -> TelemetryService`.
  - `mcp.server.build_mcp(service: TelemetryService, ui_url: str) -> FastMCP` with tools `query(expr, start="now-1h", end="now", step="auto", source="default") -> str` and `show(dataset, question) -> str` (both return compact JSON strings; failures raise `ToolError` with message and hint).
  - CLI: `telemetry-nerd serve [--data-dir] [--source-url] [--source-flavor] [--host] [--port] [--ui-dir] [--no-mcp]`.

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_mcp.py
import json

from mcp.shared.memory import create_connected_server_and_client_session

from telemetry_nerd.mcp.server import build_mcp
from tests.unit.fakes import FakeSource, make_service


async def call(mcp, name, args):
    async with create_connected_server_and_client_session(mcp._mcp_server) as client:
        return await client.call_tool(name, args)


async def test_query_then_show(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://127.0.0.1:7070")
    q = await call(mcp, "query", {"expr": "up", "start": "now-2h", "end": "now-1h"})
    assert not q.isError
    out = json.loads(q.content[0].text)
    assert out["dataset"] == "d1"
    assert out["summary"]["series_count"] == 2

    s = await call(mcp, "show", {"dataset": "d1", "question": "Stable?"})
    assert not s.isError
    shown = json.loads(s.content[0].text)
    assert shown["panel"] == "p1"
    assert shown["url"] == "http://127.0.0.1:7070/#/panel/p1"


async def test_show_without_question_is_tool_error(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    await call(mcp, "query", {"expr": "up", "start": "now-2h", "end": "now-1h"})
    s = await call(mcp, "show", {"dataset": "d1", "question": ""})
    assert s.isError
    assert "question" in s.content[0].text


async def test_rejected_chart_explains_rule(tmp_path):
    mcp = build_mcp(make_service(tmp_path, FakeSource(n_series=8)), "http://x")
    await call(mcp, "query", {"expr": "up", "start": "now-2h", "end": "now-1h"})
    s = await call(mcp, "show", {"dataset": "d1", "question": "Which is slow?"})
    assert s.isError
    assert "series_budget" in s.content[0].text


async def test_source_error_includes_hint(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    q = await call(mcp, "query", {"expr": "up", "source": "nope"})
    assert q.isError
    assert "hint:" in q.content[0].text
```

```python
# tests/unit/test_bootstrap.py
from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service


def test_build_service_creates_data_dir(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    svc = build_service(settings)
    assert (tmp_path / "data" / "series.duckdb").exists()
    assert (tmp_path / "data" / "workspace.db").exists()
    assert set(svc.sources) == {"default"}
    assert svc.sources["default"].name == "default"
```

Run: `uv run pytest tests/unit/test_mcp.py tests/unit/test_bootstrap.py -v` → FAIL.

- [ ] **Step 2: Implement**

Create empty `src/telemetry_nerd/mcp/__init__.py`.

```python
# src/telemetry_nerd/config.py
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_REPO_UI = Path(__file__).resolve().parents[2] / "ui" / "dist"


@dataclass
class Settings:
    data_dir: Path = Path(".tn-data")
    source_url: str = "http://127.0.0.1:8428"
    source_flavor: str = "victoriametrics"
    resolution_ms: int = 15_000
    host: str = "127.0.0.1"
    port: int = 7070
    ui_dir: Path | None = field(default=_REPO_UI)

    @classmethod
    def from_env(cls) -> Settings:
        s = cls()
        s.data_dir = Path(os.environ.get("TN_DATA_DIR", s.data_dir))
        s.source_url = os.environ.get("TN_SOURCE_URL", s.source_url)
        s.source_flavor = os.environ.get("TN_SOURCE_FLAVOR", s.source_flavor)
        s.port = int(os.environ.get("TN_PORT", s.port))
        return s

    @property
    def ui_url(self) -> str:
        return f"http://{self.host}:{self.port}"
```

```python
# src/telemetry_nerd/core/bootstrap.py
from __future__ import annotations

from telemetry_nerd.config import Settings
from telemetry_nerd.core.events import EventBus
from telemetry_nerd.core.service import TelemetryService
from telemetry_nerd.datasets.cache import SeriesCache
from telemetry_nerd.datasets.db import open_duckdb
from telemetry_nerd.datasets.store import DatasetStore
from telemetry_nerd.sources.promql import PromQLSource
from telemetry_nerd.workspace.store import WorkspaceStore


def build_service(settings: Settings) -> TelemetryService:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    con = open_duckdb(settings.data_dir / "series.duckdb")
    workspace = WorkspaceStore(settings.data_dir / "workspace.db")
    source = PromQLSource(
        "default",
        settings.source_url,
        flavor=settings.source_flavor,  # type: ignore[arg-type]
        resolution_ms=settings.resolution_ms,
    )
    return TelemetryService(
        sources={"default": source},
        cache=SeriesCache(con),
        datasets=DatasetStore(con, workspace.next_id),
        workspace=workspace,
        events=EventBus(),
    )
```

```python
# src/telemetry_nerd/mcp/server.py
"""MCP tools. Results are compact JSON: handles, summaries, caveats, links. Never raw series."""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from telemetry_nerd.core.service import ChartRejected, TelemetryService
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.sources.base import SourceError

INSTRUCTIONS = """\
Telemetry Nerd: an evidence-first telemetry workspace shared with the user's browser.
- `query` fetches a PromQL/MetricsQL expression as min/max/avg/count buckets and returns a
  dataset handle plus a compact summary. Write native PromQL/MetricsQL.
- `show` draws a dataset as a panel. Every panel must answer an explicit question; phrase
  it as the question the graph answers. Share the returned URL with the user.
- Report caveats from summaries (gaps, settling, fake_resolution) when you describe data.
- Scope every claim: source, selector, time range, step. Do not generalize beyond it.
"""


def _dump(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"))


def build_mcp(service: TelemetryService, ui_url: str) -> FastMCP:
    mcp = FastMCP("telemetry-nerd", instructions=INSTRUCTIONS)

    @mcp.tool()
    async def query(
        expr: str,
        start: str = "now-1h",
        end: str = "now",
        step: str = "auto",
        source: str = "default",
    ) -> str:
        """Fetch a PromQL/MetricsQL expression as a dataset of min/max/avg/count buckets.

        start/end: `now`, `now-<dur>` (e.g. now-6h), epoch ms, or ISO-8601 with timezone.
        step: `auto` (~600 buckets) or a duration like 30s, 1m, 5m.
        Returns {dataset, summary}. The summary is compact; raw series stay on the server.
        """
        try:
            return _dump(await service.query(expr, start, end, step, source))
        except SourceError as e:
            raise ToolError(f"{e} (hint: {e.hint})" if e.hint else str(e)) from e
        except ValueError as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    def show(dataset: str, question: str) -> str:
        """Draw a dataset as a panel (mean line + min/max envelope) in the shared workspace.

        question is REQUIRED: the explicit question this graph answers, e.g.
        "Did checkout latency rise after the 14:00 deploy?".
        Returns {panel, url, warnings}.
        """
        try:
            res = service.show(dataset, question)
        except ChartRejected as e:
            raise ToolError(f"chart rejected: {e}") from e
        except (NotFound, ValueError) as e:
            raise ToolError(str(e)) from e
        return _dump(
            {
                "panel": res.panel.id,
                "url": f"{ui_url}/#/panel/{res.panel.id}",
                "warnings": [i.message for i in res.issues],
            }
        )

    return mcp
```

```python
# src/telemetry_nerd/cli.py
"""`telemetry-nerd serve`: MCP over stdio + HTTP/WS UI server in one process.
stdout belongs to MCP; all logging goes to stderr."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import anyio
import uvicorn

from telemetry_nerd.api.app import create_app
from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.mcp.server import build_mcp


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="telemetry-nerd")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run MCP (stdio) and the workspace UI server")
    serve.add_argument("--data-dir", type=Path)
    serve.add_argument("--source-url")
    serve.add_argument("--source-flavor", choices=["victoriametrics", "prometheus"])
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--ui-dir", type=Path)
    serve.add_argument("--no-mcp", action="store_true", help="HTTP only (development, E2E)")
    return parser.parse_args(argv)


def _settings(args: argparse.Namespace) -> Settings:
    s = Settings.from_env()
    for attr in ("data_dir", "source_url", "source_flavor", "host", "port", "ui_dir"):
        value = getattr(args, attr)
        if value is not None:
            setattr(s, attr, value)
    return s


async def _serve(settings: Settings, with_mcp: bool) -> None:
    service = build_service(settings)
    app = create_app(service, settings.ui_dir)
    server = uvicorn.Server(
        uvicorn.Config(app, host=settings.host, port=settings.port, log_level="warning")
    )
    async with anyio.create_task_group() as tg:
        tg.start_soon(server.serve)
        if with_mcp:
            await build_mcp(service, settings.ui_url).run_stdio_async()
            server.should_exit = True


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    args = _parse(argv)
    if args.command == "serve":
        settings = _settings(args)
        logging.getLogger(__name__).info("workspace UI at %s", settings.ui_url)
        anyio.run(_serve, settings, not args.no_mcp)
```

```json
// .claude-plugin/plugin.json
{
  "name": "telemetry-nerd",
  "version": "0.1.0",
  "description": "Evidence-first telemetry analysis workspace for Claude",
  "author": { "name": "Avishai Ish-Shalom" }
}
```

```json
// .mcp.json
{
  "mcpServers": {
    "telemetry-nerd": {
      "command": "uv",
      "args": ["run", "--directory", "${CLAUDE_PLUGIN_ROOT}", "telemetry-nerd", "serve"],
      "env": { "TN_SOURCE_URL": "http://127.0.0.1:8428" }
    }
  }
}
```

(JSON files must not contain the `// path` comment lines; they are shown here only to name the file.)

Append to `justfile`:

```just
serve *args:
    uv run telemetry-nerd serve --no-mcp {{args}}
```

- [ ] **Step 3: Run tests, verify they pass**

Run: `uv run pytest tests/unit/test_mcp.py tests/unit/test_bootstrap.py -v` → all PASS.

- [ ] **Step 4: Manual smoke test**

```bash
just dev-up && just seed
just serve &   # background; stop with: kill %1
curl -s -XPOST localhost:7070/api/query -H 'content-type: application/json' \
  -d '{"expr":"tn_demo_latency_seconds","start":"now-3h","end":"now-10m"}' | head -c 600
kill %1
```

Expected: JSON with `"dataset":"d1"` and `"series_count":3`.

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd tests/unit .claude-plugin .mcp.json justfile
git commit -m "feat: MCP query/show tools, serve CLI, plugin manifest"
```

---

### Task 11: Workspace UI (Vite + React + uPlot)

**Files:**
- Create: `ui/` via Vite, then `ui/vite.config.ts`, `ui/src/api.ts`, `ui/src/chart/toUplot.ts`, `ui/src/chart/toUplot.test.ts`, `ui/src/Panel.tsx`, `ui/src/App.tsx`, `ui/src/main.tsx`, `ui/src/index.css`
- Modify: `justfile` (append UI recipes)

**Interfaces:**
- Consumes: HTTP API (Task 9): `GET /api/panels`, `GET /api/panels/{id}/data?width=`, `POST /api/render-report`, `WS /ws`.
- Produces: `toUplot(series: SeriesData[]) -> UplotModel` and `seriesName(labels)` (pure, unit tested); a panel element `<section data-panel-id=… data-render-ms=… data-budget-exceeded="true|false">` that the E2E test (Task 12) relies on, containing the question text, a `<canvas>`, the badge text `y scaled to data`, and a provenance footer.

- [ ] **Step 1: Scaffold**

From the repo root:

```bash
npm create vite@latest ui -- --template react-ts
cd ui && npm install && npm install uplot && npm install -D vitest @playwright/test
```

Delete the Vite demo files `ui/src/App.css`, `ui/src/assets/`. Add scripts to `ui/package.json`: `"test": "vitest run"`, `"e2e": "playwright test"`.

```ts
// ui/vite.config.ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:7070",
      "/ws": { target: "ws://127.0.0.1:7070", ws: true },
    },
  },
  test: { include: ["src/**/*.test.ts"] },
});
```

If TypeScript complains about the `test` key, add `/// <reference types="vitest/config" />` at the top of the file.

Append to `justfile`:

```just
ui-install:
    cd ui && npm install

ui-build:
    cd ui && npm run build

ui-test:
    cd ui && npm test

ui-dev:
    cd ui && npm run dev
```

- [ ] **Step 2: Write the failing transform test**

```ts
// ui/src/chart/toUplot.test.ts
import { describe, expect, it } from "vitest";
import { seriesName, toUplot } from "./toUplot";
import type { SeriesData } from "../api";

const s = (id: string, labels: Record<string, string>, ts: number[], v: number[]): SeriesData => ({
  id, labels, ts, avg: v, min: v.map((x) => x - 1), max: v.map((x) => x + 1), count: v.map(() => 4),
});

describe("toUplot", () => {
  it("aligns series on the union of timestamps with nulls for gaps", () => {
    const m = toUplot([s("a", { i: "a" }, [1000, 2000], [1, 2]), s("b", { i: "b" }, [2000, 3000], [5, 6])]);
    expect(m.data[0]).toEqual([1, 2, 3]); // seconds
    expect(m.data[1]).toEqual([1, 2, null]); // a avg
    expect(m.data[4]).toEqual([null, 5, 6]); // b avg
    expect(m.points).toBe(6);
  });

  it("builds a min/max band per series and never spans gaps", () => {
    const m = toUplot([s("a", { i: "a" }, [1000], [1])]);
    expect(m.bands).toEqual([{ series: [3, 2], fill: expect.stringMatching(/^rgba\(/) }]);
    expect(m.series[1].spanGaps).toBe(false);
  });
});

describe("seriesName", () => {
  it("formats labels PromQL-style", () => {
    expect(seriesName({ __name__: "up", job: "api", a: "1" })).toBe('up{a="1",job="api"}');
    expect(seriesName({})).toBe("{}");
  });
});
```

Run: `cd ui && npm test` → FAIL (module missing).

- [ ] **Step 3: Implement API client and transform**

```ts
// ui/src/api.ts
export interface ChartSpec {
  layers: { mark: string; data: string }[];
  y: { range_mode: "data" | "reference" | "semantic"; unit: string | null; label: string | null };
}
export interface Panel {
  id: string; question: string; status: string; spec: ChartSpec;
  dataset_ids: string[]; created_at_ms: number;
}
export interface SeriesData {
  id: string; labels: Record<string, string>; ts: number[];
  avg: (number | null)[]; min: (number | null)[]; max: (number | null)[]; count: (number | null)[];
}
export interface DatasetMeta {
  id: string; source: string; expr: string; start_ms: number; end_ms: number;
  step_ms: number; resolution_ms: number; representation: string;
}
export interface PanelData {
  panel: Panel; dataset: DatasetMeta; effective_step_ms: number;
  series: SeriesData[]; caveats: string[];
}
export type WorkspaceEvent = { type: string } & Record<string, unknown>;

async function json<T>(resp: Response): Promise<T> {
  if (!resp.ok) throw new Error(`${resp.status} ${await resp.text()}`);
  return resp.json() as Promise<T>;
}

export const fetchPanels = () => fetch("/api/panels").then((r) => json<Panel[]>(r));

export const fetchPanelData = (id: string, width: number) =>
  fetch(`/api/panels/${id}/data?width=${width}`).then((r) => json<PanelData>(r));

export const reportRender = (r: { panel_id: string; render_ms: number; points: number; width_px: number }) =>
  fetch("/api/render-report", {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(r),
  }).then((resp) => json<{ budget_exceeded: boolean }>(resp));

export function subscribe(onEvent: (e: WorkspaceEvent) => void): () => void {
  let socket: WebSocket | null = null;
  let stopped = false;
  const connect = () => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${proto}://${location.host}/ws`);
    socket.onmessage = (m) => onEvent(JSON.parse(m.data));
    socket.onclose = () => { if (!stopped) setTimeout(connect, 1000); };
  };
  connect();
  return () => { stopped = true; socket?.close(); };
}
```

```ts
// ui/src/chart/toUplot.ts
import type uPlot from "uplot";
import type { SeriesData } from "../api";

// Okabe-Ito: colorblind-safe categorical palette. The series budget (≤5) fits it.
export const PALETTE = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9"];

export interface UplotModel {
  data: uPlot.AlignedData;
  series: uPlot.Series[];
  bands: uPlot.Band[];
  points: number;
}

export function seriesName(labels: Record<string, string>): string {
  const { __name__, ...rest } = labels;
  const inner = Object.entries(rest)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k, v]) => `${k}="${v}"`)
    .join(",");
  return `${__name__ ?? ""}{${inner}}`;
}

function rgba(hex: string, alpha: number): string {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

export function toUplot(series: SeriesData[]): UplotModel {
  const xs = [...new Set(series.flatMap((s) => s.ts))].sort((a, b) => a - b);
  const index = new Map(xs.map((t, i) => [t, i]));
  const data: (number | null)[][] = [xs.map((t) => t / 1000)];
  const uSeries: uPlot.Series[] = [{}];
  const bands: uPlot.Band[] = [];

  series.forEach((s, k) => {
    const color = PALETTE[k % PALETTE.length];
    const column = (values: (number | null)[]) => {
      const out: (number | null)[] = new Array(xs.length).fill(null);
      s.ts.forEach((t, i) => { out[index.get(t)!] = values[i]; });
      return out;
    };
    const avgIdx = data.length;
    data.push(column(s.avg), column(s.min), column(s.max));
    const name = seriesName(s.labels);
    uSeries.push(
      { label: name, stroke: color, width: 1.5, spanGaps: false },
      { label: `${name} min`, stroke: rgba(color, 0.35), width: 0.5, spanGaps: false, points: { show: false } },
      { label: `${name} max`, stroke: rgba(color, 0.35), width: 0.5, spanGaps: false, points: { show: false } },
    );
    bands.push({ series: [avgIdx + 2, avgIdx + 1], fill: rgba(color, 0.15) });
  });

  return { data: data as uPlot.AlignedData, series: uSeries, bands, points: xs.length * series.length };
}
```

Run: `cd ui && npm test` → PASS.

- [ ] **Step 4: Implement components**

```tsx
// ui/src/Panel.tsx
import { useEffect, useRef, useState } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import { fetchPanelData, reportRender, type Panel, type PanelData } from "./api";
import { toUplot } from "./chart/toUplot";

const fmtTime = (ms: number) => new Date(ms).toISOString().replace(".000Z", "Z");
const fmtStep = (ms: number) => (ms % 60_000 === 0 ? `${ms / 60_000}m` : `${ms / 1000}s`);

export function PanelView({ panel }: { panel: Panel }) {
  const plotRef = useRef<HTMLDivElement>(null);
  const [data, setData] = useState<PanelData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [render, setRender] = useState<{ ms: number; exceeded: boolean } | null>(null);

  useEffect(() => {
    const width = Math.round(plotRef.current?.clientWidth || 800);
    fetchPanelData(panel.id, width).then(setData).catch((e) => setError(String(e)));
  }, [panel.id]);

  useEffect(() => {
    const el = plotRef.current;
    if (!data || !el) return;
    const model = toUplot(data.series);
    const width = el.clientWidth || 800;
    const unit = data.panel.spec.y.unit;
    const t0 = performance.now();
    const plot = new uPlot(
      {
        width, height: 260, series: model.series, bands: model.bands,
        scales: { x: { time: true } },
        axes: [{}, { label: unit ?? "value (unit unknown)" }],
      },
      model.data, el,
    );
    const ms = performance.now() - t0;
    reportRender({ panel_id: panel.id, render_ms: ms, points: model.points, width_px: width })
      .then((r) => setRender({ ms, exceeded: r.budget_exceeded }))
      .catch((e) => setError(String(e)));
    return () => plot.destroy();
  }, [data, panel.id]);

  return (
    <section
      className="panel"
      id={`panel-${panel.id}`}
      data-panel-id={panel.id}
      data-render-ms={render ? render.ms.toFixed(1) : undefined}
      data-budget-exceeded={render ? String(render.exceeded) : undefined}
    >
      <header>
        <span className="question">Q: {panel.question}</span>
        <span className={`status ${panel.status}`}>{panel.status}</span>
      </header>
      {panel.spec.y.range_mode === "data" && (
        <div className="badge">y scaled to data (no reference range yet)</div>
      )}
      {error && <div className="error">{error}</div>}
      <div ref={plotRef} className="plot" />
      {data && (
        <footer>
          {data.dataset.source} · <code>{data.dataset.expr}</code> ·{" "}
          {fmtTime(data.dataset.start_ms)} – {fmtTime(data.dataset.end_ms)} · step{" "}
          {fmtStep(data.effective_step_ms)} · {data.dataset.representation}, min/max envelope
          {data.caveats.length > 0 && <> · caveats: {data.caveats.join(", ")}</>}
        </footer>
      )}
    </section>
  );
}
```

```tsx
// ui/src/App.tsx
import { useEffect, useState } from "react";
import { fetchPanels, subscribe, type Panel } from "./api";
import { PanelView } from "./Panel";

export default function App() {
  const [panels, setPanels] = useState<Panel[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = () => fetchPanels().then(setPanels).catch((e) => setError(String(e)));
    load();
    return subscribe((e) => { if (e.type === "panel.created") load(); });
  }, []);

  useEffect(() => {
    const match = location.hash.match(/^#\/panel\/(\w+)$/);
    if (match) document.getElementById(`panel-${match[1]}`)?.scrollIntoView();
  }, [panels]);

  return (
    <main>
      <h1>Telemetry Nerd</h1>
      {error && <div className="error">{error}</div>}
      {panels.length === 0 && <p className="empty">No panels yet. Ask Claude a question about your metrics.</p>}
      {panels.map((p) => <PanelView key={p.id} panel={p} />)}
    </main>
  );
}
```

```tsx
// ui/src/main.tsx
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
```

```css
/* ui/src/index.css */
:root {
  --bg: #ffffff; --fg: #1a1a1a; --muted: #666; --border: #ddd; --badge: #fff4d6; --error: #b00020;
  font-family: system-ui, sans-serif; color: var(--fg); background: var(--bg);
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #16181d; --fg: #e6e6e6; --muted: #9aa0a6; --border: #333; --badge: #3a3220; --error: #ff6b6b; }
}
body { margin: 0; background: var(--bg); }
main { max-width: 1200px; margin: 0 auto; padding: 16px; }
.panel { border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 16px; }
.panel header { display: flex; justify-content: space-between; font-weight: 600; margin-bottom: 6px; }
.panel .status { font-weight: 400; color: var(--muted); }
.panel .badge { display: inline-block; background: var(--badge); font-size: 12px; padding: 2px 6px; border-radius: 4px; }
.panel footer { font-size: 12px; color: var(--muted); margin-top: 6px; }
.error { color: var(--error); }
.empty { color: var(--muted); }
```

In StrictMode React runs effects twice in development, which creates and destroys the plot once and reports a render twice. That's harmless; the production build (used by E2E) runs once.

- [ ] **Step 5: Build and check by hand**

```bash
just ui-build
just dev-up && just seed
just serve &
curl -s -XPOST localhost:7070/api/query -H 'content-type: application/json' \
  -d '{"expr":"tn_demo_latency_seconds","start":"now-3h","end":"now-10m"}' | jq -r .dataset
curl -s -XPOST localhost:7070/api/show -H 'content-type: application/json' \
  -d '{"dataset":"d1","question":"Is the instance c spike visible?"}'
open http://127.0.0.1:7070   # expect one panel: 3 lines with shaded envelopes, spike on c
kill %1
```

- [ ] **Step 6: Commit**

```bash
git add ui justfile
git commit -m "feat: workspace UI rendering line+envelope panels"
```

---

### Task 12: End-to-end skeleton test

**Files:**
- Create: `ui/playwright.config.ts`, `ui/e2e/global-setup.ts`, `ui/e2e/skeleton.spec.ts`
- Modify: `justfile` (append `e2e`)

**Interfaces:**
- Consumes: everything. VictoriaMetrics from `just dev-up` on `127.0.0.1:8428`; seed script (Task 3); server CLI with `--no-mcp` (Task 10); UI data attributes (Task 11).
- Produces: `just e2e`, the M1 acceptance gate.

- [ ] **Step 1: Write the config, setup, and test**

```ts
// ui/playwright.config.ts
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "e2e",
  globalSetup: "./e2e/global-setup.ts",
  use: { baseURL: "http://127.0.0.1:7071" },
  webServer: {
    command:
      'uv run --directory .. telemetry-nerd serve --no-mcp --port 7071 --data-dir "$(mktemp -d)" --source-url http://127.0.0.1:8428',
    url: "http://127.0.0.1:7071/api/panels",
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
```

```ts
// ui/e2e/global-setup.ts
import { execSync } from "node:child_process";

export default function globalSetup() {
  execSync("uv run python scripts/seed_synthetic.py --url http://127.0.0.1:8428 --hours 6", {
    cwd: "..",
    stdio: "inherit",
  });
}
```

```ts
// ui/e2e/skeleton.spec.ts
import { expect, test } from "@playwright/test";

test("query → show → panel renders envelope within budget, peak preserved", async ({ page, request }) => {
  const q = await request.post("/api/query", {
    data: { expr: "tn_demo_latency_seconds", start: "now-3h", end: "now-10m", step: "1m" },
  });
  expect(q.ok()).toBeTruthy();
  const { dataset, summary } = await q.json();
  expect(summary.series_count).toBe(3);

  const question = "Is instance c's latency spike visible without peak erosion?";
  const s = await request.post("/api/show", { data: { dataset, question } });
  expect(s.ok()).toBeTruthy();
  const { panel } = await s.json();

  // Heavy downsampling (≈170 buckets → 50px) must keep the 1.5s spike in the max envelope.
  const d = await (await request.get(`/api/panels/${panel.id}/data?width=50`)).json();
  const peak = Math.max(...d.series.flatMap((x: { max: (number | null)[] }) => x.max.filter((v) => v !== null)));
  expect(peak).toBeGreaterThanOrEqual(1.5);

  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.getByText(question)).toBeVisible();
  await expect(el.locator("canvas").first()).toBeVisible();
  await expect(el.getByText("y scaled to data")).toBeVisible();
  await expect(el).toHaveAttribute("data-budget-exceeded", "false");
});

test("a panel without a question is rejected", async ({ request }) => {
  const q = await request.post("/api/query", { data: { expr: "tn_demo_latency_seconds", start: "now-1h", end: "now-10m" } });
  const { dataset } = await q.json();
  const s = await request.post("/api/show", { data: { dataset, question: "" } });
  expect(s.status()).toBe(400);
});
```

Append to `justfile`:

```just
e2e: dev-up ui-build
    cd ui && npx playwright install chromium && npx playwright test
```

- [ ] **Step 2: Run the gate**

Run: `just e2e`
Expected: 2 passed. If the render budget fails, do **not** raise the budget; check `points` vs `width_px` in the report and fix the aggregation (spec §6.5).

- [ ] **Step 3: Full verification**

Run: `just lint && just test && just test-integration && just ui-test && just e2e`
Expected: everything passes. Fix lint issues with `just fmt`.

- [ ] **Step 4: Commit**

```bash
git add ui justfile
git commit -m "test: end-to-end walking skeleton gate"
```

---

## Milestone acceptance (M1)

- `just e2e` passes: an expression becomes a dataset, then a question-bearing panel with a min/max envelope, rendered within budget, with the peak preserved under downsampling.
- In Claude Code with the plugin loaded (`claude --plugin-dir .`), asking "show me demo latency for the last 3 hours" leads Claude to call `query` then `show` and share a working panel URL.
- All unit, integration, and UI tests pass; `just lint` is clean.
