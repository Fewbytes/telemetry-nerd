"""Counters born on their first event: absence read as 0 only where a live sibling reports
(eval finding telemetry-nerd-icm; payment-failure shape: span-metrics error series appear on
the first error span, the STATUS_CODE_UNSET sibling of the same service/span is continuous)."""

from __future__ import annotations

import asyncio

import numpy as np
import pyarrow as pa
import pytest

from telemetry_nerd.analysis.born_counters import (
    CAVEAT,
    born_counter,
    fill_absent,
    fill_born,
    sibling_counts,
    sibling_key,
)
from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult, labels_json
from telemetry_nerd.model.series import series_id as sid_of
from telemetry_nerd.model.time import TimeRange
from tests.unit.fakes import FakeSource, make_service

S = 15_000  # step: 15 s
N = 96  # 24 min at 15 s: 14 min before the fault, 5 min fault, 5 min settle
T0 = 1_791_021_600_000  # 2026-10-03T10:00:00Z
ONSET, END = 56, 76  # fault steps [56, 76): the error series exists for < half the window
CALLS = "traces_span_metrics_calls_total"
ERR = f'sum by (service_name) (rate({CALLS}{{status_code="STATUS_CODE_ERROR"}}[1m]))'
OK = f'sum by (service_name) (rate({CALLS}{{status_code!="STATUS_CODE_ERROR"}}[1m]))'
BY_STATUS = (
    f'sum by (status_code) (increase({CALLS}{{service_name="payment",span_name="charge"}}[1m]))'
)


def counter_type(metric: str) -> str | None:
    return "counter" if metric.endswith("_total") else "gauge"


# the rule ------------------------------------------------------------------------------------
def test_born_counter_reads_outcome_matchers_and_groupings():
    err = born_counter(ERR, counter_type)
    assert err is not None and err.outcome == ("status_code",) and err.complement == OK
    kept = born_counter(BY_STATUS, counter_type)
    assert kept is not None and kept.complement is None  # siblings are in the dataset
    regex = born_counter('rate(http_requests_total{code=~"5.."}[5m])', counter_type)
    assert regex is not None and regex.complement == 'rate(http_requests_total{code!~"5.."}[5m])'
    # summed over every outcome, a gauge, a ratio, a non-outcome matcher: absence is missing data
    assert born_counter(f"sum(rate({CALLS}[1m]))", counter_type) is None
    assert born_counter('rate(queue_depth{code="1"}[1m])', counter_type) is None
    assert born_counter(f"{ERR} / {OK}", counter_type) is None
    assert born_counter(f'sum(rate({CALLS}{{service_name="x"}}[1m]))', counter_type) is None


def test_sibling_key_drops_outcome_labels_only():
    assert sibling_key({"service_name": "payment", "status_code": "STATUS_CODE_ERROR"}) == (
        ("service_name", "payment"),
    )


def test_fill_absent_only_outside_the_observed_lifetime():
    alive = np.arange(10)
    ts = np.array([3, 4, 6, 7])  # born at 3, a gap at 5, last seen at 7
    f = fill_absent(ts, np.array([1.0, 2.0, 3.0, 4.0]), alive)
    assert list(f.ts) == [0, 1, 2, 3, 4, 6, 7, 8, 9]  # 5 stays a gap: present-but-gap
    assert list(f.y) == [0, 0, 0, 1, 2, 3, 4, 0, 0] and (f.lead, f.trail) == (3, 2)


def test_fill_needs_the_sibling_at_each_step():
    alive = np.array([2, 8, 9])  # the instrument was down at 0, 1 (sibling absent too)
    f = fill_absent(np.array([3, 4]), np.array([1.0, 1.0]), alive)
    assert list(f.ts) == [2, 3, 4, 8, 9] and (f.lead, f.trail) == (1, 2)


def test_counter_reset_by_a_restart_reads_as_zero_after_last_point():
    # a restart drops the lazily created error child; the sibling is re-created at once (it has
    # traffic), the error child only on the next error: absence after the last point is 0
    ts = np.arange(10)
    series = {
        "e": ({"status_code": "ERROR", "job": "a"}, ts[:6], np.ones(6)),
        "o": ({"status_code": "UNSET", "job": "a"}, ts, np.full(10, 5.0)),
        "other": ({"status_code": "UNSET", "job": "b"}, ts, np.full(10, 5.0)),
    }
    out, filled = fill_born(series)
    assert set(filled) == {"e"} and filled["e"].trail == 4 and filled["e"].lead == 0
    assert list(out["e"][2]) == [1.0] * 6 + [0.0] * 4


