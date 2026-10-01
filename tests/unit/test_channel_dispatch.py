import pytest

from telemetry_nerd.channel.dispatch import ChannelDispatcher
from telemetry_nerd.core.events import EventLog
from telemetry_nerd.core.presence import PresenceRegistry
from telemetry_nerd.workspace.db import open_workspace_db


@pytest.fixture
def log(tmp_path):
    return EventLog(open_workspace_db(tmp_path / "w.db"), clock=lambda: 1_000)


@pytest.fixture
def presence():
    return PresenceRegistry(clock=lambda: 2_000)


@pytest.fixture
def dispatch(log, presence):
    return ChannelDispatcher(log, presence)


def _ask(log, text="why?", thread="t1"):
    return log.append("user", "thread.message", thread, {"thread": thread, "text": text})


def _live(presence):
    conn = presence.connect("claude", "channel")
    presence.update(conn, ready=True)
    return conn


def test_delivers_only_to_live_deliverer(dispatch, log, presence):
    _ask(log)
    conn = presence.connect("claude", "channel")
    assert dispatch.next_delivery("claude", conn) is None  # not ready: session unobserved
    presence.update(conn, ready=True)
    frame = dispatch.next_delivery("claude", conn)
    assert frame is not None
    assert frame["type"] == "deliver"
    assert frame["up_to"] == 1
    assert "why?" in frame["content"]
    assert frame["meta"]["thread"] == "t1"


def test_hook_bridge_never_gets_deliveries(dispatch, log, presence):
    _ask(log)
    conn = presence.connect("claude", "hook")
    presence.update(conn, ready=True)
    assert dispatch.next_delivery("claude", conn) is None


def test_one_in_flight_and_ack_advances(dispatch, log, presence):
    conn = _live(presence)
    _ask(log, "one")
    first = dispatch.next_delivery("claude", conn)
    _ask(log, "two")
    assert dispatch.next_delivery("claude", conn) is None  # first not acked yet
    dispatch.ack("claude", conn, first["up_to"])
    assert log.cursor("claude") == 1
    second = dispatch.next_delivery("claude", conn)
    assert "two" in second["content"] and "one" not in second["content"]
    dispatch.ack("claude", conn, second["up_to"])
    assert dispatch.next_delivery("claude", conn) is None


def test_unacked_batch_redelivered_after_drop(dispatch, log, presence):
    old = _live(presence)
    _ask(log)
    sent = dispatch.next_delivery("claude", old)
    dispatch.dropped(old)
    presence.disconnect(old)
    new = _live(presence)
    again = dispatch.next_delivery("claude", new)
    assert again is not None and again["up_to"] == sent["up_to"]
    assert log.cursor("claude") == 0


def test_only_earliest_live_bridge_delivers(dispatch, log, presence):
    first = _live(presence)
    second = _live(presence)
    _ask(log)
    assert dispatch.next_delivery("claude", second) is None
    assert dispatch.next_delivery("claude", first) is not None


def test_hook_claim_refused_while_live(dispatch, log, presence):
    _ask(log)
    conn = _live(presence)
    assert dispatch.claim("claude") == {"content": None, "live": True}
    presence.disconnect(conn)
    claim = dispatch.claim("claude")
    assert "why?" in claim["content"]
    assert claim["seqs"] == [1]
    assert log.cursor("claude") == 1
    assert dispatch.claim("claude") == {"content": None}


def test_ack_and_claim_announce_cursor_change(dispatch, log, presence):
    q = presence.subscribe()
    _ask(log)
    dispatch.claim("claude")
    assert q.get_nowait() == "claude"
    conn = _live(presence)
    while not q.empty():
        q.get_nowait()
    _ask(log)
    frame = dispatch.next_delivery("claude", conn)
    dispatch.ack("claude", conn, frame["up_to"])
    assert q.get_nowait() == "claude"


def test_presence_frame_carries_cursor(dispatch, log, presence):
    _ask(log)
    dispatch.claim("claude")
    assert dispatch.presence_frame("claude") == {
        "kind": "presence",
        "status": "offline",
        "mode": None,
        "since_ms": None,
        "delivered_up_to": 1,
    }
