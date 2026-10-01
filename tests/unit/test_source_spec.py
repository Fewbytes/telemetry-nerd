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
