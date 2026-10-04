import asyncio

import pytest

from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.sources.spec import SourceSpec
from tests.unit.fakes import make_service

FINDING_SCOPE = {
    "source": "default",
    "selector": "up",
    "step": "1m",
    "aggregation": "avg",
    "time_range": {"start_ms": 0, "end_ms": 60_000},
}


def _events(svc, wid, type_):
    with svc.active.using(wid):
        return [e for e in svc.log.since(0) if e.type == type_]


async def test_create_switches_and_old_workspace_is_untouched(tmp_path):
    from telemetry_nerd.workspace.models import FindingIn

    svc = make_service(tmp_path)
    w1 = svc.active.active
    p = svc.ws.workspace.create_panel("q1", {}, [])
    h = svc.ws.objects.create_hypothesis("db is slow", "claude")
    f = svc.ws.objects.create_finding(
        FindingIn(claim="c", scope=FINDING_SCOPE, evidence=[{"kind": "panel", "panel": p.id}]),
        "claude",
    )
    t = svc.ws.objects.create_thread(p.id, None, "user")
    svc.ws.post_message(t.id, "why?", "user")
    svc.set_default_range("now-6h")
    before = svc.ws.snapshot()

    out = await svc.workspaces.create("checkout p99", "why?", "claude")
    assert out["created"] is True
    assert out["previous"] == w1
    assert svc.active.active == out["workspace"]["id"] != w1
    snap = svc.ws.snapshot()
    assert snap["panels"] == [] and snap["findings"] == [] and snap["hypotheses"] == []
    assert snap["threads"] == []
    assert svc.get_default_range() == "now-1h"

    back = await svc.workspaces.switch(w1, "claude")
    assert back["previous"] == out["workspace"]["id"] and "created" not in back
    after = svc.ws.snapshot()
    for key in ("panels", "findings", "hypotheses", "threads"):
        assert after[key] == before[key] != []
    assert [x["id"] for x in after["panels"]] == [p.id]
    assert [x["id"] for x in after["hypotheses"]] == [h.id]
    assert [x["id"] for x in after["findings"]] == [f.id]
    assert [x["id"] for x in after["threads"]] == [t.id]
    assert [x["id"] for x in back["open_threads"]] == [t.id]
    assert svc.get_default_range() == "now-6h"


async def test_user_switch_is_an_intentional_event_in_the_target_workspace(tmp_path):
    svc = make_service(tmp_path)
    w1 = svc.active.active
    out = await svc.workspaces.create("two", None, "user")
    w2 = out["workspace"]["id"]
    (e,) = _events(svc, w2, "workspace.opened")
    assert (e.klass, e.workspace, e.actor) == ("intentional", w2, "user")
    assert e.payload == {"title": "two", "question": None, "created": True, "from": w1}
    await svc.workspaces.switch(w1, "user")
    (e,) = _events(svc, w1, "workspace.opened")
    assert e.klass == "intentional" and e.payload["from"] == w2 and e.payload["created"] is False


