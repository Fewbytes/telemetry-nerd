import json

import pyarrow as pa
import pytest
from mcp import Client

from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.model.discovery import Discovery, MetricInfo
from telemetry_nerd.model.series import (
    BUCKET_SCHEMA,
    SERIES_SCHEMA,
    FetchResult,
    labels_json,
    series_id,
)
from telemetry_nerd.sources.base import LimitExceeded, SourceError

from .fakes import FakeSource, make_service

COUNTER = lambda i: float((i * 5) % 300)
GAUGE = lambda i: 100.0 + (0.0 if i % 2 else -5.0) + i * 0.1
GROWS = lambda i: float(i)
NEGATIVE = lambda i: float((i % 7) - 3)
CONST = lambda i: 7.0


class Shapes(FakeSource):
    def __init__(self, patterns, **kw):
        super().__init__(**kw)
        self.patterns = patterns
        self.queried: list[str] = []
        self.errors: dict[str, Exception] = {}

    async def fetch(self, expr, rng, step_ms):
        self.queried.append(expr)
        if expr in self.errors:
            raise self.errors[expr]
        fn = self.patterns.get(expr, CONST)
        ts = list(range(rng.start_ms, rng.end_ms + 1, step_ms))
        labels = [{"instance": f"i{k}"} for k in range(2)]
        sids = [series_id(self.name, lb) for lb in labels]
        rows = [(t, sid, fn(i)) for sid in sids for i, t in enumerate(ts)]
        v = [r[2] for r in rows]
        buckets = pa.table(
            {"ts_ms": [r[0] for r in rows], "series_id": [r[1] for r in rows],
             "avg": v, "min": v, "max": v, "count": [1] * len(rows)},
            schema=BUCKET_SCHEMA,
        )  # fmt: skip
        series = pa.table(
            {"series_id": sids, "labels": [labels_json(lb) for lb in labels]}, schema=SERIES_SCHEMA
        )
        return FetchResult(buckets, series)


def disc(*infos):
    return Discovery(tuple(infos), (), {}, None, 1.0, (), False)


METRICS = [
    MetricInfo("plain_counter"),  # nothing declared
    MetricInfo("declared_gauge", "gauge"),
    MetricInfo("loki_files_total"),  # only the name says counter
    MetricInfo("temp_seconds"),  # name rule: >= 0
    MetricInfo("steady"),
    MetricInfo("node_filesystem_size_bytes", "gauge"),  # a pack curates this one
    MetricInfo("cache_hit_ratio"),  # name rule: [0,1]
]
PATTERNS = {
    "plain_counter": COUNTER,
    "declared_gauge": GROWS,
    "loki_files_total": GAUGE,
    "temp_seconds": NEGATIVE,
    "node_filesystem_size_bytes": GROWS,
    "cache_hit_ratio": GROWS,
}


@pytest.fixture
async def svc(tmp_path):
    src = Shapes(PATTERNS, name="default", discovery=disc(*METRICS))
    s = make_service(tmp_path, src)
    await s.learn("default")
    s.src = src  # type: ignore[attr-defined]
    return s


def entry(svc, m):
    return svc.ws.catalog_entry("default", m)


def claim_of(svc, m, field, origin):
    return next((c for c in entry(svc, m).claims.get(field, []) if c.origin == origin), None)


async def scan(svc, *metrics, **kw):
    return await svc.scan_metrics("default", list(metrics), **kw)


async def test_counter_behaviour_becomes_a_stats_claim_and_an_observation(svc):
    out = await scan(svc, "plain_counter")
    (row,) = out["scanned"]
    assert row["verdict"] == "counter-like" and row["resets"] > 0 and row["small_decreases"] == 0
    c = claim_of(svc, "plain_counter", "type", "stats")
    assert (
        c and c.value == "counter" and c.confidence == 0.6 and "sample scan over 30m" in c.citation
    )
    assert entry(svc, "plain_counter").fields["type"].value == "counter"
    obs = svc.ws.samples.get("default", "plain_counter")
    assert obs and obs.verdict == "counter-like" and svc.datasets.exists(obs.dataset)
    assert svc.ws.catalog_facts("default", "plain_counter").type == "counter"


