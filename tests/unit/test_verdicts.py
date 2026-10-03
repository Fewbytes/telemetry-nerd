"""Binding verdicts (bead czt.4): per-signal health against reference windows, with evidence."""

import json
import math
from statistics import NormalDist

import numpy as np
import pyarrow as pa
import pytest
from mcp import Client

from telemetry_nerd.analysis.histogram import from_matrix, histogram_expr
from telemetry_nerd.analysis.seasonal_dist import Hist
from telemetry_nerd.analysis.spc import cusum_arl
from telemetry_nerd.analysis.verdicts import (
    CUSUM_K,
    Onset,
    cusum_h,
    episodes,
    family,
    judge_counts,
    judge_roles,
    judge_values,
    near_bound,
    order_onsets,
    share_threshold,
)
from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.model.discovery import Discovery
from telemetry_nerd.model.discovery import MetricInfo as DiscoveredMetric
from telemetry_nerd.model.series import (
    BUCKET_SCHEMA,
    SERIES_SCHEMA,
    FetchResult,
    labels_json,
    series_id,
)

from .fakes import NOW, FakeSource, make_service
from .verdict_sim import STEP_MS, Scenario, cycles, red_inputs, ts, use_inputs

T = ts()
MIN = 60_000


def _min(ms):  # minutes since now's window start
    return (ms - (T[0] - STEP_MS)) / STEP_MS


# --- the statistics (pure) ------------------------------------------------------------------------
def test_family_splits_alpha_over_roles_and_detectors():
    f = family(0.05, 3)
    assert (f.per_role, f.per_detector) == (pytest.approx(0.05 / 3), pytest.approx(0.05 / 6))


def test_cusum_h_meets_the_episode_budget():
    h = cusum_h(60, 0.01)
    assert 60 / cusum_arl(CUSUM_K, h) <= -math.log1p(-0.01) * 1.01
    assert cusum_h(60, 0.001) > h and cusum_h(600, 0.01) > h  # stricter / longer -> higher


def test_an_episode_ends_at_its_peak_or_stays_open():
    z = np.zeros(60)
    z[20:28] = 4.0
    (e,) = episodes(z, 5.0)
    assert (e.side, e.start, e.signal, e.end) == (1, 20, 21, 27)
    z2 = np.zeros(60)
    z2[50:] = 4.0
    (e2,) = episodes(z2, 5.0)
    assert e2.start == 50 and e2.end is None


def test_onsets_are_ordered_only_when_their_intervals_do_not_overlap():
    a = Onset(10 * MIN, 7 * MIN, 11 * MIN, "cusum")
    b = Onset(20 * MIN, 17 * MIN, 21 * MIN, "cusum")
    c = Onset(12 * MIN, 9 * MIN, 13 * MIN, "cusum")
    o = order_onsets({"errors": b, "duration": a})
    assert o.first == "duration" and o.clusters == [["duration"], ["errors"]]
    o2 = order_onsets({"errors": c, "duration": a})
    assert o2.first is None and o2.clusters == [["duration", "errors"]]
    before = Onset(None, None, 0, "before_window")
    assert order_onsets({"rate": before, "errors": b}).first == "rate"


def test_near_bound_counts_runs_exactly():
    y = np.array([0.5, 0.95, 0.96, 0.97, 0.5, 0.95, 0.5])
    nb = near_bound(y, 1.0)
    assert nb.steps == 4 and nb.runs == [(1, 3)]


def test_latency_threshold_is_the_reference_p95_edge():
    lo, hi = np.array([0, 0.1, 0.25, 0.5]), np.array([0.1, 0.25, 0.5, math.inf])
    refs = [Hist(j, 0, 1, lo, hi, np.array([700.0, 250, 40, 10])) for j in (1, 2, 3)]
    x, enough = share_threshold(refs, np.array([0.1, 0.25, 0.5]))
    assert x == 0.25 and enough  # 5% above 0.25 (50 per window)


def test_no_events_anywhere_is_no_change_exactly():
    zero = (np.zeros(60), np.full(60, 3000.0))
    j = judge_counts(zero, [zero] * 4, T, STEP_MS, alpha_level=0.01, alpha_episode=0.01)
    assert j.status == "no_change" and j.now_value == 0.0


