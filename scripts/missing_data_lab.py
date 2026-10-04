# /// script
# requires-python = ">=3.12"
# dependencies = ["cramjam", "httpx"]
# ///
"""Ground-truth lab for missing-data research (bead telemetry-nerd-1h9.10).

Targets the stack in deploy/missing-data-lab/compose.yml (`just lab-up`):
  prom 19090, prom-limits 19091, vm 18428, vm-limits 18429.

  uv run scripts/missing_data_lab.py seed      # synthetic history (known gaps/resets) into all four
  uv run scripts/missing_data_lab.py serve     # ~21 min: scrape exporter + remote-write pusher
                                               # following TIMELINE; writes timeline.json at the end

`seed` writes controlled history through remote write (Prometheus, VM) so gap-fill rules can be
measured without waiting. `serve` runs a real scrape target on :9101 that fails/omits series on
a fixed timeline, and pushes the same data through remote write WITHOUT staleness markers (the
OTLP/remote-write ingestion shape), so scraped and pushed gap behaviour can be compared.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

PROM = "http://127.0.0.1:19090"
PROM_LIM = "http://127.0.0.1:19091"
VM = "http://127.0.0.1:18428"
VM_LIM = "http://127.0.0.1:18429"
STALE_NAN = struct.unpack("<d", struct.pack("<Q", 0x7FF0000000000002))[0]

# seconds since t0. kind: down = scrape returns 503 (target down); vanish/hist_hole = series
# missing from an otherwise successful scrape; reset = counter restarts from 0.
TIMELINE = {
    "end_s": 1260,
    "down": [[120, 300], [480, 900]],  # 3 min and 7 min (lookback delta is 5 min)
    "vanish": [[960, 1020]],  # lab_vanish absent while target is up
    "reset_s": 1050,
    "hist_hole": [[1100, 1160]],  # lab_hist_bucket{le="1"} absent while the others are present
    "scrape_interval_s": 5,
}


def _in(t: float, spans: list[list[int]]) -> bool:
    return any(a <= t < b for a, b in spans)


# --- remote write (protobuf, hand-encoded; no generated code) -----------------------------


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _ld(field: int, payload: bytes) -> bytes:
    return _varint(field << 3 | 2) + _varint(len(payload)) + payload


def _label(k: str, v: str) -> bytes:
    return _ld(1, _ld(1, k.encode()) + _ld(2, v.encode()))


def _sample(value: float, ts_ms: int) -> bytes:
    return _ld(2, b"\x09" + struct.pack("<d", value) + b"\x10" + _varint(ts_ms))


def write_request(series: list[tuple[dict[str, str], list[tuple[int, float]]]]) -> bytes:
    body = b""
    for labels, samples in series:
        ts = b"".join(_label(k, labels[k]) for k in sorted(labels))
        ts += b"".join(_sample(v, t) for t, v in samples)
        body += _ld(1, ts)
    import cramjam  # script-only dependency; lazy so tests can import seed_series (y7hb)

    return bytes(cramjam.snappy.compress_raw(body))


def remote_write(base: str, series: list, path: str = "/api/v1/write") -> httpx.Response:
    return httpx.post(
        base + path,
        content=write_request(series),
        headers={
            "Content-Encoding": "snappy",
            "Content-Type": "application/x-protobuf",
            "X-Prometheus-Remote-Write-Version": "0.1.0",
        },
        timeout=30,
    )


# --- seed: controlled history ------------------------------------------------------------


def _gappy(start_ms: int, interval_s: int, present: int, gaps: list[int]) -> list[int]:
    """Timestamps: `present` samples, then a gap of k missing samples, repeated; ends present."""
    ts, i = [], 0
    for k in [*gaps, 0]:
        for _ in range(present):
            ts.append(start_ms + i * interval_s * 1000)
            i += 1
        i += k
    return ts


def seed_series(now_ms: int) -> list[tuple[dict[str, str], list[tuple[int, float]]]]:
    start = (now_ms - 47 * 60_000) // 60_000 * 60_000
    out = []
    i15 = _gappy(start, 15, 15, [1, 2, 3, 5, 10, 20])
    out.append(({"__name__": "syn_gauge", "job": "syn", "case": "i15"}, [(t, 1.0) for t in i15]))
    i60 = _gappy(start, 60, 3, [1, 2, 3, 4, 5, 8])
    out.append(({"__name__": "syn_gauge", "job": "syn", "case": "i60"}, [(t, 1.0) for t in i60]))
    # counter, +15 per 15 s sample (truth: exactly 1/s). samples 40..79 missing (10 min gap),
    # restart from 0 at sample 100.
    ctr = []
    for i in range(140):
        if 40 <= i < 80:
            continue
        v = 15.0 * (i + 1) if i < 100 else 15.0 * (i - 100 + 1)
        ctr.append((start + i * 15_000, v))
    out.append(({"__name__": "syn_counter_total", "job": "syn", "case": "i15"}, ctr))
    # one isolated sample (rate with a single point)
    out.append(
        ({"__name__": "syn_gauge", "job": "syn", "case": "single"}, [(start + 20 * 60_000, 7.0)])
    )
    # cumulative classic histogram; le="1" exists only for the first half (partial scrape)
    for le, base in (("1", 1), ("5", 3), ("+Inf", 4)):
        pts = [
            (start + i * 15_000, float(base * (i + 1)))
            for i in range(120)
            if not (le == "1" and 60 <= i < 90)
        ]
        out.append(({"__name__": "syn_hist_bucket", "job": "syn", "le": le}, pts))
    return out


def cmd_seed() -> None:
    now_ms = int(time.time() * 1000)
    series = seed_series(now_ms)
    for name, base in (
        ("prom", PROM),
        ("prom-limits", PROM_LIM),
        ("vm", VM),
        ("vm-limits", VM_LIM),
    ):
        for s in series:
            r = remote_write(base, [s])
            if r.status_code >= 300:
                print(
                    f"{name}: {s[0]['__name__']} {s[0].get('case', '')} -> {r.status_code} {r.text[:200]}"
                )
        print(f"{name}: seeded {len(series)} series")
    start = (now_ms - 47 * 60_000) // 60_000 * 60_000
    out = Path("tests/fixtures/missing-data/local")
    out.mkdir(parents=True, exist_ok=True)
    (out / "seed.json").write_text(
        json.dumps(
            {
                "start_ms": start,
                "series": [
                    {
                        "labels": s[0],
                        "samples": len(s[1]),
                        "first_ms": s[1][0][0],
                        "last_ms": s[1][-1][0],
                    }
                    for s in series
                ],
            },
            indent=1,
        )
        + "\n"
    )


# --- write-side probes: out-of-order, duplicates, retention ------------------------------------


def _save(backend: str, src: str, claim: str, note: str, body: dict) -> None:
    out = Path("tests/fixtures/missing-data") / backend
    out.mkdir(parents=True, exist_ok=True)
    rec = {"claim": claim, "source": src, "note": note, "status": 200, "body": body}
    (out / f"{src}__{claim}.json").write_text(json.dumps(rec, separators=(",", ":")) + "\n")
    print(f"  {src}:{claim}")


def _readback(base: str, name: str, now_ms: int, lookback: str = "30m") -> list:
    params = {"query": f"{name}[{lookback}]", "time": f"{now_ms / 1000:.3f}"}
    r = httpx.get(base + "/api/v1/query", params=params, timeout=30)
    return r.json()["data"]["result"]


def cmd_probes() -> None:
    now = int(time.time() * 1000)
    t = now - 20 * 60_000
    for src, base, backend in (("prom", PROM, "prometheus"), ("vm", VM, "victoriametrics")):
        name = f"ooo_probe_{now}"
        lab = {"__name__": name, "job": "ooo"}
        r1 = remote_write(base, [(lab, [(t, 1.0), (t + 60_000, 2.0), (t + 30_000, 3.0)])])
        r2 = remote_write(base, [(lab, [(t + 60_000, 9.0)])])
        time.sleep(6)  # new series become searchable after a short index delay
        _save(
            backend,
            src,
            "ooo_write",
            "one write of samples at t, t+60s, t+30s (out of order); then t+60s again with a "
            "different value (duplicate timestamp)",
            {
                "ooo_status": r1.status_code,
                "ooo_text": r1.text[:300],
                "dup_status": r2.status_code,
                "dup_text": r2.text[:300],
                "t_ms": t,
                "readback": _readback(base, name, int(time.time() * 1000), "40m"),
            },
        )
    # retention: samples 3 days old to a 1-day-retention VM vs a 30-day VM
    old = now - 3 * 86_400_000
    out = {}
    for src, base in (("vm-limits", VM_LIM), ("vm", VM)):
        name = f"ret_probe_{now}"
        r = remote_write(
            base,
            [({"__name__": name, "job": "ret"}, [(old + i * 60_000, 1.0) for i in range(5)])],
        )
        time.sleep(8)  # index delay for new series
        m = httpx.get(base + "/metrics", timeout=30).text
        ignored = [
            ln for ln in m.splitlines() if ln.startswith('vm_rows_ignored_total{reason="small')
        ]
        rb = httpx.get(
            base + "/api/v1/query_range",
            params={
                "query": name,
                "start": old // 1000 - 60,
                "end": old // 1000 + 600,
                "step": "60s",
            },
            timeout=30,
        ).json()["data"]["result"]
        out[src] = {
            "write_status": r.status_code,
            "ignored": ignored,
            "readback": rb,
            "age_ms": now - old,
        }
    _save(
        "victoriametrics",
        "vm-limits",
        "retention_write",
        "3-day-old samples written to -retentionPeriod=1d vs 30d",
        out,
    )


# --- serve: scrape target + pusher ---------------------------------------------------------

T0 = 0.0
RESET_BASE = [0.0]


def lab_values(t: float) -> dict[str, float]:
    """Deterministic values from elapsed seconds, so ground truth is computable offline."""
    reset_s = TIMELINE["reset_s"]
    ctr = 10.0 * t if t < reset_s else 10.0 * (t - reset_s)
    return {"gauge": 10.0 + t // 60, "ctr": ctr, "vanish": 42.0}


def exposition(t: float) -> str:
    v = lab_values(t)
    lines = [
        "# TYPE lab_gauge gauge",
        f"lab_gauge {v['gauge']}",
        "# TYPE lab_requests_total counter",
        f"lab_requests_total {v['ctr']}",
    ]
    if not _in(t, TIMELINE["vanish"]):
        lines += ["# TYPE lab_vanish gauge", f"lab_vanish {v['vanish']}"]
    n = int(t // 5)
    lines.append("# TYPE lab_hist histogram")
    for le, base in (("1", 1), ("5", 3), ("+Inf", 4)):
        if le == "1" and _in(t, TIMELINE["hist_hole"]):
            continue
        lines.append(f'lab_hist_bucket{{le="{le}"}} {base * n}')
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        t = time.time() - T0
        if _in(t, TIMELINE["down"]):
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"down\n")
            return
        body = exposition(t).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a: object) -> None:
        pass


def _safe_write(base: str, series: list) -> None:
    try:
        remote_write(base, series)
    except httpx.HTTPError as e:  # a dropped push is a gap like any other; keep the run going
        print(f"push to {base} failed: {e}", flush=True)


def pusher(stop: threading.Event) -> None:
    """Remote-write the same series (job=lab-rw) every 5 s, skipping the down windows, no stale
    markers, except lab_rw_stalemark which gets an explicit stale NaN at each gap start."""
    stale_sent: set[int] = set()
    while not stop.wait(5.0):
        now = time.time()
        t = now - T0
        ts = int(now * 1000)
        if _in(t, TIMELINE["down"]):
            for a, b in TIMELINE["down"]:
                if a <= t < b and a not in stale_sent:
                    stale_sent.add(a)
                    s = [({"__name__": "lab_rw_stalemark", "job": "lab-rw"}, [(ts, STALE_NAN)])]
                    for base in (PROM, VM):
                        _safe_write(base, s)
            continue
        v = lab_values(t)
        series = [
            ({"__name__": "lab_gauge", "job": "lab-rw"}, [(ts, v["gauge"])]),
            ({"__name__": "lab_requests_total", "job": "lab-rw"}, [(ts, v["ctr"])]),
            ({"__name__": "lab_rw_stalemark", "job": "lab-rw"}, [(ts, 1.0)]),
        ]
        if not _in(t, TIMELINE["vanish"]):
            series.append(({"__name__": "lab_vanish", "job": "lab-rw"}, [(ts, v["vanish"])]))
        for base in (PROM, VM):
            _safe_write(base, series)


def cmd_serve() -> None:
    global T0
    T0 = time.time()
    out = Path("tests/fixtures/missing-data/local")
    out.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    srv = ThreadingHTTPServer(("0.0.0.0", 9101), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=pusher, args=(stop,), daemon=True).start()
    print(f"t0={T0:.3f} ({TIMELINE['end_s']} s run)", flush=True)
    time.sleep(TIMELINE["end_s"])
    stop.set()
    srv.shutdown()
    (out / "timeline.json").write_text(
        json.dumps({"t0_ms": round(T0 * 1000), **TIMELINE}, indent=1) + "\n"
    )
    print("done; wrote timeline.json", flush=True)
    assert math.isfinite(T0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["seed", "serve", "probes"])
    {"seed": cmd_seed, "serve": cmd_serve, "probes": cmd_probes}[ap.parse_args().cmd]()
