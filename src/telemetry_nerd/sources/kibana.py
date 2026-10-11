"""Kibana discovery front door: list the index patterns / data views a Kibana instance
knows about, so a human or Claude can connect the underlying Elasticsearch/OpenSearch
cluster directly with source_connect(url=..., flavor="elasticsearch", index_pattern=...,
time_field=...).

Discovery only (telemetry-nerd-sgb.2, the "Kibana discovery front door" follow-up to the
ES/OpenSearch adapter design's "No Kibana path" non-goal): this module never queries
through Kibana itself. Kibana is consulted only to learn what index patterns exist and
which time field each one is configured with; the actual queries still go straight to
Elasticsearch/OpenSearch (sources/elasticsearch.py), same as source_discover_grafana /
sources/grafana.py never queries through Grafana.

Kibana exposes two APIs for this: the saved-objects API (`/api/saved_objects/_find?
type=index-pattern`, present since Kibana 6.x, formally deprecated but still served on
current Kibana) and the newer Data Views API (`/api/data_views`, 7.11+). This module uses
the saved-objects API: its _find response inlines each pattern's `timeFieldName` attribute,
whereas the Data Views API's list endpoint omits the time field entirely (recovering it
needs a second GET /api/data_views/data_view/<id> per entry, which turns one discovery
call into N+1 requests). Since a time field is exactly what source_connect needs and
`_find` returns it for free, the older API is more useful here despite the deprecation
notice.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import httpx
from pydantic import BaseModel, ConfigDict

from telemetry_nerd.sources.base import SourceError, SourceUnavailable
from telemetry_nerd.sources.promql import USER_AGENT
from telemetry_nerd.sources.spec import AuthRef

#: Kibana saved-objects API requires this header on state-changing requests; harmless (and
#: on some versions required) on GET too, so it is sent unconditionally.
_XSRF_HEADER = {"kbn-xsrf": "true"}

_FIND_PATH = "/api/saved_objects/_find?type=index-pattern&per_page=1000"


class KibanaIndexPattern(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str  # the index pattern string this entry resolves to, e.g. "logs-*"
    #: the date field Kibana has configured for this pattern (attributes.timeFieldName);
    #: None when the saved object has none set
    time_field: str | None = None

    def describe(self) -> dict:
        out: dict = {"id": self.id, "title": self.title}
        if self.time_field:
            out["time_field"] = self.time_field
        return out


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
        resp = await client.get(
            url, headers={"User-Agent": USER_AGENT, **_XSRF_HEADER, **headers}, timeout=30
        )
    except httpx.HTTPError as e:
        raise SourceUnavailable(f"cannot reach {url}: {e}", hint="check the Kibana url") from e
    if resp.status_code in (401, 403):
        raise SourceError(
            f"Kibana returned HTTP {resp.status_code} from {url}",
            hint="the token lacks access, or anonymous access is disabled; pass auth_env/auth_file",
        )
    if resp.status_code != 200:
        raise SourceUnavailable(
            f"Kibana returned HTTP {resp.status_code} from {url}",
            hint="check the Kibana url and that the server is reachable",
        )
    return resp


def _json(resp: httpx.Response, base: str) -> object:
    try:
        return resp.json()
    except ValueError as e:
        raise SourceUnavailable(
            f"non-JSON response from {base}",
            hint="check the url points at a Kibana instance, not a proxy page",
        ) from e


def _entry(obj: object) -> KibanaIndexPattern | None:
    if not isinstance(obj, dict):
        return None
    attrs = obj.get("attributes")
    obj_id = obj.get("id")
    if not obj_id or not isinstance(attrs, dict):
        return None
    title = attrs.get("title")
    if not title:
        return None
    time_field = attrs.get("timeFieldName")
    return KibanaIndexPattern(
        id=str(obj_id),
        title=str(title),
        time_field=str(time_field) if time_field else None,
    )


def _parse(body: object, base: str) -> list[KibanaIndexPattern]:
    objects = body.get("saved_objects") if isinstance(body, dict) else None
    if not isinstance(objects, list):
        raise SourceError(
            f"Kibana's /api/saved_objects/_find response from {base} has no saved_objects list",
            hint="check the url points at a Kibana instance, not a login page or proxy",
        )
    return sorted((p for p in (_entry(o) for o in objects) if p is not None), key=lambda p: p.title)


async def discover_index_patterns(
    url: str,
    auth: AuthRef | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    environ: Mapping[str, str] = os.environ,
) -> list[KibanaIndexPattern]:
    """Index patterns Kibana at `url` (its own origin) has saved objects for, each with the
    index pattern string (`title`) and configured time field (`time_field`), when set."""
    base = url.rstrip("/")
    headers = auth.headers(environ) if auth is not None else {}
    async with _client(client) as http:
        resp = await _get(http, f"{base}{_FIND_PATH}", headers)
        return _parse(_json(resp, base), base)