async def test_gauge_behaviour_and_gap_filling_bounds(svc):
    await scan(svc, "steady", "loki_files_total")
    assert claim_of(svc, "steady", "type", "stats") is None  # constant: no claim
    b = claim_of(svc, "steady", "bounds", "stats")
    assert b and b.value == "≥0" and b.confidence == 0.4  # nothing claimed bounds yet
    assert entry(svc, "loki_files_total").fields["type"].value == "gauge"  # evidence beat the name


async def test_a_declared_gauge_that_only_grows_is_a_finding_and_evidence_wins_over_metadata(svc):
    out = await scan(svc, "declared_gauge")
    (row,) = out["scanned"]
    assert len(row["findings"]) == 1
    f = svc.ws.objects.get_finding(row["findings"][0])
    assert (
        f.author == "system"
        and "declared a gauge" in f.claim
        and "(declared by metadata)" in f.claim
    )
    (ev,) = f.evidence
    assert (ev.name, ev.exact, ev.value) == ("increases", True, float(row["increases"]))
    assert svc.datasets.exists(ev.dataset) and "short_window" in f.caveats
    assert f.scope.selector == "declared_gauge" and f.scope.aggregation.startswith("raw samples")
    assert entry(svc, "declared_gauge").fields["type"].origin == "stats"


async def test_a_name_only_counter_that_behaves_as_a_gauge_says_the_claim_is_a_naming_convention(
    svc,
):
    out = await scan(svc, "loki_files_total")
    f = svc.ws.objects.get_finding(out["scanned"][0]["findings"][0])
    assert "a name convention only" in f.claim and "decreased without resetting" in f.claim
    assert f.evidence[0].name == "small_decreases"


async def test_negative_values_against_a_nonnegative_bound_are_a_finding_and_bounds_are_not_degraded(
    svc,
):
    out = await scan(svc, "temp_seconds", "cache_hit_ratio")
    neg = svc.ws.objects.get_finding(out["scanned"][0]["findings"][0])
    assert "negative samples" in neg.claim and neg.evidence[0].name == "negatives"
    assert (
        claim_of(svc, "temp_seconds", "bounds", "stats") is None
    )  # a bound exists: no gap to fill
    assert claim_of(svc, "cache_hit_ratio", "bounds", "stats") is None
    assert entry(svc, "cache_hit_ratio").fields["bounds"].value == "[0,1]"  # not degraded to >=0


async def test_a_pack_claim_is_never_overridden_but_the_disagreement_is_filed(svc):
    out = await scan(svc, "node_filesystem_size_bytes")
    assert claim_of(svc, "node_filesystem_size_bytes", "type", "stats") is None
    assert entry(svc, "node_filesystem_size_bytes").fields["type"].origin == "pack"
    assert len(out["scanned"][0]["findings"]) == 1  # a declared gauge that only grows


async def test_user_and_claude_types_are_not_overridden_either(svc):
    svc.ws.catalog_claim("default", "plain_counter", "type", "gauge", "user", "user")
    await scan(svc, "plain_counter")
    assert claim_of(svc, "plain_counter", "type", "stats") is None
    assert entry(svc, "plain_counter").fields["type"].origin == "user"


async def test_findings_are_filed_once_per_metric_and_kind(svc):
    first = await scan(svc, "declared_gauge")
    again = await scan(svc, "declared_gauge", refresh=True)
    assert first["scanned"][0]["findings"] and again["scanned"][0]["findings"] == []
    assert len(svc.ws.objects.list_findings()) == 1


