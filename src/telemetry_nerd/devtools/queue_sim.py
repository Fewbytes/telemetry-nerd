"""queue-sim: a Prometheus exporter over simulated queues with known parameters (bead 1h9.17).

Ground truth for Little's law (and later USL) checks. Each simulated *instance* is an M/G/c queue
(Poisson arrivals, c servers, FCFS, general service time with mean 1/mu) run as a discrete-event
simulation. The engine is pure virtual time (`Sim.advance(t)`); `serve` maps it to wall-clock time
and exposes, at `/metrics` (also `/truth`, `/healthz`), per instance:

    http_requests_total{service,pod}                   counter (counts arrivals or completions)
    http_request_duration_seconds_{bucket,sum,count}   classic histogram of the *timed* latency
    http_server_active_requests{service,pod}           in-flight gauge (arrived, not yet completed)

Knobs (`Params`, per instance, changeable on a schedule by a scenario fault):

    lam, mu, c            arrival rate (req/s/instance), service rate (1/s/server), servers
    dist, cv              service time: exp | lognormal | gamma | det with coefficient of variation cv
    counter               arrivals | completions: what http_requests_total counts
    timer                 arrival | service_start: where the latency timer starts; service_start
                          leaves queueing unmeasured (hidden queueing: L > lambda W)
    pre_delay             mean seconds (exp) in the gauge but before the timer (hidden, infinite server)
    leak                  fraction of arrivals that get stuck: counted in flight forever, never timed
    export_gauge          false: this instance's in-flight gauge is not exported (L < lambda W)
    sigma, kappa          USL: a request starting service with N in service takes
                          1 + sigma (N-1) + kappa N (N-1) times longer

Scenario (YAML/JSON): name, description, duration_s, seed, scrape_interval_s, service, instances
(count or names), defaults (Params), overrides {instance: Params}, faults [{kind, at, until, instances,
set, expect}] and an overall `expect`. Faults apply `set` over [at, until) (until omitted = to the
end) and are the ground truth written to JSON: kind, window, instances, expected Little's law
verdict/classification. Run: `just queue-sim <scenario>`; scrape target: <host>:9201/metrics.
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, fields, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import yaml

METRIC_REQUESTS = "http_requests_total"
METRIC_LATENCY = "http_request_duration_seconds"
METRIC_ACTIVE = "http_server_active_requests"
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 25.0, 60.0)
DEFAULT_PORT = 9201


@dataclass(frozen=True)
class Params:
    lam: float = 8.0
    mu: float = 1.0
    c: int = 10
    dist: str = "exp"
    cv: float = 1.0
    counter: str = "completions"
    timer: str = "arrival"
    pre_delay: float = 0.0
    leak: float = 0.0
    export_gauge: bool = True
    sigma: float = 0.0
    kappa: float = 0.0

    def __post_init__(self) -> None:
        for name, ok in (
            ("dist", ("exp", "lognormal", "gamma", "det")),
            ("counter", ("arrivals", "completions")),
            ("timer", ("arrival", "service_start")),
        ):
            if getattr(self, name) not in ok:
                raise ValueError(f"{name} must be one of {ok}, got {getattr(self, name)!r}")
        if self.lam < 0 or self.mu <= 0 or self.c < 1 or not 0 <= self.leak <= 1:
            raise ValueError(f"invalid parameters: {self}")


_ALIASES = {"lambda": "lam"}
_FIELDS = {f.name for f in fields(Params)}


def make_params(base: Params, changes: dict) -> Params:
    changes = {_ALIASES.get(k, k): v for k, v in changes.items()}
    unknown = set(changes) - _FIELDS
    if unknown:
        raise ValueError(f"unknown parameter(s) {sorted(unknown)}; known: {sorted(_FIELDS)}")
    return replace(base, **changes)


@dataclass(frozen=True)
class Fault:
    kind: str
    at: float
    until: float | None
    instances: tuple[str, ...]
    set: dict
    expect: dict


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    duration_s: float
    seed: int
    scrape_interval_s: float
    service: str
    instances: tuple[str, ...]
    defaults: dict
    overrides: dict
    faults: tuple[Fault, ...]
    expect: dict


def load_scenario(path: str | Path) -> Scenario:
    raw = yaml.safe_load(Path(path).read_text())
    n = raw.get("instances", 3)
    names = (
        tuple(f"{raw.get('service', 'queue-sim')}-{i}" for i in range(n))
        if isinstance(n, int)
        else tuple(n)
    )
    faults = []
    for f in raw.get("faults", []):
        sel = f.get("instances", "all")
        targets = names if sel == "all" else tuple(sel)
        if set(targets) - set(names):
            raise ValueError(
                f"fault {f.get('kind')}: unknown instances {set(targets) - set(names)}"
            )
        faults.append(
            Fault(f["kind"], float(f.get("at", 0.0)), f.get("until"), targets, f["set"],
                  f.get("expect", {}))
        )  # fmt: skip
    return Scenario(
        name=raw["name"], description=raw.get("description", ""),
        duration_s=float(raw["duration_s"]), seed=int(raw.get("seed", 1)),
        scrape_interval_s=float(raw.get("scrape_interval_s", 5)),
        service=raw.get("service", "queue-sim"), instances=names,
        defaults=raw.get("defaults", {}), overrides=raw.get("overrides", {}),
        faults=tuple(faults), expect=raw.get("expect", {}),
    )  # fmt: skip


class _Instance:
    def __init__(self, name: str, params: Params) -> None:
        self.name, self.p = name, params
        self.epoch = 0  # invalidates a scheduled arrival when the rate changes
        self.waiting: deque[list[float]] = deque()  # [timer_start_or_nan, entered]
        self.busy = 0
        self.arrivals = self.completions = self.requests = 0
        self.in_flight = 0
        self.lat_sum = 0.0
        self.bucket_counts = [0] * len(BUCKETS)  # cumulative filled at render
        self.n_timed = 0


class Sim:
    """Virtual-time discrete-event simulation of the scenario's instances."""

    def __init__(self, scenario: Scenario) -> None:
        self.sc = scenario
        self.rng = np.random.default_rng(scenario.seed)
        self.now = 0.0
        self._seq = 0
        self._heap: list[tuple[float, int, str, object, object]] = []
        self.inst = {
            n: _Instance(n, make_params(make_params(Params(), scenario.defaults),
                                        scenario.overrides.get(n, {})))
            for n in scenario.instances
        }  # fmt: skip
        self._base = {n: i.p for n, i in self.inst.items()}
        self._active: list[int] = []
        for i, f in enumerate(scenario.faults):
            self._push(f.at, "fault_on", i, None)
            if f.until is not None:
                self._push(f.until, "fault_off", i, None)
        for i in self.inst.values():
            self._schedule_arrival(i)

    # --- event plumbing
    def _push(self, t: float, kind: str, a: object, b: object) -> None:
        self._seq += 1
        heapq.heappush(self._heap, (t, self._seq, kind, a, b))

    def advance(self, t: float) -> None:
        while self._heap and self._heap[0][0] <= t:
            ts, _, kind, a, b = heapq.heappop(self._heap)
            self.now = ts
            getattr(self, f"_on_{kind}")(a, b)
        self.now = max(self.now, t)

    def _schedule_arrival(self, i: _Instance) -> None:
        if i.p.lam > 0:
            self._push(self.now + self.rng.exponential(1 / i.p.lam), "arrive", i, i.epoch)

    def _service_time(self, p: Params) -> float:
        m = 1 / p.mu
        if p.dist == "exp":
            return self.rng.exponential(m)
        if p.dist == "det" or p.cv <= 0:
            return m
        if p.dist == "lognormal":
            s2 = math.log(1 + p.cv**2)
            return self.rng.lognormal(math.log(m) - s2 / 2, math.sqrt(s2))
        k = 1 / p.cv**2  # gamma
        return self.rng.gamma(k, m / k)

    # --- faults
    def _apply_faults(self) -> None:
        for n, i in self.inst.items():
            p = self._base[n]
            for fi in self._active:
                f = self.sc.faults[fi]
                if n in f.instances:
                    p = make_params(p, f.set)
            if p.lam != i.p.lam:
                i.epoch += 1
                i.p = p
                self._schedule_arrival(i)
            else:
                i.p = p
            self._fill_servers(i)

    def _on_fault_on(self, fi: int, _: object) -> None:
        self._active.append(fi)
        self._apply_faults()

    def _on_fault_off(self, fi: int, _: object) -> None:
        self._active.remove(fi)
        self._apply_faults()

    # --- queue
    def _on_arrive(self, i: _Instance, epoch: int) -> None:
        if epoch != i.epoch:
            return
        i.arrivals += 1
        i.in_flight += 1
        if i.p.counter == "arrivals":
            i.requests += 1
        self._schedule_arrival(i)
        if self.rng.random() < i.p.leak:
            return  # stuck: in flight forever, never timed, never served
        if i.p.pre_delay > 0:
            self._push(self.now + self.rng.exponential(i.p.pre_delay), "enter", i, None)
        else:
            self._on_enter(i, None)

    def _on_enter(self, i: _Instance, _: object) -> None:
        i.waiting.append([self.now if i.p.timer == "arrival" else math.nan])
        self._fill_servers(i)

    def _fill_servers(self, i: _Instance) -> None:
        while i.waiting and i.busy < i.p.c:
            req = i.waiting.popleft()
            i.busy += 1
            p = i.p
            dilation = 1 + p.sigma * (i.busy - 1) + p.kappa * i.busy * (i.busy - 1)
            if math.isnan(req[0]):
                req[0] = self.now
            self._push(self.now + self._service_time(p) * dilation, "depart", i, req[0])

    def _on_depart(self, i: _Instance, timer_start: float) -> None:
        i.busy -= 1
        i.in_flight -= 1
        i.completions += 1
        if i.p.counter == "completions":
            i.requests += 1
        lat = self.now - timer_start
        i.lat_sum += lat
        i.n_timed += 1
        for k, le in enumerate(BUCKETS):
            if lat <= le:
                i.bucket_counts[k] += 1
                break
        self._fill_servers(i)

    # --- exposition
    def render(self) -> str:
        out = [f"# HELP {METRIC_REQUESTS} Requests (arrivals or completions, per scenario).\n"]
        out.append(f"# TYPE {METRIC_REQUESTS} counter\n")
        lb = lambda i: f'service="{self.sc.service}",pod="{i.name}"'
        for i in self.inst.values():
            out.append(f"{METRIC_REQUESTS}{{{lb(i)}}} {i.requests}\n")
        out.append(f"# TYPE {METRIC_LATENCY} histogram\n")
        for i in self.inst.values():
            cum = 0
            for le, n in zip(BUCKETS, i.bucket_counts, strict=True):
                cum += n
                out.append(f'{METRIC_LATENCY}_bucket{{{lb(i)},le="{le}"}} {cum}\n')
            out.append(f'{METRIC_LATENCY}_bucket{{{lb(i)},le="+Inf"}} {i.n_timed}\n')
            out.append(f"{METRIC_LATENCY}_sum{{{lb(i)}}} {i.lat_sum!r}\n")
            out.append(f"{METRIC_LATENCY}_count{{{lb(i)}}} {i.n_timed}\n")
        out.append(f"# TYPE {METRIC_ACTIVE} gauge\n")
        for i in self.inst.values():
            if i.p.export_gauge:
                out.append(f"{METRIC_ACTIVE}{{{lb(i)}}} {i.in_flight}\n")
        return "".join(out)


