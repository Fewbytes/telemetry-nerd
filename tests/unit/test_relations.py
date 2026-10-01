import json

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mcp import Client

from telemetry_nerd.catalog.models import ORIGIN_RANK
from telemetry_nerd.catalog.relations import (
    BINDING_ROLES,
    SUGGESTIONS,
    RelationClaim,
    canonical_ends,
    validate_binding,
    validate_relation,
    winner,
)
from telemetry_nerd.channel.format import describe_event
from telemetry_nerd.core.events import classify
from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.model.discovery import Discovery, MetricInfo
from telemetry_nerd.model.errors import NotFound

from .fakes import FakeSource, make_service

claims = st.builds(
    RelationClaim,
    subject=st.just("a"),
    kind=st.just("part_of"),
    object=st.just("b"),
    origin=st.sampled_from(sorted(ORIGIN_RANK)),
    confidence=st.sampled_from([0.2, 0.5, 0.9]),
    retracted=st.booleans(),
    ts_ms=st.integers(0, 4),
)


@given(st.lists(claims, min_size=1, max_size=8), st.randoms())
def test_winner_is_order_independent_and_rank_first(cs, rnd):
    shuffled = list(cs)
    rnd.shuffle(shuffled)
    assert winner(cs) == winner(shuffled)
    assert ORIGIN_RANK[winner(cs).origin] == max(ORIGIN_RANK[c.origin] for c in cs)


@given(st.lists(claims.filter(lambda c: c.origin != "user"), max_size=6), st.booleans())
def test_a_user_claim_or_retraction_always_decides(cs, retract):
    user = RelationClaim(
        subject="a",
        kind="part_of",
        object="b",
        origin="user",
        confidence=0.0,
        retracted=retract,
        ts_ms=0,
    )
    assert winner([*cs, user]).retracted is retract


def test_symmetric_kinds_canonicalize_endpoints():
    assert canonical_ends("same_quantity", "z", "a") == ("a", "z")
    assert canonical_ends("correlated", "z", "a") == ("a", "z")
    assert canonical_ends("part_of", "z", "a") == ("z", "a")


@pytest.mark.parametrize(
    ("kind", "s", "o", "params", "msg"),
    [
        ("loves", "a", "b", {}, "unknown relation kind"),
        ("part_of", "a", "a", {}, "two different"),
        ("part_of", "a", "b", {"x": 1}, "takes no params"),
        ("correlated", "a", "b", {}, "coefficient"),
        ("correlated", "a", "b", {"coefficient": 2, "lag_ms": 0, "scope": "s"}, "coefficient"),
        ("correlated", "a", "b", {"coefficient": 0.5, "lag_ms": 1.5, "scope": "s"}, "lag_ms"),
        ("correlated", "a", "b", {"coefficient": 0.5, "lag_ms": 0, "scope": " "}, "scope"),
    ],
)
def test_relation_validation(kind, s, o, params, msg):
    with pytest.raises(ValueError, match=msg):
        validate_relation(kind, s, o, params)


def test_valid_correlated_relation_passes():
    validate_relation(
        "correlated", "a", "b", {"coefficient": -0.8, "lag_ms": 30_000, "scope": "last 6h"}
    )


def test_binding_validation_and_registry_consistency():
    validate_binding("RED", {"rate": "a", "errors": None, "duration": "c"}, ["service"])
    with pytest.raises(ValueError, match="exactly the roles"):
        validate_binding("RED", {"rate": "a"}, [])
    with pytest.raises(ValueError, match="unknown binding kind"):
        validate_binding("SLO", {}, [])
    with pytest.raises(ValueError, match="join_on"):
        validate_binding("USE", dict.fromkeys(BINDING_ROLES["USE"]), ["bad-label"])
    # every role has a suggestion, so an unfilled role can always raise a Gap
    assert {(k, r) for k, rs in BINDING_ROLES.items() for r in rs} == set(SUGGESTIONS)


# service ------------------------------------------------------------------------------------
NAMES = [
    "reqs_total",
    "errs_total",
    "lat_seconds",
    "inflight",
    "node_filesystem_avail_bytes",
    "node_filesystem_size_bytes",
]


