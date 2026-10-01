"""Channel consumer identity (dtk): per-session ids that keep concurrent sessions apart.

Consumers are keyed `<kind>-<session_id>` (e.g. `claude-a1b2c3`) when a session id is
known, so each session claims deliveries with its own cursor and two live sessions
never steal each other's events. Without a session id the plain kind (`claude`) is
used — the legacy single-session behavior, unchanged. `kind_of()` recovers the kind
from either form by splitting on the FIRST dash (session ids may contain dashes).
"""

from __future__ import annotations


def consumer_id(kind: str, session_id: str | None) -> str:
    """`kind-session` when a session id is known, else the plain kind (legacy)."""
    if not session_id:
        return kind
    return f"{kind}-{session_id}"


def kind_of(consumer: str) -> str:
    """The consumer's kind: text before the first dash; plain kinds pass through."""
    return consumer.split("-", 1)[0]
