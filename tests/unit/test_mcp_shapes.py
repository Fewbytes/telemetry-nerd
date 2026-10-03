"""Tool argument shapes Claude tried in live evals (e0k): read when they mean one thing, refused
with a valid example otherwise. Replays every call the 2026-10-03 runs lost to shape."""

import json
from pathlib import Path

import pytest

from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.mcp.shapes import (
    AGGREGATION_AS_WRITTEN,
    EvidenceContext,
    ShapeError,
    finding_in,
    read_evidence,
    read_scope,
)
from telemetry_nerd.model.time import parse_time
from tests.unit.fakes import NOW, make_service
from tests.unit.test_mcp import call, text_of

FIXTURE = Path(__file__).parents[1] / "fixtures" / "evals" / "refused_calls.live-sonnet.json"
CALLS = json.loads(FIXTURE.read_text())["calls"]
LL_METHOD = "check_littles_law R = L/(lambda W), 2m windows"

# the runs' workspaces: payment p1 draws d9, p2 draws d7; overload's check_littles_law emitted
# littles_law_ratio on d5
PANELS = {"d9": ["p1"], "d7": ["p2"], "d3": ["p3", "p4"]}
METHODS = {("d5", "littles_law_ratio"): {LL_METHOD}}
CTX = EvidenceContext(
    lambda d: PANELS.get(d, []), lambda d, n: METHODS.get((d, n), set())
)  # fmt: skip


def to_ms(v):
    return None if v is None else parse_time(str(v), NOW)


def finding_args(c: dict) -> dict:
    return c["args"]


@pytest.mark.parametrize("c", [c for c in CALLS if c["tool"] == "finding_create"],
                         ids=lambda c: c["id"])  # fmt: skip
def test_replay_refused_finding_create(c):
    if c["now"] == "refused":
        with pytest.raises(ShapeError) as e:
            finding_in(c["args"], CTX, to_ms)
        msg = str(e.value)
        assert '{"kind": ' in msg, msg  # shows a valid item to copy
        return
    data, read_as = finding_in(c["args"], CTX, to_ms)
    assert data.claim == c["args"]["claim"]
    kinds = [type(ev).__name__ for ev in data.evidence]
    assert "StatisticRef" in kinds or "PanelRef" in kinds
    if "aggregation" not in json.dumps(c["args"]):
        assert data.scope.aggregation == AGGREGATION_AS_WRITTEN
        assert any("aggregation" in n for n in read_as)


def test_replay_bare_ids_refuse_only_the_code_node():
    c = next(c for c in CALLS if c["id"] == "pf-f1-bare-ids")
    with pytest.raises(ShapeError) as e:
        finding_in(c["args"], CTX, to_ms)
    msg = str(e.value)
    assert msg.startswith("evidence.3: c3 is a code node")
    # without c3 the same call reads: d9 is p1's dataset, so it collapses into p1
    args = {**c["args"], "evidence": ["d9", "p1", "a1"]}
    data, read_as = finding_in(args, CTX, to_ms)
    assert [e.model_dump() for e in data.evidence] == [
        {"kind": "panel", "panel": "p1"},
        {"kind": "annotation", "annotation": "a1"},
    ]
    assert any("d9 read as panel p1" in n for n in read_as)
    assert data.scope.time_range.start_ms == to_ms("2026-10-03T13:10:00Z")


def test_replay_multikey_dict_splits_into_refs():
    c = next(c for c in CALLS if c["id"] == "pf-f1-multikey-dict-range")
    data, _ = finding_in(c["args"], CTX, to_ms)
    assert [e.model_dump() for e in data.evidence] == [
        {"kind": "panel", "panel": "p1"},
        {"kind": "annotation", "annotation": "a1"},
    ]


def test_replay_statistic_method_comes_from_the_op():
    c = next(c for c in CALLS if c["id"] == "os-f1-no-method-annotation-id")
    data, read_as = finding_in(c["args"], CTX, to_ms)
    stat, ann = (e.model_dump() for e in data.evidence)
    assert stat["method"] == LL_METHOD and stat["interval"] == (0.7827, 1.209)
    assert ann == {"kind": "annotation", "annotation": "a1"}
    assert any("taken from the op" in n for n in read_as)