def test_fewer_than_three_reference_windows_is_insufficient():
    cs = cycles(1)
    j = judge_values(cs[0].rate, [c.rate for c in cs[1:3]], T, STEP_MS, alpha_level=0.01,
                     alpha_episode=0.01)  # fmt: skip
    assert j.status == "insufficient" and "reference windows" in j.reasons[0]


@pytest.mark.parametrize(("kind", "inputs"), [("RED", red_inputs), ("USE", use_inputs)])
def test_quiet_bindings_raise_no_flag_at_the_family_alpha(kind, inputs):
    n = 150
    flagged = sum(
        any(r.judgement.status == "changed" for r in judge_roles(inputs(cycles(s)), T, STEP_MS)[0].values())
        for s in range(n)
    )  # fmt: skip
    # family-wise 5%; calibrated 1-1.5% (scripts/calibrate_verdicts.py): binomial 99% upper bound
    assert flagged / n <= 0.05 + 2.6 * math.sqrt(0.05 * 0.95 / n)


def test_an_error_burst_is_flagged_on_errors_as_a_burst_with_its_onset():
    for s in range(12):
        res, _, order = judge_roles(
            red_inputs(cycles(s, Scenario(error_burst=(30, 37)))), T, STEP_MS
        )
        j = res["errors"].judgement
        assert j.status == "changed" and j.direction == "higher"
        assert j.pattern in ("burst", "sustained"), j.pattern
        assert _min(j.onset.lo_ms) <= 30 <= _min(j.onset.hi_ms)
        assert res["rate"].judgement.status != "changed" or s in ()
        assert order.order[0] == "errors" or res["duration"].judgement.status == "changed"


def test_latency_first_then_errors_is_ordered():
    right = covered = 0
    for s in range(12):
        sc = Scenario(latency_shift=20, error_burst=(40, 47))
        res, _, order = judge_roles(red_inputs(cycles(s, sc)), T, STEP_MS)
        d, e = res["duration"].judgement, res["errors"].judgement
        assert d.status == e.status == "changed" and d.direction == "higher"
        covered += _min(d.onset.lo_ms) <= 20 <= _min(d.onset.hi_ms)  # ~95% intervals
        assert order.first in ("duration", None)  # never errors first
        right += order.first == "duration"
    assert right >= 11 and covered >= 10


def test_a_saturation_episode_flags_utilization_and_saturation_near_the_bound():
    for s in range(8):
        res, _, order = judge_roles(
            use_inputs(cycles(s, Scenario(saturation=(20, 40)))), T, STEP_MS
        )
        u, q = res["utilization"], res["saturation"]
        assert u.judgement.status == q.judgement.status == "changed"
        assert u.judgement.direction == q.judgement.direction == "higher"
        assert u.near[""].runs and u.near[""].runs[0][0] == 20
        # onsets 2 min apart, intervals overlap: not ordered
        assert order.first in (None, "utilization")


# --- through the service -------------------------------------------------------------------------
METRICS = (
    ("http_server_requests_total", "counter"),
    ("http_server_request_duration_seconds", "histogram"),
    ("http_server_active_requests", "gauge"),
    ("node_cpu_seconds_total", "counter"),
    ("node_pressure_cpu_waiting_seconds_total", "counter"),
)
LES = [0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, math.inf]
W0 = NOW - 3_600_000  # now's window: the last hour
_N = NormalDist()


def _at(minute: float) -> int:
    return int(W0 + minute * MIN)


