# Runtime Source Management — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Bead: `telemetry-nerd-2as.1` (epic M3 `telemetry-nerd-2as`).

**Goal:** Claude adds, lists, checks and removes metric sources on a running daemon through MCP tools — no env vars, no CLI flags, no restart — with secrets passed only by reference.

**Architecture:** A `SourceSpec` (pydantic, `extra="forbid"`) is the only thing Claude can configure; it carries a secret *reference* (`auth.env` or `auth.file`), never a secret. A `SourceRegistry` (a `Mapping[str, Source]`) keeps live sources, persists specs in the workspace SQLite DB, and reloads them at startup. `PromQLSource` gains per-request headers (User-Agent, Authorization), a politeness `Gate` (max concurrency + min spacing) and a cheap `probe()`. `TelemetryService` exposes `source_connect/list/status/disconnect`; MCP tools and `GET /api/sources` call them. The `default` source stays owned by daemon settings (in-memory, never persisted, not editable via MCP).

**Tech Stack:** Python ≥3.12 (venv 3.14), uv, pydantic v2, httpx 0.28, sqlite3, mcp 2.x (`mcp.server.mcpserver.MCPServer`), pytest + pytest-asyncio (`asyncio_mode=auto`) + respx.

**Spec:** `docs/superpowers/specs/2026-09-30-telemetry-nerd-mvp-design.md` §4.1 (adapter), §7.1 (`source_connect`, `source_status`), §1.2 principle 6–7. Context: `docs/superpowers/specs/2026-10-01-public-test-sources.md` (Grafana proxy URL pattern, politeness).

## Global Constraints

- Work directly on `master`. Commit after every task; trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Do not close the bead until Task 5 passes.
- Python via `uv` only; tasks via `just`; search via `rg`/`fd`. `just lint` and `just test` must pass after every task.
- **mcp is 2.x.** Confirm APIs in `.venv/lib/python3*/site-packages/mcp/` with `rg` before use. Existing patterns: `from mcp.server.mcpserver import MCPServer`, `from mcp.server.mcpserver.exceptions import ToolError`; tests use `from mcp import Client`.
- **Secrets never leave the daemon.** No raw token is accepted by any tool or API argument, stored in SQLite, written to the event log, logged, or returned by any tool/API. Only the reference (env var name or file path) and a `set: bool` flag are ever shown.
- Every mutation appends exactly one event via `EventLog.append` (`source.connected`, `source.disconnected`). Actors are exactly `claude` | `user` | `system`.
- Logging goes to stderr; nothing prints to stdout in the daemon.
- JSON responses never contain NaN/Inf (`telemetry_nerd.model.jsonsafe`).
- Name `default` is reserved for the settings-seeded source: `source_connect`/`source_disconnect` refuse it.

## File Structure

```
src/telemetry_nerd/sources/spec.py       # NEW SourceSpec, AuthRef, Politeness, MissingSecret
src/telemetry_nerd/sources/gate.py       # NEW Gate: per-source concurrency + min request spacing
src/telemetry_nerd/sources/promql.py     # MOD headers, gate, probe(), aclose(), from_spec(); _get_json() refactor
src/telemetry_nerd/sources/base.py       # MOD Source protocol gains probe()
src/telemetry_nerd/sources/registry.py   # NEW SourceRegistry (Mapping) with SQLite persistence
src/telemetry_nerd/workspace/db.py       # MOD `sources` table
src/telemetry_nerd/core/service.py       # MOD sources: SourceRegistry; source_* methods
src/telemetry_nerd/core/bootstrap.py     # MOD build registry, load persisted, attach settings default
src/telemetry_nerd/mcp/server.py         # MOD source_connect/list/status/disconnect tools; INSTRUCTIONS
src/telemetry_nerd/api/app.py            # MOD GET /api/sources
tests/unit/fakes.py                      # MOD FakeSource.probe/aclose; make_service builds a registry
tests/unit/test_source_spec.py           # NEW
tests/unit/test_gate.py                  # NEW
tests/unit/test_promql.py                # MOD headers, probe
tests/unit/test_source_registry.py       # NEW
tests/unit/test_service_sources.py       # NEW
tests/unit/test_mcp_sources.py           # NEW
tests/unit/test_bootstrap.py             # MOD persistence across rebuild
tests/integration/test_sources_vm.py     # NEW connect a second source at runtime against VM
```

## Scope notes

- Flavor auto-detection and Grafana datasource discovery are `telemetry-nerd-3fs.2`. Here `flavor` is an argument (default `prometheus`, which also works against VictoriaMetrics via `*_over_time`).
- Native-resolution inference is `telemetry-nerd-2as.2`. Here `resolution` is an argument (default `15s`).
- UI listing of sources is out of scope; `GET /api/sources` exists for it.
- Sources live in the workspace DB for MVP even though they are daemon-level; multi-workspace (`telemetry-nerd-3fs.1`) can move them later.

---

### Task 1: SourceSpec — what Claude may configure, secrets by reference

**Files:**
- Create: `src/telemetry_nerd/sources/spec.py`
- Test: `tests/unit/test_source_spec.py`

**Interfaces:**
- Consumes: `telemetry_nerd.sources.base.SourceError`
- Produces:
  - `NAME_PATTERN: str`, `RESERVED_NAMES: frozenset[str]` (`{"default"}`)
  - `class MissingSecret(SourceError)`
  - `class AuthRef(BaseModel)`: fields `env: str | None`, `file: str | None`, `scheme: Literal["bearer", "basic"] = "bearer"`; methods `headers(environ: Mapping[str, str] = os.environ) -> dict[str, str]` (raises `MissingSecret`), `is_set(environ=os.environ) -> bool`, `public(environ=os.environ) -> dict`
  - `class Politeness(BaseModel)`: `max_concurrency: int = 4` (1–32), `min_interval_ms: int = 0` (0–60000), `timeout_s: float = 30.0` (>0, ≤300)
  - `class SourceSpec(BaseModel)`: `name: str`, `url: str` (normalized, no trailing slash), `flavor: Literal["prometheus", "victoriametrics"] = "prometheus"`, `resolution_ms: int = 15_000` (1000–3600000), `auth: AuthRef | None = None`, `politeness: Politeness`; method `public(environ=os.environ) -> dict`
  - All three models: `ConfigDict(extra="forbid", frozen=True)`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_source_spec.py
import json

import pytest
from pydantic import ValidationError

from telemetry_nerd.sources.spec import AuthRef, MissingSecret, Politeness, SourceSpec

GRAFANA = "https://play.grafana.org/api/datasources/proxy/uid/grafanacloud-prom"