def test_no_sibling_no_fill():
    ts = np.arange(5, 10)
    out, filled = fill_born({"e": ({"status_code": "ERROR"}, ts, np.ones(5))})
    assert not filled and list(out["e"][1]) == list(ts)
    # a sibling of another identity (another service) proves nothing about this one
    out, filled = fill_born(
        {"e": ({"status_code": "ERROR", "svc": "a"}, ts, np.ones(5))},
        {"o": ({"status_code": "UNSET", "svc": "b"}, np.arange(10))},
    )
    assert not filled


def test_sibling_counts_sum_every_live_sibling_per_step():
    """The traffic a born series is a thinning of (0vg7): every other outcome of the same
    identity, in the dataset and fetched, summed per step; another identity never counts."""
    ts = np.arange(4)
    series = {
        "e": ({"status_code": "ERROR", "svc": "a"}, ts[2:], np.ones(2)),
        "u": ({"status_code": "UNSET", "svc": "a"}, ts, np.full(4, 3.0)),
        "x": ({"status_code": "UNSET", "svc": "b"}, ts, np.full(4, 50.0)),
    }
    fetched = {"o": ({"status_code": "OK", "svc": "a"}, ts[1:], np.full(3, 2.0))}
    got = sibling_counts(series, fetched)
    assert list(got["e"][0]) == [0, 1, 2, 3] and list(got["e"][1]) == [3.0, 5.0, 5.0, 5.0]
    assert list(got["u"][0]) == [1, 2, 3] and list(got["u"][1]) == [2.0, 3.0, 3.0]  # e + o
    assert "x" not in got  # no sibling of its own identity


# end to end: analyze ---------------------------------------------------------------------------
def _table(rows: list[tuple[dict, np.ndarray, np.ndarray]], source="default") -> FetchResult:
    ts_all, sid_all, y_all, sids, labs = [], [], [], [], []
    for labels, ts, y in rows:
        sid = sid_of(source, labels)
        sids.append(sid)
        labs.append(labels_json(labels))
        ts_all += [int(t) for t in ts]
        sid_all += [sid] * len(ts)
        y_all += [float(v) for v in y]
    n = len(ts_all)
    b = pa.table(
        {"ts_ms": ts_all, "series_id": sid_all, "avg": y_all, "min": y_all, "max": y_all,
         "count": [4] * n},
        schema=BUCKET_SCHEMA,
    )  # fmt: skip
    return FetchResult(b, pa.table({"series_id": sids, "labels": labs}, schema=SERIES_SCHEMA))


def _ok_rate(n, seed=1):
    return 3.0 + np.random.default_rng(seed).normal(scale=0.2, size=n)


def _err_rate(n, seed=2):
    return 2.0 + np.random.default_rng(seed).normal(scale=0.2, size=n)


class PaymentFailure(FakeSource):
    """The span-metrics shape of the payment-failure run: the payment error series exists only
    from the first error span (and stays at rate 0 after the fault); the non-error sibling is
    continuous. `down`: steps where the whole instrument (both series) is missing."""

    def __init__(self, down=(), sibling=True):
        super().__init__(name="default", n_series=1)
        self.down = set(down)
        self.sibling = sibling
        self.exprs: list[str] = []

    async def fetch(self, expr, rng, step_ms):
        self.calls += 1
        self.exprs.append(expr)
        ts = np.arange(rng.start_ms, rng.end_ms + 1, step_ms)
        k = (ts - T0) // S
        up = np.array([int(i) not in self.down for i in k])
        svc = {"service_name": "payment"}
        if expr == ERR:
            born = (k >= ONSET) & up
            y = np.where(k < END, _err_rate(ts.size), 0.0)  # rate 0 after: the child persists
            return _table([(svc, ts[born], y[born])])
        if expr == OK and self.sibling:
            return _table([(svc, ts[up], _ok_rate(ts.size)[up])])
        return _table([])

    fetch_values = fetch


def _query(svc, expr):
    rng = TimeRange(T0, T0 + (N - 1) * S)
    out = asyncio.run(svc.query(expr, start=str(rng.start_ms), end=str(rng.end_ms), step="15s"))
    return out["dataset"]


