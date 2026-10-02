"""run_code end to end with a real IPython kernel (b98.4 acceptance); slow.

query (fixture source) -> run_code (bootstrap CI, tn.put with an interval) -> output ingested
with lineage -> show; failing, timed-out and crashing runs fail their node; evidence citing a
no_uncertainty output is rejected."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from telemetry_nerd.kernels.manager import KernelConfig, KernelManager
from telemetry_nerd.mcp.server import build_mcp
from tests.unit.fakes import make_service
from tests.unit.test_mcp import call, text_of

pytestmark = pytest.mark.slow
module_loop = pytest.mark.asyncio(loop_scope="module")

BOOTSTRAP = """
import numpy as np
import polars as pl
import telemetry_nerd.tn as tn

df = tn.dataset("d1").sort("series_id", "ts_ms")
rng = np.random.default_rng(7)
parts = []
for (sid,), g in df.group_by("series_id", maintain_order=True):
    v = g["avg"].to_numpy()
    # 95% bootstrap CI of the mean over a centred 9-bucket window, per bucket
    lo, hi, mean = [], [], []
    for i in range(v.size):
        w = v[max(0, i - 4): i + 5]
        boot = rng.choice(w, (400, w.size)).mean(axis=1)
        mean.append(w.mean()); lo.append(np.quantile(boot, 0.025)); hi.append(np.quantile(boot, 0.975))
    parts.append(g.select("ts_ms", "series_id").with_columns(
        avg=pl.Series(mean), lo=pl.Series(lo), hi=pl.Series(hi)))
out = pl.concat(parts)
name = tn.put(out, like="d1", description="rolling mean, 95% bootstrap CI",
              uncertainty={"method": "bootstrap", "level": 0.95})
print(name, out.height)
"""
BARE = """
import telemetry_nerd.tn as tn
tn.put(tn.dataset("d1").select("ts_ms", "series_id", "avg"), like="d1")
"""


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def kernels(tmp_path_factory):
    m = KernelManager(KernelConfig(root=tmp_path_factory.mktemp("kernels"), interrupt_grace_s=1))
    yield m
    await m.aclose()


@pytest_asyncio.fixture(loop_scope="module")
async def env(tmp_path, kernels):
    svc = make_service(tmp_path, kernels=kernels, runs_root=tmp_path / "runs")
    mcp = build_mcp(svc, "http://x")
    q = await call(mcp, "query", {"expr": "up", "start": "now-2h", "end": "now-1h"})
    assert json.loads(text_of(q))["dataset"] == "d1"
    return svc, mcp


async def run(mcp, code, **kw) -> dict:
    r = await call(mcp, "run_code", {"code": code, **kw})
    assert not r.is_error, text_of(r)
    return json.loads(text_of(r))


@module_loop
async def test_bootstrap_ci_output_has_lineage_and_shows(env):
    svc, mcp = env
    out = await run(mcp, BOOTSTRAP, inputs=["d1"])
    assert (out["code_node"], out["status"]) == ("c1", "ok"), out
    assert out["stdout"].startswith("out1 ")
    [o] = out["outputs"]
    assert o["dataset"] == "d2" and o["evidence_ok"] is True
    assert o["uncertainty"] == {"method": "bootstrap", "level": 0.95, "kind": "confidence"}
    lo, hi = o["series"][0]["last_interval"]
    assert lo <= o["series"][0]["last"] <= hi
    meta = svc.datasets.meta("d2")
    assert meta.producer["node"] == "c1" and meta.parents == ["d1"]
    assert svc.datasets.interval("d2") is not None
    node = svc.code.get("c1")
    assert node.code == BOOTSTRAP and node.inputs == ["d1"]
    assert [x.dataset for x in node.outputs] == ["d2"]

    s = await call(mcp, "show", {"dataset": "d2", "question": "Rolling mean with its 95% CI?"})
    assert not s.is_error, text_of(s)
    panel = svc.workspace.get_panel(json.loads(text_of(s))["panel"])
    assert panel.dataset_ids == ["d2"]
    # provenance: panel -> dataset -> code node
    assert svc.datasets.meta(panel.dataset_ids[0]).producer["node"] == node.id
    # the panel payload itself: unit from meta, lo/hi as the band, provenance to c1 and d1
    data = svc.panel_data(panel.id, width_px=400)
    (series,) = data["series"][:1]
    assert series["lo"] and series["hi"]
    pairs = [
        (lo, a, hi) for lo, a, hi in zip(series["lo"], series["avg"], series["hi"]) if a is not None
    ]
    assert pairs and all(lo <= a <= hi for lo, a, hi in pairs)
    dmeta = data["dataset"]
    assert dmeta["producer"] == {
        "kind": "code", "node": "c1", "output": "out1", "description": "rolling mean, 95% bootstrap CI",
    }  # fmt: skip
    assert dmeta["parents"] == ["d1"]
    assert dmeta["uncertainty"]["method"] == "bootstrap" and dmeta["uncertainty"]["level"] == 0.95
    assert panel.spec["y"].get("unit") == meta.unit  # declared by the code, or inferred from d1


@module_loop
async def test_failing_code_fails_with_traceback(env):
    _, mcp = env
    out = await run(mcp, "def f():\n    return {}['missing']\nf()")
    assert (out["status"], out["exec_status"]) == ("failed", "error")
    assert "KeyError" in out["error"] and "KeyError" in out["traceback"]
    assert out["outputs"] == []


@module_loop
async def test_timeout_fails_the_node(env):
    svc, mcp = env
    out = await run(mcp, "import time\ntime.sleep(60)", timeout_s=1)
    assert (out["status"], out["exec_status"]) == ("failed", "timeout")
    assert svc.code.get(out["code_node"]).duration_s < 30


@module_loop
async def test_crash_fails_the_node_and_reports_lost_state(env):
    _, mcp = env
    assert (await run(mcp, "kept = 1"))["status"] == "ok"
    out = await run(mcp, "import os\nos._exit(3)")
    assert (out["status"], out["exec_status"]) == ("failed", "crashed")
    assert out["restarted"] is True and "variables" in out["note"]
    after = await run(mcp, "kept")
    assert after["status"] == "failed" and "NameError" in after["error"]
    # the daemon side is fine: a re-run of the first node works on the fresh kernel
    again = await call(mcp, "rerun_code", {"code_node": "c1"})
    assert json.loads(text_of(again))["status"] == "ok"


@module_loop
async def test_no_uncertainty_output_cannot_be_evidence(env):
    svc, mcp = env
    out = await run(mcp, BARE, inputs=["d1"])
    [o] = out["outputs"]
    assert o["evidence_ok"] is False and "no_uncertainty" in o["caveats"]
    meta = svc.datasets.meta("d1")
    f = await call(
        mcp,
        "finding_create",
        {
            "claim": "instance i1 sits around 2",
            "scope": {"source": "default", "selector": "up", "start": meta.start_ms,
                      "end": meta.end_ms, "step": "15s", "aggregation": "mean"},
            "evidence": [{"kind": "statistic", "dataset": o["dataset"], "name": "mean",
                          "value": 2.0, "interval": [1.9, 2.1], "method": "mean"}],
        },
    )  # fmt: skip
    assert f.is_error
    assert "no_uncertainty" in text_of(f) and "is not evidence" in text_of(f)
