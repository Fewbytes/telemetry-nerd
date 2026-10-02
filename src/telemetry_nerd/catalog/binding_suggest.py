"""Binding proposals (bead czt.1): which catalogued metrics fill the roles of a littles_law / RED /
USE model, so Claude can confirm one with `catalog_bind` instead of hand-assembling it.

Pure and deterministic: the input is what the catalog already knows about a source (metric names,
resolved type/unit/role/histogram_family fields, relations, existing bindings). Nothing is queried
from the source, so label names are *conventions*, never verified: every suggestion carries them
as hints (`join_on`) and says so.

Structure: a `Scope` is one instrumentation family that identifies one entity per label value
(OTel HTTP server -> a service, node_exporter CPU -> an instance, ...). It declares *signals*
(request_rate, request_latency, in_flight, utilization, ...) as ranked `Rule`s; each binding kind
maps its roles onto signals. Fixed scopes know exact names (knowledge packs); generic scopes
group `<prefix>_requests_total`-style names by prefix.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from telemetry_nerd.catalog.models import CatalogEntry
from telemetry_nerd.catalog.relations import (
    BINDING_ROLES,
    SUGGESTIONS,
    ResolvedBinding,
    ResolvedRelation,
    metric_slug,
)

#: a role is never reported above this: nothing here has looked at the data
MAX_CONFIDENCE = 0.9
#: penalty when the catalog does not know the metric's type (a pack/metadata claim would)
UNKNOWN_TYPE_PENALTY = 0.15
#: penalty when a capacity metric the expression divides by is not in the catalog
MISSING_NEED_PENALTY = 0.15
RELATION_BOOST = 0.05
ROLE_CLAIM_BOOST = 0.05
MIN_FILLED = 2

HIST = ("histogram",)
COUNTER = ("counter",)
GAUGE = ("gauge",)


@dataclass(frozen=True)
class Rule:
    """One way a metric can carry a signal. `pattern` fullmatches the metric name; a `p` group is
    the entity prefix of a generic scope. `{m}` in `expr` is the metric name."""

    pattern: str
    types: tuple[str, ...]
    confidence: float
    form: str  # counter_rate | histogram_count | histogram | label_split | gauge | ratio | summary
    expr: str
    basis: str = "pack"  # pack: a known exact name; naming: a generic convention
    note: str | None = None
    needs: tuple[str, ...] = ()
    #: expected catalog `role` claim (pack vocabulary) that corroborates the match
    role_claim: str | None = None


@dataclass(frozen=True)
class Scope:
    id: str
    label: str
    key: str  # default binding key (the entity kind), overridable by the caller
    join_on: tuple[str, ...]
    signals: Mapping[str, tuple[Rule, ...]]
    kinds: tuple[str, ...]
    generic: bool = False
    caveat: str | None = None


def R(pattern, types, conf, form, expr, **kw):
    return Rule(pattern, tuple(types), conf, form, expr, **kw)


def exact(*names: str) -> str:
    return "|".join(re.escape(n) for n in names)


#: signal -> role per binding kind (USE roles are signals themselves)
KIND_SIGNALS: dict[str, dict[str, str]] = {
    "RED": {"rate": "request_rate", "errors": "request_errors", "duration": "request_latency"},
    "littles_law": {
        "arrival_rate": "request_rate",
        "latency": "request_latency",
        "concurrency": "in_flight",
    },
    "USE": {"utilization": "utilization", "saturation": "saturation", "errors": "errors"},
}

_LABEL_UNVERIFIED = "label names are conventions; check the series' labels before using the expr"


def _otel_http_rules() -> dict[str, tuple[Rule, ...]]:
    svc = "service_name"
    count = f"sum by ({svc}) (rate({{m}}_count[5m]))"
    return {
        "request_rate": (
            R(
                exact("http_server_requests_total"),
                COUNTER,
                0.8,
                "counter_rate",
                f"sum by ({svc}) (rate({{m}}[5m]))",
            ),
            R(
                exact("http_server_request_duration_seconds"),
                HIST,
                0.75,
                "histogram_count",
                count,
                note="rate of the histogram's observation count",
            ),
            R(
                exact("http_server_duration_seconds", "http_server_duration_milliseconds"),
                HIST,
                0.6,
                "histogram_count",
                count,
                note="pre-stable semconv name",
            ),
        ),
        "request_errors": (
            R(
                exact("http_server_requests_total"),
                COUNTER,
                0.5,
                "label_split",
                f'sum by ({svc}) (rate({{m}}{{http_response_status_code=~"5.."}}[5m]))',
                note=_LABEL_UNVERIFIED,
            ),
            R(
                exact("http_server_request_duration_seconds"),
                HIST,
                0.45,
                "label_split",
                f'sum by ({svc}) (rate({{m}}_count{{http_response_status_code=~"5.."}}[5m]))',
                note=_LABEL_UNVERIFIED + "; errors are 5xx by the semconv status label",
            ),
        ),
        "request_latency": (
            R(
                exact("http_server_request_duration_seconds"),
                HIST,
                0.9,
                "histogram",
                "histogram",
                role_claim="latency",
            ),
            R(
                exact("http_server_duration_seconds"),
                HIST,
                0.7,
                "histogram",
                "histogram",
                note="pre-stable semconv name",
            ),
            R(
                exact("http_server_duration_milliseconds"),
                HIST,
                0.65,
                "histogram",
                "histogram",
                note="unit is milliseconds, not seconds",
            ),
        ),
        "in_flight": (
            R(
                exact("http_server_active_requests"),
                GAUGE,
                0.85,
                "gauge",
                f"sum by ({svc}) ({{m}})",
                note="semconv UpDownCounter: requests in flight",
            ),
        ),
    }


def _otel_rpc_rules() -> dict[str, tuple[Rule, ...]]:
    svc = "service_name"
    return {
        "request_rate": (
            R(
                exact("rpc_server_call_duration_seconds"),
                HIST,
                0.75,
                "histogram_count",
                f"sum by ({svc}) (rate({{m}}_count[5m]))",
                note="rate of the histogram's observation count",
            ),
            R(
                exact("rpc_server_duration_milliseconds"),
                HIST,
                0.6,
                "histogram_count",
                f"sum by ({svc}) (rate({{m}}_count[5m]))",
                note="pre-stable semconv name",
            ),
        ),
        "request_errors": (
            R(
                exact("rpc_server_call_duration_seconds"),
                HIST,
                0.5,
                "label_split",
                f'sum by ({svc}) (rate({{m}}_count{{rpc_grpc_status_code!="0"}}[5m]))',
                note=_LABEL_UNVERIFIED + "; gRPC status codes, other RPC systems differ",
            ),
            R(
                exact("rpc_server_duration_milliseconds"),
                HIST,
                0.45,
                "label_split",
                f'sum by ({svc}) (rate({{m}}_count{{rpc_grpc_status_code!="0"}}[5m]))',
                note=_LABEL_UNVERIFIED,
            ),
        ),
        "request_latency": (
            R(
                exact("rpc_server_call_duration_seconds"),
                HIST,
                0.9,
                "histogram",
                "histogram",
                role_claim="latency",
            ),
            R(
                exact("rpc_server_duration_milliseconds"),
                HIST,
                0.65,
                "histogram",
                "histogram",
                note="unit is milliseconds, not seconds",
            ),
        ),
    }


def _spanmetrics_rules() -> dict[str, tuple[Rule, ...]]:
    svc = "service_name"
    calls = exact("traces_spanmetrics_calls_total", "traces_span_metrics_calls_total")
    lat = exact(
        "traces_spanmetrics_latency",
        "traces_spanmetrics_duration_seconds",
        "traces_span_metrics_duration_seconds",
    )
    return {
        "request_rate": (
            R(calls, COUNTER, 0.85, "counter_rate", f"sum by ({svc}) (rate({{m}}[5m]))"),
            R(
                "calls_total",
                COUNTER,
                0.55,
                "counter_rate",
                f"sum by ({svc}) (rate({{m}}[5m]))",
                basis="naming",
                note="bare span-metrics-connector name: confirm it comes from spans",
            ),
        ),
        "request_errors": (
            R(
                calls,
                COUNTER,
                0.8,
                "label_split",
                f'sum by ({svc}) (rate({{m}}{{status_code="STATUS_CODE_ERROR"}}[5m]))',
                note="span status is the error signal; " + _LABEL_UNVERIFIED,
            ),
        ),
        "request_latency": (
            R(
                lat,
                HIST,
                0.85,
                "histogram",
                "histogram",
                role_claim="latency",
                note="span duration; on Grafana Play exposed only as a native histogram",
            ),
        ),
    }


_STEM = r"(?:(?P<p>.+?)_)?(?:http_)?"


def _generic_app_rules() -> dict[str, tuple[Rule, ...]]:
    return {
        "request_rate": (
            R(
                _STEM + r"(?:requests|calls|rpcs|queries)_total",
                COUNTER,
                0.6,
                "counter_rate",
                "sum(rate({m}[5m]))",
                basis="naming",
            ),
            R(
                _STEM + r"(?:request|response|call|rpc|handler)_(?:duration|latency)_"
                r"(?:seconds|milliseconds)",
                HIST,
                0.5,
                "histogram_count",
                "sum(rate({m}_count[5m]))",
                basis="naming",
                note="rate of the histogram's observation count",
            ),
        ),
        "request_errors": (
            R(
                _STEM + r"(?:request_|response_)?(?:errors|failures|failed_requests|failed)_total",
                COUNTER,
                0.55,
                "counter_rate",
                "sum(rate({m}[5m]))",
                basis="naming",
            ),
            R(
                _STEM + r"(?:requests|calls|rpcs)_total",
                COUNTER,
                0.4,
                "label_split",
                'sum(rate({m}{<status-label>=~"5.."}[5m]))',
                basis="naming",
                note="no dedicated error counter: split the request counter by its status label "
                "(name unknown to the catalog)",
            ),
        ),
        "request_latency": (
            R(
                _STEM + r"(?:request|response|call|rpc|handler)_(?:duration|latency)_"
                r"(?:seconds|milliseconds)",
                HIST,
                0.6,
                "histogram",
                "histogram",
                basis="naming",
            ),
            R(
                _STEM + r"(?:duration|latency)_seconds",
                HIST,
                0.45,
                "histogram",
                "histogram",
                basis="naming",
                note="generic duration name: confirm it times requests",
            ),
            R(
                _STEM + r"(?:request|response|call|rpc|handler)_(?:duration|latency)_"
                r"(?:seconds|milliseconds)",
                ("summary",),
                0.3,
                "summary",
                "summary",
                basis="naming",
                note="a summary carries precomputed quantiles: not mergeable across instances "
                "and unusable for Little's law mean latency",
            ),
        ),
        "in_flight": (
            R(
                _STEM + r"(?:requests_in_flight|requests_inflight|in_flight_requests|"
                r"inflight_requests|active_requests|requests_active|concurrent_requests|"
                r"current_requests|requests_current)",
                GAUGE,
                0.6,
                "gauge",
                "sum({m})",
                basis="naming",
            ),
        ),
    }


def _node_rules() -> list[Scope]:
    inst = ("instance",)
    cpu = {
        "utilization": (
            R(
                exact("node_cpu_seconds_total"),
                COUNTER,
                0.9,
                "ratio",
                '1 - avg by (instance) (rate({m}{mode="idle"}[5m]))',
                note="busy fraction across cores (1 - idle)",
                role_claim="cpu-time",
            ),
        ),
        "saturation": (
            R(
                exact("node_pressure_cpu_waiting_seconds_total"),
                COUNTER,
                0.8,
                "counter_rate",
                "rate({m}[5m])",
                note="PSI: share of time tasks waited for CPU",
            ),
            R(
                exact("node_schedstat_waiting_seconds_total"),
                COUNTER,
                0.7,
                "counter_rate",
                "sum by (instance) (rate({m}[5m]))",
                note="run-queue wait seconds per second",
            ),
            R(
                exact("node_load1"),
                GAUGE,
                0.6,
                "ratio",
                'node_load1 / count by (instance) (node_cpu_seconds_total{mode="idle"})',
                needs=("node_cpu_seconds_total",),
                note="Linux load also counts tasks blocked on IO: an ambiguous saturation signal",
                role_claim="saturation",
            ),
            R(
                exact("node_cpu_core_throttles_total", "node_cpu_package_throttles_total"),
                COUNTER,
                0.3,
                "counter_rate",
                "sum by (instance) (rate({m}[5m]))",
                note="thermal throttling: a different saturation mechanism",
            ),
        ),
    }
    mem = {
        "utilization": (
            R(
                exact("node_memory_MemAvailable_bytes"),
                GAUGE,
                0.9,
                "ratio",
                "1 - {m} / node_memory_MemTotal_bytes",
                needs=("node_memory_MemTotal_bytes",),
            ),
            R(
                exact("node_memory_MemFree_bytes"),
                GAUGE,
                0.4,
                "ratio",
                "1 - {m} / node_memory_MemTotal_bytes",
                needs=("node_memory_MemTotal_bytes",),
                note="counts reclaimable cache as used: prefer MemAvailable",
            ),
        ),
        "saturation": (
            R(
                exact("node_pressure_memory_waiting_seconds_total"),
                COUNTER,
                0.85,
                "counter_rate",
                "rate({m}[5m])",
                note="PSI: share of time tasks waited on memory",
            ),
            R(
                exact("node_vmstat_pswpin", "node_vmstat_pswpout"),
                COUNTER,
                0.65,
                "counter_rate",
                "rate({m}[5m])",
                note="swap activity (pages/s)",
            ),
            R(
                exact("node_vmstat_pgmajfault"),
                COUNTER,
                0.5,
                "counter_rate",
                "rate({m}[5m])",
                note="major page faults (pages/s)",
            ),
        ),
        "errors": (
            R(exact("node_vmstat_oom_kill"), COUNTER, 0.85, "counter_rate", "rate({m}[5m])"),
        ),
    }
    disk = {
        "utilization": (
            R(
                exact("node_disk_io_time_seconds_total"),
                COUNTER,
                0.9,
                "counter_rate",
                "rate({m}[5m])",
                note="fraction of time the device was busy; saturates early on parallel (NVMe) "
                "devices",
                role_claim="utilization",
            ),
        ),
        "saturation": (
            R(
                exact("node_disk_io_time_weighted_seconds_total"),
                COUNTER,
                0.8,
                "counter_rate",
                "rate({m}[5m])",
                note="average queue length",
                role_claim="saturation",
            ),
            R(exact("node_disk_io_now"), GAUGE, 0.6, "gauge", "{m}", note="instantaneous queue"),
            R(
                exact("node_pressure_io_waiting_seconds_total"),
                COUNTER,
                0.7,
                "counter_rate",
                "rate({m}[5m])",
                note="PSI is per host, not per device",
            ),
        ),
    }
    fs = {
        "utilization": (
            R(
                exact("node_filesystem_avail_bytes"),
                GAUGE,
                0.9,
                "ratio",
                "1 - {m} / node_filesystem_size_bytes",
                needs=("node_filesystem_size_bytes",),
            ),
            R(
                exact("node_filesystem_free_bytes"),
                GAUGE,
                0.5,
                "ratio",
                "1 - {m} / node_filesystem_size_bytes",
                needs=("node_filesystem_size_bytes",),
                note="includes root-reserved blocks: avail is what users can use",
            ),
            R(
                exact("node_filesystem_files_free"),
                GAUGE,
                0.45,
                "ratio",
                "1 - {m} / node_filesystem_files",
                needs=("node_filesystem_files",),
                note="inode utilization, a different capacity than bytes",
            ),
        ),
        "errors": (R(exact("node_filesystem_device_error"), GAUGE, 0.6, "gauge", "{m}"),),
    }
    net = {
        "utilization": (
            R(
                exact("node_network_receive_bytes_total"),
                COUNTER,
                0.75,
                "ratio",
                "rate({m}[5m]) / node_network_speed_bytes",
                needs=("node_network_speed_bytes",),
                note="receive direction; transmit may saturate first",
            ),
            R(
                exact("node_network_transmit_bytes_total"),
                COUNTER,
                0.7,
                "ratio",
                "rate({m}[5m]) / node_network_speed_bytes",
                needs=("node_network_speed_bytes",),
                note="transmit direction",
            ),
        ),
        "saturation": (
            R(
                exact("node_network_transmit_drop_total", "node_network_receive_drop_total"),
                COUNTER,
                0.7,
                "counter_rate",
                "rate({m}[5m])",
                note="drops are the observable symptom of a full queue/ring",
            ),
            R(
                exact("node_softnet_dropped_total", "node_softnet_times_squeezed_total"),
                COUNTER,
                0.5,
                "counter_rate",
                "sum by (instance) (rate({m}[5m]))",
                note="softirq backlog, per host not per device",
            ),
        ),
        "errors": (
            R(
                exact("node_network_receive_errs_total", "node_network_transmit_errs_total"),
                COUNTER,
                0.85,
                "counter_rate",
                "rate({m}[5m])",
                role_claim="errors",
            ),
        ),
    }
    k = ("USE",)
    return [
        Scope("node_cpu", "node_exporter CPU", "node:cpu", inst, cpu, k),
        Scope("node_memory", "node_exporter memory", "node:memory", inst, mem, k),
        Scope("node_disk", "node_exporter disk", "node:disk", (*inst, "device"), disk, k),
        Scope(
            "node_filesystem",
            "node_exporter filesystem",
            "node:filesystem",
            (*inst, "mountpoint"),
            fs,
            k,
        ),
        Scope("node_network", "node_exporter network", "node:network", (*inst, "device"), net, k),
    ]


def _k8s_and_runtime_rules() -> list[Scope]:
    ctr = ("namespace", "pod", "container")
    k = ("USE",)
    cpu = {
        "utilization": (
            R(
                exact("container_cpu_usage_seconds_total"),
                COUNTER,
                0.85,
                "ratio",
                "rate({m}[5m]) / (container_spec_cpu_quota / container_spec_cpu_period)",
                needs=("container_spec_cpu_quota", "container_spec_cpu_period"),
                note="cores used over the CPU limit (no limit: compare with node capacity)",
            ),
        ),
        "saturation": (
            R(
                exact("container_cpu_cfs_throttled_periods_total"),
                COUNTER,
                0.9,
                "ratio",
                "rate({m}[5m]) / rate(container_cpu_cfs_periods_total[5m])",
                needs=("container_cpu_cfs_periods_total",),
                note="CFS throttling: share of periods in which the container hit its CPU limit",
            ),
            R(
                exact("container_cpu_cfs_throttled_seconds_total"),
                COUNTER,
                0.7,
                "counter_rate",
                "rate({m}[5m])",
                note="seconds throttled per second (not normalised by period count)",
            ),
        ),
    }
    mem = {
        "utilization": (
            R(
                exact("container_memory_working_set_bytes"),
                GAUGE,
                0.9,
                "ratio",
                "{m} / container_spec_memory_limit_bytes",
                needs=("container_spec_memory_limit_bytes",),
                note="what the kubelet evicts on; a 0 limit means unlimited",
            ),
            R(
                exact("container_memory_usage_bytes"),
                GAUGE,
                0.5,
                "ratio",
                "{m} / container_spec_memory_limit_bytes",
                needs=("container_spec_memory_limit_bytes",),
                note="includes page cache: prefer working_set",
            ),
            R(
                exact("container_memory_rss"),
                GAUGE,
                0.4,
                "ratio",
                "{m} / container_spec_memory_limit_bytes",
                needs=("container_spec_memory_limit_bytes",),
                note="anonymous memory only",
            ),
        ),
        "errors": (
            R(
                exact("container_oom_events_total"),
                COUNTER,
                0.85,
                "counter_rate",
                "rate({m}[5m])",
            ),
            R(
                exact("container_memory_failcnt", "container_memory_failures_total"),
                COUNTER,
                0.6,
                "counter_rate",
                "rate({m}[5m])",
                note="memory limit hits / allocation failures",
            ),
        ),
    }
    jvm = {
        "utilization": (
            R(
                exact("jvm_memory_used_bytes"),
                GAUGE,
                0.75,
                "ratio",
                'sum({m}{jvm_memory_type="heap"}) / sum(jvm_memory_limit_bytes{jvm_memory_type="heap"})',
                needs=("jvm_memory_limit_bytes",),
                note="heap use over the configured maximum; " + _LABEL_UNVERIFIED,
            ),
        ),
        "saturation": (
            R(
                exact("jvm_gc_duration_seconds"),
                HIST,
                0.7,
                "ratio",
                "rate({m}_sum[5m])",
                note="share of wall time spent in GC (seconds per second)",
            ),
        ),
    }
    go = {
        "utilization": (
            R(
                exact("process_runtime_go_mem_heap_inuse_bytes"),
                GAUGE,
                0.6,
                "ratio",
                "{m} / process_runtime_go_mem_heap_sys_bytes",
                needs=("process_runtime_go_mem_heap_sys_bytes",),
                note="heap in use over heap obtained from the OS (the Go runtime has no hard cap)",
            ),
        ),
        "saturation": (
            R(
                exact("process_runtime_go_gc_pause_ns_total"),
                COUNTER,
                0.6,
                "ratio",
                "rate({m}[5m]) / 1e9",
                note="unit is nanoseconds: share of time paused for GC",
            ),
        ),
    }
    return [
        Scope("k8s_cpu", "Kubernetes container CPU", "k8s:container-cpu", ctr, cpu, k),
        Scope("k8s_memory", "Kubernetes container memory", "k8s:container-memory", ctr, mem, k),
        Scope("jvm_memory", "JVM memory", "jvm:memory", ("service_name",), jvm, k),
        Scope("go_runtime", "Go runtime memory", "go:runtime", ("service_name",), go, k),
    ]


def _scopes() -> tuple[Scope, ...]:
    rl = ("RED", "littles_law")
    return (
        Scope(
            "otel_http",
            "OTel HTTP server",
            "http.server",
            ("service_name",),
            _otel_http_rules(),
            rl,
        ),
        Scope(
            "otel_rpc", "OTel RPC server", "rpc.server", ("service_name",), _otel_rpc_rules(), rl
        ),
        Scope(
            "spanmetrics",
            "span-metrics (traces_spanmetrics_*)",
            "spanmetrics",
            ("service_name",),
            _spanmetrics_rules(),
            rl,
            caveat="derived from spans: counts only sampled traces if the pipeline samples",
        ),
        *_node_rules(),
        *_k8s_and_runtime_rules(),
        Scope("app", "naming convention", "app", (), _generic_app_rules(), rl, generic=True),
    )


SCOPES = _scopes()
_COMPILED: dict[tuple[str, str, int], re.Pattern[str]] = {}


def _re(scope: str, sig: str, i: int, pat: str) -> re.Pattern[str]:
    k = (scope, sig, i)
    if k not in _COMPILED:
        _COMPILED[k] = re.compile(pat)
    return _COMPILED[k]


@dataclass
class _Meta:
    name: str
    type: str | None
    role: str | None
    native: bool


def _metas(entries: Iterable[CatalogEntry]) -> dict[str, _Meta]:
    out = {}
    for e in entries:
        if e.is_family:
            continue
        t, r, h = e.fields.get("type"), e.fields.get("role"), e.fields.get("histogram_family")
        members = h.value if h is not None else []
        out[e.metric] = _Meta(
            e.metric,
            t.value if t else None,
            r.value if r else None,
            bool(h) and not any(m.endswith("_bucket") for m in members),
        )
    return out


def _candidate(m: _Meta, rule: Rule, metas: Mapping[str, _Meta]) -> dict[str, Any] | None:
    conf = rule.confidence
    caveats: list[str] = []
    if m.type is None:
        conf -= UNKNOWN_TYPE_PENALTY
        caveats.append("metric type unknown to the catalog")
    elif m.type not in rule.types:
        return None
    if missing := [n for n in rule.needs if n not in metas]:
        conf -= MISSING_NEED_PENALTY
        caveats.append(f"expr needs {', '.join(missing)}, not in the catalog")
    if rule.role_claim and m.role == rule.role_claim:
        conf += ROLE_CLAIM_BOOST
    template, note, form = rule.expr, rule.note, rule.form
    if m.native and "{m}_count" in template:  # native histograms have no _count series
        simple = "rate({m}_count[5m])"
        if simple in template:
            template = template.replace(simple, "histogram_count(rate({m}[5m]))")
        else:
            caveats.append("native histogram: wrap the rate in histogram_count(...), no _count")
    expr = template.replace("{m}", m.name)
    if form == "histogram":
        expr = (
            f"histogram_avg(rate({m.name}[5m]))  # mean W; quantiles: "
            f"histogram_quantile(q, rate({m.name}[5m]))"
            if m.native
            else f"rate({m.name}_sum[5m]) / rate({m.name}_count[5m])  # mean W; quantiles: "
            f"histogram_quantile(q, sum by (le) (rate({m.name}_bucket[5m])))"
        )
    out: dict[str, Any] = {
        "metric": m.name,
        "confidence": round(max(0.05, min(conf, MAX_CONFIDENCE)), 3),
        "basis": [rule.basis],
        "form": form,
        "expr": expr,
    }
    if m.native:
        out["histogram"] = "native"
    elif m.type == "histogram":
        out["histogram"] = "classic"
    if note:
        out["note"] = note
    if caveats:
        out["caveats"] = caveats
    return out


def _signal_candidates(
    scope: Scope, signal: str, metas: Mapping[str, _Meta], exclude: set[str]
) -> dict[str, list[dict[str, Any]]]:
    """prefix ('' for fixed scopes) -> candidates, best first, one per metric."""
    by_prefix: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for i, rule in enumerate(scope.signals.get(signal, ())):
        pat = _re(scope.id, signal, i, rule.pattern)
        for name in sorted(metas):
            if scope.generic and (name in exclude or "_client_" in name):
                continue
            mt = pat.fullmatch(name)
            if mt is None:
                continue
            p = (mt.groupdict().get("p") or "") if scope.generic else ""
            cand = _candidate(metas[name], rule, metas)
            if cand is None:
                continue
            cur = by_prefix[p].get(name)
            if cur is None or cand["confidence"] > cur["confidence"]:
                by_prefix[p][name] = cand
    return {
        p: sorted(c.values(), key=lambda x: (-x["confidence"], x["metric"]))
        for p, c in by_prefix.items()
    }


def _drop_summary_if_histogram(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Never offer a precomputed-percentile summary while a histogram is available."""
    if any(c["form"] == "histogram" for c in cands):
        return [c for c in cands if c["form"] != "summary"]
    return cands