@pytest.fixture
async def svc(tmp_path):
    d = Discovery(
        tuple(MetricInfo(n, None, None, None) for n in NAMES), (), {}, None, 1.0, (), False
    )
    s = make_service(tmp_path, FakeSource(name="default", discovery=d))
    await s.learn("default")
    return s


async def test_pack_edge_is_stored_and_queryable(svc):
    got = svc.ws.catalog_relations("default", "node_filesystem_size_bytes", "bounded_by")
    (r,) = got["relations"]
    assert (r.subject, r.object, r.winner.origin) == (
        "node_filesystem_avail_bytes",
        "node_filesystem_size_bytes",
        "pack",
    )
    assert svc.ws.catalog_relations("default", "reqs_total")["relations"] == []


async def test_claude_retracts_a_pack_edge_and_history_stays(svc):
    ws = svc.ws
    ws.relate(
        "default",
        "node_filesystem_avail_bytes",
        "bounded_by",
        "node_filesystem_size_bytes",
        "claude",
        "claude",
        confidence=0.8,
        basis="not at the same labels on tmpfs",
        retract=True,
    )
    assert ws.catalog_relations("default")["relations"] == []
    (gone,) = ws.catalog_relations("default", include_retracted=True)["relations"]
    assert gone.winner.origin == "claude" and gone.contested is True
    assert {c.origin for c in gone.claims} == {"pack", "claude"}


async def test_user_restores_an_edge_claude_retracted(svc):
    ws = svc.ws
    ws.relate(
        "default",
        "node_filesystem_avail_bytes",
        "bounded_by",
        "node_filesystem_size_bytes",
        "claude",
        "claude",
        confidence=0.9,
        basis="x",
        retract=True,
    )
    ws.relate(
        "default",
        "node_filesystem_avail_bytes",
        "bounded_by",
        "node_filesystem_size_bytes",
        "user",
        "user",
    )
    (r,) = ws.catalog_relations("default")["relations"]
    assert r.winner.origin == "user" and r.contested


async def test_relate_validates_endpoints_origin_and_params(svc):
    ws = svc.ws
    with pytest.raises(NotFound, match="source_learn"):
        ws.relate("default", "reqs_total", "part_of", "ghost", "claude", "claude", confidence=0.5)
    with pytest.raises(ValueError, match="reserved"):
        ws.relate(
            "default", "errs_total", "part_of", "reqs_total", "user", "claude", confidence=0.5
        )
    with pytest.raises(ValueError, match="confidence is required"):
        ws.relate("default", "errs_total", "part_of", "reqs_total", "claude", "claude")
    with pytest.raises(ValueError, match="unknown level"):
        ws.relate(
            "default", "a", "part_of", "b", "claude", "claude", confidence=0.5, level="galaxy"
        )  # type: ignore[arg-type]


async def test_symmetric_relation_is_one_edge_either_way(svc):
    ws = svc.ws
    ws.relate(
        "default",
        "lat_seconds",
        "same_quantity",
        "inflight",
        "claude",
        "claude",
        confidence=0.6,
        basis="b",
    )
    ws.relate(
        "default",
        "inflight",
        "same_quantity",
        "lat_seconds",
        "claude",
        "claude",
        confidence=0.7,
        basis="b",
    )
    (r,) = ws.catalog_relations("default", kind="same_quantity")["relations"]
    assert (r.subject, r.object) == ("inflight", "lat_seconds") and r.winner.confidence == 0.7


async def test_workspace_level_relates_datasets(svc):
    d1 = (await svc.query("reqs_total", start="now-2h", end="now-1h"))["dataset"]
    d2 = (await svc.query("errs_total", start="now-2h", end="now-1h"))["dataset"]
    ws = svc.ws
    ws.relate(
        "",
        d2,
        "part_of",
        d1,
        "claude",
        "claude",
        confidence=0.8,
        basis="errors are a subset of requests",
        level="workspace",
    )
    (r,) = ws.catalog_relations("", level="workspace")["relations"]
    assert (r.subject, r.object) == (d2, d1)
    assert ws.catalog_relations("default")["relations"] != [r]  # not visible at catalog level
    with pytest.raises(NotFound, match="dataset"):
        ws.relate("", "d99", "part_of", d1, "claude", "claude", confidence=0.8, level="workspace")


