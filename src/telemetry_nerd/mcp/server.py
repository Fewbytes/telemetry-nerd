"""MCP tools. Results are compact JSON: handles, summaries, caveats, links. Never raw series."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from pydantic import ConfigDict, TypeAdapter, ValidationError, model_validator

from telemetry_nerd.charts.spec import Window
from telemetry_nerd.charts.yview import YView
from telemetry_nerd.core.cause_hint import cause_hint
from telemetry_nerd.core.code_ops import CodeDisabled
from telemetry_nerd.core.evidence_discipline import citable_statistics
from telemetry_nerd.core.littles_compact import compact as littles_compact
from telemetry_nerd.core.littles_compact import statistics as littles_statistics
from telemetry_nerd.core.service import ChartRejected, TelemetryService
from telemetry_nerd.core.wire import with_cite
from telemetry_nerd.mcp.shapes import EvidenceContext, ShapeError, finding_in, hypothesis_scope
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.jsonsafe import dumps
from telemetry_nerd.model.time import format_duration, iso, parse_duration, parse_time
from telemetry_nerd.retro.models import LessonScope
from telemetry_nerd.sources.base import SourceError
from telemetry_nerd.sources.grafana import discover_datasources, probe_backend, proxy_url
from telemetry_nerd.sources.public import PUBLIC_SOURCES
from telemetry_nerd.sources.spec import AuthRef, SourceSpec
from telemetry_nerd.workspace.models import (
    AnnotationIn,
    Finding,
    GapIn,
    HypothesisScope,
    HypothesisStatus,
)

INSTRUCTIONS = """\
Telemetry Nerd: an evidence-first telemetry workspace shared with the user's browser.
- Load the `triage` skill for an incident (slow, errors, what changed): the flow and when to stop.
- Load the `evidence` skill before finding_create or hypotheses: scope, uncertainty, sources.
- Load the `charting` skill to pick the view (mark, y-view, overlay) that answers a question.
- `query` fetches a PromQL/MetricsQL expression as min/max/avg/count buckets and returns a
  dataset handle plus a compact summary. Write native PromQL/MetricsQL.
- Sources: `source_list` shows what is connected. To look at other data, `source_connect`
  it (Prometheus, VictoriaMetrics, Thanos, Mimir, or a Grafana datasource proxy URL);
  no restart is needed. Never ask for or pass a raw token: use auth_file/auth_env.
  `query(source=...)` picks the source.
- `show` draws a dataset as a panel. Every panel must answer an explicit question; phrase
  it as the question the graph answers. Share the returned URL with the user.
  A plain counter selector is drawn as its rate (the answer's `auto` says so); `raw=true` draws
  the running total. A metric the catalog bounds on both sides gets its natural axis.
- When one outlier or a faded low-n bucket squashes a panel's y range, `suggest_y_view` (e.g.
  mode=meaningful) with a one-line reason; do not re-query to hide data.
- To ask "is now different from before?" about a time panel, `show_marginal(panel,
  reference=previous|week)`; against the metric's learned normal, `reference=profile` (hourly
  means vs the operating profile's same seasonal hours; plain series only); cite n for both time ranges and whether it is requests or per-step samples.
- Periodicity (cron, GC, retries, scrape artefacts, diurnal): `spectrum(dataset)`. Report only
  `significant` peaks with their interval, cite `evidence`; say which periods cannot be seen
  (shorter than `limits.shortest` = 2 x query step, longer than range/2). red_noise: long periods look
  more significant than they are.
- "Is this metric stable / drifting / shifted / periodic / in control?": `analyze(dataset)`, then
  `show(dataset, question, mark="spc")`. State the verdict with its reasons and numbers, the
  baseline time range the control limits came from, n_eff, and caveats; cite `evidence`. "Has it
  changed since yesterday / last week?" as SPC: `analyze(dataset, baseline="day"|"week"|"previous")`.
- "Is now unusual for this time of day / week?": `compare_seasonal(dataset)` (also when
  analyze/spectrum/show `suggest` it), then `show(dataset, question, mark="seasonal")`. State the
  reference it chose (cycles, alignment/timezone, excluded cycles), the ratio with the normal
  range, and say so when history is insufficient. Never for percentile series.
- Many series of one metric ("CPU of all nodes", > 5 pods): `fleet(dataset)`, then
  `show(dataset, question, mark="fleet")`; never 100 lines. State the member count, spread,
  the named outliers (kind, since, effect with interval), churn and missing share. Never for
  percentile series (compare per-member rates or threshold fractions).
- Filters: `filter(dataset, kind, period, reason)` only when the question needs it: lowpass for
  trend/sustained shift, highpass to remove baseline/diurnal before looking for spikes/steps,
  bandpass around a spectrum peak. Never filter percentiles or raw counters. `show` the result;
  state the filter and why in your answer; raw stays one click away for the user.
- Latency of a service that returns errors: `split_outcome(dataset)` opens successful and failed
  requests as two panels (fast errors flatter latency, slow ones hide in it). Say which label and
  values it used and what it left out (4xx, unknown values).
- Report caveats from summaries (gaps, settling, fake_resolution) when you describe data.
- NEVER present a percentile without its sample count. A quantile over n samples is
  meaningless unless n >= ~10/(1-q) per bucket: p50 ~20, p95 ~200, p99 ~1000, p99.9 ~10000.
  Always query the count behind it (histogram_count(increase(...[w])) or
  increase(..._count[w]), w the query window) and say where the percentile is not meaningful.
- NEVER AGGREGATE PERCENTILES — not across series (sum/avg/max of histogram_quantile or of
  summary {quantile=...} series) and not across time (avg_over_time, rollups, downsampling
  of a percentile series). Aggregate the underlying histogram first, then take the quantile
  once: histogram_quantile(q, sum by (...) (rate(x[w]))). Likewise divide sums by counts
  only after aggregating both: histogram_sum(sum(rate(x[w]))) / histogram_count(sum(...)).
- Histograms first: for latency, `query_distribution` the histogram (the `_bucket` metric or
  native histogram, no functions) and `show` it (heatmap). Use `show(mark="histogram",
  windows=[spike, baseline])` to compare time ranges and cite n per time range. Quantiles only on
  request; distribution summaries give them as the BUCKET holding them, never a value.
  For "p95 over time" when a histogram exists, prefer `query_distribution` +
  `show(mark="percentiles", quantiles=[0.5, 0.95])` over `histogram_quantile`: each step shows
  the bucket holding q, only where n is enough. For tails use `show(mark="ccdf", windows=[...])`.
- "What fraction was slower than X?" is `fraction_over` on a distribution dataset: exact at
  bucket edges, bounded inside a bucket; cite its `evidence` statistic.
- Write rate(x[$__rate_interval]) (quantiles too), not a fixed [1m]/[5m], before analyze, verdicts
  or charts: it is the shortest query window the series interval allows at this query step. A query window
  spanning many steps makes neighbouring points share data, so a 10-minute time range at a 5s
  step under [1m] holds ~10 independent values (effective n), and analyze reports
  insufficient_data (its `hint` says so) or misses a short surge.
- Never look at latency alone: show it with throughput, and with concurrency when relevant
  (Little's law: mean concurrency L = throughput λ x mean latency W; W from
  histogram_sum/histogram_count, not from a percentile). "Do concurrency, throughput and latency
  agree?" is `check_littles_law(binding=... | arrival_rate, latency, concurrency)`, then
  `show(<its concurrency dataset>, question, mark="littles")`. Special-cause windows first when
  the summary opens with them: the verdict answers only whether L = λW holds overall, so
  `consistent` with promoted load-peak windows is an incident at those windows, not "nothing
  happened". Then the discrepancy (L − λW and L/(λW) − 1 with the measurement interval, whatever
  the verdict), then the verdict
  with each variation's source — measurement system (interval; systematic offset), common cause
  (small-system ±X% per window), special cause (transient windows; say "at a load peak"
  explicitly) — then warnings, every assumption marked flagged/assumed, the hints; cite
  `evidence`. No concurrency signal: say the check cannot be done; never derive L.
- Scope every claim: source, selector, time range, step. Do not generalize beyond it.
- Results are model outputs, not facts: say "under model M (its assumptions) ..." or "consistent
  with ...", citing the model the op states. When an op reports an optimistic and a cautious
  model (analyze `stability.departure`: Poisson vs clustered events), the label rests on the
  cautious one; give the optimistic p only as context.
- `workspace_get` shows open threads (user questions awaiting you), hypotheses, findings.
  `reply` answers a thread. `hypothesis_create`/`hypothesis_update` track explanations.
- `hypothesis_create` names the suspected service/resource (a label value or metric you query);
  `scope` ({selector, start, end, source?}) says where it applies.
  `hypothesis_update(status="supported")` is refused unless the statement names a concrete
  subject, a finding with stance=for backs it, and an alternative was considered (another
  hypothesis refuted/inconclusive, or `alternatives_considered`: which ones, how ruled out).
  `refuted` needs a finding against it (or `reason`: what rules it out, citing findings).
  `hypothesis_update(hidden=true, hidden_reason=...)` puts a duplicate/off-topic/superseded/
  decoy hypothesis aside without a verdict; reversible (`hidden=false`), never deletes it.
- A finding links several hypotheses, one stance each: `finding_create(hypotheses=[{"id":
  "h1", "stance": "for"}, {"id": "h2", "stance": "against"}], ...)`. A thread reply cannot be
  evidence: when data or a user reply contradicts a hypothesis, record the data as a finding
  linked to it with stance="against", then `hypothesis_update`: that is how the contradiction
  surfaces on it.
- `finding_create` needs a scope and evidence; a statistic states its uncertainty: an
  interval, exact, or `uncertainty_unknown: true` when none can be derived. Unknown is citable
  but never silent: the finding carries `uncertainty unknown` (say so when you report it).
  Derive an interval first where you can (bootstrap, effective n, bucket bounds, Wilson).
  A tier-1 op over data of unknown uncertainty (a code output) still gives evidence with its
  own interval, marked `params.input_uncertainty` + caveat `input_uncertainty_unknown`: that
  interval is a lower bound; pass the statistic as is and say so.
- A claim names only entities (service, pod, job values) its cited evidence covers; otherwise
  finding_create refuses it (claim_beyond_evidence) and lists the datasets that hold them: cite
  those. Only when the claim must reach further, pass `scope_note` with why (flagged
  beyond_evidence). Report the result's `scope` status (covered | beyond_evidence |
  undetermined) and `source_flags` (variation source derived from the op, or undetermined).
- Summaries carry `coverage` per series (share of expected samples, longest gap; `pct` is null when nothing could be judged: all unknown or source-filled) and `unknown_spans` ([start, end, reason]); `silent_members` (alive but no samples; top few named, `silent_more` counts the rest; may be the sick ones); missing data is evidence too — scope claims around it.
- `gap_create` records a signal you wish existed. `annotate` marks events/regions/thresholds.
- When you tell the user to look at an object ("see p5"), also call `highlight(object, note?)`
  so it is accented in their UI; `unhighlight` clears it. Mention ids like p5/f2 in text: they
  become hoverable chips.
- Before charting unfamiliar metrics, check what the catalog knows: `catalog_search` (words
  match names and descriptions)/`catalog_get` (run `source_learn` once per source).
  "Which services exist, and what does each report?" is `entities(kind="service")` (label index,
  cheap): start the blast radius there, with `binding_suggest(kind="RED")` over every service
  (span metrics `traces_span_metrics_*` cover services with no HTTP/RPC metrics of their own).
- An empty result (`empty_result`, a catalog `note`) is absence of evidence, not evidence of
  absence. Never say a service or signal does not exist unless `entities` (or a series check)
  was run and is cited; otherwise say it was not found where you looked. When you work out what a metric is (unit, type, role,
  bounds), record it with `catalog_write` and a basis; never claim a unit you cannot justify.
  Relations (`catalog_relate`: bounded_by, part_of, ...) and model bindings (`catalog_bind`:
  littles_law, RED, USE) go the same way; a binding role with no signal raises a Gap.
  `binding_suggest` proposes bindings (RED per service, USE per instance/device, Little's law
  triples) from names, packs and relations: check the picks and alternatives, then
  `binding_accept` or `catalog_bind`. `show_binding` draws a binding (or a suggestion) as one
  linked panel group: shared time axis, per-role form, gap cards for missing roles.
  "Is it healthy / what moved first?" is `binding_verdict(group=<pg id> | kind+key |
  suggestion)`: per role changed / no_change against stated reference time ranges, its pattern and
  onset interval, and which signal moved first (only when onset intervals do not overlap);
  one family-wise alpha over the roles. Report the reference and the alpha; cite `evidence`.
  Load the `model-views` skill before binding or judging: the workflow, how to report verdicts
  honestly, how to read the Little's law check and its assumptions, worked examples.
- When you can read the service's repo, `catalog_context` turns its metric registrations,
  dashboards and docs into cited claims (description, type, unit): find the files with rg, read the
  few that matter, send their text. Say where a claim came from when you cite it.
- `catalog_scan` measures a bounded set of metrics over a short time range (resets, small decreases,
  negatives) and files contradictions with declared types or bounds as system findings; a short
  time range only suggests, so say so when you cite it.
- `workspace_activity` lists what the user did since a sequence number.
- Lessons from earlier sessions: `lessons_for(source, services)` once the scope is known; a
  lesson is a prior to check, not evidence. At the end (/telemetry-nerd:wrap), propose what the
  investigation established: `catalog_propose` (catalog values, with evidence) and
  `lesson_propose` (scope never broader than its evidence); the user decides in the UI.
- A new, unrelated question is a new workspace: `workspace_create(title, question)`;
  `workspace_list` / `workspace_switch` go back to an earlier one. Ids are global, so `p3`
  always means the same panel. A tool call never changes the workspace except these.
