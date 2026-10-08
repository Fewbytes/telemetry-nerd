import base64
import hashlib
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from telemetry_nerd.sources.oauth import (
    CallbackListener,
    OAuthLoginFailed,
    TokenProvider,
    TokenState,
    generate_pkce,
    generate_state,
    load_token,
    oauth_login,
    save_token,
    token_path,
)
from telemetry_nerd.sources.spec import MissingSecret, OAuthRef


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

    assert load_token(provider._path) == TokenState(
        "AT1", "RT1", pytest.approx(time.time() + 3600, abs=5)
    )


async def test_complete_login_sends_client_secret_when_configured(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["form"] = dict(httpx.QueryParams(request.content.decode()))
        return httpx.Response(200, json={"access_token": "AT1", "refresh_token": "RT1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = TokenProvider(
        _oauth_ref(client_secret_env="IDP_SECRET"), "sso", tmp_path,
        client=client, environ={"IDP_SECRET": "s3cr3t"},
    )  # fmt: skip
    await provider.complete_login("code123", "verifier123", "http://localhost:1234/callback")
    assert seen["form"]["client_secret"] == "s3cr3t"


async def test_complete_login_rejects_non_200(tmp_path):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(400, json={"error": "invalid_grant"})
        )
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
        return httpx.Response(
            200, json={"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600}
        )

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
            lambda r: httpx.Response(
                200, json={"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600}
            )
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
        transport=httpx.MockTransport(
            lambda r: httpx.Response(400, json={"error": "invalid_grant"})
        )
    )
    provider = TokenProvider(_oauth_ref(), "sso", tmp_path, client=client)
    _seed_token(provider, expires_in_s=30)
    with pytest.raises(MissingSecret, match="token exchange failed"):
        await provider.headers()


async def test_concurrent_refresh_calls_hit_the_token_endpoint_once(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            200, json={"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = TokenProvider(_oauth_ref(), "sso", tmp_path, client=client)
    _seed_token(provider, expires_in_s=30)

    results = await asyncio.gather(*(provider.headers() for _ in range(5)))
    assert all(h == {"Authorization": "Bearer AT1"} for h in results)
    assert len(calls) == 1


async def test_oauth_login_completes_against_a_fake_idp_and_callback(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "AT0", "refresh_token": "RT0", "expires_in": 3600})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with CallbackListener() as listener:
        login = oauth_login(_oauth_ref(), "sso", tmp_path, listener, client=client)
        url = login.url()  # phase 1: the login URL, before any network call

        async def fire():
            params = dict(httpx.QueryParams(httpx.URL(url).params))
            async with httpx.AsyncClient() as c:
                await c.get(listener.redirect_uri, params={"code": "code123", "state": params["state"]})

        task = asyncio.create_task(fire())
        await login.complete()  # phase 2: drive the exchange to completion
        await task

    provider = TokenProvider(_oauth_ref(), "sso", tmp_path)
    assert await provider.headers() == {"Authorization": "Bearer AT0"}


async def test_oauth_login_rejects_a_mismatched_callback_state(tmp_path):
    async with CallbackListener() as listener:
        login = oauth_login(_oauth_ref(), "sso", tmp_path, listener)
        login.url()

        async def fire():
            async with httpx.AsyncClient() as c:
                await c.get(listener.redirect_uri, params={"code": "code123", "state": "wrong"})

        task = asyncio.create_task(fire())
        with pytest.raises(OAuthLoginFailed, match="did not match"):
            await login.complete()
        await task
