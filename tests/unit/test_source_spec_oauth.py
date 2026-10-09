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
        "flow": "authorization_code",
    }


def test_auth_and_oauth_both_set_is_rejected():
    with pytest.raises(ValidationError):
        SourceSpec.model_validate(
            {
                "name": "sso",
                "url": "https://prom.example.com",
                "auth": {"env": "TOKEN", **_oauth()},
            }
        )


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
    # control case: a plain AuthRef still works unchanged
    spec = SourceSpec.model_validate(
        {"name": "sso", "url": "https://prom.example.com", "auth": {"env": "TOKEN"}}
    )
    assert isinstance(spec.auth, AuthRef)
