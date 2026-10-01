import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from telemetry_nerd.catalog.models import ORIGIN_RANK, Claim, ordered, resolve, validate_value
from telemetry_nerd.catalog.store import CatalogStore
from telemetry_nerd.channel.format import describe_event
from telemetry_nerd.core.events import classify
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.workspace.db import open_workspace_db

from .fakes import make_service

origins = st.sampled_from(sorted(ORIGIN_RANK))
claims = st.builds(
    Claim,
    field=st.just("unit"),
    value=st.sampled_from(["seconds", "bytes", "ratio", "1"]),
    origin=origins,
    confidence=st.sampled_from([0.1, 0.5, 0.9, 1.0]),
    ts_ms=st.integers(0, 5),
)


@given(st.lists(claims, min_size=1, max_size=8), st.randoms())
def test_resolve_is_order_independent_and_picks_the_top_rank(cs, rnd):
    shuffled = list(cs)
    rnd.shuffle(shuffled)
    assert resolve(cs) == resolve(shuffled)
    best = max(ORIGIN_RANK[c.origin] for c in cs)
    assert ORIGIN_RANK[resolve(cs).origin] == best


@given(st.lists(claims, max_size=6), st.sampled_from(["seconds", "bytes"]))
def test_user_claim_always_wins(cs, value):
    user = Claim(field="unit", value=value, origin="user", confidence=0.0, ts_ms=0)
    assert resolve([*cs, user]).origin == "user"


@given(st.lists(claims, min_size=1, max_size=8))
def test_within_an_origin_confidence_then_recency_decide(cs):
    win = resolve(cs)
    peers = [c for c in cs if c.origin == win.origin]
    assert win.confidence == max(c.confidence for c in peers)
    assert win.ts_ms == max(c.ts_ms for c in peers if c.confidence == win.confidence)


def test_resolve_empty_and_ordered_winner_first():
    assert resolve([]) is None
    cs = [
        Claim(field="unit", value="a", origin="rule", confidence=1, ts_ms=1),
        Claim(field="unit", value="b", origin="claude", confidence=0.2, ts_ms=0),
    ]
    assert [c.origin for c in ordered(cs)] == ["claude", "rule"]


@pytest.fixture
def store(tmp_path):
    return CatalogStore(open_workspace_db(tmp_path / "w.db"))


def claim(field="unit", value="seconds", origin="rule", confidence=0.5, ts=1, **kw):
    return Claim(field=field, value=value, origin=origin, confidence=confidence, ts_ms=ts, **kw)


def test_store_round_trips_claims_with_provenance(store):
    c = claim("histogram_family", ["lat_bucket", "lat_sum"], "pack", 0.9, citation="otel-semconv")
    store.put_claim("vm", "lat", c)
    e = store.entry("vm", "lat")
    assert e.fields["histogram_family"] == c
    assert e.present and e.first_seen_ms == 1


def test_same_origin_replaces_other_origins_survive(store):
    store.put_claim("vm", "m", claim(value="bytes", origin="rule", ts=1))
    store.put_claim("vm", "m", claim(value="seconds", origin="claude", ts=2))
    store.put_claim("vm", "m", claim(value="ratio", origin="rule", ts=3))
    e = store.entry("vm", "m")
    assert {c.origin: c.value for c in e.claims["unit"]} == {"rule": "ratio", "claude": "seconds"}
    assert e.fields["unit"].value == "seconds"
    assert list(e.conflicts()) == ["unit"]
    assert e.conflicts()["unit"][0].origin == "rule"


@given(st.lists(claims, min_size=1, max_size=10))
@settings(max_examples=40, deadline=None)
def test_store_resolution_matches_pure_resolution(tmp_path_factory, cs):
    s = CatalogStore(open_workspace_db(tmp_path_factory.mktemp("c") / "w.db"))
    final = {}
    for c in cs:
        s.put_claim("vm", "m", c)
        final[c.origin] = c
    assert s.entry("vm", "m").fields["unit"] == resolve(final.values())


def test_unknown_entry_not_found(store):
    with pytest.raises(NotFound):
        store.entry("vm", "nope")


def test_sources_are_isolated(store):
    store.put_claim("a", "m", claim(value="bytes"))
    store.put_claim("b", "m", claim(value="seconds"))
    assert store.entry("a", "m").fields["unit"].value == "bytes"
    assert [e.metric for e in store.list_entries("b")] == ["m"]


def test_relearn_diff_new_removed_returned(store):
    d1 = store.relearn("vm", ["a", "b", "c"], 10)
    assert (d1.new, d1.removed, d1.returned) == (["a", "b", "c"], [], [])
    d2 = store.relearn("vm", ["a", "c", "d"], 20)
    assert (d2.new, d2.removed, d2.returned) == (["d"], ["b"], [])
    assert [e.metric for e in store.list_entries("vm")] == ["a", "c", "d"]
    assert store.entry("vm", "b").present is False
    d3 = store.relearn("vm", ["a", "b", "c", "d"], 30)
    assert (d3.new, d3.removed, d3.returned) == ([], [], ["b"])
    assert store.entry("vm", "b").present is True
    assert store.entry("vm", "a").first_seen_ms == 10 and store.entry("vm", "a").last_seen_ms == 30