class ScenarioSource(FakeSource):
    """Seeded values keyed by (minute, member): any window is reproducible. Per-hour level
    jitter, per-minute noise; scenario changes at absolute times inside now's window."""

    def __init__(
        self,
        seed=0,
        error_burst=None,
        latency_shift=None,
        saturation=None,
        members=2,
        errorless=(),
        born=None,
    ):
        ms = tuple(DiscoveredMetric(n, t, None, None) for n, t in METRICS)
        hist = {n: "classic" for n, t in METRICS if t == "histogram"}
        super().__init__(
            name="default", n_series=members,
            discovery=Discovery(ms, ("service_name", "instance"), hist, None, 1.0, (), False),
        )  # fmt: skip
        self.seed, self.members = seed, members
        self.burst, self.shift, self.sat = error_burst, latency_shift, saturation
        # members that never had an error: no error series (born on the first error, like
        # span-metrics STATUS_CODE_ERROR or a {code="500"} child)
        self.errorless = errorless
        # member -> minute: its error series is born at that minute of now's window (lep)
        self.born = born or {}
        self.exprs: list[str] = []

    def _rng(self, t: int, m: int, salt: int, hourly=False):
        return np.random.default_rng([self.seed, t // (3_600_000 if hourly else MIN), m, salt])

    def _in(self, span, t):
        return span is not None and _at(span[0]) < t <= _at(span[1])

    def _req(self, t, m):
        lam = 50 * math.exp(self._rng(t, m, 1, True).normal(0, 0.05))
        return float(
            self._rng(t, m, 2).poisson(lam * 60 * math.exp(self._rng(t, m, 3).normal(0, 0.03)))
        )

    def _value(self, expr, t, m):
        if "5.." in expr:
            p = 0.002 * math.exp(
                self._rng(t, m, 4, True).normal(0, 0.1) + self._rng(t, m, 5).normal(0, 0.2)
            )
            if self._in(self.burst, t):
                p *= 10
            return float(self._rng(t, m, 6).binomial(int(self._req(t, m)), min(p, 1))) / 60
        if "requests_total" in expr:
            return self._req(t, m) / 60
        if "idle" in expr:
            if self._in(self.sat, t):
                return 0.97 + self._rng(t, m, 7).normal(0, 0.01)
            return (
                0.4 + self._rng(t, m, 8, True).normal(0, 0.03) + self._rng(t, m, 9).normal(0, 0.03)
            )
        if "pressure" in expr:
            q = 0.05 * math.exp(
                self._rng(t, m, 10, True).normal(0, 0.1) + self._rng(t, m, 11).normal(0, 0.15)
            )
            return q * (5 if self._in(self.sat, t) else 1)
        if "active_requests" in expr:
            return 7 + self._rng(t, m, 12).normal(0, 0.5)
        return 1.0

    async def fetch(self, expr, rng, step_ms):
        self.calls += 1
        self.exprs.append(expr)
        label = "instance" if "node_" in expr else "service_name"
        tss = list(range(rng.start_ms, rng.end_ms + 1, step_ms))
        ms = [m for m in range(self.members) if not ("5.." in expr and m in self.errorless)]
        labels = [{label: f"s{k}"} for k in ms]
        sids = [series_id(self.name, lb) for lb in labels]
        rows = [
            (t, sids[i], self._value(expr, t, m))
            for i, m in enumerate(ms)
            for t in tss
            if not ("5.." in expr and m in self.born and t <= _at(self.born[m]))
        ]
        buckets = pa.table(
            {"ts_ms": [r[0] for r in rows], "series_id": [r[1] for r in rows],
             "avg": [r[2] for r in rows], "min": [r[2] for r in rows], "max": [r[2] for r in rows],
             "count": [4] * len(rows)},
            schema=BUCKET_SCHEMA,
        )  # fmt: skip
        series = pa.table(
            {"series_id": sids, "labels": [labels_json(lb) for lb in labels]}, schema=SERIES_SCHEMA
        )
        return FetchResult(buckets, series)

    async def fetch_histogram(self, selector, by, rng, step_ms):
        self.calls += 1
        self.hist_selectors.append(selector)
        tss = range(rng.start_ms, rng.end_ms + 1, step_ms)
        groups = list(range(self.members)) if by else [None]
        result = []
        for g in groups:
            ms = range(self.members) if g is None else [g]
            per_t = {}
            for t in tss:
                cum = np.zeros(len(LES))
                for m in ms:
                    med = 0.08 * math.exp(
                        self._rng(t, m, 13, True).normal(0, 0.03)
                        + self._rng(t, m, 14).normal(0, 0.04)
                    )
                    if self.shift is not None and t > _at(self.shift):
                        med *= 1.6
                    cdf = [
                        _N.cdf(math.log(le / med) / 0.5) if math.isfinite(le) else 1.0 for le in LES
                    ]
                    probs = np.diff(np.r_[0.0, cdf])
                    cum += np.cumsum(self._rng(t, m, 15).multinomial(int(self._req(t, m)), probs))
                per_t[t] = cum
            lab = {} if g is None else {"service_name": f"s{g}"}
            for i, le in enumerate(LES):
                result.append({
                    "metric": {**lab, "le": "+Inf" if not math.isfinite(le) else f"{le:g}"},
                    "values": [[t / 1000, str(per_t[t][i])] for t in tss],
                })  # fmt: skip
        return from_matrix(self.name, result, expr=histogram_expr(selector, by, step_ms))


async def _svc(tmp_path, **kw):
    svc = make_service(tmp_path, ScenarioSource(**kw))
    await svc.learn("default")
    return svc


RANGE = {"start": "now-1h", "end": "now", "step": "1m"}


async def test_red_verdict_orders_a_latency_shift_before_an_error_burst(tmp_path):
    svc = await _svc(tmp_path, seed=3, latency_shift=20, error_burst=(40, 47))
    out = await svc.binding_verdict(
        source="default", suggestion="RED:otel_http", reference="previous", **RANGE
    )
    roles = out["roles"]
    assert roles["duration"]["status"] == roles["errors"]["status"] == "changed"
    assert roles["rate"]["status"] == "no_change"
    assert roles["duration"]["direction"] == "higher" and roles["errors"]["pattern"] == "burst"
    assert out["summary"]["first"] == "duration"
    assert out["summary"]["moved"][:2] == ["duration", "errors"]
    assert out["summary"]["text"].startswith("First duration higher")
    assert out["reference"]["scheme"] == "previous" and len(out["reference"]["windows"]) == 4
    assert out["family"]["roles_judged"] == 3 and out["family"]["per_detector"] == pytest.approx(
        0.05 / 6, rel=1e-3
    )
    # latency: a threshold from the reference histograms, never a percentile
    th = roles["duration"]["threshold"]
    assert th["x"] == 0.25 and th["now_share"] > th["reference_share"]
    src = svc.sources.get("default")
    assert src.hist_selectors and not any("quantile" in e for e in src.exprs)
    # evidence: statistics citing the role datasets, unflagged over clean source data
    ev = {e["name"]: e for e in roles["errors"]["evidence"]}
    assert {"errors_odds_ratio_vs_reference", "errors_share_now", "errors_onset_ms"} <= set(ev)
    assert ev["errors_odds_ratio_vs_reference"]["dataset"] == roles["errors"]["dataset"]
    on = ev["errors_onset_ms"]
    assert on["interval"][0] <= _at(40) <= on["interval"][1]
    assert all(
        "input_uncertainty" not in e["params"]
        for r in roles.values()
        for e in r.get("evidence", [])
    )
    assert "input_uncertainty_unknown" not in out["caveats"]
    # spec §5.4: moved roles are special causes, the unchanged one sits in the envelope
    assert roles["duration"]["source"] == roles["errors"]["source"] == "special_cause"
    assert roles["rate"]["source"] == "common_cause"
    assert ev["errors_odds_ratio_vs_reference"]["source"] == "special_cause"
    assert ev["errors_onset_ms"]["source"] == "special_cause"
    assert "source" not in ev["errors_share_now"]  # a level, not a variation
    assert "special cause" in out["summary"]["text"]
    assert {(v["role"], v["source"]) for v in out["variation"]} >= {
        ("duration", "special_cause"), ("errors", "special_cause"), ("rate", "common_cause"),
    }  # fmt: skip


async def test_quiet_red_verdict_flags_nothing(tmp_path):
    svc = await _svc(tmp_path, seed=5)
    out = await svc.binding_verdict(
        source="default", suggestion="RED:otel_http", reference="previous", **RANGE
    )
    assert {r["status"] for r in out["roles"].values()} == {"no_change"}
    assert out["summary"]["moved"] == [] and out["summary"]["text"].startswith("No golden signal")
    assert {v["source"] for v in out["variation"]} == {"common_cause"}


async def test_a_change_on_data_with_measurement_issues_is_source_undetermined(
    tmp_path, monkeypatch
):
    from telemetry_nerd.core import verdict_ops

    svc = await _svc(tmp_path, seed=3, error_burst=(40, 47))
    real = verdict_ops.measurement_caveats
    # every role's data comes from a partial fetch
    monkeypatch.setattr(verdict_ops, "measurement_caveats", lambda meta: [*real(meta), "partial"])
    out = await svc.binding_verdict(
        source="default", suggestion="RED:otel_http", reference="previous", **RANGE
    )
    e = out["roles"]["errors"]
    assert e["status"] == "changed" and e["source"] == "undetermined"
    assert {v["source"] for v in e["variation"]} == {"undetermined", "measurement_system"}
    assert "source undetermined" in e["text"]
    assert out["roles"]["rate"]["source"] == "common_cause"  # no change: still the envelope


async def test_absent_error_series_read_as_zero_is_a_stated_measurement_assumption(tmp_path):
    """vv0: a member without an error series counts 0 errors where its requests report (born
    on the first error, as analyze reads it via born_counters): the role carries the
    absent_as_zero caveat and a measurement-system item, but the burst is still a special cause
    (a stated assumption, not untrusted data)."""
    svc = await _svc(tmp_path, seed=3, error_burst=(40, 47), members=3, errorless=(2,))
    out = await svc.binding_verdict(
        source="default", suggestion="RED:otel_http", reference="previous", **RANGE
    )
    e = out["roles"]["errors"]
    assert e["status"] == "changed" and e["source"] == "special_cause"
    assert "absent_as_zero" in e["caveats"] and "absent_as_zero" in out["caveats"]
    [item] = [v for v in e["variation"] if v.get("caveat") == "absent_as_zero"]
    assert item["source"] == "measurement_system" and "live sibling" in item["finding"]
    assert any("without an error series counted as 0 errors" in n for n in e["notes"])
    assert "absent_as_zero" not in out["roles"]["rate"].get("caveats", [])


async def test_error_series_born_inside_now_counts_0_before_its_first_point(tmp_path):
    """lep: the error series of s0 is born at minute 20 of now's window. Its 20 earlier steps
    count 0 errors where requests report (as analyze reads them), not gaps lost from both sides:
    the same burst is judged as with the series present throughout, disclosed as absent_as_zero."""
    (tmp_path / "born").mkdir()
    (tmp_path / "full").mkdir()
    born = await _svc(tmp_path / "born", seed=3, error_burst=(40, 47), born={0: 20})
    full = await _svc(tmp_path / "full", seed=3, error_burst=(40, 47))
    kw = {"source": "default", "suggestion": "RED:otel_http", "reference": "previous", **RANGE}
    b = (await born.binding_verdict(**kw))["roles"]["errors"]
    f = (await full.binding_verdict(**kw))["roles"]["errors"]
    assert b["status"] == f["status"] == "changed" and b["source"] == "special_cause"
    assert "absent_as_zero" in b["caveats"]
    assert any("born inside the window: " in n for n in b["notes"])
    # the steps before birth are counted (as 0), not excluded from both sides
    assert not any("excluded, not counted as 0" in n for n in b.get("notes", []))


async def test_error_series_present_everywhere_carries_no_absent_as_zero(tmp_path):
    svc = await _svc(tmp_path, seed=3, error_burst=(40, 47))
    out = await svc.binding_verdict(
        source="default", suggestion="RED:otel_http", reference="previous", **RANGE
    )
    e = out["roles"]["errors"]
    assert "absent_as_zero" not in e.get("caveats", [])
    assert not any(v.get("caveat") == "absent_as_zero" for v in e["variation"])


async def test_use_saturation_episode_is_at_capacity_and_annotates_the_group(tmp_path):
    svc = await _svc(tmp_path, seed=7, saturation=(20, 40))
    svc.ws.binding_accept("default", "USE:node_cpu", basis="test", overrides={"errors": None})
    g = await svc.show_binding(source="default", kind="USE", key="node:cpu", **RANGE)
    out = await svc.binding_verdict(group=g.id, reference="previous")
    roles = out["roles"]
    u, s = roles["utilization"], roles["saturation"]
    assert u["status"] == s["status"] == "changed" and u.get("at_capacity")
    assert u["near_bound"]["bound"] == 1.0 and u["near_bound"]["runs"]
    assert u["members"]["judged"] == 2 and len(u["members"]["changed"]) == 2
    assert roles["errors"]["status"] == "gap"
    assert out["group"] == g.id
    g2 = svc.ws.group_get(g.id)
    rv = {r.role: r.verdict for r in g2.roles}
    assert rv["utilization"]["status"] == "changed" and rv["utilization"]["at_capacity"]
    assert rv["utilization"]["source"] == "special_cause"  # the group card's badge reads it
    assert rv["errors"] is None  # the gap card stays a gap
    assert g2.verdict["moved"] and g2.verdict["reference"] == "previous windows"
    snap = svc.ws.snapshot()
    assert snap["groups"][0]["verdict"]["text"] == out["summary"]["text"]


async def test_reference_profile_needs_a_seasonal_profile_and_day_takes_seven_days(tmp_path):
    svc = await _svc(tmp_path, seed=1)
    with pytest.raises(ValueError, match="operating_profile"):
        await svc.binding_verdict(
            source="default", suggestion="RED:otel_http", reference="profile", **RANGE
        )
    out = await svc.binding_verdict(
        source="default", suggestion="RED:otel_http", reference="day", **RANGE
    )
    assert out["reference"]["scheme"] == "1d" and len(out["reference"]["windows"]) == 7
    from telemetry_nerd.model.time import iso

    assert out["reference"]["windows"][0][1] == iso(NOW - 86_400_000)  # a day before now's end
    with pytest.raises(ValueError, match="reference"):
        await svc.binding_verdict(
            source="default", suggestion="RED:otel_http", reference="nope", **RANGE
        )


async def test_littles_law_binding_carries_the_model_check_on_concurrency(tmp_path):
    svc = await _svc(tmp_path, seed=2)
    out = await svc.binding_verdict(
        source="default", suggestion="littles_law:otel_http", reference="previous", **RANGE
    )
    c = out["roles"]["concurrency"]
    assert "model_check" in c
    mc = c["model_check"]
    assert "error" in mc or (mc["verdict"] and "not a test in this family" in mc["note"])
    if "error" not in mc:
        assert mc["variation"] and all(
            v in out["variation"] for v in ({**x, "role": "concurrency"} for x in mc["variation"])
        )
    assert set(out["roles"]) == {"arrival_rate", "latency", "concurrency"}


async def test_mcp_binding_verdict(tmp_path):
    svc = await _svc(tmp_path, seed=3, error_burst=(30, 40))
    mcp = build_mcp(svc, "http://ui")
    async with Client(mcp) as c:
        res = await c.call_tool(
            "binding_verdict",
            {"suggestion": "RED:otel_http", "range": "1h", "step": "1m", "reference": "previous"},
        )
    out = json.loads(res.content[0].text)
    assert out["roles"]["errors"]["status"] == "changed"
    assert out["summary"]["moved"][0] == "errors"


# --- principle 16 (8jjy): a count change is labelled only when it holds under the cautious
# dispersion ------------------------------------------------------------------------------------
def test_counts_flagged_only_under_the_median_dispersion_do_not_hold_under_the_cautious():
    """Three reference windows at independence, one far noisier (6 quiet steps, 6 busy, in
    turn: phi ~ 8 on its blocks): the typical model (median phi) flags now's doubled level, the
    cautious one (the noisiest reference window) does not, so the change holds only under the
    optimistic model. Blocks hold >= 5 clusters under the cautious phi (3 steps here)."""
    req = np.full(60, 3000.0)
    noisy = np.where(np.arange(60) % 12 < 6, 2.0, 10.0)
    refs = [(np.full(60, 6.0), req)] * 3 + [(noisy, req)]
    j = judge_counts(
        (np.full(60, 12.0), req), refs, T, STEP_MS, alpha_level=0.0083, alpha_episode=0.0083
    )
    assert j.status == "changed" and j.level.flagged and not j.episodes
    dp = j.dispersion
    assert j.block == 3 and dp.source == "reference_max"
    assert max(dp.reference) == pytest.approx(8.4, abs=0.1)  # phi; x tau ~ 1.1 in typical, cautious
    assert dp.typical < 1.2 and dp.cautious == pytest.approx(max(dp.reference) * dp.tau)
    assert j.level_cautious.p > j.level.p and not j.level_cautious.flagged
    assert not j.holds_cautious


def test_clustered_errors_raise_no_episode_at_the_family_alpha():
    """8jjy: errors in negative-binomial clusters (size ~5, heterogeneous across windows) in
    now and the reference: per-step blocks left skewed residuals that drifted the CUSUM (14%
    false alarms); blocks of >= 5 clusters under the cautious phi keep it at the family alpha."""
    sc = Scenario(error_cluster=5, cluster_sd=0.5)
    n = 150
    flagged = sum(
        judge_roles(red_inputs(cycles(s, sc, null=sc)), T, STEP_MS)[0]["errors"].judgement.status
        == "changed"
        for s in range(n)
    )
    assert flagged / n <= 0.05 + 2.6 * math.sqrt(0.05 * 0.95 / n)


def test_counts_after_an_all_zero_reference_take_the_judged_window_as_the_cautious_bound():
    """8jjy: no reference events -> the typical model is independence (phi = 1); a burst of
    2000 errors in 4 minutes is also one clustered episode, which the judged window's own
    dispersion (the burst included: conservative) allows for. Spread evenly, the same errors are
    no more dispersed than independence, so the change holds under both."""
    req = np.full(60, 3000.0)
    zero = (np.zeros(60), req)
    burst = np.zeros(60)
    burst[30:34] = 500.0
    even = np.random.default_rng(3).poisson(2000 / 60, 60).astype(float)
    for now, holds in ((burst, False), (even, True)):
        j = judge_counts((now, req), [zero] * 7, T, STEP_MS, alpha_level=0.0083,
                         alpha_episode=0.0083)  # fmt: skip
        assert j.status == "changed" and j.dispersion.typical == 1.0 and not j.dispersion.reference
        assert j.holds_cautious is holds
        if holds:
            assert j.dispersion.source == "poisson_floor"
        else:
            assert j.dispersion.source == "judged" and j.dispersion.cautious > 100


def test_a_count_change_only_the_typical_model_sees_is_undetermined_with_both_p():
    from types import SimpleNamespace

    from telemetry_nerd.analysis.verdicts import RoleResult
    from telemetry_nerd.core.binding_view import RolePlan
    from telemetry_nerd.core.verdict_ops import VerdictOps, _Role

    req = np.full(60, 3000.0)
    zero = (np.zeros(60), req)
    burst = np.zeros(60)
    burst[30:34] = 500.0
    j = judge_counts((burst, req), [zero] * 7, T, STEP_MS, alpha_level=0.0083,
                     alpha_episode=0.0083)  # fmt: skip
    ds = SimpleNamespace(record_statistics=lambda _: None, meta=lambda _: None)
    ops = VerdictOps(SimpleNamespace(datasets=ds))  # type: ignore[arg-type]
    plan = RolePlan("errors", "errors_total", "error_ratio", "errors?")
    d = ops._wire("errors", _Role(plan), RoleResult(j, 0.0083, 0.0083), 7, T, STEP_MS)
    assert d["status"] == "changed" and d["source"] == "undetermined"
    assert d["label_rests_on"] == "cautious"
    m = d["models"]
    assert m["typical"]["flagged"] and not m["cautious"]["flagged"]
    assert m["cautious"]["dispersion_source"] == "judged"
    assert "independent events" in m["typical"]["assumes"]
    assert "cautious" in d["method"] and "undetermined" in d["method"]
    ev = d["evidence"][0]
    assert ev["source"] == "undetermined" and "p_cautious" in ev["params"]
    assert "only under the typical dispersion model" in d["variation"][0]["finding"]
