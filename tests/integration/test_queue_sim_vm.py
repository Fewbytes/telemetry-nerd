"""check_littles_law on queue-sim data in a real VictoriaMetrics (beads 317, 9fd, xa4, 00s).

The simulation runs in virtual time (the exporter's engine, no sleeping); every 5 s of sim time its
/metrics text is imported with that timestamp (plus scrape jitter) so VictoriaMetrics' own rollups
(increase() tiles, the gauge's last sample) see the series. Verdicts are compared with the
scenario's ground truth, and every window's measurement interval with the exact per-window L,
lambda and W of the simulation (queue_sim.exact_windows). The real-time scrape path (exporters
scraped by VictoriaMetrics over HTTP) is scripts/validate_queue_sim.py.
"""

import random
import re
from dataclasses import replace
from datetime import datetime
from itertools import product

import pytest

from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.devtools.queue_sim import (
    SCENARIO_DIR,
    Sim,
    exact_windows,
    ground_truth,
    load_scenario,
)
from telemetry_nerd.devtools.synthetic import push
from telemetry_nerd.model.time import now_ms
from telemetry_nerd.sources.spec import SourceSpec

pytestmark = pytest.mark.integration

SCRAPE_S = 5
WARMUP_S = 20


def placed_t0(ago_ms: int, phase_ms: int = 0) -> int:
    """The scenario start ~`ago_ms` before now, at a fixed `phase_ms` past a 5 s tile edge (1i26).
    With the scrape jitter seeded, where the scrapes fall against VictoriaMetrics' tiles is the
    only thing a wall-clock t0 left to chance: within ~150 ms of an edge a few scrapes change
    tiles, so a verdict could depend on the millisecond the test ran. Pinned, a run is
    reproducible; the placements themselves are swept by the tests' parameters."""
    tile = SCRAPE_S * 1000
    return (now_ms() - ago_ms) // tile * tile + phase_ms


@pytest.fixture(scope="module")
def vm(vm_url):
    # No skip when Docker is missing: integration is a required CI gate and must fail loudly.
    return vm_url


