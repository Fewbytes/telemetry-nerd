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


def test_peek_does_not_advance_until_ack(log):
    log.append("user", "panel.created", "p1")  # ambient
    log.append("user", "thread.message", "t1", {"text": "hi"})
    assert log.cursor("claude") == 0
    intentional, ambient, up_to = log.peek("claude")
    assert ([e.seq for e in intentional], [e.seq for e in ambient], up_to) == ([2], [1], 2)
    assert log.peek("claude")[2] == 2  # unacked: same batch again
    log.ack("claude", up_to)
    assert log.cursor("claude") == 2
    assert log.peek("claude") == ([], [], 2)


def test_peek_without_intentional_is_empty(log):
    log.append("user", "panel.created", "p1")
    assert log.peek("claude") == ([], [], 0)


def test_ack_is_monotonic(log):
    log.append("user", "thread.message", "t1")
    log.append("user", "thread.message", "t1")
    log.ack("claude", 2)
    log.ack("claude", 1)  # a stale ack never moves the cursor back
    assert log.cursor("claude") == 2


def test_claim_does_not_advance_past_trailing_ambient(log):
    """Ambient after the last intentional event stays pending (spec §7.2 / dql)."""
    log.append("user", "thread.message", "t1", {"text": "hi"})  # seq 1: intentional
    log.append("user", "panel.created", "p2")  # seq 2: ambient, trailing
    intentional, ambient = log.claim("claude")
    assert [e.seq for e in intentional] == [1]
    assert ambient == []
    assert log.cursor("claude") == 1
    # the trailing ambient attaches to the NEXT intentional event
    log.append("user", "thread.message", "t2", {"text": "again"})  # seq 3
    intentional, ambient = log.claim("claude")
    assert [e.seq for e in intentional] == [3]
    assert [e.seq for e in ambient] == [2]
    assert log.cursor("claude") == 3


def test_peek_up_to_excludes_trailing_ambient(log):
    log.append("user", "thread.message", "t1", {"text": "hi"})  # seq 1: intentional
    log.append("user", "panel.created", "p2")  # seq 2: ambient, trailing
    intentional, ambient, up_to = log.peek("claude")
    assert ([e.seq for e in intentional], [e.seq for e in ambient], up_to) == ([1], [], 1)
    log.ack("claude", up_to)
    assert log.cursor("claude") == 1
    # still pending: the trailing ambient was not consumed by the ack
    assert log.peek("claude") == ([], [], 1)


def test_seed_consumer_inherits_kind_max_cursor(log):
    """First-seen per-session consumers start where the kind left off (dtk)."""
    log.append("user", "thread.message", "t1", {"text": "hi"})
    log.claim("claude")  # legacy consumer consumes seq 1, cursor → 1
    log.append("user", "thread.message", "t2", {"text": "again"})  # seq 2
    log.seed_consumer("claude-s1", "claude")
    assert log.cursor("claude-s1") == 1
    intentional, _ = log.claim("claude-s1")
    assert [e.seq for e in intentional] == [2]  # only NEW events, no backlog flood
    assert log.cursor("claude") == 1  # legacy row untouched


def test_seed_consumer_is_idempotent(log):
    log.append("user", "thread.message", "t1", {"text": "hi"})
    log.claim("claude-s1")  # claims seq 1, cursor → 1
    log.seed_consumer("claude-s1", "claude")
    assert log.cursor("claude-s1") == 1  # existing row never regresses


def test_seed_consumer_across_peer_sessions(log):
    log.append("user", "thread.message", "t1", {"text": "hi"})
    log.claim("claude-s1")  # cursor → 1
    log.append("user", "thread.message", "t2", {"text": "x"})  # seq 2
    log.claim("claude-s2")  # cursor → 2
    log.append("user", "thread.message", "t3", {"text": "y"})  # seq 3
    log.seed_consumer("claude-s3", "claude")
    assert log.cursor("claude-s3") == 2  # max across kind peers, not just legacy row


def test_classify_user_highlight_depends_on_note():
    assert classify("user", "object.highlighted", {"note": "why?"}) == "intentional"
    assert classify("user", "object.highlighted", {"note": None}) == "ambient"
    assert classify("user", "object.highlighted", {"note": "  "}) == "ambient"
    assert classify("user", "object.highlighted") == "ambient"
    assert classify("claude", "object.highlighted", {"note": "x"}) == "internal"
    assert classify("user", "object.unhighlighted") == "internal"
