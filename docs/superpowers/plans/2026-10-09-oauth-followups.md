# OAuth Follow-Ups: Grafana Discovery + Client Credentials Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the two gaps explicitly deferred from the OAuth source-auth epic — wire real OAuth headers into Grafana-proxy discovery/probing (telemetry-nerd-8v8o), and add a `client_credentials` flow for machine-to-machine sources alongside the existing `authorization_code` + PKCE flow (telemetry-nerd-d72h).

**Architecture:** Both land in `sources/oauth.py`'s `TokenProvider`/`TelemetryService` plumbing. d72h adds a `flow` field to `OAuthRef` and branches `TokenProvider`'s login/refresh on it; 8v8o extracts today's private `_oauth_login` into a public `TelemetryService.ensure_oauth_login` that the Grafana MCP branch calls before probing, then threads the resulting header into `probe_backend`. d72h lands first (Tasks 1-3) since `ensure_oauth_login` (Task 4) must already know about both flows before 8v8o's MCP wiring (Task 6) can use it.

**Tech Stack:** Python, httpx (`httpx.MockTransport` for tests), pydantic, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-10-09-oauth-followups-design.md`

## Global Constraints

- `OAuthRef.flow: Literal["authorization_code", "client_credentials"] = "authorization_code"` — default preserves every existing spec/config.
- `flow == "client_credentials"` requires `client_secret_env` (model validator) — this grant has no PKCE and no public-client option.
- `TokenState.refresh_token` becomes `str | None` — `client_credentials` tokens have no refresh token.
- `_exchange`'s "no refresh_token in response is an error" rule applies only when `flow == "authorization_code"`.
- No new exception types; reuse `MissingSecret` / `SourceUnavailable` / `SourceError` throughout.
- `client_credentials` never opens a `CallbackListener`, never calls `webbrowser.open`, never logs a `source.oauth_login_url` event — it is one HTTP round trip.
- Grafana+OAuth reuses `grafana.py`'s existing `oauth_headers` parameter on `probe_backend`/`discover_datasources` (already shipped, never wired to a caller) — no changes to `grafana.py` itself.
- Token file identity (`issuer_key`, atomic 0o600 writes, single-flight refresh lock) is unchanged by either feature.

---

### Task 1: `OAuthRef.flow` field

**Files:**
- Modify: `src/telemetry_nerd/sources/spec.py` (the `OAuthRef` class, lines 101-142)
- Test: `tests/unit/test_source_spec_oauth.py`

**Interfaces:**
- Produces: `OAuthRef.flow: Literal["authorization_code", "client_credentials"]`, default `"authorization_code"`. `OAuthRef.public()` includes `"flow": self.flow`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_source_spec_oauth.py` (follow that file's existing `OAuthRef.model_validate({...})` helper style):

```python
def test_oauth_ref_defaults_to_authorization_code_flow():
    ref = OAuthRef.model_validate(
        {
            "authorize_url": "https://idp.example.com/authorize",
            "token_url": "https://idp.example.com/token",
            "client_id": "tn-client",
        }
    )
    assert ref.flow == "authorization_code"
    assert ref.public()["flow"] == "authorization_code"


def test_client_credentials_flow_requires_client_secret_env():
    with pytest.raises(ValidationError, match="client_secret_env"):
        OAuthRef.model_validate(
            {
                "authorize_url": "https://idp.example.com/authorize",
                "token_url": "https://idp.example.com/token",
                "client_id": "tn-client",
                "flow": "client_credentials",
            }
        )


def test_client_credentials_flow_with_client_secret_env_is_valid():
    ref = OAuthRef.model_validate(
        {
            "authorize_url": "https://idp.example.com/authorize",
            "token_url": "https://idp.example.com/token",
            "client_id": "tn-client",
            "client_secret_env": "IDP_SECRET",
            "flow": "client_credentials",
        }
    )
    assert ref.flow == "client_credentials"
```

Check the top of `tests/unit/test_source_spec_oauth.py` for `ValidationError` and `pytest` imports; add `from pydantic import ValidationError` if not already imported.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_source_spec_oauth.py -k flow -v`
Expected: FAIL — `flow` is not a known field / `AttributeError`.

- [ ] **Step 3: Implement**

In `src/telemetry_nerd/sources/spec.py`, inside `OAuthRef` (after the `scopes` field, before the validators):

```python
    flow: Literal["authorization_code", "client_credentials"] = "authorization_code"
```

Add a new model validator (after `_env_is_a_name`, before `client_secret`):

```python
    @model_validator(mode="after")
    def _client_credentials_needs_secret(self) -> OAuthRef:
        if self.flow == "client_credentials" and self.client_secret_env is None:
            raise ValueError(
                "flow='client_credentials' requires client_secret_env: this grant has no "
                "PKCE and no public-client option"
            )
        return self
```

Update `public()` to include the new field:

```python
    def public(self) -> dict:
        return {
            "authorize_url": self.authorize_url,
            "token_url": self.token_url,
            "client_id": self.client_id,
            "scopes": self.scopes,
            "client_secret_env": self.client_secret_env,
            "flow": self.flow,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_source_spec_oauth.py -v`
Expected: PASS (all tests in the file, not just the new ones).

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/sources/spec.py tests/unit/test_source_spec_oauth.py
git commit -m "feat(oauth): add flow field to OAuthRef for client_credentials"
```

---

### Task 2: `TokenState.refresh_token` optional + flow-aware `_exchange`

**Files:**
- Modify: `src/telemetry_nerd/sources/oauth.py` (`TokenState` dataclass lines 63-72, `_exchange` lines 168-198)
- Test: `tests/unit/test_oauth.py`

**Interfaces:**
- Consumes: `OAuthRef.flow` (Task 1).
- Produces: `TokenState.refresh_token: str | None`. `TokenProvider._exchange(data, *, prior_refresh_token)` no longer raises when the response has no `refresh_token` AND `self._oauth.flow != "authorization_code"`; when it does raise (non-200, or missing-refresh on `authorization_code`), the hint text branches on `self._oauth.flow`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_oauth.py`. Reuse the file's `_oauth_ref(**kw)` helper (it already takes `**kw` overrides) and `_SOURCE_URL` constant:

```python
async def test_exchange_allows_missing_refresh_token_for_client_credentials(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "AT1", "expires_in": 3600})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    oauth = _oauth_ref(flow="client_credentials", client_secret_env="IDP_SECRET")
    provider = TokenProvider(oauth, "sso", _SOURCE_URL, tmp_path, client=client)

    state = await provider._exchange({"grant_type": "client_credentials"}, prior_refresh_token=None)

    assert state.access_token == "AT1"
    assert state.refresh_token is None


async def test_exchange_still_requires_refresh_token_for_authorization_code(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "AT1", "expires_in": 3600})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = TokenProvider(_oauth_ref(), "sso", _SOURCE_URL, tmp_path, client=client)

    with pytest.raises(MissingSecret, match="refresh_token"):
        await provider._exchange({"grant_type": "authorization_code"}, prior_refresh_token=None)


