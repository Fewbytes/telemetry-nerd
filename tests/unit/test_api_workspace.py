import pytest
from starlette.testclient import TestClient

from telemetry_nerd.api.app import create_app
from tests.unit.fakes import make_service

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
        "time_range": {"start_ms": 1, "end_ms": 2},
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


def test_events_replay_and_heartbeat(client):
    client.post("/api/threads", json={"text": "hi"})
    body = client.get("/api/events?since=0&limit=10").json()
    assert body["events"][0]["seq"] == 1
    assert body["last_seq"] == body["events"][-1]["seq"]
    assert client.get("/api/events?since=abc").status_code == 400
    assert client.get("/api/channel/status?consumer=claude").json() == {"channel_active": False}
    assert client.post("/api/channel/heartbeat", json={"consumer": "claude"}).json() == {"ok": True}
    assert client.get("/api/channel/status?consumer=claude").json() == {"channel_active": True}
