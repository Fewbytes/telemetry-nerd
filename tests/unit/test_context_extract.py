import json

import pytest

from telemetry_nerd.catalog.context import (
    GRAFANA_UNITS,
    Extraction,
    call_args,
    extract,
    extract_dashboard,
    extract_go,
    extract_js,
    extract_markdown,
    extract_python,
    otel_name,
    prom_name,
    series_names,
)

PY = """
import prometheus_client
from prometheus_client import Counter, Gauge, Histogram, Summary, Info, Enum

NS = "shop"
REQS = Counter("requests", "Requests served.", ["method", "code"], namespace=NS, subsystem="http")
DONE = Counter("jobs_done_total", "Jobs finished.")
DEPTH = Gauge("queue_depth", "Items waiting.", namespace="shop")
LAT = Histogram("latency", "Request latency.", ["route"], unit="seconds", buckets=(0.1, 0.5, 1, float("inf")))
SIZE = Summary("payload", "Payload sizes.", unit="bytes")
BUILD = Info("build", "Build information.")
STATE = Enum("state", "Service state.", states=["up", "down"])
DYN = Counter(f"dyn_{NS}", "dynamic")
def f(name):
    return Gauge(name, "from a variable")
"""


def run(fn, path, text):
    out = Extraction()
    fn(path, text, out)
    return out


def by_base(out):
    return {d.base: d for d in out.definitions}


def test_python_prometheus_client_names_follow_the_library():
    out = run(extract_python, "app/metrics.py", PY)
    d = by_base(out)
    assert set(d) == {
        "shop_http_requests", "jobs_done_total", "shop_queue_depth", "latency_seconds",
        "payload_bytes", "build", "state",
    }  # fmt: skip
    assert d["shop_http_requests"].names == ("shop_http_requests_total",)  # the client adds _total
    assert d["jobs_done_total"].names == ("jobs_done_total",)  # not doubled
    assert d["shop_queue_depth"].names == ("shop_queue_depth",)
    assert d["latency_seconds"].names == (
        "latency_seconds_bucket",
        "latency_seconds_sum",
        "latency_seconds_count",
    )
    assert d["payload_bytes"].names == ("payload_bytes_sum", "payload_bytes_count")
    assert d["build"].names == ("build_info",)
    assert d["state"].kind == "enum"


def test_python_details_help_labels_unit_buckets_and_lines():
    d = by_base(run(extract_python, "app/metrics.py", PY))
    r = d["shop_http_requests"]
    assert (r.help, r.labels, r.unit, r.line) == ("Requests served.", ("method", "code"), None, 6)
    lat = d["latency_seconds"]
    assert lat.unit == "s" and lat.buckets == (0.1, 0.5, 1.0) and lat.labels == ("route",)
    assert lat.citation == "code: app/metrics.py:9 (python prometheus_client Histogram)"
    assert r.confidence == 0.85


def test_python_dynamic_names_are_skipped_and_said_so():
    out = run(extract_python, "app/metrics.py", PY)
    reasons = [s.reason for s in out.skipped]
    assert any("Counter: the name is not a plain string" in r for r in reasons)
    assert any("Gauge: the name is not a plain string" in r for r in reasons)
    assert len(out.definitions) == 7  # nothing was guessed for the two dynamic ones


def test_python_without_the_library_is_ignored_and_bad_syntax_reported():
    assert run(extract_python, "x.py", 'c = Counter("a_b", "h")').definitions == []
    out = run(extract_python, "x.py", "def (:")
    assert out.definitions == [] and "not valid Python" in out.skipped[0].reason


