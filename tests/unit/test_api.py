import json
import time

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from telemetry_nerd.api.app import create_app
from tests.unit.fakes import FakeSource, NonFiniteSource, make_service


def _app(service, **kw):
    """TestClient's Host is `testserver`; it is not an allowed host by default."""
    kw.setdefault("allowed_hosts", ["testserver", "127.0.0.1", "localhost", "[::1]"])
    return create_app(service, **kw)


@pytest.fixture
def client(tmp_path):
    with TestClient(_app(make_service(tmp_path))) as c:
        yield c


JSON = {"content-type": "application/json"}


def make_panel(client, question="Is it stable?"):
    ds = client.post("/api/query", json={"expr": "up", "start": "now-2h", "end": "now-1h"}).json()
    return client.post("/api/show", json={"dataset": ds["dataset"], "question": question})


def test_query_show_list_data(client):
    resp = make_panel(client)
    assert resp.status_code == 200
    panel = resp.json()["panel"]
    assert [p["id"] for p in client.get("/api/panels").json()] == [panel["id"]]
    data = client.get(f"/api/panels/{panel['id']}/data?width=200").json()
    assert len(data["series"]) == 2


def test_show_blank_question_is_400(client):
    assert make_panel(client, question="").status_code == 400


def test_show_unknown_dataset_is_404(client):
    assert client.post("/api/show", json={"dataset": "nope", "question": "q?"}).status_code == 404


def test_unknown_panel_is_404(client):
    assert client.get("/api/panels/p99/data").status_code == 404


def test_unknown_source_is_400_with_hint(client):
    resp = client.post("/api/query", json={"expr": "up", "source": "nope"})
    assert resp.status_code == 400
    assert resp.json()["hint"]


def test_rejected_chart_is_422(tmp_path):
    with TestClient(_app(make_service(tmp_path, FakeSource(n_series=9)))) as c:
        resp = make_panel(c)
        assert resp.status_code == 422
        assert resp.json()["issues"][0]["rule"] == "series_budget"


def test_render_report_budget(client):
    ok = client.post(
        "/api/render-report",
        json={"panel_id": "p1", "render_ms": 20, "points": 400, "width_px": 400},
    )
    assert ok.json() == {"budget_exceeded": False}
    slow = client.post(
        "/api/render-report",
        json={"panel_id": "p1", "render_ms": 250, "points": 400, "width_px": 400},
    )
    assert slow.json() == {"budget_exceeded": True}
    dense = client.post(
        "/api/render-report",
        json={"panel_id": "p1", "render_ms": 5, "points": 5000, "width_px": 400},
    )
    assert dense.json() == {"budget_exceeded": True}


def test_websocket_receives_panel_created(client):
    with client.websocket_connect("/ws") as ws:
        make_panel(client)
        seen = []
        for _ in range(5):
            event = ws.receive_json()
            seen.append(event)
            if event.get("type") == "panel.created":
                break
    assert any(e.get("type") == "panel.created" and e["object_id"] == "p1" for e in seen)


def test_websocket_disconnect_unsubscribes(tmp_path):
    service = make_service(tmp_path)
    with TestClient(_app(service)) as c:
        with c.websocket_connect("/ws"):
            for _ in range(100):
                if service.log.subscriber_count == 1:
                    break
                time.sleep(0.01)
            assert service.log.subscriber_count == 1
        for _ in range(200):
            if service.log.subscriber_count == 0:
                break
            time.sleep(0.01)
        assert service.log.subscriber_count == 0


@pytest.mark.parametrize("path", ["/api/query", "/api/show", "/api/render-report"])
@pytest.mark.parametrize("content", [b"{not json", b"[1, 2]", b'"str"'])
def test_bad_body_is_400_with_hint(client, path, content):
    resp = client.post(path, content=content, headers=JSON)
    assert resp.status_code == 400
    assert resp.json()["hint"]


def test_query_requires_expr(client):
    for body in ({}, {"expr": 5}):
        resp = client.post("/api/query", json=body)
        assert resp.status_code == 400
        assert resp.json()["hint"]


def test_show_requires_dataset_and_question(client):
    for body in ({}, {"dataset": "d1"}, {"question": "q?"}, {"dataset": 1, "question": "q"}):
        resp = client.post("/api/show", json=body)
        assert resp.status_code == 400
        assert resp.json()["hint"]


def test_budget_event_nests_report(tmp_path):
    service = make_service(tmp_path)
    queue = service.log.subscribe()
    with TestClient(_app(service)) as c:
        body = {"panel_id": "p1", "render_ms": 250, "points": 1, "width_px": 400, "type": "evil"}
        c.post("/api/render-report", json=body)
    ev = queue.get_nowait()
    assert (ev["type"], ev["object_id"], ev["actor"]) == ("render.budget_exceeded", "p1", "user")
    assert ev["payload"] == {"report": body}


def _strict(resp):
    def boom(c):
        raise AssertionError(f"non-standard JSON constant {c}")

    return json.loads(resp.text, parse_constant=boom)


def test_non_finite_values_still_yield_valid_json(tmp_path):
    with TestClient(_app(make_service(tmp_path, NonFiniteSource()))) as c:
        resp = c.post("/api/query", json={"expr": "up", "start": "now-2h", "end": "now-1h"})
        assert resp.status_code == 200
        _strict(resp)
        panel = c.post("/api/show", json={"dataset": resp.json()["dataset"], "question": "q?"})
        pid = panel.json()["panel"]["id"]
        data = c.get(f"/api/panels/{pid}/data?width=200")
        assert data.status_code == 200
        body = _strict(data)
        assert body["series"][0]["avg"][0] is None
        assert "non_finite" in body["caveats"]


