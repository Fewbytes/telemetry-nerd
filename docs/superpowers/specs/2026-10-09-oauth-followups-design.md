# OAuth Follow-Ups: Grafana Discovery + Client Credentials Design

> Beads: telemetry-nerd-8v8o (Grafana+OAuth), telemetry-nerd-d72h (Client Credentials)
> Both are explicitly-deferred follow-ups from the OAuth source auth epic
> (telemetry-nerd-f5kw, `docs/superpowers/specs/2026-10-08-oauth-source-auth-design.md`).

## Problem

Two gaps left open by the OAuth epic, by deliberate ruling at the time:

1. **Grafana+OAuth (8v8o):** `source_connect(grafana=..., uid=..., oauth_*=...)` is
   explicitly rejected with a `ToolError` today. `probe_backend`/`discover_datasources`
   already accept a precomputed `oauth_headers` parameter (Task 8 of the epic), but nothing
   ever threads a real header into it, and nothing drives the login before the probe needs
   a token.
2. **Client Credentials (d72h):** the epic implemented Authorization Code + PKCE only
   (interactive login). Machine-to-machine sources — no human in the loop — have no flow.

## Scope

Both land in the same module (`sources/oauth.py`) and share `TokenProvider`'s plumbing, so
one spec covers both; they are independently shippable (separate tasks, separate beads) and
neither depends on the other.

## Architecture

### 8v8o — Grafana + OAuth

`TelemetryService` gets one new public method, extracted from today's private
`_oauth_login`:

```python
async def ensure_oauth_login(
    self, oauth: OAuthRef, name: str, source_url: str, actor: Actor
) -> None:
    """No-op if a valid token already exists for (oauth, name, source_url). Otherwise
    drives login: interactive PKCE for authorization_code, a direct token POST for
    client_credentials (see d72h below) — no browser, no listener, no login URL."""
```

`source_connect`'s existing OAuth gate calls this instead of inlining the login dance.
The MCP `source_connect` tool's `grafana=`+`uid=` branch, when `oauth_*` is given, calls
`service.ensure_oauth_login(oauth_ref, name, datasource_url, actor="claude")` **before**
`probe_backend` — at that point a token is guaranteed to exist — then builds
`headers = await TokenProvider(oauth_ref, name, datasource_url, data_dir).headers()` and
passes `oauth_headers=headers` into `probe_backend`. Once `probe_backend` returns the
detected backend/flavor, the rest of the flow is unchanged: build the `SourceSpec` with
`auth=oauth_ref` and call `service.source_connect(spec)`. That call's own OAuth gate finds
the token already persisted (same `name` + `issuer_key`) and skips straight through.

No new state, no new storage: the token file written during discovery is the exact file
`PromQLSource.from_spec`'s `TokenProvider` reads afterward — this is the same
decoupled-by-filename design the epic already used for the Grafana-less case (Task 9).

### d72h — Client Credentials

`OAuthRef` gains one field:

```python
flow: Literal["authorization_code", "client_credentials"] = "authorization_code"
```

A model validator requires `client_secret_env` when `flow == "client_credentials"` (this
grant has no PKCE, no browser, no "public client" option — a secret is mandatory, not
optional, for this flow).

`TokenProvider` branches on `self._oauth.flow`:

- **Login:** `authorization_code` keeps `login_url()`/`complete_login()` unchanged.
  `client_credentials` gets a new `async def client_credentials_login(self) -> None` that
  POSTs `grant_type=client_credentials, client_id, client_secret` directly to `token_url`
  (no code, no verifier, no redirect_uri) and persists the result. `ensure_oauth_login`
  calls this instead of opening a `CallbackListener` when the flow is `client_credentials` —
  no login URL, no browser, no 300s timeout; it's one fast HTTP round trip.
- **Refresh:** `authorization_code` keeps today's `grant_type=refresh_token` path using
  `state.refresh_token`. `client_credentials` has no refresh token to spend — "refreshing"
  means re-running the exact same `client_credentials` POST. `_refresh` branches on flow
  before building its request body.
- **`_exchange`'s refresh_token requirement** (`if refresh is None: raise MissingSecret(...)`)
  only applies when `flow == "authorization_code"` — a `client_credentials` response
  legitimately has no `refresh_token` field, and that's not an error.

`TokenState.refresh_token` becomes `str | None` (currently always `str`); for
`client_credentials` it's stored as `None` and `_refresh`'s branch never reads it.

## Data Flow

**8v8o:** `source_connect(grafana=, uid=, oauth_*=)` → `ensure_oauth_login` (PKCE login,
same UX as a direct `url=` OAuth source: browser opens, event logged, blocks up to 300s) →
`probe_backend(datasource_url, oauth_headers=...)` detects flavor → `SourceSpec` built →
`service.source_connect(spec)` → gate sees existing token → `PromQLSource.from_spec` builds
its own `TokenProvider` against the same file → normal operation from here, identical to
every other OAuth source.

**d72h:** `source_connect(url=, oauth_flow="client_credentials", oauth_*=)` →
`ensure_oauth_login` detects the flow → `client_credentials_login()` (one POST, no
interactivity) → token persisted → `source_connect` proceeds to build/probe the adapter →
normal operation; refresh later is the same one-POST exchange, triggered proactively near
expiry or reactively on a 401, exactly like today's refresh-token path but without a refresh
token.

## Error Handling

- **8v8o:** a login failure (timeout, state mismatch, exchange failure) during the Grafana
  path surfaces through the same `SourceError`→`ToolError` mapping `source_connect` already
  uses — no new error types. A `probe_backend` failure after a successful login is unchanged
  from today's non-OAuth Grafana probe failures.
- **d72h:** `client_credentials_login()`'s failure modes reuse `MissingSecret`/
  `SourceUnavailable` exactly as `complete_login()` does today, except the hint text changes
  — there's no "re-run source_connect to log in again" story since there's no browser step
  to redo; the hint becomes "check client_id and client_secret, or that the IdP app has
  client_credentials enabled for this client." `_refresh`'s existing fix (telemetry-nerd-fybd:
  only delete the token file on a 400/401, not on transient 5xx) applies identically to the
  `client_credentials` refresh path — same code path, same status-code gate.

## Testing

- **8v8o:** one fixture-based test (fake IdP + fake Grafana buildinfo endpoint on one shared
  `MockTransport` dispatched by host) driving `source_connect(grafana=, uid=, oauth_*=)`
  end to end: login → probe_backend receives the real bearer token → backend detected →
  source connected. One test confirming the MCP tool's existing grafana+oauth `ToolError`
  rejection is removed/replaced by this real path (update, don't just delete, the Task 9
  regression test that currently asserts the rejection).
- **d72h:** `TokenProvider` unit tests for `client_credentials_login()` (POST shape, no
  code/verifier/redirect_uri sent, secret always included), `_refresh`'s client_credentials
  branch (re-POSTs the same grant, no `refresh_token` in the body), and confirmation that
  `_exchange` does NOT raise on a response with no `refresh_token` when flow is
  `client_credentials`. One `source_connect`-level test confirming no `CallbackListener` is
  opened and no event is logged for this flow (purely a control-flow assertion, e.g.
  monkeypatch `CallbackListener.__init__` to fail the test if called).

## Out of Scope

- Dynamic client registration (RFC 7591) — unchanged from the epic's original scope
  decision; users still register an OAuth app with their IdP themselves.
- Refreshing a `client_credentials` token proactively on a schedule independent of request
  traffic — the existing proactive-refresh-on-`headers()`-call model is reused unchanged;
  there's no need for a background timer.
