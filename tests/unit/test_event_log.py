import asyncio

import pytest

from telemetry_nerd.core.events import EventLog, classify
from telemetry_nerd.workspace.db import open_workspace_db


class Clock:
    def __init__(self, t=1_000):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def log(tmp_path, clock):
    return EventLog(open_workspace_db(tmp_path / "w.db"), clock=clock)


def test_classify():
    assert classify("user", "thread.message") == "intentional"
    assert classify("user", "panel.created") == "ambient"
    assert classify("claude", "thread.message") == "internal"
    assert classify("user", "render.budget_exceeded") == "internal"
    assert classify("system", "annotation.created") == "internal"


def test_append_assigns_increasing_seq_and_persists(tmp_path, clock):
    path = tmp_path / "w.db"
    log = EventLog(open_workspace_db(path), clock=clock)
    e1 = log.append("claude", "panel.created", "p1", {"question": "q?"})
    e2 = log.append("user", "thread.message", "t1", {"text": "why?"})
    assert (e1.seq, e2.seq) == (1, 2)
    assert e2.klass == "intentional"
    assert e1.to_dict() == {
        "seq": 1,
        "ts_ms": 1_000,
        "actor": "claude",
        "type": "panel.created",
        "object_id": "p1",
        "klass": "internal",
        "payload": {"question": "q?"},
    }
    reopened = EventLog(open_workspace_db(path), clock=clock)
    assert [e.seq for e in reopened.since(0)] == [1, 2]
    assert reopened.last_seq == 2


def test_since_filters_and_limits(log):
    for i in range(5):
        log.append("system", "x", None, {"i": i})
    assert [e.seq for e in log.since(3)] == [4, 5]
    assert [e.seq for e in log.since(0, limit=2)] == [1, 2]


def test_invalid_actor_rejected(log):
    with pytest.raises(ValueError):
        log.append("robot", "x")


async def test_live_subscribers_receive_dicts(log):
    q = log.subscribe()
    assert log.subscriber_count == 1
    log.append("user", "focus.changed", None, {"start_ms": 1, "end_ms": 2})
    got = await asyncio.wait_for(q.get(), 1)
    assert got["type"] == "focus.changed" and got["klass"] == "ambient"
    log.unsubscribe(q)
    assert log.subscriber_count == 0


def test_claim_waits_for_intentional_then_returns_with_ambient(log):
    log.append("user", "panel.created", "p1")  # ambient
    log.append("claude", "panel.created", "p2")  # internal
    assert log.claim("claude") == ([], [])
    log.append("user", "thread.message", "t1", {"text": "hi"})
    intentional, ambient = log.claim("claude")
    assert [e.seq for e in intentional] == [3]
    assert [e.seq for e in ambient] == [1]
    assert log.claim("claude") == ([], [])  # cursor advanced


def test_claim_cursors_are_per_consumer(log):
    log.append("user", "thread.message", "t1")
    assert len(log.claim("a")[0]) == 1
    assert len(log.claim("b")[0]) == 1


def test_heartbeat_and_channel_active(log, clock):
    assert not log.channel_active("claude")
    log.heartbeat("claude")
    assert log.channel_active("claude")
    clock.t += 61_000
    assert not log.channel_active("claude")