def test_removed_metric_keeps_its_claims(store):
    store.relearn("vm", ["a"], 1)
    store.put_claim("vm", "a", claim(value="bytes"))
    store.relearn("vm", [], 2)
    e = store.entry("vm", "a")
    assert not e.present and e.fields["unit"].value == "bytes"
    assert store.list_entries("vm") == []
    assert [x.metric for x in store.list_entries("vm", present_only=False)] == ["a"]


def test_partial_listing_never_marks_removed(store):
    store.relearn("vm", ["a", "b"], 1)
    d = store.relearn("vm", ["a"], 2, complete=False)
    assert d.removed == []
    assert store.entry("vm", "b").present is True


@given(st.lists(st.sets(st.sampled_from("abcdef")), min_size=1, max_size=6))
@settings(deadline=None)
def test_relearn_present_set_tracks_last_complete_listing(tmp_path_factory, listings):
    s = CatalogStore(open_workspace_db(tmp_path_factory.mktemp("r") / "w.db"))
    for t, names in enumerate(listings):
        s.relearn("vm", names, t)
    assert {e.metric for e in s.list_entries("vm")} == listings[-1]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", "gauge"),
        ("bounds", "[0,1]"),
        ("additivity_series", "additive"),
        ("unit", "seconds"),
        ("histogram_family", ["a_bucket"]),
    ],
)
def test_validate_accepts(field, value):
    assert validate_value(field, value) == value


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", "untyped"),
        ("bounds", "positive"),
        ("additivity_time", "sum"),
        ("unit", " "),
        ("histogram_family", []),
        ("histogram_family", "a_bucket"),
        ("bogus", "x"),
    ],
)
def test_validate_rejects(field, value):
    with pytest.raises(ValueError):
        validate_value(field, value)


# service ---------------------------------------------------------------
@pytest.fixture
def ws(tmp_path):
    return make_service(tmp_path).ws


def test_claim_through_service_logs_event_and_resolves(ws):
    ws.catalog_claim("vm", "m", "unit", "bytes", "rule", "system", confidence=0.4)
    ws.catalog_claim("vm", "m", "unit", "seconds", "claude", "claude", confidence=0.7)
    e = [x for x in ws.log.since(0) if x.type == "catalog.claimed"]
    assert len(e) == 2 and e[-1].klass == "internal"
    assert e[-1].payload["origin"] == "claude" and e[-1].payload["value"] == "seconds"
    assert ws.catalog_entry("vm", "m").fields["unit"].value == "seconds"


def test_user_edit_is_intentional_and_wins(ws):
    ws.catalog_claim("vm", "m", "unit", "seconds", "claude", "claude", confidence=1.0)
    ws.catalog_claim("vm", "m", "unit", "ms", "user", "user")
    last = [x for x in ws.log.since(0) if x.type == "catalog.claimed"][-1]
    assert last.klass == "intentional"
    assert describe_event(last) == 'user set unit of m on vm to "ms"'
    assert ws.catalog_entry("vm", "m").fields["unit"].value == "ms"


def test_user_origin_cannot_be_forged_or_misused(ws):
    with pytest.raises(ValueError, match="reserved"):
        ws.catalog_claim("vm", "m", "unit", "ms", "user", "claude", confidence=1.0)
    with pytest.raises(ValueError, match="reserved"):
        ws.catalog_claim("vm", "m", "unit", "ms", "claude", "user", confidence=1.0)


def test_service_validates_inputs(ws):
    with pytest.raises(ValueError, match="confidence is required"):
        ws.catalog_claim("vm", "m", "unit", "ms", "claude", "claude")
    with pytest.raises(ValueError, match="unknown origin"):
        ws.catalog_claim("vm", "m", "unit", "ms", "oracle", "claude", confidence=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid type"):
        ws.catalog_claim("vm", "m", "type", "untyped", "rule", "system", confidence=1)
    with pytest.raises(ValueError):
        ws.catalog_claim("vm", "m", "unit", "ms", "claude", "claude", confidence=2)
    assert [x for x in ws.log.since(0) if x.type == "catalog.claimed"] == []


def test_relearn_through_service_logs_counts(ws):
    ws.catalog_relearn("vm", ["a", "b"], "system")
    d = ws.catalog_relearn("vm", ["b", "c"], "system")
    assert (d.new, d.removed) == (["c"], ["a"])
    ev = [x for x in ws.log.since(0) if x.type == "catalog.relearned"][-1]
    assert ev.payload == {"source": "vm", "complete": True, "new": 1, "removed": 1, "returned": 0}
    assert ev.klass == "internal"


def test_classify_catalog_claim():
    assert classify("user", "catalog.claimed") == "intentional"
    assert classify("claude", "catalog.claimed") == "internal"
    assert classify("system", "catalog.relearned") == "internal"


def test_differently_worded_descriptions_are_not_conflicts(store):
    store.put_claim("vm", "m", claim("description", "Seconds spent.", "metadata"))
    store.put_claim("vm", "m", claim("description", "Time spent, in seconds.", "pack"))
    e = store.entry("vm", "m")
    assert len(e.claims["description"]) == 2 and e.conflicts() == {}
