"""run_code (b98.4): code nodes, lineage, evidence, GC, HTTP + MCP surface.

A fake kernel runs the code in-process (tn reads TN_RUN_DIR from the environment), so these
stay fast; tests/unit/test_run_code_kernel.py drives a real IPython kernel."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sqlite3
import traceback
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from telemetry_nerd.api.app import create_app
from telemetry_nerd.core.code_ops import CodeDisabled
from telemetry_nerd.core.events import EventLog
from telemetry_nerd.kernels.manager import ExecResult
from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.workspace.db import open_workspace_db
from telemetry_nerd.workspace.models import FindingIn
from tests.unit.fakes import make_service
from tests.unit.test_mcp import call, text_of

PUT_CI = """
import polars as pl
import telemetry_nerd.tn as tn
df = tn.dataset("d1")
out = df.select("ts_ms", "series_id", "avg", "count")
out = out.with_columns(lo=pl.col("avg") - 0.5, hi=pl.col("avg") + 0.5)
print("rows", out.height)
tn.put(out, like="d1", uncertainty={"method": "bootstrap", "level": 0.95})
"""
PUT_BARE = """
import polars as pl
import telemetry_nerd.tn as tn
df = tn.dataset("d1")
tn.put(df.select("ts_ms", "series_id", "avg", "count"), like="d1")
"""


class FakeKernels:
    """KernelManager stand-in: exec in-process with the run's env and cwd."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.next: ExecResult | None = None  # canned result instead of running the code

    async def execute(self, workspace_id, code, *, env=None, cwd=None, timeout_s=None):
        self.calls.append({"ws": workspace_id, "env": dict(env or {}), "cwd": cwd, "t": timeout_s})
        if self.next is not None:
            res, self.next = self.next, None
            return res
        out, old_env, old_cwd = io.StringIO(), dict(os.environ), os.getcwd()
        os.environ.update(env or {})
        os.chdir(cwd)
        try:
            with contextlib.redirect_stdout(out):
                exec(compile(code, "<cell>", "exec"), {})  # noqa: S102 - test-only kernel
        except Exception as e:  # noqa: BLE001 - reported like a kernel would
            return ExecResult(
                "error", out.getvalue(), error=f"{type(e).__name__}: {e}",
                traceback=traceback.format_exc(), duration_s=0.01,
            )  # fmt: skip
        finally:
            os.environ.clear()
            os.environ.update(old_env)
            os.chdir(old_cwd)
        return ExecResult("ok", out.getvalue(), duration_s=0.01)

    async def aclose(self) -> None:
        pass


@pytest.fixture
def kernels():
    return FakeKernels()


@pytest.fixture
async def svc(tmp_path, kernels):
    s = make_service(tmp_path, kernels=kernels, runs_root=tmp_path / "runs")
    await s.query("up", "now-2h", "now-1h")  # d1: 2 series
    return s


def _events(svc, type_=None):
    return [e for e in svc.log.since(0) if type_ is None or e.type == type_]


async def test_run_creates_node_with_lineage_and_events(svc, kernels):
    node = await svc.code.run(PUT_CI, ["d1"], timeout_s=30)
    assert node.id == "c1"  # code nodes have their own prefix
    assert (node.status, node.exec_status, node.inputs) == ("ok", "ok", ["d1"])
    assert node.stdout.startswith("rows ") and node.finished_at_ms is not None
    [out] = node.outputs
    assert (out.name, out.dataset, out.uncertainty) == ("out1", "d2", None)
    meta = svc.datasets.meta("d2")
    assert meta.producer == {"kind": "code", "node": "c1", "output": "out1"}
    assert meta.parents == ["d1"] and meta.expr == "code:c1/out1"
    # the kernel got this run's directory as env + cwd, and the timeout
    call_ = kernels.calls[0]
    run_dir = svc.code.runs.run_dir("c1")
    assert call_["env"] == {"TN_RUN_DIR": str(run_dir)} and call_["cwd"] == run_dir
    assert (call_["ws"], call_["t"]) == ("w1", 30)
    started, created, finished = (
        _events(svc, "code.started")[0], _events(svc, "dataset.created")[-1],
        _events(svc, "code.finished")[0],
    )  # fmt: skip
    assert (started.actor, started.object_id, started.payload["inputs"]) == ("claude", "c1", ["d1"])
    assert (created.actor, created.object_id, created.payload["code_node"]) == ("code", "d2", "c1")
    assert (finished.actor, finished.payload["status"], finished.payload["outputs"]) == (
        "code", "ok", ["d2"],
    )  # fmt: skip
    assert svc.code.get("c1") == node  # persisted