def test_minimal_spec_defaults():
    spec = SourceSpec(name="play", url=GRAFANA + "/")
    assert spec.url == GRAFANA  # trailing slash stripped
    assert spec.flavor == "prometheus"
    assert spec.resolution_ms == 15_000
    assert spec.auth is None
    assert spec.politeness == Politeness()


@pytest.mark.parametrize("name", ["Play", "1play", "", "a" * 33, "has space", "dots.no"])
def test_name_pattern(name):
    with pytest.raises(ValidationError):
        SourceSpec(name=name, url=GRAFANA)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com",
        "example.com",
        "https://user:pass@example.com",
        "https://example.com/api?token=abc",
        "https://example.com/#frag",
    ],
)
def test_url_rejects_non_http_credentials_query_and_fragment(url):
    with pytest.raises(ValidationError):
        SourceSpec(name="x", url=url)


def test_unknown_fields_are_rejected_so_tokens_cannot_sneak_in():
    with pytest.raises(ValidationError):
        SourceSpec.model_validate({"name": "x", "url": GRAFANA, "token": "glsa_abc"})
    with pytest.raises(ValidationError):
        AuthRef.model_validate({"env": "TOKEN", "value": "glsa_abc"})


@pytest.mark.parametrize("env", ["glsa_0123456789abcdef", "Bearer abc", "lower_case", "A-B", ""])
def test_auth_env_must_be_a_variable_name(env):
    with pytest.raises(ValidationError, match="NAME of an environment variable"):
        AuthRef(env=env)


def test_auth_needs_exactly_one_reference(tmp_path):
    with pytest.raises(ValidationError):
        AuthRef()
    with pytest.raises(ValidationError):
        AuthRef(env="TOKEN", file=str(tmp_path / "t"))


def test_auth_file_must_be_absolute():
    with pytest.raises(ValidationError, match="absolute"):
        AuthRef(file="relative/token.txt")


def test_bearer_header_from_env():
    assert AuthRef(env="TN_T").headers({"TN_T": "s3cr3t"}) == {"Authorization": "Bearer s3cr3t"}


def test_basic_header_from_file(tmp_path):
    f = tmp_path / "cred"
    f.write_text("user:pw\n")
    assert AuthRef(file=str(f), scheme="basic").headers({}) == {
        "Authorization": "Basic dXNlcjpwdw=="
    }


def test_missing_secret_has_actionable_hint(tmp_path):
    with pytest.raises(MissingSecret) as e:
        AuthRef(env="TN_NOPE").headers({})
    assert "TN_NOPE" in str(e.value)
    assert e.value.hint and "daemon" in e.value.hint
    with pytest.raises(MissingSecret):
        AuthRef(file=str(tmp_path / "missing")).headers({})


def test_public_view_never_contains_the_secret():
    spec = SourceSpec(name="x", url=GRAFANA, auth=AuthRef(env="TN_T"))
    env = {"TN_T": "s3cr3t-value"}
    view = spec.public(env)
    assert "s3cr3t-value" not in json.dumps(view)
    assert view["auth"] == {"env": "TN_T", "file": None, "scheme": "bearer", "set": True}
    assert spec.public({})["auth"]["set"] is False
    assert view["name"] == "x" and view["url"] == GRAFANA
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_source_spec.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'telemetry_nerd.sources.spec'`

- [ ] **Step 3: Implement**

```python
# src/telemetry_nerd/sources/spec.py
"""What Claude may configure about a source at runtime.

Secrets are references (an environment variable name or a file path) resolved
inside the daemon; a secret value is never an argument, stored, logged or returned.
"""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from telemetry_nerd.sources.base import SourceError

NAME_PATTERN = r"^[a-z][a-z0-9_-]{0,31}$"
RESERVED_NAMES = frozenset({"default"})
_ENV_VAR = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")


class MissingSecret(SourceError):
    pass


class AuthRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    env: str | None = None
    file: str | None = None
    scheme: Literal["bearer", "basic"] = "bearer"

    @field_validator("env")
    @classmethod
    def _env_is_a_name(cls, v: str | None) -> str | None:
        if v is not None and not _ENV_VAR.match(v):
            raise ValueError(
                "auth env must be the NAME of an environment variable (e.g. GRAFANA_TOKEN) "
                "in the daemon's environment, never the secret itself"
            )
        return v

    @field_validator("file")
    @classmethod
    def _file_is_absolute(cls, v: str | None) -> str | None:
        if v is not None and not Path(v).is_absolute():
            raise ValueError("auth file must be an absolute path readable by the daemon")
        return v

    @model_validator(mode="after")
    def _exactly_one(self) -> AuthRef:
        if (self.env is None) == (self.file is None):
            raise ValueError("auth needs exactly one of env or file")
        return self

    def _secret(self, environ: Mapping[str, str]) -> str | None:
        if self.env is not None:
            return environ.get(self.env) or None
        try:
            return Path(self.file).read_text().strip() or None  # type: ignore[arg-type]
        except OSError:
            return None

    def is_set(self, environ: Mapping[str, str] = os.environ) -> bool:
        return self._secret(environ) is not None

    def headers(self, environ: Mapping[str, str] = os.environ) -> dict[str, str]:
        secret = self._secret(environ)
        if secret is None:
            where = f"environment variable {self.env}" if self.env else f"file {self.file}"
            raise MissingSecret(
                f"secret not found: {where} is unset or empty",
                hint=(
                    "ask the user to write the token to that file, or to export the variable "
                    "in the daemon's environment (env vars need a daemon restart; files do not)"
                ),
            )
        if self.scheme == "basic":
            return {"Authorization": "Basic " + base64.b64encode(secret.encode()).decode()}
        return {"Authorization": f"Bearer {secret}"}

    def public(self, environ: Mapping[str, str] = os.environ) -> dict:
        return {"env": self.env, "file": self.file, "scheme": self.scheme, "set": self.is_set(environ)}


class Politeness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_concurrency: int = Field(4, ge=1, le=32)
    min_interval_ms: int = Field(0, ge=0, le=60_000)
    timeout_s: float = Field(30.0, gt=0, le=300)


class SourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=NAME_PATTERN)
    url: str
    flavor: Literal["prometheus", "victoriametrics"] = "prometheus"
    resolution_ms: int = Field(15_000, ge=1_000, le=3_600_000)
    auth: AuthRef | None = None
    politeness: Politeness = Field(default_factory=Politeness)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("url must look like http(s)://host[:port][/path]")
        if parts.username or parts.password:
            raise ValueError(
                "credentials in the url are not allowed; put the secret in a file or env var "
                "and reference it with auth"
            )
        if parts.query or parts.fragment:
            raise ValueError("url must not contain a query string or fragment")
        return v.rstrip("/")

    def public(self, environ: Mapping[str, str] = os.environ) -> dict:
        out = self.model_dump()
        out["auth"] = self.auth.public(environ) if self.auth else None
        return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_source_spec.py -q && just lint`
Expected: all PASS, lint clean.

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/sources/spec.py tests/unit/test_source_spec.py
git commit -m "feat(sources): SourceSpec with secret references (2as.1)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: PromQLSource — headers, politeness gate, probe

**Files:**
- Create: `src/telemetry_nerd/sources/gate.py`
- Modify: `src/telemetry_nerd/sources/promql.py` (constructor, `_query_range` → `_get_json`, new `probe`, `aclose`, `from_spec`)
- Modify: `src/telemetry_nerd/sources/base.py` (`Source` protocol gains `probe`)
- Test: `tests/unit/test_gate.py`, `tests/unit/test_promql.py` (append)

**Interfaces:**
- Consumes: `SourceSpec`, `MissingSecret` (Task 1)
- Produces:
  - `class Gate(max_concurrency: int = 4, min_interval_ms: int = 0, *, clock=time.monotonic, sleep=asyncio.sleep)` with `slot()` async context manager
  - `PromQLSource.__init__(..., headers: Mapping[str, str] | None = None, gate: Gate | None = None)` (existing params unchanged)
  - `PromQLSource.from_spec(spec: SourceSpec, environ: Mapping[str, str] = os.environ, client: httpx.AsyncClient | None = None) -> PromQLSource` (raises `MissingSecret`)
  - `async PromQLSource.probe() -> dict` → `{"reachable": True, "latency_ms": int, "application"?: str, "version"?: str}`; raises `SourceError`/`SourceUnavailable` when unreachable
  - `async PromQLSource.aclose() -> None` (closes only a client it created)
  - `USER_AGENT: str` in `promql.py`
  - `Source` protocol: `async def probe(self) -> dict: ...`

- [ ] **Step 1: Write the failing gate tests**

```python
# tests/unit/test_gate.py
import asyncio

from telemetry_nerd.sources.gate import Gate


async def test_concurrency_is_bounded():
    gate = Gate(max_concurrency=2)
    active = peak = 0

    async def work():
        nonlocal active, peak
        async with gate.slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(work() for _ in range(6)))
    assert peak == 2


async def test_min_interval_spaces_request_starts():
    now = [100.0]
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    gate = Gate(max_concurrency=4, min_interval_ms=500, clock=lambda: now[0], sleep=fake_sleep)
    for _ in range(3):
        async with gate.slot():
            pass
    assert slept == [0.5, 0.5]