async def test_claude_switch_does_not_echo(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.workspaces.create("two", None, "claude")
    (e,) = _events(svc, out["workspace"]["id"], "workspace.opened")
    assert e.klass == "internal"


async def test_switch_to_active_is_a_noop(tmp_path):
    svc = make_service(tmp_path)
    w1 = svc.active.active
    q = svc.active.subscribe()
    out = await svc.workspaces.switch(w1, "user")
    assert out["workspace"]["id"] == out["previous"] == w1
    assert _events(svc, w1, "workspace.opened") == []
    assert q.empty()


async def test_switch_unknown_is_not_found(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(NotFound):
        await svc.workspaces.switch("w99", "user")


async def test_archive_active_is_refused(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(ValueError, match="switch to another"):
        svc.workspaces.update(svc.active.active, archived=True, actor="user")
    assert not svc.registry.get(svc.active.active).archived


async def test_reopen_unarchives(tmp_path):
    svc = make_service(tmp_path)
    w1 = svc.active.active
    await svc.workspaces.create("two", None, "user")
    svc.workspaces.update(w1, archived=True, actor="user")
    assert w1 not in [w["id"] for w in svc.workspaces.list()["workspaces"]]
    assert w1 in [w["id"] for w in svc.workspaces.list(include_archived=True)["workspaces"]]
    out = await svc.workspaces.switch(w1, "user")
    assert out["workspace"]["archived"] is False
    assert svc.active.active == w1


async def test_update_logs_changed_fields_ambient(tmp_path):
    svc = make_service(tmp_path)
    w1 = svc.active.active
    svc.workspaces.update(w1, title="renamed", actor="user")
    svc.workspaces.update(w1, title="renamed", actor="user")  # nothing changed
    (e,) = _events(svc, w1, "workspace.updated")
    assert e.payload == {"title": "renamed"} and e.klass == "ambient"
    with pytest.raises(ValueError):
        svc.workspaces.update(w1, title=" ", actor="user")


async def test_list_caps_and_reports_more(tmp_path):
    svc = make_service(tmp_path)
    for i in range(3):
        await svc.workspaces.create(f"w{i}", None, "claude")
    out = svc.workspaces.list(limit=2)
    assert len(out["workspaces"]) == 2 and out["more"] == 2 and out["active"] == svc.active.active


async def _connect_vm(svc, name="vm", url="http://vm:8428"):
    await svc.source_connect(SourceSpec(name=name, url=url))


async def test_reopen_restores_a_disconnected_source(tmp_path):
    svc = make_service(tmp_path)
    w1 = svc.active.active
    w2 = (await svc.workspaces.create("two", None, "claude"))["workspace"]["id"]
    await _connect_vm(svc)
    await svc.query("up", source="vm")
    assert list(svc.registry.sources(w2)) == ["vm"]
    assert svc.registry.sources(w1) == {}
    await svc.source_disconnect("vm")
    assert "vm" not in svc.sources
    back = await svc.workspaces.switch(w1, "claude")
    assert back["sources"] == []
    out = await svc.workspaces.switch(w2, "claude")
    assert out["sources"] == [{"name": "vm", "status": "restored"}]
    assert "vm" in svc.sources
    again = await svc.workspaces.restore_sources(w2)
    assert again == [{"name": "vm", "status": "connected"}]


async def test_name_conflict_is_reported(tmp_path):
    svc = make_service(tmp_path)
    w1 = svc.active.active
    await _connect_vm(svc)
    await svc.query("up", source="vm")
    await svc.workspaces.create("two", None, "claude")
    await svc.source_disconnect("vm")
    await _connect_vm(svc, url="http://other:8428")
    (res,) = (await svc.workspaces.switch(w1, "claude"))["sources"]
    assert res["name"] == "vm" and res["status"] == "conflict" and "another name" in res["hint"]
    assert svc.sources.spec("vm").url == "http://other:8428"  # never overridden


async def test_failed_restore_is_reported_not_raised(tmp_path):
    def factory(spec):
        if spec.url.startswith("http://dead"):
            raise RuntimeError("boom")
        from tests.unit.fakes import fake_factory

        return fake_factory(spec)

    svc = make_service(tmp_path, factory=factory)
    w1 = svc.active.active
    svc.registry.note_source(w1, "dead", {"name": "dead", "url": "http://dead:1"})
    await svc.workspaces.create("two", None, "claude")
    (res,) = (await svc.workspaces.switch(w1, "claude"))["sources"]
    assert res["status"] == "failed" and "boom" in res["error"]


async def test_default_source_is_never_recorded(tmp_path):
    svc = make_service(tmp_path)
    await svc.query("up")
    assert svc.registry.sources(svc.active.active) == {}


async def test_notify_reaches_subscribers(tmp_path):
    svc = make_service(tmp_path)
    q = svc.active.subscribe()
    out = await svc.workspaces.create("two", None, "user")
    frame = q.get_nowait()
    assert frame["kind"] == "workspace" and frame["active"]["id"] == out["workspace"]["id"]
    svc.workspaces.update(out["workspace"]["id"], title="three", actor="user")
    assert q.get_nowait()["active"]["title"] == "three"


@pytest.mark.parametrize("error", [RuntimeError("restore blew up"), asyncio.CancelledError()])
async def test_switch_notifies_before_restoring_sources(tmp_path, monkeypatch, error):
    """A slow, failing or cancelled source restore must not delay or skip the UI frame."""
    svc = make_service(tmp_path)
    w1 = svc.active.active
    out = await svc.workspaces.create("two", None, "user")
    q = svc.active.subscribe()

    async def boom(wid):
        raise error

    monkeypatch.setattr(svc.workspaces, "restore_sources", boom)
    with pytest.raises(type(error)):
        await svc.workspaces.switch(w1, "user")
    assert svc.active.active == w1 != out["workspace"]["id"]
    assert q.get_nowait()["active"]["id"] == w1


async def test_known_spec_but_not_live_is_reconnected_with_replace(tmp_path, monkeypatch):
    svc = make_service(tmp_path)
    await _connect_vm(svc)
    await svc.query("up", source="vm")
    wid = svc.active.active
    svc.sources._live.pop("vm")  # spec persisted and recorded, but the source is not live
    assert svc.sources.spec("vm") is not None and "vm" not in svc.sources
    calls = []
    real = svc.workspaces._connect

    async def spy(spec, **kw):
        calls.append((spec.name, kw))
        return await real(spec, **kw)

    monkeypatch.setattr(svc.workspaces, "_connect", spy)
    assert await svc.workspaces.restore_sources(wid) == [{"name": "vm", "status": "restored"}]
    assert calls == [("vm", {"replace": True, "actor": "system"})]
    assert "vm" in svc.sources


async def test_live_name_without_a_persisted_spec_is_a_conflict(tmp_path):
    from tests.unit.fakes import FakeSource

    svc = make_service(tmp_path)
    wid = svc.active.active
    live = FakeSource()
    svc.sources.attach("settings-src", live)  # live, settings-owned: no runtime spec
    assert svc.sources.spec("settings-src") is None
    svc.registry.note_source(wid, "settings-src", {"name": "settings-src", "url": "http://x:1"})
    (res,) = await svc.workspaces.restore_sources(wid)
    assert res["name"] == "settings-src" and res["status"] == "conflict"
    assert "another name" in res["hint"]
    assert svc.sources["settings-src"] is live and svc.sources.spec("settings-src") is None


async def test_switch_to_archived_unarchives_without_an_update_event(tmp_path):
    svc = make_service(tmp_path)
    w1 = svc.active.active
    await svc.workspaces.create("two", None, "user")
    svc.workspaces.update(w1, archived=True, actor="user")
    before = len(_events(svc, w1, "workspace.updated"))
    out = await svc.workspaces.switch(w1, "user")
    assert out["workspace"]["archived"] is False and not svc.registry.get(w1).archived
    assert len(_events(svc, w1, "workspace.updated")) == before
    assert len(_events(svc, w1, "workspace.opened")) == 1
