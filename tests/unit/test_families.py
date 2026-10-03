import json
import random
import time
from pathlib import Path

import pytest
from mcp import Client
from starlette.testclient import TestClient

from telemetry_nerd.api.app import create_app
from telemetry_nerd.catalog.browse import Browse
from telemetry_nerd.catalog.families import detect, template_of
from telemetry_nerd.catalog.families_eval import evaluate
from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.model.discovery import Discovery, MetricInfo
from telemetry_nerd.model.errors import NotFound

from .fakes import FakeSource, make_service

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "families"


def dags(n, *, state="done", kind="job_run", ids=None):
    ids = ids or [f"dag{i}_task{i % 3}" for i in range(n)]
    return [f"{kind}_{i}_{state}" for i in ids]


# algorithm ---------------------------------------------------------------------------------
def test_a_slot_between_a_fixed_prefix_and_suffix_is_one_family():
    d = detect(dags(30))
    (f,) = d.families
    assert f.template == "job_run_*_done" and (f.members, f.distinct) == (30, 30)
    assert d.assignment["job_run_dag0_task0_done"] == ("job_run_*_done", "dag0_task0")


def test_dimensions_rebuild_the_name():
    names = dags(40, ids=[f"a{i}_b_{'x_' * (i % 3)}c{i}" for i in range(40)])
    d = detect(names)
    assert len(d.assignment) == 40
    for n, (tpl, dim) in d.assignment.items():
        pre, suf = tpl.split("*")
        assert f"{pre}{dim}{suf}" == n  # variable-length slots, exact round trip


def test_support_boundary():
    assert detect(dags(19)).families == ()
    assert len(detect(dags(20)).families) == 1


def test_a_lone_namespace_prefix_is_not_a_family():
    names = [
        f"node_thing{i}_total" for i in range(40)
    ]  # unrelated metrics sharing node_ and _total
    assert detect(names).families == ()
    assert (
        detect([f"node_a_b{i}_c{i}_total" for i in range(40)]).families != ()
    )  # structure around it


def test_distinct_statistics_are_not_identifiers():
    names = [f"node_netstat_Tcp_Stat{i}" for i in range(40)]  # one token each: different statistics
    assert detect(names).families == ()


def test_a_small_closed_set_splits_into_separate_families():
    names = [
        f"svc_req_{site}_id{i}_part{i}_count" for site in ("eqiad", "codfw") for i in range(30)
    ]
    got = sorted(f.template for f in detect(names).families)
    assert got == ["svc_req_codfw_*_count", "svc_req_eqiad_*_count"]


def test_rare_names_are_left_alone_not_forced_into_the_family():
    names = [*dags(30), "job_run_special", "job_run_other_thing_failed_badly"]
    d = detect(names)
    assert "job_run_special" not in d.assignment and len(d.assignment) == 30


def test_order_does_not_matter():
    names = dags(60) + dags(60, kind="job_stop") + [f"misc_metric_{i}" for i in range(5)]
    a = detect(names)
    shuffled = names[:]
    random.Random(3).shuffle(shuffled)
    b = detect(shuffled)
    assert a.families == b.families and a.assignment == b.assignment


def test_short_names_are_ignored():
    assert detect(["a", "a_b", *[f"x_{i}" for i in range(50)]]).families == ()


def test_template_text():
    assert template_of(("a", "b"), ("c",)) == "a_b_*_c" and template_of(("a",), ()) == "a_*"


def test_three_hundred_thousand_names_in_seconds():
    names = [
        f"airflow_ti_finish_dag{i % 900}_task{i}_{s}" for i in range(24_000) for s in ("ok", "bad")
    ] * 6
    names += [f"other_{i}_gauge" for i in range(30_000)]
    t = time.monotonic()
    d = detect(names)
    assert time.monotonic() - t < 15 and d.families


# evaluation on the hand-labelled Wikimedia sample --------------------------------------------
def test_precision_and_recall_on_the_labelled_wikimedia_sample():
    names = json.loads((FIX / "wikimedia_names_sample.json").read_text())
    labels = json.loads((FIX / "labels.json").read_text())
    d = detect(names)
    s = evaluate(d, labels)
    # measured: precision 0.980, recall 0.923, dimension agreement 1.0 (120 labelled names)
    assert s.positives == 52 and s.labelled == 120
    assert s.precision >= 0.95 and s.recall >= 0.9 and s.dimension_agreement >= 0.95
    airflow = [f for f in d.families if f.template.startswith("airflow_")]
    assert 0 < len(airflow) <= 40  # thousands of Airflow names collapse to a few dozen templates
    assert sum(f.members for f in airflow) > 4000
    assert any(f.template == "airflow_ti_finish_mediawiki_dumps_sql_xml_regular_*" for f in airflow)


def test_unique_metrics_in_the_sample_stay_individual():
    names = json.loads((FIX / "wikimedia_names_sample.json").read_text())
    d = detect(names)
    for n in ("node_os_info", "airflow_scheduler_heartbeat", "up", "alertmanager_cluster_members"):
        assert n not in d.assignment


# catalog integration -------------------------------------------------------------------------
FAMILY = "airflow_ti_finish_*_removed"