async def test_exchange_failure_hint_differs_for_client_credentials(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid_client")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    oauth = _oauth_ref(flow="client_credentials", client_secret_env="IDP_SECRET")
    provider = TokenProvider(oauth, "sso", _SOURCE_URL, tmp_path, client=client)

    with pytest.raises(MissingSecret) as exc_info:
        await provider._exchange({"grant_type": "client_credentials"}, prior_refresh_token=None)
    assert "client_credentials enabled" in exc_info.value.hint
```

(Check `MissingSecret`'s `hint` attribute name against `src/telemetry_nerd/sources/base.py`'s `SourceError` — the existing code already constructs `MissingSecret(msg, hint=...)`, so `.hint` is the stored attribute; if the actual attribute differs, match what `SourceError.__init__` stores.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_oauth.py -k "exchange_allows or exchange_still or exchange_failure_hint" -v`
Expected: FAIL — `test_exchange_still_requires_refresh_token_for_authorization_code` passes already (don't worry if it does — the point is the other two fail): `test_exchange_allows_missing_refresh_token_for_client_credentials` fails because `_exchange` currently raises unconditionally when `refresh is None`; `test_exchange_failure_hint_differs_for_client_credentials` fails because the hint text is currently fixed to "re-run source_connect to log in again".

- [ ] **Step 3: Implement**

In `src/telemetry_nerd/sources/oauth.py`, change `TokenState`:

```python
@dataclass(frozen=True)
class TokenState:
    access_token: str
    refresh_token: str | None
    expires_at: float  # epoch seconds
    #: identifies which (IdP, client, source) this token belongs to; a stored token whose
    #: issuer_key doesn't match the current spec is treated as absent everywhere (bead: a
    #: source name reused with a different IdP/url must not reuse another IdP's token)
    issuer_key: str
```

Change `_exchange`:

```python
    async def _exchange(self, data: dict, *, prior_refresh_token: str | None) -> TokenState:
        try:
            resp = await self._client.post(self._oauth.token_url, data=data, timeout=30.0)
        except httpx.HTTPError as e:
            raise SourceUnavailable(
                f"cannot reach the OAuth token endpoint: {e}",
                hint="check token_url and that the IdP is reachable",
            ) from e
        if resp.status_code != 200:
            hint = (
                "check client_id and client_secret, or that the IdP app has "
                "client_credentials enabled for this client"
                if self._oauth.flow == "client_credentials"
                else "re-run source_connect to log in again"
            )
            exc = MissingSecret(f"OAuth token exchange failed: HTTP {resp.status_code}", hint=hint)
            exc.status_code = resp.status_code
            raise exc
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
        if refresh is None and self._oauth.flow == "authorization_code":
            raise MissingSecret(
                "OAuth token endpoint did not return a refresh_token",
                hint="the IdP app must be configured for offline access / refresh tokens",
            )
        return TokenState(access, refresh, time.time() + expires_in, self._issuer_key)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_oauth.py -v`
Expected: PASS (full file — this touches a method every existing OAuth test depends on).

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/sources/oauth.py tests/unit/test_oauth.py
git commit -m "feat(oauth): make refresh_token optional, flow-aware exchange errors"
```

---

### Task 3: `client_credentials_login()` + `_refresh` client_credentials branch

**Files:**
- Modify: `src/telemetry_nerd/sources/oauth.py` (`TokenProvider` class, add method; `_refresh` lines 226-259)
- Test: `tests/unit/test_oauth.py`

**Interfaces:**
- Consumes: `TokenProvider._exchange` (Task 2), `OAuthRef.flow`/`client_secret` (Task 1 / existing).
- Produces: `TokenProvider.client_credentials_login(self) -> None` — POSTs `grant_type=client_credentials`, persists the result. `_refresh` now branches on `self._oauth.flow`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_oauth.py`:

```python
async def test_client_credentials_login_posts_grant_and_persists_token(tmp_path, monkeypatch):
    monkeypatch.setenv("IDP_SECRET", "s3cret")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["form"] = dict(httpx.QueryParams(request.content.decode()))
        return httpx.Response(200, json={"access_token": "AT1", "expires_in": 3600})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    oauth = _oauth_ref(flow="client_credentials", client_secret_env="IDP_SECRET")
    provider = TokenProvider(oauth, "sso", _SOURCE_URL, tmp_path, client=client)

    await provider.client_credentials_login()

    assert seen["form"]["grant_type"] == "client_credentials"
    assert seen["form"]["client_id"] == "tn-client"
    assert seen["form"]["client_secret"] == "s3cret"
    assert "code" not in seen["form"]
    assert "code_verifier" not in seen["form"]
    assert "redirect_uri" not in seen["form"]

    state = load_token(provider._path)
    assert state.access_token == "AT1"
    assert state.refresh_token is None


async def test_client_credentials_login_without_secret_in_environ_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("IDP_SECRET", raising=False)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    oauth = _oauth_ref(flow="client_credentials", client_secret_env="IDP_SECRET")
    provider = TokenProvider(oauth, "sso", _SOURCE_URL, tmp_path, client=client)

    with pytest.raises(MissingSecret, match="IDP_SECRET"):
        await provider.client_credentials_login()


async def test_refresh_client_credentials_re_posts_same_grant_no_refresh_token(tmp_path, monkeypatch):
    monkeypatch.setenv("IDP_SECRET", "s3cret")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        calls.append(form)
        return httpx.Response(200, json={"access_token": f"AT{len(calls)}", "expires_in": 3600})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    oauth = _oauth_ref(flow="client_credentials", client_secret_env="IDP_SECRET")
    provider = TokenProvider(oauth, "sso", _SOURCE_URL, tmp_path, client=client)
    await provider.client_credentials_login()  # AT1, calls[0]

    from telemetry_nerd.sources.oauth import load_token as _load

    stale = _load(provider._path)
    refreshed = await provider._refresh(stale)

    assert calls[1]["grant_type"] == "client_credentials"
    assert "refresh_token" not in calls[1]
    assert refreshed.access_token == "AT2"
    assert refreshed.refresh_token is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_oauth.py -k "client_credentials_login or refresh_client_credentials" -v`
Expected: FAIL — `AttributeError: 'TokenProvider' object has no attribute 'client_credentials_login'`; the refresh test fails with the same error chain or a `KeyError`/`refresh_token` `TypeError` from `_refresh` using `state.refresh_token` unconditionally.

- [ ] **Step 3: Implement**

In `src/telemetry_nerd/sources/oauth.py`, add `client_credentials_login` to `TokenProvider` (place after `complete_login`):

```python
    async def client_credentials_login(self) -> None:
        secret = self._oauth.client_secret(self._environ)
        if secret is None:
            raise MissingSecret(
                f"client_secret_env {self._oauth.client_secret_env!r} is unset or empty",
                hint="export that environment variable in the daemon's environment",
            )
        data = {
            "grant_type": "client_credentials",
            "client_id": self._oauth.client_id,
            "client_secret": secret,
        }
        state = await self._exchange(data, prior_refresh_token=None)
        async with self._lock:
            save_token(self._path, state)
```

Replace `_refresh` with:

```python
    async def _refresh(
        self, state: TokenState, *, rejected_access_token: str | None = None
    ) -> TokenState:
        async with self._lock:
            current = self._load_current()
            check_against = rejected_access_token or state.access_token
            if current is not None and current.access_token != check_against:
                return current  # another caller already refreshed while we waited
            if self._oauth.flow == "client_credentials":
                secret = self._oauth.client_secret(self._environ)
                if secret is None:
                    raise MissingSecret(
                        f"client_secret_env {self._oauth.client_secret_env!r} is unset or empty",
                        hint="export that environment variable in the daemon's environment",
                    )
                data = {
                    "grant_type": "client_credentials",
                    "client_id": self._oauth.client_id,
                    "client_secret": secret,
                }
                prior_refresh_token = None
            else:
                data = {
                    "grant_type": "refresh_token",
                    "refresh_token": state.refresh_token,
                    "client_id": self._oauth.client_id,
                }
                secret = self._oauth.client_secret(self._environ)
                if secret is not None:
                    data["client_secret"] = secret
                prior_refresh_token = state.refresh_token
            try:
                new_state = await self._exchange(data, prior_refresh_token=prior_refresh_token)
            except MissingSecret as e:
                # only a real rejection (invalid_grant: 400/401) means the refresh token is
                # dead with no recovery path otherwise; a transient IdP failure (5xx, 429, a
                # momentarily malformed response) must not force a fresh interactive login
                if getattr(e, "status_code", None) in (400, 401):
                    # a dead refresh token leaves no recovery path otherwise: delete the
                    # token file so the next source_connect logs in again instead of
                    # repeating this — but never a token a concurrent fresh login already
                    # wrote in our place (source_connect(replace=True) racing an old
                    # adapter's in-flight refresh)
                    current = self._load_current()
                    if current is None or current.access_token == state.access_token:
                        self._path.unlink(missing_ok=True)
                raise
            save_token(self._path, new_state)
            return new_state
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_oauth.py -v`
Expected: PASS (full file).

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/sources/oauth.py tests/unit/test_oauth.py
git commit -m "feat(oauth): add client_credentials_login and refresh branch"
```

---

### Task 4: `TelemetryService.ensure_oauth_login`

**Files:**
- Modify: `src/telemetry_nerd/core/service.py` (replace `_has_valid_token`/`_oauth_login` at lines 1993-2008, update `source_connect` call site at lines 1969-1970)
- Test: `tests/unit/test_service_sources.py`

**Interfaces:**
- Consumes: `TokenProvider.client_credentials_login` (Task 3), `OAuthLogin`/`CallbackListener`/`oauth_login` (existing), `OAuthRef.flow` (Task 1).
- Produces: `TelemetryService.ensure_oauth_login(self, oauth: OAuthRef, name: str, source_url: str, actor: Actor) -> None` — no-op if a valid token already exists for `(oauth, name, source_url)`; otherwise drives the right login for `oauth.flow`. `source_connect` and the future Grafana MCP branch (Task 6) both call this directly.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_service_sources.py`. Reuse `_oauth_spec`/`_real_factory` helpers already in the file:

```python
async def test_ensure_oauth_login_is_a_noop_with_a_valid_existing_token(tmp_path):
    from telemetry_nerd.sources.oauth import TokenState, issuer_key, save_token, token_path

    service = make_service(tmp_path, factory=_real_factory(tmp_path))
    spec = _oauth_spec()
    save_token(
        token_path(tmp_path, spec.name),
        TokenState("AT0", "RT0", time.time() + 3600, issuer_key(spec.auth, spec.url)),
    )

    # must not open a browser or a CallbackListener: a valid token already exists
    import telemetry_nerd.core.service as service_module

    def fail_if_called(*a, **kw):
        raise AssertionError("CallbackListener must not be constructed")

    orig = service_module.CallbackListener
    service_module.CallbackListener = fail_if_called
    try:
        await service.ensure_oauth_login(spec.auth, spec.name, spec.url, "claude")
    finally:
        service_module.CallbackListener = orig


async def test_ensure_oauth_login_client_credentials_skips_browser_and_listener(
    tmp_path, monkeypatch
):
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    monkeypatch.setenv("IDP_SECRET", "s3cret")

    service = make_service(tmp_path, factory=_real_factory(tmp_path))
    oauth = _oauth_spec(
        auth={
            "authorize_url": "https://idp.example.com/authorize",
            "token_url": "https://idp.example.com/token",
            "client_id": "tn-client",
            "client_secret_env": "IDP_SECRET",
            "flow": "client_credentials",
        }
    ).auth

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "AT1", "expires_in": 3600})

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "telemetry_nerd.sources.oauth.httpx.AsyncClient",
        lambda **kw: real_async_client(transport=httpx.MockTransport(handler)),
    )

    await service.ensure_oauth_login(oauth, "sso", "http://prom.example.com", "claude")

    assert opened == []
    events = [e for e in service.log.tail(50) if e.type == "source.oauth_login_url"]
    assert events == []
    from telemetry_nerd.sources.oauth import load_token, token_path

    state = load_token(token_path(tmp_path, "sso"))
    assert state.access_token == "AT1"
```

Check `httpx` is already imported at the top of `tests/unit/test_service_sources.py` (it is, per the earlier read) and that `time` is too (it is).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_service_sources.py -k ensure_oauth_login -v`
Expected: FAIL — `AttributeError: 'TelemetryService' object has no attribute 'ensure_oauth_login'`.

- [ ] **Step 3: Implement**

In `src/telemetry_nerd/core/service.py`, replace `_has_valid_token` and `_oauth_login` (lines 1993-2008) with:

```python
    async def ensure_oauth_login(
        self, oauth: OAuthRef, name: str, source_url: str, actor: Actor
    ) -> None:
        """No-op if a valid token already exists for (oauth, name, source_url). Otherwise
        drives login: interactive PKCE for authorization_code, a direct token POST for
        client_credentials — no browser, no listener, no login URL."""
        state = load_token(token_path(self.data_dir, name))
        if state is not None and state.issuer_key == issuer_key(oauth, source_url):
            return
        if oauth.flow == "client_credentials":
            provider = TokenProvider(oauth, name, source_url, self.data_dir)
            try:
                await provider.client_credentials_login()
            finally:
                await provider.aclose()
            return
        async with CallbackListener() as listener:
            login = oauth_login(oauth, name, source_url, self.data_dir, listener)
            url = login.url()
            self.log.append(actor, "source.oauth_login_url", name, {"url": url})
            try:  # best effort: a headless/SSH daemon falls back to the log entry/timeout hint
                webbrowser.open(url)
            except webbrowser.Error:
                pass
            await login.complete()
```

Update the `oauth` import at the top of the file to add `TokenProvider`:

```python
from telemetry_nerd.sources.oauth import (
    CallbackListener,
    TokenProvider,
    issuer_key,
    load_token,
    oauth_login,
    token_path,
)
```

Update `source_connect` (lines 1969-1970):

```python
        if isinstance(spec.auth, OAuthRef):
            await self.ensure_oauth_login(spec.auth, spec.name, spec.url, actor)
```

(This replaces `if isinstance(spec.auth, OAuthRef) and not self._has_valid_token(spec): await self._oauth_login(spec, actor)` — `ensure_oauth_login`'s own token check subsumes `_has_valid_token`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_service_sources.py -v`
Expected: PASS (full file — `source_connect`'s existing OAuth tests exercise the same code path through the new method).

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/core/service.py tests/unit/test_service_sources.py
git commit -m "feat(oauth): extract ensure_oauth_login, support client_credentials in source_connect"
```

---

### Task 5: MCP `source_connect` tool — `oauth_flow` parameter

**Files:**
- Modify: `src/telemetry_nerd/mcp/server.py` (`source_connect` tool, lines 866-1017)
- Test: `tests/unit/test_mcp_sources.py`

**Interfaces:**
- Consumes: `OAuthRef.flow` (Task 1).
- Produces: `source_connect(..., oauth_flow: str = "authorization_code", ...)` MCP param; the `oauth` dict built at lines 958-964 includes `"flow": oauth_flow`.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_mcp_sources.py`:

```python
async def test_client_credentials_oauth_source_connect_skips_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("IDP_SECRET", "s3cret")
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "idp.example.com":
            return httpx.Response(200, json={"access_token": "AT1", "expires_in": 3600})
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}
        )

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "telemetry_nerd.sources.promql.httpx.AsyncClient",
        lambda **kw: real_async_client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(
        "telemetry_nerd.sources.oauth.httpx.AsyncClient",
        lambda **kw: real_async_client(transport=httpx.MockTransport(handler)),
    )

    mcp = build_mcp(make_service(tmp_path, factory=lambda spec: source_factory(spec, tmp_path)), "http://x")
    r = await call(
        mcp,
        "source_connect",
        {
            "name": "m2m",
            "url": URL,
            "oauth_authorize_url": "https://idp.example.com/authorize",
            "oauth_token_url": "https://idp.example.com/token",
            "oauth_client_id": "tn-client",
            "oauth_client_secret_env": "IDP_SECRET",
            "oauth_flow": "client_credentials",
        },
    )
    assert not r.is_error
    assert opened == []
```

Add `httpx` import and `from telemetry_nerd.core.bootstrap import source_factory` at the top of the file if not already present at module scope (the earlier read showed `source_factory`/`ElasticsearchSource` imported mid-file at lines 151-152 for a different test — either reuse that import or add `import httpx` at the top alongside the existing imports).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_mcp_sources.py -k client_credentials -v`
Expected: FAIL — `oauth_flow` is an unexpected keyword argument (MCP tool call returns a tool error, or the test's `assert not r.is_error` fails), since the current `source_connect` signature has no `oauth_flow` param.

- [ ] **Step 3: Implement**

In `src/telemetry_nerd/mcp/server.py`, add a parameter to the `source_connect` signature (after `oauth_scopes`, lines 877-881):

```python
        oauth_authorize_url: str | None = None,
        oauth_token_url: str | None = None,
        oauth_client_id: str | None = None,
        oauth_client_secret_env: str | None = None,
        oauth_scopes: list[str] | None = None,
        oauth_flow: str = "authorization_code",
```

Update the `oauth` dict construction (lines 958-964):

```python
        oauth = None
        if oauth_authorize_url is not None:
            if grafana is not None:
                raise ToolError(
                    "Grafana datasources with OAuth are not yet supported; connect with "
                    "url= and oauth_* directly, or use a static auth_env/auth_file token"
                )
            oauth = {
                "authorize_url": oauth_authorize_url,
                "token_url": oauth_token_url,
                "client_id": oauth_client_id,
                "client_secret_env": oauth_client_secret_env,
                "scopes": oauth_scopes or [],
                "flow": oauth_flow,
            }
```

(This step's rejection of `grafana is not None` is unchanged here — Task 6 removes it. Leave it in place so this task's diff stays scoped to `oauth_flow` plumbing.)

Add one line to the tool's docstring, in the OAuth paragraph (after the existing OAuth sentence, around line 929):

```
        oauth_flow: "authorization_code" (default, interactive SSO login) or
        "client_credentials" (machine-to-machine: no browser, requires oauth_client_secret_env).
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_mcp_sources.py -v`
Expected: PASS (full file).

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/mcp/server.py tests/unit/test_mcp_sources.py
git commit -m "feat(oauth): expose oauth_flow on the source_connect MCP tool"
```

---

### Task 6: Grafana + OAuth — wire real headers into discovery/probe

**Files:**
- Modify: `src/telemetry_nerd/mcp/server.py` (`source_connect` tool, grafana branch at lines 987-1002, remove rejection at lines 952-957)
- Test: `tests/unit/test_mcp_sources.py` (update the existing rejection test), `tests/unit/test_grafana.py` or `tests/unit/test_mcp_sources.py` (new end-to-end test)

**Interfaces:**
- Consumes: `TelemetryService.ensure_oauth_login` (Task 4), `TokenProvider.headers()` (existing), `grafana.probe_backend(..., oauth_headers=...)` (existing, already supports this kwarg — no change to `grafana.py`).
- Produces: `source_connect(grafana=..., uid=..., oauth_*=...)` now performs a real OAuth login and connects, instead of raising `ToolError`.

- [ ] **Step 1: Update the existing rejection test into a real-path test**

Replace `test_grafana_with_oauth_is_rejected_not_attempted` in `tests/unit/test_mcp_sources.py` with:

```python
async def test_grafana_with_oauth_logs_in_then_probes_with_the_token(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "idp.example.com":
            return httpx.Response(200, json={"access_token": "AT1", "expires_in": 3600})
        if request.url.path.endswith("/api/v1/status/buildinfo"):
            assert request.headers.get("Authorization") == "Bearer AT1"
            return httpx.Response(200, json={"data": {"version": "2.1.0"}})
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}
        )

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "telemetry_nerd.sources.grafana.httpx.AsyncClient",
        lambda **kw: real_async_client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(
        "telemetry_nerd.sources.promql.httpx.AsyncClient",
        lambda **kw: real_async_client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(
        "telemetry_nerd.sources.oauth.httpx.AsyncClient",
        lambda **kw: real_async_client(transport=httpx.MockTransport(handler)),
    )

    mcp = build_mcp(
        make_service(tmp_path, factory=lambda spec: source_factory(spec, tmp_path)), "http://x"
    )
    r = await call(
        mcp,
        "source_connect",
        {
            "name": "sso",
            "grafana": "https://play.grafana.org",
            "uid": "grafanacloud-prom",
            "oauth_authorize_url": "https://idp.example.com/authorize",
            "oauth_token_url": "https://idp.example.com/token",
            "oauth_client_id": "tn-client",
        },
    )
    assert not r.is_error
    body = json.loads(text(r))
    assert body["backend"] == "prometheus"
```

Check `source_factory`, `json`, and `httpx` are importable in this test file's scope — add `import httpx` and `from telemetry_nerd.core.bootstrap import source_factory` near the top if not already present (the mid-file import at the original lines 151-152 can be hoisted to the top-of-file imports in this task, since this task now needs it for a second test too).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_mcp_sources.py -k grafana_with_oauth -v`
Expected: FAIL — `r.is_error` is `True` with "not yet supported" (the current rejection still fires).

- [ ] **Step 3: Implement**

In `src/telemetry_nerd/mcp/server.py`:

1. Remove the rejection at lines 952-957 — delete the `if grafana is not None: raise ToolError(...)` block from inside `if oauth_authorize_url is not None:`, leaving just the `oauth = {...}` dict construction:

```python
        oauth = None
        if oauth_authorize_url is not None:
            oauth = {
                "authorize_url": oauth_authorize_url,
                "token_url": oauth_token_url,
                "client_id": oauth_client_id,
                "client_secret_env": oauth_client_secret_env,
                "scopes": oauth_scopes or [],
                "flow": oauth_flow,
            }
```

2. Add the import (top of file, alongside existing `telemetry_nerd.sources` imports):

```python
from telemetry_nerd.sources.oauth import TokenProvider
from telemetry_nerd.sources.spec import OAuthRef
```

(Check whether `OAuthRef` is already imported in this file — the earlier read showed `AuthRef` imported for the existing `AuthRef.model_validate(auth)` call; add `OAuthRef` alongside it if it's a combined import line, otherwise add a new line.)

3. Replace the grafana branch (original lines 987-1002):

```python
        try:
            if grafana is not None:
                if url is not None:
                    raise ToolError("pass either grafana+uid or url, not both")
                if uid is None:
                    raise ToolError(
                        "grafana needs uid: see source_discover_grafana(url=...) for datasource uids"
                    )
                datasource_url = proxy_url(grafana, uid)
                oauth_headers = None
                static_auth = None
                if oauth is not None:
                    oauth_ref = OAuthRef.model_validate(oauth)
                    await service.ensure_oauth_login(oauth_ref, name, datasource_url, actor="claude")
                    oauth_headers = await TokenProvider(
                        oauth_ref, name, datasource_url, service.data_dir
                    ).headers()
                elif auth:
                    static_auth = AuthRef.model_validate(auth)
                backend, detected_flavor = await probe_backend(
                    datasource_url, static_auth, oauth_headers=oauth_headers
                )
                out = await service.source_connect(
                    spec(datasource_url, detected_flavor), replace=replace
                )
                return _dump({**out, "backend": backend})
```

(`TokenProvider(...).headers()` opens its own short-lived `httpx.AsyncClient` since no `client=` is passed — matching how `ensure_oauth_login`'s own `client_credentials_login` path and every other one-shot `TokenProvider` use in this codebase already works; nothing to `aclose()` here since `headers()` only refreshes-if-needed and returns, and the instance is discarded immediately after.)

4. Update the docstring's Grafana+OAuth note (originally implying it's unsupported — around lines 900-904/913): change "Through Grafana (grafana+uid) is not supported yet." (which was about Elasticsearch via Grafana, leave that one alone) and the OAuth paragraph (lines 925-929) to drop any remaining "mutually exclusive" wording that implied Grafana exclusion — confirm by re-reading the rendered docstring after the edit that no leftover sentence still claims grafana+oauth is rejected.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_mcp_sources.py tests/unit/test_grafana.py -v`
Expected: PASS (both files).

- [ ] **Step 5: Run full suite and commit**

```bash
uv run pytest tests/unit -q
just lint
git add src/telemetry_nerd/mcp/server.py tests/unit/test_mcp_sources.py
git commit -m "feat(oauth): support OAuth login for Grafana-proxied datasources"
```

---

## Self-Review Notes (from plan authoring, not a task)

- Spec coverage: Problem/Architecture/Data Flow/Error Handling for both 8v8o and d72h are each covered — Task 1 (OAuthRef.flow + validator), Tasks 2-3 (TokenProvider branching, error hints), Task 4 (ensure_oauth_login, both flows), Task 5 (MCP oauth_flow param), Task 6 (Grafana wiring + real headers + updated regression test). The spec's "Out of Scope" items (dynamic client registration, proactive scheduled refresh) have no corresponding task — correct, by design.
- `grafana.py` itself needs no changes — `oauth_headers` was already shipped in the original epic's Task 8 and is exercised by `test_discover_datasources_uses_oauth_headers_when_given`/`test_probe_backend_uses_oauth_headers_when_given`, already passing.
- Task ordering: 1→2→3→4→5→6 is a strict dependency chain (each task's interface feeds the next); no parallelism available, but each is independently testable and revertible.
