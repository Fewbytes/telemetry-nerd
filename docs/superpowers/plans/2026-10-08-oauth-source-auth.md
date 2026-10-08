# OAuth Source Auth (Authorization Code + PKCE) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a source (PromQL or Elasticsearch/OpenSearch, including through the Grafana front door) authenticate via OAuth2 Authorization Code + PKCE, with a shared, refresh-capable token provider replacing the current one-shot `AuthRef.headers()` bake-in.

**Architecture:** A new `sources/oauth.py` module owns the PKCE login dance, token persistence, and refresh/retry logic behind a `TokenProvider` class and a `CallbackListener` that briefly runs a local HTTP server to catch the IdP redirect. `PromQLSource`, `ElasticsearchSource`, and `grafana.py`'s discovery functions each call `TokenProvider.headers()` per request instead of baking a static header dict at construction, with a refresh-and-retry-once path on a 401. `source_connect` gains an OAuth login step that runs before the adapter is built.

**Tech Stack:** Python 3.12+, httpx (token exchange + adapter requests), `asyncio.start_server` (callback listener — no new HTTP framework dependency), pydantic (config models), pytest + pytest-asyncio (tests), `httpx.MockTransport` (adapter test fixtures, matching existing `tests/unit/test_es_source.py` pattern).

**Spec:** `docs/superpowers/specs/2026-10-08-oauth-source-auth-design.md`

## Global Constraints

- Authorization Code + PKCE only (no Client Credentials flow — out of scope per the spec).
- No secret value is ever a direct config field. `OAuthRef.client_secret` is **not** a field;
  instead `OAuthRef.client_secret_env` names an environment variable, exactly like
  `AuthRef.env` — this follows the invariant stated at the top of `sources/spec.py`
  ("a secret value is never an argument, stored, logged or returned") and is a correction of
  the spec's looser wording, not a scope change.
