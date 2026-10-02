"""The tier2-code skill (b98.7): every ```python example in it runs in a real kernel (slow).

Examples are marked `<!-- run: inputs=d1,d2 show=<output name> -->` right above the fence. The
fixture source returns data shaped like what each example expects (d1 errors, d2 requests,
d3 a latency histogram, d4 a linear disk-usage series); each example must finish `ok`, store
only outputs with a clean uncertainty status (spec §5.3), and (when `show=` is given) draw with `show`. Also checks
that the skill's prose only names real tools."""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest
import pytest_asyncio
from mcp import Client

from telemetry_nerd.analysis.histogram import from_matrix, histogram_expr
from telemetry_nerd.kernels.manager import KernelConfig, KernelManager
from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, labels_json, series_id
from telemetry_nerd.sources.base import FetchResult
from tests.unit.fakes import FakeSource, make_service
from tests.unit.test_mcp import call, text_of

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "tier2-code"
RUN = re.compile(r"<!--\s*run:\s*(?P<attrs>[^>]*?)\s*-->\s*```python\n(?P<code>.*?)```", re.DOTALL)


def examples() -> list[tuple[str, dict, str]]:
    out = []
    for path in sorted(SKILL.rglob("*.md")):
        for i, m in enumerate(RUN.finditer(path.read_text())):
            attrs = dict(a.split("=", 1) for a in m["attrs"].split())
            out.append((f"{path.relative_to(SKILL)}#{i}", attrs, m["code"]))
    return out


EXAMPLES = examples()


class SkillSource(FakeSource):
    """errors / requests / disk series and a latency histogram with realistic shape."""

    async def fetch_histogram(self, selector, by, rng, step_ms):
        ts = range(rng.start_ms, rng.end_ms + 1, step_ms)
        r = np.random.default_rng(3)
        share = {"0.1": 0.60, "0.5": 0.90, "1": 0.97, "10": 1.0, "+Inf": 1.0}
        totals = np.cumsum(r.poisson(2000, len(ts)))  # counters grow with time
        result = [
            {
                "metric": {"instance": f"i{k}", "le": le},
                "values": [[t / 1000, str(int(tot * f * (k + 1)))] for t, tot in zip(ts, totals)],
            }
            for k in range(self.n_series)
            for le, f in share.items()
        ]
        return from_matrix(self.name, result, expr=histogram_expr(selector, by, step_ms))

    async def fetch(self, expr, rng, step_ms):
        ts = np.arange(rng.start_ms, rng.end_ms + 1, step_ms)
        r = np.random.default_rng(len(expr))  # deterministic per expression
        if "disk" in expr:
            hours = (ts - ts[0]) / 3_600_000
            noise = np.zeros(ts.size)
            for i in range(1, ts.size):  # mild AR(1)
                noise[i] = 0.3 * noise[i - 1] + r.normal(0, 0.4)
            vals = {"": 100 + 0.5 * hours + noise}
        elif "errors" in expr:
            vals = {f"i{k}": r.poisson(6 + 2 * k, ts.size).astype(float) for k in range(2)}
        else:
            vals = {f"i{k}": r.poisson(1000 + 500 * k, ts.size).astype(float) for k in range(2)}
        labels = [{"instance": k} if k else {} for k in vals]
        sids = [series_id(self.name, lb) for lb in labels]
        rows = [(int(t), sid, float(v[i])) for sid, v in zip(sids, vals.values(), strict=True)
                for i, t in enumerate(ts)]  # fmt: skip
        buckets = pa.table(
            {
                "ts_ms": [x[0] for x in rows],
                "series_id": [x[1] for x in rows],
                "avg": [x[2] for x in rows],
                "min": [x[2] for x in rows],
                "max": [x[2] for x in rows],
                "count": [1] * len(rows),
            },
            schema=BUCKET_SCHEMA,
        )
        series = pa.table(
            {"series_id": sids, "labels": [labels_json(lb) for lb in labels]}, schema=SERIES_SCHEMA
        )
        return FetchResult(buckets, series)


pytestmark = pytest.mark.slow
module_loop = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def kernels(tmp_path_factory):
    m = KernelManager(KernelConfig(root=tmp_path_factory.mktemp("kernels"), interrupt_grace_s=1))
    yield m
    await m.aclose()


