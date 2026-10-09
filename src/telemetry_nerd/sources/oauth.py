"""OAuth2 Authorization Code + PKCE support, shared by PromQLSource, ElasticsearchSource,
and grafana.py in place of AuthRef's static secret for sources behind interactive SSO.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Self
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from telemetry_nerd.sources.base import SourceError, SourceUnavailable
from telemetry_nerd.sources.spec import MissingSecret, OAuthRef

_EXPIRY_MARGIN_S = 60

#: refresh locks shared across TokenProvider instances for the same token file: two
#: instances can be live at once (e.g. source_connect(replace=True) while the old adapter
#: is still in use) and a per-instance lock would let them refresh concurrently, risking an
#: IdP's rotation-reuse detection revoking the whole token family
_refresh_locks: dict[Path, asyncio.Lock] = {}


def _lock_for(path: Path) -> asyncio.Lock:
    lock = _refresh_locks.get(path)
    if lock is None:
        lock = _refresh_locks[path] = asyncio.Lock()
    return lock


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
    refresh_token: str | None
    expires_at: float  # epoch seconds
    #: identifies which (IdP, client, source) this token belongs to; a stored token whose
    #: issuer_key doesn't match the current spec is treated as absent everywhere (bead: a
    #: source name reused with a different IdP/url must not reuse another IdP's token)
    issuer_key: str


def issuer_key(oauth: OAuthRef, source_url: str) -> str:
    return f"{oauth.token_url}|{oauth.client_id}|{source_url}"


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
    """Atomic, 0o600 from creation: a crash mid-write must never leave a corrupt or
    world-readable token file in place."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(asdict(state)))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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
        source_url: str,
        data_dir: Path,
        *,
        client: httpx.AsyncClient | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        self._oauth = oauth
        self._path = token_path(data_dir, name)
        self._issuer_key = issuer_key(oauth, source_url)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient()
        self._environ = environ if environ is not None else dict(os.environ)
        self._lock = _lock_for(self._path)

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
        async with self._lock:
            save_token(self._path, state)

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

    def _load_current(self) -> TokenState | None:
        """The stored token, or None if there is none or it belongs to a different
        IdP/client/source (a reused source name must never see another issuer's token)."""
        state = load_token(self._path)
        if state is None or state.issuer_key != self._issuer_key:
            return None
        return state

    async def headers(self) -> dict[str, str]:
        state = self._load_current()
        if state is None:
            raise MissingSecret(
                "source has no OAuth login yet",
                hint="run source_connect to log in",
            )
        if state.expires_at - _EXPIRY_MARGIN_S <= time.time():
            state = await self._refresh(state)
        return {"Authorization": f"Bearer {state.access_token}"}

    async def on_401(self, rejected_access_token: str) -> bool:
        state = self._load_current()
        if state is None:
            return False
        await self._refresh(state, rejected_access_token=rejected_access_token)
        return True

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


_CALLBACK_BODY = b"<html><body>Logged in. You can close this window.</body></html>"


class CallbackListener:
    """A local HTTP server that lives only for the duration of one OAuth login: it accepts
    exactly one GET to /callback, captures code+state, and shuts down. No ASGI framework —
    one route, one request, raw enough to parse by hand."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self._host = host
        self._server: asyncio.base_events.Server | None = None
        self._result: asyncio.Future[tuple[str, str]] | None = None

    async def __aenter__(self) -> Self:
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
        except TimeoutError as e:
            raise OAuthLoginTimeout(
                f"no OAuth callback received within {timeout_s:g}s",
                hint="the login page was never completed; run source_connect again",
            ) from e


class OAuthLogin:
    """Drives one PKCE login against an already-open `CallbackListener`. Two-phase: `url()`
    returns the login URL immediately (no network call), then `complete()` awaits the
    callback and finishes the token exchange. Split so a caller (e.g. `source_connect`) can
    hand the URL to the user before the slow, human-speed wait begins."""

    def __init__(
        self,
        oauth: OAuthRef,
        name: str,
        source_url: str,
        data_dir: Path,
        listener: CallbackListener,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 300.0,
    ) -> None:
        self._provider = TokenProvider(oauth, name, source_url, data_dir, client=client)
        self._listener = listener
        self._timeout_s = timeout_s
        self._url, self._state, self._verifier = self._provider.login_url(listener.redirect_uri)

    def url(self) -> str:
        return self._url

    async def complete(self) -> None:
        try:
            try:
                code, got_state = await self._listener.wait_for_code(self._timeout_s)
            except OAuthLoginTimeout as e:
                raise OAuthLoginTimeout(
                    f"{e}; if the browser did not open, log in at {self._url}", hint=e.hint
                ) from e
            if got_state != self._state:
                raise OAuthLoginFailed(
                    "OAuth callback state did not match (possible CSRF)",
                    hint="run source_connect again to get a fresh login URL",
                )
            await self._provider.complete_login(code, self._verifier, self._listener.redirect_uri)
        finally:
            await self._provider.aclose()


def oauth_login(
    oauth: OAuthRef,
    name: str,
    source_url: str,
    data_dir: Path,
    listener: CallbackListener,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = 300.0,
) -> OAuthLogin:
    return OAuthLogin(
        oauth, name, source_url, data_dir, listener, client=client, timeout_s=timeout_s
    )
