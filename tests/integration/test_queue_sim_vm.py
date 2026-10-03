"""check_littles_law on queue-sim data in a real VictoriaMetrics (bead 317).

The simulation runs in virtual time (the exporter's engine, no sleeping); every 5 s of sim time its
/metrics text is imported with that timestamp (plus scrape jitter) so VictoriaMetrics' own rate()
sees the series. Verdicts are compared with the scenario's ground truth. The real-time scrape
path (exporter over HTTP, vmagent-style) is scripts/validate_queue_sim.py.
"""

import random
import re
from dataclasses import replace
from datetime import datetime

import pytest

from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.devtools.queue_sim import SCENARIO_DIR, Sim, ground_truth, load_scenario
from telemetry_nerd.devtools.synthetic import push
from telemetry_nerd.model.time import now_ms
from telemetry_nerd.sources.spec import SourceSpec

pytestmark = pytest.mark.integration

SCRAPE_S = 5
WARMUP_S = 20


@pytest.fixture(scope="module")
def vm(request):
    try:
        return request.getfixturevalue("vm_url")
    except Exception as e:  # noqa: BLE001 - no container runtime
        pytest.skip(f"VictoriaMetrics container unavailable: {e}")


def seed_vm(vm_url: str, name: str, t0_ms: int, counter: str | None = None) -> dict:
    sc = load_scenario(SCENARIO_DIR / f"{name}.yaml")
    if counter:
        sc = replace(sc, defaults={**sc.defaults, "counter": counter})
    sim, rng, lines = Sim(sc), random.Random(1), []
    label = f"{name}-{counter}" if counter else name
    for k in range(1, int(sc.duration_s // SCRAPE_S) + 1):
        sim.advance(k * SCRAPE_S)
        ts = t0_ms + k * SCRAPE_S * 1000 + rng.randint(-150, 150)  # scrape jitter
        for line in sim.render().splitlines():
            if not line.startswith("#"):
                line = re.sub(r"^(\w+)\{", rf'\1{{scenario="{label}",', line)
                lines.append(f"{line} {ts}\n")
    push(vm_url, "".join(lines))
    return {**ground_truth(sc, t0_ms), "label": label}


@pytest.mark.parametrize(
    ("name", "counter"),
    [
        ("consistent", None),
        ("hidden_queueing", None),
        ("missing_instance", None),
        ("overload_spike", "completions"),
        ("overload_spike", None),  # arrivals counter
    ],
)
async def test_check_matches_ground_truth(vm, tmp_path, name, counter):
    t0 = (now_ms() - 1_100_000) // 300_000 * 300_000  # windows align to wall-clock multiples
    truth = seed_vm(vm, name, t0, counter)
    svc = build_service(Settings(data_dir=tmp_path / "d", source_url="http://127.0.0.1:9"))
    await svc.source_connect(
        SourceSpec(name="vm", url=vm, flavor="victoriametrics", resolution_ms=SCRAPE_S * 1000)
    )
    sel = lambda m: f'{m}{{scenario="{truth["label"]}"}}'
    out = await svc.check_littles_law(
        source="vm", by=["pod"], window="1m", start=str(t0 + WARMUP_S * 1000),
        end=str(t0 + int(truth["duration_s"] * 1000)),
        **{k: sel(v) for k, v in truth["metrics"].items()},
    )  # fmt: skip
    assert out["verdict"] == truth["expect"]["verdict"], out["summary"]
    cls = out["classification"]
    kind = truth["expect"]["classification"]
    assert bool(cls["systematic"]) == (kind == "systematic")
    assert bool(cls["transient"]) == (kind == "transient")
    if kind == "transient":  # every flagged window overlaps the fault or its drain
        [f] = truth["faults"]
        lo, hi = f["start_unix_ms"], t0 + int(f["effect"]["drain_end_s"] * 1000) + 60_000
        for t in cls["transient"]:
            a = int(datetime.fromisoformat(t["window"][0]).timestamp() * 1000)
            assert lo - 60_000 <= a <= hi, t
