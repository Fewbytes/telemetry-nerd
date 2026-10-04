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


def test_describe_highlight_events():
    noted = ev(4, "object.highlighted", "p3", {"note": "why spiky?", "ttl_ms": None})
    assert describe_event(noted) == 'user highlighted p3: "why spiky?"'
    bare = ev(5, "object.highlighted", "p3", {"note": None, "ttl_ms": None})
    assert describe_event(bare) == "user highlighted p3"


def test_meta_workspace_is_last_intentional_events_workspace():
    e = Event(5, 1_005, "user", "panel.closed", "p1", "intentional", {}, workspace="w3")
    _, meta = format_channel([e], [])
    assert meta["workspace"] == "w3"


def test_mixed_batch_prefixes_other_workspace_lines():
    ask = Event(
        5, 1_005, "user", "thread.message", "t1", "intentional",
        {"thread": "t1", "text": "q?"}, workspace="w1",
    )  # fmt: skip
    opened = Event(
        6, 1_006, "user", "workspace.opened", "w3", "intentional",
        {"title": "T", "question": None, "created": False, "from": "w1"}, workspace="w3",
    )  # fmt: skip
    text, meta = format_channel([ask, opened], [])
    assert meta["workspace"] == "w3"
    lines = text.split("\n")
    assert lines[0].startswith("[w1] user asked in t1")
    assert lines[1] == 'user reopened workspace w3 "T"; previous w1'


def test_describe_workspace_opened_new_and_updated():
    new = ev(
        1, "workspace.opened", "w4",
        {"title": "checkout p99", "question": "why did p99 double at 14:00?", "created": True, "from": "w1"},
    )  # fmt: skip
    assert describe_event(new) == (
        'user opened a new workspace w4 "checkout p99" '
        '(question: "why did p99 double at 14:00?"); previous w1'
    )
    assert (
        describe_event(ev(2, "workspace.updated", "w1", {"title": "X"})) == 'user renamed w1 to "X"'
    )
    assert (
        describe_event(ev(3, "workspace.updated", "w1", {"archived": True})) == "user archived w1"
    )
    assert (
        describe_event(ev(4, "workspace.updated", "w1", {"archived": False}))
        == "user unarchived w1"
    )
    assert describe_event(ev(5, "workspace.updated", "w1", {"question": "Q?"})) == (
        'user changed the question of w1 to "Q?"'
    )


def test_workspace_updated_joins_several_changed_keys():
    e = ev(2, "workspace.updated", "w1", {"title": "X", "question": "Q?", "archived": True})
    assert describe_event(e) == (
        'user renamed w1 to "X"; changed the question of w1 to "Q?"; archived w1'
    )


def test_ambient_lines_from_another_workspace_are_prefixed():
    current = Event(5, 1_005, "user", "panel.closed", "p1", "intentional", {}, workspace="w3")
    here = Event(6, 1_006, "user", "panel.closed", "p2", "ambient", {}, workspace="w3")
    other = Event(7, 1_007, "user", "panel.closed", "p9", "ambient", {}, workspace="w1")
    content, _ = format_channel([current], [here, other])
    assert content.splitlines()[-1] == ("ambient: user closed p2; [w1] user closed p9")
