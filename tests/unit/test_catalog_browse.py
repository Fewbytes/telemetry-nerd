import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from starlette.testclient import TestClient

from telemetry_nerd.api.app import create_app
from telemetry_nerd.catalog.browse import Browse, browse
from telemetry_nerd.catalog.models import ORIGIN_RANK, Claim
from telemetry_nerd.catalog.sample_store import SampleStore
from telemetry_nerd.catalog.store import CatalogStore
from telemetry_nerd.workspace.db import open_workspace_db

from .fakes import FakeSource, make_service


@pytest.fixture
def con(tmp_path):
    return open_workspace_db(tmp_path / "w.db")


@pytest.fixture
def cat(con):
    return CatalogStore(con)


def put(cat, metric, field, value, origin, conf=0.7, ts=1, source="vm"):
    cat.put_claim(
        source, metric, Claim(field=field, value=value, origin=origin, confidence=conf, ts_ms=ts)
    )


def names(con, **kw):
    total, page, _ = browse(con, "vm", Browse(**kw))
    return total, page


@pytest.fixture
def filled(cat):
    cat.relearn("vm", ["a_one", "axone", "b_two", "c_three", "d_four"], 1)
    put(cat, "a_one", "unit", "s", "rule", 0.7)
    put(cat, "a_one", "unit", "ms", "claude", 0.8)  # claude wins, rule loses (and they disagree)
    put(cat, "a_one", "role", "latency", "claude", 0.8)
    put(cat, "axone", "unit", "B", "rule", 0.3)  # weak winner
    put(cat, "b_two", "role", "capacity", "pack", 0.85)
    put(cat, "b_two", "unit", "B", "pack", 0.85)
    put(cat, "b_two", "description", "free bytes", "pack", 0.85)
    put(cat, "c_three", "unit", "s", "metadata", 0.9)
    put(cat, "c_three", "description", "queue wait", "metadata", 0.9)
    put(
        cat, "c_three", "description", "how long it waits", "pack", 0.85
    )  # prose differs: no conflict
    return cat


def test_default_listing_is_alphabetical_and_counts_everything(con, filled):
    total, page, summary = browse(con, "vm", Browse())
    assert total == 5 and page == ["a_one", "axone", "b_two", "c_three", "d_four"]
    assert summary == {
        "metrics": 5,
        "reviewed": 1,
        "conflicts": 1,
        "findings": 0,
        "families": 0,
        "family_members": 0,
    }  # b_two reviewed; a_one conflicts


def test_search_matches_name_or_description_and_wildcards_are_literal(con, filled):
    assert names(con, q="a_one")[1] == ["a_one"]  # "_" is not a wildcard
    assert names(con, q="free")[1] == ["b_two"]  # description
    assert names(con, q="WAITS")[1] == ["c_three"]  # case-insensitive
    assert names(con, q="100%")[1] == []
    assert names(con, prefix="a")[1] == ["a_one", "axone"] and names(con, prefix="a_")[1] == [
        "a_one"
    ]


def test_origin_filter_is_about_the_winning_claim(con, filled):
    assert names(con, origin="claude")[1] == ["a_one"]
    assert names(con, origin="rule")[1] == ["axone"]  # a_one has a rule claim, but it loses
    assert names(con, origin="pack")[1] == ["b_two"]
    assert names(con, origin="user")[1] == []


def test_confidence_filter_is_about_the_winner_not_a_losing_claim(con, filled):
    assert names(con, max_confidence=0.5)[1] == ["axone"]
    assert names(con, max_confidence=0.75)[1] == ["axone"]  # a_one's loser (0.7) does not count
    assert names(con, max_confidence=0.95)[1] == ["a_one", "axone", "b_two", "c_three"]


def test_conflicts_ignore_prose(con, filled):
    assert names(con, conflicts=True)[1] == ["a_one"]
    put(filled, "c_three", "unit", "ms", "claude", 0.7)
    assert names(con, conflicts=True)[1] == ["a_one", "c_three"]


def test_findings_filter(con, filled):
    SampleStore(con).set_finding("vm", "axone", "gauge_grows", "f1")
    assert names(con, findings=True)[1] == ["axone"]
    assert browse(con, "vm", Browse())[2]["findings"] == 1


def test_reviewed_means_interpreted_and_undisputed(con, filled):
    assert names(con, reviewed=True)[1] == [
        "b_two"
    ]  # a_one's role is Claude's but its unit conflicts
    assert names(con, reviewed=False)[1] == ["a_one", "axone", "c_three", "d_four"]
    put(filled, "d_four", "role", "state", "user", 1.0)
    assert names(con, reviewed=True)[1] == ["b_two", "d_four"]


