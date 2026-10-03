"""Tier-2 runs (spec §5.2): `run_code` turns a snippet into a `code` node with lineage.

    node (running) -> rx.prepare_run(node, inputs) -> kernels.execute(TN_RUN_DIR=run_dir,
    cwd=run_dir) -> rx.ingest_run(node, succeeded=ok) -> node ok | failed, outputs = datasets

Nodes are immutable once finished: a re-run is a new node with `rerun_of`. Events: the caller
(Claude, or the user via the UI) starts a run (`code.started`); what the run produced is the
code's own doing (actor `code`: `dataset.created` per output, `code.finished`).

GC policy for run directories (`gc`): a run dir is kept while its node is running, among the
`keep_recent` newest nodes of each workspace (re-run, debugging), or referenced: one of its outputs is drawn on a
panel (open or closed), cited by a finding, or a parent of another dataset. Everything else,
including directories with no node in any workspace, is removed. Run directories are shared
by all workspaces, so recovery and GC look at every workspace, not just the active one.
Removing a run dir never loses data: outputs are datasets in the store and the code + inputs live on the node.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from telemetry_nerd.charts.units import infer_unit
from telemetry_nerd.core.events import Actor, EventLog
from telemetry_nerd.core.summary import summarize_distribution
from telemetry_nerd.core.workspace_service import WorkspaceService
from telemetry_nerd.datasets.store import DatasetMeta, DatasetStore
from telemetry_nerd.exchange import fmt
from telemetry_nerd.exchange.fmt import ExchangeError
from telemetry_nerd.exchange.run import IngestResult, RunExchange
from telemetry_nerd.kernels.manager import ExecResult, KernelManager
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.time import format_duration, iso, now_ms
from telemetry_nerd.workspace.models import CodeIssue, CodeNode, CodeOutput, PanelRef, StatisticRef

log = logging.getLogger(__name__)

MAX_CODE_CHARS = 100_000
MAX_TIMEOUT_S = 3600.0
#: what run_code returns of stdout/stderr/traceback (the node keeps the kernel's bounded copy)
SHORT_OUTPUT = 1200
SHORT_TRACEBACK = 2000
TOP_SERIES = 3
#: code_get paging: default and maximum characters per call
PAGE_DEFAULT = 4000
PAGE_MAX = 20000
CODE_PARTS = ("all", "stdout", "stderr", "traceback", "code")


class CodeDisabled(RuntimeError):
    """Tier-2 execution is not wired (no kernel manager / run directory)."""


def _short(text: str | None, limit: int) -> str | None:
    if not text or len(text) <= limit:
        return text or None
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} characters cut; read the rest with code_get] ...\n{text[-half:]}"


def _tail(text: str | None, limit: int) -> str | None:
    if not text or len(text) <= limit:
        return text or None
    return f"... [{len(text) - limit} characters cut] ...\n{text[-limit:]}"


def _round(x: float | None) -> float | None:
    return None if x is None else float(f"{x:.6g}")


@dataclass
class CodeOps:
    datasets: DatasetStore
    ws: WorkspaceService
    log: EventLog
    kernels: KernelManager | None = None
    runs: RunExchange | None = None
    clock: Callable[[], int] = now_ms
    keep_recent: int = 20
    #: the workspace a run belongs to (its kernel is keyed by it)
    scope: Callable[[], str] = lambda: "w1"
    #: every workspace id: recovery and GC span them all
    workspace_ids: Callable[[], list[str]] = lambda: ["w1"]
    #: pin the workspace for the duration of a block
    using: Callable[[str], AbstractContextManager[None]] = lambda wid: contextlib.nullcontext()
    _running: set[str] = field(default_factory=set, init=False, repr=False)

    @property
    def enabled(self) -> bool:
        return self.kernels is not None and self.runs is not None

    # --- runs -------------------------------------------------------------------------------

    def _check_inputs(self, inputs: list[str]) -> list[str]:
        handles = list(dict.fromkeys(inputs))
        for h in handles:
            meta = self.datasets.meta(h)  # NotFound for an unknown handle
            if meta.representation not in fmt.EXPORTABLE:
                raise ValueError(
                    f"{h} is a {meta.representation} dataset; run_code inputs can be "
                    f"{', '.join(fmt.EXPORTABLE)} (query the histogram or the underlying "
                    "series instead of a percentile)"
                )
        return handles

    async def run(
        self,
        code: str,
        inputs: list[str] | None = None,
        *,
        timeout_s: float | None = None,
        actor: Actor = "claude",
        rerun_of: str | None = None,
    ) -> CodeNode:
        """Run `code` in the workspace kernel with `inputs` exported to its run directory.
        Validation problems raise (no node is created); everything after that, including a
        failing, timed-out or crashing run, ends as a finished node."""
        if not self.enabled:
            raise CodeDisabled("tier-2 code execution is not enabled in this daemon")
        if not code or not code.strip():
            raise ValueError("code is empty")
        if len(code) > MAX_CODE_CHARS:
            raise ValueError(f"code is {len(code)} characters; the limit is {MAX_CODE_CHARS}")
        if timeout_s is not None and not 0 < timeout_s <= MAX_TIMEOUT_S:
            raise ValueError(f"timeout_s must be in (0, {MAX_TIMEOUT_S:g}]")
        handles = self._check_inputs(list(inputs or []))
        assert self.kernels is not None and self.runs is not None
        with self.log.transaction():
            node = self.ws.objects.create_code(code, handles, actor, timeout_s, rerun_of)
            self.log.append(
                actor,
                "code.started",
                node.id,
                {"inputs": handles, "rerun_of": rerun_of, "lines": code.count("\n") + 1},
            )
        self._running.add(node.id)
        try:
            return await self._execute(node)
        finally:
            self._running.discard(node.id)
            self._gc_quietly()

    async def rerun(
        self, node_id: str, *, timeout_s: float | None = None, actor: Actor = "claude"
    ) -> CodeNode:
        """Run a node's code again on the same inputs, as a new node (`rerun_of`)."""
        old = self.ws.objects.get_code(node_id)
        return await self.run(
            old.code,
            old.inputs,
            timeout_s=timeout_s if timeout_s is not None else old.timeout_s,
            actor=actor,
            rerun_of=old.id,
        )

    async def _execute(self, node: CodeNode) -> CodeNode:
        assert self.kernels is not None and self.runs is not None
        try:
            run_dir = self.runs.prepare_run(node.id, node.inputs)
        except (ExchangeError, NotFound, OSError) as e:
            return self._finish(node, ExecResult("error", error=f"inputs: {e}"), None, "not_run")
        try:
            res = await self.kernels.execute(
                self.scope(),
                node.code,
                env={fmt.ENV_RUN_DIR: str(run_dir)},
                cwd=run_dir,
                timeout_s=node.timeout_s,
            )
        except RuntimeError as e:  # kernel manager closed: the daemon is shutting down
            return self._finish(node, ExecResult("error", error=str(e)), None, "not_run")
        except asyncio.CancelledError:  # the caller went away: never leave the node running
            self._finish(
                node, ExecResult("error", error="cancelled: the request was abandoned"), None,
                "cancelled",
            )  # fmt: skip
            raise
        try:
            ingest = self.runs.ingest_run(node.id, succeeded=res.ok)
        except Exception as e:  # a broken run dir fails this node, never the daemon
            log.warning("run_code: ingest of %s failed", node.id, exc_info=True)
            failed = dataclasses.replace(
                res, status="error", error=f"ingest failed: {type(e).__name__}: {e}", traceback=None
            )
            return self._finish(node, failed, None, res.status)
        return self._finish(node, res, ingest, res.status)

    def _finish(
        self, node: CodeNode, res: ExecResult, ingest: IngestResult | None, exec_status: str
    ) -> CodeNode:
        ok = res.ok and exec_status == "ok"
        outputs = [
            CodeOutput(
                name=d.name,
                dataset=d.dataset_id,
                representation=d.representation,
                rows=d.rows,
                caveats=list(d.caveats),
                uncertainty=d.uncertainty,
            )
            for d in (ingest.datasets if ingest else [])
        ]
        issues = [
            CodeIssue(name=i.name, code=i.code, message=i.message)
            for i in (ingest.issues if ingest else [])
        ]
        error = res.error
        if res.status == "timeout" and not error:
            error = f"timed out after {node.timeout_s or 'the default'} s"
        with self.log.transaction():
            done = self.ws.objects.finish_code(
                node.id,
                status="ok" if ok else "failed",
                exec_status=exec_status,
                duration_s=round(res.duration_s, 3),
                stdout=res.stdout,
                stderr=res.stderr,
                result=res.result,
                error=None if ok else (error or res.status),
                traceback=res.traceback,
                restarted=res.restarted,
                truncated=res.truncated,
                outputs=outputs,
                issues=issues,
            )
            for o in outputs:
                self.log.append(
                    "code",
                    "dataset.created",
                    o.dataset,
                    {"expr": f"code:{node.id}/{o.name}", "code_node": node.id},
                )
            self.log.append(
                "code",
                "code.finished",
                node.id,
                {
                    "status": done.status,
                    "exec_status": exec_status,
                    "duration_s": done.duration_s,
                    "outputs": [o.dataset for o in outputs],
                    "issues": len(issues),
                    "restarted": done.restarted,
                    "error": done.error,
                },
            )
        return done

    def recover(self) -> list[str]:
        """Fail nodes left `running` by a daemon that stopped mid-run (startup only)."""
        failed: list[str] = []
        for wid in self.workspace_ids():
            with self.using(wid):
                stale = [
                    c
                    for c in self.ws.objects.list_code()
                    if c.status == "running" and c.id not in self._running
                ]
                for c in stale:
                    self._finish(
                        c,
                        ExecResult(
                            "crashed", error="interrupted: the daemon stopped during this run"
                        ),
                        None,
                        "interrupted",
                    )
                failed.extend(c.id for c in stale)
        return failed

    # --- GC ---------------------------------------------------------------------------------

    def _referenced_datasets(self) -> set[str]:
        """Datasets drawn or cited in the current workspace."""
        refs: set[str] = set()
        for p in self.ws.workspace.list_panels(include_closed=True):
            refs.update(p.dataset_ids)
        for f in self.ws.objects.list_findings():
            for ev in f.evidence:
                if isinstance(ev, StatisticRef):
                    refs.add(ev.dataset)
                elif isinstance(ev, PanelRef):
                    try:
                        refs.update(self.ws.workspace.get_panel(ev.panel).dataset_ids)
                    except NotFound:
                        pass
        return refs

    def keep(self) -> Callable[[str], bool]:
        """The GC predicate (see the module docstring), computed once per GC pass."""
        nodes: list[CodeNode] = []
        recent: set[str] = set()
        refs: set[str] = set()
        for wid in self.workspace_ids():
            with self.using(wid):
                ws_nodes = self.ws.objects.list_code()
                nodes.extend(ws_nodes)
                newest = sorted(ws_nodes, key=lambda c: int(c.id[1:]))[-self.keep_recent :]
                recent |= {c.id for c in newest}
                refs |= self._referenced_datasets()
        for m in self.datasets.list_metas():
            refs.update(m.parents)
        keep = set(self._running) | recent
        for c in nodes:
            if c.status == "running" or any(o.dataset in refs for o in c.outputs):
                keep.add(c.id)
        return keep.__contains__

    def gc(self) -> list[str]:
        if self.runs is None:
            return []
        return self.runs.gc(self.keep())

    def _gc_quietly(self) -> None:
        try:
            removed = self.gc()
            if removed:
                log.info("run_code: removed run dirs %s", ", ".join(removed))
        except Exception:  # GC is housekeeping: never fail a run over it
            log.warning("run_code: run dir GC failed", exc_info=True)

    def startup(self) -> None:
        """Daemon start: fail interrupted runs, then GC run directories."""
        if not self.enabled:
            return
        failed = self.recover()
        if failed:
            log.warning("run_code: marked interrupted runs failed: %s", ", ".join(failed))
        self._gc_quietly()

    # --- reads ------------------------------------------------------------------------------

    def get(self, node_id: str) -> CodeNode:
        return self.ws.objects.get_code(node_id)

    def list(self) -> list[CodeNode]:
        return self.ws.objects.list_code()

    def text_page(
        self, node_id: str, part: str = "all", offset: int = 0, limit: int = PAGE_DEFAULT
    ) -> dict:
        """One bounded page of a node's stored text (code, stdout, stderr, traceback, or all of
        them as labelled sections). Characters, not bytes; never dataset rows. `next_offset`
        is set while more text remains."""
        if part not in CODE_PARTS:
            raise ValueError(f"part {part!r} must be one of {', '.join(CODE_PARTS)}")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        if not 0 < limit <= PAGE_MAX:
            raise ValueError(f"limit must be between 1 and {PAGE_MAX} characters")
        node = self.get(node_id)
        texts = {
            "code": node.code,
            "stdout": node.stdout,
            "stderr": node.stderr,
            "traceback": node.traceback or "",
        }
        if part == "all":
            sections = [(k, v) for k, v in texts.items() if v]
            if node.error:
                sections.append(("error", node.error))
            if node.result is not None:
                sections.append(("result", node.result))
            text = "\n\n".join(f"## {k}\n{v}" for k, v in sections)
        else:
            text = texts[part]
        page = text[offset : offset + limit]
        end = offset + len(page)
        out: dict = {
            "code_node": node.id,
            "part": part,
            "status": node.status,
            "offset": offset,
            "total_chars": len(text),
            "text": page,
            "next_offset": end if end < len(text) else None,
        }
        if node.truncated and part in ("all", "stdout", "stderr"):
            out["note"] = "the kernel cut stdout/stderr (head and tail kept) before it was stored"
        return out

    def result(self, node: CodeNode) -> dict:
        """The compact run_code answer: never bulk data."""
        out: dict = {
            "code_node": node.id,
            "status": node.status,
            "exec_status": node.exec_status,
            "duration_s": node.duration_s,
            "inputs": node.inputs,
        }
        if node.rerun_of:
            out["rerun_of"] = node.rerun_of
        if node.stdout:
            out["stdout"] = _short(node.stdout, SHORT_OUTPUT)
        if node.stderr and node.status == "failed":
            out["stderr"] = _short(node.stderr, SHORT_OUTPUT)
        if node.result is not None:
            out["result"] = _short(node.result, SHORT_OUTPUT // 2)
        if node.truncated:
            out["truncated"] = True
        out["outputs"] = [self.output_summary(o) for o in node.outputs]
        if node.issues:
            out["issues"] = [i.model_dump() for i in node.issues]
        if node.status == "failed":
            out["error"] = node.error
            if node.traceback:
                out["traceback"] = _tail(node.traceback, SHORT_TRACEBACK)
        if node.restarted:
            out["restarted"] = True
            out["note"] = (
                "the kernel restarted: variables and imports from earlier runs are gone; "
                "datasets are safe (re-create state from inputs)"
            )
        return out

    def output_summary(self, o: CodeOutput) -> dict:
        try:
            meta = self.datasets.meta(o.dataset)
        except NotFound:
            return {"dataset": o.dataset, "name": o.name, "missing": True}
        base: dict = {
            "dataset": o.dataset,
            "name": o.name,
            "representation": o.representation,
            "rows": o.rows,
        }
        if meta.unit:
            base["unit"] = meta.unit
        if meta.uncertainty:
            base["uncertainty"] = meta.uncertainty
        if o.uncertainty:  # citable, but a finding citing it is flagged (spec §5.3)
            base["uncertainty_status"] = o.uncertainty
        base["caveats"] = o.caveats
        if meta.parents:
            base["parents"] = meta.parents
        try:
            if meta.representation == fmt.ESTIMATE:
                base |= _fit_summary(meta)
            elif meta.representation == fmt.DISTRIBUTION:
                _, dist = self.datasets.get_distribution(o.dataset)
                s = summarize_distribution(meta, dist, now_ms=self.clock(), settle_ms=0, top=2)
                base |= {k: s[k] for k in ("range", "step", "buckets", "series") if k in s}
            else:
                base |= self._series_summary(meta)
        except Exception as e:  # a summary must not turn a finished run into an error
            log.warning("run_code: summary of %s failed", o.dataset, exc_info=True)
            base["summary_error"] = f"{type(e).__name__}: {e}"
        return base

    def _series_summary(self, meta: DatasetMeta) -> dict:
        """Per series: n, min/max of avg, the last value and its interval (6 significant digits).

        Deliberately not `core.summary.summarize`: that one is show()'s view (mean, gaps,
        coverage, the min/max *columns*, which a code output may leave null, 4 digits, series
        by max), while a run_code answer reports what the code just produced, ending with the
        last value and its declared interval."""
        _, res = self.datasets.get(meta.id)
        df = pl.from_arrow(res.buckets)
        assert isinstance(df, pl.DataFrame)
        interval = self.datasets.interval(meta.id)
        if interval is not None:
            iv = pl.from_arrow(interval)
            assert isinstance(iv, pl.DataFrame)
            df = df.join(iv, on=["series_id", "ts_ms"], how="left")
        labels = dict(
            zip(res.series.column("series_id").to_pylist(), res.series.column("labels").to_pylist(), strict=True)
        )  # fmt: skip
        out: dict = {
            "range": [iso(meta.start_ms), iso(meta.end_ms)],
            "step": format_duration(meta.step_ms),
            "series_count": len(labels),
        }
        if df.height == 0:
            return out
        series = []
        for sid, g in df.sort("ts_ms").group_by("series_id", maintain_order=True):
            v = g["avg"].drop_nulls()
            if v.len() == 0:
                continue
            last = g.filter(pl.col("avg").is_not_null()).tail(1).row(0, named=True)
            s: dict = {
                "labels": json.loads(labels.get(sid[0], "{}")),
                "n": v.len(),
                "min": _round(v.min()),  # type: ignore[arg-type]
                "max": _round(v.max()),  # type: ignore[arg-type]
                "last": _round(last["avg"]),
            }
            if "lo" in g.columns and last.get("lo") is not None:
                s["last_interval"] = [_round(last["lo"]), _round(last["hi"])]
            series.append(s)
        out["series"] = series[:TOP_SERIES]
        if len(series) > TOP_SERIES:
            out["more_series"] = len(series) - TOP_SERIES
        return out


def _fit_summary(meta: DatasetMeta) -> dict:
    fit = meta.fit or {}
    params = {}
    for name, p in (fit.get("params") or {}).items():
        params[name] = (
            {"value": p.get("value")}
            | ({"interval": p["interval"]} if p.get("interval") is not None else {})
            | ({"exact": True} if p.get("exact") else {})
        )
    return {
        "model": fit.get("model"),
        "method": fit.get("method"),
        "params": params,
        **({"goodness": fit["goodness"]} if fit.get("goodness") else {}),
        "diagnostics": sorted((fit.get("diagnostics") or {}).keys()),
    }


def make_unit_of(ws: WorkspaceService) -> Callable[[DatasetMeta], str | None]:
    """Unit exported with an input: the declared one, else what the catalog says."""

    def unit_of(meta: DatasetMeta) -> str | None:
        if meta.unit or meta.producer is not None:
            return meta.unit
        try:
            return infer_unit(meta.expr, lambda m: ws.catalog_facts(meta.source, m))
        except Exception:  # an unparsable expression has no inferable unit
            log.debug("run_code: no unit for %s", meta.expr, exc_info=True)
            return None

    return unit_of


def build_runs(
    datasets: DatasetStore, ws: WorkspaceService, runs_root: Path | None
) -> RunExchange | None:
    if runs_root is None:
        return None
    return RunExchange(datasets, runs_root, unit_of=make_unit_of(ws))
