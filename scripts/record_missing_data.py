# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx"]
# ///
"""Record missing-data fixtures (bead telemetry-nerd-1h9.10): one small JSON file per claim under
tests/fixtures/missing-data/<backend>/<source>__<claim>.json, replayed by tests/unit/test_missing_data_fixtures.py.

  uv run scripts/record_missing_data.py local     # needs `just lab-up`, `seed`, and a finished `serve`
  uv run scripts/record_missing_data.py public    # registry sources, 1 request at a time, >= 1.1 s apart

Fixture: {"claim", "source", "note", "request": {"path", "params"}, "status", "body"}.
Public requests are narrow (single series, <= 3 days, coarse steps) and stop at the first sign of
trouble per source; never broad scans. Requires network; CI never runs this.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "missing-data"
UA = "telemetry-nerd/research (+https://github.com/Fewbytes/telemtry-nerd; bead 1h9.10; polite, 1 req at a time)"
LAB = {
    "prom": "http://127.0.0.1:19090",
    "prom-limits": "http://127.0.0.1:19091",
    "vm": "http://127.0.0.1:18428",
    "vm-limits": "http://127.0.0.1:18429",
}
WM = "https://grafana.wikimedia.org/api/datasources/proxy/uid"
PUBLIC = {
    "wikimedia-raw": ("thanos", f"{WM}/000000026"),
    "wikimedia-1h": ("thanos", f"{WM}/PA7DE9A562EF40E24"),
    "wikimedia-5m": ("thanos", f"{WM}/P1B4DE8CFE4C343FA"),
    "cern-eos": (
        "thanos",
        "https://monit-grafana-open.cern.ch/api/datasources/proxy/uid/b490b4f4-28d2-4f0b-8fe1-b0bb61c365f6",
    ),
    "grafana-play": (
        "mimir",
        "https://play.grafana.org/api/datasources/proxy/uid/grafanacloud-prom",
    ),
    "cern-openstack": (
        "mimir",
        "https://monit-grafana-open.cern.ch/api/datasources/proxy/uid/bf9ylnkygnnr4c",
    ),
    "vm-playground": ("victoriametrics", "https://play.victoriametrics.com/select/0/prometheus"),
    "percona-pmm": (
        "victoriametrics",
        "https://pmmdemo.percona.com/graph/api/datasources/proxy/uid/PA58DA793C7250F1B",
    ),
    "prometheus-demo": ("prometheus", "https://prometheus.demo.prometheus.io"),
    "promlabs-demo": ("prometheus", "https://demo.promlabs.com"),
}


FORCE = bool(os.environ.get("FORCE"))


class Rec:
    def __init__(self, backend: str, source: str, base: str, polite: bool) -> None:
        self.backend, self.source, self.base, self.polite = backend, source, base, polite
        self.out = ROOT / backend
        self.out.mkdir(parents=True, exist_ok=True)
        self._last = 0.0

    def get(self, claim: str, path: str, params: dict, note: str = "", trim=None) -> dict:
        cached = self.out / f"{self.source}__{claim}.json"
        if cached.exists() and not FORCE:
            return json.loads(cached.read_text())  # resume an interrupted run
        if self.polite:
            time.sleep(max(0.0, 1.1 - (time.monotonic() - self._last)))
        t0 = time.monotonic()
        try:
            r = httpx.get(
                self.base + path,
                params=params,
                headers={"User-Agent": UA},
                timeout=90 if self.polite else 30,
            )
            status, text = r.status_code, r.text
        except httpx.HTTPError as e:
            status, text = 0, f"transport error: {e}"
        self._last = time.monotonic()
        try:
            body = json.loads(text)
        except ValueError:
            body = text[:500]
            if path == "/flags":  # VictoriaMetrics: plain text, one flag per line
                keys = ("retention", "latencyOffset", "Staleness", "minScrapeInterval")
                body = "\n".join(ln for ln in text.splitlines() if any(k in ln for k in keys))
        if trim and isinstance(body, dict):
            body = trim(body)
        rec = {
            "claim": claim,
            "source": self.source,
            "note": note,
            "request": {"path": path, "params": params},
            "status": status,
            "body": body,
        }
        (self.out / f"{self.source}__{claim}.json").write_text(
            json.dumps(rec, separators=(",", ":")) + "\n"
        )
        n = len(json.dumps(body))
        print(
            f"  {self.source}:{claim} -> {status} {n}B ({time.monotonic() - t0:.1f}s)", flush=True
        )
        return rec


def qr(query: str, start: float, end: float, step: str, **extra) -> dict:
    return {"query": query, "start": f"{start:.3f}", "end": f"{end:.3f}", "step": step, **extra}


def first_series(n: int):
    def trim(body: dict) -> dict:
        data = body.get("data")
        if isinstance(data, dict) and isinstance(data.get("result"), list):
            data["result"] = data["result"][:n]
        return body

    return trim


def keep_labels(*keep: str):
    def trim(body: dict) -> dict:
        data = body.get("data")
        if isinstance(data, dict) and isinstance(data.get("result"), list):
            for item in data["result"]:
                item["metric"] = {k: v for k, v in item["metric"].items() if k in keep}
        return body

    return trim


def flags_only(body):
    """Keep retention / lookback flags (Prometheus JSON)."""
    if isinstance(body, dict):
        data = body.get("data", {})
        body["data"] = {k: v for k, v in data.items() if "retention" in k or "lookback" in k}
    return body


def result(rec: dict) -> list:
    b = rec["body"]
    return b["data"]["result"] if isinstance(b, dict) and b.get("status") == "success" else []


def series_end(rec: Rec, prefix: str, selector: str, now: float, back: str = "3h") -> None:
    """Find a series that stopped reporting 20 min - `back` ago (`last_over_time(m[back]) unless last_over_time(m[20m])`),
    fetch its raw samples, then record the raw selector and count_over_time around its last sample
    (lookback fill / staleness) at a 15 s step."""
    q = f"topk(1, last_over_time({selector}[{back}]) unless last_over_time({selector}[20m]))"
    found = rec.get(f"{prefix}_find", "/api/v1/query", {"query": q, "time": f"{now:.3f}"})
    res = result(found)
    if not res:
        print(f"  {prefix}: no ended series found for {selector}")
        return
    metric = {k: v for k, v in res[0]["metric"].items() if k != "__name__"}
    sel = selector.split("{")[0] + "{" + ",".join(f'{k}="{v}"' for k, v in metric.items()) + "}"
    samples = rec.get(
        f"{prefix}_samples",
        "/api/v1/query",
        {"query": f"{sel}[{back}]", "time": f"{now:.3f}"},
        note="raw samples of the ended series (does it end with a stale marker? gaps?)",
    )
    values = result(samples)[0]["values"]
    last = float(values[-1][0])
    a, b = last - 300, last + 900
    rec.get(
        f"{prefix}_raw", "/api/v1/query_range", qr(sel, a, b, "15s"), note=f"last sample {last}"
    )
    rec.get(
        f"{prefix}_count",
        "/api/v1/query_range",
        qr(f"count_over_time({sel}[15s])", a, b, "15s"),
        note=f"last sample {last}",
    )


def engine_probe(rec: Rec, now: float, metric: str = "process_cpu_seconds_total") -> None:
    """Shared-engine claims on one counter series: raw samples over 10 min, then increase()/
    count_over_time() over windows shorter and longer than the sample spacing."""
    found = rec.get(
        "engine_find", "/api/v1/query", {"query": f"topk(1, {metric})", "time": f"{now:.0f}"}
    )
    res = result(found)
    if not res:
        print(f"  engine_probe: no {metric}")
        return
    metric_labels = {k: v for k, v in res[0]["metric"].items() if k != "__name__"}
    sel = metric + "{" + ",".join(f'{k}="{v}"' for k, v in metric_labels.items()) + "}"
    end = float(int(now) // 60 * 60 - 120)
    start = end - 600
    rec.get("engine_samples", "/api/v1/query", {"query": f"{sel}[10m]", "time": f"{end:.0f}"})
    for w in ("30s", "2m"):
        rec.get(
            f"engine_increase_{w}",
            "/api/v1/query_range",
            qr(f"increase({sel}[{w}])", start, end, "30s"),
        )
        rec.get(
            f"engine_count_{w}",
            "/api/v1/query_range",
            qr(f"count_over_time({sel}[{w}])", start, end, "30s"),
        )


def public() -> None:
    now = time.time()
    day = 86400
    for name in sys.argv[2:] or list(PUBLIC):
        backend, base = PUBLIC[name]
        rec = Rec(backend, name, base, polite=True)
        print(name)
        rec.get("buildinfo", "/api/v1/status/buildinfo", {})
        if name.startswith("wikimedia"):
            thanos_downsample(rec, name, now)
        if name == "wikimedia-raw":
            series_end(rec, "wm_ended", "kube_pod_info", now)
            thanos_misc(rec, now)
        if name in ("wikimedia-raw", "cern-eos", "grafana-play", "cern-openstack"):
            engine_probe(
                rec,
                now,
                "node_network_receive_bytes_total"
                if name == "wikimedia-raw"
                else "process_cpu_seconds_total",
            )
        if name == "cern-eos":
            series_end(rec, "eos_ended", "node_load1", now)
        if backend == "mimir":
            mimir(rec, name, now)
        if backend == "victoriametrics":
            vm_public(rec, name, now)
        if backend == "prometheus":
            series_end(rec, f"{name}_ended", "up", now)
        if name in ("grafana-play", "vm-playground"):
            rec.get(
                "xq1_samples_per_5m",
                "/api/v1/query",
                {"query": "count_over_time(up[5m])", "time": f"{now:.0f}"},
                note="samples per series in one 5 m window: do series in one query share an interval?",
                trim=keep_labels("job"),
            )
        if name not in ("wikimedia-1h", "wikimedia-5m"):
            # retention edge + step limit, one cheap request each
            rec.get(
                "retention_edge",
                "/api/v1/query_range",
                qr("up", now - 900 * day, now - 899 * day, "3600s"),
                note="range far before any plausible retention",
                trim=first_series(3),
            )
            if backend != "victoriametrics":  # VM allows 30000 points: that response is huge
                rec.get(
                    "step_limit_over",
                    "/api/v1/query_range",
                    qr("up", now - 4 * 3600, now, "1s"),
                    note="14401 steps: over the 11000 points/series limit",
                )


def thanos_downsample(rec: Rec, name: str, now: float) -> None:
    sel = 'node_load1{site="eqiad",instance="wdqs1018:9100"}'
    end = (now - 20 * 86400) // 3600 * 3600
    start = end - 3 * 86400
    for tag, extra in (
        ("default", {}),
        ("res1h", {"max_source_resolution": "1h"}),
        ("res5m", {"max_source_resolution": "5m"}),
        ("resauto", {"max_source_resolution": "auto"}),
    ):
        if name == "wikimedia-raw" and tag != "default":
            continue
        if name == "wikimedia-5m" and tag in ("res1h", "resauto"):
            continue
        for fn in ("count_over_time", "max_over_time"):
            rec.get(
                f"{name}_{fn}_{tag}",
                "/api/v1/query_range",
                qr(f"{fn}({sel}[1h])", start, end, "1h", **extra),
                note=f"20 d ago, 3 d window, [1h] at 1h step, {extra or 'no resolution param'}",
            )
    # raw sample spacing: instant range vector over 15 min on each datasource/param
    for tag, extra in (
        ("default", {}),
        ("res1h", {"max_source_resolution": "1h"}),
        ("res5m", {"max_source_resolution": "5m"}),
    ):
        if name == "wikimedia-raw" and tag != "default":
            continue
        if name == "wikimedia-5m" and tag == "res1h":
            continue
        rec.get(
            f"{name}_samples_{tag}",
            "/api/v1/query",
            {"query": f"{sel}[3h]", "time": f"{end:.0f}", **extra},
            note="raw samples: spacing shows which resolution answered",
        )


def thanos_misc(rec: Rec, now: float) -> None:
    sel = 'node_load1{site="eqiad",instance="wdqs1018:9100"}'
    rec.get("dedup_on", "/api/v1/query", {"query": f"count({sel})", "time": f"{now:.0f}"})
    rec.get(
        "dedup_off",
        "/api/v1/query",
        {"query": f"count({sel})", "time": f"{now:.0f}", "dedup": "false"},
        note="dedup=false: do replicas show?",
    )
    rec.get(
        "partial_response_param",
        "/api/v1/query",
        {"query": f"count({sel})", "time": f"{now:.0f}", "partial_response": "false"},
        note="does the proxy pass the param; response has warnings key?",
    )
    for i in range(3):
        rec.get(
            f"metadata_limit_{i}",
            "/api/v1/metadata",
            {"limit": "5"},
            note="non-determinism probe (TQ5): same request, compare names",
        )


def mimir(rec: Rec, name: str, now: float) -> None:
    if name == "grafana-play":
        # OTLP-ingested (hosted OTel demo: no staleness markers expected) vs scraped (k8s)
        series_end(rec, "play_otlp_ended", "target_info", now)
    series_end(rec, f"{name}_scrape_ended", "kube_pod_info", now)
    # query-frontend split seams: 26 h spanning a UTC midnight, 5 min step, one series
    day = 86400
    midnight = now // day * day
    rec.get(
        "split_seam_up",
        "/api/v1/query_range",
        qr("topk(1, up)", midnight - 20 * 3600, midnight + 6 * 3600, "300s"),
        note="range crosses UTC midnight (Mimir splits by 24 h); check duplicates/gaps at the seam",
    )
    rec.get(
        "range_limit",
        "/api/v1/query_range",
        qr("topk(1, up)", now - 800 * day, now, "86400s"),
        note="range longer than max_query_length / retention?",
    )


def vm_public(rec: Rec, name: str, now: float) -> None:
    series_end(rec, f"{name}_ended", "up", now)
    rec.get(
        "shape_range",
        "/api/v1/query_range",
        qr("topk(1, up)", now - 1800, now - 900, "60s"),
        note="top-level fields (isPartial?)",
    )
    rec.get("shape_instant", "/api/v1/query", {"query": "count(up)", "time": f"{now:.0f}"})
    rec.get(
        "export_stats",
        "/api/v1/query",
        {"query": "count(up)", "time": f"{now:.0f}", "trace": "1"},
        note="trace=1 shows internal search window",
    )


# --- local lab -------------------------------------------------------------------------------


def local() -> None:
    tl = json.loads((ROOT / "local" / "timeline.json").read_text())
    seed = json.loads((ROOT / "local" / "seed.json").read_text())
    t0 = tl["t0_ms"] / 1000
    s0 = seed["start_ms"] / 1000
    for lab_name, backend in (("prom", "prometheus"), ("vm", "victoriametrics")):
        rec = Rec(backend, lab_name, LAB[lab_name], polite=False)
        print(lab_name)
        rec.get("buildinfo", "/api/v1/status/buildinfo", {})
        rec.get(
            "flags",
            "/flags" if backend == "victoriametrics" else "/api/v1/status/flags",
            {},
            note="retention / lookback / latency flags are readable from the server",
            trim=flags_only,
        )
        extra = {"nocache": "1"} if backend == "victoriametrics" else {}
        w = (s0, s0 + 2700)
        # synthetic history: gap fill rule
        for case, interval in (("i15", 15), ("i60", 60)):
            rec.get(
                f"gapfill_{case}_raw",
                "/api/v1/query_range",
                qr(f'syn_gauge{{case="{case}"}}', *w, "5s", **extra),
                note=f"raw selector, 5 s step over a series scraped every {interval}s with known gaps",
            )
            rec.get(
                f"gapfill_{case}_samples",
                "/api/v1/query",
                {"query": f'syn_gauge{{case="{case}"}}[1h]', "time": f"{s0 + 3000:.0f}"},
                note="the real samples (ground truth for gap positions)",
            )
        for step in (10, 30, 60):
            rec.get(
                f"gapfill_i60_step{step}",
                "/api/v1/query_range",
                qr('syn_gauge{case="i60"}', *w, f"{step}s", **extra),
            )
        for w_s in (15, 30, 60, 300):
            rec.get(
                f"increase_w{w_s}",
                "/api/v1/query_range",
                qr(f"increase(syn_counter_total[{w_s}s])", s0, s0 + 2100, f"{w_s}s", **extra),
                note="counter +15/15s (truth 1/s), 10 min gap from +600 s, reset at +1500 s",
            )
        for expr, tag in (
            ('count_over_time(syn_gauge{case="i15"}[15s])', "count_w15"),
            ('count_over_time(syn_gauge{case="i15"}[60s])', "count_w60"),
            ('max_over_time(syn_gauge{case="i15"}[15s])', "max_w15"),
            ("resets(syn_counter_total[15s])", "resets_w15"),
            ("resets(syn_counter_total[30s])", "resets_w30"),
            ("rate(syn_counter_total[15s])", "rate_w15"),
            ("rate(syn_counter_total[60s])", "rate_w60"),
        ):
            step = "15s" if "w15" in tag or "w30" in tag else "60s"
            rec.get(tag, "/api/v1/query_range", qr(expr, s0, s0 + 2200, step, **extra))
        for tag, expr in (
            ("count_selector_w60", 'count_over_time(syn_gauge{case="i60"}[60s])'),
            ("count_subquery_w60", 'count_over_time((syn_gauge{case="i60"} * 1)[60s:15s])'),
            (
                "count_subquery_rate_w60",
                'count_over_time((rate(syn_counter_total{case="i15"}[1m]))[60s:15s])',
            ),
        ):
            rec.get(
                tag,
                "/api/v1/query_range",
                qr(expr, s0, s0 + 2900, "60s", **extra),
                note="adapter windows: selector -> x[step]; expression -> (expr)[step:resolution]",
            )
        if backend == "victoriametrics":
            rec.get(
                "rollup_w60",
                "/api/v1/query_range",
                qr('rollup(syn_gauge{case="i60"}[60s])', s0, s0 + 2900, "60s", **extra),
                note="the adapter's VM query: absent (not filled) buckets inside a gap?",
            )
        rec.get(
            "single_count",
            "/api/v1/query_range",
            qr(
                'count_over_time(syn_gauge{case="single"}[60s])',
                s0 + 1100,
                s0 + 1300,
                "60s",
                **extra,
            ),
        )
        rec.get(
            "single_raw",
            "/api/v1/query_range",
            qr('syn_gauge{case="single"}', s0 + 1100, s0 + 1800, "60s", **extra),
            note="one isolated sample at +1200 s",
        )
        rec.get(
            "single_rate",
            "/api/v1/query_range",
            qr('rate(syn_counter_total{case="i15"}[5s])', s0, s0 + 100, "15s", **extra),
            note="window shorter than the sample interval",
        )
        # scraped scenario
        for tag, expr, step in (
            ("up", 'up{job="lab"}', "5s"),
            ("gauge_raw", 'lab_gauge{job="lab"}', "5s"),
            ("gauge_count", 'count_over_time(lab_gauge{job="lab"}[5s])', "5s"),
            ("gauge_count_w15", 'count_over_time(lab_gauge{job="lab"}[15s])', "15s"),
            ("vanish_raw", 'lab_vanish{job="lab"}', "5s"),
            ("vanish_count", 'count_over_time(lab_vanish{job="lab"}[5s])', "5s"),
            ("ctr_raw", 'lab_requests_total{job="lab"}', "5s"),
            ("ctr_increase", 'increase(lab_requests_total{job="lab"}[15s])', "15s"),
            ("ctr_resets", 'resets(lab_requests_total{job="lab"}[15s])', "15s"),
            ("rw_gauge_raw", 'lab_gauge{job="lab-rw"}', "5s"),
            ("rw_gauge_count", 'count_over_time(lab_gauge{job="lab-rw"}[5s])', "5s"),
            ("rw_stalemark_raw", 'lab_rw_stalemark{job="lab-rw"}', "5s"),
            ("rw_vanish_raw", 'lab_vanish{job="lab-rw"}', "5s"),
            ("rw_ctr_increase", 'increase(lab_requests_total{job="lab-rw"}[15s])', "15s"),
        ):
            rec.get(
                f"scrape_{tag}",
                "/api/v1/query_range",
                qr(expr, t0 - 10, t0 + tl["end_s"], step, **extra),
            )
        rec.get(
            "scrape_gauge_samples",
            "/api/v1/query",
            {"query": 'lab_gauge{job="lab"}[20m]', "time": f"{t0 + tl['end_s']:.0f}"},
            note="raw samples incl. whether stale markers surface (they never do)",
        )
        # partial histogram buckets (seeded: le=1 missing for 30 samples)
        rec.get(
            "hist_increase",
            "/api/v1/query_range",
            qr("sum by (le) (increase(syn_hist_bucket[30s]))", s0, s0 + 1800, "15s", **extra),
            note="le=1 bucket series absent for samples 60..89; others present",
        )
        rec.get(
            "hist_scrape_increase",
            "/api/v1/query_range",
            qr(
                "sum by (le) (increase(lab_hist_bucket[15s]))", t0 + 1060, t0 + 1200, "15s", **extra
            ),
            note="le=1 absent in a successful scrape for 1100-1160 s",
        )
        # recency / latency offset
        rec.get(
            "recent_up",
            "/api/v1/query_range",
            qr('up{job="lab"}', time.time() - 120, time.time(), "5s", **extra),
            note="how close to now do points go (latencyOffset)",
        )
        rec.get(
            "recent_instant",
            "/api/v1/query",
            {"query": 'up{job="lab"}[1m]', "time": f"{time.time():.0f}"},
        )
    # limits
    p0 = s0
    lim_calls = (
        ("prom-limits", "prometheus", "limit_max_samples", "syn_gauge", p0, p0 + 2700, "5s"),
        (
            "prom-limits",
            "prometheus",
            "limit_max_samples_count",
            "count_over_time(syn_gauge[1m])",
            p0,
            p0 + 2700,
            "5s",
        ),
        ("prom", "prometheus", "limit_points", "syn_gauge", p0, p0 + 11001, "1s"),
        ("prom", "prometheus", "limit_parse", "syn_gauge{", p0, p0 + 60, "5s"),
        (
            "vm-limits",
            "victoriametrics",
            "limit_points",
            "syn_gauge{case='i15'}",
            p0,
            p0 + 2700,
            "5s",
        ),
        (
            "vm-limits",
            "victoriametrics",
            "limit_series",
            "{__name__=~'syn.*'}",
            p0,
            p0 + 2700,
            "60s",
        ),
        ("vm", "victoriametrics", "limit_points_default", "syn_gauge", p0, p0 + 200000, "1s"),
    )
    for lab_name, backend, claim, expr, a, b, step in lim_calls:
        Rec(backend, lab_name, LAB[lab_name], False).get(
            claim, "/api/v1/query_range", qr(expr, a, b, step)
        )


if __name__ == "__main__":
    {"local": local, "public": public}[sys.argv[1]]()