def test_statistic_without_method_no_op_record_is_refused_with_example():
    c = next(c for c in CALLS if c["id"] == "os-f1-no-method-annotation-id")
    with pytest.raises(ShapeError) as e:
        finding_in(c["args"], EvidenceContext(), to_ms)
    msg = str(e.value)
    assert msg.startswith("evidence.0.method: Field required")
    assert "name the op that computed it" in msg and '"method": "analyze"' in msg


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("p1", [{"kind": "panel", "panel": "p1"}]),
        ("p1, a1", [{"kind": "panel", "panel": "p1"}, {"kind": "annotation", "annotation": "a1"}]),
        ({"kind": "panel", "id": "p2"}, [{"kind": "panel", "panel": "p2"}]),
        ({"kind": "Annotation", "id": "a1"}, [{"kind": "annotation", "annotation": "a1"}]),
        ({"id": "a3"}, [{"kind": "annotation", "annotation": "a3"}]),
        ([{"panel": ["p1", "p2"]}], [{"kind": "panel", "panel": "p1"},
                                     {"kind": "panel", "panel": "p2"}]),
        ('[{"kind": "panel", "panel": "p1"}]', [{"kind": "panel", "panel": "p1"}]),
        ({"kind": "dataset", "id": "d7"}, [{"kind": "panel", "panel": "p2"}]),
        (
            {"metric": "m", "field": "unit", "origins": ["a", "b"], "source": "s"},
            [{"kind": "claim", "metric": "m", "field": "unit", "origins": ["a", "b"],
              "source": "s"}],
        ),
        ({"metric": "m", "name": "mean", "value": 1}, None),  # statistic and claim fields
        (
            {"metric": "m", "field": "unit", "origins": ["a", "b"]},
            [{"kind": "claim", "metric": "m", "field": "unit", "origins": ["a", "b"]}],
        ),
    ],
)  # fmt: skip
def test_read_evidence_shapes(raw, want):
    if want is None:
        with pytest.raises(ShapeError):
            read_evidence(raw, CTX)
        return
    assert read_evidence(raw, CTX).value == want


@pytest.mark.parametrize(
    ("raw", "needle"),
    [
        ("d3", "drawn by several panels (p3, p4)"),  # ambiguous: never pick one
        ("d4", 'show(dataset="d4"'),  # not drawn: say how to make it evidence
        ("h1", "pass hypothesis='h1'"),
        ("f2", "is a finding"),
        ("x9z", "not an evidence id"),
        ({"kind": "annotation", "id": "p1"}, "not an annotation id"),
        ({"dataset": "d5", "name": "mean", "value": 1, "panel": "p1"}, "cannot tell which"),
        ({"kind": "dataset", "dataset": "d5", "name": "mean"}, "'dataset' is not an evidence"),
        ({"weird": 1}, "cannot tell which evidence kind"),
        ([], "give a list"),
    ],
)
def test_read_evidence_refuses_ambiguous_with_example(raw, needle):
    with pytest.raises(ShapeError) as e:
        read_evidence(raw, CTX)
    assert needle in str(e.value)
    assert '{"kind": ' in str(e.value) or "hypothesis=" in str(e.value) or "gap" in str(e.value)


def test_read_scope_shapes():
    base = {"source": "s", "selector": "up", "step": "1m"}
    a = read_scope({**base, "range": ["now-2h", "now-1h"]}, {}).value
    b = read_scope({**base, "time_range": {"start": "now-2h", "end": "now-1h"}}, {}).value
    c = read_scope(None, {**base, "start": "now-2h", "end": "now-1h"}).value
    d = read_scope({"source": "s"}, {"selector": "up", "step": "1m", "range": ["now-2h", "now-1h"]})
    assert a == b == c == d.value
    assert a["start"] == "now-2h" and a["aggregation"] == AGGREGATION_AS_WRITTEN
    kept = read_scope({**base, "range": ["now-2h", "now-1h"], "aggregation": "sum"}, {}).value
    assert kept["aggregation"] == "sum"
    bl = read_scope({**base, "range": ["now-2h", "now-1h"], "baseline": ["now-4h", "now-3h"]}, {})
    assert (bl.value["baseline_start"], bl.value["baseline_end"]) == ("now-4h", "now-3h")


@pytest.mark.parametrize(
    ("scope", "flat", "needle"),
    [
        ({"source": "s", "selector": "up", "start": "now-2h", "end": "now-1h"}, {},
         'scope.step: field required (scope.step missing; add "step": "30s")'),
        ({"source": "s", "selector": "up", "range": ["now-2h"], "step": "1m"}, {},
         "scope.range: give [start, end]"),
        ({"source": "s", "selector": "up", "range": ["now-2h", "now-1h"], "start": "now-3h",
          "step": "1m"}, {}, "scope.start is given twice"),
        ({"source": "s", "selector": "up", "range": ["now-2h", "now-1h"], "step": "1m"},
         {"source": "other"}, "scope.source is given twice"),
        ({"source": "s", "selector": "up", "range": ["now-2h", "now-1h"], "step": "1m",
          "window": "5m"}, {}, "unknown field(s) window"),
        ("nope", {}, "scope: give an object"),
    ],
)  # fmt: skip
def test_read_scope_refuses_with_example(scope, flat, needle):
    with pytest.raises(ShapeError) as e:
        read_scope(scope, flat)
    assert needle in str(e.value)