async def test_budget_limit_fresh_skip_refresh_and_clock(svc, monkeypatch):
    names = ["plain_counter", "declared_gauge", "steady"]
    out = await scan(svc, *names, limit=2)
    assert [r["metric"] for r in out["scanned"]] == names[:2]
    assert (out["stopped"], out["remaining"]) == ("limit", 1)
    assert len(svc.src.queried) == 2
    again = await scan(svc, *names, limit=2)  # the first two are fresh: only the third is queried
    assert [r["metric"] for r in again["scanned"]] == ["steady"]
    assert {s["metric"] for s in again["skipped"]} == set(names[:2])
    assert "refresh" in again["skipped"][0]["reason"]
    redo = await scan(svc, "plain_counter", refresh=True)
    assert [r["metric"] for r in redo["scanned"]] == ["plain_counter"]
    # a day later they are scanned again without refresh
    t = svc.clock() + 86_400_001
    monkeypatch.setattr(svc, "clock", lambda: t)
    later = await scan(svc, "plain_counter")
    assert len(later["scanned"]) == 1
    # wall-clock budget
    none = await scan(svc, "steady", refresh=True, budget_s=0)
    assert none["scanned"] == [] and (none["stopped"], none["remaining"]) == ("budget", 1)


async def test_hard_cap_on_queries_per_call(svc):
    svc.ws.catalog.relearn("default", [f"m{i}" for i in range(150)], 1)
    out = await svc.scan_metrics("default", prefix="m", limit=10_000)
    assert len(out["scanned"]) + len(out["failed"]) == 100 and out["stopped"] == "limit"


async def test_series_cap_unknown_metrics_and_source_errors_are_reported_not_hidden(svc):
    svc.src.errors["steady"] = LimitExceeded("query returned 900 series (limit 500)")
    svc.src.errors["plain_counter"] = SourceError("boom")
    out = await scan(svc, "steady", "plain_counter", "ghost", "declared_gauge")
    assert [r["metric"] for r in out["scanned"]] == ["declared_gauge"]
    assert any(s["metric"] == "steady" and "900 series" in s["reason"] for s in out["skipped"])
    assert any(s["metric"] == "ghost" and "source_learn" in s["reason"] for s in out["skipped"])
    assert out["failed"] == [{"metric": "plain_counter", "reason": "boom"}]


async def test_window_is_validated(svc):
    for bad in ("1m", "12h"):
        with pytest.raises(ValueError, match="window must be between"):
            await scan(svc, "steady", window=bad)


async def test_default_targets_are_the_hot_metrics_and_prefix_scans_hot_first(svc):
    assert (await svc.scan_metrics("default"))["scanned"] == []  # nothing hot yet
    await svc.query("declared_gauge", start="now-2h", end="now-1h")
    out = await svc.scan_metrics("default")
    assert [r["metric"] for r in out["scanned"]] == ["declared_gauge"]
    pre = await svc.scan_metrics("default", prefix="s")
    assert [r["metric"] for r in pre["scanned"]] == ["steady"]


async def test_the_card_shows_measured_resets(svc):
    ds = (await svc.query("plain_counter", start="now-2h", end="now-1h"))["dataset"]
    pid = svc.show(ds, "q?").panel.id
    before = (await svc.panel_card(pid))["quality"]["resets"]
    assert before["measured"] is False and "catalog_scan" in before["reason"]
    await scan(svc, "plain_counter")
    after = (await svc.panel_card(pid))["quality"]["resets"]
    assert after["measured"] and after["resets"] > 0 and after["verdict"] == "counter-like"


async def test_mcp_catalog_scan(svc):
    async with Client(build_mcp(svc, "http://x")) as c:
        res = await c.call_tool(
            "catalog_scan", {"source": "default", "metrics": ["declared_gauge"]}
        )
        assert not res.is_error
        out = json.loads(res.content[0].text)
        assert out["scanned"][0]["findings"] and out["window"] == "30m"
        bad = await c.call_tool(
            "catalog_scan", {"source": "default", "metrics": ["steady"], "window": "1m"}
        )
        assert bad.is_error
        nosrc = await c.call_tool("catalog_scan", {"source": "nope"})
        assert nosrc.is_error