async def test_result_is_compact_and_summarizes_outputs(svc):
    node = await svc.code.run(PUT_CI, ["d1"])
    r = svc.code.result(node)
    assert (r["code_node"], r["status"]) == ("c1", "ok") and r["stdout"].startswith("rows ")
    [o] = r["outputs"]
    assert o["dataset"] == "d2" and "uncertainty_status" not in o and o["parents"] == ["d1"]
    assert o["uncertainty"]["method"] == "bootstrap"
    assert o["series_count"] == 2 and "last_interval" in o["series"][0]
    assert "traceback" not in r and "restarted" not in r
    assert len(json.dumps(r)) < 2500


async def test_long_stdout_is_cut_in_the_answer_not_on_the_node(svc):
    node = await svc.code.run("print('x' * 50_000)")
    r = svc.code.result(node)
    assert len(node.stdout) > 50_000 and len(r["stdout"]) < 1500
    assert "characters cut" in r["stdout"]


async def test_failing_code_fails_the_node_with_traceback(svc):
    node = await svc.code.run("import telemetry_nerd.tn as tn\n1/0", ["d1"])
    assert (node.status, node.exec_status) == ("failed", "error")
    assert node.error == "ZeroDivisionError: division by zero"
    assert "ZeroDivisionError" in node.traceback
    r = svc.code.result(node)
    assert r["error"] and "ZeroDivisionError" in r["traceback"]
    assert _events(svc, "code.finished")[0].payload["status"] == "failed"


async def test_failed_run_ingests_nothing_and_reports_it(svc):
    code = PUT_CI + "\nraise RuntimeError('after put')"
    node = await svc.code.run(code, ["d1"])
    assert node.status == "failed" and node.outputs == []
    assert [i.code for i in node.issues] == ["run_failed"]
    assert not svc.datasets.exists("d2")


@pytest.mark.parametrize(
    ("res", "exec_status"),
    [
        (ExecResult("timeout", error="timed out after 1 s", duration_s=1.0), "timeout"),
        (ExecResult("crashed", error="kernel died (SIGKILL)", restarted=True), "crashed"),
    ],
)
async def test_timeout_and_crash_fail_the_node(svc, kernels, res, exec_status):
    kernels.next = res
    node = await svc.code.run("while True: pass", timeout_s=1)
    assert (node.status, node.exec_status, node.error) == ("failed", exec_status, res.error)
    r = svc.code.result(node)
    if res.restarted:
        assert r["restarted"] is True and "variables" in r["note"]
    else:
        assert "restarted" not in r


async def test_validation_errors_create_no_node(svc):
    with pytest.raises(ValueError, match="empty"):
        await svc.code.run("   ")
    with pytest.raises(Exception, match="d99"):
        await svc.code.run("1", ["d99"])
    with pytest.raises(ValueError, match="timeout_s"):
        await svc.code.run("1", timeout_s=0)
    assert svc.code.list() == []


async def test_disabled_without_kernels(tmp_path):
    s = make_service(tmp_path)
    with pytest.raises(CodeDisabled):
        await s.code.run("1")