- Tier-2 `run_code` is for the long tail only: tier-1 tools first. Declare `inputs` (dataset
  handles) up front, print only small aggregates, declare uncertainty or `exact` on every output
  (a no_uncertainty output is citable only flagged 'uncertainty unknown'), `show` results. Read a failed run in full with
  `code_get`. Load the `tier2-code` skill before writing code: when to use it, the `tn` API, how
  to declare intervals, worked examples.
"""


WORKSPACE_LIST_BYTES = 1900


def _capped_workspace(w: dict) -> dict:
    """`w` with its title and question cut to the workspace_list caps."""
    w = {**w, "title": w["title"][:60]}
    if w.get("question"):
        w["question"] = w["question"][:80]
    return w


def _workspace_row(w: dict) -> dict:
    """A workspace_list row: capped text, non-zero counts only."""
    capped = _capped_workspace(w)
    row: dict = {"id": capped["id"], "title": capped["title"]}
    if capped.get("question"):
        row["question"] = capped["question"]
    if w.get("archived"):
        row["archived"] = True
    row["last_activity"] = w["last_activity_ms"]
    row.update({k: n for k, n in w.get("counts", {}).items() if n})
    return row


def _trim_to_fit(items: list, render: Callable[[list], dict]) -> tuple[list, int]:
    """Drop items from the end until `render(items)` dumps within WORKSPACE_LIST_BYTES
    (principle 6: the result stays small). Returns the kept items and how many were cut."""
    items = list(items)
    cut = 0
    while items and len(_dump(render(items))) > WORKSPACE_LIST_BYTES:
        items.pop()
        cut += 1
    return items, cut


def _auth_ref(auth_env: str | None, auth_file: str | None, auth_scheme: str) -> dict | None:
    """The auth reference (never the secret) a tool's auth_env/auth_file/auth_scheme name."""
    if auth_env is None and auth_file is None:
        return None
    return {"env": auth_env, "file": auth_file, "scheme": auth_scheme}


def _dump(obj: dict) -> str:
    return dumps(obj, separators=(",", ":"))


def _issues(e: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(x) for x in err['loc']) or '<root>'}: {err['msg']}" for err in e.errors()
    )


def _source_error(e: SourceError) -> ToolError:
    return ToolError(f"{e} (hint: {e.hint})" if e.hint else str(e))


GAP_EXAMPLE = (
    '{"missing_signal": "queue depth of the payments worker", '
    '"needed_for": "saturation of the payments service", '
    '"suggestion": {"name": "payments_queue_depth", "type": "gauge", "labels": ["worker"]}}'
)
_METRIC_TYPES = ("counter", "gauge", "histogram", "summary")


def _guess_type(name: str) -> str:
    return (
        "counter"
        if name.endswith("_total")
        else "histogram"
        if name.endswith("_bucket")
        else "gauge"
    )


def _gap_suggestion(raw: Any) -> Any:
    """Accept the shapes Claude naturally tries: a dict with `metric` for `name`, labels as a
    string, or a bare string 'name', 'name (gauge)', 'name:gauge'. Anything else is left for the
    model validation to reject with the expected example."""
    if isinstance(raw, str):
        parts = [p for p in re.split(r"[\s:()\[\],]+", raw.strip()) if p]
        if not parts:
            return raw
        kind = next((p.lower() for p in parts[1:] if p.lower() in _METRIC_TYPES), None)
        return {"name": parts[0], "type": kind or _guess_type(parts[0])}
    if isinstance(raw, dict):
        d = dict(raw)
        if "name" not in d:
            for alias in ("metric", "signal", "metric_name"):
                if alias in d:
                    d["name"] = d.pop(alias)
                    break
        if isinstance(d.get("labels"), str):
            d["labels"] = [x for x in re.split(r"[\s,]+", d["labels"]) if x]
        if isinstance(d.get("type"), str):
            d["type"] = d["type"].strip().lower()
        if "type" not in d and isinstance(d.get("name"), str):
            d["type"] = _guess_type(d["name"])
        return d
    return raw


def _gap_args(missing_signal: Any, needed_for: Any, suggestion: Any, description: Any) -> dict:
    sug = _gap_suggestion(suggestion)
    sug_name = sug.get("name") if isinstance(sug, dict) else None
    return {
        "missing_signal": missing_signal or description or sug_name,
        "needed_for": needed_for or description,
        "suggestion": sug,
    }


def group_summary(g, ui_url: str) -> dict:
    """What Claude needs back from show_binding: panel ids per role, how each is drawn, gaps."""
    roles: dict[str, dict] = {}
    for r in g.roles:
        if r.view == "gap":
            roles[r.role] = {
                "gap": r.gap,
                "suggest": r.suggestion.model_dump() if r.suggestion else None,
                "why": r.why,
            }
            continue
        d: dict = {"metric": r.metric, "form": r.form, "view": r.view}
        if r.panel:
            d["panel"] = r.panel
        if r.members is not None:
            d["members"] = r.members
        if r.error:
            d["error"] = r.error
        if r.notes:
            d["notes"] = r.notes
        roles[r.role] = d
    out = {
        "group": g.id,
        "url": f"{ui_url}/#/group/{g.id}",
        "kind": g.kind,
        "key": g.key,
        "basis": g.suggestion if g.basis == "suggestion" else f"binding ({g.binding_origin})",
        "range": {"start": iso(g.start_ms), "end": iso(g.end_ms)},
        "step": format_duration(g.step_ms),
        "roles": roles,
        "gaps": [r.role for r in g.roles if r.view == "gap"],
    }
    if g.notes:
        out["notes"] = g.notes
    return out


def _fail(e: Exception) -> ToolError:
    """Message plus a hint so Claude can fix the call."""
    if isinstance(e, ValidationError):
        return ToolError(f"invalid arguments: {_issues(e)} (hint: fix the listed fields)")
    if isinstance(e, NotFound):
        return ToolError(f"{e} (hint: check the id against workspace_get)")
    return ToolError(f"{e} (hint: check the arguments)")


log = logging.getLogger(__name__)


def crash_message(name: str, e: BaseException) -> str:
    """What Claude reads when a tool crashed (an exception no tool anticipated): the tool, the
    exception type and its message. The SDK withholds a crash's text (UnexpectedToolError carries
    only 'Error executing tool <name>'), which left Claude a bare error with nothing to act on
    (gzrz); this is a local daemon, its own exception text is safe to show."""
    root = e.__cause__ if isinstance(e, UnexpectedToolError) and e.__cause__ else e
    while isinstance(root, UnexpectedToolError) and root.__cause__ is not None:
        root = root.__cause__
    return (
        f"internal error in {name}: {type(root).__name__}: {root or '(no message)'} "
        "(a telemetry-nerd bug, not your arguments: retrying the same call will fail the same "
        "way; reach the data another way, e.g. query, and mention the error to the user)"
    )


class TelemetryMCP(MCPServer):
    """MCPServer whose crashes reach Claude as a message naming the tool and the exception.

    Every call runs pinned to one workspace (spec D4): `pin` is entered at call entry, and the
    pin is a ContextVar, so sync tools (anyio worker threads) and tasks the tool spawns keep it.
    """

    pin: Callable[[], AbstractContextManager[Any]] = nullcontext

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        try:
            with self.pin():
                return await super().call_tool(name, arguments, context)
        except UnexpectedToolError as e:
            log.error("tool %s crashed", name, exc_info=e.__cause__ or e)
            raise ToolError(crash_message(name, e)) from e.__cause__


def _finding_result(f: Finding, ui_url: str) -> dict:
    """finding_create's answer: what the server derived for the recorded finding."""
    out: dict = {"finding": f.id, "url": f"{ui_url}/#/finding/{f.id}"}
    if f.hypotheses:
        out["hypotheses"] = [x.model_dump() for x in f.hypotheses]
    if f.evidence_flags:
        out["uncertainty"] = [e.model_dump() for e in f.evidence_flags]
    if f.scope_check is not None:
        out["scope"] = f.scope_check.model_dump()
    if f.sources:
        out["sources"] = f.sources  # spec §5.4: variation sources of the cited statistics
    if f.source_flags:
        out["source_flags"] = [e.model_dump() for e in f.source_flags]
    return out