async def test_zero_interval_never_sleeps():
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    gate = Gate(min_interval_ms=0, sleep=fake_sleep)
    for _ in range(3):
        async with gate.slot():
            pass
    assert slept == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_gate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'telemetry_nerd.sources.gate'`

- [ ] **Step 3: Implement Gate**

```python
# src/telemetry_nerd/sources/gate.py
"""Per-source politeness: bounded concurrency and minimum spacing between request starts."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager


class Gate:
    def __init__(
        self,
        max_concurrency: int = 4,
        min_interval_ms: int = 0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sem = asyncio.Semaphore(max_concurrency)
        self._spacing = asyncio.Lock()
        self._interval = min_interval_ms / 1000
        self._clock = clock
        self._sleep = sleep
        self._next_start: float | None = None

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        async with self._sem:
            if self._interval:
                async with self._spacing:
                    if self._next_start is not None:
                        wait = self._next_start - self._clock()
                        if wait > 0:
                            await self._sleep(wait)
                    self._next_start = self._clock() + self._interval
            yield
```

- [ ] **Step 4: Run gate tests**

Run: `uv run pytest tests/unit/test_gate.py -q`
Expected: PASS

- [ ] **Step 5: Write failing PromQLSource tests (append to `tests/unit/test_promql.py`; move the two new `from telemetry_nerd...` imports into the file's top import block, or ruff E402 fails)**

```python
# --- appended to tests/unit/test_promql.py ---
from telemetry_nerd.sources.promql import USER_AGENT
from telemetry_nerd.sources.spec import AuthRef, MissingSecret, SourceSpec


@respx.mock
async def test_from_spec_sends_user_agent_and_auth():
    route = respx.get(**ROUTE).mock(
        return_value=httpx.Response(200, json=matrix([]))
    )
    spec = SourceSpec(name="s", url=BASE, auth=AuthRef(env="TN_T"))
    src = PromQLSource.from_spec(spec, environ={"TN_T": "tok"})
    await src.fetch("up", RNG, 60_000)
    sent = route.calls.last.request.headers
    assert sent["user-agent"] == USER_AGENT
    assert sent["authorization"] == "Bearer tok"
    await src.aclose()


def test_from_spec_missing_secret_raises():
    spec = SourceSpec(name="s", url=BASE, auth=AuthRef(env="TN_NOPE"))
    with pytest.raises(MissingSecret):
        PromQLSource.from_spec(spec, environ={})


def test_from_spec_maps_politeness_and_resolution():
    spec = SourceSpec(
        name="s", url=BASE, flavor="victoriametrics", resolution_ms=20_000,
        politeness={"timeout_s": 90},
    )
    src = PromQLSource.from_spec(spec, environ={})
    assert src.flavor == "victoriametrics"
    assert src.resolution_ms == 20_000
    assert src.limits.timeout_s == 90
    assert src.name == "s"


@respx.mock
async def test_probe_reports_buildinfo():
    respx.get(host="vm.test", path="/api/v1/status/buildinfo").mock(
        return_value=httpx.Response(
            200, json={"status": "success", "data": {"application": "Grafana Mimir", "version": "2.15.0"}}
        )
    )
    out = await PromQLSource("s", BASE).probe()
    assert out["reachable"] is True
    assert out["application"] == "Grafana Mimir"
    assert out["version"] == "2.15.0"
    assert isinstance(out["latency_ms"], int)


@respx.mock
async def test_probe_falls_back_to_trivial_query_when_buildinfo_hidden():
    respx.get(host="vm.test", path="/api/v1/status/buildinfo").mock(
        return_value=httpx.Response(404, text="404 page not found")
    )
    q = respx.get(host="vm.test", path="/api/v1/query").mock(
        return_value=httpx.Response(
            200, json={"status": "success", "data": {"resultType": "scalar", "result": [0, "1"]}}
        )
    )
    out = await PromQLSource("s", BASE).probe()
    assert out["reachable"] is True
    assert q.calls.last.request.url.params["query"] == "1"


@respx.mock
async def test_probe_unreachable_raises_source_unavailable():
    respx.get(host="vm.test", path="/api/v1/status/buildinfo").mock(side_effect=httpx.ConnectError("refused"))
    respx.get(host="vm.test", path="/api/v1/query").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(SourceUnavailable):
        await PromQLSource("s", BASE).probe()
```

- [ ] **Step 6: Run to verify failure**

Run: `uv run pytest tests/unit/test_promql.py -q`
Expected: new tests FAIL (`ImportError: cannot import name 'USER_AGENT'`); old tests still pass once the import exists.

- [ ] **Step 7: Implement in `promql.py`**

Add imports and constant near the top:

```python
import os
import time
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version

from telemetry_nerd.sources.gate import Gate
from telemetry_nerd.sources.spec import SourceSpec


def _user_agent() -> str:
    try:
        ver = version("telemetry-nerd")
    except PackageNotFoundError:
        ver = "dev"
    return f"telemetry-nerd/{ver} (+https://github.com/Fewbytes/telemtry-nerd)"


USER_AGENT = _user_agent()
```

Constructor: add `headers` and `gate` keyword parameters and track client ownership:

```python
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
```

Split `_query_range` into a generic `_get_json` (transport, HTTP status, JSON, `status == "success"`) plus the matrix checks. Keep every existing error message and hint byte-for-byte (existing tests assert them):

```python
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
```

In `base.py`, extend the protocol:

```python
class Source(Protocol):
    name: str
    identity: str  # stable id of what this source reads (flavor, endpoint, resolution)
    resolution_ms: int

    async def fetch(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult: ...

    async def probe(self) -> dict: ...
```

Also update `FakeSource` and `NonFiniteSource` in `tests/unit/fakes.py` so they keep satisfying the protocol:

```python
    async def probe(self) -> dict:
        return {"reachable": True, "latency_ms": 0, "version": "fake"}
```

(add to `FakeSource`; `NonFiniteSource` subclasses `FakeSource` and inherits it).

- [ ] **Step 8: Run all unit tests**

Run: `just test && just lint`
Expected: PASS (all old `test_promql.py` tests unchanged and green).

- [ ] **Step 9: Commit**

```bash
git add src/telemetry_nerd/sources/gate.py src/telemetry_nerd/sources/promql.py src/telemetry_nerd/sources/base.py tests/unit/test_gate.py tests/unit/test_promql.py tests/unit/fakes.py
git commit -m "feat(sources): polite PromQL client with auth headers and probe (2as.1)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: SourceRegistry, persistence, service operations, bootstrap

**Files:**
- Create: `src/telemetry_nerd/sources/registry.py`
- Modify: `src/telemetry_nerd/workspace/db.py` (add `sources` table to `_SCHEMA`)
- Modify: `src/telemetry_nerd/core/service.py` (`sources: SourceRegistry`; new methods)
- Modify: `src/telemetry_nerd/core/bootstrap.py`
- Modify: `tests/unit/fakes.py` (`make_service` builds a registry)
- Test: `tests/unit/test_source_registry.py`, `tests/unit/test_service_sources.py`, `tests/unit/test_bootstrap.py`

**Interfaces:**
- Consumes: `SourceSpec`, `RESERVED_NAMES`, `MissingSecret` (Task 1); `PromQLSource.from_spec`, `Source.probe` (Task 2)
- Produces:
  - `class SourceRegistry(Mapping[str, Source])` — `__init__(db: sqlite3.Connection, factory: Callable[[SourceSpec], Source], clock: Callable[[], int] = now_ms)`; `load() -> None`; `build(spec) -> Source`; `add(spec, source, *, replace=False) -> Source | None` (persists; returns replaced live source); `attach(name, source, spec=None) -> None` (in-memory only); `remove(name) -> Source | None` (deletes persisted spec); `spec(name) -> SourceSpec | None`; `describe() -> list[dict]`
  - `TelemetryService.sources: SourceRegistry`
  - `async TelemetryService.source_connect(spec: SourceSpec, *, replace: bool = False, actor: Actor = "claude") -> dict` → `{"source": <public>, "status": <probe>}`
  - `TelemetryService.source_list() -> list[dict]`
  - `async TelemetryService.source_status(name: str) -> dict`
  - `async TelemetryService.source_disconnect(name: str, actor: Actor = "claude") -> None`
  - Events: `source.connected` / `source.disconnected`, `object_id` = source name, payload `{"source": <public view>}` / `{}`

- [ ] **Step 1: Add the table**

In `src/telemetry_nerd/workspace/db.py`, append to `_SCHEMA` (inside the string, before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS sources (
    name TEXT PRIMARY KEY,
    spec TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);
```

- [ ] **Step 2: Write failing registry tests**

```python
# tests/unit/test_source_registry.py
import pytest

from telemetry_nerd.sources.base import SourceError
from telemetry_nerd.sources.registry import SourceRegistry
from telemetry_nerd.sources.spec import AuthRef, MissingSecret, SourceSpec
from telemetry_nerd.workspace.db import open_workspace_db
from tests.unit.fakes import FakeSource


def fake_factory(spec: SourceSpec):
    return FakeSource(name=spec.name, identity=f"fake|{spec.url}")


def make(tmp_path, factory=fake_factory):
    return SourceRegistry(open_workspace_db(tmp_path / "w.db"), factory, clock=lambda: 1)


def spec(name="play", url="https://play.test/prom", **kw):
    return SourceSpec(name=name, url=url, **kw)


def test_add_get_iterate_and_persist(tmp_path):
    reg = make(tmp_path)
    s = spec()
    reg.add(s, reg.build(s))
    assert set(reg) == {"play"} and len(reg) == 1
    assert reg["play"].name == "play"
    assert reg.get("nope") is None

    again = make(tmp_path)
    again.load()
    assert set(again) == {"play"}
    assert again.spec("play") == s


def test_add_existing_name_requires_replace(tmp_path):
    reg = make(tmp_path)
    s = spec()
    reg.add(s, reg.build(s))
    with pytest.raises(SourceError, match="already exists") as e:
        reg.add(s, reg.build(s))
    assert "replace" in (e.value.hint or "")
    old = reg["play"]
    s2 = spec(url="https://other.test")
    assert reg.add(s2, reg.build(s2), replace=True) is old
    assert reg.spec("play") == s2


def test_remove_deletes_persisted_spec(tmp_path):
    reg = make(tmp_path)
    s = spec()
    reg.add(s, reg.build(s))
    assert reg.remove("play") is not None
    assert "play" not in reg
    fresh = make(tmp_path)
    fresh.load()
    assert "play" not in fresh


def test_remove_unknown_raises_with_hint(tmp_path):
    with pytest.raises(SourceError) as e:
        make(tmp_path).remove("ghost")
    assert "source_list" in (e.value.hint or "")


def test_attach_is_in_memory_only(tmp_path):
    reg = make(tmp_path)
    reg.attach("default", FakeSource(name="default"))
    assert "default" in reg
    fresh = make(tmp_path)
    fresh.load()
    assert "default" not in fresh


def test_load_keeps_specs_whose_secret_is_missing_as_broken(tmp_path):
    def strict(s: SourceSpec):
        if s.auth:
            raise MissingSecret("secret not found: environment variable TN_X is unset", hint="h")
        return fake_factory(s)

    reg = make(tmp_path, strict)
    s = spec(auth=AuthRef(env="TN_X"))
    reg.add(s, FakeSource(name="play"))  # persisted while the secret existed
    fresh = make(tmp_path, strict)
    fresh.load()
    assert "play" not in fresh  # not live
    [entry] = fresh.describe()
    assert entry["name"] == "play"
    assert "TN_X" in entry["broken"]


def test_describe_is_public_and_marks_settings_owned(tmp_path):
    reg = make(tmp_path)
    reg.attach("default", FakeSource(name="default"), spec("default", "http://127.0.0.1:8428"))
    s = spec()
    reg.add(s, reg.build(s))
    by_name = {d["name"]: d for d in reg.describe()}
    assert by_name["default"]["managed_by"] == "settings"
    assert by_name["play"]["managed_by"] == "runtime"
    assert by_name["play"]["url"] == "https://play.test/prom"
    assert by_name["play"]["broken"] is None
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/unit/test_source_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'telemetry_nerd.sources.registry'`

- [ ] **Step 4: Implement the registry**

```python
# src/telemetry_nerd/sources/registry.py
"""Live sources by name. Runtime-added specs persist in SQLite and reload at startup."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Mapping

