import asyncio

from telemetry_nerd.core.presence import PresenceRegistry


def _registry() -> PresenceRegistry:
    return PresenceRegistry(clock=lambda: 5_000)


def test_offline_without_bridges():
    reg = _registry()
    assert reg.status("claude") == "offline"
    assert not reg.live("claude")
    assert reg.snapshot("claude") == {"status": "offline", "mode": None, "since_ms": None}


def test_hook_bridge_is_terminal():
    reg = _registry()
    reg.connect("claude", "hook")
    assert reg.status("claude") == "terminal"
    assert reg.snapshot("claude") == {"status": "terminal", "mode": "hook", "since_ms": 5_000}


def test_channel_bridge_is_live_only_when_ready():
    reg = _registry()
    conn = reg.connect("claude", "channel")
    assert reg.status("claude") == "terminal"  # session not observed yet
    assert reg.deliverer("claude") is None
    reg.update(conn, ready=True)
    assert reg.status("claude") == "live"
    assert reg.live("claude")
    assert reg.deliverer("claude") == conn


def test_mode_upgrade_after_handshake():
    reg = _registry()
    conn = reg.connect("claude", "hook")
    reg.update(conn, ready=True)
    assert reg.status("claude") == "terminal"
    reg.update(conn, mode="channel")
    assert reg.status("claude") == "live"


def test_disconnect_recomputes():
    reg = _registry()
    conn = reg.connect("claude", "channel")
    reg.update(conn, ready=True)
    reg.disconnect(conn)
    assert reg.status("claude") == "offline"
    reg.disconnect(conn)  # idempotent


def test_most_live_bridge_wins_and_deliverer_is_earliest_live():
    reg = _registry()
    hook = reg.connect("claude", "hook")
    first = reg.connect("claude", "channel")
    second = reg.connect("claude", "channel")
    reg.update(second, ready=True)
    reg.update(first, ready=True)
    assert reg.snapshot("claude")["mode"] == "channel"
    assert reg.deliverer("claude") == first
    reg.disconnect(first)
    assert reg.deliverer("claude") == second
    reg.disconnect(second)
    assert reg.status("claude") == "terminal"
    assert hook in reg.connections("claude")


def test_consumers_are_independent():
    reg = _registry()
    reg.connect("a", "hook")
    assert reg.status("a") == "terminal"
    assert reg.status("b") == "offline"


async def test_subscribers_get_consumer_on_every_change():
    reg = _registry()
    q = reg.subscribe()
    conn = reg.connect("claude", "channel")
    reg.update(conn, ready=True)
    reg.changed("claude")  # e.g. the cursor moved
    reg.disconnect(conn)
    got = [await asyncio.wait_for(q.get(), 1) for _ in range(4)]
    assert got == ["claude"] * 4
    reg.unsubscribe(q)
    reg.connect("claude", "hook")
    assert q.empty()