def test_analyze_reads_a_born_error_series_against_its_fetched_sibling(tmp_path):
    src = PaymentFailure()
    svc = make_service(tmp_path, source=src, clock=lambda: T0 + 2 * N * S)
    d = _query(svc, ERR)
    out = asyncio.run(svc.analyze_profiled(d))
    assert OK in src.exprs  # the live sibling was fetched: same range and step
    (s,) = out["series"]
    assert s["verdict"] == "level_shifted", s
    zero = s["absent_as_zero"]
    assert zero["before_first_point"] == ONSET and zero["after_last_point"] == 0
    assert zero["live_sibling"]["expr"] == OK and "assumption" in zero["assumption"]
    shift = s["stability"]["shifts"][0]
    assert shift["source"] == "special_cause" and shift["delta"] > 1
    assert shift["evidence"]["source"] == "special_cause"
    assert any(v.get("caveat") == CAVEAT and v["source"] == "measurement_system"
               for v in s["variation"])  # fmt: skip
    assert CAVEAT in out["caveats"]
    assert any(v.get("caveat") == CAVEAT for v in out["variation"])
    # the panel reads the series exactly as analyze did (the sibling is found in the store)
    panel = svc.diagnostics.panel(d, None, None)
    assert len(panel["series"][0]["ts"]) == N and CAVEAT in panel["caveats"]


def test_analyze_reads_in_dataset_siblings_without_fetching(tmp_path):
    svc = make_service(tmp_path)
    ts = T0 + np.arange(N) * S
    err = ({"status_code": "STATUS_CODE_ERROR"}, ts[ONSET:END], 60 * _err_rate(END - ONSET))
    ok = ({"status_code": "STATUS_CODE_UNSET"}, ts, 60 * _ok_rate(N))
    d = svc.datasets.put(
        source="default", expr=BY_STATUS, rng=TimeRange(T0, T0 + (N - 1) * S), step_ms=S,
        resolution_ms=15_000, result=_table([err, ok]),
    ).id  # fmt: skip
    out = svc.analyze(d)
    by = {s["labels"]["status_code"]: s for s in out["series"]}
    e = by["STATUS_CODE_ERROR"]
    assert e["absent_as_zero"]["before_first_point"] == ONSET
    assert e["absent_as_zero"]["after_last_point"] == N - END  # expired / not reported: 0
    assert e["verdict"] == "level_shifted", e  # the burst from zero: onset and end
    up, down = e["stability"]["shifts"]
    assert up["delta"] > 0 > down["delta"] and up["source"] == down["source"] == "special_cause"
    assert "no events" in e["absent_as_zero"]["spc"]  # all-zero baseline: no envelope
    assert "absent_as_zero" not in by["STATUS_CODE_UNSET"]


def test_instrument_down_stays_unknown(tmp_path):
    # both series missing until the fault: nothing proves the instrument was alive before it
    src = PaymentFailure(down=range(ONSET + 4))
    svc = make_service(tmp_path, source=src, clock=lambda: T0 + 2 * N * S)
    out = asyncio.run(svc.analyze_profiled(_query(svc, ERR)))
    (s,) = out["series"]
    assert s["verdict"] == "insufficient_data" and s["reasons"] == ["skipped: too_gappy"]
    assert CAVEAT not in out["caveats"]


def test_only_live_sibling_steps_are_read_as_zero(tmp_path):
    src = PaymentFailure(down=range(16))  # instrument down for the first 16 steps
    svc = make_service(tmp_path, source=src, clock=lambda: T0 + 2 * N * S)
    out = asyncio.run(svc.analyze_profiled(_query(svc, ERR)))
    (s,) = out["series"]
    assert s["absent_as_zero"]["before_first_point"] == ONSET - 16
    assert any(v.get("missing_steps") == 16 for v in s["variation"])  # still missing, not 0


def test_no_sibling_series_keeps_the_old_reading(tmp_path):
    src = PaymentFailure(sibling=False)
    svc = make_service(tmp_path, source=src, clock=lambda: T0 + 2 * N * S)
    d = _query(svc, ERR)
    (s,) = asyncio.run(svc.analyze_profiled(d))["series"]
    assert s["verdict"] == "insufficient_data" and s["reasons"] == ["skipped: too_gappy"]


def test_spectrum_names_the_unchecked_reading(tmp_path):
    src = PaymentFailure()
    svc = make_service(tmp_path, source=src, clock=lambda: T0 + 2 * N * S)
    d = _query(svc, ERR)
    with pytest.raises(ValueError, match="too_gappy"):
        svc.spectrum(d)
    prep = svc.signal._prepare(d, "spectrum", 4096, allow_empty=True)
    assert "live sibling" in prep.skipped[0]["hint"]