def test_serialization_error_is_not_reported_as_400(tmp_path):
    service = make_service(tmp_path)

    async def bad_query(*a, **k):
        return {"x": object()}  # not serializable: a server bug, not a client error

    service.query = bad_query  # type: ignore[method-assign]
    with TestClient(_app(service), raise_server_exceptions=False) as c:
        assert c.post("/api/query", json={"expr": "up"}).status_code == 500


def test_default_allowed_hosts_reject_foreign_host_header(tmp_path):
    service = make_service(tmp_path)
    with TestClient(create_app(service), base_url="http://evil.example") as c:
        resp = c.get("/api/panels")
        assert resp.status_code == 400
    for host in ("127.0.0.1:7070", "localhost:7070", "[::1]:7070"):
        with TestClient(create_app(service), base_url=f"http://{host}") as c:
            assert c.get("/api/panels").status_code == 200, host


def test_dns_rebinding_host_is_rejected_even_with_custom_list(tmp_path):
    service = make_service(tmp_path)
    with TestClient(_app(service), base_url="http://rebind.evil:7070") as c:
        assert c.get("/api/panels").status_code == 400


@pytest.mark.parametrize("path", ["/api/query", "/api/show", "/api/render-report"])
@pytest.mark.parametrize("ctype", ["text/plain", "application/x-www-form-urlencoded", None])
def test_post_requires_json_content_type(client, path, ctype):
    headers = {"content-type": ctype} if ctype else {}
    resp = client.post(path, content=b'{"expr": "up"}', headers=headers)
    assert resp.status_code == 415
    assert "application/json" in resp.json()["hint"]


def test_json_content_type_with_charset_is_accepted(client):
    resp = client.post(
        "/api/query",
        content=b'{"expr": "up", "start": "now-2h", "end": "now-1h"}',
        headers={"content-type": "application/json; charset=utf-8"},
    )
    assert resp.status_code == 200


def test_websocket_rejects_foreign_origin(client):
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/ws", headers={"origin": "http://evil.example"}),
    ):
        pass
    assert exc.value.code == 1008


@pytest.mark.parametrize(
    "origin", ["http://localhost:7070", "http://127.0.0.1:7070", "http://[::1]:7070"]
)
def test_websocket_accepts_local_origin_and_no_origin(client, origin):
    with client.websocket_connect("/ws", headers={"origin": origin}):
        pass
    with client.websocket_connect("/ws"):
        pass


def test_websocket_since_replays_then_streams_live(tmp_path):
    service = make_service(tmp_path)
    service.log.append("system", "a", None, {})
    service.log.append("system", "b", None, {})
    with TestClient(_app(service)) as c, c.websocket_connect("/ws?since=1") as ws:
        assert ws.receive_json()["seq"] == 2
        assert ws.receive_json()["kind"] == "presence"  # after the replay, before live events
        service.log.append("system", "c", None, {})
        assert ws.receive_json()["seq"] == 3


def test_websocket_rejects_bad_since(tmp_path):
    service = make_service(tmp_path)
    with (
        TestClient(_app(service)) as c,
        pytest.raises(WebSocketDisconnect),
        c.websocket_connect("/ws?since=abc"),
    ):
        pass


def test_websocket_replay_has_no_gap_beyond_one_batch(tmp_path):
    service = make_service(tmp_path)
    for _ in range(2500):
        service.log.append("system", "x", None, {})
    with TestClient(_app(service)) as c, c.websocket_connect("/ws?since=0") as ws:
        seqs = [ws.receive_json()["seq"] for _ in range(2500)]
    assert seqs == list(range(1, 2501))


def test_render_report_non_string_panel_id_is_not_a_500(tmp_path):
    service = make_service(tmp_path)
    with TestClient(_app(service)) as c:
        body = {"panel_id": {"x": 1}, "render_ms": 250, "points": 1, "width_px": 400}
        assert c.post("/api/render-report", json=body).status_code == 200
    assert service.log.since(0)[-1].object_id is None


async def test_mcp_over_streamable_http(live_daemon):
    import json

    from mcp import Client

    live_daemon.service.ws.ask("hello?", "user")
    async with Client(live_daemon.mcp_url) as client:
        r = await client.call_tool("workspace_get", {})
    assert not r.is_error
    brief = json.loads(r.content[0].text)  # type: ignore[union-attr]
    assert len(brief["open_threads"]) == 1


def test_mcp_rejects_foreign_host(live_daemon):
    import httpx

    r = httpx.post(f"{live_daemon.mcp_url}", headers={"Host": "evil.example"}, json={})
    assert r.status_code in (400, 421)


def test_list_sources(client):
    r = client.get("/api/sources")
    assert r.status_code == 200
    assert [s["name"] for s in r.json()["sources"]] == ["default"]


def test_mcp_transport_security_allows_http_and_https_origins_for_each_host():
    from telemetry_nerd.api.app import mcp_transport_security

    sec = mcp_transport_security(["tn.example.com", "[::1]"])
    assert {"tn.example.com:*", "tn.example.com", "[::1]:*", "[::1]"} <= set(sec.allowed_hosts)
    for origin in (
        "http://tn.example.com",
        "https://tn.example.com",
        "https://tn.example.com:*",
        "https://[::1]",
        "http://[::1]:*",
    ):
        assert origin in sec.allowed_origins
    assert "https://evil.example" not in sec.allowed_origins


def test_query_distribution_route(client):
    r = client.post(
        "/api/query-distribution",
        json={"selector": "x_bucket", "by": ["instance"], "start": "now-2h", "end": "now-1h"},
    )
    assert r.status_code == 200 and r.json()["summary"]["representation"] == "distribution"
    bad = client.post("/api/query-distribution", json={"selector": "x_bucket", "by": "instance"})
    assert bad.status_code == 400
