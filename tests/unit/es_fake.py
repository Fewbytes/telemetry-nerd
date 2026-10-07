"""An Elasticsearch/OpenSearch cluster for one index pattern, served through httpx.MockTransport,
and hand-written responses (unit tests never touch a real backend)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx

from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.elasticsearch import ElasticsearchSource

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "elasticsearch"
URL = "http://es.test:9200"
PATTERN = "tn-access-*"
START = 1_700_000_040_000  # a multiple of 60 s
RNG = TimeRange(START, START + 240_000)  # query buckets ending START .. START + 240 s


def _agg(kind: str, **extra) -> dict:
    return {kind: {"type": kind, "searchable": True, "aggregatable": True, **extra}}


#: field -> {type: info}, as _field_caps answers for one field
CAPS: dict[str, dict] = {
    "@timestamp": _agg("date"),
    "event.duration": _agg("long", meta={"unit": ["nanos"]}),
    "http.response.status_code": _agg("long"),
    "service.name": _agg("keyword"),
    "url.path": _agg("keyword"),
    "message": {"text": {"type": "text", "searchable": True, "aggregatable": False}},
    "message.keyword": _agg("keyword"),
}


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def es_error(status: int, etype: str, reason: str, *, root: tuple[str, str] | None = None,
             caused_by: dict | None = None) -> httpx.Response:  # fmt: skip
    rtype, rreason = root or (etype, reason)
    err: dict = {"type": etype, "reason": reason,
                 "root_cause": [{"type": rtype, "reason": rreason}]}  # fmt: skip
    if caused_by is not None:
        err["caused_by"] = caused_by
    return httpx.Response(status, json={"error": err, "status": status})


def search_response(buckets: list[dict], *, timed_out: bool = False, total: int = 1,
                    failed: int = 0, failures: list | None = None) -> dict:  # fmt: skip
    shards: dict = {"total": total, "successful": total - failed, "skipped": 0, "failed": failed}
    if failures is not None:
        shards["failures"] = failures
    return {"took": 3, "timed_out": timed_out, "_shards": shards, "hits": {"hits": []},
            "aggregations": {"__tn_time": {"buckets": buckets}}}  # fmt: skip


def tb(key: int, doc_count: int, **aggs) -> dict:
    """One date_histogram bucket (key = bucket START, as Elasticsearch returns it)."""
    return {"key_as_string": str(key), "key": key, "doc_count": doc_count, **aggs}


def _respond(out: dict | httpx.Response) -> httpx.Response:
    return out if isinstance(out, httpx.Response) else httpx.Response(200, json=out)


class FakeEs:
    """`search`: a response dict, an httpx.Response, or a callable(request body) returning one.
    `root`: the `GET /` answer. `caps`: field -> {type: info}. `indices`: what PATTERN matches."""

    def __init__(
        self,
        search: dict | httpx.Response | Callable[[dict], dict | httpx.Response] | None = None,
        root: dict | httpx.Response | None = None,
        caps: dict[str, dict] | None = None,
        indices: tuple[str, ...] = ("tn-access-1",),
    ) -> None:
        self.search = search if search is not None else search_response([])
        self.root = root if root is not None else fixture("root_es8.json")
        self.caps = CAPS if caps is None else caps
        self.indices = list(indices)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/":
            return _respond(self.root)
        if path == f"/{PATTERN}/_field_caps":
            want = request.url.params["fields"]
            fields = dict(self.caps) if want == "*" else {
                k: v for k, v in self.caps.items() if k == want
            }  # fmt: skip
            return httpx.Response(200, json={"indices": self.indices, "fields": fields})
        if path == f"/{PATTERN}/_search":
            body = json.loads(request.content)
            return _respond(self.search(body) if callable(self.search) else self.search)
        return es_error(404, "index_not_found_exception", f"no such index [{path}]")

    def source(self, flavor: str = "elasticsearch", **kw) -> ElasticsearchSource:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self))
        return ElasticsearchSource("es", URL, index_pattern=PATTERN, time_field="@timestamp",
                                   flavor=flavor, client=client, **kw)  # fmt: skip

    def searches(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith("/_search")]

    def caps_requests(self) -> list[str]:
        return [
            r.url.params["fields"] for r in self.requests if r.url.path.endswith("/_field_caps")
        ]
