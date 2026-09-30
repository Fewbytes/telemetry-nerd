import pytest
from starlette.testclient import TestClient

from telemetry_nerd.api.app import create_app
from tests.unit.fakes import FakeSource, make_service


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(make_service(tmp_path))) as c:
        yield c


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
    with TestClient(create_app(make_service(tmp_path, FakeSource(n_series=9)))) as c:
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
        events = [ws.receive_json(), ws.receive_json()]
    assert {"type": "panel.created", "panel": "p1"} in events