async def test_rerun_is_a_new_node_on_the_same_inputs(svc):
    first = await svc.code.run(PUT_CI, ["d1"], timeout_s=10)
    again = await svc.code.rerun(first.id)
    assert (again.id, again.rerun_of, again.inputs, again.code) == ("c2", "c1", ["d1"], PUT_CI)
    assert again.timeout_s == 10 and again.outputs[0].dataset == "d3"
    assert svc.code.get("c1").outputs[0].dataset == "d2"  # the old node is unchanged
    assert svc.datasets.meta("d3").producer["node"] == "c2"


async def test_evidence_from_no_uncertainty_output_is_flagged_not_rejected(svc):
    """Spec §5.3 (x2x.1): no_uncertainty means unknown, not zero: citable, flagged."""
    node = await svc.code.run(PUT_BARE, ["d1"])
    [out] = node.outputs
    assert out.uncertainty == "no_uncertainty" and "no_uncertainty" in out.caveats
    assert svc.code.result(node)["outputs"][0]["uncertainty_status"] == "no_uncertainty"
    finding = {
        "claim": "level is about 1",
        "scope": {
            "source": "default", "selector": "up", "step": "15s", "aggregation": "mean",
            "time_range": {"start_ms": svc.datasets.meta("d1").start_ms,
                           "end_ms": svc.datasets.meta("d1").end_ms},
        },
        "evidence": [{"kind": "statistic", "dataset": out.dataset, "name": "mean level",
                      "value": 1.0, "interval": [0.5, 1.5], "method": "bootstrap"}],
    }  # fmt: skip
    f = svc.ws.finding_create(FindingIn.model_validate(finding), "claude")
    assert [e.flag for e in f.evidence_flags] == ["input_uncertainty_unknown"]
    # a panel of it: its values are shown without a known interval
    panel = svc.show(out.dataset, "Level per instance?").panel
    finding["evidence"] = [{"kind": "panel", "panel": panel.id}]
    f = svc.ws.finding_create(FindingIn.model_validate(finding), "claude")
    assert [e.flag for e in f.evidence_flags] == ["uncertainty_unknown"]
    # an output with a declared interval: no flag
    ok = (await svc.code.run(PUT_CI, ["d1"])).outputs[0]
    finding["evidence"] = [{"kind": "statistic", "dataset": ok.dataset, "name": "mean level",
                            "value": 1.0, "interval": [0.5, 1.5], "method": "bootstrap"}]  # fmt: skip
    f = svc.ws.finding_create(FindingIn.model_validate(finding), "claude")
    assert f.id == "f3" and f.evidence_flags == []


async def test_fit_params_without_interval_are_rejected_by_name(svc):
    code = """
import telemetry_nerd.tn as tn
tn.put_fit("linear", {"slope": {"value": 2.0, "interval": [1.5, 2.5]}, "icept": {"value": 1.0}},
           method="OLS", diagnostics={"dw": 2.0})
"""
    node = await svc.code.run(code, ["d1"])
    fit = node.outputs[0]
    assert fit.representation == "estimate"
    r = svc.code.result(node)["outputs"][0]
    assert r["model"] == "linear" and r["params"]["slope"]["interval"] == [1.5, 2.5]
    meta = svc.datasets.meta("d1")
    base = {
        "claim": "slope", "scope": {
            "source": "default", "selector": "up", "step": "15s", "aggregation": "mean",
            "time_range": {"start_ms": meta.start_ms, "end_ms": meta.end_ms}},
    }  # fmt: skip

    def stat(name):
        # cited as stored (value + interval of the fit parameter)
        return {"kind": "statistic", "dataset": fit.dataset, "name": name, "value": 2.0,
                "interval": [1.5, 2.5], "method": "OLS"}  # fmt: skip

    with pytest.raises(ValueError, match="icept"):
        svc.ws.finding_create(
            FindingIn.model_validate(base | {"evidence": [stat("icept")]}), "claude"
        )
    f = svc.ws.finding_create(
        FindingIn.model_validate(base | {"evidence": [stat("slope")]}), "claude"
    )
    assert f.id == "f1"