# --- ground truth


def exact_windows(sc: Scenario, spans_s: list[tuple[float, float]], dt: float = 0.02) -> list[dict]:
    """The exact Little's law quantities per window (sim seconds, ascending, non-overlapping),
    summed over the instances, from the simulation itself (deterministic for a seed): L the time
    average of the EXPORTED in-flight gauge (integrated every `dt`), lambda what the request
    counter counts per second, W the mean timed latency of the window's completions, R = L /
    (lambda W). What a perfect instrument would report for the check's windows."""

    def totals(sim: Sim) -> tuple[float, float, float, float]:
        ins = sim.inst.values()
        return (
            float(sum(i.in_flight for i in ins if i.p.export_gauge)),
            float(sum(i.requests for i in ins)),
            float(sum(i.lat_sum for i in ins)),
            float(sum(i.n_timed for i in ins)),
        )

    sim, out = Sim(sc), []
    for a, b in spans_s:
        sim.advance(a)
        _, A0, S0, C0 = totals(sim)
        k = max(1, round((b - a) / dt))
        h = (b - a) / k
        integral = 0.0
        for j in range(k):
            sim.advance(a + (j + 0.5) * h)
            integral += totals(sim)[0] * h
        sim.advance(b)
        _, A1, S1, C1 = totals(sim)
        L, lam = integral / (b - a), (A1 - A0) / (b - a)
        W = (S1 - S0) / (C1 - C0) if C1 > C0 else math.nan
        out.append({"start_s": a, "end_s": b, "L": L, "lambda": lam, "W": W,
                    "R": L / (lam * W) if lam > 0 and W > 0 else math.nan})  # fmt: skip
    return out