- Token file: `<data_dir>/oauth-tokens/<source_name>.json`, permissions `0600`, plain JSON
  (no new crypto layer — same trust model as today's `file`-based `AuthRef`).
- Statically-configured sources (`AuthRef`) must see zero behavior change: no added
  per-request cost, no new code path for them.
- `MissingSecret` (from `sources/spec.py`) is reused for every OAuth failure that means "no
  usable credential right now" (no token yet, refresh failed) — same error family the rest
  of auth already raises, same hint shape (`hint=...`).
- Every new async entry point gets at least one test exercising it; no bare `except Exception`.

---

## File Map

- Create: `src/telemetry_nerd/sources/oauth.py` — `OAuthLoginTimeout`, `OAuthLoginFailed`,
  PKCE helpers, `TokenState`, `TokenProvider`, `CallbackListener`.
- Create: `tests/unit/test_oauth.py` — PKCE, token persistence, `TokenProvider` unit tests.
- Create: `tests/unit/test_oauth_listener.py` — `CallbackListener` unit tests.
- Modify: `src/telemetry_nerd/sources/spec.py` — `OAuthRef` model, `SourceSpec.auth` union,
  `SourceSpec.public()`.
- Modify: `src/telemetry_nerd/sources/promql.py` — `PromQLSource` OAuth integration.
- Modify: `src/telemetry_nerd/sources/elasticsearch.py` — `ElasticsearchSource` OAuth
  integration.
- Modify: `src/telemetry_nerd/sources/grafana.py` — `discover_datasources`/`probe_backend`
  accept precomputed OAuth headers.
- Modify: `src/telemetry_nerd/core/bootstrap.py` — thread `data_dir` into `source_factory`.
- Modify: `src/telemetry_nerd/core/service.py` — `source_connect` OAuth login step.
- Modify: `src/telemetry_nerd/mcp/server.py` — `source_connect` tool: OAuth config params.
- Modify: `tests/unit/test_sources.py`, `tests/unit/test_es_source.py`,
  `tests/unit/test_grafana.py`, `tests/unit/test_service_sources.py` — new OAuth-path tests
  alongside the existing ones.

---

### Task 1: `OAuthRef` config model

**Files:**
- Modify: `src/telemetry_nerd/sources/spec.py`
- Test: `tests/unit/test_source_spec_oauth.py` (new)

**Interfaces:**
- Produces: `OAuthRef` (pydantic `BaseModel`, `frozen=True`) with fields
  `authorize_url: str`, `token_url: str`, `client_id: str`,
  `client_secret_env: str | None = None`, `scopes: list[str] = []`; methods
  `client_secret(self, environ: Mapping[str, str] = os.environ) -> str | None` and
  `public(self) -> dict`. `SourceSpec.auth: AuthRef | OAuthRef | None = None`.
  `SourceSpec.public()` dispatches on the auth type.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_source_spec_oauth.py
import pytest
from pydantic import ValidationError

from telemetry_nerd.sources.spec import AuthRef, OAuthRef, SourceSpec


def _oauth(**kw) -> dict:
    return {
        "authorize_url": "https://idp.example.com/authorize",
        "token_url": "https://idp.example.com/token",
        "client_id": "tn-client",
        **kw,
    }


def test_oauth_ref_round_trips_through_source_spec():
    spec = SourceSpec.model_validate(
        {"name": "sso", "url": "https://prom.example.com", "auth": _oauth()}
    )
    assert isinstance(spec.auth, OAuthRef)
    assert spec.auth.client_id == "tn-client"
    assert spec.auth.scopes == []


def test_oauth_authorize_and_token_url_must_be_https():
    with pytest.raises(ValidationError, match="https"):
        OAuthRef.model_validate(_oauth(authorize_url="http://idp.example.com/authorize"))


def test_oauth_client_secret_env_must_be_a_name():
    with pytest.raises(ValidationError, match="NAME of an environment variable"):
        OAuthRef.model_validate(_oauth(client_secret_env="not a name"))


def test_oauth_client_secret_is_never_a_direct_field():
    with pytest.raises(ValidationError):
        OAuthRef.model_validate(_oauth(client_secret="shh"))  # type: ignore[arg-type]


def test_oauth_client_secret_reads_the_env_var():
    ref = OAuthRef.model_validate(_oauth(client_secret_env="IDP_SECRET"))
    assert ref.client_secret({"IDP_SECRET": "s3cr3t"}) == "s3cr3t"
    assert ref.client_secret({}) is None


def test_source_spec_public_redacts_oauth_client_secret_env_presence_only():
    spec = SourceSpec.model_validate(
        {"name": "sso", "url": "https://prom.example.com",
         "auth": _oauth(client_secret_env="IDP_SECRET")}
    )  # fmt: skip
    out = spec.public()
    assert out["auth"] == {
        "authorize_url": "https://idp.example.com/authorize",
        "token_url": "https://idp.example.com/token",
        "client_id": "tn-client",
        "scopes": [],
        "client_secret_env": "IDP_SECRET",
    }


def test_auth_and_oauth_both_set_is_rejected():
    with pytest.raises(ValidationError):
        SourceSpec.model_validate(
            {
                "name": "sso",
                "url": "https://prom.example.com",
                "auth": {"env": "TOKEN"},
            }
        )
        # control case: a plain AuthRef still works unchanged
    spec = SourceSpec.model_validate(
        {"name": "sso", "url": "https://prom.example.com", "auth": {"env": "TOKEN"}}
    )
    assert isinstance(spec.auth, AuthRef)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_source_spec_oauth.py -v`
Expected: FAIL — `ImportError: cannot import name 'OAuthRef'`

- [ ] **Step 3: Implement `OAuthRef` and the `SourceSpec.auth` union**

In `src/telemetry_nerd/sources/spec.py`, add after the existing `AuthRef` class (keep
`AuthRef` exactly as-is):

```python
class OAuthRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    authorize_url: str
    token_url: str
    client_id: str
    #: NAME of an environment variable holding the client secret, never the secret itself
    #: (same convention as AuthRef.env); None for a public PKCE client with no secret
    client_secret_env: str | None = None
    scopes: list[str] = Field(default_factory=list)

    @field_validator("authorize_url", "token_url")
    @classmethod
    def _https_endpoint(cls, v: str) -> str:
        parts = urlsplit(v)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("OAuth endpoint urls must look like https://host[:port]/path")
        return v

    @field_validator("client_secret_env")
    @classmethod
    def _env_is_a_name(cls, v: str | None) -> str | None:
        if v is not None and not _ENV_VAR.match(v):
            raise ValueError(
                "client_secret_env must be the NAME of an environment variable "
                "(e.g. OKTA_CLIENT_SECRET) in the daemon's environment, never the secret itself"
            )
        return v

    def client_secret(self, environ: Mapping[str, str] = os.environ) -> str | None:
        if self.client_secret_env is None:
            return None
        return environ.get(self.client_secret_env) or None

    def public(self) -> dict:
        return {
            "authorize_url": self.authorize_url,
            "token_url": self.token_url,
            "client_id": self.client_id,
            "scopes": self.scopes,
            "client_secret_env": self.client_secret_env,
        }
```

Add the import this needs at the top of the file (it already imports `urlsplit` from
`urllib.parse` for `SourceSpec._url`, so only `Field` needs checking — it is already
imported). Now change `SourceSpec`:

```python
    auth: AuthRef | OAuthRef | None = None
```

(replacing the current `auth: AuthRef | None = None`), and change `SourceSpec.public`:

```python
    def public(self, environ: Mapping[str, str] = os.environ) -> dict:
        out = self.model_dump()
        if isinstance(self.auth, OAuthRef):
            out["auth"] = self.auth.public()
        elif isinstance(self.auth, AuthRef):
            out["auth"] = self.auth.public(environ)
        else:
            out["auth"] = None
        return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_source_spec_oauth.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Run the full spec.py test suite to check for regressions**

Run: `uv run pytest tests/unit/test_source_resolution.py tests/unit/test_sources.py -k spec -v`
Expected: PASS, no regressions

- [ ] **Step 6: Commit**

```bash
git add src/telemetry_nerd/sources/spec.py tests/unit/test_source_spec_oauth.py
git commit -m "feat(oauth): add OAuthRef config model to SourceSpec"
```

---

### Task 2: PKCE helpers and token file persistence

**Files:**
- Create: `src/telemetry_nerd/sources/oauth.py`
- Test: `tests/unit/test_oauth.py` (new)

**Interfaces:**
- Consumes: nothing from earlier tasks (pure + filesystem only).
- Produces: `generate_pkce() -> tuple[str, str]` (verifier, challenge),
  `generate_state() -> str`, `TokenState` (`dataclass(frozen=True)`: `access_token: str`,
  `refresh_token: str`, `expires_at: float`), `token_path(data_dir: Path, name: str) -> Path`,
  `load_token(path: Path) -> TokenState | None`, `save_token(path: Path, state: TokenState) ->
  None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_oauth.py
import base64
import hashlib
import json

from telemetry_nerd.sources.oauth import (
    TokenState,
    generate_pkce,
    generate_state,
    load_token,
    save_token,
    token_path,
)


def test_generate_pkce_challenge_is_sha256_of_verifier():
    verifier, challenge = generate_pkce()
    assert len(verifier) >= 43  # RFC 7636 minimum length
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert challenge == expected.rstrip(b"=").decode()


def test_generate_pkce_is_random_each_call():
    a, _ = generate_pkce()
    b, _ = generate_pkce()
    assert a != b


def test_generate_state_is_random_each_call():
    assert generate_state() != generate_state()


def test_token_path_is_under_oauth_tokens_subdir(tmp_path):
    path = token_path(tmp_path, "sso")
    assert path == tmp_path / "oauth-tokens" / "sso.json"
    assert path.parent.is_dir()


def test_save_then_load_token_round_trips(tmp_path):
    path = token_path(tmp_path, "sso")
    state = TokenState(access_token="a", refresh_token="r", expires_at=123.0)
    save_token(path, state)
    assert load_token(path) == state


def test_save_token_sets_restrictive_permissions(tmp_path):
    path = token_path(tmp_path, "sso")
    save_token(path, TokenState("a", "r", 123.0))
    assert (path.stat().st_mode & 0o777) == 0o600


def test_load_token_missing_file_returns_none(tmp_path):
    assert load_token(token_path(tmp_path, "sso")) is None


def test_load_token_malformed_json_returns_none(tmp_path):
    path = token_path(tmp_path, "sso")
    path.write_text("not json")
    assert load_token(path) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_oauth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telemetry_nerd.sources.oauth'`

- [ ] **Step 3: Implement the module**

```python
# src/telemetry_nerd/sources/oauth.py
"""OAuth2 Authorization Code + PKCE support, shared by PromQLSource, ElasticsearchSource,
and grafana.py in place of AuthRef's static secret for sources behind interactive SSO.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from telemetry_nerd.sources.base import SourceError, SourceUnavailable
from telemetry_nerd.sources.spec import MissingSecret, OAuthRef

_EXPIRY_MARGIN_S = 60


class OAuthLoginTimeout(SourceError):
    pass


class OAuthLoginFailed(SourceError):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate_pkce() -> tuple[str, str]:
    """(code_verifier, code_challenge) per RFC 7636, S256 method."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def generate_state() -> str:
    return _b64url(secrets.token_bytes(16))


@dataclass(frozen=True)
class TokenState:
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds


def token_path(data_dir: Path, name: str) -> Path:
    d = Path(data_dir) / "oauth-tokens"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.json"


def load_token(path: Path) -> TokenState | None:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    try:
        return TokenState(**raw)
    except TypeError:
        return None


def save_token(path: Path, state: TokenState) -> None:
    path.write_text(json.dumps(asdict(state)))
    path.chmod(0o600)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_oauth.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/sources/oauth.py tests/unit/test_oauth.py
git commit -m "feat(oauth): PKCE helpers and token file persistence"
```

---

### Task 3: `TokenProvider` — login URL and code exchange

**Files:**
- Modify: `src/telemetry_nerd/sources/oauth.py`
- Modify: `tests/unit/test_oauth.py`

**Interfaces:**
- Consumes: `generate_pkce`, `generate_state`, `TokenState`, `save_token`, `load_token`,
  `token_path` (Task 2); `OAuthRef` (Task 1).
- Produces: `class TokenProvider`: `__init__(self, oauth: OAuthRef, name: str, data_dir:
  Path, client: httpx.AsyncClient | None = None) -> None`; `async def aclose(self) ->
  None`; `def login_url(self, redirect_uri: str) -> tuple[str, str, str]` (url, state,
  verifier); `async def complete_login(self, code: str, verifier: str, redirect_uri: str)
  -> None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_oauth.py`:

```python
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from telemetry_nerd.sources.oauth import TokenProvider
from telemetry_nerd.sources.spec import MissingSecret, OAuthRef


def _oauth_ref(**kw) -> OAuthRef:
    return OAuthRef.model_validate(
        {
            "authorize_url": "https://idp.example.com/authorize",
            "token_url": "https://idp.example.com/token",
            "client_id": "tn-client",
            "scopes": ["offline_access", "metrics.read"],
            **kw,
        }
    )


def test_login_url_includes_pkce_challenge_and_state(tmp_path):
    provider = TokenProvider(_oauth_ref(), "sso", tmp_path)
    url, state, verifier = provider.login_url("http://localhost:1234/callback")
    parts = urlsplit(url)
    params = parse_qs(parts.query)
    assert parts.scheme == "https" and parts.hostname == "idp.example.com"
    assert params["response_type"] == ["code"]
    assert params["client_id"] == ["tn-client"]
    assert params["redirect_uri"] == ["http://localhost:1234/callback"]
    assert params["scope"] == ["offline_access metrics.read"]
    assert params["state"] == [state]
    assert params["code_challenge_method"] == ["S256"]
    assert len(params["code_challenge"][0]) >= 43
    assert len(verifier) >= 43


async def test_complete_login_exchanges_code_and_persists_token(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["form"] = dict(httpx.QueryParams(request.content.decode()))
        return httpx.Response(200, json={
            "access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600,
        })  # fmt: skip

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = TokenProvider(_oauth_ref(), "sso", tmp_path, client=client)
    await provider.complete_login("code123", "verifier123", "http://localhost:1234/callback")

    assert seen["form"]["grant_type"] == "authorization_code"
    assert seen["form"]["code"] == "code123"
    assert seen["form"]["code_verifier"] == "verifier123"
    assert seen["form"]["redirect_uri"] == "http://localhost:1234/callback"
    assert seen["form"]["client_id"] == "tn-client"
    assert "client_secret" not in seen["form"]

    headers = await provider.headers()
    assert headers == {"Authorization": "Bearer AT1"}


async def test_complete_login_sends_client_secret_when_configured(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["form"] = dict(httpx.QueryParams(request.content.decode()))
        return httpx.Response(200, json={"access_token": "AT1", "refresh_token": "RT1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = TokenProvider(
        _oauth_ref(client_secret_env="IDP_SECRET"), "sso", tmp_path, client=client
    )
    provider._environ = {"IDP_SECRET": "s3cr3t"}  # set after construction, see Step 3 note
    await provider.complete_login("code123", "verifier123", "http://localhost:1234/callback")
    assert seen["form"]["client_secret"] == "s3cr3t"


async def test_complete_login_rejects_non_200(tmp_path):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    )
    provider = TokenProvider(_oauth_ref(), "sso", tmp_path, client=client)
    with pytest.raises(MissingSecret, match="token exchange failed"):
        await provider.complete_login("bad", "verifier", "http://localhost:1234/callback")


async def test_complete_login_requires_a_refresh_token(tmp_path):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"access_token": "AT1"}))
    )
    provider = TokenProvider(_oauth_ref(), "sso", tmp_path, client=client)
    with pytest.raises(MissingSecret, match="refresh_token"):
        await provider.complete_login("code", "verifier", "http://localhost:1234/callback")
```

Note on the client-secret test: `TokenProvider` reads the secret through `OAuthRef.client_secret(environ)`, and the plan threads `environ` into the constructor rather than mutating a private attribute — fix the test above to match Step 3's real constructor signature:

```python
    provider = TokenProvider(
        _oauth_ref(client_secret_env="IDP_SECRET"), "sso", tmp_path,
        client=client, environ={"IDP_SECRET": "s3cr3t"},
    )  # fmt: skip
```

(replacing the `provider._environ = ...` line above — use this version in the actual test file).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_oauth.py -k "login_url or complete_login" -v`
Expected: FAIL — `ImportError: cannot import name 'TokenProvider'`

- [ ] **Step 3: Implement `TokenProvider` (login + exchange)**

Append to `src/telemetry_nerd/sources/oauth.py`:

```python
class TokenProvider:
    """Owns one source's OAuth token: login, refresh, and the on-disk token file.

    One instance per OAuth-configured source for the adapter's lifetime; cheap to
    construct for a one-shot login (the token file is the source of truth, not instance
    state, so a throwaway instance used only for login is read correctly by a later one).
    """

    def __init__(
        self,
        oauth: OAuthRef,
        name: str,
        data_dir: Path,
        *,
        client: httpx.AsyncClient | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        self._oauth = oauth
        self._path = token_path(data_dir, name)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient()
        self._environ = environ if environ is not None else dict(__import__("os").environ)
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def login_url(self, redirect_uri: str) -> tuple[str, str, str]:
        verifier, challenge = generate_pkce()
        state = generate_state()
        params = {
            "response_type": "code",
            "client_id": self._oauth.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self._oauth.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{self._oauth.authorize_url}?{urlencode(params)}", state, verifier

    async def complete_login(self, code: str, verifier: str, redirect_uri: str) -> None:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self._oauth.client_id,
            "code_verifier": verifier,
        }
        secret = self._oauth.client_secret(self._environ)
        if secret is not None:
            data["client_secret"] = secret
        state = await self._exchange(data, prior_refresh_token=None)
        save_token(self._path, state)

    async def _exchange(self, data: dict, *, prior_refresh_token: str | None) -> TokenState:
        try:
            resp = await self._client.post(self._oauth.token_url, data=data, timeout=30.0)
        except httpx.HTTPError as e:
            raise SourceUnavailable(
                f"cannot reach the OAuth token endpoint: {e}",
                hint="check token_url and that the IdP is reachable",
            ) from e
        if resp.status_code != 200:
            raise MissingSecret(
                f"OAuth token exchange failed: HTTP {resp.status_code}",
                hint="re-run source_connect to log in again",
            )
        try:
            body = resp.json()
            access = body["access_token"]
            refresh = body.get("refresh_token", prior_refresh_token)
            expires_in = float(body.get("expires_in", 3600))
        except (KeyError, TypeError, ValueError) as e:
            raise MissingSecret(
                "OAuth token endpoint returned a malformed response",
                hint="check token_url points at the IdP's token endpoint",
            ) from e
        if refresh is None:
            raise MissingSecret(
                "OAuth token endpoint did not return a refresh_token",
                hint="the IdP app must be configured for offline access / refresh tokens",
            )
        return TokenState(access, refresh, time.time() + expires_in)
```

(`headers()`, used by the last assertion in the first new test, is implemented in Task 4;
leave that assertion in place — it is this task's integration proof once Task 4 lands, but
since tasks run in order it will already be available when this task's tests execute. If
running Task 3 in isolation before Task 4 exists, comment out that one assertion and the
whole `test_complete_login_exchanges_code_and_persists_token` body after
`save_token`/`load_token` can instead assert directly: `assert load_token(provider._path) ==
TokenState("AT1", "RT1", pytest.approx(time.time() + 3600, abs=5))`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_oauth.py -v`
Expected: PASS (if Task 4's `headers()` is not yet implemented, use the fallback assertion
noted above for that one test; otherwise all pass)

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/sources/oauth.py tests/unit/test_oauth.py
git commit -m "feat(oauth): TokenProvider login URL and code exchange"
```

---

### Task 4: `TokenProvider` — per-request headers, proactive refresh, reactive refresh on 401

**Files:**
- Modify: `src/telemetry_nerd/sources/oauth.py`
- Modify: `tests/unit/test_oauth.py`

**Interfaces:**
- Consumes: `TokenProvider.__init__`, `TokenProvider._exchange` (Task 3); `TokenState`,
  `load_token`, `save_token` (Task 2).
- Produces: `async def headers(self) -> dict[str, str]`; `async def on_401(self) -> bool`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_oauth.py`:

```python
import asyncio


def _seed_token(provider: TokenProvider, *, expires_in_s: float) -> None:
    from telemetry_nerd.sources.oauth import TokenState, save_token

    save_token(
        provider._path,
        TokenState("AT0", "RT0", time.time() + expires_in_s),
    )


async def test_headers_returns_the_stored_token_when_fresh(tmp_path):
    provider = TokenProvider(_oauth_ref(), "sso", tmp_path)
    _seed_token(provider, expires_in_s=3600)
    assert await provider.headers() == {"Authorization": "Bearer AT0"}


async def test_headers_raises_missing_secret_when_never_logged_in(tmp_path):
    provider = TokenProvider(_oauth_ref(), "sso", tmp_path)
    with pytest.raises(MissingSecret, match="no OAuth login"):
        await provider.headers()


async def test_headers_refreshes_proactively_near_expiry(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = TokenProvider(_oauth_ref(), "sso", tmp_path, client=client)
    _seed_token(provider, expires_in_s=30)  # inside the 60s margin

    headers = await provider.headers()
    assert headers == {"Authorization": "Bearer AT1"}
    assert calls[0]["grant_type"] == "refresh_token"
    assert calls[0]["refresh_token"] == "RT0"


async def test_on_401_forces_a_refresh_and_reports_success(tmp_path):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600})
        )
    )
    provider = TokenProvider(_oauth_ref(), "sso", tmp_path, client=client)
    _seed_token(provider, expires_in_s=3600)  # still "fresh", but the server says 401
    assert await provider.on_401() is True
    assert await provider.headers() == {"Authorization": "Bearer AT1"}


async def test_on_401_with_no_token_at_all_returns_false(tmp_path):
    provider = TokenProvider(_oauth_ref(), "sso", tmp_path)
    assert await provider.on_401() is False


async def test_refresh_failure_raises_missing_secret(tmp_path):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    )
    provider = TokenProvider(_oauth_ref(), "sso", tmp_path, client=client)
    _seed_token(provider, expires_in_s=30)
    with pytest.raises(MissingSecret, match="token exchange failed"):
        await provider.headers()


async def test_concurrent_refresh_calls_hit_the_token_endpoint_once(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = TokenProvider(_oauth_ref(), "sso", tmp_path, client=client)
    _seed_token(provider, expires_in_s=30)

    results = await asyncio.gather(*(provider.headers() for _ in range(5)))
    assert all(h == {"Authorization": "Bearer AT1"} for h in results)
    assert len(calls) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_oauth.py -k "headers or on_401 or concurrent_refresh" -v`
Expected: FAIL — `AttributeError: 'TokenProvider' object has no attribute 'headers'`

- [ ] **Step 3: Implement `headers()` and `on_401()`**

Append to the `TokenProvider` class in `src/telemetry_nerd/sources/oauth.py`:

```python
    async def headers(self) -> dict[str, str]:
        state = load_token(self._path)
        if state is None:
            raise MissingSecret(
                "source has no OAuth login yet",
                hint="run source_connect to log in",
            )
        if state.expires_at - _EXPIRY_MARGIN_S <= time.time():
            state = await self._refresh(state)
        return {"Authorization": f"Bearer {state.access_token}"}

    async def on_401(self) -> bool:
        state = load_token(self._path)
        if state is None:
            return False
        await self._refresh(state)
        return True

    async def _refresh(self, state: TokenState) -> TokenState:
        async with self._lock:
            current = load_token(self._path)
            if current is not None and current.access_token != state.access_token:
                return current  # another caller already refreshed while we waited
            data = {
                "grant_type": "refresh_token",
                "refresh_token": state.refresh_token,
                "client_id": self._oauth.client_id,
            }
            secret = self._oauth.client_secret(self._environ)
            if secret is not None:
                data["client_secret"] = secret
            new_state = await self._exchange(data, prior_refresh_token=state.refresh_token)
            save_token(self._path, new_state)
            return new_state
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_oauth.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/sources/oauth.py tests/unit/test_oauth.py
git commit -m "feat(oauth): per-request headers with proactive and reactive refresh"
```

---

### Task 5: `CallbackListener`

**Files:**
- Modify: `src/telemetry_nerd/sources/oauth.py`
- Test: `tests/unit/test_oauth_listener.py` (new)

**Interfaces:**
- Consumes: `OAuthLoginTimeout`, `OAuthLoginFailed` (Task 2).
- Produces: `class CallbackListener` — async context manager; `.redirect_uri -> str`
  property (valid only inside the `async with` block); `async def wait_for_code(self,
  timeout_s: float = 300.0) -> tuple[str, str]` (code, state).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_oauth_listener.py
import asyncio

import httpx
import pytest

from telemetry_nerd.sources.oauth import CallbackListener, OAuthLoginTimeout


async def test_redirect_uri_is_localhost_with_a_live_port():
    async with CallbackListener() as listener:
        assert listener.redirect_uri.startswith("http://127.0.0.1:")
        assert listener.redirect_uri.endswith("/callback")


async def test_wait_for_code_returns_the_callback_code_and_state():
    async with CallbackListener() as listener:
        async def fire():
            async with httpx.AsyncClient() as client:
                await client.get(listener.redirect_uri, params={"code": "abc", "state": "xyz"})

        fired = asyncio.create_task(fire())
        code, state = await listener.wait_for_code(timeout_s=5)
        await fired
        assert (code, state) == ("abc", "xyz")


async def test_wait_for_code_times_out_with_no_callback():
    async with CallbackListener() as listener:
        with pytest.raises(OAuthLoginTimeout):
            await listener.wait_for_code(timeout_s=0.2)


async def test_callback_response_body_tells_the_user_to_close_the_window():
    async with CallbackListener() as listener:
        async def fire():
            async with httpx.AsyncClient() as client:
                return await client.get(
                    listener.redirect_uri, params={"code": "abc", "state": "xyz"}
                )

        task = asyncio.create_task(fire())
        await listener.wait_for_code(timeout_s=5)
        resp = await task
        assert resp.status_code == 200
        assert "close this window" in resp.text.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_oauth_listener.py -v`
Expected: FAIL — `ImportError: cannot import name 'CallbackListener'`

- [ ] **Step 3: Implement `CallbackListener`**

Append to `src/telemetry_nerd/sources/oauth.py`:

```python
_CALLBACK_BODY = b"<html><body>Logged in. You can close this window.</body></html>"


class CallbackListener:
    """A local HTTP server that lives only for the duration of one OAuth login: it accepts
    exactly one GET to /callback, captures code+state, and shuts down. No ASGI framework —
    one route, one request, raw enough to parse by hand."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self._host = host
        self._server: asyncio.base_events.Server | None = None
        self._result: asyncio.Future[tuple[str, str]] | None = None

    async def __aenter__(self) -> CallbackListener:
        self._result = asyncio.get_running_loop().create_future()
        self._server = await asyncio.start_server(self._handle, self._host, 0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    @property
    def redirect_uri(self) -> str:
        assert self._server is not None, "CallbackListener must be used as `async with`"
        port = self._server.sockets[0].getsockname()[1]
        return f"http://{self._host}:{port}/callback"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b""):
                    break
            parts = request_line.decode("latin-1").split()
            path = parts[1] if len(parts) >= 2 else "/"
            query = urlsplit(path).query
            params = parse_qs(query)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
                + str(len(_CALLBACK_BODY)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + _CALLBACK_BODY
            )
            await writer.drain()
            if self._result is not None and not self._result.done():
                code = params.get("code", [None])[0]
                state = params.get("state", [None])[0]
                if code and state:
                    self._result.set_result((code, state))
                else:
                    self._result.set_exception(
                        OAuthLoginFailed(
                            "OAuth callback was missing code or state",
                            hint="try source_connect again",
                        )
                    )
        finally:
            writer.close()

    async def wait_for_code(self, timeout_s: float = 300.0) -> tuple[str, str]:
        assert self._result is not None, "CallbackListener must be used as `async with`"
        try:
            return await asyncio.wait_for(self._result, timeout_s)
        except asyncio.TimeoutError as e:
            raise OAuthLoginTimeout(
                f"no OAuth callback received within {timeout_s:g}s",
                hint="the login page was never completed; run source_connect again",
            ) from e
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_oauth_listener.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/sources/oauth.py tests/unit/test_oauth_listener.py
git commit -m "feat(oauth): local callback listener for the PKCE redirect"
```

---

### Task 6: `PromQLSource` OAuth integration

**Files:**
- Modify: `src/telemetry_nerd/sources/promql.py`
- Modify: `tests/unit/test_sources.py`

**Interfaces:**
- Consumes: `TokenProvider` (Task 3/4), `OAuthRef` (Task 1).
- Produces: `PromQLSource.__init__(..., token_provider: TokenProvider | None = None)`;
  `PromQLSource.from_spec(spec, environ=os.environ, client=None, data_dir: Path | None =
  None)` — `data_dir` is required (raises `AssertionError` if omitted) when `spec.auth` is
  an `OAuthRef`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_sources.py` (adjust the import block at the top of that file to
add `OAuthRef`, `TokenProvider`, and `httpx` if not already present):

```python
from pathlib import Path

from telemetry_nerd.sources.oauth import TokenProvider, save_token, token_path
from telemetry_nerd.sources.oauth import TokenState
from telemetry_nerd.sources.spec import OAuthRef


def _oauth_spec(tmp_path, **kw) -> SourceSpec:
    return SourceSpec.model_validate(
        {
            "name": "sso",
            "url": "http://prom.example.com",
            "auth": {
                "authorize_url": "https://idp.example.com/authorize",
                "token_url": "https://idp.example.com/token",
                "client_id": "tn-client",
            },
            **kw,
        }
    )


def _seed_oauth_token(tmp_path, name: str = "sso") -> None:
    save_token(token_path(tmp_path, name), TokenState("AT0", "RT0", time.time() + 3600))


async def test_from_spec_requires_data_dir_for_oauth_sources(tmp_path):
    with pytest.raises(AssertionError):
        PromQLSource.from_spec(_oauth_spec(tmp_path))


async def test_oauth_source_sends_the_bearer_token_per_request(tmp_path):
    import time

    _seed_oauth_token(tmp_path)
    seen = {}

    def fake(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": []}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    src = PromQLSource.from_spec(_oauth_spec(tmp_path), client=client, data_dir=tmp_path)
    await src.probe()
    assert seen["auth"] == "Bearer AT0"


async def test_oauth_source_refreshes_and_retries_once_on_401(tmp_path):
    _seed_oauth_token(tmp_path)
    calls = {"query": 0, "token": 0}

    def fake(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600})
        calls["query"] += 1
        if request.headers.get("Authorization") == "Bearer AT0":
            return httpx.Response(401, json={"status": "error", "error": "unauthorized"})
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": []}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    src = PromQLSource.from_spec(_oauth_spec(tmp_path), client=client, data_dir=tmp_path)
    await src.probe()
    assert calls == {"query": 2, "token": 1}


async def test_oauth_source_raises_after_retry_still_401(tmp_path):
    _seed_oauth_token(tmp_path)

    def fake(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600})
        return httpx.Response(401, json={"status": "error", "error": "unauthorized"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    src = PromQLSource.from_spec(_oauth_spec(tmp_path), client=client, data_dir=tmp_path)
    with pytest.raises(SourceError, match="401"):
        await src.probe()
```

Check `PromQLSource.probe()`'s existing request path (`_get_json` via whichever endpoint
`probe()` calls — read `src/telemetry_nerd/sources/promql.py`'s `probe` method before
writing the final assertions; the mock `fake` above must answer whatever path `probe()`
actually hits, matching the existing `test_sources.py` probe tests' fixtures for the real
path and response shape).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_sources.py -k oauth -v`
Expected: FAIL — `TypeError: from_spec() got an unexpected keyword argument 'data_dir'`

- [ ] **Step 3: Implement the integration**

In `src/telemetry_nerd/sources/promql.py`, add the import:

```python
from telemetry_nerd.sources.oauth import TokenProvider
from telemetry_nerd.sources.spec import OAuthRef
```

Change `PromQLSource.__init__` to accept the provider:

```python
        headers: Mapping[str, str] | None = None,
        gate: Gate | None = None,
        backend: str | None = None,
        token_provider: TokenProvider | None = None,
    ) -> None:
        ...
        self._headers = {"User-Agent": USER_AGENT, **(headers or {})}
        self._gate = gate or Gate()
        self._token_provider = token_provider
```

Change `aclose`:

```python
    async def aclose(self) -> None:
        if self._token_provider is not None:
            await self._token_provider.aclose()
        if self._owns_client:
            await self._client.aclose()
```

Change `from_spec`:

```python
    @classmethod
    def from_spec(
        cls,
        spec: SourceSpec,
        environ: Mapping[str, str] = os.environ,
        client: httpx.AsyncClient | None = None,
        data_dir: Path | None = None,
    ) -> PromQLSource:
        """Build a live source; resolves the secret reference now (raises MissingSecret)."""
        token_provider = None
        headers: Mapping[str, str] = {}
        if isinstance(spec.auth, OAuthRef):
            assert data_dir is not None, "OAuth sources need data_dir"
            token_provider = TokenProvider(spec.auth, spec.name, data_dir, environ=dict(environ))
        elif spec.auth is not None:
            headers = spec.auth.headers(environ)
        return cls(
            spec.name,
            spec.url,
            flavor=spec.flavor,
            resolution_ms=spec.resolution_ms,
            limits=Limits(timeout_s=spec.politeness.timeout_s),
            client=client,
            headers=headers,
            gate=Gate(spec.politeness.max_concurrency, spec.politeness.min_interval_ms),
            token_provider=token_provider,
        )
```

Add `from pathlib import Path` to the imports if not already present. Finally, change
`_get_json` to merge OAuth headers per request and retry once on 401:

```python
    async def _get_json(
        self, path: str, params: Mapping[str, str | list[str]], timeout_s: float | None = None
    ) -> dict:
        url = f"{self.base_url}{path}"
        timeout_s = timeout_s or self.limits.timeout_s
        headers = self._headers
        if self._token_provider is not None:
            headers = {**headers, **(await self._token_provider.headers())}
        try:
            async with self._gate.slot():
                resp = await self._client.get(
                    url, params=params, headers=headers, timeout=timeout_s
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
        if resp.status_code == 401 and self._token_provider is not None:
            await self._token_provider.on_401()
            headers = {**self._headers, **(await self._token_provider.headers())}
            async with self._gate.slot():
                resp = await self._client.get(
                    url, params=params, headers=headers, timeout=timeout_s
                )
        if resp.status_code == 401:
            raise SourceError(
                f"authentication failed (HTTP 401) querying {self.base_url}",
                hint="re-run source_connect to log in again, or check the static credential",
            )
        if resp.status_code == 429 or resp.status_code >= 500:
            raise SourceUnavailable(
                f"source returned HTTP {resp.status_code}",
                hint="the source is overloaded or failing; retry shortly or narrow the query",
            )
        ...  # the rest of the method (JSON parsing etc.) is unchanged
```

Leave the rest of `_get_json` (from `try: body = resp.json()` onward) exactly as it is
today — only the header computation and the new 401 block are new.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_sources.py -v`
Expected: PASS, including the new `oauth` tests and all pre-existing tests in the file

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/sources/promql.py tests/unit/test_sources.py
git commit -m "feat(oauth): PromQLSource per-request OAuth headers with retry-once-on-401"
```

---

### Task 7: `ElasticsearchSource` OAuth integration

**Files:**
- Modify: `src/telemetry_nerd/sources/elasticsearch.py`
- Modify: `tests/unit/test_es_source.py`

**Interfaces:**
- Consumes: `TokenProvider` (Task 3/4), `OAuthRef` (Task 1). Mirrors Task 6 exactly, applied
  to `ElasticsearchSource._request` instead of `PromQLSource._get_json`.
- Produces: `ElasticsearchSource.__init__(..., token_provider: TokenProvider | None =
  None)`; `ElasticsearchSource.from_spec(spec, environ=os.environ, client=None, data_dir:
  Path | None = None)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_es_source.py` (reuse `FakeEs`'s `URL`/`PATTERN` helpers and the
`spec()` helper already defined in that file):

```python
import time

from telemetry_nerd.sources.oauth import TokenState, save_token, token_path


def oauth_spec(**kw) -> SourceSpec:
    return spec(
        auth={
            "authorize_url": "https://idp.example.com/authorize",
            "token_url": "https://idp.example.com/token",
            "client_id": "tn-client",
        },
        **kw,
    )


def _seed_oauth_token(tmp_path, name: str = "logs") -> None:
    save_token(token_path(tmp_path, name), TokenState("AT0", "RT0", time.time() + 3600))


async def test_from_spec_requires_data_dir_for_oauth_sources(tmp_path):
    with pytest.raises(AssertionError):
        ElasticsearchSource.from_spec(oauth_spec())


async def test_oauth_source_sends_bearer_token_per_request(tmp_path):
    _seed_oauth_token(tmp_path)
    fake = FakeEs()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    src = ElasticsearchSource.from_spec(oauth_spec(), client=client, data_dir=tmp_path)
    await src.probe()
    assert fake.requests[0].headers["Authorization"] == "Bearer AT0"


async def test_oauth_source_refreshes_and_retries_once_on_401(tmp_path):
    _seed_oauth_token(tmp_path)
    calls = {"query": 0, "token": 0}
    root_body = fixture("root.json") if False else None  # placeholder removed below

    def fake(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600})
        calls["query"] += 1
        if request.headers.get("Authorization") == "Bearer AT0":
            return httpx.Response(401, json=es_error("security_exception", "unauthorized"))
        return FakeEs().__call__(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    src = ElasticsearchSource.from_spec(oauth_spec(), client=client, data_dir=tmp_path)
    await src.probe()
    assert calls["token"] == 1 and calls["query"] == 2
```

Remove the dead `root_body = ...` placeholder line before committing — it was left in while
drafting and is not needed; `FakeEs().__call__(request)` already answers the retried request
correctly using the fixtures the file already loads. Check `FakeEs`'s actual call signature
in `tests/unit/es_fake.py` before finalizing — it must be invokable as a plain
`httpx.MockTransport` handler the way `test_from_spec_sends_the_api_key_and_user_agent`
already uses it (`httpx.MockTransport(fake)` where `fake = FakeEs()`), so
`FakeEs().__call__` vs. just `FakeEs()(request)` — use whichever the existing file's `fake(...)`
calling convention is (read `es_fake.py`'s `FakeEs.__call__` before writing this test, and
match it exactly rather than guessing).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_es_source.py -k oauth -v`
Expected: FAIL — `TypeError: from_spec() got an unexpected keyword argument 'data_dir'`

- [ ] **Step 3: Implement the integration**

Apply the same shape of change as Task 6, in `src/telemetry_nerd/sources/elasticsearch.py`:

```python
from pathlib import Path

from telemetry_nerd.sources.oauth import TokenProvider
from telemetry_nerd.sources.spec import OAuthRef
```

`__init__` gains `token_provider: TokenProvider | None = None`, stored as
`self._token_provider = token_provider`; `aclose` closes it first, same as Task 6.

`from_spec`:

```python
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
            assert data_dir is not None, "OAuth sources need data_dir"
            token_provider = TokenProvider(spec.auth, spec.name, data_dir, environ=dict(environ))
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
```

`_request`:

```python
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
        try:
            async with self._gate.slot():
                resp = await self._client.request(
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
        if resp.status_code == 401 and self._token_provider is not None:
            await self._token_provider.on_401()
            headers = {**self._headers, **(await self._token_provider.headers())}
            async with self._gate.slot():
                resp = await self._client.request(
                    method, url, params=params, json=body, headers=headers,
                    timeout=timeout_s,
                )  # fmt: skip
        if resp.status_code == 401:
            raise SourceError(
                f"authentication failed (HTTP 401) querying {self.base_url}",
                hint="re-run source_connect to log in again, or check the static credential",
            )
        ...  # the rest of the method is unchanged — read it in the file before editing to
        # preserve its existing error-mapping table exactly
```

Read the current body of `_request` past this point (error-mapping for ES-specific status
codes) before editing, and keep every line after the original `except httpx.HTTPError`
block exactly as it is — only the header computation and the 401 block above are new.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_es_source.py -v`
Expected: PASS, including new `oauth` tests and all pre-existing tests

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/sources/elasticsearch.py tests/unit/test_es_source.py
git commit -m "feat(oauth): ElasticsearchSource per-request OAuth headers with retry-once-on-401"
```

---

### Task 8: `grafana.py` OAuth headers + `bootstrap.py` data_dir wiring

**Files:**
- Modify: `src/telemetry_nerd/sources/grafana.py`
- Modify: `src/telemetry_nerd/core/bootstrap.py`
- Modify: `tests/unit/test_grafana.py`

**Interfaces:**
- Consumes: nothing new from `oauth.py` directly — `grafana.py` stays decoupled from
  `TokenProvider`; a caller that already has one resolves `await
  token_provider.headers()` and passes the result in.
- Produces: `discover_datasources(url, auth=None, *, oauth_headers: Mapping[str, str] |
  None = None, client=None, environ=os.environ)`; `probe_backend(proxy_url, auth=None, *,
  oauth_headers: Mapping[str, str] | None = None, client=None, environ=os.environ,
  timeout_s=30.0)`; `source_factory(spec: SourceSpec, data_dir: Path) -> Source` (now takes
  `data_dir`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_grafana.py`:

```python
async def test_discover_datasources_uses_oauth_headers_when_given():
    captured = {}

    def fake(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"datasources": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    await discover_datasources(
        "https://grafana.example.com",
        auth=None,
        oauth_headers={"Authorization": "Bearer AT0"},
        client=client,
    )
    assert captured["auth"] == "Bearer AT0"


async def test_probe_backend_uses_oauth_headers_when_given():
    captured = {}

    def fake(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"data": {"version": "2.1.0"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    await probe_backend(
        "https://grafana.example.com/proxy",
        auth=None,
        oauth_headers={"Authorization": "Bearer AT0"},
        client=client,
    )
    assert captured["auth"] == "Bearer AT0"
```

Also add a `tests/unit/test_bootstrap.py` case (that file already exists per the earlier
`fd` listing):

```python
def test_source_factory_threads_data_dir_to_oauth_sources(tmp_path):
    from telemetry_nerd.core.bootstrap import source_factory
    from telemetry_nerd.sources.spec import SourceSpec

    spec = SourceSpec.model_validate(
        {
            "name": "sso",
            "url": "http://prom.example.com",
            "auth": {
                "authorize_url": "https://idp.example.com/authorize",
                "token_url": "https://idp.example.com/token",
                "client_id": "tn-client",
            },
        }
    )
    source = source_factory(spec, tmp_path)
    assert source._token_provider is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_grafana.py tests/unit/test_bootstrap.py -k "oauth or data_dir" -v`
Expected: FAIL — `TypeError: discover_datasources() got an unexpected keyword argument 'oauth_headers'`

- [ ] **Step 3: Implement**

In `src/telemetry_nerd/sources/grafana.py`, change both signatures:

```python
async def discover_datasources(
    url: str,
    auth: AuthRef | None = None,
    *,
    oauth_headers: Mapping[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
    environ: Mapping[str, str] = os.environ,
) -> list[GrafanaDatasource]:
    base = url.rstrip("/")
    headers = oauth_headers if oauth_headers is not None else (
        auth.headers(environ) if auth is not None else {}
    )
    if auth is not None or oauth_headers is not None:
        path, parse = "/api/datasources", _from_datasources_api
    else:
        path, parse = "/api/frontend/settings", _from_frontend_settings
    async with _client(client) as http:
        resp = await _get(http, f"{base}{path}", headers)
        return parse(_json(resp, base))
```

```python
async def probe_backend(
    proxy_url: str,
    auth: AuthRef | None = None,
    *,
    oauth_headers: Mapping[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
    environ: Mapping[str, str] = os.environ,
    timeout_s: float = 30.0,
) -> tuple[str, Flavor]:
    headers = oauth_headers if oauth_headers is not None else (
        auth.headers(environ) if auth is not None else {}
    )
    async with _client(client) as http:
        try:
            resp = await http.get(
                f"{proxy_url.rstrip('/')}/api/v1/status/buildinfo",
                headers={"User-Agent": USER_AGENT, **headers},
                timeout=timeout_s,
            )
            ...  # unchanged from here
```

In `src/telemetry_nerd/core/bootstrap.py`:

```python
def source_factory(spec: SourceSpec, data_dir: Path) -> Source:
    """The live adapter for a spec (resolves its secret reference now: raises MissingSecret)."""
    if spec.flavor in ES_FLAVORS:
        return ElasticsearchSource.from_spec(spec, data_dir=data_dir)
    return PromQLSource.from_spec(spec, data_dir=data_dir)
```

And in `build_service`, bind `data_dir` with `functools.partial` (add `import functools` at
the top of the file):

```python
    sources = SourceRegistry(wcon, functools.partial(source_factory, data_dir=settings.data_dir))
```

Add `from pathlib import Path` to `bootstrap.py`'s imports if not already present (it is
not, per the file currently importing `Settings` but not `Path` directly).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_grafana.py tests/unit/test_bootstrap.py -v`
Expected: PASS, including pre-existing tests in both files (the `Factory` type alias in
`sources/registry.py` is `Callable[[SourceSpec], Source]` and does not need to change since
`functools.partial` erases the second parameter from the callable's call signature as seen
by `SourceRegistry`)

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/sources/grafana.py src/telemetry_nerd/core/bootstrap.py \
        tests/unit/test_grafana.py tests/unit/test_bootstrap.py
git commit -m "feat(oauth): grafana.py accepts precomputed OAuth headers; thread data_dir to source_factory"
```

---

### Task 9: `source_connect` OAuth login step (service + MCP tool)

**Files:**
- Modify: `src/telemetry_nerd/core/service.py`
- Modify: `src/telemetry_nerd/mcp/server.py`
- Modify: `tests/unit/test_service_sources.py`
- Modify: `tests/unit/test_mcp_sources.py`

**Interfaces:**
- Consumes: `TokenProvider`, `CallbackListener`, `OAuthLoginFailed` (Tasks 3-5);
  `OAuthRef` (Task 1); `TelemetryService.source_connect` (existing, `core/service.py:1948`).
- Produces: `TelemetryService.source_connect(spec, *, replace=False, actor="claude") ->
  dict` — unchanged signature and return shape. Per the spec's single-call design: when
  `spec.auth` is an `OAuthRef` with no persisted token yet for `spec.name`, this one call
  drives the full PKCE login (opens `CallbackListener`, returns its URL to the caller via a
  log line for visibility, awaits the callback up to 300s) and then proceeds to build and
  probe the adapter in the same call — exactly as `gcloud auth login` blocks until the
  browser flow completes. A new module-level function,
  `oauth_login(oauth: OAuthRef, name: str, data_dir: Path, listener: CallbackListener) ->
  str` (in `sources/oauth.py`), does the login given an already-open listener, so service
  code and tests can both drive it without touching `TokenProvider` internals; it returns
  the login URL (for the caller to surface to the user before awaiting), via an
  `asyncio.Event`-free return: see Step 3 for the exact two-phase shape (get the URL, then
  await completion) that makes this testable without network access.

- [ ] **Step 1: Add `oauth_login` to `sources/oauth.py` and write its failing test**

Append to `tests/unit/test_oauth.py`:

```python
from telemetry_nerd.sources.oauth import CallbackListener, OAuthLoginFailed, oauth_login


async def test_oauth_login_completes_against_a_fake_idp_and_callback(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "AT0", "refresh_token": "RT0", "expires_in": 3600})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with CallbackListener() as listener:
        login = oauth_login(_oauth_ref(), "sso", tmp_path, listener, client=client)
        url = next(login)  # phase 1: the login URL, before any network call

        async def fire():
            params = dict(httpx.QueryParams(httpx.URL(url).params))
            async with httpx.AsyncClient() as c:
                await c.get(listener.redirect_uri, params={"code": "code123", "state": params["state"]})

        task = asyncio.create_task(fire())
        with pytest.raises(StopAsyncIteration):
            await login.asend(None)  # phase 2: drive the exchange to completion
        await task

    provider = TokenProvider(_oauth_ref(), "sso", tmp_path)
    assert await provider.headers() == {"Authorization": "Bearer AT0"}


async def test_oauth_login_rejects_a_mismatched_callback_state(tmp_path):
    async with CallbackListener() as listener:
        login = oauth_login(_oauth_ref(), "sso", tmp_path, listener)
        next(login)

        async def fire():
            async with httpx.AsyncClient() as c:
                await c.get(listener.redirect_uri, params={"code": "code123", "state": "wrong"})

        task = asyncio.create_task(fire())
        with pytest.raises(OAuthLoginFailed, match="did not match"):
            await login.asend(None)
        await task
```

A two-phase generator is more machinery than this plan's other tests use, and is only
justified here because it is the one place a caller legitimately needs the login URL
*before* the (slow, human-speed) wait completes. If, once writing this, a plain two-method
class (`class OAuthLogin: def url(self) -> str; async def complete(self) -> None`) reads
easier than a generator while keeping the same two-phase property, prefer the class — pick
whichever reads clearer and keep it consistent with `oauth.py`'s existing style (plain
functions and one class so far); do not implement both.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/test_oauth.py -k oauth_login -v`
Expected: FAIL — `ImportError: cannot import name 'oauth_login'`

- [ ] **Step 3: Implement `oauth_login`**

Append to `src/telemetry_nerd/sources/oauth.py`:

```python
async def oauth_login(
    oauth: OAuthRef,
    name: str,
    data_dir: Path,
    listener: CallbackListener,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = 300.0,
):
    """Two-phase coroutine generator: `url = next(gen)` gets the login URL immediately
    (before any network call), then `await gen.asend(None)` awaits the callback and
    completes the token exchange, raising StopAsyncIteration on success. Used by
    source_connect, which must hand the URL to the caller before the slow, human-speed wait
    for the callback begins."""
    provider = TokenProvider(oauth, name, data_dir, client=client)
    try:
        url, state, verifier = provider.login_url(listener.redirect_uri)
        yield url
        code, got_state = await listener.wait_for_code(timeout_s)
        if got_state != state:
            raise OAuthLoginFailed(
                "OAuth callback state did not match (possible CSRF)",
                hint="run source_connect again to get a fresh login URL",
            )
        await provider.complete_login(code, verifier, listener.redirect_uri)
    finally:
        await provider.aclose()
```

This is an `async def` generator (has both `yield` and `await`) — callers drive it with
`next()` for the first, synchronous-looking yield and `asend(None)` for the rest, exactly as
the tests above do. If the class alternative from Step 1's note is chosen instead, give it
the equivalent two-method shape and update the tests to call `.url()` then `await
.complete()` rather than `next()`/`asend`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_oauth.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/sources/oauth.py tests/unit/test_oauth.py
git commit -m "feat(oauth): oauth_login drives the PKCE dance against an open CallbackListener"
```

- [ ] **Step 6: Wire `source_connect` to use it — write the failing test**

Append to `tests/unit/test_service_sources.py` (reuse that file's existing `make_service`/
fixture helpers — read its top of file for the exact names before writing, and match them
rather than inventing new ones):

```python
import asyncio
import time

import httpx

from telemetry_nerd.sources.oauth import TokenState, save_token, token_path


def _oauth_spec(**kw) -> SourceSpec:
    return SourceSpec.model_validate(
        {
            "name": "sso",
            "url": "http://prom.example.com",
            "auth": {
                "authorize_url": "https://idp.example.com/authorize",
                "token_url": "https://idp.example.com/token",
                "client_id": "tn-client",
            },
            **kw,
        }
    )


async def test_source_connect_with_oauth_and_a_valid_token_connects_without_logging_in(
    tmp_path, monkeypatch
):
    save_token(token_path(tmp_path, "sso"), TokenState("AT0", "RT0", time.time() + 3600))
    service = make_service(tmp_path)

    def fake_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}
        )

    monkeypatch.setattr(
        "telemetry_nerd.sources.promql.httpx.AsyncClient",
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(fake_transport)),
    )
    out = await service.source_connect(_oauth_spec())
    assert service.sources.spec("sso") is not None
    assert out["source"]["auth"]["client_id"] == "tn-client"


async def test_source_connect_with_oauth_and_no_token_drives_a_real_login(tmp_path, monkeypatch):
    service = make_service(tmp_path)

    def fake_transport(request: httpx.Request) -> httpx.Response:
        if request.url.host == "idp.example.com":
            return httpx.Response(
                200, json={"access_token": "AT0", "refresh_token": "RT0", "expires_in": 3600}
            )
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}
        )

    monkeypatch.setattr(
        "telemetry_nerd.sources.promql.httpx.AsyncClient",
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(fake_transport)),
    )
    monkeypatch.setattr(
        "telemetry_nerd.sources.oauth.httpx.AsyncClient",
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(fake_transport)),
    )

    connect_task = asyncio.create_task(service.source_connect(_oauth_spec()))

    async def fire_once_listening() -> None:
        # the real CallbackListener binds an ephemeral port synchronously inside
        # source_connect before awaiting the callback; poll source_status-free state by
        # retrying a short connect loop instead of guessing a sleep duration
        from telemetry_nerd.sources.oauth import token_path as _token_path

        for _ in range(200):  # ~2s worst case, matching the file's other polling tests
            login_url = service.pending_oauth_login_url("sso")  # see Step 7 below
            if login_url is not None:
                break
            await asyncio.sleep(0.01)
        assert login_url is not None
        state = dict(httpx.QueryParams(httpx.URL(login_url).params))["state"]
        redirect_uri = dict(httpx.QueryParams(httpx.URL(login_url).params))["redirect_uri"]
        async with httpx.AsyncClient() as c:
            await c.get(redirect_uri, params={"code": "code123", "state": state})

    await asyncio.gather(connect_task, fire_once_listening())
    assert service.sources.spec("sso") is not None
```

The second test's polling helper (`service.pending_oauth_login_url`) is new surface this
task must add — `TelemetryService` needs some way for a concurrent caller (here, the test;
in production, nothing concurrent needs it, since the login URL is only useful to the one
caller already inside the `source_connect` call) to observe the in-flight login URL. On
reflection this is test-only scaffolding with no real caller — simplify instead: have
`source_connect` log the login URL (`self.log.append(actor, "source.oauth_login_url",
spec.name, {"url": url})`) the instant it is available, and have the test read it back via
`service.log` (check that file's existing `EventLog`/`self.log` query surface — e.g.
`log.recent()` or similar, matching however other tests in this project already assert on
logged events) instead of inventing `pending_oauth_login_url`. Rewrite
`fire_once_listening` to poll `service.log` for that event instead of a new method, once
the actual `EventLog` read API is confirmed by reading `core/events.py`.

- [ ] **Step 7: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_service_sources.py -k oauth -v`
Expected: FAIL — the first test fails with `MissingSecret` (no OAuth wiring yet in
`source_connect`); the second hangs or times out (no login event is ever logged) — if it
hangs, Ctrl-C and fix the polling helper before re-running, don't let it block the suite.

- [ ] **Step 8: Implement**

In `src/telemetry_nerd/core/service.py`, add imports:

```python
from telemetry_nerd.sources.oauth import CallbackListener, oauth_login, token_path
from telemetry_nerd.sources.spec import OAuthRef
```

`TelemetryService` needs `data_dir` to construct the login — check whether it already has
a `data_dir` field; if not, add one (`bootstrap.py`'s `build_service` already has
`settings.data_dir` in scope, so pass `data_dir=settings.data_dir` into the
`TelemetryService(...)` call there). Change `source_connect`:

```python
    async def source_connect(
        self, spec: SourceSpec, *, replace: bool = False, actor: Actor = "claude"
    ) -> dict:
        self._refuse_reserved(spec.name)
        if self.sources.spec(spec.name) is not None and not replace:
            raise SourceError(
                f"source {spec.name!r} already exists",
                hint="pass replace=true to reconfigure it, or choose another name",
            )
        if isinstance(spec.auth, OAuthRef) and token_path(self.data_dir, spec.name).exists() is False:
            await self._oauth_login(spec, actor)
        source = self.sources.build(spec)  # raises MissingSecret before any network call
        try:
            status = await source.probe()
        except BaseException:  # failed or cancelled: the built source must not leak
            await self._close(source)
            raise
        try:  # a concurrent connect may have taken the name while this one probed
            old = self.sources.add(spec, source, replace=replace)
        except BaseException:
            await self._close(source)
            raise
        if old is not None:
            await self._close(old)
        self._resolution_tried.pop(spec.name, None)
        res = await self.learn_resolution(spec.name)
        public = spec.public()
        self.log.append(actor, "source.connected", spec.name, {"source": public})
        out = {"source": public, "status": status}
        if res is not None:
            out["resolution"] = res
        return out

    async def _oauth_login(self, spec: SourceSpec, actor: Actor) -> None:
        assert isinstance(spec.auth, OAuthRef)
        async with CallbackListener() as listener:
            login = oauth_login(spec.auth, spec.name, self.data_dir, listener)
            url = await login.__anext__()
            self.log.append(actor, "source.oauth_login_url", spec.name, {"url": url})
            try:
                await login.asend(None)
            except StopAsyncIteration:
                pass
```

(`token_path(...).exists()` is a direct existence check rather than
`load_token(...) is not None`, deliberately: a present-but-unparseable token file should
still skip the login step and let `TokenProvider.headers()` raise its own `MissingSecret`
once the adapter actually queries, rather than this method silently papering over a corrupt
file by re-logging in — this is a judgment call made while writing this task; if it proves
wrong in review, the fix is a one-line swap back to `load_token(...) is None`.)

Use whichever `EventLog` read method `core/events.py` actually exposes to finish the
test's polling helper (Step 6) — read that file now, before finishing this step, and match
its real API rather than the placeholder names used above.

- [ ] **Step 9: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_service_sources.py -v`
Expected: PASS

Run: `uv run pytest tests/unit/test_service_sources.py -v`
Expected: PASS

- [ ] **Step 10: Wire the MCP tool and add its test**

In `src/telemetry_nerd/mcp/server.py`, `source_connect`'s tool wrapper needs new OAuth
config parameters mirroring `auth_env`/`auth_file`/`auth_scheme`:

```python
        oauth_authorize_url: str | None = None,
        oauth_token_url: str | None = None,
        oauth_client_id: str | None = None,
        oauth_client_secret_env: str | None = None,
        oauth_scopes: list[str] | None = None,
```

added to the tool's parameter list (after `auth_scheme`), and in the docstring:

```
        OAuth (interactive SSO login, e.g. Okta): give oauth_authorize_url, oauth_token_url,
        oauth_client_id (and oauth_client_secret_env / oauth_scopes if the IdP app needs
        them) instead of auth_env/auth_file. The first source_connect call drives an
        interactive login (it returns once you, the user, finish logging in at the URL
        Claude shows you) and then connects; mutually exclusive with auth_env/auth_file.
```

and in the body, build the auth payload:

```python
        oauth = None
        if oauth_authorize_url is not None:
            oauth = {
                "authorize_url": oauth_authorize_url,
                "token_url": oauth_token_url,
                "client_id": oauth_client_id,
                "client_secret_env": oauth_client_secret_env,
                "scopes": oauth_scopes or [],
            }
        auth = oauth or _auth_ref(auth_env, auth_file, auth_scheme)
```

(replacing the existing `auth = _auth_ref(auth_env, auth_file, auth_scheme)` line), and
raise `ToolError` up front if both are given:

```python
        if oauth is not None and (auth_env is not None or auth_file is not None):
            raise ToolError("pass either oauth_* or auth_env/auth_file, not both")
```

Add a test to `tests/unit/test_mcp_sources.py` (read its existing structure first — it
likely drives the tool through the `mcp.Client`/`build_mcp` pattern seen in
`test_grafana.py`'s imports) asserting that calling `source_connect` with
`oauth_authorize_url` set and `auth_env` also set raises a `ToolError`-shaped failure
(`isError` true, matching whatever assertion style the file's existing error-path tests use —
copy their exact pattern rather than inventing a new one).

- [ ] **Step 11: Run the full suite to check for regressions**

Run: `uv run pytest tests/unit -q`
Expected: PASS (all unit tests)

- [ ] **Step 12: Commit**

```bash
git add src/telemetry_nerd/core/service.py src/telemetry_nerd/core/bootstrap.py \
        src/telemetry_nerd/mcp/server.py \
        tests/unit/test_service_sources.py tests/unit/test_mcp_sources.py
git commit -m "feat(oauth): source_connect drives the PKCE login before connecting an OAuth source"
```

---

## Final Checks

After Task 9, before considering the epic done:

- [ ] Run `just lint && uv run pytest tests/unit -q` — must be green.
- [ ] Run `cd ui && npx vitest run && cd .. && just ui-check` — no UI files changed by this
  plan, so this is a regression check only.
- [ ] Grep for any remaining direct `spec.auth.headers(` call sites outside
  `sources/promql.py`/`sources/elasticsearch.py`/`sources/grafana.py`/`sources/spec.py`
  that might need the same OAuth-awareness (there should be none — these are the three
  consumers identified in the spec).
- [ ] File a follow-up bead for Client Credentials flow if a real use case for it surfaces
  (explicitly out of scope here per the spec).
