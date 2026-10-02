import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from telemetry_nerd.api.app import create_app
from tests.unit.fakes import NOW, make_service

HOSTS = ["testserver", "127.0.0.1", "localhost", "[::1]"]


@pytest.fixture
def service(tmp_path):
    return make_service(tmp_path)


@pytest.fixture
def client(service):
    with TestClient(create_app(service, allowed_hosts=HOSTS)) as c:
        yield c


def test_health(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["version"]
    assert body["last_seq"] == 0


def test_annotation_create_delete_and_snapshot(client):
    resp = client.post(
        "/api/annotations", json={"kind": "region", "t_start_ms": 5, "t_end_ms": 9, "label": "x"}
    )
    assert resp.status_code == 200
    ann = resp.json()
    snap = client.get("/api/workspace").json()
    assert [a["id"] for a in snap["annotations"]] == [ann["id"]]
    assert snap["last_seq"] >= 1
    assert client.post(f"/api/annotations/{ann['id']}/delete", json={}).status_code == 200
    events = client.get("/api/events?since=0").json()["events"]
    assert {e["actor"] for e in events} == {"user"}


def test_invalid_annotation_is_422_with_issues(client):
    resp = client.post("/api/annotations", json={"kind": "region", "t_start_ms": 9, "t_end_ms": 5})
    assert resp.status_code == 422
    body = resp.json()
    assert body["issues"] and body["hint"] and body["error"]


def test_unknown_key_is_422(client):
    resp = client.post("/api/annotations", json={"kind": "point", "t_ms": 1, "bogus": 1})
    assert resp.status_code == 422


def test_hypothesis_status(client, service):
    h = service.ws.hypothesis_create("cpu saturation", "claude")
    resp = client.post(f"/api/hypotheses/{h.id}/status", json={"status": "refuted", "note": "n"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "refuted"


def test_unknown_hypothesis_is_404(client):
    assert client.post("/api/hypotheses/h99/status", json={"status": "refuted"}).status_code == 404


def test_bad_status_is_422(client, service):
    h = service.ws.hypothesis_create("s", "claude")
    assert client.post(f"/api/hypotheses/{h.id}/status", json={"status": "x"}).status_code == 422


def _make_finding(client, service):
    ds = client.post("/api/query", json={"expr": "up", "start": "now-2h", "end": "now-1h"}).json()
    panel = client.post("/api/show", json={"dataset": ds["dataset"], "question": "q?"}).json()[
        "panel"
    ]
    scope = {
        "source": "default",
        "selector": "up",
        "time_range": {"start_ms": NOW - 7_200_000, "end_ms": NOW - 3_600_000},
        "step": "15s",
        "aggregation": "avg",
    }
    from telemetry_nerd.workspace.models import FindingIn

    f = service.ws.finding_create(
        FindingIn.model_validate(
            {"claim": "c", "scope": scope, "evidence": [{"kind": "panel", "panel": panel["id"]}]}
        ),
        "claude",
    )
    return f, panel


def test_finding_verdict(client, service):
    f, _ = _make_finding(client, service)
    assert (
        client.post(f"/api/findings/{f.id}/verdict", json={"verdict": "maybe"}).status_code == 422
    )
    resp = client.post(
        f"/api/findings/{f.id}/verdict", json={"verdict": "rejected", "comment": "no"}
    )
    assert resp.status_code == 200
    assert resp.json()["verdict"] == "rejected"


def test_panel_close_and_list(client, service):
    _, panel = _make_finding(client, service)
    assert [p["id"] for p in client.get("/api/panels").json()] == [panel["id"]]
    assert client.post(f"/api/panels/{panel['id']}/close", json={}).status_code == 200
    assert client.post("/api/panels/p99/close", json={}).status_code == 404


def test_thread_message_focus(client):
    resp = client.post(
        "/api/threads",
        json={"text": "why?", "selection": {"start_ms": 1, "end_ms": 5}},
    )
    assert resp.status_code == 200
    tid = resp.json()["id"]
    msg = client.post(f"/api/threads/{tid}/messages", json={"text": "more"})
    assert msg.status_code == 200
    assert msg.json()["text"] == "more"
    assert client.post("/api/threads/t99/messages", json={"text": "x"}).status_code == 404
    assert client.post(f"/api/threads/{tid}/messages", json={"text": " "}).status_code == 400
    assert client.post("/api/focus", json={"start_ms": 1, "end_ms": 5}).json() == {"ok": True}
    assert client.post("/api/focus", json={"start_ms": 5, "end_ms": 1}).status_code == 422


def test_thread_then_claim(client):
    client.post("/api/threads", json={"text": "what happened at noon?"})
    claim = client.post("/api/channel/claim", json={"consumer": "claude"}).json()
    assert "what happened at noon?" in claim["content"]
    assert claim["meta"]["thread"] == "t1"
    assert claim["seqs"]
    assert client.post("/api/channel/claim", json={"consumer": "claude"}).json() == {
        "content": None
    }


def test_events_replay(client):
    client.post("/api/threads", json={"text": "hi"})
    body = client.get("/api/events?since=0&limit=10").json()
    assert body["events"][0]["seq"] == 1
    assert body["last_seq"] == body["events"][-1]["seq"]
    assert client.get("/api/events?since=abc").status_code == 400


def _status(client) -> dict:
    return client.get("/api/channel/status?consumer=claude").json()


def test_channel_status_offline_without_bridge(client):
    assert _status(client) == {
        "channel_active": False,
        "status": "offline",
        "mode": None,
        "delivered_up_to": 0,
    }
    assert client.post("/api/channel/heartbeat", json={"consumer": "claude"}).status_code in (
        404,
        405,
    )


def test_bridge_presence_and_delivery_ack(client):
    client.post("/api/threads", json={"text": "asked before the handshake"})
    with client.websocket_connect("/ws/bridge?consumer=claude") as br:
        br.send_json({"type": "hello", "mode": "channel"})
        # Connected but not ready: terminal, and the hook may still deliver.
        assert _status(client)["status"] == "terminal"
        br.send_json({"type": "ready"})
        frame = br.receive_json()
        assert frame["type"] == "deliver"
        assert "asked before the handshake" in frame["content"]
        assert frame["meta"]["thread"] == "t1"
        status = _status(client)
        assert status["status"] == "live" and status["channel_active"]
        assert status["delivered_up_to"] == 0  # not acked yet
        # The hook is refused while a channel bridge is live.
        claim = client.post("/api/channel/claim", json={"consumer": "claude"}).json()
        assert claim == {"content": None, "live": True}
        br.send_json({"type": "ack", "up_to": frame["up_to"]})
        client.post("/api/threads/t1/messages", json={"text": "follow-up"})
        frame = br.receive_json()
        assert "follow-up" in frame["content"]
        assert _status(client)["delivered_up_to"] >= 1
    assert _status(client)["status"] == "offline"


def test_unacked_delivery_returns_to_hook_after_bridge_leaves(client):
    client.post("/api/threads", json={"text": "lost in transit?"})
    with client.websocket_connect("/ws/bridge") as br:
        br.send_json({"type": "hello", "mode": "channel"})
        br.send_json({"type": "ready"})
        assert br.receive_json()["type"] == "deliver"
    claim = client.post("/api/channel/claim", json={"consumer": "claude"}).json()
    assert "lost in transit?" in claim["content"]


def test_hook_mode_bridge_is_terminal_and_gets_nothing(client):
    with client.websocket_connect("/ws/bridge") as br:
        br.send_json({"type": "hello", "mode": "hook"})
        br.send_json({"type": "ready"})
        assert _status(client)["status"] == "terminal"
        client.post("/api/threads", json={"text": "hook delivers this"})
        claim = client.post("/api/channel/claim", json={"consumer": "claude"}).json()
        assert "hook delivers this" in claim["content"]
        # Upgrading to channel after the handshake makes it live.
        br.send_json({"type": "mode", "mode": "channel"})
        assert _status(client)["status"] == "live"


def test_bridge_rejects_frames_before_hello(client):
    with client.websocket_connect("/ws/bridge") as br:
        br.send_json({"type": "ready"})
        with pytest.raises(WebSocketDisconnect) as e:
            br.receive_json()
    assert e.value.code == 1003


def test_ui_socket_gets_presence_frames(client):
    with client.websocket_connect("/ws?since=0") as ui:
        first = ui.receive_json()
        assert first == {
            "kind": "presence",
            "status": "offline",
            "mode": None,
            "since_ms": None,
            "delivered_up_to": 0,
            "sessions": [],
        }
        with client.websocket_connect("/ws/bridge") as br:
            br.send_json({"type": "hello", "mode": "hook"})
            frame = ui.receive_json()
            assert frame["kind"] == "presence" and frame["status"] == "terminal"
            # the bridge's hello changed the session list the UI carries (dtk)
            sessions = frame["sessions"]
            assert [(s["consumer"], s["kind"], s["status"], s["mode"]) for s in sessions] == [
                ("claude", "claude", "terminal", "hook")
            ]
        frame = ui.receive_json()
        assert frame["kind"] == "presence" and frame["status"] == "offline"


def test_highlight_routes(client, service):
    _, panel = _make_finding(client, service)
    r = client.post("/api/highlights", json={"object": panel["id"], "note": "see this"})
    assert r.status_code == 200
    e = [x for x in service.ws.log.since(0) if x.type == "object.highlighted"][-1]
    assert e.actor == "user" and e.klass == "intentional"
    assert e.payload == {"note": "see this", "ttl_ms": None}
    client.post("/api/highlights", json={"object": panel["id"]})
    e = [x for x in service.ws.log.since(0) if x.type == "object.highlighted"][-1]
    assert e.klass == "ambient"
    assert client.post(f"/api/highlights/{panel['id']}/clear", json={}).status_code == 200
    assert service.ws.log.since(0)[-1].type == "object.unhighlighted"
    assert client.post("/api/highlights", json={"object": "p99"}).status_code == 404
    assert client.post("/api/highlights", json={}).status_code == 400


def _seed_panel(client):
    ds = client.post("/api/query", json={"expr": "up", "start": "now-2h", "end": "now-1h"}).json()[
        "dataset"
    ]
    return client.post("/api/show", json={"dataset": ds, "question": "Up?"}).json()["panel"]["id"]


def test_select_y_view_route(client):
    pid = _seed_panel(client)
    r = client.post(f"/api/panels/{pid}/y-view", json={"mode": "band", "lo": 1.0, "hi": 2.0})
    assert r.status_code == 200 and r.json()["spec"]["y"]["selected"]["mode"] == "band"
    assert client.post(f"/api/panels/{pid}/y-view", json={"mode": "meaningful"}).status_code == 400
    assert client.post(f"/api/panels/{pid}/y-view", json={"mode": 3}).status_code == 400
    assert client.post("/api/panels/p99/y-view", json={"mode": "zero"}).status_code == 404


def test_marginal_route(client):
    pid = _seed_panel(client)
    r = client.post(f"/api/panels/{pid}/marginal", json={"reference": "previous"})
    assert r.status_code == 200 and r.json()["spec"]["marginal"]["reference"] == "previous"
    assert (
        client.post(f"/api/panels/{pid}/marginal", json={"reference": "yesterday"}).status_code
        == 400
    )
    assert (
        client.post(f"/api/panels/{pid}/marginal", json={"reference": None}).json()["spec"][
            "marginal"
        ]
        is None
    )
    assert client.post("/api/panels/p99/marginal", json={"reference": "week"}).status_code == 404


def test_show_records_the_y_context_and_it_can_be_refreshed(client):
    ds = client.post(
        "/api/query", json={"expr": "tn_demo_latency_seconds", "start": "now-2h", "end": "now-1h"}
    ).json()["dataset"]
    shown = client.post("/api/show", json={"dataset": ds, "question": "q?"}).json()["panel"]
    ctx = shown["spec"]["y"]["context"]
    assert ctx is not None and ctx["natural_lo"] == 0.0 and ctx["bounds_origin"] == "rule"
    again = client.post(f"/api/panels/{shown['id']}/y-context", json={})
    assert again.status_code == 200 and again.json()["spec"]["y"]["context"] is not None
    assert client.post("/api/panels/p99/y-context", json={}).status_code == 404