def build_mcp(service: TelemetryService, ui_url: str) -> MCPServer:
    mcp = TelemetryMCP("telemetry-nerd", instructions=INSTRUCTIONS)
    mcp.pin = service.active.pinned

    @mcp.tool()
    async def query(
        expr: str,
        start: str | None = None,
        end: str | None = None,
        step: str = "auto",
        source: str = "default",
        allow_nonmergeable: bool = False,
        question: str | None = None,
    ) -> str:
        """Fetch a PromQL/MetricsQL expression as a dataset of min/max/avg/count buckets.

        question: optional, the question this data should answer; echoed back with the
        `show(dataset, question)` call that draws it (a panel needs its question).

        source: a name from source_list (default "default").
        start/end: `now`, `now-<dur>` (e.g. now-6h), epoch ms, or ISO-8601 with timezone.
        Omitted: start is the workspace's default range (the user sets it; now-1h unless set),
        end is now.
        step: `auto` (~600 buckets) or a duration like 30s, 1m, 5m.
        Name-template families (catalog entries like airflow_ti_finish_*_removed): write the template
        where a metric name goes to select every member; the text in the slot becomes the `dimension`
        label, so `sum by (dimension) (airflow_ti_finish_*_removed)` and fleet(by=["dimension"]) work.
        Range functions over a family (rate(...[5m])) need a victoriametrics source (keep_metric_names).
        Quantiles: write histogram_quantile(q, sum by (...) (rate(x[$__rate_interval])))
        as the WHOLE expression. It is evaluated per step (never rolled up) and each bucket
        carries n, the observations behind it; buckets with n < 10/(1-q) are flagged
        low_count. Wrapping a quantile in sum/avg/max/*_over_time is refused.
        The catalog also flags plain metrics it knows are pre-computed, non-mergeable
        statistics (a summary's own `quantile=` series, an exported `..._p99` gauge, median,
        MAD, IQR, truncated mean — see the mergeability table): aggregating those with
        avg/sum/min/max/*_over_time or a multi-series merge is refused the same way.
        `allow_nonmergeable=true` charts it anyway: the summary gets the caveat
        `nonmergeable_aggregation` and `summary.nonmergeable` {uses, explanation} (averaging
        pre-computed p90s across 24 hours overstated it by 68.5% in the canonical example —
        recompute from the merged histogram/raw data instead). Such a dataset still cannot go
        through fleet, compare_seasonal, analyze, spectrum or filter: those refuse a percentile.
        $__rate_interval expands to max(4 x series interval, step + series interval) (the series
        interval is the scrape interval for scraped metrics); prefer it over a fixed [1m]/[5m] as
        the query window of every rate (a query window spanning many steps cuts the effective n).
        Returns {dataset, summary}. The summary is compact; raw series stay on the server.
        """
        try:
            # pi-lens-ignore: python-sql-injection
            out = await service.query(
                expr, start, end, step, source, allow_nonmergeable=allow_nonmergeable
            )
            if question and question.strip():
                q = question.strip()
                out = {**out, "question": q,
                       "next": f"show(dataset={json.dumps(out['dataset'])}, "
                               f"question={json.dumps(q, ensure_ascii=False)})"}  # fmt: skip
            return _dump(out)
        except SourceError as e:
            raise _source_error(e) from e
        except ValueError as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    def spectrum(
        dataset: str, top: int = 3, min_period: str | None = None, max_period: str | None = None
    ) -> str:
        """Which periods does a time series contain (cron, GC, retries, diurnal cycles)?

        Lomb-Scargle on the dataset's buckets: nothing is interpolated across gaps, a linear
        trend is removed first. Returns the top peaks per series with period, an interval (the
        half-power width, never finer than 1/range), power, fap (white-noise false-alarm
        probability), fap_red_noise (against an AR(1) background fitted to what trend / level
        shifts leave, stronger confirmed peaks removed first), local_ratio and `significant`
        (fap < 1% AND power >= 10x its neighbourhood AND fap_red_noise < 1%: AR(1) wandering and
        level steps are not periods).
        Report only significant peaks; significant peaks carry an `evidence` statistic for
        finding_create. `limits` states what cannot be seen: periods shorter than 2 x step and
        longer than half the range. Caveats: red_noise (long periods look more significant than
        they are), sampling_artifact (periodic gaps, not the signal), coarsened, gaps.
        Refused with a hint on percentile series, distributions and raw counters (use a rate).
        Draw it with show(mark="spectrum"), or show(mark="spectrogram", segment=...) for periods
        that appear or disappear over time."""
        try:
            return _dump(service.spectrum(dataset, top, min_period, max_period))
        except (NotFound, ValueError) as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    async def analyze(
        dataset: str,
        baseline_start: str | None = None,
        baseline_end: str | None = None,
        baseline: str = "window",
        baseline_cycles: int = 1,
        tz: str = "UTC",
    ) -> str:
        """Diagnose a time series in one call: is it stable, drifting, level-shifted, periodic,
        noisy, or is there too little data to say?

        Runs: periods (Lomb-Scargle, confirmed against AR(1) red noise, not just white noise);
        trend (99% interval, autocorrelation-adjusted); level shifts (CUSUM changepoints with
        p (the bias-corrected point AR(1) model) and p_cautious, the label and the 99% interval
        on the size resting on the cautious one; shifts_undetermined: material shifts the point
        model alone sees, verdict undetermined, never a special cause); KPSS stationarity; variance change; shape (skew,
        tails, zeros, bimodality); and a control chart (SPC). Control limits come ONLY from the
        baseline time range (default: first half of the range; or baseline_start/baseline_end, e.g.
        a calm day before an incident), never from the data being judged; with autocorrelation
        the run rules/EWMA/CUSUM use AR(1) residuals. Intervals use the effective sample size
        n_eff, not n. Gaps are never interpolated (EWMA/CUSUM decay across a gap; a long gap
        restarts them).
        baseline: "window" (default: inside the dataset, as above) or a separately fetched
        earlier time range, judged against with every point of the dataset: "previous" (the
        preceding time range of the same length), "day" / "week" (the same time range on the previous
        day / week, aligned by `tz` like compare_seasonal); baseline_cycles takes several
        (previous <= 4, day <= 7, week <= 4) for a longer baseline. When the baseline spans
        fewer than 2 daily/weekly cycles and the operating profile is seasonal, the centre line
        follows the profile's seasonal shape (re-fitted without the judged hours): a seasonal
        residual chart (spc.centre.seasonal = "profile"). Without a cached profile it is
        computed first (bounded wait); `seasonal_centre` says what happened: profile,
        computed_now, not_seasonal, pending (re-run shortly) or unavailable (why).
        Returns per series: verdict, also, reasons (with numbers), and the sections; headline
        numbers carry an `evidence` statistic for finding_create. Caveats: coarsened, gaps,
        red_noise, short_baseline, near_random_walk, seasonal_not_in_baseline, no_period_search
        (< 32 points: judged for shifts / trend / SPC, periods not searched), future_range (the
        requested range's end is after now: the default baseline split over the first half of
        the OBSERVED span only, not the full requested range), baseline_not_calm (the baseline
        itself holds an excursion, e.g. the tail of an earlier, different episode: pass an
        explicit baseline_start/baseline_end for a calmer stretch), absent_as_zero
        (an error/outcome counter series born on its first event, e.g. status_code=ERROR, was
        read as 0 where its live sibling reports: a measurement-system assumption stated in
        the series' `absent_as_zero` and `variation`; report it with any claim on that series).
        Refused with a hint on percentile series, distributions and raw counters (use a rate).
        Verdicts: stable, drifting, level_shifted, transient (a run of judged points away from
        the baseline, then back), periodic, noisy, undetermined (tested against the baseline,
        but only the optimistic model calls it a change: both p values in the reasons), or
        insufficient_data. With too short a baseline for control limits, or n_eff < 10,
        `stability.excursion` tests the judged points against the baseline (baseline model and
        cautious model; the label rests on the cautious one; cite its `evidence`). A ratio
        A / B over one counter whose A outcome series is born on its first event (errors /
        calls) is computed from its parts, A read as 0 where B reports (`ratio`).
        Sources of variation (`variation` per series and dataset-wide, `source` on items and
        evidence): limits / centre / sigma = common cause (the envelope; never chase points in
        it); shifts, drift, significant detectors = special cause; gaps, partial / untrusted
        data = measurement system; run-rule signals that cannot be told apart = undetermined.
        Draw it with show(dataset, question, mark="spc", windows=[{start, end}] = the baseline)."""
        try:
            if baseline != "window":
                if baseline_start or baseline_end:
                    raise ValueError(
                        f"baseline={baseline!r} is fetched separately: drop baseline_start/end"
                    )
                return _dump(
                    with_cite(
                        await service.analyze_reference(dataset, baseline, baseline_cycles, tz)
                    )
                )
            return _dump(
                with_cite(await service.analyze_profiled(dataset, baseline_start, baseline_end))
            )
        except SourceError as e:
            raise _source_error(e) from e
        except (NotFound, ValueError) as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    async def compare_seasonal(
        dataset: str,
        cycles: list[str] | None = None,
        tz: str = "UTC",
        exclude: list[str] | None = None,
        threshold: float | None = None,
    ) -> str:
        """Is now unusual for this time of day / week? Compares the dataset's time range with the
        same phase of previous cycles.

        cycles: which references to consider, of "previous" (the 4 preceding time ranges: the
        non-seasonal baseline), "1d" (same time range on the previous 7 days), "1w" (previous 4
        weeks); default all. Each previous cycle is fetched (cached). The reference is chosen by
        leave-one-cycle-out error (a more specific cycle must win by 5%) and stated.
        tz: IANA timezone for alignment (default UTC): "same hour yesterday" in local time across
        DST is 23h/25h back. exclude: local dates (e.g. ["2026-12-25"]) whose cycles to drop
        (holidays); missing cycles and atypical ones (t prediction interval) are dropped too,
        each listed with its reason.
        The band is the median of the kept cycles +- quantiles of leave-one-cycle-out residuals
        (the spread ACROSS cycles, never one reference week). Verdict per series: usual |
        unusual (direction higher/lower/mixed) | insufficient_history (< 3 usable cycles), from
        three detectors at 1% each: level over the time range (ratio vs a t interval with k-1
        df), extreme points (Sidak over the time range), share of points outside the band. `ratio.evidence` is
        a statistic for finding_create. Refused on raw counters. Draw with show(dataset,
        question, mark="seasonal").
        Latency (a histogram_quantile series or a query_distribution dataset): never percentiles
        across cycles; the histogram is fetched for now and each previous cycle (same local
        time range) and compared per cycle: n and share above `threshold` per cycle (exact at a
        bucket edge: a requested threshold snaps to the nearest shared edge, stated; default the
        edge where the reference's share above is nearest 1%), band = t prediction interval of
        the cycles' logit shares; verdict on that share (1%); `shape` = CDF distance per cycle
        (descriptive). `share_over.evidence` is a statistic; each cycle lists its dataset for
        show(mark="histogram"). Summary quantiles (no histogram) are refused.
        Sources (`variation`, `source`): the band = common cause (cycle-to-cycle spread);
        unusual = special cause; cycles excluded as missing = measurement system, atypical =
        special cause back then, user-excluded = undetermined."""
        try:
            return _dump(
                with_cite(
                    await service.compare_seasonal(
                        dataset, cycles, tz, exclude, threshold=threshold
                    )
                )
            )
        except SourceError as e:
            raise _source_error(e) from e
        except (NotFound, ValueError) as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    async def check_littles_law(
        binding: str | None = None,
        arrival_rate: str | None = None,
        latency: str | None = None,
        concurrency: str | None = None,
        by: list[str] | None = None,
        start: str = "now-6h",
        end: str = "now",
        window: str = "auto",
        warmup: str | None = None,
        latency_unit: str | None = None,
        arrivals: str = "auto",
        source: str = "default",
        detail: bool = False,
        group: str | None = None,
    ) -> str:
        """Is measured mean concurrency L consistent with throughput x mean latency (L = λ·W)?

        Signals: `binding` = the key of a littles_law binding (catalog_bind / catalog_relations;
        its join_on is the default `by`), or the roles directly (they override the binding):
        arrival_rate = a request counter (or a histogram's _count); latency = the histogram or
        summary base name with _sum/_count (W = rate(_sum)/rate(_count), a MEAN; a percentile-only
        latency is refused with a hint); concurrency = the in-flight gauge. Metric names or
        selectors `name{job="api"}`; the check writes the rates and `sum by (by)`.
        window: judged span (default ~range/12); warmup: excluded from the start (e.g. "10m");
        latency_unit: s|ms|us|ns when the catalog does not know it (else ASSUMED s, flagged);
        arrivals: auto|arrivals|completions — what the counter counts (stated in assumptions).
        Result leads with `summary` (text: special-cause windows when any — the verdict answers
        only whether L = λW holds overall, so `consistent` + promoted windows is not "nothing
        happened" — then discrepancy, verdict, warnings), `discrepancy` (L − λW
        and L/(λW) − 1, whole range and per window, with the MEASUREMENT interval: gauge sampling
        floored by a Poisson-occupancy process, steady-state edge straddle, counter scrape timing,
        rate lookback bound when counters are read with rate() (on VictoriaMetrics they are read as
        increase() tiles ending at the gauge's scrapes: no lookback) — the counts' Poisson noise is not
        in it; windows are anchored at `start`), `verdict`,
        `classification` and `warnings`. Sources: measurement system (the interval; a systematic
        offset over most windows), common cause (`common_cause`: at N requests/window L and λW
        fluctuate ±X%; the windows' own spread; a load-peak window inside it is "not a signal by
        itself"), special cause (transient windows beyond both, with `phase` peak|drain|other and
        load context; and load-peak windows in `classification.promoted`, promoted on evidence
        of leaving steady state — backlog growth, W rising across consecutive windows, growth
        across repeated peaks; own 5% FWER — with `reason` and numbers). Verdict: consistent | L_high / L_low (a
        systematic offset: time outside the latency timer, stuck/leaked requests, latency on a
        subset / concurrency missing instances, gauge missing bursts, latency on a superset) |
        inconsistent_in_windows (transients only); ≤5% false alarms overall. No concurrency
        signal: the check says it cannot be done. `variation` lists the labelled findings.
        `total` is the
        ungrouped view; `groups` per `by` value; `unmatched` = groups missing from a signal
        (localises a missing instance). `assumptions` (steady state, arrivals vs completions,
        label sets, units, alignment, warm-up, gauge sampling) each ok/assumed/flagged. Each
        group's `evidence` statistics go to finding_create as is. Draw: show(<datasets.concurrency>,
        question, mark="littles").
        Compact by default: the total and every flagged group (not consistent, flagged/transient
        windows, promoted peaks) in full minus consistent windows' rows; other groups one row
        each in `groups` (`group_columns`; at most 10, flagged first, the rest counted in
        `other_groups`). detail=true: everything (large with many groups); group="pod=x": that
        group in full (`group`)."""
        try:
            out = await service.check_littles_law(
                source=source, binding=binding, arrival_rate=arrival_rate, latency=latency,
                concurrency=concurrency, by=by, start=start, end=end, window=window,
                warmup=warmup, latency_unit=latency_unit, arrivals=arrivals,
            )  # fmt: skip
            if detail:
                return _dump(with_cite(out))
            res = littles_compact(out, group)
            service.datasets.record_statistics(littles_statistics(res))
            return _dump(with_cite(res))
        except SourceError as e:
            raise _source_error(e) from e
        except (NotFound, ValueError) as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    def fleet(
        dataset: str,
        by: list[str] | None = None,
        scale: str = "auto",
        normalise: str = "none",
        band_window: int | None = None,
    ) -> str:
        """Analyse many series of one metric as a group (a fleet: pods, nodes, instances) instead
        of drawing them as lines: spread across members per step, outlying members, churn.

        Band (`band`, the panel's default view): a stable SPC reference, per-step median +-
        2 / 3 sigma, sigma = the pooled robust sigma the outlier tests use (+-6 steps; log
        scale: multiplicative; per behaviour group when split); the dashed flag line is the
        tests' single-step bar (approximate), `outside_3sigma_unflagged` counts member-steps
        beyond 3 sigma the family-wise tests did not flag against the ~0.27% a normal fleet
        gives (zones are for reading, flags come only from the tests), and `widening` lists
        steps where more members lie beyond 3 sigma than chance gives (common cause). Spread (`spread`,
        the panel's quantile view): median, quartiles, 10/90% and min/max across the members
        that reported at each step (descriptive; missing members reduce n, never imputed; drawn
        with missing-member bounds), n per step and missing share. Outliers are judged against the OTHER members (leave-one-out median / MAD),
        robust to the outliers themselves, by: level (consistently off), change (drifting or
        shifted over the time range) and excursions (spikes, 5- and 15-step episodes). Family-wise
        error 1% across members, tests and steps (Bonferroni); heavy-tailed noise is detected
        and thresholds follow the fleet's own peaks. Each outlier: member labels, kind
        (persistent | drifting | shifted | transient), direction, score (>= 1 = beyond the
        threshold), since (start of the current deviation; since_window_start = at least
        since the time range began), effect (ratio or difference with 99% interval, from n_eff),
        transient episodes (sustained vs momentary relative to the autocorrelation time; a
        level or change outlier may also carry episodes beyond its own level, `calibrated:
        false`: a lead, not a finding) and
        an `evidence` statistic for finding_create. Churn: members that appeared or stopped
        reporting (state silent = may be the sick one; marked_stale = the source's staleness marker). Unknown spans
        (failed fetches) leave n and alive; partial buckets are flagged on members/episodes;
        `located` caveats carry where = {series, spans}. Heterogeneous fleets (> 10% named) are
        split into behaviour groups when a SigClust test supports it (`clusters`: k, the label
        that explains them, per-group outliers tagged `cluster`).
        by: label names that identify members (must be unique per series; default: the labels
        that vary). scale: auto (log = ratios when every value > 0) | log | linear.
        normalise: none | member (each member relative to its own median: compares shapes of
        members with different sizes; the level test is off). band_window: odd steps, 13 (the
        tests' pool, default) to min(steps, 121); larger pools sigma wider and smooths the
        centre by a moving median for a calmer band (flags unchanged). Ranges over 1440 steps are
        averaged per member first. Refused on percentile series (median of p99s is not the
        fleet p99), distributions, raw counters, < 5 members, members with different units.
        Sources (`variation`, `source`): the spread = common cause; behaviour groups = systemic
        structure (common cause); outliers = special cause (undetermined when they rest on
        partial buckets); churn, missing / unknown members = measurement system (a silent
        member: undetermined). Draw with show(dataset, question, mark="fleet")."""
        try:
            return _dump(with_cite(service.fleet(dataset, by, scale, normalise, band_window)))
        except (NotFound, ValueError) as e:
            raise ToolError(str(e)) from e

    @mcp.tool(name="filter")
    def filter_tool(
        dataset: str, kind: str, period: str, reason: str, period_hi: str | None = None
    ) -> str:
        """Derive a filtered dataset: kind lowpass | highpass | bandpass (Gaussian, zero-phase).

        The cutoff is a PERIOD at half power, e.g. "15m" (bandpass: period..period_hi, with
        period_hi >= 4 x period). It must lie between 2 x step (Nyquist) and half the range.
        lowpass: trend, capacity, sustained shifts: smooths scrape jitter; NEVER use it to look
        for spikes (it erases them). highpass: removes the slow baseline/diurnal part to expose
        spikes, bursts and steps. bandpass: isolate a periodicity found by spectrum.
        reason: ONE line (<= 160 chars) shown on the panel; state why this filter answers the
        question. Gaps split the series (nothing is interpolated); points within 3 sigma of a gap
        or the range edge are flagged and dashed. Refused on percentile series, distributions
        and raw counters. Returns {dataset, summary}; `show` the dataset: the panel offers
        filtered / raw (and overlay or removed part) and the user can switch at any time."""
        try:
            return _dump(service.filter(dataset, kind, period, reason, period_hi))
        except (NotFound, ValueError) as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    async def query_distribution(
        selector: str,
        by: list[str] | None = None,
        start: str = "now-1h",
        end: str = "now",
        step: str = "auto",
        source: str = "default",
    ) -> str:
        """Fetch a histogram as a distribution dataset: counts per value bucket per step.

        selector: the histogram metric with label filters, no functions:
          classic   http_server_request_duration_seconds_bucket{job="api"}   (le buckets)
          VictoriaMetrics  x_bucket{...}                                      (vmrange buckets)
          native    traces_spanmetrics_latency{service="checkout"}           (no _bucket suffix)
        by: labels kept as separate series (e.g. ["cloud_region"]); all others are summed.
        Counts are increase() over a query window equal to the query step (tiles; never from
        quantiles), so they add up over time and across adjacent buckets. step: auto (~300
        columns), never shorter than two series intervals.
        Returns {dataset, summary}: n per series, columns with/without data, low-n columns,
        the bucket scheme (the resolution limit) and the BUCKET holding p50/p90/p99 where n
        is large enough. Draw it with `show` (heatmap), or `show(mark="histogram",
        windows=[...])` to compare time ranges.
        """
        try:
            return _dump(
                await service.query_distribution(selector, by or [], start, end, step, source)
            )
        except SourceError as e:
            raise _source_error(e) from e
        except ValueError as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    def fraction_over(
        dataset: str,
        x: float,
        start: str | None = None,
        end: str | None = None,
        by_series: bool = False,
    ) -> str:
        """What fraction of observations exceeded x? From a distribution dataset (query_distribution).

        x in the metric's unit (e.g. 0.25 for 250 ms). start/end: optional time range (default the
        whole dataset); it snaps outward to whole steps. Counts are merged across series by
        summing (additive) unless by_series=true.
        Exact when x is a source bucket edge; otherwise `fraction` is [min, max] bounded by the
        bucket containing x (`inside_bucket`), never interpolated. `ci95` is the Wilson interval
        for sampling noise. n is the number of observations; `low_count` means too few to trust.
        Each series carries an `evidence` statistic you can pass to finding_create as is.
        """
        try:
            return _dump(service.fraction_over(dataset, x, start, end, by_series))
        except (NotFound, ValueError) as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    async def source_connect(
        name: str,
        url: str | None = None,
        grafana: str | None = None,
        uid: str | None = None,
        flavor: str = "prometheus",
        resolution: str = "auto",
        auth_env: str | None = None,
        auth_file: str | None = None,
        auth_scheme: str = "bearer",
        max_concurrency: int = 4,
        min_interval: str = "0s",
        timeout: str = "30s",
        replace: bool = False,
        profile_source: str | None = None,
        timezone: str = "UTC",
    ) -> str:
        """Connect a Prometheus-compatible source at runtime (no daemon restart).

        Public demo sources connect by name alone, e.g. source_connect(name="grafana-play"):
        url, flavor, resolution (series interval) and politeness come from the registry (see public_sources);
        only `replace` applies then. Give `url` to connect anything else.
        url: API base without /api/v1, e.g. http://prometheus:9090, a VictoriaMetrics
        /select/0/prometheus path, or a Grafana datasource proxy
        https://<grafana>/api/datasources/proxy/uid/<uid>.
        grafana + uid: connect a datasource you found with source_discover_grafana (e.g.
        source_connect(name="play-mimir", grafana="https://play.grafana.org",
        uid="grafanacloud-prom")), without building the proxy url yourself. The datasource's
        own buildinfo (through Grafana's proxy) decides `flavor` (e.g. VictoriaMetrics ->
        victoriametrics); the given `flavor` argument is ignored. Mutually exclusive with url.
        flavor: "victoriametrics" (MetricsQL rollup) or "prometheus" (also works on VM,
        Thanos, Mimir); ignored when grafana+uid detect it.
        resolution: the series interval. "auto" (default): measured from the sample spacing
        (median spacing per job, i.e. the scrape interval for scraped metrics; the coarsest
        job's when they differ), re-measured by source_learn; or
        a duration (e.g. 15s, 60s) that overrides the measurement. source_status shows both.
        Secrets: NEVER pass a token. Ask the user to put it in a file (auth_file, absolute
        path; picked up immediately) or an env var of the daemon (auth_env, the variable
        NAME; needs a daemon restart if set later). auth_scheme: bearer | basic ("user:pass").
        With grafana+uid, this is Grafana's own token (the proxy forwards it); it needs
        access to that datasource.
        Politeness for shared/public servers: lower max_concurrency, set min_interval
        (e.g. 500ms), raise timeout (e.g. 60s).
        profile_source: name of another source with downsampled data of the same series (e.g. a
        Thanos downsample-1h datasource, connected with resolution=1h); long-time-range operating
        profiles are read from it.
        timezone: IANA timezone (e.g. Europe/Berlin) the operating profile counts hour-of-day and
        hour-of-week in. Set it where load follows people (business hours, DST shifts); default UTC.
        The source is probed before it is saved; it persists across daemon restarts.
        Returns {source, status} (plus {backend} when detected via grafana+uid).
        """
        auth = _auth_ref(auth_env, auth_file, auth_scheme)

        def spec(source_url: str, source_flavor: str) -> SourceSpec:
            return SourceSpec.model_validate(
                {
                    "name": name,
                    "url": source_url,
                    "flavor": source_flavor,
                    "resolution_ms": None if resolution == "auto" else parse_duration(resolution),
                    "auth": auth,
                    "politeness": {
                        "max_concurrency": max_concurrency,
                        "min_interval_ms": parse_duration(min_interval),
                        "timeout_s": parse_duration(timeout) / 1000,
                    },
                    "profile_source": profile_source,
                    "timezone": timezone,
                }
            )

        try:
            if grafana is not None:
                if url is not None:
                    raise ToolError("pass either grafana+uid or url, not both")
                if uid is None:
                    raise ToolError(
                        "grafana needs uid: see source_discover_grafana(url=...) for datasource uids"
                    )
                datasource_url = proxy_url(grafana, uid)
                backend, detected_flavor = await probe_backend(
                    datasource_url, AuthRef.model_validate(auth) if auth else None
                )
                out = await service.source_connect(
                    spec(datasource_url, detected_flavor), replace=replace
                )
                return _dump({**out, "backend": backend})
            if url is None:
                entry = PUBLIC_SOURCES.get(name)
                if entry is None:
                    raise ToolError(
                        f"no url given and {name!r} is not a public source "
                        f"(known: {', '.join(sorted(PUBLIC_SOURCES))})"
                    )
                return _dump(await service.source_connect(entry.to_spec(), replace=replace))
            return _dump(await service.source_connect(spec(url, flavor), replace=replace))
        except ValidationError as e:
            raise _fail(e) from e
        except SourceError as e:
            raise _source_error(e) from e
        except ValueError as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    async def source_discover_grafana(
        url: str,
        auth_env: str | None = None,
        auth_file: str | None = None,
        auth_scheme: str = "bearer",
    ) -> str:
        """List datasources exposed by a Grafana instance, e.g. https://play.grafana.org or
        https://grafana.wikimedia.org, so you can connect one by uid with
        source_connect(name=..., grafana=url, uid=...).

        Without auth_env/auth_file: /api/frontend/settings, what an anonymous visitor's own
        browser loads (what it contains depends on the instance's anonymous-access setting).
        With a Grafana API token or service account token (auth_env/auth_file, NEVER pass
        the token itself; auth_scheme: bearer | basic): /api/datasources, the authoritative
        list, including datasources hidden from anonymous users.
        Each datasource: uid (pass to source_connect), name, type (Grafana's plugin id),
        is_default, backend_hint (Grafana's own unverified jsonData.prometheusType, e.g.
        Mimir/Thanos/Cortex, when an admin set it), and supported. supported=false
        datasources (Loki, InfluxDB, Elasticsearch, ...) are listed but cannot be connected
        (see telemetry-nerd-sgb). The actual backend/flavor
        (Prometheus/Thanos/Mimir/VictoriaMetrics) is only established once connected:
        source_connect(grafana=..., uid=...) asks the datasource's own buildinfo.
        """
        try:
            auth = _auth_ref(auth_env, auth_file, auth_scheme)
            datasources = await discover_datasources(
                url, AuthRef.model_validate(auth) if auth else None
            )
            return _dump(
                {"grafana_url": url.rstrip("/"), "datasources": [d.describe() for d in datasources]}
            )
        except ValidationError as e:
            raise _fail(e) from e
        except SourceError as e:
            raise _source_error(e) from e

    @mcp.tool()
    def public_sources() -> str:
        """Public demo/test sources you can connect by name with source_connect(name=...):
        backend, flavor, politeness limits (concurrency, min interval, timeout, max query
        range) and suggested use. None is connected until you ask. They are shared servers
        run by third parties: interactive volume only, narrow queries, never broad scans."""
        return _dump({"sources": [e.describe() for e in PUBLIC_SOURCES.values()]})

    @mcp.tool()
    def source_list() -> str:
        """Connected sources: name, url, flavor, resolution (series interval), auth reference (never the
        secret), politeness, whether it is live or broken (e.g. its secret is missing)."""
        return _dump({"sources": service.source_list()})

    @mcp.tool()
    async def source_status(name: str) -> str:
        """Probe a source now: {reachable, latency_ms, application?, version?, resolution} or
        {reachable: false, error, hint}. resolution: the source's series interval (the
        finest query step worth reading), its origin (learned from sample spacing | configured |
        assumed) and the spacing measured per job."""
        try:
            return _dump(await service.source_status(name))
        except SourceError as e:
            raise _source_error(e) from e

    @mcp.tool()
    async def source_disconnect(name: str) -> str:
        """Remove a runtime source. Existing datasets and panels keep working."""
        try:
            await service.source_disconnect(name)
        except SourceError as e:
            raise _source_error(e) from e
        return _dump({"disconnected": name})

    @mcp.tool()
    async def source_learn(source: str = "default") -> str:
        """Discover a source's metrics and learn what is cheap to know (declared metadata, naming
        conventions, knowledge packs for node_exporter/Kubernetes). Slow on huge sources (it lists
        every metric name); run it once per source, then use catalog_search. Returns counts,
        caveats and `families`: name groups (metrics sharing a prefix, least-reviewed first;
        catalog_family(family=<name>) lists one's metrics); `name_template_families` counts the
        name-template families. Which services report which families: `entities`."""
        try:
            out = await service.learn(source, "claude")
            templates = out.pop("families", 0)
            return _dump(
                {
                    **out,
                    "name_template_families": templates,
                    "families": service.ws.catalog_overview(source),
                    "next": "catalog_family(family=<a families[].family>) lists its metrics; "
                    "catalog_search(query=<words>) matches names and descriptions; "
                    'entities(kind="service") lists the services and the families each reports',
                }
            )
        except SourceError as e:
            raise _source_error(e) from e

    @mcp.tool()
    def catalog_search(
        query: str | None = None,
        prefix: str | None = None,
        needs_review: bool = False,
        limit: int = 50,
        source: str = "default",
    ) -> str:
        """Find catalogued metrics. `query` matches name or description; `prefix` is a name
        prefix (a family, e.g. node_cpu). `needs_review` keeps metrics nobody has interpreted yet
        (no role from pack/claude/user) or whose claims conflict. Metrics this workspace already
        queried come first (hot). Rows carry the winning type/unit/role/bounds and their origin."""
        try:
            return _dump(service.ws.catalog_search(source, query, prefix, needs_review, limit))
        except (NotFound, ValueError) as e:
            raise _fail(e) from e

    @mcp.tool()
    def catalog_context(
        files: list[dict[str, str]], dry_run: bool = False, source: str = "default"
    ) -> str:
        """Teach the catalog what the repo, docs and dashboards say about this source's metrics.
        You read the files (rg/ast-grep to find them), send `files` as [{path, text}] (<= 50 files,
        <= 1 MB each; the daemon reads nothing itself). Extracted deterministically: Python
        prometheus_client and OpenTelemetry registrations, Go prometheus/OpenTelemetry, JS/TS
        OpenTelemetry, Grafana dashboard JSON (panel unit and description for single-metric panels),
        markdown metric tables, and the pipeline between code and source: OpenTelemetry Collector
        configs (metricstransform renames/scales, simple transform/OTTL renames and units,
        prometheus exporter namespace and add_metric_suffixes) and Prometheus relabel renames of
        __name__. A code definition then finds its renamed series (lower confidence, the rules cited).
        Names built at runtime are skipped and reported, never guessed.
        Writes origin=context claims (description, type, unit) with a file:line citation for metrics
        this source has; they rank below measured behaviour, Claude and the user. If one disagrees
        with what the source declares, a pack or a scan, a finding is filed. Returns matched
        metrics, `unmatched` definitions (in code, not in this source: a lead), skipped files.
        dry_run=true previews without writing."""
        try:
            return _dump(service.ws.catalog_context(source, files, dry_run, "claude"))
        except (NotFound, ValueError) as e:
            raise _fail(e) from e

    @mcp.tool()
    def catalog_family(
        template: str | None = None,
        action: str | None = None,
        basis: str | None = None,
        source: str = "default",
        family: str | None = None,
    ) -> str:
        """Metric families, two kinds. A NAME GROUP is metrics sharing a name prefix, as
        source_learn's `families` lists them (rpc_server, http_server, node_cpu):
        catalog_family(family="rpc_server") returns its metrics. A NAME-TEMPLATE family encodes a
        dimension in the name (airflow_ti_finish_*_removed); decide one: `confirm` (its members
        really do share a metric) or `split` (unrelated metrics: dissolve it for good), e.g.
        catalog_family(template="airflow_ti_finish_*_removed", action="confirm", basis="members
        differ only in the task name"). No arguments LISTS both kinds (undecided templates first).
        `family` is an alias of `template`; `source` defaults to "default". Deciding needs a
        `basis`; you cannot change a family the user confirmed."""
        try:
            template = template or family
            known = service.ws.families_list(source)
            names = {f["template"] for f in known}
            usage = (
                'catalog_family(family="<a name group>") lists its metrics; '
                'catalog_family(template="<one of families[].template>", '
                'action="confirm"|"split", basis="what you checked") decides a template'
            )
            if not action:
                if template in names:
                    return _dump(service.ws.family_members(source, template))
                if template:
                    group = service.ws.catalog_name_group(source, template)
                    if group["metrics"]:
                        return _dump(group)
                return _dump(
                    {
                        "families": known,
                        "name_groups": service.ws.catalog_overview(source, top=100),
                        "usage": usage,
                    }
                    | (
                        {
                            "note": f"no name group or name-template family {template!r} in "
                            f"source {source!r} (searched {len(names)} templates and the name "
                            "prefixes of every catalogued metric). The catalog holds what "
                            "source_learn saw; it is not evidence that a signal is absent: "
                            "try catalog_search with words, or `entities`."
                        }
                        if template
                        else {}
                    )
                )
            if not template:
                raise ValueError("name the family: template=<one of families[].template>")
            if not basis or not basis.strip():
                raise ValueError("basis is required: one line saying what you checked")
            if template not in names:
                near = sorted(t for t in names if template.strip("*_") in t)
                if not near and service.ws.catalog_name_group(source, template)["metrics"]:
                    raise ValueError(
                        f"{template!r} is a name group (a shared prefix), not a name-template "
                        "family: there is nothing to confirm or split. List its metrics with "
                        f'catalog_family(family="{template}")'
                    )
                raise NotFound(
                    f"no family {template!r} on {source!r}; "
                    f"{'did you mean ' + repr(near[:5]) if near else 'list them with no arguments'}"
                )
            return _dump(
                service.ws.catalog_family_decide(
                    source, template, action, "claude", "claude", basis=basis.strip()
                )
            )
        except (NotFound, ValueError) as e:
            raise _fail(e) from e

    @mcp.tool()
    def catalog_get(metric: str, source: str = "default") -> str:
        """One metric's full catalog entry: resolved fields and every competing claim with its
        origin, confidence and basis, plus the fields where claims disagree."""
        try:
            e = service.ws.catalog_entry(source, metric)
            rels = service.ws.catalog_relations(source, metric)
            return _dump(
                {
                    "relations": [_rel_dict(r) for r in rels["relations"]],
                    "bindings": [_bind_dict(b) for b in rels["bindings"]],
                    **e.model_dump(exclude={"fields"}),
                    "resolved": {f: c.value for f, c in e.fields.items()},
                    "conflicts": {
                        f: [c.model_dump() for c in cs] for f, cs in e.conflicts().items()
                    },
                }
            )
        except (NotFound, ValueError) as e:
            raise _fail(e) from e

    @mcp.tool()
    def catalog_write(claims: list[dict[str, Any]], source: str = "default") -> str:
        """Record what you have learned about metrics, up to 200 claims per call. Each claim:
        {metric, field, value, confidence, basis}. field is one of type, unit, bounds,
        additivity_series, additivity_time, role, description, histogram_family, thresholds ([{value, label,
        tone bad|warn|info}]: known good/bad lines such as an SLO, drawn on the metric's charts), statistic
        (count, count_below, sum, min, max, mean, ratio, median, percentile, truncated_mean,
        mad, iqr — the mergeability table: only count/min/max/sum merge across time/series
        directly, mean/ratio need the counts too, the rest never merge). Relations between
        metrics (bounded_by, part_of, ...) go through catalog_relate.
        `basis` (required) is one line saying what you checked; confidence is at most 0.9
        (1.0 is reserved for the user). Writes are origin=claude: they never override a user
        claim or a higher-ranked origin, and the result says whether each claim is now the
        winner. Bad claims are rejected individually."""
        try:
            return _dump({"results": service.ws.catalog_write_claude(source, claims)})
        except ValueError as e:
            raise _fail(e) from e

    @mcp.tool()
    def catalog_propose(claims: list[dict[str, Any]], source: str = "default") -> str:
        """End-of-session (/telemetry-nerd:wrap): PROPOSE catalog updates for the user to
        approve in the UI, up to 50 per call. Each: {metric, field, value, confidence, basis,
        evidence?} with the fields and values of catalog_write; evidence: finding/panel ids
        (f3, p7) the value rests on. Nothing reaches the catalog until the user approves
        (then a claim origin=claude verified_by=user; an edited value is the user's own claim).
        Use catalog_write instead for what you need now, mid-investigation. Items are checked
        one by one: unknown metric, bad value, duplicate of an open proposal, already confirmed
        by the user. Returns {results: [{metric, field, status: proposed|rejected,
        proposal?, reason?}]}."""
        try:
            return _dump({"results": service.retro.catalog_propose(source, claims)})
        except ValueError as e:
            raise _fail(e) from e

    @mcp.tool()
    def lesson_propose(
        text: str,
        scope: dict[str, Any],
        evidence: list[str],
        expires: str | None = None,
    ) -> str:
        """PROPOSE a methodology lesson for later sessions (the user approves it in the UI).
        text: one actionable sentence ("for checkout, split latency by region before reading
        p95: one region carries the tail").
        scope (required): {source, service?, metric_family?, labels?} where it applies.
        evidence (required): the findings/panels (f3, p7) it comes from.
        The scope is NEVER broader than the evidence: at least one cited item must cover all
        of it (evidence about service_name="checkout" carries a checkout lesson, not a
        source-wide one; two services are not "the source"); refused with what each item
        covers (lesson_beyond_evidence). metric_family (a prefix or * glob) narrows it to one
        metric family; without it the lesson applies across the scope's metrics.
        expires: a duration (90d) or date (2027-03-01); default 180 days, at most 2 years.
        Returns {lesson, scope_check: {covered_by, partial}}."""
        try:
            lesson = service.retro.lesson_propose(
                text, LessonScope.model_validate(scope), evidence, expires
            )
            return _dump(
                {"lesson": lesson.id, "status": lesson.status,
                 "scope_check": lesson.scope_check.model_dump(),
                 "expires": iso(lesson.expires_at_ms)}
            )  # fmt: skip
        except (ValidationError, NotFound, ValueError) as e:
            raise _fail(e) from e

    @mcp.tool()
    def lesson_refute(lesson: str, evidence: list[str], reason: str) -> str:
        """Mark an approved lesson refuted: a finding (f…) whose scope overlaps the lesson's
        shows it no longer holds. reason: what the finding shows. The lesson then never
        surfaces again (the user sees why in the Proposals view)."""
        try:
            out = service.retro.refute_lesson(lesson, evidence, reason, "claude")
            return _dump({"lesson": out.id, "status": out.status})
        except (NotFound, ValueError) as e:
            raise _fail(e) from e

    @mcp.tool()
    def lessons_for(
        source: str,
        services: list[str] | None = None,
        metric_families: list[str] | None = None,
        labels: dict[str, str] | None = None,
    ) -> str:
        """Approved lessons from earlier sessions that apply here: scope source equal, and its
        service / metric_family / labels, when it names one, among those you pass. Call it once
        the source and the services in question are known (start, investigate). `held` counts
        approved lessons on this source scoped to other services or families (pass them to see
        those). A lesson is a prior from past evidence, not evidence: say it applies, cite its
        evidence ids, and still check it in the data."""
        return _dump(service.retro.lessons_for(source, services, metric_families, labels))

    @mcp.tool()
    def proposals_list(status: str | None = None) -> str:
        """Catalog proposals and lessons on file, newest first (status: proposed | approved |
        rejected | refuted | expired to filter). Check it before proposing so nothing is
        proposed twice."""
        out = service.retro.listing(status)
        brief = {
            "pending": out["pending"],
            "catalog": [
                {k: p[k] for k in ("id", "status", "metric", "field", "value")}
                for p in out["catalog"][:30]
            ],
            "lessons": [
                {
                    "id": x["id"],
                    "state": x["state"],
                    "text": x["text"][:120],
                    "scope": LessonScope.model_validate(x["scope"]).describe(),
                }
                for x in out["lessons"][:30]
            ],
        }
        return _dump(brief)

    @mcp.tool()
    async def catalog_scan(
        source: str = "default",
        metrics: list[str] | None = None,
        prefix: str | None = None,
        limit: int = 25,
        window: str = "30m",
        refresh: bool = False,
    ) -> str:
        """Measure what a short time range (`window`) of raw samples says about catalogued metrics: negatives,
        monotonic growth, counter resets, small decreases (a counter never does that). Writes
        origin=stats claims (type counter/gauge when the evidence is strong, bounds >=0 to fill a
        gap; never over a pack, Claude or user claim; typical_range = observed p1-p99 with
        min/max for non-counters, descriptive and shown as a "typical range" y view, never a
        bound) and files contradictions (a declared gauge
        that only grows, a counter that decreases, negative values) as system findings.
        Targets: `metrics`, else `prefix`, else the metrics this workspace already queried. Bounded:
        <= 100 queries per call, a time budget, metrics scanned in the last day skipped, metrics
        above the source's series cap skipped. A short time range only suggests: say so."""
        try:
            return _dump(
                await service.scan_metrics(source, metrics, prefix, limit, window, refresh)
            )
        except SourceError as e:
            raise _source_error(e) from e
        except ValueError as e:
            raise _fail(e) from e

    def _rel_dict(r: Any) -> dict:
        w = r.winner
        out: dict[str, Any] = {
            "subject": r.subject, "kind": r.kind, "object": r.object, "origin": w.origin,
            "confidence": w.confidence, "contested": r.contested,
        }  # fmt: skip
        if w.params:
            out["params"] = w.params
        if w.basis:
            out["basis"] = w.basis
        if w.retracted:
            out["retracted"] = True
        return out

    def _bind_dict(b: Any) -> dict:
        w = b.winner
        out: dict[str, Any] = {
            "kind": b.kind, "key": b.key, "roles": w.roles, "join_on": w.join_on,
            "origin": w.origin, "confidence": w.confidence, "contested": b.contested,
        }  # fmt: skip
        if w.basis:
            out["basis"] = w.basis
        if w.retracted:
            out["retracted"] = True
        return out

    @mcp.tool()
    def catalog_relate(
        claims: list[dict[str, Any]], level: str = "catalog", source: str = "default"
    ) -> str:
        """Record typed edges between metrics (level=catalog) or datasets (level=workspace), up to
        200 per call. Each claim: {subject, kind, object, confidence, basis, params?, retract?}.
        kinds: derived_from, part_of (errors part_of requests), same_quantity, upstream_of,
        bounded_by (subject never exceeds object at the same labels: avail bounded_by size),
        threshold_by (the object is a known threshold line for the subject: critical temperature,
        request; params tone bad|warn|info),
        bounded_by/threshold_by take params join_on (labels shared), matchers ({"resource": "memory"}),
        applies_to ("rate" bounds rate(subject)), zero_is_unlimited, expr (derived target such as
        "quota / period"), label,
        correlated (needs params {coefficient, lag_ms, scope}; evidence, capped at 0.7).
        `basis` is required; confidence at most 0.9. Set retract=true to say an edge does not hold
        (it removes a wrong pack edge without erasing history). Origin is always claude: user
        claims outrank you, and the result says whether each claim is now the winner."""
        try:
            return _dump({"results": service.ws.relate_claude(source, claims, level)})  # type: ignore[arg-type]
        except ValueError as e:
            raise _fail(e) from e

    @mcp.tool()
    def catalog_bind(
        kind: str,
        key: str,
        roles: dict[str, str | None],
        confidence: float,
        basis: str,
        join_on: list[str] | None = None,
        retract: bool = False,
        level: str = "catalog",
        source: str = "default",
    ) -> str:
        """Bind signals to the roles of a model for one service/resource `key`. kinds and roles:
        littles_law {arrival_rate, latency, concurrency}; RED {rate, errors, duration};
        USE {utilization, saturation, errors}. Give every role: a metric name, or null when no
        such signal exists: each null raises a Gap recommending the missing instrumentation.
        `join_on` lists the labels that tie the signals together. Same basis/confidence rules as
        catalog_relate; origin is claude."""
        try:
            return _dump(
                service.ws.bind_claude(
                    source,
                    kind,
                    key,
                    roles,
                    join_on=join_on,
                    confidence=confidence,
                    basis=basis,
                    retract=retract,
                    level=level,  # type: ignore[arg-type]
                )
            )
        except (NotFound, ValueError) as e:
            raise _fail(e) from e

    @mcp.tool()
    async def entities(
        kind: str = "service",
        label: str | None = None,
        metric: str | None = None,
        window: str = "1h",
        recent: str = "5m",
        end: str = "now",
        limit: int = 50,
        source: str = "default",
    ) -> str:
        """Which services exist? (or instances, namespaces, nodes): the values of the
        identifying labels (kind=service: service_name, service, app, k8s_deployment_name, job,
        ...; instance: service_instance_id, instance, pod, host; namespace; node) in series with
        samples in the `window` (a time range) before `end`, from the source's label index (cheap: no samples
        read; cached a minute). Per entity: the metric families and metrics it reports,
        `active_recent` (samples in the last `recent`), and the binding_suggest ids whose metrics
        it reports (`next`: binding_suggest(kind="RED", key=<entity>)). `metric` narrows to the
        entities one metric reports; `label` searches one label of your choosing. `coverage`
        says which labels were searched and which are absent: run this before saying a service
        does not exist, and cite it; an entity not listed is "not found under these labels in
        this time range", never "absent"."""
        try:
            return _dump(
                await service.entity_index.entities(
                    source, kind, label, metric, window, recent, end, limit
                )
            )
        except SourceError as e:
            raise _source_error(e) from e
        except ValueError as e:
            raise _fail(e) from e

    @mcp.tool()
    def binding_suggest(
        kind: str | None = None,
        key: str | None = None,
        limit: int = 10,
        source: str = "default",
    ) -> str:
        """Propose model bindings from what the catalog already knows (metric names, types, packs,
        relations; nothing is queried from the source). kind: littles_law | RED | USE (default all).
        key: a suggested scope key (http.server, node:cpu, k8s:container-cpu, ...) to narrow, or
        the service/instance name to bind for (it is then used as the binding key). Returns
        ranked suggestions: id, kind, key, roles {role: metric|null}, per-role `detail` (confidence,
        basis [pack|naming|relation], form, expr hint: rate() for counters, histogram _sum/_count
        and _bucket for latency, never a precomputed percentile while a histogram exists, plus
        `alternatives` and `ambiguous`), `unfilled` roles with the instrumentation that would fill
        them, and join_on label hints (conventions: verify against the series). Check the picks,
        then confirm with `binding_accept(id, basis)` or `catalog_bind`."""
        try:
            return _dump(service.ws.binding_suggest(source, kind, key, limit))
        except ValueError as e:
            raise _fail(e) from e

    @mcp.tool()
    def binding_accept(
        id: str,
        basis: str,
        key: str | None = None,
        overrides: dict[str, str | None] | None = None,
        join_on: list[str] | None = None,
        confidence: float | None = None,
        source: str = "default",
    ) -> str:
        """Confirm a `binding_suggest` suggestion by id: a catalog_bind with its roles and
        join_on. `basis` (required) says what you checked. `key` names the entity to bind (as in
        binding_suggest); `overrides` {role: metric|null} swaps a role for one of its listed
        alternatives; confidence defaults to the suggestion's (at most 0.9). Unfilled roles raise
        Gaps exactly as in catalog_bind."""
        try:
            return _dump(
                service.ws.binding_accept(
                    source, id, basis=basis, key=key, overrides=overrides,
                    join_on=join_on, confidence=confidence,
                )
            )  # fmt: skip
        except (NotFound, ValueError) as e:
            raise _fail(e) from e

    @mcp.tool()
    async def show_binding(
        source: str = "default",
        kind: str | None = None,
        key: str | None = None,
        suggestion: str | None = None,
        range: str | None = None,
        start: str = "now-1h",
        end: str = "now",
        step: str = "auto",
        matchers: dict[str, str] | None = None,
        error_matcher: str | None = None,
    ) -> str:
        """Draw a USE / RED / Little's law metric set as ONE panel group: one time range and step,
        linked crosshair and selection, a header naming the binding. Pass `kind` + `key` of a
        confirmed binding (catalog_bind / binding_accept), or `suggestion` (a binding_suggest id,
        e.g. RED:otel_http) to look before confirming. range: a duration back from now ("6h"),
        else start/end. matchers {label: value} narrow every role (e.g. {"service_name": "cart"}).
        Each role is drawn in the form its semantics call for: rates of counters; RED errors as
        errors / requests with a Wilson 95% band (a separate error counter over the requests, or
        the request metric split by the status matcher of the suggestion's expr hint; when no
        status label is known pass error_matcher, e.g. 'status_code=~"5.."'); latency from a
        histogram as its distribution (heatmap / percentile bands, never averaged percentiles);
        utilization on its natural 0..1 axis; saturation with its bounded_by limit line;
        concurrency as the gauge. Roles with more than 5 members are drawn as fleets; a missing
        role is a gap card naming the instrumentation that would fill it. Label names in hints
        are conventions: check `notes` and role errors. Returns {group, kind, key, range, step,
        roles {role: {panel, metric, form, view, members, notes} | {gap, suggest} | {error}},
        gaps}. Little's-law consistency is check_littles_law's job, not this view's."""
        try:
            if range is not None:
                start, end = f"now-{range.strip()}", "now"
            g = await service.show_binding(
                source=source, kind=kind, key=key, suggestion=suggestion, start=start, end=end,
                step=step, matchers=matchers, error_matcher=error_matcher, actor="claude",
            )  # fmt: skip
        except SourceError as e:
            raise _source_error(e) from e
        except (NotFound, ValueError) as e:
            raise _fail(e) from e
        return _dump(group_summary(g, ui_url))

    @mcp.tool()
    async def binding_verdict(
        source: str = "default",
        kind: str | None = None,
        key: str | None = None,
        group: str | None = None,
        suggestion: str | None = None,
        range: str | None = None,
        start: str = "now-1h",
        end: str = "now",
        step: str = "auto",
        reference: str = "auto",
        tz: str = "UTC",
        matchers: dict[str, str] | None = None,
        error_matcher: str | None = None,
        alpha: float = 0.05,
    ) -> str:
        """Per-signal verdicts for a USE / RED / Little's law binding: did each role move
        against reference time ranges, how, when, and which golden signal moved first. Pass a panel
        group id (`group`, from show_binding: its time range, step and matchers are used and its
        roles get verdict badges), or kind + key of a confirmed binding, or a binding_suggest
        `suggestion`. reference: previous (4 preceding time ranges) | day (7 previous days) | week
        (4 previous weeks) | profile (the cached operating profile's seasonality picks day or
        week) | auto (profile if cached, else previous). Per role: errors = the error share on
        effective n (Wilson / binomial); latency = the share of requests above the reference's
        ~p95 bucket edge (from histograms; never averaged percentiles); rates, concurrency,
        utilization, saturation = the per-step value; utilization near its bound is reported as
        at_capacity. Two detectors per role (level over the time range vs the reference spread; CUSUM
        episodes for onsets), Bonferroni over roles at family-wise `alpha`. Returns {reference,
        family, roles {role: {status changed|no_change|insufficient|gap|error, direction,
        pattern level|shift|blip|burst|sustained, onset {at, interval}, level, episodes,
        evidence, source, ...}}, summary {moved, first, order, text}, variation}. Sources: a
        changed role = special cause (undetermined when its data has measurement-system
        issues), no change = common cause; the Little's law model_check carries its own.
        Ordering is claimed only when onset intervals do not overlap ("simultaneous" otherwise)."""
        try:
            if range is not None:
                start, end = f"now-{range.strip()}", "now"
            out = await service.binding_verdict(
                source=source, kind=kind, key=key, group=group, suggestion=suggestion,
                start=start, end=end, step=step, reference=reference, tz=tz, matchers=matchers,
                error_matcher=error_matcher, alpha=alpha, actor="claude",
            )  # fmt: skip
        except SourceError as e:
            raise _source_error(e) from e
        except (NotFound, ValueError) as e:
            raise _fail(e) from e
        return _dump(with_cite(out))

    @mcp.tool()
    def catalog_relations(
        source: str = "default",
        metric: str | None = None,
        kind: str | None = None,
        level: str = "catalog",
        include_retracted: bool = False,
    ) -> str:
        """Resolved relations and bindings, optionally only those touching `metric` or of one
        `kind`. `contested` means a lower-ranked claim disagrees with the winner."""
        try:
            out = service.ws.catalog_relations(source, metric, kind, level, include_retracted)  # type: ignore[arg-type]
        except ValueError as e:
            raise _fail(e) from e
        return _dump(
            {
                "relations": [_rel_dict(r) for r in out["relations"]],
                "bindings": [_bind_dict(b) for b in out["bindings"]],
            }
        )

    @mcp.tool()
    async def operating_profile(expr: str, source: str = "default", refresh: bool = False) -> str:
        """What `expr` normally looks like: 1h buckets over 30 days (T1 operating profile).
        Computed on first use (panels you show are profiled in the background), cached, refreshed
        daily; refresh=true recomputes now. Counters are profiled as rates; rate query windows widen to
        the 1h step; quantile expressions are profiled as per-hour quantiles (never averaged).
        Returns kind, coverage, robust range over hourly values (p0.5..p99.5, median, MAD),
        envelope (robust tails of intra-hour min/max), absolute min/max, and per series the
        seasonal model chosen by leave-one-out error (none | hour_of_day | hour_of_week, counted in the source's timezone: UTC unless source_connect set one)
        with its amplitude and band width. Read the caveats: short_history, gaps,
        low_n_tails, looks_like_counter, quantile_series."""
        try:
            return _dump(await service.operating_profile(expr, source, refresh))
        except SourceError as e:
            raise _source_error(e) from e

    @mcp.tool()
    async def show(
        dataset: str,
        question: str,
        unit: str | None = None,
        mark: str = "auto",
        windows: list[dict] | None = None,
        quantiles: list[float] | None = None,
        view: str | None = None,
        segment: str | None = None,
        overlap: float | None = None,
        raw: bool = False,
        bounds_lo: float | None = None,
        bounds_hi: float | None = None,
    ) -> str:
        """Draw a dataset as a panel (mean line + min/max envelope) in the shared workspace.

        question is REQUIRED: the explicit question this graph answers, e.g.
        "Did checkout latency rise after the 14:00 deploy?".
        unit: optional y-axis unit (e.g. "s", "B", "ms", "items", "req/s"). Pass it
        when you know the unit from context the metric name doesn't reveal — you read
        the emitting code, or you know the generating tool's conventions. Your unit
        overrides suffix inference and is persisted with provenance ("provided by
        claude"), so only pass a unit you can actually vouch for. It is CHECKED: a unit that
        contradicts what the catalog or a derivation rule says the expression returns is
        refused (a label never rescales data). Named failure mode — ratio vs percent: a
        fraction of a whole (1 - rate(idle), errors/total, s/s) is unit "ratio" in [0,1];
        "%" means 0-100 and needs 100 * (...) in the expression. Same for s vs ms and bytes
        vs bits. If the catalog is wrong, fix it with catalog_write (basis) instead.
        bounds_lo / bounds_hi: optional natural bounds of what the expression measures, for a
        derived expression the catalog cannot bound (a ratio of your own making, an
        error rate, a utilisation). Like unit, they are recorded as "asserted by claude" and
        shown in the badge, so only assert what physically holds (e.g. 0 and 1 for a fraction
        of a whole). They OVERRIDE the catalog's and any derived bounds for this panel. Not
        stored (a warning says so) on heatmap, histogram, spc, spectrum or filter panels. Closed bounds become the default y axis (zoom stays available, badged).
        Known derivations (1 - rate(idle), errors/total, used/limit, 100 * ratio) are bounded
        automatically; pass these only when no rule applies or the rule is wrong.
        mark: auto (heatmap for distributions, lines otherwise). For distributions also:
        percentiles (per step, the SOURCE bucket holding each of `quantiles`, 1-4 of 0.5, 0.9,
        0.95, 0.99, 0.999, drawn only where n >= 10/(1-q); never interpolated), histogram,
        ecdf, quantile_curve (inverse ECDF as bucket boxes), ccdf (P(X > x), log-log, exact at
        bucket edges). The last four need windows.
        spc (time series): control chart from analyze: series, centre line, 3-sigma band,
        violations, shaded baseline; windows=[{start, end}] is the baseline (default first half).
        seasonal (time series, after compare_seasonal): now vs previous cycles (faint), their
        median and 90% band, flagged points; the user can switch to the ratio view.
        fleet (many series of one metric, after fleet or directly): spread band across members
        (min-max, 10-90, 25-75 shaded), median, outlying members drawn and labelled, n per step.
        littles (the concurrency dataset of check_littles_law): per group, L and λ·W per window
        with 95% bands and a ratio strip (1 = consistent) coloured by verdict.
        spectrum / spectrogram (time series): periodicity panels; spectrogram needs `segment`
        (e.g. "30m", state it in your answer) and takes `overlap` (default 0.5). view: for a panel
        drawn from filter(): the default view (overlay | filtered | removed | raw).
        windows (histogram/ecdf): 1-4 [{start, end, label}] compared on one chart, e.g. the
        spike vs the preceding baseline; each time range sums whole query steps, n is shown per
        time range.
        More series than a line chart draws (5) are never dropped: members of one group (pods,
        instances... of one metric) are drawn as a fleet (mark auto); other series as the 4 that
        stand out most plus one grey "others" band (their median and min-max). `warnings` says
        which, and names every summarised series. Percentiles over the budget are refused.
        A plain selector of a counter (a running total) is drawn as its rate, from a new dataset
        over the same time range; the answer says so under `auto`. raw=true draws exactly the dataset.
        Code outputs (expr code:<node>/<name>) are fixed data: drawn as produced, unit as the code
        declared it, a declared interval as the band; ops that re-fetch (compare_seasonal,
        reference time ranges, marginals, profiles) refuse them. A fit (estimate) is not drawn: show
        its _prediction dataset or cite its parameters as statistics.
        Returns {panel, url, warnings, auto?, y_range_notes?}: the y range defaults to the reference
        range (data, normal range, physical limit); y_range_notes says what was not available.
        """
        try:
            wins = [
                Window(
                    start_ms=parse_time(str(w["start"]), service.clock()),
                    end_ms=parse_time(str(w["end"]), service.clock()),
                    label=str(w.get("label", "")),
                )
                for w in windows or []
            ]
            res = await service.show_auto(
                dataset,
                question,
                raw=raw,
                unit=unit,
                mark=mark,
                windows=wins,
                quantiles=quantiles,
                view=view,
                segment=segment,
                overlap=overlap,
                bounds_lo=bounds_lo,
                bounds_hi=bounds_hi,
            )
        except ChartRejected as e:
            raise ToolError(f"chart rejected: {e}") from e
        except (NotFound, ValueError) as e:
            raise ToolError(str(e)) from e
        ctx = await service.y_context(res.panel.id, "claude")  # catalog bounds, limit, normal range
        out: dict[str, Any] = {
            "panel": res.panel.id,
            "url": f"{ui_url}/#/panel/{res.panel.id}",
            "warnings": [i.message for i in res.issues],
        }
        if res.panel.spec.get("auto"):
            a = res.panel.spec["auto"]
            out["auto"] = {
                "transform": a["transform"],
                "from": a["source_dataset"],
                "reason": a["reason"],
            }
            out["drawn_dataset"] = res.panel.dataset_ids[0]
        if ctx is not None and ctx.notes:
            out["y_range_notes"] = ctx.notes
        if ctx is not None and ctx.reframes:  # proposals: accept one with `reframe`, never silent
            out["reframings"] = [
                {"index": i, "title": r.title, "reason": r.reason, "basis": r.basis}
                for i, r in enumerate(ctx.reframes)
            ]
        if ctx is not None and ctx.lines:
            out["context"] = [
                {
                    "kind": ln.kind,
                    "line": ln.label or ln.metric,
                    "origin": ln.origin,
                    "basis": ln.basis,
                }
                for ln in ctx.lines
            ]
        if hint := service.seasonal_suggestion(dataset, mark):
            out["suggest"] = hint
        return _dump(out)

    @mcp.tool()
    async def split_outcome(dataset: str) -> str:
        """Latency by outcome: for a histogram-backed dataset (latency distribution or
        histogram_quantile series) open TWO new panels, successful and failed requests, so fast errors
        cannot flatter the latency and slow errors cannot hide in it. The outcome label (status,
        code, outcome, result, ...) and its values are found and classified deterministically: HTTP
        2xx/3xx success, 5xx failure; 4xx and unknown values are left out and listed in `excluded`.
        Use it whenever you report latency for a service that returns errors, and say which label
        and values were used. Returns {label, success: {panel, dataset, values}, failure, excluded}."""
        try:
            return _dump(await service.split_outcome(dataset, "claude"))
        except (ValidationError, NotFound, ValueError, SourceError) as e:
            raise _fail(e) from e

    @mcp.tool()
    async def reframe(panel: str, index: int) -> str:
        """Show a metric in a form that carries its own context: accept reframing `index` from a
        panel's `show` answer (`reframings`), e.g. available instead of free memory, or used as a % of
        its limit. Creates a NEW panel over the same time range marked "reframed from <panel>"; the original
        stays. Propose reframings to the user in your reply; call this when it helps the question."""
        try:
            res = await service.reframe(panel, index, "claude")
        except (NotFound, ValueError, SourceError) as e:
            raise ToolError(str(e)) from e
        return _dump(
            {
                "panel": res.panel.id,
                "url": f"{ui_url}/#/panel/{res.panel.id}",
                "reframed_from": panel,
                "warnings": [i.message for i in res.issues],
            }
        )

    ws = service.ws

    def _t(text: str | None) -> int | None:
        return None if text is None else parse_time(str(text), service.clock())

    @mcp.tool()
    async def suggest_y_view(
        panel: str,
        mode: str,
        label: str,
        reason: str,
        lo: float | None = None,
        hi: float | None = None,
        replace: bool = False,
        baseline: str | None = None,
    ) -> str:
        """Offer the user another y-axis view of a time-series or fleet panel; the USER picks.
        (A fleet panel takes zero, data, reference, natural-bounds and band; not log, indexed or meaningful.)

        mode: zero (include 0), data (fit the data), meaningful (percentile panels: range
        only over buckets with n >= n_min, so a faded low-n outlier does not squash the
        real values), band (lo..hi), typical (the metric's observed p1-p99 from catalog_scan:
        descriptive, spikes beyond it are counted, never hidden), log (all values must be > 0; good when data spans
        more than 2 decades), indexed (needs baseline: window = each series' own mean over the time range,
        previous/week = the same series point by point; log axis, 1 centred; THE way to compare
        series of different scales, never a dual axis). label: short button text (<=40 chars). reason: ONE line
        shown to the user, e.g. "one n=13 bucket at 34 s squashes the 0.4-1.3 s p95".
        At most 4 per panel; same label replaces; replace=true clears the others.
        Never a second y-axis: for p50 vs p99 on separate scales, show two panels.
        Returns {panel, view, warnings}. The user's choice arrives as ambient
        panel.y_view_selected."""
        try:
            v = YView(
                mode=mode,
                label=label,
                reason=reason or None,
                lo=lo,
                hi=hi,  # type: ignore[arg-type]
                baseline=baseline,
                author="claude",  # type: ignore[arg-type]
            )
            ref = (
                await service.ensure_reference(panel, baseline, "claude")
                if mode == "indexed" and baseline in ("previous", "week")
                else None
            )
            saved, warnings = ws.suggest_y_view(panel, v, "claude", replace=replace, reference=ref)
            return _dump({"panel": panel, "view": saved.id, "warnings": warnings})
        except (ValidationError, NotFound, ValueError) as e:
            raise _fail(e) from e

    @mcp.tool()
    async def set_overlays(
        panel: str, normal: bool | None = None, limit: bool | None = None, ghost: bool | None = None
    ) -> str:
        """Switch reference layers on a time-series panel; only the flags you pass change.
        normal: seasonal normal band (the same hour of the week, from the operating profile);
        limit: the physical limit line (a bounded_by metric, e.g. filesystem size);
        ghost: the same time range last week as a faint dashed line (fetches it, so off by default).
        normal and limit default to on where the data exists; the UI says why one is unavailable."""
        try:
            return _dump(await service.set_overlays(panel, "claude", normal, limit, ghost))
        except SourceError as e:
            raise _source_error(e) from e
        except (NotFound, ValueError) as e:
            raise _fail(e) from e

    @mcp.tool()
    async def show_marginal(
        panel: str, reference: str = "previous", reason: str = "", off: bool = False
    ) -> str:
        """Show a marginal histogram beside a time-series panel: the current time range's value
        distribution vs a reference time range, on the panel's own y scale. reference: previous
        (time range of equal length just before), week (same time range 7 days earlier) or profile (the
        metric's operating profile: its hourly values in the same hours of the day/week the
        time range covers; both sides are HOURLY means; plain series with a step of at most 1h,
        never percentiles or histograms).
        Histogram-backed panels compare OBSERVATIONS (requests); plain series compare
        per-step values (scrape samples, NOT requests): say which when you cite it, with n.
        reason: one line shown to the user. off=true hides it. Returns {basis, what, n, datasets}:
        the reference datasets are normal handles (e.g. fraction_over on the distribution one)."""
        try:
            out = await service.set_marginal(
                panel, None if off else reference, "claude", reason=reason or None
            )
            return _dump(out)
        except (ValidationError, NotFound, ValueError, SourceError) as e:
            raise _fail(e) from e

    @mcp.tool()
    def annotate(
        kind: str,
        panel: str | None = None,
        at: str | None = None,
        until: str | None = None,
        value: float | None = None,
        value_hi: float | None = None,
        label: str = "",
    ) -> str:
        """Mark the workspace. kind: event (needs at), region (at+until), threshold (value),
        band (value+value_hi), note. at/until: now, now-5m, epoch ms, or ISO-8601 with tz.
        Returns {annotation}."""
        try:
            data = AnnotationIn.model_validate(
                {
                    "kind": kind,
                    "panel": panel,
                    "t_start_ms": _t(at),
                    "t_end_ms": _t(until),
                    "value": value,
                    "value_hi": value_hi,
                    "label": label,
                }
            )
            return _dump({"annotation": ws.annotate(data, "claude").model_dump()})
        except (ValidationError, NotFound, ValueError) as e:
            raise _fail(e) from e

    @mcp.tool()
    async def run_code(
        code: str, inputs: list[str] | None = None, timeout_s: float | None = None
    ) -> str:
        """Tier-2: run Python in the workspace's persistent IPython kernel, for questions no
        tier-1 tool answers (custom statistics, bootstrap CIs, models). Try query/analyze/
        spectrum/fleet/fraction_over/... first.

        inputs: the dataset handles the code reads, declared up front (e.g. ["d3", "d5"]);
        they are exported before the run, the code cannot query sources. Not percentile
        datasets (query the histogram). timeout_s: wall clock (default 120 s); a timeout or
        crash fails the node and may restart the kernel (variables are lost, datasets kept).
        In the code (tn, pl = polars and np = numpy are already bound; importing is fine):
            import telemetry_nerd.tn as tn
            df = tn.dataset("d3")  # polars: ts_ms, series_id, avg, min, max, count
            tn.meta("d3")          # unit, step_ms, representation, caveats, ...
            tn.put(out, like="d3", uncertainty={"method": "bootstrap", "level": 0.95})
              # out has lo/hi columns; or exact=True for exact counts. Without either the
              # output is tagged no_uncertainty: citable, flagged 'uncertainty unknown'.
              # Inputs with lo/hi: carry their error in and add "propagation": "<how>" to
              # uncertainty, else the output is uncertainty_not_propagated (a lower bound).
            tn.put_fit("linear", {"slope": {"value": b, "interval": [lo, hi]}},
                       method="OLS", diagnostics={...})
        Each run is a code node (c1, c2, ...) with lineage to its inputs and outputs.
        Returns {code_node, status (ok|failed), exec_status, duration_s, stdout (short),
        result, outputs: [{dataset, name, representation, rows, unit, uncertainty,
        uncertainty_status?, caveats, series summary or fit params}], issues (outputs not ingested),
        error + traceback on failure, restarted + note when kernel state was lost}. Never
        bulk data: print small aggregates only; `show` an output to draw it."""
        try:
            node = await service.code.run(code, inputs or [], timeout_s=timeout_s)
        except (NotFound, ValueError, CodeDisabled) as e:
            raise _fail(e) from e
        return _dump(service.code.result(node))

    @mcp.tool()
    async def rerun_code(code_node: str, timeout_s: float | None = None) -> str:
        """Run a code node's stored code again on the same inputs (e.g. after a kernel
        restart or a failed run). Creates a NEW node with rerun_of=<code_node>; finished
        nodes are never changed. To change the code, call run_code. Returns what run_code
        returns."""
        try:
            node = await service.code.rerun(code_node, timeout_s=timeout_s)
        except (NotFound, ValueError, CodeDisabled) as e:
            raise _fail(e) from e
        return _dump(service.code.result(node))

    @mcp.tool()
    def code_get(code_node: str, part: str = "all", offset: int = 0, limit: int = 4000) -> str:
        """Read a code node's stored text, one bounded page at a time: the full code, stdout,
        stderr or traceback that run_code only returns shortened. part: all (labelled sections)
        | stdout | stderr | traceback | code. offset/limit in characters (limit at most 20000);
        follow next_offset while it is not null. Text only, never dataset rows: use `show` or
        `query` for data. Returns {code_node, part, status, offset, total_chars, text,
        next_offset, note?}."""
        try:
            return _dump(service.code.text_page(code_node, part, offset, limit))
        except (NotFound, ValueError) as e:
            raise _fail(e) from e

    @mcp.tool()
    def hypothesis_create(statement: str, scope: dict[str, Any] | str | None = None) -> str:
        """Record a hypothesis to test, naming its concrete subject. scope (optional): where it
        applies, in a finding scope's shape: {selector, start, end, source?, step?,
        aggregation?} (`range`: [start, end] works too), e.g. {"selector":
        "traces_span_metrics_calls_total{service_name=\"payment\"}", "start": "now-1h",
        "end": "now"}; its selector's services and metrics count as the hypothesis' subjects.
        Prose ("payment, checkout; 14:30-15:00Z") is stored as text, shown, not checked.
        Returns {hypothesis: id, scope?, read_as?}."""
        try:
            sc, notes = hypothesis_scope(scope, _t)
            data = HypothesisScope.model_validate(sc) if sc is not None else None
            h = ws.hypothesis_create(statement, "claude", data)
        except ShapeError as e:
            raise ToolError(f"invalid arguments: {e}") from e
        except (ValidationError, ValueError) as e:
            raise _fail(e) from e
        out: dict[str, Any] = {"hypothesis": h.id}
        if h.scope is not None:
            out["scope"] = h.scope.model_dump(exclude_none=True)
        if notes:
            out["read_as"] = notes
        return _dump(out)

    @mcp.tool()
    def hypothesis_update(
        status: str | None = None,
        hypothesis: str | None = None,
        note: str | None = None,
        alternatives_considered: str | None = None,
        id: str | None = None,
        reason: str | None = None,
        hidden: bool | None = None,
        hidden_reason: str | None = None,
    ) -> str:
        """Change a hypothesis status (proposed, supported, refuted, inconclusive), e.g.
        hypothesis_update(hypothesis="h1", status="refuted") (`id` works for `hypothesis`).
        Refuted ones stay visible. `supported` is refused unless the statement names a concrete
        subject (a service/resource label value or metric the workspace has queried), a finding
        with stance=for backs it, and an alternative was considered: another hypothesis refuted
        or inconclusive, or alternatives_considered (which alternatives, how ruled out).
        `refuted` is refused unless a finding with stance=against is linked to it, or `reason`
        says what rules it out (cite the findings); `inconclusive` needs a linked finding, a
        reason or a note saying why. The reason is stored with the status.

        `hidden` (fygk) puts a hypothesis aside without a verdict, e.g. hypothesis_update(
        hypothesis="h3", hidden=true, hidden_reason="duplicate of h1") — a duplicate, off-topic,
        superseded or decoy hypothesis you want off the active board without claiming it is
        true or false (that is `status`). Reversible (hidden=false brings it back); it is never
        deleted, evidence links to it keep working, and workspace_get's hypothesis count still
        includes it (just not in the active list, unless a finding still cites it). `status` and
        `hidden` may be given together or separately; `hidden_reason` is never read as (and
        never satisfies) the evidence-based `reason` a refuted/inconclusive status needs, and
        vice versa — a housekeeping note is not evidence (principle 13).
        Returns {hypothesis, status?, hidden?}."""
        if hypothesis is not None and id is not None and hypothesis != id:
            raise ToolError(f"hypothesis={hypothesis!r} and id={id!r} disagree: give one, e.g. "
                            'hypothesis_update(hypothesis="h1", status="refuted")')  # fmt: skip
        hypothesis = hypothesis if hypothesis is not None else id
        if hypothesis is None:
            raise ToolError('hypothesis is required: e.g. hypothesis_update(hypothesis="h1", '
                            'status="refuted")')  # fmt: skip
        if status is None and hidden is None:
            raise ToolError('give status or hidden: e.g. hypothesis_update(hypothesis="h1", '
                            'status="refuted") or hypothesis_update(hypothesis="h1", hidden=true)')  # fmt: skip
        out: dict[str, Any] = {"hypothesis": hypothesis}
        try:
            if status is not None:
                st = TypeAdapter(HypothesisStatus).validate_python(status.strip().lower())
                h = ws.hypothesis_update(
                    hypothesis, st, "claude", note=note,
                    alternatives_considered=alternatives_considered, reason=reason,
                )  # fmt: skip
                out["status"] = h.status
            if hidden is not None:
                h = ws.hypothesis_hide(hypothesis, hidden, "claude", reason=hidden_reason)
                out["hidden"] = h.hidden
            return _dump(out)
        except ValidationError as e:
            raise ToolError(f"status must be one of proposed, supported, refuted, inconclusive; "
                            f"got {status!r}") from e  # fmt: skip
        except (NotFound, ValueError) as e:
            raise _fail(e) from e

    def _evidence_context() -> EvidenceContext:
        def panels_of(did: str) -> list[str]:
            return [p.id for p in ws.workspace.list_panels() if p.dataset_ids[:1] == [did]]

        def statistics_of(did: str) -> list[dict]:
            return citable_statistics(service.datasets.labelled_statistics([did]), [], 3)

        return EvidenceContext(panels_of, service.datasets.statistic_methods, statistics_of)

    @mcp.tool()
    def finding_create(
        claim: str,
        evidence: list[dict[str, Any] | str] | dict[str, Any] | str,
        scope: dict[str, Any] | str | None = None,
        caveats: list[str] | None = None,
        hypothesis: str | None = None,
        stance: str | None = None,
        answers_panel: str | None = None,
        scope_note: str | None = None,
        hypotheses: list[dict[str, Any] | str] | dict[str, Any] | str | None = None,
        source: str | None = None,
        selector: str | None = None,
        start: str | int | None = None,
        end: str | int | None = None,
        step: str | None = None,
        aggregation: str | None = None,
        range: list[str | int] | None = None,
    ) -> str:
        """Record a scoped, evidenced claim. scope: {source, selector, start, end, step,
        aggregation?, baseline_start?, baseline_end?} (times: now-2h, epoch ms, ISO; `range`:
        [start, end] works too; the fields may also be passed flat, beside claim). aggregation
        defaults to "as written in scope.selector".
        evidence: [{kind: panel, panel} | {kind: annotation, annotation} |
        {kind: statistic, dataset, name, value, method, interval: [lo, hi] | exact: true |
        uncertainty_unknown: true, source?}]. Short forms: "p1", "a1", or "d3" (read as the one
        panel drawing d3); {"panel": "p1", "annotation": "a1"} (one item per ref);
        {kind: annotation, id: "a1"}. A dataset alone is not evidence: draw it (show) or cite a
        statistic. Pass an op's evidence statistic as is: its
        `source` (common_cause | special_cause | measurement_system | undetermined) says what
        the variation is attributed to; never relabel it (a source contradicting the op's
        label is refused; one no op gave is flagged source_unverified). hypotheses: the hypotheses this
        finding bears on, one stance each: [{"id": "h1", "stance": "for"}, {"id": "h2",
        "stance": "against"}] (one observation may back one cause and rule out another);
        hypothesis + stance (for|against) is the one-link form. Coverage of the time range is checked per evidence series that
        scope.selector's label matchers name (e.g. up{pod="x"} for a claim about one pod);
        write it as PromQL (sum by (code) (rate(x{svc="a"}[5m])) or x{svc="a"}).
        Every entity the claim names (service, pod, job... values) must be covered by the
        cited evidence: refused otherwise, with the datasets that hold them; or pass scope_note
        saying why the claim reaches beyond its evidence (flagged beyond_evidence).
        Returns {finding, url, scope: {status covered|beyond_evidence|undetermined, ...},
        uncertainty?: [{evidence, flag, message}], sources?, source_flags?, citable_statistics?,
        cite?, read_as?}: what the server derived (scope against evidence, uncertainty unknown /
        lower bound, variation sources: taken from the op that emitted a statistic, or
        undetermined; citable_statistics: labelled op statistics of the cited panels' datasets,
        to cite as is; read_as: how a short form was read); report them with the finding."""
        args = {"claim": claim, "evidence": evidence, "scope": scope, "caveats": caveats,
                "hypothesis": hypothesis, "stance": stance, "hypotheses": hypotheses,
                "answers_panel": answers_panel,
                "scope_note": scope_note, "source": source, "selector": selector,
                "start": start, "end": end, "step": step, "aggregation": aggregation,
                "range": range}  # fmt: skip
        try:
            data, read_as = finding_in(args, _evidence_context(), _t)
            f = ws.finding_create(data, "claude")
        except ShapeError as e:
            raise ToolError(f"invalid arguments: {e}") from e
        except (ValidationError, NotFound, ValueError) as e:
            raise _fail(e) from e
        out = _finding_result(f, ui_url)
        citable, cite = ws.citable_statistics(f)
        if citable:  # hk2r: the ops' labelled statistics behind the cited panels
            out["citable_statistics"] = citable
            out["cite"] = cite
        if hint := cause_hint(f, ws.objects.list_hypotheses()):
            out["hint"] = hint
        if read_as:
            out["read_as"] = read_as
        return _dump(out)

    @mcp.tool()
    def gap_create(
        missing_signal: str | None = None,
        needed_for: str | None = None,
        suggestion: dict[str, Any] | str | None = None,
        description: str | None = None,
    ) -> str:
        """Record a signal you need but cannot query (so the user can instrument it). Example:
        gap_create(missing_signal="queue depth of the payments worker",
        needed_for="saturation of the payments service",
        suggestion={"name": "payments_queue_depth", "type": "gauge", "labels": ["worker"]}).
        suggestion.type is counter|gauge|histogram|summary; labels is optional. suggestion is
        optional too: leave it out when no metric can be named (missing history, a service
        that emits nothing); never invent one. Forgiving: a string suggestion
        ("payments_queue_depth gauge") is parsed, `description` fills a missing
        missing_signal/needed_for. Returns {gap}."""
        try:
            data = GapIn.model_validate(
                _gap_args(missing_signal, needed_for, suggestion, description)
            )
            return _dump({"gap": ws.gap_create(data, "claude").id})
        except (ValidationError, ValueError) as e:
            detail = _issues(e) if isinstance(e, ValidationError) else str(e)
            raise ToolError(f"gap_create: {detail}. Expected: {GAP_EXAMPLE}") from e

    @mcp.tool()
    def reply(thread: str, text: str) -> str:
        """Answer a user thread. Returns {message: id}."""
        try:
            # channel delivery is global: the question may come from a workspace the user has
            # since switched away from; the answer joins its thread's own workspace
            with service.active.using(ws.objects.owner(thread) or service.active()):
                return _dump({"message": ws.post_message(thread, text, "claude").id})
        except (ValidationError, NotFound, ValueError) as e:
            raise _fail(e) from e

    @mcp.tool()
    def highlight(object: str, note: str | None = None, seconds: int = 300) -> str:
        """Draw the user's attention to a panel/annotation/hypothesis/finding/gap id
        (e.g. when you say 'look at p5'). Shown as an accent plus optional short note;
        expires after `seconds` (default 300; 0 = until cleared)."""
        try:
            if seconds < 0:
                raise ValueError("seconds must be >= 0")
            ws.highlight(object, "claude", note=note, ttl_ms=seconds * 1000 or None)
            return _dump({"highlighted": object})
        except (NotFound, ValueError) as e:
            raise _fail(e) from e

    @mcp.tool()
    def unhighlight(object: str) -> str:
        """Clear a highlight you set earlier."""
        try:
            ws.unhighlight(object, "claude")
            return _dump({"cleared": object})
        except (NotFound, ValueError) as e:
            raise _fail(e) from e

    @mcp.tool()
    def workspace_get() -> str:
        """Compact workspace brief: workspace {id, title, question}, panels, hypotheses, findings,
        open_threads, last_seq."""
        return _dump(ws.brief())

    @mcp.tool()
    def workspace_activity(since: int | None = None) -> str:
        """What happened since sequence number `since` (default: the most recent events)."""
        return _dump(ws.activity(since))

    @mcp.tool()
    async def workspace_create(title: str, question: str | None = None) -> str:
        """Start a new workspace for a new, unrelated question; it becomes the active one.

        Returns the new workspace, the previous id and its url. Later calls act on it."""
        try:
            out = await service.workspaces.create(title, question, "claude")
        except (ValueError, NotFound) as e:
            raise _fail(e) from e
        return _dump({**out, "workspace": _capped_workspace(out["workspace"]), "url": ui_url})

    @mcp.tool()
    def workspace_list(include_archived: bool = False) -> str:
        """Workspaces, newest activity first (20 max, `more` counts the rest), with counts.

        A count key absent from a row means zero."""
        out = service.workspaces.list(include_archived, 20)
        head = {"active": out["active"], "url": ui_url}
        total = len(out["workspaces"]) + out["more"]
        # `more` counts every row not shown, the ones cut for size too
        rows, _ = _trim_to_fit(
            [_workspace_row(w) for w in out["workspaces"]],
            lambda kept: {**head, "workspaces": kept, "more": total - len(kept)},
        )
        return _dump({**head, "workspaces": rows, "more": total - len(rows)})

    @mcp.tool()
    async def workspace_switch(id: str) -> str:
        """Make workspace `id` the active one (calls after this one act on it).

        Returns counts, open threads and source status; call workspace_get for the brief.
        Source statuses: connected, restored (re-attached), conflict (the name is taken by
        another spec), failed (with the error), disconnected (switching to the already-active
        workspace reports state only, no reconnect)."""
        try:
            out = await service.workspaces.switch(id, "claude")
        except (ValueError, NotFound) as e:
            raise _fail(e) from e
        out = {**out, "url": ui_url, "hint": "workspace_get for the brief"}
        threads, cut = _trim_to_fit(  # the brief has the rest
            [{**t, "last": t["last"][:60]} for t in out["open_threads"]],
            lambda kept: {**out, "open_threads": kept},
        )
        return _dump(
            {**out, "open_threads": threads, **({"more_open_threads": cut} if cut else {})}
        )

    @mcp.tool()
    async def workspace_update(
        id: str,
        title: str | None = None,
        question: str | None = None,
        archived: bool | None = None,
    ) -> str:
        """Rename workspace `id`, change its question ("" clears it), or (un)archive it (not
        the active one). Titles are capped at 120 characters, questions at 500."""
        try:
            info = service.workspaces.update(
                id, title=title, question=question, archived=archived, actor="claude"
            )
        except (ValueError, NotFound) as e:
            raise _fail(e) from e
        return _dump({"workspace": _capped_workspace(info.to_dict()), "url": ui_url})

    _forbid_unknown_arguments(mcp)
    return mcp