async def test_gc_keeps_running_recent_and_referenced(svc):
    svc.code.keep_recent = 1
    a = await svc.code.run(PUT_CI, ["d1"])  # c1 -> d2, will be drawn
    svc.show(a.outputs[0].dataset, "CI per instance?")
    await svc.code.run("x = 1")  # c2: unreferenced
    await svc.code.run("y = 2")  # c3: most recent
    root = svc.code.runs.root
    (root / "stray").mkdir()  # no node in this workspace
    assert sorted(p.name for p in root.iterdir()) == ["c1", "c3", "stray"]  # GC ran after c3
    assert svc.code.gc() == ["stray"]
    assert sorted(p.name for p in root.iterdir()) == ["c1", "c3"]


async def test_startup_fails_interrupted_runs(svc):
    stale = svc.ws.objects.create_code("x = 1", [], "claude")
    svc.code.startup()
    node = svc.code.get(stale.id)
    assert (node.status, node.exec_status) == ("failed", "interrupted")
    assert "daemon stopped" in node.error


async def test_workspace_brief_snapshot_and_activity_show_code(svc):
    await svc.code.run(PUT_CI, ["d1"])
    assert svc.ws.brief()["code"] == [{"id": "c1", "status": "ok", "outputs": ["d2"]}]
    [c] = svc.ws.snapshot()["code"]
    assert c["id"] == "c1" and c["inputs"] == ["d1"] and "code" not in c  # no code text
    summaries = [e["summary"] for e in svc.ws.activity(0)["events"]]
    assert "claude ran code c1 on d1" in summaries
    assert any(s.startswith("code c1 ok in") and "→ d2" in s for s in summaries)


async def test_http_code_nodes_are_read_only(svc):
    await svc.code.run("1/0")
    stale = svc.ws.objects.create_code("x = 1", [], "claude")  # left running by a dead daemon
    with TestClient(create_app(svc, allowed_hosts=["testserver"])) as client:
        _http_checks(client)
        assert client.get(f"/api/code/{stale.id}").json()["status"] == "failed"  # lifespan


def _http_checks(client) -> None:
    r = client.get("/api/code/c1")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == "1/0" and body["status"] == "failed" and body["traceback"]
    assert [c["id"] for c in client.get("/api/code").json()] == ["c1", "c2"]
    assert client.get("/api/code/c9").status_code == 404
    assert client.post("/api/code/c1").status_code == 405


async def test_http_rerun_makes_a_new_node_as_user(svc):
    await svc.code.run("1/0")
    with TestClient(create_app(svc, allowed_hosts=["testserver"])) as client:
        r = client.post("/api/code/c1/rerun", json={})
        assert r.status_code == 200
        body = r.json()
        assert (body["id"], body["rerun_of"], body["author"]) == ("c2", "c1", "user")
        assert body["status"] == "failed" and "ZeroDivisionError" in body["traceback"]
        assert client.get("/api/code/c1").json()["status"] == "failed"  # c1 stays as it was
        assert client.post("/api/code/c9/rerun", json={}).status_code == 404
        assert client.post("/api/code/c1/rerun", content="x").status_code == 415
    assert svc.ws.activity(0)["events"][-1]["summary"].startswith("code c2 failed")


async def test_http_rerun_without_kernels_is_409(tmp_path):
    from tests.unit.fakes import make_service

    svc = make_service(tmp_path)
    svc.ws.objects.create_code("x = 1", [], "claude")
    with TestClient(create_app(svc, allowed_hosts=["testserver"])) as client:
        assert client.post("/api/code/c1/rerun", json={}).status_code == 409


