import json
from pathlib import Path

import pytest

from telemetry_nerd.catalog.models import validate_value
from telemetry_nerd.catalog.packs import (
    PACK_CONFIDENCE,
    PackError,
    PackIndex,
    builtin_packs,
    parse_pack,
)
from telemetry_nerd.model.discovery import Discovery, MetricInfo

from .fakes import FakeSource, make_service

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "packs"
HEADER = '[pack]\nname = "t"\nversion = "1"\ncitation = "c"\n'


def idx(body: str) -> PackIndex:
    return PackIndex((parse_pack(HEADER + body),))


def claims(index, metric):
    return {c.field: c.value for c in index.claims_for(metric)}


def test_exact_beats_regex_on_the_same_field():
    ix = idx(
        '[[metric]]\nmatch = "m_.*"\nunit = "s"\nrole = "latency"\n'
        '[[metric]]\nname = "m_x"\nunit = "ms"\n'
    )
    assert claims(ix, "m_x") == {"unit": "ms", "role": "latency"}
    assert claims(ix, "m_y") == {"unit": "s", "role": "latency"}
    assert claims(ix, "other") == {}


def test_names_list_and_full_match_regex():
    ix = idx(
        '[[metric]]\nnames = ["a", "b"]\ntype = "gauge"\n[[metric]]\nmatch = "c_[0-9]"\ntype = "counter"\n'
    )
    assert claims(ix, "b") == {"type": "gauge"}
    assert claims(ix, "c_1") == {"type": "counter"}
    assert claims(ix, "c_12") == {} and claims(ix, "xc_1") == {}  # full match only


def test_claims_are_pack_origin_with_version_citation():
    (c,) = idx('[[metric]]\nname = "a"\ntype = "gauge"\n').claims_for("a")
    assert (c.origin, c.confidence) == ("pack", PACK_CONFIDENCE)
    assert c.citation == "pack t@1: c"


@pytest.mark.parametrize(
    "entry",
    [
        'name = "a"\nnames = ["b"]',  # more than one selector
        'type = "gauge"',  # no selector
        'name = "a"\ntype = "untyped"',
        'name = "a"\nunit = "bytes"',  # not canonical
        'name = "a"\nrole = "vibes"',
        'name = "a"\nbounds = "positive"',
        'name = "a"\nbounded_by = []',
        'match = "("',
        'name = "a"\nsurprise = 1',
    ],
)
def test_invalid_entries_are_rejected(entry):
    with pytest.raises(PackError):
        parse_pack(HEADER + f"[[metric]]\n{entry}\n")


def test_duplicate_exact_names_are_rejected():
    with pytest.raises(PackError, match="duplicate"):
        parse_pack(HEADER + '[[metric]]\nname = "a"\n[[metric]]\nnames = ["a", "b"]\n')


def test_malformed_toml_and_missing_header_are_rejected():
    with pytest.raises(PackError):
        parse_pack("[[metric")
    with pytest.raises(PackError):
        parse_pack('[[metric]]\nname = "a"\n')


def test_first_pack_wins_a_field_across_packs():
    a = parse_pack(HEADER + '[[metric]]\nname = "m"\nunit = "s"\n')
    b = parse_pack(
        HEADER.replace('"t"', '"u"') + '[[metric]]\nname = "m"\nunit = "ms"\nrole = "latency"\n'
    )
    assert claims(PackIndex((a, b)), "m") == {"unit": "s", "role": "latency"}


# the built-in packs, grounded in real metadata --------------------------------------------
def real(name):
    return json.loads((FIX / name).read_text())


NODE = real("wikimedia_node_metadata.json")
K8S = real("play_k8s_metadata.json")
PACKS = {lp.pack.name: lp for lp in builtin_packs().packs}


@pytest.mark.parametrize(("pack", "meta"), [("node_exporter", NODE), ("kubernetes", K8S)])
def test_every_pack_name_exists_in_real_metadata(pack, meta):
    lp = PACKS[pack]
    assert lp.exact_names() <= meta.keys()
    for e in lp.entries:
        if e.match:
            assert any(e.matches(n) for n in meta), e.match
        for target in e.bounded_by or []:
            assert target in meta, f"{target} (bounded_by) is not a real metric"


@pytest.mark.parametrize(("pack", "meta"), [("node_exporter", NODE), ("kubernetes", K8S)])
def test_packs_never_contradict_a_declared_type(pack, meta):
    ix = PackIndex((PACKS[pack],))
    for name, (e,) in meta.items():
        declared, claimed = e["type"], claims(ix, name).get("type")
        # node_exporter exports many cumulative counters as "unknown"; anything else must agree
        if claimed is not None and declared != "unknown":
            assert declared == claimed, f"{name}: declared {declared}, pack {claimed}"