def _unknown_check(name: str, model: type) -> Any:
    """A before-validator naming the arguments a tool does not take, and the ones it does."""
    fields = model.model_fields  # type: ignore[attr-defined]
    allowed = [f.alias or k for k, f in fields.items()]

    def check(cls, data: Any) -> Any:
        if isinstance(data, dict):
            unknown = [k for k in data if k not in allowed]
            if unknown:
                raise ValueError(
                    f"{name} takes no argument {', '.join(map(repr, unknown))}; its arguments "
                    f"are {', '.join(allowed)}"
                )
        return data

    return classmethod(check)


def _forbid_unknown_arguments(mcp: MCPServer) -> None:
    """Reject arguments a tool does not take (e.g. source_connect(resolution_ms=...) instead of
    resolution): the SDK's argument models ignore them, so a misspelt or invented argument would
    be dropped silently and its default used. The schema says so too (additionalProperties)."""
    for tool in mcp._tool_manager.list_tools():
        base = tool.fn_metadata.arg_model
        strict = type(
            base.__name__,
            (base,),
            {
                "model_config": ConfigDict(**base.model_config, extra="forbid"),
                "check_unknown_arguments": model_validator(mode="before")(
                    _unknown_check(tool.name, base)
                ),
            },
        )
        tool.fn_metadata.arg_model = strict
        tool.parameters = {**tool.parameters, "additionalProperties": False}