async def test_mcp_run_and_rerun(svc):
    mcp = build_mcp(svc, "http://x")
    r = await call(mcp, "run_code", {"code": PUT_CI, "inputs": ["d1"]})
    assert not r.is_error
    out = json.loads(text_of(r))
    assert out["code_node"] == "c1" and out["outputs"][0]["dataset"] == "d2"
    again = json.loads(text_of(await call(mcp, "rerun_code", {"code_node": "c1"})))
    assert (again["code_node"], again["rerun_of"]) == ("c2", "c1")
    bad = await call(mcp, "run_code", {"code": "1", "inputs": ["d404"]})
    assert bad.is_error and "d404" in text_of(bad)
    missing = await call(mcp, "rerun_code", {"code_node": "c99"})
    assert missing.is_error and "c99" in text_of(missing)


def test_old_event_tables_accept_the_code_actor(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL, "
        "actor TEXT NOT NULL CHECK (actor IN ('claude', 'user', 'system')), type TEXT NOT NULL, "
        "object_id TEXT, klass TEXT NOT NULL CHECK (klass IN ('intentional', 'ambient', "
        "'internal')), payload TEXT NOT NULL)"
    )
    con.execute(
        "INSERT INTO events (ts_ms, actor, type, klass, payload) "
        "VALUES (1, 'user', 'x', 'internal', '{}')"
    )
    con.commit()
    con.close()
    log = EventLog(open_workspace_db(path))
    assert [e.seq for e in log.since(0)] == [1]
    assert log.append("code", "code.finished", "c1").seq == 2
    open_workspace_db(Path(path))  # idempotent


async def test_cancelled_run_does_not_stay_running(svc, kernels):
    async def hang(*a, **kw):
        await asyncio.sleep(3600)

    kernels.execute = hang
    task = asyncio.create_task(svc.code.run("x = 1"))
    while not svc.code.list():
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    node = svc.code.get("c1")
    assert (node.status, node.exec_status) == ("failed", "cancelled")


async def test_mcp_code_get_pages_stdout_and_reads_traceback(svc):
    mcp = build_mcp(svc, "http://x")
    await call(mcp, "run_code", {"code": "print('x' * 9000)\nprint('END')"})
    r = json.loads(text_of(await call(mcp, "code_get", {"code_node": "c1", "part": "stdout"})))
    assert (r["offset"], len(r["text"]), r["total_chars"]) == (0, 4000, 9005)
    assert r["next_offset"] == 4000
    seen, page = r["text"], r
    while page["next_offset"] is not None:
        page = json.loads(
            text_of(await call(mcp, "code_get", {"code_node": "c1", "part": "stdout",
                                                 "offset": page["next_offset"], "limit": 5000}))
        )  # fmt: skip
        seen += page["text"]
    assert seen == "x" * 9000 + "\nEND\n"
    assert page["next_offset"] is None

    await call(mcp, "run_code", {"code": "print('before')\n{}['missing']"})
    tb = json.loads(text_of(await call(mcp, "code_get", {"code_node": "c2", "part": "traceback"})))
    assert "KeyError" in tb["text"] and tb["status"] == "failed"
    code = json.loads(text_of(await call(mcp, "code_get", {"code_node": "c2", "part": "code"})))
    assert code["text"] == "print('before')\n{}['missing']"
    allp = json.loads(text_of(await call(mcp, "code_get", {"code_node": "c2"})))
    for section in ("## code", "## stdout", "## traceback", "## error"):
        assert section in allp["text"]


async def test_mcp_code_get_rejects_bad_arguments(svc):
    mcp = build_mcp(svc, "http://x")
    await call(mcp, "run_code", {"code": "print(1)"})
    for args in (
        {"code_node": "c99"},
        {"code_node": "c1", "part": "bogus"},
        {"code_node": "c1", "offset": -1},
        {"code_node": "c1", "limit": 0},
        {"code_node": "c1", "limit": 10**6},
    ):
        assert (await call(mcp, "code_get", args)).is_error, args