def test_all_builtin_claims_are_valid_catalog_values():
    for meta in (NODE, K8S):
        for name in meta:
            for c in builtin_packs().claims_for(name):
                validate_value(c.field, c.value)


def test_counters_and_rates_are_consistent_with_additivity():
    for name in [*NODE, *K8S]:
        c = claims(builtin_packs(), name)
        if c.get("type") == "counter":
            assert c.get("additivity_time") == "additive", name
            assert c.get("bounds") == "≥0", name


def disc_from(*metas):
    infos = [
        MetricInfo(n, None if e["type"] == "unknown" else e["type"], e["help"], e["unit"] or None)
        for meta in metas
        for n, (e,) in meta.items()
    ]
    return Discovery(tuple(infos), (), {}, None, 1.0, (), False)


@pytest.fixture
async def learned(tmp_path):
    svc = make_service(tmp_path, FakeSource(name="default", discovery=disc_from(NODE, K8S)))
    await svc.learn("default")
    return svc.ws


def test_wikimedia_node_exporter_metrics_get_descriptions_and_bounded_by(learned):
    e = learned.catalog_entry("default", "node_filesystem_avail_bytes")
    (rel,) = learned.catalog_relations("default", "node_filesystem_avail_bytes", "bounded_by")[
        "relations"
    ]
    assert (rel.subject, rel.object, rel.winner.origin) == (
        "node_filesystem_avail_bytes",
        "node_filesystem_size_bytes",
        "pack",
    )
    assert "bounded_by" not in e.fields  # a relation, not a field
    assert "available to non-root" in e.fields["description"].value
    assert e.fields["description"].origin == "pack"  # outranks the declared HELP
    mem = learned.catalog_relations("default", "node_memory_MemAvailable_bytes", "bounded_by")
    assert [r.object for r in mem["relations"]] == ["node_memory_MemTotal_bytes"]


def test_pack_corrects_untyped_counters(learned):
    e = learned.catalog_entry("default", "node_vmstat_oom_kill")
    assert e.fields["type"].value == "counter" and e.fields["type"].origin == "pack"
    assert learned.catalog_facts("default", "node_vmstat_oom_kill").type == "counter"


def test_pack_unit_beats_a_misleading_name_and_the_conflict_is_kept(learned):
    e = learned.catalog_entry("default", "node_network_speed_bytes")
    assert e.fields["unit"].value == "B/s" and e.fields["unit"].origin == "pack"
    assert [(c.origin, c.value) for c in e.conflicts()["unit"]] == [("rule", "B")]


def test_timestamps_are_labeled_as_such(learned):
    for name in ("node_boot_time_seconds", "kube_pod_created", "container_last_seen"):
        assert learned.catalog_entry("default", name).fields["role"].value == "timestamp"


def test_container_metrics_without_unit_suffix_get_bytes(learned):
    for name in ("container_memory_rss", "container_memory_cache"):
        assert learned.catalog_entry("default", name).fields["unit"].value == "B"
    assert learned.catalog_facts("default", "container_memory_rss").unit == "B"


def test_kube_state_flags_are_zero_one_and_additive(learned):
    e = learned.catalog_entry("default", "kube_pod_status_phase")
    assert (e.fields["bounds"].value, e.fields["additivity_series"].value) == ("[0,1]", "additive")


def test_user_claim_beats_pack(learned):
    learned.catalog_claim("default", "node_load1", "role", "mine", "user", "user")
    assert learned.catalog_entry("default", "node_load1").fields["role"].value == "mine"


async def test_pack_relations_only_link_metrics_the_source_has(tmp_path):
    only_avail = Discovery(
        (MetricInfo("node_filesystem_avail_bytes", "gauge", "h", None),),
        (),
        {},
        None,
        1.0,
        (),
        False,
    )
    svc = make_service(tmp_path, FakeSource(name="default", discovery=only_avail))
    out = await svc.learn("default")
    assert out["relations_changed"] == 0  # the size metric is not in this source: no dangling edge
    assert svc.ws.catalog_relations("default")["relations"] == []


async def test_relearning_does_not_rewrite_unchanged_relations(tmp_path):
    svc = make_service(tmp_path, FakeSource(name="default", discovery=disc_from(NODE)))
    first = await svc.learn("default")
    again = await svc.learn("default")
    assert first["relations_changed"] > 0 and again["relations_changed"] == 0
