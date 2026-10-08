"""What Claude may configure about a source at runtime.

Secrets are references (an environment variable name or a file path) resolved
inside the daemon; a secret value is never an argument, stored, logged or returned.
"""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from telemetry_nerd.model.time import check_timezone
from telemetry_nerd.sources.base import SourceError

NAME_PATTERN = r"^[a-z][a-z0-9_-]{0,31}$"
RESERVED_NAMES = frozenset({"default"})
_ENV_VAR = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
ES_FLAVORS = frozenset({"elasticsearch", "opensearch"})
_INDEX_FORBIDDEN = re.compile(r'[\s/\\"<>|#]')


class MissingSecret(SourceError):
    pass


class AuthRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    env: str | None = None
    file: str | None = None
    scheme: Literal["bearer", "basic", "apikey"] = "bearer"

    @field_validator("env")
    @classmethod
    def _env_is_a_name(cls, v: str | None) -> str | None:
        if v is not None and not _ENV_VAR.match(v):
            raise ValueError(
                "auth env must be the NAME of an environment variable (e.g. GRAFANA_TOKEN) "
                "in the daemon's environment, never the secret itself"
            )
        return v

    @field_validator("file")
    @classmethod
    def _file_is_absolute(cls, v: str | None) -> str | None:
        if v is not None and not Path(v).is_absolute():
            raise ValueError("auth file must be an absolute path readable by the daemon")
        return v

    @model_validator(mode="after")
    def _exactly_one(self) -> AuthRef:
        if (self.env is None) == (self.file is None):
            raise ValueError("auth needs exactly one of env or file")
        return self

    def _secret(self, environ: Mapping[str, str]) -> str | None:
        if self.env is not None:
            return environ.get(self.env) or None
        try:
            return Path(self.file).read_text().strip() or None  # type: ignore[arg-type]
        except OSError:
            return None

    def is_set(self, environ: Mapping[str, str] = os.environ) -> bool:
        return self._secret(environ) is not None

    def headers(self, environ: Mapping[str, str] = os.environ) -> dict[str, str]:
        secret = self._secret(environ)
        if secret is None:
            where = f"environment variable {self.env}" if self.env else f"file {self.file}"
            raise MissingSecret(
                f"secret not found: {where} is unset or empty",
                hint=(
                    "ask the user to write the token to that file, or to export the variable "
                    "in the daemon's environment (env vars need a daemon restart; files do not)"
                ),
            )
        if self.scheme == "apikey":
            # the encoded key Elasticsearch hands out (base64 of id:api_key), used as is
            return {"Authorization": f"ApiKey {secret}"}
        if self.scheme == "basic":
            return {"Authorization": "Basic " + base64.b64encode(secret.encode()).decode()}
        return {"Authorization": f"Bearer {secret}"}

    def public(self, environ: Mapping[str, str] = os.environ) -> dict:
        return {
            "env": self.env,
            "file": self.file,
            "scheme": self.scheme,
            "set": self.is_set(environ),
        }


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


class Politeness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_concurrency: int = Field(4, ge=1, le=32)
    min_interval_ms: int = Field(0, ge=0, le=60_000)
    timeout_s: float = Field(30.0, gt=0, le=300)


class SourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=NAME_PATTERN)
    url: str
    flavor: Literal["prometheus", "victoriametrics", "elasticsearch", "opensearch"] = "prometheus"
    #: None: learned from the series' scrape spacing (PromQLSource.learn_resolution); a value
    #: overrides what is learned
    resolution_ms: int | None = Field(None, ge=1_000, le=3_600_000)
    auth: AuthRef | OAuthRef | None = None
    politeness: Politeness = Field(default_factory=Politeness)
    #: another registered source with downsampled data of the same series (e.g. Thanos
    #: downsample-1h) that serves long-window operating profiles (bead 2as.7). On Wikimedia this
    #: pairing currently answers from raw data (2as.25: the tier needs `max_source_resolution`,
    #: which the source does not send; raw is kept >= 300 d, exact and fast), so it is harmless and
    #: kept for sources whose raw retention is short.
    profile_source: str | None = Field(default=None, pattern=NAME_PATTERN)
    #: IANA timezone the operating profile counts hours in: human-driven load follows local time
    #: and shifts an hour across DST, which UTC buckets smear (bead 2as.24)
    timezone: str = "UTC"
    #: Elasticsearch/OpenSearch: the index pattern this source reads (one source = one pattern)
    index_pattern: str | None = None
    #: Elasticsearch/OpenSearch: the date field documents are bucketed by; no default (indices
    #: use @timestamp, timestamp, metadata.timestamp, ...: any guess is wrong somewhere)
    time_field: str | None = None

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, v: str) -> str:
        return check_timezone(v)

    @field_validator("index_pattern")
    @classmethod
    def _index_pattern(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not 1 <= len(v) <= 255:
            raise ValueError("index_pattern must be 1-255 characters")
        if v != v.lower():
            raise ValueError("index_pattern must be lowercase (index names are)")
        if _INDEX_FORBIDDEN.search(v):
            raise ValueError('index_pattern must not contain whitespace or / \\ " < > | #')
        if any(part in ("*", "_all") for part in v.split(",")):
            raise ValueError(
                "index_pattern '*' / '_all' includes system indices: name the pattern, "
                "e.g. access-logs-*"
            )
        return v

    @field_validator("time_field")
    @classmethod
    def _time_field(cls, v: str | None) -> str | None:
        if v is not None and (not v or re.search(r"\s", v)):
            raise ValueError("time_field must be a field path like @timestamp")
        return v

    @model_validator(mode="after")
    def _profile_source_is_another(self) -> SourceSpec:
        if self.profile_source == self.name:
            raise ValueError("profile_source must name another source, not the source itself")
        return self

    @model_validator(mode="after")
    def _flavor_fields(self) -> SourceSpec:
        if self.flavor in ES_FLAVORS:
            missing = [f for f in ("index_pattern", "time_field") if getattr(self, f) is None]
            if missing:
                raise ValueError(
                    f"flavor {self.flavor} needs {' and '.join(missing)} (e.g. "
                    "index_pattern='access-logs-*', time_field='@timestamp'; there is no default "
                    "time field)"
                )
            if self.profile_source is not None:
                raise ValueError(
                    "profile_source is PromQL-only: operating profiles are not available on "
                    "Elasticsearch/OpenSearch sources"
                )
        elif self.index_pattern is not None or self.time_field is not None:
            raise ValueError(
                "index_pattern and time_field are for flavor elasticsearch/opensearch only"
            )
        return self

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("url must look like http(s)://host[:port][/path]")
        if parts.username or parts.password:
            raise ValueError(
                "credentials in the url are not allowed; put the secret in a file or env var "
                "and reference it with auth"
            )
        if parts.query or parts.fragment:
            raise ValueError("url must not contain a query string or fragment")
        return v.rstrip("/")

    def public(self, environ: Mapping[str, str] = os.environ) -> dict:
        out = self.model_dump()
        if isinstance(self.auth, OAuthRef):
            out["auth"] = self.auth.public()
        elif isinstance(self.auth, AuthRef):
            out["auth"] = self.auth.public(environ)
        else:
            out["auth"] = None
        return out