def test_removed_metrics_are_hidden_unless_asked(con, filled):
    filled.relearn("vm", ["a_one", "axone", "b_two", "c_three"], 2)
    assert names(con)[1] == ["a_one", "axone", "b_two", "c_three"]
    assert names(con, removed=True)[1][-1] == "d_four"
    assert browse(con, "vm", Browse())[2]["metrics"] == 4


def test_sorts(con, filled):
    assert names(con, sort="weakest")[1][:2] == ["d_four", "axone"]  # unclaimed first, then 0.3
    put(filled, "c_three", "unit", "ms", "claude", 0.7)
    put(filled, "c_three", "role", "latency", "claude", 0.7)
    put(filled, "c_three", "role", "state", "pack", 0.7)
    assert names(con, sort="conflicts")[1][0] == "c_three"  # two disagreeing fields: unit and role


def test_paging(con, cat):
    cat.relearn("vm", [f"m{i:03d}" for i in range(250)], 1)
    total, p1 = names(con, limit=100)
    assert total == 250 and len(p1) == 100 and p1[0] == "m000"
    assert names(con, limit=100, offset=200)[1] == [f"m{i:03d}" for i in range(200, 250)]
    assert names(con, offset=999)[1] == [] and names(con, offset=999)[0] == 250
    assert len(names(con, limit=10_000)[1]) == 100  # clamped
    assert len(names(con, limit=0)[1]) == 1  # at least one


def test_sources_are_separate_and_bad_arguments_are_refused(con, cat):
    cat.relearn("vm", ["x"], 1)
    cat.relearn("other", ["y"], 1)
    assert browse(con, "other", Browse())[1] == ["y"]
    assert browse(con, "nope", Browse()) == (
        0,
        [],
        {
            "metrics": 0,
            "reviewed": 0,
            "conflicts": 0,
            "findings": 0,
            "families": 0,
            "family_members": 0,
        },
    )
    with pytest.raises(ValueError, match="unknown sort"):
        browse(con, "vm", Browse(sort="chaos"))
    with pytest.raises(ValueError, match="unknown origin"):
        browse(con, "vm", Browse(origin="oracle"))


claims = st.lists(
    st.tuples(
        st.sampled_from(["m1", "m2", "m3"]),
        st.sampled_from(["unit", "role", "type"]),
        st.sampled_from(sorted(ORIGIN_RANK)),
        st.sampled_from([0.1, 0.5, 0.9]),
        st.integers(0, 3),
        st.sampled_from(["a", "b", "c"]),
    ),
    min_size=1,
    max_size=14,
)


@given(claims)
@settings(max_examples=40, deadline=None)
def test_sql_winners_agree_with_the_python_resolver(tmp_path_factory, rows):
    con = open_workspace_db(tmp_path_factory.mktemp("b") / "w.db")
    cat = CatalogStore(con)
    cat.relearn("vm", ["m1", "m2", "m3"], 1)
    for metric, field, origin, conf, ts, value in rows:
        put(cat, metric, field, value, origin, conf, ts)
    for origin in ORIGIN_RANK:
        expect = [
            m for m in ("m1", "m2", "m3")
            if any(f.origin == origin for k, f in cat.entry("vm", m).fields.items() if k in ("type", "unit", "role", "bounds"))
        ]  # fmt: skip
        assert names_for(con, origin=origin) == expect, origin
    expect_conf = [m for m in ("m1", "m2", "m3") if cat.entry("vm", m).conflicts()]
    assert names_for(con, conflicts=True) == expect_conf


def names_for(con, **kw):
    return browse(con, "vm", Browse(**kw))[1]


def test_a_large_catalog_pages_without_loading_every_claim(tmp_path):
    con = open_workspace_db(tmp_path / "big.db")
    cat = CatalogStore(con)
    n = 20_000
    con.execute("BEGIN")
    con.executemany(
        "INSERT INTO catalog_metrics (source, metric, first_seen_ms, last_seen_ms, present) "
        "VALUES ('vm', ?, 1, 1, 1)",
        [(f"metric_{i:06d}",) for i in range(n)],
    )
    con.executemany(
        "INSERT INTO catalog_claims VALUES ('vm', ?, ?, ?, ?, ?, NULL, NULL, 1)",
        [
            (f"metric_{i:06d}", f, o, '"x"', c)
            for i in range(n)
            for f, o, c in (
                ("unit", "rule", 0.7),
                ("unit", "metadata", 0.9),
                ("role", "pack", 0.85),
            )
        ],
    )
    con.execute("COMMIT")
    statements: list[str] = []
    con.set_trace_callback(statements.append)
    t0 = time.process_time()  # CPU time: robust to a loaded machine (zek0.3)
    total, page, summary = browse(
        con, "vm", Browse(origin="metadata", conflicts=False, limit=50, offset=10_000)
    )
    elapsed = time.process_time() - t0
    con.set_trace_callback(None)
    assert total == n and len(page) == 50 and page[0] == "metric_010000"
    # both unit claims say "x": no disagreement; every metric has a pack role: all reviewed
    assert summary == {
        "metrics": n,
        "reviewed": n,
        "conflicts": 0,
        "findings": 0,
        "families": 0,
        "family_members": 0,
    }
    assert len(statements) <= 5  # count, page, summary: no per-metric queries
    assert elapsed < 5.0
    assert cat.has_metric("vm", "metric_000001")