def seed_vm(vm_url: str, name: str, t0_ms: int, counter: str | None = None) -> dict:
    sc = load_scenario(SCENARIO_DIR / f"{name}.yaml")
    if counter:
        sc = replace(sc, defaults={**sc.defaults, "counter": counter})
    sim, rng, lines = Sim(sc), random.Random(1), []
    label = f"{name}-{counter or 'own'}-{t0_ms}"
    for k in range(1, int(sc.duration_s // SCRAPE_S) + 1):
        sim.advance(k * SCRAPE_S)
        ts = t0_ms + k * SCRAPE_S * 1000 + rng.randint(-150, 150)  # scrape jitter
        for line in sim.render().splitlines():
            if not line.startswith("#"):
                line = re.sub(r"^(\w+)\{", rf'\1{{scenario="{label}",', line)
                lines.append(f"{line} {ts}\n")
    push(vm_url, "".join(lines))
    return {**ground_truth(sc, t0_ms), "label": label, "sc": sc}


async def run_check(vm: str, tmp_path, truth: dict, window: str, warmup_s: int = WARMUP_S) -> dict:
    svc = build_service(Settings(data_dir=tmp_path / "d", source_url="http://127.0.0.1:9"))
    await svc.source_connect(
        SourceSpec(name="vm", url=vm, flavor="victoriametrics", resolution_ms=SCRAPE_S * 1000)
    )
    sel = lambda m: f'{m}{{scenario="{truth["label"]}"}}'
    t0 = truth["started_unix_ms"]
    return await svc.check_littles_law(
        source="vm", by=["pod"], window=window, start=str(t0 + warmup_s * 1000),
        end=str(t0 + int(truth["duration_s"] * 1000)),
        **{k: sel(v) for k, v in truth["metrics"].items()},
    )  # fmt: skip


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def assert_covers_exact(out: dict, truth: dict) -> None:
    """Each judged window's 95% measurement interval holds the exact R of the simulation (the
    estimand: the realised path); at most one pointwise miss."""
    t0 = truth["started_unix_ms"]
    rows = [w for w in out["total"]["windows"] if w[2] is not None]
    cols = out["total"]["window_columns"]
    span = _ms(out["total"]["windows"][1][0]) - _ms(out["total"]["windows"][0][0])
    spans = [((_ms(w[0]) - t0) / 1000, (_ms(w[0]) + span - t0) / 1000) for w in rows]
    spans = [(a, min(b, truth["duration_s"])) for a, b in spans]
    exact = exact_windows(truth["sc"], spans)
    lo, hi = cols.index("ci95_lo"), cols.index("ci95_hi")
    misses = [
        (w[0], e["R"], w[lo], w[hi]) for w, e in zip(rows, exact, strict=True)
        if not w[lo] <= e["R"] <= w[hi]
    ]  # fmt: skip
    assert len(misses) <= 1, misses


@pytest.mark.parametrize(
    ("name", "window"),
    [
        ("consistent", "auto"),
        ("hidden_queueing", "auto"),
        ("missing_instance", "auto"),
        ("leak", "auto"),  # 1m: 9 windows (the trend over windows needs 6)
    ],
)
async def test_check_matches_ground_truth(vm, tmp_path, name, window):
    t0 = placed_t0(1_100_000, 2_500)  # scrapes mid-tile
    truth = seed_vm(vm, name, t0)
    out = await run_check(vm, tmp_path, truth, window)
    exp = truth["expect"]
    assert out["verdict"] == exp["verdict"], out["summary"]
    cls = out["classification"]
    if exp["classification"] == "none":
        assert not cls["systematic"] and not cls["transient"] and not cls["promoted"]
    elif exp["classification"] == "systematic":
        assert cls["systematic"] and not cls["promoted"]
        # excursions of the same fault only (a hidden queue growing in a burst), never draining
        assert all(t["direction"] == exp["verdict"] and t["phase"] != "drain"
                   for t in cls["transient"])  # fmt: skip
    else:  # growing: a leak is a drifting systematic offset (00s)
        assert cls["systematic"] and out["total"]["growing"]["growing"]
        assert out["total"]["growing"]["slope_ci"][0] > 0
    assert_covers_exact(out, truth)  # exact L: the exported gauges (missing_instance: 2 of 3)


@pytest.mark.parametrize(
    ("offset_s", "window", "counter", "phase_ms"),
    [
        *product([0, 20, 40], ["1m", "auto"], ["completions", "arrivals"], [2_500]),
        (0, "auto", "arrivals", 60),  # 1i26: the CI placement
    ],
)
async def test_overload_spike_is_special_cause_wherever_it_falls(
    vm, tmp_path, counter, window, offset_s, phase_ms
):
    """xa4/9fd: rho 1.5 for 60 s. Wherever the spike falls on the window grid (windows follow
    the requested start), a window of its episode (load + drain) is special cause — a transient
    beyond the envelope or a promoted load peak — nothing outside it is, `peak` only on windows
    overlapping the load and `drain` only after it began; every window's interval holds the
    exact R (the rate() lookback that smeared the spike into its neighbours is gone).

    The scrapes sit mid-tile (2.5 s past an edge). 1i26: 60 ms past an edge (the jitter moves a
    few scrapes across it), the CI placement where the load-peak window, not steady, sat in the
    systematic level and read `L_high` (measurement system) on instruments that agree."""
    t0 = placed_t0(1_400_000, phase_ms)
    truth = seed_vm(vm, "overload_spike", t0, counter)
    # windows are anchored at the requested start: moving it moves the grid over the spike
    out = await run_check(vm, tmp_path, truth, window, WARMUP_S + offset_s)
    cls = out["classification"]
    [f] = truth["faults"]
    lo, load_end = f["start_unix_ms"], f["end_unix_ms"]
    hi = t0 + int(f["effect"]["drain_end_s"] * 1000)
    win = _ms(out["total"]["windows"][1][0]) - _ms(out["total"]["windows"][0][0])
    special = [t for t in cls["transient"] if t["source"] == "special_cause"] + [
        {**p, "phase": "peak"} for p in cls["promoted"]
    ]
    at_episode = [t for t in special if _ms(t["window"][0]) < hi and _ms(t["window"][1]) > lo]
    if counter == "completions":
        assert at_episode, out["summary"]
    # an ARRIVALS counter partly compensates (more arrivals in lambda, fewer completed latencies
    # in W): the exact R of the episode's windows can stay within ~15% of 1, and its backlog
    # growth near the distribution-free promotion threshold, so a placement can go unflagged
    # (measured: 1 of the 6 here, window 2m). The check must still be truthful there: the
    # intervals hold the exact R (below) and nothing outside the episode is flagged.
    for t in cls["transient"] + cls["promoted"]:
        assert lo - win < _ms(t["window"][1]) and _ms(t["window"][0]) < hi + win, t
    for t in special:
        a, b = _ms(t["window"][0]), _ms(t["window"][1])
        if t["phase"] == "peak":
            assert a < load_end and b > lo, t
        if t["phase"] == "drain":
            assert b > lo, t
    if at_episode:
        assert out["verdict"] == "inconsistent_in_windows" or (
            out["verdict"] == "consistent" and cls["promoted"]
        )
    else:
        assert out["verdict"] == "consistent"
    assert out["total"]["bias"]["alignment"] == 0
    assert_covers_exact(out, truth)


@pytest.mark.parametrize("name", ["consistent", "overload_spike"])
async def test_scrapes_on_the_tile_edges_do_not_inflate_lambda(vm, tmp_path, name):
    """Scrapes landing on the 5 s tile edges (+-150 ms jitter) leave some tiles without a sample:
    their increase is 0 and the next tile holds two scrapes. Dropping them set two intervals of
    counts against one gauge reading (lambda 1.4-1.6x: an L_low 'systematic offset' in every
    window); they are kept, so the check is as on any other phase."""
    t0 = placed_t0(1_400_000)
    truth = seed_vm(vm, name, t0, "completions")
    out = await run_check(vm, tmp_path, truth, "auto", 60)
    assert out["classification"]["systematic"] is None, out["summary"]
    assert abs(out["discrepancy"]["ratio"] - 1) < 0.05
    assert_covers_exact(out, truth)


async def test_a_5s_source_connected_without_a_resolution_learns_it_and_takes_1m_windows(
    vm, tmp_path
):
    """wbw: the round-3 overload run read a 5 s source at the default 15 s; check_littles_law
    refused 1m windows and analyze had too few points for the 3x arrival surge. Connected with
    no resolution, the source measures its scrape spacing (the series pushed at 5 s) and the
    default settings take 1m windows."""
    t0 = placed_t0(1_100_000, 2_500)  # the last samples fall inside the probe's 10m lookback
    truth = seed_vm(vm, "overload_spike", t0)
    svc = build_service(Settings(data_dir=tmp_path / "d", source_url="http://127.0.0.1:9"))
    await svc.source_connect(SourceSpec(name="vm", url=vm, flavor="victoriametrics"))
    learned = await svc.learn("vm")  # catalog names: push sources have no `up` to probe
    assert learned["resolution"]["origin"] == "learned", learned["resolution"]
    assert svc.sources["vm"].resolution_ms == SCRAPE_S * 1000
    status = await svc.source_status("vm")
    assert status["resolution"]["resolution"] == f"{SCRAPE_S}s"
    sel = lambda m: f'{m}{{scenario="{truth["label"]}"}}'
    end = t0 + int(truth["duration_s"] * 1000)
    out = await svc.check_littles_law(
        source="vm", by=["pod"], window="1m", start=str(t0 + WARMUP_S * 1000), end=str(end),
        **{k: sel(v) for k, v in truth["metrics"].items()},
    )  # fmt: skip
    assert out["window"] == "1m"
    assert_covers_exact(out, truth)