@pytest_asyncio.fixture(loop_scope="module")
async def env(tmp_path, kernels):
    svc = make_service(tmp_path, SkillSource(), kernels=kernels, runs_root=tmp_path / "runs")
    mcp = build_mcp(svc, "http://x")
    steps = [
        ("query", {"expr": "errors_total", "start": "now-12h", "end": "now", "step": "5m"}),
        ("query", {"expr": "requests_total", "start": "now-12h", "end": "now", "step": "5m"}),
        ("query_distribution", {"selector": "latency_seconds", "start": "now-2h", "end": "now",
                                "step": "5m"}),
        ("query", {"expr": "disk_used_bytes", "start": "now-12h", "end": "now", "step": "5m"}),
    ]  # fmt: skip
    for i, (tool, args) in enumerate(steps, 1):
        r = await call(mcp, tool, args)
        assert not r.is_error, text_of(r)
        assert json.loads(text_of(r))["dataset"] == f"d{i}"
    return svc, mcp


@module_loop
@pytest.mark.parametrize("name,attrs,code", EXAMPLES, ids=[e[0] for e in EXAMPLES])
async def test_skill_example_runs_and_is_drawable(env, name, attrs, code):
    _, mcp = env
    inputs = [h for h in attrs.get("inputs", "").split(",") if h]
    r = await call(mcp, "run_code", {"code": code, "inputs": inputs})
    out = json.loads(text_of(r))
    assert out["status"] == "ok", (name, out.get("error"), out.get("traceback"), out.get("issues"))
    assert out["outputs"] and not out.get("issues"), (name, out)
    assert not any(o.get("uncertainty_status") for o in out["outputs"]), (name, out["outputs"])
    if show := attrs.get("show"):
        ds = next(o["dataset"] for o in out["outputs"] if o["name"] == show)
        s = await call(mcp, "show", {"dataset": ds, "question": "result of the example?"})
        assert not s.is_error, (name, text_of(s))
        # the full stdout of the run is readable through code_get
        g = await call(mcp, "code_get", {"code_node": out["code_node"], "part": "stdout"})
        assert not g.is_error


async def test_skill_has_examples_and_names_real_tools(tmp_path):
    assert len(EXAMPLES) >= 5, [e[0] for e in EXAMPLES]
    head = (SKILL / "SKILL.md").read_text().split("---")[1]
    assert "name: tier2-code" in head and "description:" in head
    text = "".join(p.read_text() for p in SKILL.rglob("*.md"))
    async with Client(build_mcp(make_service(tmp_path), "http://x")) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
    assert {"run_code", "rerun_code", "code_get", "show", "query", "finding_create"} <= tools
    named = set(re.findall(r"`(\w+)\(", text)) - {"tn", "put", "put_fit", "dataset", "meta"}
    tool_like = {n for n in named if n in tools or n.endswith(("_code", "_get", "_create"))}
    assert tool_like <= tools, tool_like - tools


@module_loop
async def test_fit_parameter_is_cited_exactly_as_stored(env):
    """The skill's finding_create snippet: a fit parameter is evidence exactly as stored."""
    svc, mcp = env
    code = next(c for n, a, c in EXAMPLES if "worked-examples" in n and "put_fit" in c)
    out = json.loads(text_of(await call(mcp, "run_code", {"code": code, "inputs": ["d4"]})))
    fit = next(o for o in out["outputs"] if o["representation"] == "estimate")
    p = fit["params"]["slope_per_hour"] if "params" in fit else None
    assert p and p["interval"], fit
    meta = svc.datasets.meta("d4")
    scope = {"source": "default", "selector": "disk_used_bytes", "start": meta.start_ms,
             "end": meta.end_ms, "step": "5m", "aggregation": "mean"}  # fmt: skip

    def finding(value, interval):
        return call(
            mcp,
            "finding_create",
            {
                "claim": "disk grows linearly",
                "scope": scope,
                "evidence": [{"kind": "statistic", "dataset": fit["dataset"],
                              "name": "slope_per_hour", "value": value, "interval": interval,
                              "method": "OLS, n_eff-corrected"}],
            },
        )  # fmt: skip

    ok = await finding(p["value"], p["interval"])
    assert not ok.is_error, text_of(ok)
    bad = await finding(p["value"], [p["interval"][0] - 1, p["interval"][1]])
    assert bad.is_error and "as stored" in text_of(bad)