from telemetry_nerd.model.time import now_ms
from telemetry_nerd.sources.base import Source, SourceError
from telemetry_nerd.sources.spec import SourceSpec

Factory = Callable[[SourceSpec], Source]


class SourceRegistry(Mapping[str, Source]):
    def __init__(
        self, db: sqlite3.Connection, factory: Factory, clock: Callable[[], int] = now_ms
    ) -> None:
        self._db = db
        self._factory = factory
        self._clock = clock
        self._live: dict[str, Source] = {}
        self._specs: dict[str, SourceSpec] = {}
        self._attached: set[str] = set()  # settings-owned, never persisted
        self._broken: dict[str, str] = {}

    # Mapping: only live sources are queryable
    def __getitem__(self, name: str) -> Source:
        return self._live[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._live)

    def __len__(self) -> int:
        return len(self._live)

    def load(self) -> None:
        rows = self._db.execute("SELECT name, spec FROM sources ORDER BY created_at_ms, name")
        for name, raw in rows.fetchall():
            spec = SourceSpec.model_validate_json(raw)
            self._specs[name] = spec
            try:
                self._live[name] = self._factory(spec)
            except SourceError as e:
                # e.g. the secret's env var is not set in this daemon: keep the spec, report it
                self._broken[name] = str(e)

    def build(self, spec: SourceSpec) -> Source:
        return self._factory(spec)

    def spec(self, name: str) -> SourceSpec | None:
        return self._specs.get(name)

    def add(self, spec: SourceSpec, source: Source, *, replace: bool = False) -> Source | None:
        if spec.name in self._specs and not replace:
            raise SourceError(
                f"source {spec.name!r} already exists",
                hint="pass replace=true to reconfigure it, or choose another name",
            )
        self._db.execute(
            "INSERT INTO sources (name, spec, created_at_ms) VALUES (?, ?, ?) "
            "ON CONFLICT (name) DO UPDATE SET spec = excluded.spec",
            (spec.name, spec.model_dump_json(), self._clock()),
        )
        old = self._live.get(spec.name)
        self._specs[spec.name] = spec
        self._live[spec.name] = source
        self._broken.pop(spec.name, None)
        return old

    def attach(self, name: str, source: Source, spec: SourceSpec | None = None) -> None:
        self._live[name] = source
        self._attached.add(name)
        if spec is not None:
            self._specs[name] = spec

    def remove(self, name: str) -> Source | None:
        if name not in self._specs and name not in self._live:
            raise SourceError(f"unknown source {name!r}", hint="see source_list for names")
        self._db.execute("DELETE FROM sources WHERE name = ?", (name,))
        self._specs.pop(name, None)
        self._broken.pop(name, None)
        self._attached.discard(name)
        return self._live.pop(name, None)

    def describe(self) -> list[dict]:
        out = []
        for name in sorted(set(self._specs) | set(self._live)):
            spec = self._specs.get(name)
            entry = spec.public() if spec else {"name": name}
            entry["managed_by"] = "settings" if name in self._attached else "runtime"
            entry["live"] = name in self._live
            entry["broken"] = self._broken.get(name)
            out.append(entry)
        return out
```

- [ ] **Step 5: Run registry tests**

Run: `uv run pytest tests/unit/test_source_registry.py -q`
Expected: PASS

- [ ] **Step 6: Switch the service and fakes to the registry**

In `tests/unit/fakes.py`, replace `make_service` (keep its signature; add an optional `factory`):

```python
from telemetry_nerd.sources.registry import SourceRegistry
from telemetry_nerd.sources.spec import SourceSpec


def fake_factory(spec: SourceSpec) -> FakeSource:
    return FakeSource(name=spec.name, identity=f"fake|{spec.url}")


def make_service(tmp_path, source=None, clock=lambda: NOW, factory=fake_factory) -> TelemetryService:
    source = source or FakeSource()
    con = open_duckdb(tmp_path / "series.duckdb")
    wcon = open_workspace_db(tmp_path / "workspace.db")
    workspace = WorkspaceStore(wcon, clock=clock)
    datasets = DatasetStore(con, workspace.next_id, clock=clock)
    log = EventLog(wcon, clock=clock)
    objects = ObjectStore(wcon, workspace.next_id, clock=clock)
    sources = SourceRegistry(wcon, factory, clock=clock)
    sources.attach("default", source)
    return TelemetryService(
        sources=sources,
        cache=SeriesCache(con, clock=clock),
        datasets=datasets,
        workspace=workspace,
        log=log,
        ws=WorkspaceService(workspace, objects, datasets, log),
        clock=clock,
    )
```

In `src/telemetry_nerd/core/service.py`: change the field type to `sources: SourceRegistry` (import from `telemetry_nerd.sources.registry`), and update the unknown-source hint in `query`:

```python
        if src is None:
            raise SourceError(
                f"unknown source {source!r}",
                hint=(
                    f"available sources: {', '.join(sorted(self.sources)) or 'none'}; "
                    "connect one with source_connect"
                ),
            )
```

- [ ] **Step 7: Write failing service tests**

```python
# tests/unit/test_service_sources.py
import json

import pytest

from telemetry_nerd.sources.base import SourceError, SourceUnavailable
from telemetry_nerd.sources.spec import AuthRef, SourceSpec
from tests.unit.fakes import FakeSource, make_service

URL = "https://play.test/prom"


