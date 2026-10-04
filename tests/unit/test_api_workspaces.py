"""HTTP workspace routes and the UI socket following the active workspace (spec "HTTP")."""

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


def _annotate(client, label="x"):
    resp = client.post(
        "/api/annotations", json={"kind": "region", "t_start_ms": 5, "t_end_ms": 9, "label": label}
    )
    assert resp.status_code == 200
    return resp.json()


def _create(client, title="checkout p99", question=None):
    resp = client.post("/api/workspaces", json={"title": title, "question": question})
    assert resp.status_code == 200
    return resp.json()


def test_snapshot_names_the_active_workspace(client, service):
    snap = client.get("/api/workspace").json()
    w1 = service.active.active
    assert snap["workspace"]["id"] == w1
    assert set(snap["workspace"]) == {"id", "title", "question", "archived", "created_at_ms"}


def test_create_switches_and_open_brings_panels_back(client, service):
    w1 = service.active.active
    ann = _annotate(client)
    out = _create(client, "checkout p99", "why did p99 double?")
    w2 = out["workspace"]["id"]
    assert out["created"] is True and out["previous"] == w1 and w2 != w1
    snap = client.get("/api/workspace").json()
    assert snap["workspace"]["id"] == w2
    assert snap["workspace"]["question"] == "why did p99 double?"
    assert snap["annotations"] == [] and snap["panels"] == []

    back = client.post(f"/api/workspaces/{w1}/open", json={})
    assert back.status_code == 200 and back.json()["workspace"]["id"] == w1
    snap = client.get("/api/workspace").json()
    assert snap["workspace"]["id"] == w1
    assert [a["id"] for a in snap["annotations"]] == [ann["id"]]
    (opened,) = [e for e in service.log.since(0) if e.type == "workspace.opened"]
    assert opened.actor == "user"


def test_list_hides_archived_unless_asked(client, service):
    w1 = service.active.active
    w2 = _create(client, "two")["workspace"]["id"]
    resp = client.post(f"/api/workspaces/{w1}/update", json={"archived": True})
    assert resp.status_code == 200 and resp.json()["archived"] is True
    listed = client.get("/api/workspaces").json()
    assert listed["active"] == w2
    assert [w["id"] for w in listed["workspaces"]] == [w2]
    assert listed["more"] == 0
    everything = client.get("/api/workspaces?archived=1").json()
    assert {w["id"] for w in everything["workspaces"]} == {w1, w2}


def test_update_renames(client, service):
    w1 = service.active.active
    resp = client.post(f"/api/workspaces/{w1}/update", json={"title": "  renamed  "})
    assert resp.status_code == 200 and resp.json()["title"] == "renamed"
    assert client.get("/api/workspace").json()["workspace"]["title"] == "renamed"


def test_update_with_blank_title_is_400(client, service):
    resp = client.post(f"/api/workspaces/{service.active.active}/update", json={"title": " "})
    assert resp.status_code == 400
    assert "blank" in resp.json()["error"]


def test_archive_active_is_400(client, service):
    resp = client.post(f"/api/workspaces/{service.active.active}/update", json={"archived": True})
    assert resp.status_code == 400
    assert "switch to another" in resp.json()["error"]


def test_create_with_blank_title_is_400(client):
    assert client.post("/api/workspaces", json={"title": "  "}).status_code == 400


@pytest.mark.parametrize(
    "body",
    [{}, {"title": 3}, {"title": "t", "question": 3}],
)
def test_create_rejects_bad_bodies(client, body):
    resp = client.post("/api/workspaces", json=body)
    assert resp.status_code == 400 and resp.json()["hint"]


@pytest.mark.parametrize(
    "body",
    [{"title": 3}, {"question": ["q"]}, {"archived": "yes"}],
)
def test_update_rejects_bad_fields(client, service, body):
    resp = client.post(f"/api/workspaces/{service.active.active}/update", json=body)
    assert resp.status_code == 400 and resp.json()["hint"]


def test_unknown_workspace_is_404(client):
    assert client.post("/api/workspaces/w99/open", json={}).status_code == 404
    assert client.post("/api/workspaces/w99/update", json={"title": "t"}).status_code == 404


def test_message_to_a_thread_of_another_workspace_is_409(client, service):
    w1 = service.active.active
    thread = client.post("/api/threads", json={"text": "why?"}).json()
    _create(client, "two")
    resp = client.post(f"/api/threads/{thread['id']}/messages", json={"text": "and?"})
    assert resp.status_code == 409
    body = resp.json()
    assert w1 in body["error"]
    assert f"/api/workspaces/{w1}/open" in body["hint"]


def test_ui_socket_gets_a_workspace_frame_on_open(client, service):
    w1 = service.active.active
    w2 = _create(client, "two")["workspace"]["id"]
    with client.websocket_connect("/ws") as ui:
        assert ui.receive_json()["kind"] == "presence"
        client.post(f"/api/workspaces/{w1}/open", json={})
        frames = [ui.receive_json(), ui.receive_json()]
        control = [f for f in frames if f.get("kind") == "workspace"]
        assert len(control) == 1 and control[0]["active"]["id"] == w1
        assert "seq" not in control[0]
        (opened,) = [f for f in frames if f.get("type") == "workspace.opened"]
        assert opened["workspace"] == w1
        client.post(f"/api/workspaces/{w2}/update", json={"title": "renamed"})
        # the update is logged in w2 (inactive): only the control frame reaches the UI
        frame = ui.receive_json()
        assert frame["kind"] == "workspace" and frame["active"]["id"] == w1


def test_ui_socket_forwards_only_active_workspace_events(client, service):
    w1 = service.active.active
    w2 = _create(client, "two")["workspace"]["id"]

    def append_in(wid, label):
        with service.active.using(wid):
            service.log.append("claude", "test.event", None, {"label": label})

    with client.websocket_connect("/ws") as ui:
        assert ui.receive_json()["kind"] == "presence"
        client.portal.call(append_in, w1, "inactive")
        client.portal.call(append_in, w2, "active")
        frame = ui.receive_json()
        assert frame["payload"] == {"label": "active"} and frame["workspace"] == w2


def test_ui_socket_replays_since_through_the_active_workspace(client, service):
    _annotate(client, "old")
    w2 = _create(client, "two")["workspace"]["id"]
    _annotate(client, "new")
    with client.websocket_connect("/ws?since=0") as ui:
        replay = []
        while (frame := ui.receive_json()).get("kind") != "presence":
            replay.append(frame)
    # replay may span a switch; the queued workspace control frame then makes the UI reload
    assert replay and {f["workspace"] for f in replay} == {w2}


def test_overlong_title_or_question_is_400(client, service):
    assert client.post("/api/workspaces", json={"title": "t" * 121}).status_code == 400
    resp = client.post(
        f"/api/workspaces/{service.active.active}/update", json={"question": "q" * 501}
    )
    assert resp.status_code == 400 and "500" in resp.json()["error"]


def test_empty_question_clears_it(client, service):
    w1 = service.active.active
    client.post(f"/api/workspaces/{w1}/update", json={"question": "why?"})
    resp = client.post(f"/api/workspaces/{w1}/update", json={"question": ""})
    assert resp.status_code == 200 and resp.json()["question"] is None