def _boost_with_relations(
    kind: str,
    roles: dict[str, list[dict[str, Any]]],
    relations: Iterable[ResolvedRelation],
    metas: Mapping[str, _Meta],
) -> None:
    """Corroborate with catalog relations: `errors part_of requests` ties the two roles together;
    an error-looking part_of child of the rate metric is offered even without a naming match."""
    if kind not in ("RED", "littles_law"):
        return
    rate_role = "rate" if kind == "RED" else "arrival_rate"
    parts = [(r.subject, r.object) for r in relations if r.kind == "part_of"]
    rate_names = {c["metric"] for c in roles.get(rate_role, [])}
    for child, parent in parts:
        if parent not in rate_names:
            continue
        for c in roles.get(rate_role, []):
            if c["metric"] == parent:
                _add_basis(c, "relation", f"{child} part_of {parent}")
        if kind != "RED":
            continue
        errs = roles.setdefault("errors", [])
        hit = next((c for c in errs if c["metric"] == child), None)
        if hit is not None:
            _add_basis(hit, "relation", f"{child} part_of {parent}")
        elif (
            child in metas
            and metas[child].type in ("counter", None)
            and re.search(r"err|fail|5xx", child)
        ):
            errs.append(
                {
                    "metric": child,
                    "confidence": 0.55,
                    "basis": ["relation"],
                    "form": "counter_rate",
                    "expr": f"sum(rate({child}[5m]))",
                    "note": f"catalog relation: {child} part_of {parent}",
                }
            )
        errs.sort(key=lambda x: (-x["confidence"], x["metric"]))


