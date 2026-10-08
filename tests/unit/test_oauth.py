import base64
import hashlib

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
