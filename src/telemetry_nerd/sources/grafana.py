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
from collections.abc import Mapping
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
    "not Prometheus/MetricsQL-compatible; only PromQL sources are supported "
    "(see telemetry-nerd-sgb for log/other adapters)"
)


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
        return f"{grafana_url.rstrip('/')}/api/datasources/proxy/uid/{self.uid}"

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


def _from_frontend_settings(body: object) -> list[GrafanaDatasource]:
    datasources = body.get("datasources") if isinstance(body, dict) else None
    if not isinstance(datasources, dict):
        raise SourceError(
            "Grafana's /api/frontend/settings response has no datasources map",
            hint="check the url points at a Grafana instance, not a login page or proxy",
        )
    out = []
    for name, raw in datasources.items():
        if isinstance(raw, dict) and (ds := _entry(raw.get("uid"), raw.get("name", name), raw)):
            out.append(ds)
    return sorted(out, key=lambda d: d.name)


def _from_datasources_api(body: object) -> list[GrafanaDatasource]:
    if not isinstance(body, list):
        raise SourceError(
            "Grafana's /api/datasources response is not a list",
            hint="check the token has at least Viewer access",
        )
    out = []
    for raw in body:
        if isinstance(raw, dict) and (ds := _entry(raw.get("uid"), raw.get("name"), raw)):
            out.append(ds)
    return sorted(out, key=lambda d: d.name)


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
    owns_client = client is None
    client = client or httpx.AsyncClient()
    try:
        if auth is not None:
            resp = await _get(client, f"{base}/api/datasources", headers)
            body = _json(resp, base)
            return _from_datasources_api(body)
        resp = await _get(client, f"{base}/api/frontend/settings", headers)
        body = _json(resp, base)
        return _from_frontend_settings(body)
    finally:
        if owns_client:
            await client.aclose()


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
    owns_client = client is None
    client = client or httpx.AsyncClient()
    try:
        resp = await client.get(
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
    finally:
        if owns_client:
            await client.aclose()
    return "prometheus", "prometheus"