async def test_a_classic_histogram_base_is_skipped_not_scanned(tmp_path):
    """6gp: the base name X is catalogued but has no series of its own."""
    d = Discovery(
        (MetricInfo("lat_seconds_bucket"), MetricInfo("lat_seconds_sum"),
         MetricInfo("lat_seconds_count")),
        (), {"lat_seconds": "classic"}, None, 1.0, (), False,
    )  # fmt: skip
    svc = make_service(tmp_path, Shapes({}, name="default", discovery=d))
    await svc.learn("default")
    out = await svc.scan_metrics("default", metrics=["lat_seconds"])
    assert out["scanned"] == [] and "lat_seconds_count" in out["skipped"][0]["reason"]
    assert svc.sources.get("default").queried == []


async def test_scan_learns_the_characteristic_range_of_a_gauge_not_a_counter(svc):
    """4f1: observed p1-p99 (with min/max and order-statistic intervals) as a stats claim."""
    out = await scan(svc, "loki_files_total", "plain_counter")
    rows = {r["metric"]: r for r in out["scanned"]}
    assert "typical_range" in rows["loki_files_total"]["claims"]
    assert "typical_range" not in rows["plain_counter"]["claims"]  # a running total's range
    assert claim_of(svc, "plain_counter", "typical_range", "stats") is None
    c = claim_of(svc, "loki_files_total", "typical_range", "stats")
    v = c.value
    assert c.confidence == 0.5 and "not a bound" in c.citation and v["window"] == "30m"
    assert v["min"] <= v["lo"] <= v["median"] <= v["hi"] <= v["max"]
    assert v["lo_ci95"][0] <= v["lo"] <= v["lo_ci95"][1] and "lower bound" in v["interval"]
    # 7vr: the metric card lists it, with its origin and confidence like any learned fact
    from telemetry_nerd.core.card_payload import field_rows

    rows_ = {r["field"]: r for r in field_rows(entry(svc, "loki_files_total"))}
    assert (
        rows_["typical_range"]["origin"] == "stats" and rows_["typical_range"]["confidence"] == 0.5
    )
    assert "typical_range" not in {r["field"] for r in field_rows(entry(svc, "plain_counter"))}
    assert v["series"] == 2 and v["n"] >= 30
    assert rows["loki_files_total"]["range"]["p99"] == v["hi"]
    # it feeds the y context of a plain-selector panel, labelled as observed
    ds = (await svc.query("loki_files_total", start="now-2h", end="now-1h"))["dataset"]
    pid = svc.show(ds, "files?").panel.id
    ctx = await svc.y_context(pid)
    assert ctx.typical and (ctx.typical.lo, ctx.typical.hi) == (v["lo"], v["hi"])
    assert "observed, not a bound" in ctx.typical.basis and "30m scan" in ctx.typical.basis


def test_characteristic_range_needs_enough_samples_and_survives_a_spike():
    from telemetry_nerd.analysis.samples import characteristic_range

    assert characteristic_range([1.0] * 29 + [None]) is None
    r = characteristic_range([float(i % 10) for i in range(1000)] + [1e6])
    assert r.max == 1e6 and r.hi == 9.0 and r.lo == 0.0 and r.n == 1001
    assert r.hi_ci[0] <= r.hi <= r.hi_ci[1]


def test_the_typical_view_needs_a_typical_range():
    from telemetry_nerd.charts.spec import YContext, YTypical
    from telemetry_nerd.charts.yview import ValueStats, YView, check_view

    st = ValueStats(0.0, 5.0, quantile=False)
    v = YView(mode="typical", label="typical range")
    with pytest.raises(ValueError, match="catalog_scan"):
        check_view(v, st, YContext())
    assert check_view(v, st, YContext(typical=YTypical(lo=1, hi=2, basis="b"))) == []
