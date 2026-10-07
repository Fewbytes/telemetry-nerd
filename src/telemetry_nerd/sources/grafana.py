"""Grafana front door: list datasources exposed by a Grafana URL, and the backend/flavor
detection `source_connect(grafana=..., uid=...)` uses to query one as Prometheus-compatible.

Grafana is not itself a metrics backend: it fronts Prometheus, Thanos, Mimir and
VictoriaMetrics (all through its "prometheus" datasource plugin) as well as non-PromQL
stores (Loki, InfluxDB, Elasticsearch, ...). Discovery only reports what Grafana itself
says about a datasource (its plugin type, and jsonData.prometheusType when an admin set
it) - unverified hints, not facts. The real backend is established once connected, from
the datasource's own buildinfo (see `detect_backend`), the same spike pattern the public
source registry uses (sources/public.py) except there the backend is a human-verified
annotation; here it is asked for directly because a Grafana URL comes with no such label.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict

from telemetry_nerd.sources.base import SourceError, SourceUnavailable
from telemetry_nerd.sources.promql import USER_AGENT
from telemetry_nerd.sources.spec import AuthRef

#: Grafana datasource plugin ids this project can query as PromQL/MetricsQL. Grafana's
#: built-in "prometheus" plugin is also how Thanos, Mimir and VictoriaMetrics are added.
SUPPORTED_TYPES = frozenset({"prometheus"})

Backend = Literal["prometheus", "thanos", "mimir", "victoriametrics"]
Flavor = Literal["prometheus", "victoriametrics"]

UNSUPPORTED_HINT = (
    "not Prometheus/MetricsQL-compatible, so it cannot be queried through Grafana yet; "
    'Elasticsearch/OpenSearch connect directly: source_connect(url=..., flavor="elasticsearch", '
    "index_pattern=..., time_field=...) (see telemetry-nerd-sgb for other adapters)"
)


def proxy_url(grafana_url: str, uid: str) -> str:
    """The Prometheus-compatible API base of datasource `uid`, through Grafana's proxy."""
    return f"{grafana_url.rstrip('/')}/api/datasources/proxy/uid/{uid}"


class GrafanaDatasource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    uid: str
    name: str
    type: str  # Grafana plugin id, e.g. "prometheus", "loki", "influxdb"
    is_default: bool = False
    #: Grafana's own hint at the real backend (jsonData.prometheusType: Prometheus, Cortex,
    #: Mimir, Thanos), when an admin set it; not verified until connected (detect_backend)
    backend_hint: str | None = None

    @property
    def supported(self) -> bool:
        return self.type in SUPPORTED_TYPES

    def proxy_url(self, grafana_url: str) -> str:
        return proxy_url(grafana_url, self.uid)

    def describe(self) -> dict:
        out: dict = {
            "uid": self.uid,
            "name": self.name,
            "type": self.type,
            "is_default": self.is_default,
            "supported": self.supported,
        }
        if self.backend_hint:
            out["backend_hint"] = self.backend_hint
        if not self.supported:
            out["reason"] = f"datasource type {self.type!r} is {UNSUPPORTED_HINT}"
        return out


def _entry(uid: object, name: object, raw: Mapping[str, object]) -> GrafanaDatasource | None:
    if not uid:  # Grafana's built-in pseudo-entries (--Dashboard--, --Mixed--) have none
        return None
    json_data = raw.get("jsonData")
    hint = json_data.get("prometheusType") if isinstance(json_data, dict) else None
    return GrafanaDatasource(
        uid=str(uid),
        name=str(name),
        type=str(raw.get("type", "")),
        is_default=bool(raw.get("isDefault", False)),
        backend_hint=str(hint) if hint else None,
    )


def _sorted(entries: Iterable[GrafanaDatasource | None]) -> list[GrafanaDatasource]:
    return sorted((ds for ds in entries if ds is not None), key=lambda d: d.name)


def _from_frontend_settings(body: object) -> list[GrafanaDatasource]:
    datasources = body.get("datasources") if isinstance(body, dict) else None
    if not isinstance(datasources, dict):
        raise SourceError(
            "Grafana's /api/frontend/settings response has no datasources map",
            hint="check the url points at a Grafana instance, not a login page or proxy",
        )
    return _sorted(
        _entry(raw.get("uid"), raw.get("name", name), raw)
        for name, raw in datasources.items()
        if isinstance(raw, dict)
    )


def _from_datasources_api(body: object) -> list[GrafanaDatasource]:
    if not isinstance(body, list):
        raise SourceError(
            "Grafana's /api/datasources response is not a list",
            hint="check the token has at least Viewer access",
        )
    return _sorted(
        _entry(raw.get("uid"), raw.get("name"), raw) for raw in body if isinstance(raw, dict)
    )


@asynccontextmanager
async def _client(client: httpx.AsyncClient | None) -> AsyncIterator[httpx.AsyncClient]:
    """`client` as given (the caller closes it), or a new one closed on exit."""
    if client is not None:
        yield client
        return
    owned = httpx.AsyncClient()
    try:
        yield owned
    finally:
        await owned.aclose()


