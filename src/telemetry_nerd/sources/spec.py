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


class MissingSecret(SourceError):
    pass


class AuthRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    env: str | None = None
    file: str | None = None
    scheme: Literal["bearer", "basic"] = "bearer"

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


class Politeness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_concurrency: int = Field(4, ge=1, le=32)
    min_interval_ms: int = Field(0, ge=0, le=60_000)
    timeout_s: float = Field(30.0, gt=0, le=300)


class SourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=NAME_PATTERN)
    url: str
    flavor: Literal["prometheus", "victoriametrics"] = "prometheus"
    #: None: learned from the series' scrape spacing (PromQLSource.learn_resolution); a value
    #: overrides what is learned
    resolution_ms: int | None = Field(None, ge=1_000, le=3_600_000)
    auth: AuthRef | None = None
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

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, v: str) -> str:
        return check_timezone(v)

    @model_validator(mode="after")
    def _profile_source_is_another(self) -> SourceSpec:
        if self.profile_source == self.name:
            raise ValueError("profile_source must name another source, not the source itself")
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
        out["auth"] = self.auth.public(environ) if self.auth else None
        return out
