from telemetry_nerd.channel.format import describe_event, format_channel
from telemetry_nerd.core.events import Event


def ev(seq, type, object_id, payload, actor="user", klass="intentional"):
    return Event(seq, 1_000 + seq, actor, type, object_id, klass, payload)


def test_describe_thread_message_with_selection():
    e = ev(
        3,
        "thread.message",
        "t1",
        {
            "thread": "t1",
            "text": "why the dip?",
            "anchor": "p3",
            "selection": {"start_ms": 0, "end_ms": 300_000},
        },
    )
    assert describe_event(e) == (
        'user asked in t1 about p3 [1970-01-01T00:00:00Z–1970-01-01T00:05:00Z]: "why the dip?"'
    )


def test_describe_verdict_and_status():
    assert describe_event(
        ev(
            4,
            "finding.verdict",
            "f2",
            {"verdict": "rejected", "comment": "wrong window", "claim": "p99 doubled"},
        )
    ) == ('user rejected f2 ("p99 doubled"): wrong window')
    assert describe_event(
        ev(
            5,
            "hypothesis.status_changed",
            "h1",
            {"from": "proposed", "to": "refuted", "note": None},
        )
    ) == ("user set h1 proposed → refuted")


def test_format_channel_meta_and_ambient_digest():
    content, meta = format_channel(
        [
            ev(
                7,
                "thread.message",
                "t9",
                {"thread": "t9", "text": "look here", "anchor": "p3", "selection": None},
            )
        ],
        [
            ev(5, "panel.created", "p4", {"question": "by pod?"}, klass="ambient"),
            ev(6, "panel.closed", "p2", {}, klass="ambient"),
        ],
    )
    assert meta == {
        "workspace": "w1",
        "event": "thread.message",
        "seqs": "7",
        "panel": "p3",
        "thread": "t9",
    }
    assert content.splitlines()[0] == 'user asked in t9 about p3: "look here"'
    assert "ambient: user opened p4 (by pod?); user closed p2" in content
    assert all(k.replace("_", "").isalnum() for k in meta)


def test_meta_panel_only_for_panel_anchor():
    _, meta = format_channel(
        [
            ev(
                1,
                "thread.message",
                "t1",
                {"thread": "t1", "text": "x", "anchor": "f2", "selection": None},
            )
        ],
        [],
    )
    assert "panel" not in meta


def test_format_channel_anchorless_chat_message_has_no_panel_meta():
    content, meta = format_channel(
        [
            ev(
                3,
                "thread.message",
                "t2",
                {"thread": "t2", "text": "overall?", "anchor": None, "selection": None},
            )
        ],
        [],
    )
    assert content.splitlines()[0] == 'user asked in t2: "overall?"'
    assert "panel" not in meta
    assert meta["thread"] == "t2"