def discovery(n=40, extra=()):
    infos = [
        MetricInfo(f"airflow_ti_finish_a{i}_b{i}_removed", "counter", None, None) for i in range(n)
    ]
    infos += [MetricInfo(f"airflow_ti_finish_a{i}_b{i}_success", "counter") for i in range(n)]
    infos += [
        MetricInfo("airflow_scheduler_heartbeat", "counter"),
        MetricInfo("up", "gauge"),
        *extra,
    ]
    return Discovery(tuple(infos), (), {}, None, 1.0, (), False)


@pytest.fixture
async def svc(tmp_path):
    s = make_service(tmp_path, FakeSource(name="default", discovery=discovery()))
    s.out = await s.learn("default")  # type: ignore[attr-defined]
    return s


async def test_learn_collapses_names_into_families_and_says_so(svc):
    out = svc.out
    assert out["families"] == 2 and out["family_members"] == 80 and out["metrics"] == 82
    names = {e.metric for e in svc.ws.catalog.list_entries("default")}
    assert names == {
        "airflow_ti_finish_*_removed",
        "airflow_ti_finish_*_success",
        "airflow_scheduler_heartbeat",
        "up",
    }


async def test_the_family_speaks_for_its_members(svc):
    e = svc.ws.catalog_entry("default", FAMILY)
    assert e.is_family and e.family_members == 40
    assert "40 metrics share this name template" in e.fields["description"].value
    assert (e.fields["type"].value, e.fields["type"].origin) == ("counter", "metadata")
    assert "declared by 40 of 40" in e.fields["type"].citation
    m = svc.ws.catalog_entry("default", "airflow_ti_finish_a3_b3_removed")
    assert (m.family, m.dimension) == (FAMILY, "a3_b3")
    assert (
        m.inherited_from == FAMILY and m.fields["type"].value == "counter"
    )  # inherited, not copied
    assert svc.ws.catalog.claims_for("default", "airflow_ti_finish_a3_b3_removed")[0].origin in (
        "metadata",
        "rule",
    )
    n_claims = svc.ws.catalog._db.execute(
        "SELECT COUNT(*) FROM catalog_claims WHERE metric LIKE 'airflow_ti_finish_a%'"
    ).fetchone()[0]
    assert n_claims == 0  # members carry no claims of their own


async def test_a_member_with_its_own_claim_overrides_the_family(svc):
    svc.ws.catalog_claim(
        "default", "airflow_ti_finish_a3_b3_removed", "unit", "count", "user", "user"
    )
    m = svc.ws.catalog_entry("default", "airflow_ti_finish_a3_b3_removed")
    assert m.inherited_from is None and m.fields["unit"].origin == "user"


async def test_members_still_resolve_unit_and_type_for_charts(svc):
    f = svc.ws.catalog_facts("default", "airflow_ti_finish_a3_b3_removed")
    assert f.type == "counter"


async def test_relearning_changes_nothing_and_never_marks_families_removed(svc):
    again = await svc.learn("default")
    assert again["claims_changed"] == 0 and again["removed"] == 0 and again["new"] == 0
    assert again["families"] == 2
    assert svc.ws.catalog_entry("default", FAMILY).present


async def test_browse_collapses_families_and_lists_members_on_request(svc):
    out = svc.ws.catalog_browse("default", Browse())
    rows = {r["metric"]: r for r in out["rows"]}
    assert set(rows) == {
        "airflow_ti_finish_*_removed",
        "airflow_ti_finish_*_success",
        "airflow_scheduler_heartbeat",
        "up",
    }
    fam = rows[FAMILY]
    assert (
        fam["is_family"]
        and fam["family_members"] == 40
        and fam["family_info"]["status"] == "detected"
    )
    assert (
        out["summary"]["families"] == 2
        and out["summary"]["family_members"] == 80
        and out["summary"]["metrics"] == 4
    )
    full = svc.ws.catalog_browse("default", Browse(members=True, limit=100))
    assert full["total"] == 84  # four entries plus the 80 members
    only = svc.ws.catalog_browse("default", Browse(family=FAMILY, limit=100))
    assert only["total"] == 40 and all(r["family"] == FAMILY for r in only["rows"])
    members = svc.ws.family_members("default", FAMILY, limit=5)
    assert len(members["members_page"]) == 5 and members["members"] == 40
    assert members["members_page"][0]["dimension"].startswith("a")


async def test_search_and_overview_show_families_not_members(svc):
    res = svc.ws.catalog_search("default", prefix="airflow_ti")
    assert [r["metric"] for r in res["results"]] == [
        "airflow_ti_finish_*_removed",
        "airflow_ti_finish_*_success",
    ]
    assert all("_a3_b3_" not in r["metric"] for r in res["results"])  # no member name leaks through


async def test_metric_section_marks_families_and_members(svc):
    fam = svc.ws.metric_section("default", FAMILY)["family"]
    assert fam["role"] == "family" and fam["members"] == 40
    mem = svc.ws.metric_section("default", "airflow_ti_finish_a3_b3_removed")["family"]
    assert mem == {
        "role": "member",
        "template": FAMILY,
        "dimension": "a3_b3",
        "inherited": True,
    }