def test_python_opentelemetry_follows_the_otlp_translation():
    src = """
meter = get_meter("x")
a = meter.create_counter("http.server.requests", unit="1", description="Requests.")
b = meter.create_histogram("http.server.duration", unit="s", description="Latency.")
c = meter.create_up_down_counter("queue.size", unit="{item}", description="Items.")
d = meter.create_gauge(name="process.memory", unit="By", description="Resident memory.")
e = meter.create_counter(dynamic_name)
"""
    d = by_base(run(extract_python, "otel.py", src))
    assert d["http_server_requests"].names == ("http_server_requests_total",)
    assert d["http_server_duration_seconds"].names[0] == "http_server_duration_seconds_bucket"
    assert d["http_server_duration_seconds"].unit == "s"
    assert d["queue_size"].kind == "gauge" and d["queue_size"].unit is None
    assert (
        d["process_memory_bytes"].unit == "B"
        and d["process_memory_bytes"].help == "Resident memory."
    )
    assert any(
        "name is not a plain string" in s.reason
        for s in run(extract_python, "otel.py", src).skipped
    )


def test_otel_name_and_naming_helpers():
    assert otel_name("a.b", "ms", "gauge") == ("a_b_milliseconds", ("a_b_milliseconds",))
    assert otel_name("a.b_seconds", "s", "gauge")[0] == "a_b_seconds"  # the suffix is not doubled
    assert prom_name(["ns", None, "x"], "seconds") == "ns_x_seconds"
    assert prom_name([None, None, "x_seconds"], "seconds") == "x_seconds"
    assert series_names("counter", "x_total", client="go") == ("x_total",)
    assert series_names("counter", "x", client="go") == ("x",)  # Go does not add _total


GO = """
package metrics

var (
	requests = promauto.NewCounterVec(prometheus.CounterOpts{
		Namespace: "shop",
		Subsystem: "http",
		Name:      "requests_total",
		Help:      "Requests served.",
	}, []string{"method", "code"})

	depth = prometheus.NewGauge(prometheus.GaugeOpts{Name: "queue_depth", Help: "Items waiting."})

	latency = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "request_duration_seconds",
		Help:    "Latency.",
		Buckets: []float64{0.05, 0.1, 0.5, 1},
	})

	dynamic = prometheus.NewCounter(prometheus.CounterOpts{Name: prefix + "_x", Help: "dyn"})
)

func init() {
	c, _ := meter.Int64Counter("jobs.done", metric.WithUnit("1"), metric.WithDescription("Jobs finished."))
	h, _ := meter.Float64Histogram("job.duration", metric.WithUnit("s"), metric.WithDescription("Job time."))
	_ = meter.Int64Counter(name)
}
"""


def test_go_client_and_otel():
    out = run(extract_go, "metrics/metrics.go", GO)
    d = by_base(out)
    assert d["shop_http_requests_total"].names == (
        "shop_http_requests_total",
    )  # Go: no suffix added
    assert d["shop_http_requests_total"].labels == ("method", "code")
    assert d["shop_http_requests_total"].help == "Requests served."
    assert d["queue_depth"].kind == "gauge"
    assert d["request_duration_seconds"].buckets == (0.05, 0.1, 0.5, 1.0)
    assert d["request_duration_seconds"].names[0] == "request_duration_seconds_bucket"
    assert d["jobs_done"].names == ("jobs_done_total",) and d["jobs_done"].help == "Jobs finished."
    assert d["job_duration_seconds"].unit == "s"
    assert d["queue_depth"].line == 12
    reasons = [s.reason for s in out.skipped]
    assert any("Name is not a plain string" in r for r in reasons) and any(
        "name is not a plain string" in r for r in reasons
    )


JS = """
const meter = metrics.getMeter("app");
const c = meter.createCounter("orders.placed", { description: "Orders placed.", unit: "1" });
const h = meter.createHistogram('checkout.duration', { description: "Checkout time.", unit: "ms" });
const g = meter.createObservableGauge(`dyn.${x}`, { description: "dynamic" });
"""


def test_javascript_opentelemetry():
    out = run(extract_js, "src/metrics.ts", JS)
    d = by_base(out)
    assert (
        d["orders_placed"].names == ("orders_placed_total",)
        and d["orders_placed"].help == "Orders placed."
    )
    assert d["checkout_duration_milliseconds"].unit == "ms"
    assert len(out.definitions) == 2 and "not a plain string" in out.skipped[0].reason