class DownSource(FakeSource):
    async def probe(self) -> dict:
        raise SourceUnavailable("cannot reach", hint="check the URL")


async def test_connect_probes_persists_and_logs(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.source_connect(SourceSpec(name="play", url=URL))
    assert out["source"]["name"] == "play"
    assert out["status"]["reachable"] is True
    assert "play" in svc.sources
    [event] = [e for e in svc.log.since(0, 100) if e.type == "source.connected"]
    assert event.object_id == "play" and event.actor == "claude"


async def test_query_routes_to_named_source(tmp_path):
    svc = make_service(tmp_path)
    await svc.source_connect(SourceSpec(name="play", url=URL))
    await svc.query("up", "now-2h", "now-1h", source="play")
    assert svc.sources["play"].calls == 1
    assert svc.sources["default"].calls == 0


async def test_unreachable_source_is_not_added(tmp_path):
    svc = make_service(tmp_path, factory=lambda s: DownSource(name=s.name))
    with pytest.raises(SourceUnavailable):
        await svc.source_connect(SourceSpec(name="down", url=URL))
    assert "down" not in svc.sources
    assert all(d["name"] != "down" for d in svc.source_list())


async def test_reserved_name_refused(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(SourceError, match="reserved"):
        await svc.source_connect(SourceSpec(name="default", url=URL))
    with pytest.raises(SourceError, match="reserved"):
        await svc.source_disconnect("default")


async def test_duplicate_refused_before_probing(tmp_path):
    probes = []

    class Counting(FakeSource):
        async def probe(self) -> dict:
            probes.append(self.name)
            return {"reachable": True}

    svc = make_service(tmp_path, factory=lambda s: Counting(name=s.name))
    await svc.source_connect(SourceSpec(name="play", url=URL))
    with pytest.raises(SourceError, match="already exists"):
        await svc.source_connect(SourceSpec(name="play", url=URL))
    assert probes == ["play"]
    await svc.source_connect(SourceSpec(name="play", url=URL + "/v2"), replace=True)
    assert probes == ["play", "play"]


async def test_disconnect_removes_and_logs(tmp_path):
    svc = make_service(tmp_path)
    await svc.source_connect(SourceSpec(name="play", url=URL))
    await svc.source_disconnect("play")
    assert "play" not in svc.sources
    assert any(e.type == "source.disconnected" for e in svc.log.since(0, 100))


async def test_status_reports_unreachable_without_raising(tmp_path):
    svc = make_service(tmp_path)
    svc.sources.attach("down", DownSource(name="down"))
    out = await svc.source_status("down")
    assert out["reachable"] is False
    assert out["hint"] == "check the URL"
    with pytest.raises(SourceError, match="unknown source"):
        await svc.source_status("ghost")


async def test_secret_never_in_list_or_events(tmp_path, monkeypatch):
    monkeypatch.setenv("TN_TEST_TOKEN", "s3cr3t-value")
    svc = make_service(tmp_path)
    await svc.source_connect(SourceSpec(name="auth", url=URL, auth=AuthRef(env="TN_TEST_TOKEN")))
    blob = json.dumps(svc.source_list()) + json.dumps(
        [e.to_dict() for e in svc.log.since(0, 100)]
    )
    assert "s3cr3t-value" not in blob
    assert "TN_TEST_TOKEN" in blob
```

- [ ] **Step 8: Run to verify failure**

Run: `uv run pytest tests/unit/test_service_sources.py -q`
Expected: FAIL — `AttributeError: 'TelemetryService' object has no attribute 'source_connect'`

- [ ] **Step 9: Implement service methods**

Add to `TelemetryService` in `src/telemetry_nerd/core/service.py` (imports: `from telemetry_nerd.sources.spec import RESERVED_NAMES, SourceSpec`):

```python
    @staticmethod
    def _refuse_reserved(name: str) -> None:
        if name in RESERVED_NAMES:
            raise SourceError(
                f"source name {name!r} is reserved for the daemon's configured source",
                hint="choose another name, e.g. the host or Grafana datasource name",
            )

    @staticmethod
    async def _close(source: object) -> None:
        aclose = getattr(source, "aclose", None)
        if aclose is not None:
            await aclose()

    async def source_connect(
        self, spec: SourceSpec, *, replace: bool = False, actor: Actor = "claude"
    ) -> dict:
        self._refuse_reserved(spec.name)
        if self.sources.spec(spec.name) is not None and not replace:
            raise SourceError(
                f"source {spec.name!r} already exists",
                hint="pass replace=true to reconfigure it, or choose another name",
            )
        source = self.sources.build(spec)  # raises MissingSecret before any network call
        try:
            status = await source.probe()
        except SourceError:
            await self._close(source)
            raise
        old = self.sources.add(spec, source, replace=replace)
        if old is not None:
            await self._close(old)
        public = spec.public()
        self.log.append(actor, "source.connected", spec.name, {"source": public})
        return {"source": public, "status": status}

    def source_list(self) -> list[dict]:
        return self.sources.describe()

    async def source_status(self, name: str) -> dict:
        source = self.sources.get(name)
        if source is None:
            raise SourceError(f"unknown source {name!r}", hint="see source_list for names")
        try:
            return await source.probe()
        except SourceError as e:
            return {"reachable": False, "error": str(e), "hint": e.hint}

    async def source_disconnect(self, name: str, actor: Actor = "claude") -> None:
        self._refuse_reserved(name)
        old = self.sources.remove(name)
        if old is not None:
            await self._close(old)
        self.log.append(actor, "source.disconnected", name, {})
```

- [ ] **Step 10: Bootstrap — load persisted sources, attach the settings default**

Replace the source construction in `src/telemetry_nerd/core/bootstrap.py`:

```python
from telemetry_nerd.sources.registry import SourceRegistry
from telemetry_nerd.sources.spec import SourceSpec

    # ...inside build_service, after wcon/workspace are created:
    sources = SourceRegistry(wcon, PromQLSource.from_spec)
    sources.load()
    # `default` is owned by daemon settings: rebuilt from them on every start, never persisted.
    default = SourceSpec(
        name="default",
        url=settings.source_url,
        flavor=settings.source_flavor,  # type: ignore[arg-type]
        resolution_ms=settings.resolution_ms,
    )
    sources.attach("default", PromQLSource.from_spec(default), default)
    return TelemetryService(
        sources=sources,
        # ...rest unchanged
    )
```

Remove the now-unused direct `PromQLSource("default", ...)` construction.

Append to `tests/unit/test_bootstrap.py`:

```python
async def test_runtime_sources_survive_rebuild(tmp_path, monkeypatch):
    from telemetry_nerd.sources.spec import SourceSpec

    settings = Settings(data_dir=tmp_path / "data")
    svc = build_service(settings)

    async def ok(self) -> dict:
        return {"reachable": True}

    monkeypatch.setattr("telemetry_nerd.sources.promql.PromQLSource.probe", ok)
    await svc.source_connect(SourceSpec(name="play", url="https://play.test/prom"))
    rebuilt = build_service(settings)
    assert set(rebuilt.sources) == {"default", "play"}
    assert rebuilt.sources["play"].base_url == "https://play.test/prom"
```

- [ ] **Step 11: Run everything**

Run: `just test && just lint`
Expected: PASS (existing `test_bootstrap.py::test_build_service_creates_data_dir` still passes: `set(svc.sources) == {"default"}` via the Mapping).

- [ ] **Step 12: Commit**

```bash
git add src/telemetry_nerd/sources/registry.py src/telemetry_nerd/workspace/db.py src/telemetry_nerd/core/service.py src/telemetry_nerd/core/bootstrap.py tests/unit/fakes.py tests/unit/test_source_registry.py tests/unit/test_service_sources.py tests/unit/test_bootstrap.py
git commit -m "feat(sources): persisted runtime source registry and service ops (2as.1)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: MCP tools and HTTP listing

**Files:**
- Modify: `src/telemetry_nerd/mcp/server.py` (4 tools, `INSTRUCTIONS`, `query` docstring)
- Modify: `src/telemetry_nerd/api/app.py` (`GET /api/sources`)
- Test: `tests/unit/test_mcp_sources.py`, `tests/unit/test_api.py` (append)

**Interfaces:**
- Consumes: `TelemetryService.source_connect/list/status/disconnect`, `SourceSpec` (Tasks 1, 3); `parse_duration` from `telemetry_nerd.model.time`
- Produces MCP tools (all return compact JSON strings):
  - `source_connect(name, url, flavor="prometheus", resolution="15s", auth_env=None, auth_file=None, auth_scheme="bearer", max_concurrency=4, min_interval="0s", timeout="30s", replace=False)` → `{"source": {...public}, "status": {...}}`
  - `source_list()` → `{"sources": [...]}`
  - `source_status(name)` → probe dict
  - `source_disconnect(name)` → `{"disconnected": name}`
  - HTTP `GET /api/sources` → `{"sources": [...]}`

- [ ] **Step 1: Write failing MCP tests**

```python
# tests/unit/test_mcp_sources.py
import json

from mcp import Client
from mcp.types import TextContent

from telemetry_nerd.mcp.server import build_mcp
from tests.unit.fakes import make_service

URL = "https://play.grafana.org/api/datasources/proxy/uid/grafanacloud-prom"


async def call(mcp, name, args):
    async with Client(mcp) as client:
        return await client.call_tool(name, args)


def text(result) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


async def test_connect_list_query_disconnect(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    r = await call(
        mcp, "source_connect",
        {"name": "play", "url": URL, "resolution": "20s", "min_interval": "500ms", "timeout": "60s"},
    )
    assert not r.is_error, text(r)
    out = json.loads(text(r))
    assert out["source"]["resolution_ms"] == 20_000
    assert out["source"]["politeness"] == {
        "max_concurrency": 4, "min_interval_ms": 500, "timeout_s": 60.0,
    }
    assert out["status"]["reachable"] is True

    names = [s["name"] for s in json.loads(text(await call(mcp, "source_list", {})))["sources"]]
    assert names == ["default", "play"]

    q = await call(mcp, "query", {"expr": "up", "start": "now-2h", "end": "now-1h", "source": "play"})
    assert not q.is_error, text(q)

    d = await call(mcp, "source_disconnect", {"name": "play"})
    assert json.loads(text(d)) == {"disconnected": "play"}


async def test_token_like_auth_env_is_rejected_with_guidance(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    r = await call(mcp, "source_connect", {"name": "g", "url": URL, "auth_env": "glsa_abcdef0123"})
    assert r.is_error
    assert "NAME of an environment variable" in text(r)


async def test_url_with_credentials_rejected(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    r = await call(mcp, "source_connect", {"name": "g", "url": "https://u:p@grafana.test"})
    assert r.is_error
    assert "credentials in the url" in text(r)


async def test_missing_secret_is_a_tool_error_with_hint(tmp_path):
    from telemetry_nerd.sources.promql import PromQLSource

    mcp = build_mcp(make_service(tmp_path, factory=PromQLSource.from_spec), "http://x")
    r = await call(mcp, "source_connect", {"name": "g", "url": URL, "auth_env": "TN_SURELY_UNSET_VAR"})
    assert r.is_error
    assert "TN_SURELY_UNSET_VAR" in text(r) and "hint:" in text(r)


async def test_status_unknown_source_is_tool_error(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    r = await call(mcp, "source_status", {"name": "ghost"})
    assert r.is_error and "source_list" in text(r)
```

Append to `tests/unit/test_api.py` (uses the file's existing `client` fixture, a `TestClient` over `make_service`):

```python
def test_list_sources(client):
    r = client.get("/api/sources")
    assert r.status_code == 200
    assert [s["name"] for s in r.json()["sources"]] == ["default"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_mcp_sources.py tests/unit/test_api.py -q`
Expected: FAIL — unknown tool `source_connect`; 404 on `/api/sources`.

- [ ] **Step 3: Implement the tools**

In `src/telemetry_nerd/mcp/server.py` add imports `from telemetry_nerd.model.time import parse_duration, parse_time` and `from telemetry_nerd.sources.spec import SourceSpec`, then inside `build_mcp` (after `query`):

```python
    def _source_error(e: SourceError) -> ToolError:
        return ToolError(f"{e} (hint: {e.hint})" if e.hint else str(e))

    @mcp.tool()
    async def source_connect(
        name: str,
        url: str,
        flavor: str = "prometheus",
        resolution: str = "15s",
        auth_env: str | None = None,
        auth_file: str | None = None,
        auth_scheme: str = "bearer",
        max_concurrency: int = 4,
        min_interval: str = "0s",
        timeout: str = "30s",
        replace: bool = False,
    ) -> str:
        """Connect a Prometheus-compatible source at runtime (no daemon restart).

        url: API base without /api/v1, e.g. http://prometheus:9090, a VictoriaMetrics
        /select/0/prometheus path, or a Grafana datasource proxy
        https://<grafana>/api/datasources/proxy/uid/<uid>.
        flavor: "victoriametrics" (MetricsQL rollup) or "prometheus" (also works on VM, Thanos, Mimir).
        resolution: the source's scrape interval, e.g. 15s, 30s, 60s.
        Secrets: NEVER pass a token. Ask the user to put it in a file (auth_file, absolute
        path; picked up immediately) or an env var of the daemon (auth_env, the variable
        NAME; needs a daemon restart if set later). auth_scheme: bearer | basic ("user:pass").
        Politeness for shared/public servers: lower max_concurrency, set min_interval (e.g. 500ms),
        raise timeout (e.g. 60s).
        The source is probed before it is saved; it persists across daemon restarts.
        Returns {source, status}.
        """
        try:
            auth = None
            if auth_env is not None or auth_file is not None:
                auth = {"env": auth_env, "file": auth_file, "scheme": auth_scheme}
            spec = SourceSpec.model_validate(
                {
                    "name": name,
                    "url": url,
                    "flavor": flavor,
                    "resolution_ms": parse_duration(resolution),
                    "auth": auth,
                    "politeness": {
                        "max_concurrency": max_concurrency,
                        "min_interval_ms": parse_duration(min_interval),
                        "timeout_s": parse_duration(timeout) / 1000,
                    },
                }
            )
            return _dump(await service.source_connect(spec, replace=replace))
        except ValidationError as e:
            raise _fail(e) from e
        except SourceError as e:
            raise _source_error(e) from e
        except ValueError as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    def source_list() -> str:
        """Connected sources: name, url, flavor, resolution, auth reference (never the secret),
        politeness, whether it is live or broken (e.g. its secret is missing)."""
        return _dump({"sources": service.source_list()})

    @mcp.tool()
    async def source_status(name: str) -> str:
        """Probe a source now: {reachable, latency_ms, application?, version?} or
        {reachable: false, error, hint}."""
        try:
            return _dump(await service.source_status(name))
        except SourceError as e:
            raise _source_error(e) from e

    @mcp.tool()
    async def source_disconnect(name: str) -> str:
        """Remove a runtime source. Existing datasets and panels keep working."""
        try:
            await service.source_disconnect(name)
        except SourceError as e:
            raise _source_error(e) from e
        return _dump({"disconnected": name})
```

`parse_duration` already accepts `ms` (`"500ms"` → 500).

Update `INSTRUCTIONS` — add after the `query` bullet:

```
- Sources: `source_list` shows what is connected. To look at other data, `source_connect`
  it (Prometheus, VictoriaMetrics, Thanos, Mimir, or a Grafana datasource proxy URL);
  no restart is needed. Never ask for or pass a raw token: use auth_file/auth_env.
  `query(source=...)` picks the source.
```

and change the `query` tool docstring's first line to mention `source: a name from source_list (default "default")`.

- [ ] **Step 4: HTTP listing**

In `src/telemetry_nerd/api/app.py`, next to `workspace`:

```python
    @_api
    async def list_sources(request: Request) -> object:
        return {"sources": service.source_list()}
```

and register `Route("/api/sources", list_sources),` in the routes list (after `/api/workspace`).

- [ ] **Step 5: Run everything**

Run: `just test && just lint`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/telemetry_nerd/mcp/server.py src/telemetry_nerd/api/app.py tests/unit/test_mcp_sources.py tests/unit/test_api.py
git commit -m "feat(mcp): source_connect/list/status/disconnect tools (2as.1)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Integration against VictoriaMetrics + live smoke test

**Files:**
- Create: `tests/integration/test_sources_vm.py`

**Interfaces:**
- Consumes: `build_service`, `Settings`, `SourceSpec`, `vm_url` fixture (`tests/integration/conftest.py`), `push`/`exposition` (`telemetry_nerd.devtools.synthetic`)
- Produces: nothing new.

- [ ] **Step 1: Write the integration test**

```python
# tests/integration/test_sources_vm.py
import pytest

from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.devtools.synthetic import exposition, push
from telemetry_nerd.model.time import now_ms
from telemetry_nerd.sources.spec import SourceSpec

pytestmark = pytest.mark.integration


async def test_connect_second_source_at_runtime_and_survive_restart(vm_url, tmp_path):
    t0 = (now_ms() - 2 * 3_600_000) // 60_000 * 60_000
    push(vm_url, exposition("tn_it_rt", {"instance": "a"}, [(t0 + i * 15_000, 1.0) for i in range(240)]))
    settings = Settings(data_dir=tmp_path / "d", source_url="http://127.0.0.1:9")  # default unreachable
    svc = build_service(settings)

    out = await svc.source_connect(
        SourceSpec(name="vm", url=vm_url, flavor="victoriametrics")
    )
    assert out["status"]["reachable"] is True

    res = await svc.query('tn_it_rt{instance="a"}', "now-90m", "now-30m", step="1m", source="vm")
    assert res["summary"]["series_count"] == 1

    restarted = build_service(settings)
    assert "vm" in restarted.sources
    assert (await restarted.source_status("vm"))["reachable"] is True
    assert (await restarted.source_status("default"))["reachable"] is False
```

- [ ] **Step 2: Run it**

Run: `uv run pytest -m integration tests/integration/test_sources_vm.py -q`
Expected: PASS (needs podman/Docker for testcontainers; if the container runtime is unavailable, report that instead of skipping silently).

- [ ] **Step 3: Live smoke test against Grafana Play (manual, polite)**

Run the daemon from this checkout (`just serve`, or reuse the running one after restarting it once to load the new code), then from a Claude session or with `scripts/mcp_call.py` (see its `--help`):

1. `source_connect(name="play", url="https://play.grafana.org/api/datasources/proxy/uid/grafanacloud-prom", resolution="20s", max_concurrency=2, min_interval="500ms", timeout="60s")` → `status.reachable == true`, `application` reported.
2. `query(expr='sum by (cloud_region) (rate(traces_spanmetrics_calls_total{service="checkoutservice", span_kind="SPAN_KIND_SERVER"}[5m]))', start="now-3h", source="play")` → a dataset with ≤6 series.
3. `show(dataset=..., question="Is checkout call rate different across regions?")` → panel renders in the UI.
4. Restart the daemon; `source_list()` still shows `play`.

Expected: all four succeed with no restart between steps 1–3. Note any rough edges as new beads (`bd create ... --parent telemetry-nerd-2as`).

- [ ] **Step 4: Commit and close**

```bash
git add tests/integration/test_sources_vm.py
git commit -m "test(sources): runtime source connect against VictoriaMetrics (2as.1)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
bd close telemetry-nerd-2as.1 --reason "Runtime sources via MCP: connect/list/status/disconnect, persisted, secret refs; integration + Grafana Play smoke passed"
```

---

## Milestone acceptance (2as.1)

- From a running daemon, Claude connects Grafana Play and Wikimedia via `source_connect` and queries both without a restart.
- Sources persist across a daemon restart; `default` is rebuilt from settings each start.
- No tool, API response, event, log line, or DB row contains a secret value (unit-tested for list + events; spec rejects token-shaped `auth_env`, URL credentials, query strings, unknown fields).
- Shared/public sources can be throttled (`max_concurrency`, `min_interval`, `timeout`).