async def _get(client: httpx.AsyncClient, url: str, headers: Mapping[str, str]) -> httpx.Response:
    try:
        resp = await client.get(url, headers={"User-Agent": USER_AGENT, **headers}, timeout=30)
    except httpx.HTTPError as e:
        raise SourceUnavailable(f"cannot reach {url}: {e}", hint="check the Grafana url") from e
    if resp.status_code in (401, 403):
        raise SourceError(
            f"Grafana returned HTTP {resp.status_code} from {url}",
            hint="the token lacks access, or anonymous access is disabled; pass auth_env/auth_file",
        )
    if resp.status_code != 200:
        raise SourceUnavailable(
            f"Grafana returned HTTP {resp.status_code} from {url}",
            hint="check the Grafana url and that the server is reachable",
        )
    return resp


async def discover_datasources(
    url: str,
    auth: AuthRef | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    environ: Mapping[str, str] = os.environ,
) -> list[GrafanaDatasource]:
    """Datasources Grafana at `url` (its own origin, not a datasource proxy path) exposes.

    With `auth` (a Grafana API token or service account token): /api/datasources, the
    authoritative list (every datasource, including ones hidden from anonymous users).
    Without it: /api/frontend/settings, what an anonymous visitor's own browser loads;
    what is in it depends on the instance's anonymous-access setting."""
    base = url.rstrip("/")
    headers = auth.headers(environ) if auth is not None else {}
    if auth is not None:
        path, parse = "/api/datasources", _from_datasources_api
    else:
        path, parse = "/api/frontend/settings", _from_frontend_settings
    async with _client(client) as http:
        resp = await _get(http, f"{base}{path}", headers)
        return parse(_json(resp, base))


def _json(resp: httpx.Response, base: str) -> object:
    try:
        return resp.json()
    except ValueError as e:
        raise SourceUnavailable(
            f"non-JSON response from {base}",
            hint="check the url points at a Grafana instance, not a proxy page",
        ) from e


#: application names (lowercased, substring match) a buildinfo response self-identifies
#: with. Prometheus and Thanos never set this field (VERIFIED: wikimedia-thanos fixture),
#: so they are not listed here and fall through to the shape-based check below.
_APPLICATION_BACKEND: tuple[tuple[str, Backend], ...] = (
    ("victoria", "victoriametrics"),
    ("mimir", "mimir"),
    ("cortex", "mimir"),
    ("thanos", "thanos"),
)


def detect_backend(buildinfo_data: Mapping[str, object]) -> tuple[str, Flavor]:
    """(backend, query flavor) from a /api/v1/status/buildinfo response's `data`.

    Mimir self-identifies (`application: "Grafana Mimir"`, VERIFIED on the grafana-play and
    cern-openstack fixtures). VictoriaMetrics' buildinfo is minimal - just `version`, none
    of Prometheus/Thanos's revision/branch/buildUser/goVersion (VERIFIED on vm-playground
    and percona-pmm fixtures) - which is the signal the acceptance criteria asks for.
    Anything else with the full Prometheus-shaped fields is assumed prometheus-flavor:
    Prometheus and Thanos cannot be told apart from buildinfo alone (both omit
    `application`, VERIFIED on prometheus-demo and wikimedia-thanos fixtures), which is why
    the public registry (sources/public.py) labels Thanos by hand instead of detecting it.
    """
    app = str(buildinfo_data.get("application", "")).strip().lower()
    if app:
        for needle, backend in _APPLICATION_BACKEND:
            if needle in app:
                return backend, (
                    "victoriametrics" if backend == "victoriametrics" else "prometheus"
                )
        return app, "prometheus"
    if buildinfo_data.keys() <= {"version"} and "version" in buildinfo_data:
        return "victoriametrics", "victoriametrics"
    return "prometheus", "prometheus"


async def probe_backend(
    proxy_url: str,
    auth: AuthRef | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    environ: Mapping[str, str] = os.environ,
    timeout_s: float = 30.0,
) -> tuple[str, Flavor]:
    """Ask the datasource's own buildinfo endpoint, through the Grafana proxy, which
    backend and query flavor to connect it with. Best effort: a buildinfo endpoint that is
    unreachable or hidden by the proxy falls back to prometheus/prometheus (the dialect
    that also answers Thanos and Mimir); source_connect still probes the resulting source
    for real (a trivial query) before saving it, so an actually-unreachable source still
    fails loudly there."""
    headers = auth.headers(environ) if auth is not None else {}
    async with _client(client) as http:
        try:
            resp = await http.get(
                f"{proxy_url.rstrip('/')}/api/v1/status/buildinfo",
                headers={"User-Agent": USER_AGENT, **headers},
                timeout=timeout_s,
            )
            if resp.status_code == 200:
                body = resp.json()
                data = body.get("data") if isinstance(body, dict) else None
                if isinstance(data, dict):
                    return detect_backend(data)
        except (httpx.HTTPError, ValueError):
            pass
    return "prometheus", "prometheus"