def gaps(ws):
    return [g for g in ws.objects.list_gaps()]


async def test_unfilled_role_raises_a_gap_once(svc):
    ws = svc.ws
    out = ws.bind(
        "default",
        "littles_law",
        "checkout",
        {"arrival_rate": "reqs_total", "latency": "lat_seconds", "concurrency": None},
        "claude",
        "claude",
        join_on=["service"],
        confidence=0.8,
        basis="b",
    )
    assert len(out["gaps"]) == 1
    (g,) = gaps(ws)
    assert g.id == out["gaps"][0]
    assert g.suggestion.name == "checkout_active_requests" and g.suggestion.type == "gauge"
    assert "concurrency" in g.missing_signal and "littles_law" in g.missing_signal
    again = ws.bind(
        "default",
        "littles_law",
        "checkout",
        {"arrival_rate": "reqs_total", "latency": "lat_seconds", "concurrency": None},
        "claude",
        "claude",
        confidence=0.9,
        basis="b2",
    )
    assert again["gaps"] == [] and len(gaps(ws)) == 1  # idempotent


async def test_filled_binding_raises_no_gap_and_keeps_old_gap_when_filled_later(svc):
    ws = svc.ws
    full = {"arrival_rate": "reqs_total", "latency": "lat_seconds", "concurrency": "inflight"}
    assert (
        ws.bind(
            "default", "littles_law", "api", full, "claude", "claude", confidence=0.8, basis="b"
        )["gaps"]
        == []
    )
    ws.bind(
        "default",
        "USE",
        "disk",
        {"utilization": "node_filesystem_avail_bytes", "saturation": None, "errors": None},
        "claude",
        "claude",
        confidence=0.6,
        basis="b",
    )
    assert len(gaps(ws)) == 2
    ws.bind(
        "default",
        "USE",
        "disk",
        {"utilization": "node_filesystem_avail_bytes", "saturation": "inflight", "errors": None},
        "claude",
        "claude",
        confidence=0.7,
        basis="b",
    )
    assert len(gaps(ws)) == 2  # gaps are never deleted


async def test_retracted_binding_raises_no_gaps(svc):
    out = svc.ws.bind(
        "default",
        "RED",
        "svc",
        {"rate": None, "errors": None, "duration": None},
        "claude",
        "claude",
        confidence=0.5,
        basis="b",
        retract=True,
    )
    assert out["gaps"] == [] and gaps(svc.ws) == []


async def test_user_binding_beats_claude_and_claude_is_told(svc):
    ws = svc.ws
    ws.bind(
        "default",
        "RED",
        "api",
        {"rate": "reqs_total", "errors": "errs_total", "duration": "lat_seconds"},
        "user",
        "user",
    )
    res = ws.bind_claude(
        "default",
        "RED",
        "api",
        {"rate": "inflight", "errors": None, "duration": None},
        confidence=0.9,
        basis="b",
    )
    assert res["effective"] is False and res["outranked_by"] == "user"
    (b,) = ws.catalog_relations("default")["bindings"]
    assert b.winner.roles["rate"] == "reqs_total" and b.contested


async def test_bind_rejects_unknown_metric_and_bad_roles(svc):
    with pytest.raises(NotFound):
        svc.ws.bind(
            "default",
            "RED",
            "x",
            {"rate": "ghost", "errors": None, "duration": None},
            "claude",
            "claude",
            confidence=0.5,
        )
    with pytest.raises(ValueError, match="exactly the roles"):
        svc.ws.bind("default", "RED", "x", {"rate": None}, "claude", "claude", confidence=0.5)
    with pytest.raises(ValueError, match="key"):
        svc.ws.bind(
            "default",
            "RED",
            " ",
            dict.fromkeys(BINDING_ROLES["RED"]),
            "claude",
            "claude",
            confidence=0.5,
        )


