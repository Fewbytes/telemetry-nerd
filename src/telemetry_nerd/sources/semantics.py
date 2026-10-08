"""Per-backend missing-data semantics (spec 2026-10-02 §6; evidence in docs/data-source-quirks.md).

Pure data: what each metrics backend does at the edges of its data, so `bucket_state` can
decide what `ok` may mean. Every property is a `Fact` that says how well it is established:

* VERIFIED   reproduced here; `evidence` lists recorded fixtures (ids relative to
             tests/fixtures/, without `.json`) and tests/unit/test_missing_data_fixtures.py pins it
* DOCUMENTED upstream docs say so; `evidence` lists URLs; not reproduced
* UNKNOWN    no evidence; consumers must treat the property as "cannot tell"

Consumers must not trust a property below VERIFIED to produce `ok`; see `Fact.trusted`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal


class Status(StrEnum):
    VERIFIED = "verified"
    DOCUMENTED = "documented"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Fact[T]:
    value: T
    status: Status
    evidence: tuple[str, ...] = ()

    @property
    def trusted(self) -> bool:
        return self.status is Status.VERIFIED


def verified[T](value: T, *fixtures: str) -> Fact[T]:
    return Fact(value, Status.VERIFIED, fixtures)


def documented[T](value: T, *urls: str) -> Fact[T]:
    return Fact(value, Status.DOCUMENTED, urls)


def unknown[T](value: T) -> Fact[T]:
    """Best current guess, explicitly not evidence."""
    return Fact(value, Status.UNKNOWN)


@dataclass(frozen=True)
class GapFill:
    """How far an instant selector evaluation reaches back for a sample (filling gaps)."""

    kind: Literal["fixed", "adaptive"]
    #: fixed: the lookback delta; adaptive: None (depends on the detected scrape interval)
    max_ms: int | None
    #: a gap of exactly `max_ms` is NOT filled (window is left-open: (t - max, t])
    exclusive: bool = True


@dataclass(frozen=True)
class Tier:
    """A downsampled resolution tier served by the same endpoint."""

    resolution_ms: int
    #: how the tier is requested (Thanos: `max_source_resolution` query parameter)
    request: str
    #: `count_over_time` counts downsampled points, not raw samples
    count_is_downsampled_points: bool = True


@dataclass(frozen=True)
class LimitError:
    kind: Literal["max_points", "max_samples", "max_series", "range_too_long", "timeout"]
    http_status: int
    pattern: str  # regex on the error message

    def matches(self, message: str) -> bool:
        return re.search(self.pattern, message, re.IGNORECASE) is not None


@dataclass(frozen=True)
class MissingDataSemantics:
    backend: str
    #: gap filling of instant selector evaluation (raw `query_range` of a selector)
    gap_fill: Fact[GapFill]
    #: `*_over_time(x[w])` windows are left-open (t - w, t]; samples on the left edge are excluded
    window_left_open: Fact[bool]
    #: `*_over_time` / `rollup` over a window with no samples yields no point (not 0, not filled)
    empty_window: Fact[Literal["absent", "zero"]]
    #: `count_over_time((expr)[w:res])` over a non-selector expression: subquery steps are
    #: instant evaluations, so lookback fills gaps and the count overstates observed samples
    subquery_fills_gaps: Fact[bool]
    #: a failed scrape / vanished series writes a staleness marker that ends the series at once
    stale_marker_on_scrape_failure: Fact[bool]
    #: pushed samples (remote write / OTLP) carry staleness markers (False: a stopped sender
    #: leaves the series evaluating for the whole gap_fill window)
    push_stale_markers: Fact[bool]
    #: staleness markers are visible in raw range-vector responses (`x[w]`) as NaN samples;
    #: query_range / *_over_time results never contain them
    stale_marker_visible: Fact[bool]
    #: `increase`/`rate` over a window: extrapolated to the window edges, or exact using the
    #: sample before the window
    rate_edge: Fact[Literal["extrapolate", "previous_sample"]]
    #: first window after a gap carries the whole gap's change (fake spike): increase,
    #: increase_pure, delta (every window reaching back over the gap) and idelta (raw sample,
    #: first bucket only); rate/irate/deriv/rate_over_sum do not (vm__pg_* fixtures)
    post_gap_increase_spike: Fact[bool]
    #: `increase`/`rate` need at least two samples inside a window shorter than the interval
    rate_needs_two_samples_in_window: Fact[bool]
    #: a counter reset between the last sample of one step window and the first of the next is
    #: seen by `resets(x[step])` (False: window = step misses boundary resets)
    reset_visible_in_step_window: Fact[bool]
    #: downsampled tiers served by this endpoint (empty: none)
    tiers: Fact[tuple[Tier, ...]]
    #: the response says which resolution answered
    resolution_reported: Fact[bool]
    #: HTTP-visible marker of a partial response; None if the backend gives no signal
    partial_response_signal: Fact[str | None]
    #: replicas are merged server side, so a gap in one replica is invisible
    dedup_hides_replica_gaps: Fact[bool]
    #: a range entirely before retention returns an empty success (not an error)
    retention_edge: Fact[Literal["empty", "error"]]
    #: out-of-order samples within one series on write
    out_of_order_write: Fact[Literal["dropped", "accepted"]]
    #: query limits and how they surface
    limit_errors: Fact[tuple[LimitError, ...]]
    notes: tuple[str, ...] = field(default=())

    def facts(self) -> dict[str, Fact]:
        return {k: v for k, v in self.__dict__.items() if isinstance(v, Fact)}

    def unverified(self) -> list[str]:
        return [k for k, f in self.facts().items() if not f.trusted]

    def summary(self) -> list[str]:
        """One line per property for the source card / provenance footer (spec §6 rule 3)."""
        g = self.gap_fill.value
        fill = (
            f"fills gaps up to {g.max_ms // 1000}s"
            if g.kind == "fixed" and g.max_ms
            else "fills gaps up to about one series interval"
        )
        edge = (
            "rate/increase extrapolate to query-window edges"
            if self.rate_edge.value == "extrapolate"
            else "increase/delta/idelta use the previous sample (post-gap spike)"
            if self.post_gap_increase_spike.value
            else "rate/increase use the previous sample"
        )
        lines = [
            f"{self.backend}: raw selector {fill}; range functions (*_over_time) never fill",
            edge,
            "scrape failures end series at once"
            if self.stale_marker_on_scrape_failure.value
            else "no staleness markers",
        ]
        if not self.push_stale_markers.value:
            lines.append(
                "pushed (OTLP/remote-write) series linger for the fill window after a stop"
            )
        if self.tiers.value:
            lines.append(
                "downsampled tiers: "
                + ", ".join(f"{t.resolution_ms // 60000}m" for t in self.tiers.value)
            )
        if bad := self.unverified():
            lines.append(f"unverified: {', '.join(bad)}")
        return lines


# --- evidence ids (tests/fixtures/<id>.json) ---------------------------------------------------
_P = "missing-data/prometheus"
_V = "missing-data/victoriametrics"
_T = "missing-data/thanos"
_M = "missing-data/mimir"
_DOC_PROM_API = "https://prometheus.io/docs/prometheus/latest/querying/api/#format-overview"
_DOC_PROM_STALE = "https://prometheus.io/docs/prometheus/latest/querying/basics/#staleness"
_DOC_PROM_LEFT_OPEN = (
    "https://prometheus.io/docs/prometheus/latest/migration/"
    "#range-selectors-and-lookback-exclude-samples-coinciding-with-the-left-boundary"
)
_DOC_THANOS_PARTIAL = "https://thanos.io/tip/components/query.md/#partial-response"
_DOC_MIMIR_LIMITS = (
    "https://grafana.com/docs/mimir/latest/references/http-api/#error-codes"
    "  (err-mimir-max-series-per-query, err-mimir-max-query-length)"
)
_DOC_MIMIR_OOO = (
    "https://grafana.com/docs/mimir/latest/configure/configure-out-of-order-samples-ingestion/"
)

_PROM_LIMITS = (
    LimitError("max_points", 400, r"exceeded maximum resolution of [\d,]+ points per timeseries"),
    LimitError("max_samples", 422, r"too many samples into memory"),
)
_MIMIR_LIMITS = (
    *_PROM_LIMITS[:1],
    LimitError(
        "max_series",
        422,
        r"err-mimir-max-(series|chunks)-per-query|maximum number of (series|chunks)",
    ),
    LimitError(
        "range_too_long", 422, r"err-mimir-max-query-length|query time range exceeds the limit"
    ),
)
_VM_LIMITS = (
    LimitError("max_points", 422, r"too many points for the given start"),
    LimitError("max_series", 422, r"number of matching timeseries exceeds"),
    LimitError("max_samples", 422, r"search\.maxSamplesPerQuery|too many samples"),
)

PROMETHEUS = MissingDataSemantics(
    backend="prometheus",
    gap_fill=verified(
        GapFill("fixed", 300_000),
        f"{_P}/prom__gapfill_i15_raw",
        f"{_P}/prom__gapfill_i15_samples",
        f"{_P}/prom__gapfill_i60_raw",
        f"{_P}/prom__gapfill_i60_samples",
    ),
    window_left_open=verified(True, f"{_P}/prom__count_w15"),
    empty_window=verified("absent", f"{_P}/prom__count_selector_w60"),
    subquery_fills_gaps=verified(
        True, f"{_P}/prom__count_subquery_w60", f"{_P}/prom__count_selector_w60"
    ),
    stale_marker_on_scrape_failure=verified(
        True, f"{_P}/prom__scrape_gauge_raw", f"{_P}/prom__scrape_up"
    ),
    push_stale_markers=verified(False, f"{_P}/prom__scrape_rw_gauge_raw"),
    stale_marker_visible=verified(False, f"{_P}/prom__scrape_gauge_samples"),
    rate_edge=verified("extrapolate", f"{_P}/prom__increase_w300", f"{_P}/prom__increase_w30"),
    post_gap_increase_spike=verified(False, f"{_P}/prom__increase_w15", f"{_P}/prom__increase_w60"),
    rate_needs_two_samples_in_window=verified(
        True, f"{_P}/prom__increase_w15", f"{_P}/prom__rate_w15"
    ),
    reset_visible_in_step_window=verified(
        False, f"{_P}/prom__resets_w15", f"{_P}/prom__resets_w30"
    ),
    tiers=documented((), _DOC_PROM_STALE),
    resolution_reported=documented(False, _DOC_PROM_API),
    partial_response_signal=documented("warnings", _DOC_PROM_API),
    dedup_hides_replica_gaps=documented(False, _DOC_PROM_STALE),
    retention_edge=verified("empty", f"{_P}/prometheus-demo__retention_edge"),
    out_of_order_write=verified("dropped", f"{_P}/prom__ooo_write"),
    limit_errors=verified(
        _PROM_LIMITS,
        f"{_P}/prom__limit_points",
        f"{_P}/prom-limits__limit_max_samples",
    ),
    notes=(
        "Prometheus 3.x: query windows and lookback are left-open, so a gap of exactly 5 m is not filled",
        (
            "a reset between the last sample of one query bucket and the first of the next is "
            "invisible to resets(x[step]); use a query window wider than the step to see it"
        ),
    ),
)

THANOS = MissingDataSemantics(
    backend="thanos",
    gap_fill=verified(
        GapFill("fixed", 300_000, exclusive=False),  # 0.32.5: a gap of exactly 300 s IS filled
        f"{_T}/cern-eos__eos_ended_samples",
        f"{_T}/cern-eos__eos_ended_raw",
    ),
    # Thanos 0.32 (CERN EOS) lookback is inclusive (Prometheus 2.x); 0.38 (Wikimedia) untested
    window_left_open=unknown(True),
    empty_window=documented("absent", _DOC_PROM_STALE),
    subquery_fills_gaps=documented(True, _DOC_PROM_STALE),
    stale_marker_on_scrape_failure=verified(
        True, f"{_T}/wikimedia-raw__wm_ended_samples", f"{_T}/wikimedia-raw__wm_ended_raw"
    ),
    push_stale_markers=unknown(False),
    stale_marker_visible=verified(False, f"{_T}/wikimedia-raw__wm_ended_samples"),
    rate_edge=verified(
        "extrapolate",
        f"{_T}/wikimedia-raw__engine_samples",
        f"{_T}/wikimedia-raw__engine_increase_2m",
        f"{_T}/cern-eos__engine_samples",
        f"{_T}/cern-eos__engine_increase_2m",
    ),
    post_gap_increase_spike=documented(False, _DOC_PROM_STALE),
    rate_needs_two_samples_in_window=verified(
        True,
        f"{_T}/wikimedia-raw__engine_count_30s",
        f"{_T}/wikimedia-raw__engine_increase_30s",
    ),
    reset_visible_in_step_window=documented(False, _DOC_PROM_STALE),
    tiers=verified(
        (
            Tier(300_000, "max_source_resolution=5m"),
            Tier(3_600_000, "max_source_resolution=1h"),
        ),
        f"{_T}/wikimedia-1h__wikimedia-1h_samples_res5m",
        f"{_T}/wikimedia-1h__wikimedia-1h_samples_res1h",
        f"{_T}/wikimedia-1h__wikimedia-1h_count_over_time_res5m",
        f"{_T}/wikimedia-1h__wikimedia-1h_count_over_time_res1h",
    ),
    resolution_reported=verified(False, f"{_T}/wikimedia-1h__wikimedia-1h_count_over_time_res5m"),
    partial_response_signal=documented("warnings", _DOC_THANOS_PARTIAL),
    dedup_hides_replica_gaps=verified(
        True, f"{_T}/wikimedia-raw__dedup_on", f"{_T}/wikimedia-raw__dedup_off"
    ),
    retention_edge=verified("empty", f"{_T}/wikimedia-raw__retention_edge"),
    out_of_order_write=unknown("dropped"),
    limit_errors=verified(
        _PROM_LIMITS[:1],
        f"{_T}/wikimedia-raw__step_limit_over",
        f"{_T}/cern-eos__step_limit_over",
    ),
    notes=(
        "a downsample datasource still serves raw data unless max_source_resolution is sent",
        "count_over_time on a tier counts downsampled points (12 per 1 h on 5 m), not raw samples",
        "min/max/avg from a tier differ from raw at window edges",
        "limit errors arrive as text/plain on some proxies (Wikimedia), JSON on others (CERN)",
        "/metadata returns a different subset per call",
    ),
)

MIMIR = MissingDataSemantics(
    backend="mimir",
    gap_fill=verified(
        GapFill("fixed", 300_000),
        f"{_M}/grafana-play__play_otlp_ended_samples",
        f"{_M}/grafana-play__play_otlp_ended_raw",
    ),
    window_left_open=documented(True, _DOC_PROM_LEFT_OPEN),
    empty_window=documented("absent", _DOC_PROM_STALE),
    subquery_fills_gaps=documented(True, _DOC_PROM_STALE),
    stale_marker_on_scrape_failure=verified(
        True,
        f"{_M}/grafana-play__grafana-play_scrape_ended_samples",
        f"{_M}/grafana-play__grafana-play_scrape_ended_raw",
    ),
    push_stale_markers=verified(
        False,
        f"{_M}/grafana-play__play_otlp_ended_samples",
        f"{_M}/grafana-play__play_otlp_ended_raw",
    ),
    stale_marker_visible=documented(False, _DOC_PROM_STALE),
    rate_edge=verified(
        "extrapolate",
        f"{_M}/grafana-play__engine_samples",
        f"{_M}/grafana-play__engine_increase_2m",
        f"{_M}/cern-openstack__engine_samples",
        f"{_M}/cern-openstack__engine_increase_2m",
    ),
    post_gap_increase_spike=documented(False, _DOC_PROM_STALE),
    rate_needs_two_samples_in_window=verified(
        True,
        f"{_M}/grafana-play__engine_count_30s",
        f"{_M}/grafana-play__engine_increase_30s",
    ),
    reset_visible_in_step_window=documented(False, _DOC_PROM_STALE),
    tiers=documented((), _DOC_MIMIR_LIMITS),
    resolution_reported=documented(False, _DOC_MIMIR_LIMITS),
    partial_response_signal=documented("warnings", _DOC_PROM_API),
    dedup_hides_replica_gaps=documented(
        True,
        "https://grafana.com/docs/mimir/latest/configure/configure-high-availability-deduplication/",
    ),
    retention_edge=verified(
        "empty", f"{_M}/grafana-play__retention_edge", f"{_M}/cern-openstack__retention_edge"
    ),
    out_of_order_write=documented("dropped", _DOC_MIMIR_OOO),
    limit_errors=Fact(
        _MIMIR_LIMITS,
        Status.VERIFIED,
        (f"{_M}/grafana-play__step_limit_over", f"{_M}/cern-openstack__step_limit_over"),
    ),
    notes=(
        "only the max-points limit was reproduced; series/chunks/length patterns are from docs",
        "no seam at query-frontend split boundaries for step-aligned queries",
    ),
)

VICTORIAMETRICS = MissingDataSemantics(
    backend="victoriametrics",
    gap_fill=verified(
        GapFill("adaptive", None),
        f"{_V}/vm__gapfill_i15_raw",
        f"{_V}/vm__gapfill_i15_samples",
        f"{_V}/vm__gapfill_i60_raw",
        f"{_V}/vm__gapfill_i60_samples",
        f"{_V}/vm__gapfill_i60_step10",
        f"{_V}/vm__gapfill_i60_step30",
        f"{_V}/vm__gapfill_i60_step60",
    ),
    window_left_open=verified(True, f"{_V}/vm__count_w15"),
    empty_window=verified("absent", f"{_V}/vm__count_selector_w60", f"{_V}/vm__rollup_w60"),
    subquery_fills_gaps=verified(
        True, f"{_V}/vm__count_subquery_w60", f"{_V}/vm__count_selector_w60"
    ),
    stale_marker_on_scrape_failure=verified(
        True, f"{_V}/vm__scrape_gauge_raw", f"{_V}/vm__scrape_up"
    ),
    push_stale_markers=verified(False, f"{_V}/vm__scrape_rw_gauge_raw"),
    stale_marker_visible=verified(True, f"{_V}/vm__scrape_gauge_samples"),
    rate_edge=verified("previous_sample", f"{_V}/vm__increase_w300", f"{_V}/vm__increase_w15"),
    post_gap_increase_spike=verified(
        True,
        f"{_V}/vm__increase_w15",
        f"{_V}/vm__increase_w60",
        f"{_V}/vm__increase_w300",
        f"{_V}/vm__pg_counter_delta_w75",
        f"{_V}/vm__pg_counter_idelta_w15",
        f"{_V}/vm__pg_counter_increase_pure_w75",
        f"{_V}/vm__pg_gauge_delta_w75",
    ),
    rate_needs_two_samples_in_window=verified(False, f"{_V}/vm__increase_w15"),
    reset_visible_in_step_window=verified(True, f"{_V}/vm__resets_w15", f"{_V}/vm__resets_w30"),
    tiers=documented((), "https://docs.victoriametrics.com/#downsampling  (enterprise only)"),
    resolution_reported=documented(False, "https://docs.victoriametrics.com/#downsampling"),
    partial_response_signal=verified(
        "isPartial",
        f"{_V}/vm-playground__shape_range",
        f"{_V}/percona-pmm__shape_range",
    ),
    dedup_hides_replica_gaps=unknown(False),
    retention_edge=verified(
        "empty", f"{_V}/percona-pmm__retention_edge", f"{_V}/vm-limits__retention_write"
    ),
    out_of_order_write=verified("accepted", f"{_V}/vm__ooo_write"),
    limit_errors=verified(
        _VM_LIMITS,
        f"{_V}/vm-limits__limit_points",
        f"{_V}/vm-limits__limit_series",
        f"{_V}/vm__limit_points_default",
    ),
    notes=(
        "gap fill window is roughly one detected series interval (15 s series: ~22 s, 60 s: ~66 s)",
        (
            "increase() inside a gap returns 0 for the first steps, then absent; the first step "
            "after the gap carries the whole gap's increase"
        ),
        (
            "samples older than -retentionPeriod are dropped silently (204) and counted in "
            "vm_rows_ignored_total"
        ),
        "duplicate timestamps are stored twice; reads pick one",
        "raw range vectors (x[w]) contain NaN staleness markers: scrape_interval() sees them",
        "single node: no isPartial field; cluster (select/N): isPartial true when a node is down",
    ),
)


@dataclass(frozen=True)
class ElasticsearchMissingDataSemantics:
    """What an Elasticsearch/OpenSearch source does at its data edges (spec: later-work #6;
    evidence in docs/data-source-quirks.md). Its query model is bucketed aggregation tiles, not
    PromQL instant/range-vector evaluation, so it does not reuse `MissingDataSemantics`'s
    PromQL-function fields (`gap_fill`, `rate_edge`, `subquery_fills_gaps`, ...): those describe
    behaviors this backend's query language has no equivalent of, and forcing a value onto them
    would misrepresent what was actually checked.

    Evidence here is reproduced per-run against a real Elasticsearch/OpenSearch testcontainer
    (`tests/integration/test_es_missing_data.py`), not static recorded fixtures: `Fact.evidence`
    lists the test ids that reproduce it.
    """

    backend: Literal["elasticsearch"]
    #: a query range entirely before the earliest document (or an index with no documents at all)
    #: answers 200 with empty aggregation buckets, never an error
    retention_edge: Fact[Literal["empty", "error"]]
    #: a query bucket between the first and last non-empty one with no matching documents:
    #: `rate`/`field_rate` forms report 0/s (a document count of zero is an exact observation);
    #: `stats`/percentile forms report no row (absent). Leading/trailing empty buckets (before the
    #: first document or after the last) never appear at all: `min_doc_count: 0` without
    #: `extended_bounds` only fills gaps *between* non-empty buckets.
    empty_window: Fact[Literal["zero_or_absent_by_form"]]
    #: visibility lag: a document is not searchable until the index's next refresh
    #: (`index.refresh_interval`, default 1s). The adapter does not read this setting, probe it,
    #: or expose an ingest-lag number: a just-written document can be silently missing from the
    #: newest query bucket until the next refresh, and this looks identical to "no data yet".
    settling: Fact[Literal["refresh_interval_unobserved"]]
    #: HTTP-visible marker of a partial response: `_shards.failed > 0` and/or `timed_out: true` in
    #: the search response body; the adapter surfaces either as a `FetchResult.failed` /
    #: `DistResult.failed` span ("PartialResponse: ..."). The field shapes are confirmed against
    #: real clusters; an actual shard failure or query timeout was not reproduced (a healthy
    #: single-node test cluster never produces one on demand without destructive fault injection,
    #: and a synthetic low `timeout` did not reliably trigger `timed_out: true` either).
    partial_response_signal: Fact[Literal["shards_failed_or_timed_out"]]
    notes: tuple[str, ...] = field(default=())

    def facts(self) -> dict[str, Fact]:
        return {k: v for k, v in self.__dict__.items() if isinstance(v, Fact)}

    def unverified(self) -> list[str]:
        return [k for k, f in self.facts().items() if not f.trusted]


_ES = "tests/integration/test_es_missing_data.py"
_ES_RATE = "tests/integration/test_es_containers.py::test_rate_is_documents_per_second_with_an_interior_zero"
_ES_STATS = "tests/integration/test_es_containers.py::test_stats_is_the_mean_latency_with_its_count"

ELASTICSEARCH = ElasticsearchMissingDataSemantics(
    backend="elasticsearch",
    retention_edge=verified(
        "empty",
        f"{_ES}::test_retention_edge_is_empty_not_error",
    ),
    empty_window=verified(
        "zero_or_absent_by_form",
        _ES_RATE,
        _ES_STATS,
    ),
    settling=verified(
        "refresh_interval_unobserved",
        f"{_ES}::test_document_is_invisible_until_the_next_refresh",
    ),
    partial_response_signal=documented(
        "shards_failed_or_timed_out",
        "https://www.elastic.co/guide/en/elasticsearch/reference/current/search-search.html",
    ),
    notes=(
        (
            "reproduced against Elasticsearch 8.15.3 and OpenSearch 2.17.1 single-node test "
            "clusters; both speak the same search API so one profile serves both flavors"
        ),
        (
            "a healthy single-node test cluster cannot be made to fail a shard or time out on "
            "demand without destructive fault injection, so partial_response_signal's field "
            "shapes are confirmed live but an actual partial response was not reproduced here"
        ),
    ),
)

PROFILES: dict[str, MissingDataSemantics] = {
    p.backend: p for p in (PROMETHEUS, THANOS, MIMIR, VICTORIAMETRICS)
}
_FLAVOR_BACKEND = {"prometheus": "prometheus", "victoriametrics": "victoriametrics"}


def semantics_for(backend: str | None, flavor: str = "prometheus") -> MissingDataSemantics:
    """Profile of a registered backend; without one, the flavor's own engine."""
    if backend is not None:
        return PROFILES[backend]
    return PROFILES[_FLAVOR_BACKEND[flavor]]


def classify_limit_error(message: str) -> str | None:
    """Limit kind if `message` is a known server-side query limit (any backend), else None."""
    for profile in PROFILES.values():
        for err in profile.limit_errors.value:
            if err.matches(message):
                return err.kind
    return None