async def test_split_dissolves_for_good_and_members_get_their_own_claims_back(svc):
    out = svc.ws.catalog_family_decide("default", FAMILY, "split", "user", "user")
    assert out["released"] == 40
    ev = [e for e in svc.ws.log.since(0) if e.type == "catalog.family_split"][-1]
    assert (ev.actor, ev.klass) == ("user", "intentional")
    assert not svc.ws.catalog.has_metric("default", FAMILY)
    again = await svc.learn("default")
    assert (
        again["families"] == 1
    )  # the other family stays; the rejected template does not come back
    m = svc.ws.catalog_entry("default", "airflow_ti_finish_a3_b3_removed")
    assert m.family is None and m.fields["type"].value == "counter"  # T0 claims of its own again
    assert (await svc.learn("default"))["families"] == 1


async def test_confirm_pins_a_family_and_survives_a_listing_that_no_longer_shows_it(tmp_path):
    s = make_service(tmp_path, FakeSource(name="default", discovery=discovery()))
    await s.learn("default")
    s.ws.catalog_family_decide("default", FAMILY, "confirm", "user", "user", basis=None)
    s.sources.get("default").discovery = discovery(n=5)  # too few names to detect it now
    await s.learn("default")
    assert s.ws.families.info("default", FAMILY)["status"] == "confirmed"
    assert s.ws.catalog.has_metric("default", FAMILY)


async def test_decisions_are_checked(svc):
    with pytest.raises(NotFound):
        svc.ws.catalog_family_decide("default", "nope_*_x", "split", "user", "user")
    with pytest.raises(ValueError, match="action"):
        svc.ws.catalog_family_decide("default", FAMILY, "melt", "user", "user")
    with pytest.raises(ValueError, match="reserved"):
        svc.ws.catalog_family_decide("default", FAMILY, "split", "user", "claude")
    svc.ws.catalog_family_decide("default", FAMILY, "confirm", "user", "user")
    with pytest.raises(ValueError, match="only the user"):
        svc.ws.catalog_family_decide("default", FAMILY, "split", "claude", "claude", basis="x")
    ev = [e for e in svc.ws.log.since(0) if e.type == "catalog.family_confirmed"][-1]
    assert ev.klass == "intentional"


async def test_mcp_family_tool(svc):
    async with Client(build_mcp(svc, "http://x")) as c:
        bad = await c.call_tool(
            "catalog_family", {"source": "default", "template": FAMILY, "action": "split"}
        )
        assert bad.is_error and "basis" in bad.content[0].text
        ok = await c.call_tool(
            "catalog_family",
            {
                "source": "default",
                "template": "airflow_ti_finish_*_success",
                "action": "confirm",
                "basis": "same task counter per dag",
            },
        )
        assert not ok.is_error and json.loads(ok.content[0].text)["action"] == "confirm"
        ev = [e for e in svc.ws.log.since(0) if e.type == "catalog.family_confirmed"][-1]
        assert (ev.actor, ev.klass) == ("claude", "internal")


async def test_api_members_and_decisions(svc):
    with TestClient(create_app(svc, allowed_hosts=["testserver"])) as c:
        m = c.get(f"/api/catalog/default/families/{FAMILY}/members", params={"limit": 3}).json()
        assert len(m["members_page"]) == 3 and m["members"] == 40
        assert c.get("/api/catalog/default/families/ghost_*_x/members").status_code == 404
        listing = c.get(
            "/api/catalog", params={"source": "default", "family": FAMILY, "limit": 100}
        ).json()
        assert listing["total"] == 40
        assert (
            c.get(
                "/api/catalog", params={"source": "default", "members": "1", "limit": 100}
            ).json()["total"]
            == 84
        )
        r = c.post(
            "/api/catalog/families",
            json={"source": "default", "template": FAMILY, "action": "split"},
        )
        assert r.status_code == 200 and r.json()["released"] == 40
        assert (
            c.post(
                "/api/catalog/families",
                json={"source": "default", "template": FAMILY, "action": "split"},
            ).status_code
            == 404
        )
        assert c.post(
            "/api/catalog/families", json={"source": "default", "template": "x", "action": "melt"}
        ).status_code in (400, 404)


async def test_mcp_family_tool_forgiving(svc):
    async with Client(build_mcp(svc, "http://x")) as c:
        listed = await c.call_tool("catalog_family", {})
        assert not listed.is_error
        out = json.loads(listed.content[0].text)
        assert FAMILY in {f["template"] for f in out["families"]} and "usage" in out
        # `family` alias for `template`, `source` omitted
        ok = await c.call_tool(
            "catalog_family", {"family": FAMILY, "action": "confirm", "basis": "same counter"}
        )
        assert not ok.is_error and json.loads(ok.content[0].text)["template"] == FAMILY
        # unknown family: the error says what exists
        miss = await c.call_tool(
            "catalog_family", {"template": "airflow_ti_finish", "action": "split", "basis": "x"}
        )
        assert miss.is_error and "did you mean" in miss.content[0].text
