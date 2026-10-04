import asyncio
import threading

from telemetry_nerd.workspace.scope import ActiveWorkspace


def test_unpinned_reads_follow_the_active_id():
    a = ActiveWorkspace("w1")
    a.set_active("w2")
    assert a() == "w2"
    assert a.active == "w2"


def test_pinned_reads_survive_a_switch():
    a = ActiveWorkspace("w1")
    with a.pinned():
        a.set_active("w2")
        assert a() == "w1"
    assert a() == "w2"


def test_using_pins_an_explicit_id_and_restores():
    a = ActiveWorkspace("w1")
    with a.using("w7"):
        assert a() == "w7"
    assert a() == "w1"


async def test_tasks_inherit_the_pin():
    a = ActiveWorkspace("w1")
    with a.pinned():
        seen = asyncio.create_task(_read(a))  # copies the context at creation
    a.set_active("w2")
    assert await seen == "w1"


async def _read(a):
    await asyncio.sleep(0)
    return a()


def test_notify_reaches_subscribers_and_drops_when_full():
    a = ActiveWorkspace("w1")
    q = a.subscribe()
    a.notify({"type": "x"})
    assert q.get_nowait() == {"type": "x"}
    for _ in range(100):
        a.notify({"n": 1})
    a.notify({"n": 2})  # full: dropped, no raise
    assert q.qsize() == 100
    a.unsubscribe(q)
    a.notify({"n": 3})
    assert q.qsize() == 100


async def test_to_thread_inherits_the_pin():
    a = ActiveWorkspace("w1")
    with a.using("w4"):
        assert await asyncio.to_thread(a) == "w4"


async def test_notify_from_a_worker_thread_runs_on_the_loop():
    from tests.unit.test_event_log import _record_threads

    a = ActiveWorkspace("w1")
    q = a.subscribe()
    seen = _record_threads(q)
    await asyncio.to_thread(a.notify, {"kind": "workspace"})
    assert await asyncio.wait_for(q.get(), 1) == {"kind": "workspace"}
    assert seen == [threading.get_ident()]
