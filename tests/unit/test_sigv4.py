"""Signer: SigV4 signing of outgoing headers, independent of the AWS credential chain."""

import pytest
from botocore.credentials import Credentials

from telemetry_nerd.sources.sigv4 import Signer
from telemetry_nerd.sources.spec import AuthRef, MissingSecret

FAKE = Credentials("AKIAFAKE", "secretfake")
FAKE_WITH_TOKEN = Credentials("AKIAFAKE", "secretfake", token="sessiontoken123")


def test_sign_adds_authorization_and_date():
    signer = Signer("us-east-1", FAKE)
    headers = signer.sign("GET", "http://es.test:9200/", {}, b"")
    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAFAKE/")
    assert "es/aws4_request" in headers["Authorization"]
    assert "X-Amz-Date" in headers
    assert "X-Amz-Security-Token" not in headers


def test_sign_includes_session_token_when_credentials_are_temporary():
    signer = Signer("us-east-1", FAKE_WITH_TOKEN)
    headers = signer.sign("POST", "http://es.test:9200/idx/_search", {}, b'{"a":1}')
    assert headers["X-Amz-Security-Token"] == "sessiontoken123"
    assert "x-amz-security-token" in headers["Authorization"]  # a signed header, not an extra


def test_sign_covers_the_body_a_different_body_changes_the_signature():
    signer = Signer("us-east-1", FAKE)
    a = signer.sign("POST", "http://es.test:9200/idx/_search", {}, b'{"a":1}')
    b = signer.sign("POST", "http://es.test:9200/idx/_search", {}, b'{"a":2}')
    assert a["Authorization"] != b["Authorization"]


def test_sign_covers_query_params():
    signer = Signer("us-east-1", FAKE)
    a = signer.sign("GET", "http://es.test:9200/idx/_field_caps", {}, b"", params={"fields": "x"})
    b = signer.sign("GET", "http://es.test:9200/idx/_field_caps", {}, b"", params={"fields": "y"})
    assert a["Authorization"] != b["Authorization"]


def test_from_auth_raises_missing_secret_with_a_hint_when_no_credentials_are_found(monkeypatch):
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent/config")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent/credentials")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    with pytest.raises(MissingSecret, match="no AWS credentials"):
        Signer.from_auth(AuthRef(scheme="sigv4", region="us-east-1"))


def test_from_auth_resolves_the_standard_credential_chain(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secretfake")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    signer = Signer.from_auth(AuthRef(scheme="sigv4", region="eu-west-1"))
    headers = signer.sign("GET", "http://es.test:9200/", {}, b"")
    assert "AKIAFAKE" in headers["Authorization"]
    assert "eu-west-1/es/aws4_request" in headers["Authorization"]