def _overload_effect(sc: Scenario, f: Fault, end: float) -> dict:
    """Backlog built and the time it takes to drain, for the most overloaded targeted instance."""
    peak, drain_end = 0.0, f.until if f.until is not None else end
    for n in f.instances:
        p = make_params(
            make_params(make_params(Params(), sc.defaults), sc.overrides.get(n, {})), f.set
        )
        base = make_params(make_params(Params(), sc.defaults), sc.overrides.get(n, {}))
        cap = p.c * p.mu
        if f.until is not None and p.lam > cap:
            backlog = (p.lam - cap) * (f.until - f.at)
            peak = max(peak, backlog)
            if cap > base.lam:
                drain_end = max(drain_end, f.until + backlog / (cap - base.lam))
    return {"backlog_peak_per_instance": round(peak, 1), "drain_end_s": round(drain_end, 1)}


def ground_truth(sc: Scenario, started_unix_ms: int, ended_unix_ms: int | None = None) -> dict:
    faults = []
    for f in sc.faults:
        end = f.until if f.until is not None else sc.duration_s
        d = {
            "kind": f.kind, "start_s": f.at, "end_s": end,
            "start_unix_ms": started_unix_ms + int(f.at * 1000),
            "end_unix_ms": started_unix_ms + int(end * 1000),
            "instances": list(f.instances), "set": f.set, "expect": f.expect,
        }  # fmt: skip
        if f.kind == "overload":
            d["effect"] = _overload_effect(sc, f, sc.duration_s)
        faults.append(d)
    return {
        "scenario": sc.name, "description": sc.description, "service": sc.service,
        "instances": list(sc.instances), "group_by": "pod",
        "metrics": {"arrival_rate": METRIC_REQUESTS, "latency": METRIC_LATENCY,
                    "concurrency": METRIC_ACTIVE},
        "duration_s": sc.duration_s, "scrape_interval_s": sc.scrape_interval_s,
        "started_unix_ms": started_unix_ms, "ended_unix_ms": ended_unix_ms,
        "defaults": sc.defaults, "overrides": sc.overrides, "faults": faults,
        "expect": sc.expect,
    }  # fmt: skip