def _add_basis(c: dict[str, Any], basis: str, why: str) -> None:
    if basis not in c["basis"]:
        c["basis"].append(basis)
        c["confidence"] = round(min(MAX_CONFIDENCE, c["confidence"] + RELATION_BOOST), 3)
        c.setdefault("relations", []).append(why)


def _apply_bounded_by(
    cands: Iterable[dict[str, Any]], relations: Iterable[ResolvedRelation]
) -> None:
    """A `bounded_by` relation from the candidate to a capacity metric corroborates a ratio form."""
    for c in cands:
        if c["form"] != "ratio":
            continue
        for r in relations:
            if r.kind == "bounded_by" and r.subject == c["metric"]:
                _add_basis(c, "relation", f"{r.subject} bounded_by {r.object}")


def suggest_bindings(
    entries: Iterable[CatalogEntry],
    relations: Iterable[ResolvedRelation] = (),
    bindings: Iterable[ResolvedBinding] = (),
    *,
    kind: str | None = None,
    key: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Ranked binding candidates from catalog state. See module docstring.

    `key`: if it names a suggested scope key (`http.server`, `node:cpu`, ...) only those scopes are
    returned; otherwise it is the entity (a service/instance name) the binding is for and every
    matching scope is returned under that key."""
    if kind is not None and kind not in BINDING_ROLES:
        raise ValueError(f"unknown binding kind {kind!r}; expected one of {sorted(BINDING_ROLES)}")
    metas = _metas(entries)
    rels = list(relations)
    existing = {(b.kind, b.key): b for b in bindings}
    key_matches_scope = key is not None and any(sc.key == key for sc in SCOPES)
    kinds = [kind] if kind else list(BINDING_ROLES)

    # names a fixed request-oriented scope already explains: the generic scope leaves them be
    fixed_names: set[str] = set()
    for sc in SCOPES:
        if not sc.generic and "RED" in sc.kinds:
            for sig in sc.signals:
                for cands in _signal_candidates(sc, sig, metas, set()).values():
                    fixed_names.update(c["metric"] for c in cands)

    out: list[dict[str, Any]] = []
    for sc in SCOPES:
        if key_matches_scope and sc.key != key:
            continue
        for k in kinds:
            if k not in sc.kinds:
                continue
            per_signal = {
                sig: _signal_candidates(sc, sig, metas, fixed_names)
                for sig in set(KIND_SIGNALS[k].values())
                if sig in sc.signals
            }
            prefixes = sorted({p for d in per_signal.values() for p in d})
            if sc.generic and key and not key_matches_scope and key in prefixes:
                prefixes = [key]  # the caller named the entity and it is one of the prefixes
            for p in prefixes:
                roles: dict[str, list[dict[str, Any]]] = {}
                for role, sig in KIND_SIGNALS[k].items():
                    cs = [dict(c) for c in per_signal.get(sig, {}).get(p, [])]
                    roles[role] = _drop_summary_if_histogram(cs)
                _boost_with_relations(k, roles, rels, metas)
                for cs in roles.values():
                    _apply_bounded_by(cs, rels)
                    cs.sort(key=lambda x: (-x["confidence"], x["metric"]))
                s = _assemble(sc, k, p, roles, key, key_matches_scope, existing)
                if s is not None:
                    out.append(s)
    out.sort(key=lambda s: (-s["score"], s["id"]))
    shown = out[: max(1, limit)]
    return {
        "suggestions": shown,
        "total": len(out),
        "note": "Label names in join_on/expr are conventions, not read from the source. "
        "Check them (and the alternatives) before confirming with catalog_bind.",
        **({"keys": sorted({s["key"] for s in out})} if key and not shown else {}),
    }


def _assemble(
    sc: Scope,
    kind: str,
    prefix: str,
    roles: dict[str, list[dict[str, Any]]],
    key_arg: str | None,
    key_is_scope: bool,
    existing: Mapping[tuple[str, str], ResolvedBinding],
) -> dict[str, Any] | None:
    chosen: dict[str, str | None] = {}
    detail: dict[str, Any] = {}
    unfilled: list[dict[str, Any]] = []
    ambiguous: list[str] = []
    for role in BINDING_ROLES[kind]:
        cs = roles.get(role) or []
        if not cs:
            chosen[role] = None
            hint = SUGGESTIONS[(kind, role)]
            unfilled.append(
                {
                    "role": role,
                    "suggest_instrumentation": {
                        "name": hint.name.format(key=metric_slug(prefix or sc.key)),
                        "type": hint.type,
                        "why": hint.why,
                    },
                }
            )
            continue
        best, rest = cs[0], cs[1:]
        chosen[role] = best["metric"]
        detail[role] = {
            **best,
            "alternatives": rest,
            "ambiguous": bool(rest) and rest[0]["confidence"] >= best["confidence"] - 0.15,
        }
        if detail[role]["ambiguous"]:
            ambiguous.append(role)
    filled = [d for d in detail.values()]
    if len(filled) < MIN_FILLED or (sc.generic and len({c["metric"] for c in filled}) < 2):
        return None  # generic names alone must show two different metrics
    entity = prefix or sc.key
    key = key_arg if key_arg and not key_is_scope else entity
    n = len(BINDING_ROLES[kind])
    score = sum(d["confidence"] for d in filled) / n
    out: dict[str, Any] = {
        "id": f"{kind}:{sc.id}" + (f":{prefix}" if prefix else ""),
        "kind": kind,
        "key": key,
        "scope": sc.label,
        "confidence": round(score, 3),
        "score": round(score, 3),
        "roles": chosen,
        "detail": detail,
        "unfilled": unfilled,
        "ambiguous_roles": ambiguous,
        "join_on": list(sc.join_on),
        "join_on_basis": "convention, not read from the source",
    }
    if sc.caveat:
        out["caveat"] = sc.caveat
    if (kind, key) in existing:
        b = existing[(kind, key)].winner
        out["already_bound"] = {"origin": b.origin, "roles": b.roles}
    return out


def find_suggestion(result: Mapping[str, Any], suggestion_id: str) -> dict[str, Any]:
    for s in result["suggestions"]:
        if s["id"] == suggestion_id:
            return s
    raise ValueError(
        f"no suggestion {suggestion_id!r}; ids now: {[s['id'] for s in result['suggestions']]}"
    )