def test_call_args_respects_strings_and_nesting():
    text = 'f("a)b", g(1, [2, 3]), "x") rest'
    assert call_args(text, 1) == ('"a)b", g(1, [2, 3]), "x"', text.index(")", 22))
    assert call_args("f(unclosed", 1) is None


DASH = json.dumps(
    {
        "title": "Shop",
        "panels": [
            {
                "title": "Depth",
                "description": "Items waiting",
                "fieldConfig": {"defaults": {"unit": "short"}},
                "targets": [{"expr": "shop_queue_depth"}],
            },
            {
                "title": "Latency",
                "fieldConfig": {"defaults": {"unit": "s"}},
                "targets": [{"expr": "histogram_quantile(0.9, sum(rate(lat_bucket[5m])) by (le))"}],
            },
            {
                "type": "row",
                "panels": [
                    {"title": "Old", "yaxes": [{"format": "bytes"}], "targets": [{"expr": "mem"}]}
                ],
            },
            {"title": "Text only"},
        ],
    }
)


def test_dashboard_panels_with_units_and_nested_rows():
    out = Extraction()
    assert extract_dashboard("d.json", DASH, out) is True
    assert [(p.title, p.unit_id, p.exprs) for p in out.panels] == [
        ("Depth", "short", ("shop_queue_depth",)),
        ("Latency", "s", ("histogram_quantile(0.9, sum(rate(lat_bucket[5m])) by (le))",)),
        ("Old", "bytes", ("mem",)),
    ]
    assert out.panels[0].description == "Items waiting"


def test_json_that_is_not_a_dashboard():
    assert extract_dashboard("x.json", "[1, 2]", Extraction()) is False
    assert extract_dashboard("x.json", "not json", Extraction()) is False
    assert extract_dashboard("x.json", '{"panels": 3}', Extraction()) is False


def test_grafana_units_map_to_canonical_units_and_rates():
    assert GRAFANA_UNITS["percentunit"] == ("ratio", False)
    assert GRAFANA_UNITS["reqps"] == ("count", True)
    assert GRAFANA_UNITS["bytes"] == ("B", False)


MD = """
# Metrics

| Metric | Type | Description |
|---|---|---|
| `app_requests_total` | counter | Requests served, by code. |
| `app_queue_depth` | Gauge | Items waiting in the queue |
| not a metric row | foo | bar |
| `up` | gauge | single word names are not matched |
"""


def test_markdown_tables():
    out = run(extract_markdown, "README.md", MD)
    d = by_base(out)
    assert set(d) == {"app_requests_total", "app_queue_depth"}
    assert (d["app_requests_total"].kind, d["app_requests_total"].help, d["app_requests_total"].line) == (
        "counter", "Requests served, by code.", 6,
    )  # fmt: skip
    assert d["app_queue_depth"].kind == "gauge" and d["app_queue_depth"].confidence == 0.6


def test_dispatch_by_extension_and_content():
    out = extract(
        [
            {"path": "a.py", "text": PY},
            {"path": "b.go", "text": GO},
            {"path": "c.ts", "text": JS},
            {"path": "d.json", "text": DASH},
            {"path": "e.json", "text": "[]"},
            {"path": "f.md", "text": MD},
            {"path": "g.rs", "text": "fn main() {}"},
            {"path": "Makefile", "text": "all:"},
        ]
    )
    assert len(out.definitions) == 7 + 5 + 2 + 2 and len(out.panels) == 3
    assert any("no extractor for .rs" in s.reason for s in out.skipped)
    assert any("not a Grafana dashboard" in s.reason for s in out.skipped)
    assert any("without an extension" in s.reason for s in out.skipped)


@pytest.mark.parametrize("junk", ["", "\x00\x01", "Counter(", "'" * 50])
def test_junk_never_crashes_an_extractor(junk):
    for fn in (extract_python, extract_go, extract_js, extract_markdown):
        fn("x", junk, Extraction())