def test_scope_field_error_names_the_field_with_example():
    args = {"claim": "c", "evidence": ["p1"],
            "scope": {"source": "s", "selector": "up", "range": ["now-2h", "now-1h"],
                      "step": "soon"}}  # fmt: skip
    with pytest.raises(ShapeError) as e:
        finding_in(args, CTX, to_ms)
    assert str(e.value).startswith("scope.step: Value error, step must be a positive duration")
    assert '"step": "30s"' in str(e.value)
    args["scope"] = {**args["scope"], "step": "1m", "range": ["now-1h", "now-2h"]}
    with pytest.raises(ShapeError) as e:
        finding_in(args, CTX, to_ms)
    assert "scope.start/end" in str(e.value)


# --- through the MCP server ------------------------------------------------------------------


async def _workspace(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    await call(mcp, "query", {"expr": "up", "start": "now-2h", "end": "now-1h"})
    await call(mcp, "show", {"dataset": "d1", "question": "Is it up?"})
    await call(mcp, "annotate", {"kind": "event", "at": "now-90m", "label": "x", "panel": "p1"})
    await call(mcp, "hypothesis_create", {"statement": "up dropped"})
    return svc, mcp


async def test_mcp_finding_create_short_forms(tmp_path):
    svc, mcp = await _workspace(tmp_path)
    r = await call(mcp, "finding_create", {
        "claim": "up is flat", "evidence": ["d1", "a1"],
        "scope": {"source": "default", "selector": "up", "range": ["now-2h", "now-1h"],
                  "step": "15s"},
        "hypothesis": "h1", "stance": "against",
    })  # fmt: skip
    assert not r.is_error, text_of(r)
    out = json.loads(text_of(r))
    assert out["finding"] == "f1"
    assert any("d1 read as panel p1" in n for n in out["read_as"])
    assert any("scope.aggregation" in n for n in out["read_as"])
    f = svc.ws.objects.get_finding("f1")
    assert [e.model_dump() for e in f.evidence] == [
        {"kind": "panel", "panel": "p1"},
        {"kind": "annotation", "annotation": "a1"},
    ]


async def test_mcp_finding_create_flat_scope(tmp_path):
    _, mcp = await _workspace(tmp_path)
    r = await call(mcp, "finding_create", {
        "claim": "up is flat", "evidence": {"panel": "p1"}, "source": "default",
        "selector": "up", "start": "now-2h", "end": "now-1h", "step": "15s",
        "aggregation": "none",
    })  # fmt: skip
    assert not r.is_error, text_of(r)
    assert "read_as" not in json.loads(text_of(r))


async def test_mcp_finding_create_refusal_shows_example(tmp_path):
    _, mcp = await _workspace(tmp_path)
    r = await call(mcp, "finding_create", {
        "claim": "up is flat", "evidence": [{"kind": "dataset", "dataset": "d9"}],
        "scope": {"source": "default", "selector": "up", "range": ["now-2h", "now-1h"],
                  "step": "15s"},
    })  # fmt: skip
    assert r.is_error
    msg = text_of(r)
    assert "invalid arguments: evidence.0: dataset d9 is not evidence by itself" in msg
    assert 'show(dataset="d9"' in msg


async def test_mcp_hypothesis_update_id_alias(tmp_path):
    _, mcp = await _workspace(tmp_path)
    c = next(c for c in CALLS if c["id"] == "os-hypothesis-update-id")
    r = await call(mcp, "hypothesis_update", c["args"])
    assert not r.is_error, text_of(r)
    assert json.loads(text_of(r)) == {"hypothesis": "h1", "status": "refuted"}
    r = await call(mcp, "hypothesis_update", {"id": "h1", "hypothesis": "h2", "status": "x"})
    assert r.is_error and "disagree" in text_of(r)
    r = await call(mcp, "hypothesis_update", {"status": "refuted"})
    assert r.is_error and 'hypothesis_update(hypothesis="h1"' in text_of(r)
    r = await call(mcp, "hypothesis_update", {"hypothesis": "h1", "status": "wrong"})
    assert r.is_error and "status must be one of" in text_of(r)


async def test_mcp_query_accepts_question(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    c = next(c for c in CALLS if c["id"] == "pf-query-question")
    args = {**c["args"], "start": "now-2h", "end": "now-1h"}
    r = await call(mcp, "query", args)
    assert not r.is_error, text_of(r)
    out = json.loads(text_of(r))
    assert out["question"] == "Which services returned HTTP 5xx and when?"
    assert out["next"] == (
        'show(dataset="d1", question="Which services returned HTTP 5xx and when?")'
    )


async def test_mcp_gap_create_without_suggestion(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    c = next(c for c in CALLS if c["id"] == "pf-gap-description-only")
    r = await call(mcp, "gap_create", c["args"])
    assert not r.is_error, text_of(r)
    g = svc.ws.objects.list_gaps()[0]
    assert g.suggestion is None and g.missing_signal.startswith("Source holds samples")


async def test_mcp_unknown_argument_names_the_accepted_ones(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    r = await call(mcp, "hypothesis_create", {"statement": "s", "confidence": 0.5})
    assert r.is_error
    msg = text_of(r)
    assert "hypothesis_create takes no argument 'confidence'; its arguments are statement" in msg