async def test_claude_relation_discipline(svc):
    ws = svc.ws
    ok = {
        "subject": "errs_total",
        "kind": "part_of",
        "object": "reqs_total",
        "confidence": 0.9,
        "basis": "errors counted among requests",
    }
    cases = [
        {**ok, "confidence": 1.0},
        {**ok, "basis": ""},
        {**ok, "object": "ghost"},
        {
            **ok,
            "kind": "correlated",
            "confidence": 0.8,
            "params": {"coefficient": 0.9, "lag_ms": 0, "scope": "6h"},
        },
        ok,
    ]
    res = ws.relate_claude("default", cases)
    assert [r["status"] for r in res] == ["rejected"] * 4 + ["accepted"]
    assert "0.7" in res[3]["reason"]  # correlated is evidence: lower cap
    assert res[4]["effective"] is True
    with pytest.raises(ValueError, match="at most 200"):
        ws.relate_claude("default", [{}] * 201)


async def test_events_and_classes(svc):
    ws = svc.ws
    ws.relate("default", "errs_total", "part_of", "reqs_total", "user", "user")
    ws.bind(
        "default",
        "RED",
        "api",
        {"rate": "reqs_total", "errors": None, "duration": None},
        "user",
        "user",
    )
    evs = {e.type: e for e in ws.log.since(0) if e.type in ("relation.claimed", "binding.claimed")}
    assert all(e.klass == "intentional" for e in evs.values())
    assert describe_event(evs["relation.claimed"]) == "user asserted errs_total part_of reqs_total"
    assert (
        describe_event(evs["binding.claimed"])
        == "user set RED binding api: rate=reqs_total, errors=?, duration=?"
    )
    assert classify("claude", "relation.claimed") == "internal"


# MCP --------------------------------------------------------------------------------------
async def mcall(mcp, name, args):
    async with Client(mcp) as client:
        res = await client.call_tool(name, args)
    return res, (res.content[0].text if res.is_error else json.loads(res.content[0].text))


async def test_mcp_relate_bind_and_query(svc):
    mcp = build_mcp(svc, "http://x")
    _, out = await mcall(
        mcp,
        "catalog_relate",
        {
            "source": "default",
            "claims": [
                {
                    "subject": "errs_total",
                    "kind": "part_of",
                    "object": "reqs_total",
                    "confidence": 0.8,
                    "basis": "errors subset",
                },
                {
                    "subject": "errs_total",
                    "kind": "part_of",
                    "object": "ghost",
                    "confidence": 0.8,
                    "basis": "x",
                },
            ],
        },
    )
    assert [r["status"] for r in out["results"]] == ["accepted", "rejected"]

    _, bound = await mcall(
        mcp,
        "catalog_bind",
        {
            "source": "default",
            "kind": "littles_law",
            "key": "checkout",
            "roles": {"arrival_rate": "reqs_total", "latency": "lat_seconds", "concurrency": None},
            "confidence": 0.8,
            "basis": "request counter and histogram exist; no in-flight gauge",
            "join_on": ["service"],
        },
    )
    assert bound["effective"] is True and len(bound["gaps"]) == 1

    _, q = await mcall(mcp, "catalog_relations", {"source": "default", "metric": "reqs_total"})
    assert [(r["subject"], r["kind"], r["object"]) for r in q["relations"]] == [
        ("errs_total", "part_of", "reqs_total")
    ]
    assert (
        q["bindings"][0]["key"] == "checkout" and q["bindings"][0]["roles"]["concurrency"] is None
    )

    _, got = await mcall(mcp, "catalog_get", {"source": "default", "metric": "reqs_total"})
    assert got["relations"] and got["bindings"]

    res, err = await mcall(
        mcp,
        "catalog_bind",
        {
            "source": "default",
            "kind": "RED",
            "key": "k",
            "roles": {"rate": None},
            "confidence": 0.5,
            "basis": "b",
        },
    )
    assert res.is_error and "exactly the roles" in err
    res, err = await mcall(
        mcp,
        "catalog_bind",
        {
            "source": "default",
            "kind": "RED",
            "key": "k",
            "roles": dict.fromkeys(BINDING_ROLES["RED"]),
            "confidence": 1.0,
            "basis": "b",
        },
    )
    assert res.is_error and "confidence" in err
    res, _ = await mcall(mcp, "catalog_relations", {"source": "default", "level": "galaxy"})
    assert res.is_error
