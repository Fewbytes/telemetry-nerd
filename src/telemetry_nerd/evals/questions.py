"""The question asked per scenario: a neutral symptom over the run's time range.

A question names what a user would notice (failed orders, a slow storefront), never the service or
mechanism that caused it: naming the cause would score the question, not the investigation. The
time range is the whole run (baseline included), so onset and baseline are Claude's to find.

`terms` are extra words that name the root cause in prose (a traffic flood has no failing
service: "the load generator" or "a traffic surge" are both right).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from telemetry_nerd.evals.truth import Truth


@dataclass(frozen=True)
class ScenarioEval:
    question: str
    terms: tuple[str, ...] = ()


DEMO: dict[str, ScenarioEval] = {
    "payment-failure": ScenarioEval(
        "Customers reported that placing an order failed for a while between {start} and {end} "
        "UTC. What happened, which services were involved, and when did it start and end?"
    ),
    "cart-failure": ScenarioEval(
        "A few customers reported errors when completing a purchase between {start} and {end} "
        "UTC. "
        "Is there anything to it, and if so what happened and when?"
    ),
    "homepage-flood": ScenarioEval(
        "The storefront behaved unusually between {start} and {end} UTC. What changed, what "
        "caused it, and when did it start and end?",
        terms=(
            "load generator",
            "load-generator",
            "traffic surge",
            "request surge",
            "flood",
            "more requests",
            "load spike",
            "traffic spike",
            "surge in traffic",
        ),
    ),
    "ad-high-cpu": ScenarioEval(
        "Resource usage in the shop looked off between {start} and {end} UTC. Which part of the "
        "system, what changed, and when?"
    ),
    "shipping-slowdown": ScenarioEval(
        "Some customers said placing an order was very slow between {start} and {end} UTC. What "
        "happened, which services were involved, and when?"
    ),
    "catalog-lock-contention": ScenarioEval(
        "The storefront became slow and pages timed out between {start} and {end} UTC. What "
        "happened, which services were involved, and when did it start?"
    ),
}

QUEUE_SIM = ScenarioEval(
    "For the {service} service between {start} and {end} UTC: do its concurrency (in-flight "
    "requests), request rate and latency agree with each other? If they do not, where, when and "
    "why?"
)

UNATTENDED = (
    "You are running unattended for an evaluation: nobody can answer questions. Do not ask; make "
    "the most reasonable assumption, state it, and continue. The data source is already "
    "connected. You have about {max_turns} turns: record what you establish in the workspace as "
    "you go rather than at the end. Finish with the short report the command asks for."
)


def _hhmm(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


def question_for(truth: Truth, pad_s: float = 60) -> str:
    start = _hhmm(truth.run_start_ms - int(pad_s * 1000))
    end = _hhmm(truth.run_end_ms + int(pad_s * 1000))
    if truth.kind == "queue_sim":
        service = truth.raw.get("service", "the")
        return QUEUE_SIM.question.format(service=service, start=start, end=end)
    ev = DEMO.get(truth.scenario)
    if ev is None:
        return (
            f"Something looked wrong in the shop between {start} and {end} UTC. What happened, "
            "which services were involved, and when?"
        )
    return ev.question.format(start=start, end=end)


def extra_terms(scenario: str) -> tuple[str, ...]:
    ev = DEMO.get(scenario)
    return ev.terms if ev else ()