# --- real-time server


def serve(sc: Scenario, port: int, truth_path: Path | None, host: str = "0.0.0.0",
          speed: float = 1.0) -> None:  # fmt: skip
    sim = Sim(sc)
    lock = threading.Lock()
    t0 = time.monotonic()
    started = int(time.time() * 1000)
    done = threading.Event()
    finished = False

    def sim_now() -> float:
        return (time.monotonic() - t0) * speed

    def write_truth(ended: int | None) -> None:
        if truth_path:
            truth_path.parent.mkdir(parents=True, exist_ok=True)
            truth_path.write_text(json.dumps(ground_truth(sc, started, ended), indent=2) + "\n")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.startswith("/metrics"):
                with lock:
                    sim.advance(sim_now())
                    body, ctype = sim.render().encode(), "text/plain; version=0.0.4"
            elif self.path.startswith("/truth"):
                body = json.dumps(ground_truth(sc, started)).encode()
                ctype = "application/json"
            elif self.path.startswith("/healthz"):
                body, ctype = b"ok\n", "text/plain"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: object) -> None:
            pass

    def pump() -> None:  # keeps the queue moving between scrapes
        nonlocal finished
        while not done.wait(0.05):
            with lock:
                sim.advance(sim_now())
                if sim.now >= sc.duration_s and not finished:
                    finished = True
                    write_truth(int(time.time() * 1000))

    server = ThreadingHTTPServer((host, port), Handler)
    write_truth(None)
    print(f"queue-sim '{sc.name}' on http://{host}:{port}/metrics for {sc.duration_s:.0f}s "
          f"(truth: {truth_path})", flush=True)  # fmt: skip
    threading.Thread(target=pump, daemon=True).start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        while sim_now() < sc.duration_s + 2 * sc.scrape_interval_s:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        done.set()
        server.shutdown()
        if not finished:
            write_truth(int(time.time() * 1000))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("scenario", help="scenario YAML/JSON file or the name of a bundled one")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument(
        "--truth", type=Path, help="ground-truth JSON (default build/queue-sim/<name>.json)"
    )
    ap.add_argument("--speed", type=float, default=1.0, help="sim seconds per wall second")
    args = ap.parse_args(argv)
    sc = load_scenario(resolve_scenario(args.scenario))
    serve(
        sc,
        args.port,
        args.truth or Path("build/queue-sim") / f"{sc.name}.json",
        args.host,
        args.speed,
    )


SCENARIO_DIR = Path(__file__).resolve().parents[3] / "deploy" / "queue-sim" / "scenarios"


def resolve_scenario(name: str) -> Path:
    p = Path(name)
    if p.exists():
        return p
    for ext in (".yaml", ".yml", ".json"):
        q = SCENARIO_DIR / f"{name}{ext}"
        if q.exists():
            return q
    raise SystemExit(f"scenario {name!r} not found (looked in {SCENARIO_DIR})")


if __name__ == "__main__":
    main()
