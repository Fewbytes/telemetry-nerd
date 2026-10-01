"""Channel delivery policy: who gets pending UI events, and when the cursor moves.

Two paths share one cursor per consumer:
- channel: the daemon pushes a `deliver` frame to the live bridge; the bridge acks after
  `notifications/claude/channel` is sent, and only the ack advances the cursor. One
  delivery is in flight per consumer; an unacked one is resent once a bridge is ready again.
- hook: `claim` peeks and acks at once (printing to the hook's stdout is the send). It is
  refused while a channel bridge is live, so the two paths never race.
"""

from __future__ import annotations

from telemetry_nerd.channel.format import format_channel
from telemetry_nerd.core.events import EventLog
from telemetry_nerd.core.presence import PresenceRegistry


class ChannelDispatcher:
    def __init__(self, log: EventLog, presence: PresenceRegistry) -> None:
        self._log = log
        self._presence = presence
        self._in_flight: dict[str, tuple[int, int]] = {}  # consumer -> (conn, up_to)

    def next_delivery(self, consumer: str, conn: int) -> dict | None:
        """The `deliver` frame for bridge `conn`, or None when it should get nothing now."""
        if self._presence.deliverer(consumer) != conn or consumer in self._in_flight:
            return None
        intentional, ambient, up_to = self._log.peek(consumer)
        if not intentional:
            return None
        content, meta = format_channel(intentional, ambient)
        self._in_flight[consumer] = (conn, up_to)
        return {"type": "deliver", "up_to": up_to, "content": content, "meta": meta}

    def ack(self, consumer: str, conn: int, up_to: int) -> None:
        if self._in_flight.get(consumer) == (conn, up_to):
            del self._in_flight[consumer]
        self._log.ack(consumer, up_to)
        self._presence.changed(consumer)

    def dropped(self, conn: int) -> None:
        """Bridge `conn` went away: its unacked delivery becomes pending again."""
        for consumer, (owner, _) in list(self._in_flight.items()):
            if owner == conn:
                del self._in_flight[consumer]

    def claim(self, consumer: str) -> dict:
        """Hook delivery: the pending batch, acked immediately; nothing while live."""
        if self._presence.live(consumer):
            return {"content": None, "live": True}
        intentional, ambient = self._log.claim(consumer)
        if not intentional:
            return {"content": None}
        self._presence.changed(consumer)
        content, meta = format_channel(intentional, ambient)
        return {"content": content, "meta": meta, "seqs": [e.seq for e in intentional]}

    def presence_frame(self, consumer: str) -> dict:
        return {
            "kind": "presence",
            **self._presence.snapshot(consumer),
            "delivered_up_to": self._log.cursor(consumer),
        }
