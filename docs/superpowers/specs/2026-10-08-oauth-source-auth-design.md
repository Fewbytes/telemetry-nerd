# OAuth Source Auth (Authorization Code + PKCE) Design

> Bead: telemetry-nerd-f5kw

## Problem

Sources behind SSO (Grafana/Prometheus/ES via Okta etc.) need OAuth login,
not a static long-lived secret. Today `AuthRef.headers(environ)`
(`src/telemetry_nerd/sources/spec.py`) is a one-shot call: `PromQLSource`
and `ElasticsearchSource` bake its result into a static `self._headers`
dict at construction; `grafana.py` calls it fresh per call but still with
no refresh. No adapter has a refresh-on-expiry or retry-on-401 path.

## Scope

Authorization Code + PKCE (interactive user login), shared plumbing used
by `PromQLSource`, `ElasticsearchSource`, and `grafana.py` from the start.
Client Credentials (machine-to-machine) is out of scope — no stated use
case for it yet; adding it later is additive (new `OAuthRef` variant), not
a rework of the plumbing below.

## Architecture

New module `src/telemetry_nerd/sources/oauth.py`:

- `OAuthRef` (pydantic model, lives in `spec.py` next to `AuthRef`):
  `authorize_url`, `token_url`, `client_id`, `client_secret: str | None`,
  `scopes: list[str]`. Mutually exclusive with `AuthRef` on `SourceSpec`
  (`auth: AuthRef | OAuthRef | None`, enforced by a model validator — a
  source is static-secret or OAuth, never both).
- `TokenProvider`: one instance per OAuth-configured source, owns the
  access/refresh token pair, a single-flight refresh lock, and token-file
  persistence. Public surface:
  - `async def headers(self) -> dict[str, str]` — returns current
    `Authorization: Bearer <token>`, refreshing first if the stored token
    is within a 60s expiry margin.
  - `async def on_401(self) -> bool` — call sites invoke this after a 401;
    it forces one refresh (through the same single-flight lock) and
    returns whether a new token is now available, so the caller can retry
    once.
  - `def login_url(self, redirect_uri: str) -> tuple[str, str, str]` —
    returns `(authorize_url_with_params, state, code_verifier)` for the
    PKCE flow.
  - `async def complete_login(self, code: str, code_verifier: str,
    redirect_uri: str) -> None` — exchanges the code for tokens, persists
    them.
- `CallbackListener`: a short-lived local HTTP server
  (`http://localhost:<ephemeral-port>/callback`) started only during
  login, started via `asyncio`, not threads (matches the daemon's existing
  async model). Captures `code`/`state`, hands them back to the caller,
  shuts down on receipt or a timeout (5 min).
- Token persistence: `<data_dir>/oauth-tokens/<source_name>.json`, one
  file per source, containing `access_token`, `refresh_token`,
  `expires_at` (epoch seconds). Plain file, OS permissions (0600) —
  same trust model as today's `file`-based `AuthRef`, no new crypto layer.

## Components

**`SourceSpec`/`OAuthRef`** (`sources/spec.py`): config surface. No
secret value is ever a parameter — `client_secret`, like today's
`AuthRef.env`/`file`, is a reference (env var name), not the token
itself; redirect URI is never configured (always `localhost`, port chosen
at login time — see Data Flow).

**`TokenProvider`** (`sources/oauth.py`): the only piece that knows about
expiry, refresh, and the token file. Constructed from an `OAuthRef` plus
the source's name (for the token file path) and the daemon's data dir.

**Adapter call sites** (`promql.py`, `elasticsearch.py`, `grafana.py`):
replace the static `self._headers` bake-in with, for OAuth-configured
sources, a per-request `await self._token_provider.headers()` call; on a
401 response, `await self._token_provider.on_401()` then retry the request
once. Statically-configured sources (`AuthRef`) are unchanged — they keep
today's cheap one-shot header bake, no per-request cost added.

**MCP tool surface**: `source_connect` grows an `oauth` path alongside its
existing `env`/`file` auth path. Calling it for an OAuth source starts the
`CallbackListener`, returns the authorize URL for Claude to relay to the
user ("visit this URL to log in"), and blocks (daemon-side, async) until
the callback arrives or the listener times out. On success it reports
login complete; on timeout it reports that and leaves the source
unauthenticated (no partial state).

## Data Flow

1. User (via Claude) calls `source_connect` for a source configured with
   `oauth`.
2. Daemon starts `CallbackListener` on an ephemeral `localhost` port,
   builds the authorize URL with PKCE challenge + state via
   `TokenProvider.login_url(redirect_uri)`.
3. Tool call returns the URL; Claude relays it to the user; user logs in
   with their IdP in their own browser.
4. IdP redirects to `http://localhost:<port>/callback?code=...&state=...`.
   `CallbackListener` validates `state` (CSRF guard — mismatch is a hard
   failure, no exchange attempted), captures `code`.
5. `TokenProvider.complete_login(code, verifier, redirect_uri)` exchanges
   the code at `token_url`, persists the token file. Listener shuts down.
6. From then on, every adapter request for that source calls
   `TokenProvider.headers()` per request (proactive refresh near expiry)
   with refresh-and-retry-once on a 401 as the reactive backstop.

RFC 8252 (OAuth for native apps) allows any port on `localhost` as a
registered redirect URI matching only the path — this is why no fixed
port needs to be pre-registered per daemon instance; the user registers
`http://localhost/callback` (no port, or a documented wildcard pattern
per their IdP's convention) once in their IdP app config.

## Error Handling

- **Refresh fails** (refresh token expired/revoked): `TokenProvider`
  raises `MissingSecret` (reused from `spec.py` — same error family the
  rest of auth already uses), hint: "re-run source_connect to log in
  again." Call sites surface this exactly like today's missing-secret
  error.
- **401 with a token that looked valid** (revoked server-side): refresh
  once via `on_401()`, retry the request once; if the retry still 401s,
  surface as a normal upstream auth error — no infinite loop.
- **Login never completed**: `CallbackListener` times out after 5 min;
  `source_connect` reports the timeout; no token file is written, no
  partial state left behind.
- **State mismatch on callback**: treated as a CSRF attempt, exchange is
  never attempted, reported as a login failure.
- **Concurrent requests needing refresh at the same time**: `TokenProvider`
  serializes refreshes behind a single-flight lock — N in-flight requests
  trigger one token-endpoint call, not N.

## Testing

- Unit (`tests/unit/sources/test_oauth.py`): `TokenProvider` against a
  fake token endpoint (httpx mock) — initial exchange, proactive refresh
  near expiry, reactive refresh-on-401, single-flight dedup under
  concurrent callers, refresh failure raises `MissingSecret`.
- Unit: PKCE verifier/challenge generation is correct per RFC 7636; state
  mismatch on callback is rejected before any token exchange.
- e2e: a local fake OAuth server fixture (no real Okta) drives the full
  authorize → callback → token-exchange path through `CallbackListener`
  end to end.
- Adapter tests: one test per call site (`promql.py`, `elasticsearch.py`,
  `grafana.py`) proving an OAuth-configured source calls
  `TokenProvider.headers()` per request rather than baking headers at
  construction, and that a 401 triggers exactly one retry via `on_401()`.

## Out of Scope (follow-up beads if needed)

- Client Credentials flow for pure machine-to-machine sources.
- Token file encryption at rest beyond OS file permissions.
- Dynamic client registration (RFC 7591) — users register an OAuth app
  with their IdP themselves, same as any CLI tool's documented setup step.
