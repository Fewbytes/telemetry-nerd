"""Ground truth for scenario evals, normalised from two producers.

Demo scenarios (scripts/scenario.py, `version` 1, docs/demo.md "Ground-truth JSON"): entities are
services (`source_labels.service`, normally `service_name`); `affected_services` splits them into
origin (root cause), propagated (may be named as symptoms) and unaffected_control (must never be
blamed); `fault_window` + `tolerance` judge annotations.

queue-sim (devtools.queue_sim.ground_truth): entities are instances (label `pod`) of one service;
the faulted instances are the origin, the others controls. Expected sources of variation follow
the fault kind: an overload is a special cause; hidden queueing and a missing gauge are the
measurement system (the instrument, not the process); a leak shows as a drifting offset that is
both a process problem and invisible to the latency timer, so either label is accepted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

SPECIAL = "special_cause"
COMMON = "common_cause"
MEASUREMENT = "measurement_system"
UNDETERMINED = "undetermined"

#: queue-sim fault kind -> (accepted variation sources, words that name the mechanism)
QUEUE_SIM_KINDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "overload": (
        (SPECIAL,),
        ("overload", "arrival rate", "arrivals", "load spike", "backlog", "capacity", "surge"),
    ),
    "hidden_queueing": (
        (MEASUREMENT,),
        ("timer", "queueing", "queuing", "unmeasured", "not timed", "hidden queue", "wait time"),
    ),
    "missing_instance": (
        (MEASUREMENT,),
        ("gauge", "not export", "missing", "unexported", "absent"),
    ),
    "leak": (
        (SPECIAL, MEASUREMENT),
        ("leak", "stuck", "never complete", "growing", "drift", "accumulat"),
    ),
}


@dataclass(frozen=True)
class Signal:
    id: str
    entity: str
    direction: str  # up | down | flat
    #: what is measured: errors | latency | cpu | rate (from the signal's id, metric and query)
    measure: str = "rate"
    description: str = ""


def measure_of(sig: dict) -> str:
    text = " ".join(str(sig.get(k, "")) for k in ("id", "metric", "query", "description")).lower()
    if "status_code_error" in text or "error" in text:
        return "errors"
    if "duration" in text or "latency" in text or "_bucket" in text:
        return "latency"
    if "cpu" in text or "utilization" in text or "eventloop" in text:
        return "cpu"
    return "rate"


@dataclass(frozen=True)
class Truth:
    scenario: str
    kind: str  # demo | queue_sim
    #: label whose values name the entities (service_name for the demo, pod for queue-sim)
    entity_label: str
    origin: tuple[str, ...]
    propagated: tuple[str, ...]
    control: tuple[str, ...]
    #: words or entity names any one of which names the root cause in prose
    root_cause_terms: tuple[str, ...]
    root_cause_summary: str
    fault_start_ms: int
    fault_end_ms: int
    tol_start_s: float
    tol_end_s: float
    run_start_ms: int
    run_end_ms: int
    expected_sources: tuple[str, ...] = (SPECIAL,)
    signals: tuple[Signal, ...] = ()
    #: the fault covers the whole run (no onset to annotate)
    no_onset: bool = False
    #: other labels that may carry the entity (job="ns/payment", service="payment")
    extra_labels: tuple[str, ...] = ()
    #: an entity every series of the source belongs to (queue-sim: the one simulated service on
    #: the eval's throwaway VM): any expression covers it unless a matcher excludes it
    sole: str | None = None
    #: the label naming the sole entity
    sole_label: str = "service"
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def entities(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.origin, *self.propagated, *self.control)))

    @property
    def implicated(self) -> frozenset[str]:
        """Entities a finding may name as affected: origin and propagated."""
        return frozenset((*self.origin, *self.propagated))

    def expected_direction(self, entity: str, measure: str | None = None) -> str | None:
        """The one direction ground truth expects for `entity` (and `measure`), else None."""
        dirs = {
            s.direction
            for s in self.signals
            if s.entity == entity and (measure is None or s.measure == measure)
        }
        if len(dirs) == 1:
            return next(iter(dirs))
        if entity in self.control:
            return "flat"
        return None


def _ms(epoch_s: float) -> int:
    return round(float(epoch_s) * 1000)


def from_demo(gt: dict, extra_terms: tuple[str, ...] = ()) -> Truth:
    aff = gt["affected_services"]
    rc = gt.get("root_cause") or {}
    origin = tuple(aff.get("origin", []))
    # the root cause is root_cause.service (homepage-flood: the load generator, not the frontend
    # it floods), plus the scenario's own words for it (questions.DEMO terms)
    first = rc.get("service") or (origin[0] if origin else "")
    terms = tuple(t for t in dict.fromkeys((first, *extra_terms)) if t)
    fw, tol, run = gt["fault_window"], gt.get("tolerance", {}), gt["run"]
    label = (gt.get("source_labels") or {}).get("service", "service_name")
    signals = tuple(
        Signal(s["id"], s["service"], s["direction"], measure_of(s), s.get("description", ""))
        for s in gt.get("expected_signals", [])
    )
    return Truth(
        scenario=gt["scenario"],
        kind="demo",
        entity_label=label,
        origin=origin,
        propagated=tuple(aff.get("propagated", [])),
        control=tuple(aff.get("unaffected_control", [])),
        root_cause_terms=terms,
        root_cause_summary=rc.get("summary", ""),
        fault_start_ms=_ms(fw["start"]),
        fault_end_ms=_ms(fw["end"]),
        tol_start_s=float(tol.get("start_s", 150)),
        tol_end_s=float(tol.get("end_s", 180)),
        run_start_ms=_ms(run["started_at"]),
        run_end_ms=_ms(run.get("finished_at") or fw["end"]),
        signals=signals,
        extra_labels=("service", "service_name", "job", "app"),
        raw=gt,
    )


def from_queue_sim(gt: dict, tolerance_s: float | None = None) -> Truth:
    faults = gt.get("faults", [])
    instances = tuple(gt.get("instances", []))
    origin = tuple(dict.fromkeys(i for f in faults for i in f.get("instances", [])))
    start_ms = int(gt["started_unix_ms"])
    dur_ms = int(float(gt["duration_s"]) * 1000)
    end_run = int(gt.get("ended_unix_ms") or start_ms + dur_ms)
    scrape = float(gt.get("scrape_interval_s", 5))
    tol = tolerance_s if tolerance_s is not None else max(60.0, 6 * scrape)
    sources: list[str] = []
    terms: list[str] = []
    for f in faults:
        src, words = QUEUE_SIM_KINDS.get(f["kind"], ((SPECIAL,), ()))
        sources += src
        terms += words
    if faults:
        fs = min(int(f["start_unix_ms"]) for f in faults)
        fe = max(
            start_ms + int(float((f.get("effect") or {}).get("drain_end_s", 0)) * 1000)
            if (f.get("effect") or {}).get("drain_end_s")
            else int(f["end_unix_ms"])
            for f in faults
        )
    else:
        fs, fe = start_ms, end_run
    whole = bool(faults) and fs - start_ms <= tol * 1000 and fe >= start_ms + dur_ms
    # a fault on every instance leaves no control; one on a subset names the subset
    if origin and set(origin) != set(instances):
        terms = [*origin, *terms]
    service = gt.get("service")
    # the service is the faulted entity when every instance is: claims about "checkout" are
    # claims about the origin (eval round 3: they were invisible to scope and source checks)
    if service and origin and set(origin) == set(instances):
        origin = (*origin, service)
    return Truth(
        scenario=gt["scenario"],
        kind="queue_sim",
        entity_label=gt.get("group_by", "pod"),
        origin=origin,
        propagated=(),
        control=tuple(i for i in instances if i not in origin),
        root_cause_terms=tuple(dict.fromkeys(terms)),
        root_cause_summary=gt.get("description", ""),
        fault_start_ms=fs,
        fault_end_ms=fe,
        tol_start_s=tol,
        tol_end_s=tol,
        run_start_ms=start_ms,
        run_end_ms=end_run,
        expected_sources=tuple(dict.fromkeys(sources)) or (COMMON,),
        signals=(),
        no_onset=whole or not faults,
        extra_labels=("instance",),
        sole=service or None,
        raw=gt,
    )


def load_truth(src: str | Path | dict, extra_terms: tuple[str, ...] = ()) -> Truth:
    gt = src if isinstance(src, dict) else json.loads(Path(src).read_text())
    if "affected_services" in gt:
        return from_demo(gt, extra_terms)
    if "started_unix_ms" in gt and "instances" in gt:
        return from_queue_sim(gt)
    raise ValueError("not a ground-truth JSON (demo scenario v1 or queue-sim)")