# service and API ---------------------------------------------------------------------------
async def learned(tmp_path):
    from telemetry_nerd.model.discovery import Discovery, MetricInfo

    infos = (
        MetricInfo("node_filesystem_avail_bytes", "gauge", "free space"),
        MetricInfo("node_filesystem_size_bytes", "gauge", "size"),
        MetricInfo("app_odd"),
    )
    s = make_service(
        tmp_path,
        FakeSource(name="default", discovery=Discovery(infos, (), {}, None, 1.0, (), False)),
    )
    await s.learn("default")
    return s


async def test_rows_carry_provenance_findings_and_verdicts(tmp_path):
    svc = await learned(tmp_path)
    svc.ws.samples.set_finding("default", "app_odd", "gauge_grows", "f9")
    svc.ws.catalog_claim("default", "app_odd", "unit", "ms", "user", "user")
    out = svc.ws.catalog_browse("default", Browse())
    rows = {r["metric"]: r for r in out["rows"]}
    avail = rows["node_filesystem_avail_bytes"]
    assert (avail["unit"], avail["origins"]["unit"], avail["role"]) == ("B", "pack", "capacity")
    assert avail["confidences"]["unit"] == 0.85 and avail["reviewed"] is True
    odd = rows["app_odd"]
    assert odd["origins"]["unit"] == "user" and odd["findings"] == [
        {"kind": "gauge_grows", "id": "f9"}
    ]
    assert odd["reviewed"] is False and out["total"] == 3 and out["summary"]["findings"] == 1


async def test_metric_section_is_what_the_card_shows(tmp_path):
    svc = await learned(tmp_path)
    ds = (await svc.query("node_filesystem_avail_bytes", start="now-2h", end="now-1h"))["dataset"]
    pid = svc.show(ds, "q?").panel.id
    card = await svc.panel_card(pid)
    assert card["metrics"] == [svc.ws.metric_section("default", "node_filesystem_avail_bytes")]


async def test_api_lists_filters_pages_and_edits(tmp_path):
    svc = await learned(tmp_path)
    with TestClient(create_app(svc, allowed_hosts=["testserver"])) as c:
        out = c.get(
            "/api/catalog", params={"source": "default", "prefix": "node_", "limit": 1}
        ).json()
        assert (
            out["total"] == 2
            and len(out["rows"]) == 1
            and out["rows"][0]["metric"] == "node_filesystem_avail_bytes"
        )
        nxt = c.get(
            "/api/catalog", params={"source": "default", "prefix": "node_", "limit": 1, "offset": 1}
        ).json()
        assert nxt["rows"][0]["metric"] == "node_filesystem_size_bytes"
        assert [
            r["metric"]
            for r in c.get("/api/catalog", params={"source": "default", "reviewed": "no"}).json()[
                "rows"
            ]
        ] == ["app_odd"]
        detail = c.get("/api/catalog/default/node_filesystem_avail_bytes").json()
        assert detail["relations"][0]["object"] == "node_filesystem_size_bytes"
        # edit through the existing claim route; the list reflects it and the event is intentional
        c.post(
            "/api/catalog/claims",
            json={"source": "default", "metric": "app_odd", "field": "role", "value": "state"},
        )
        row = next(
            r
            for r in c.get("/api/catalog", params={"source": "default", "q": "odd"}).json()["rows"]
        )
        assert (
            row["role"] == "state" and row["origins"]["role"] == "user" and row["reviewed"] is True
        )
        ev = [e for e in svc.ws.log.since(0) if e.type == "catalog.claimed"][-1]
        assert (ev.actor, ev.klass) == ("user", "intentional")


async def test_api_rejects_bad_parameters(tmp_path):
    svc = await learned(tmp_path)
    with TestClient(create_app(svc, allowed_hosts=["testserver"])) as c:
        assert c.get("/api/catalog").status_code == 400
        for bad in (
            {"sort": "chaos"},
            {"origin": "oracle"},
            {"reviewed": "maybe"},
            {"max_confidence": "high"},
            {"limit": "x"},
        ):
            assert c.get("/api/catalog", params={"source": "default", **bad}).status_code == 400, (
                bad
            )
        assert c.get("/api/catalog", params={"source": "ghost"}).json()["total"] == 0
        assert c.get("/api/catalog/default/ghost").status_code == 404
