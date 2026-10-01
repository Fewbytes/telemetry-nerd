"""MCP tools. Results are compact JSON: handles, summaries, caveats, links. Never raw series."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import TypeAdapter, ValidationError

from telemetry_nerd.core.service import ChartRejected, TelemetryService
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.jsonsafe import dumps
from telemetry_nerd.model.time import parse_duration, parse_time
from telemetry_nerd.sources.base import SourceError
from telemetry_nerd.sources.spec import SourceSpec
from telemetry_nerd.workspace.models import AnnotationIn, FindingIn, GapIn, HypothesisStatus

INSTRUCTIONS = """\
Telemetry Nerd: an evidence-first telemetry workspace shared with the user's browser.
- `query` fetches a PromQL/MetricsQL expression as min/max/avg/count buckets and returns a
  dataset handle plus a compact summary. Write native PromQL/MetricsQL.
- Sources: `source_list` shows what is connected. To look at other data, `source_connect`
  it (Prometheus, VictoriaMetrics, Thanos, Mimir, or a Grafana datasource proxy URL);
  no restart is needed. Never ask for or pass a raw token: use auth_file/auth_env.
  `query(source=...)` picks the source.
- `show` draws a dataset as a panel. Every panel must answer an explicit question; phrase
  it as the question the graph answers. Share the returned URL with the user.
- Report caveats from summaries (gaps, settling, fake_resolution) when you describe data.
- NEVER present a percentile without its sample count. A quantile over n samples is
  meaningless unless n >= ~10/(1-q) per bucket: p50 ~20, p95 ~200, p99 ~1000, p99.9 ~10000.
  Always query the count behind it (histogram_count(increase(...[window])) or
  increase(..._count[window])) and say where the percentile is not meaningful.
- NEVER AGGREGATE PERCENTILES — not across series (sum/avg/max of histogram_quantile or of
  summary {quantile=...} series) and not across time (avg_over_time, rollups, downsampling
  of a percentile series). Aggregate the underlying histogram first, then take the quantile
  once: histogram_quantile(q, sum by (...) (rate(x[w]))). Likewise divide sums by counts
  only after aggregating both: histogram_sum(sum(rate(x[w]))) / histogram_count(sum(...)).
- Never look at latency alone: show it with throughput, and with concurrency when relevant
  (Little's law: mean concurrency L = throughput λ x mean latency W; W from
  histogram_sum/histogram_count, not from a percentile).
- Scope every claim: source, selector, time range, step. Do not generalize beyond it.
- `workspace_get` shows open threads (user questions awaiting you), hypotheses, findings.
  `reply` answers a thread. `hypothesis_create`/`hypothesis_update` track explanations.
- `finding_create` needs a scope and evidence; a statistic needs an interval unless exact.
- `gap_create` records a signal you wish existed. `annotate` marks events/regions/thresholds.
- `workspace_activity` lists what the user did since a sequence number.
"""


def _dump(obj: dict) -> str:
    return dumps(obj, separators=(",", ":"))


def _issues(e: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(x) for x in err['loc']) or '<root>'}: {err['msg']}" for err in e.errors()
    )


def _fail(e: Exception) -> ToolError:
    """Message plus a hint so Claude can fix the call."""
    if isinstance(e, ValidationError):
        return ToolError(f"invalid arguments: {_issues(e)} (hint: fix the listed fields)")
    if isinstance(e, NotFound):
        return ToolError(f"{e} (hint: check the id against workspace_get)")
    return ToolError(f"{e} (hint: check the arguments)")


def build_mcp(service: TelemetryService, ui_url: str) -> MCPServer:
    mcp = MCPServer("telemetry-nerd", instructions=INSTRUCTIONS)

    @mcp.tool()
    async def query(
        expr: str,
        start: str = "now-1h",
        end: str = "now",
        step: str = "auto",
        source: str = "default",
    ) -> str:
        """Fetch a PromQL/MetricsQL expression as a dataset of min/max/avg/count buckets.

        source: a name from source_list (default "default").
        start/end: `now`, `now-<dur>` (e.g. now-6h), epoch ms, or ISO-8601 with timezone.
        step: `auto` (~600 buckets) or a duration like 30s, 1m, 5m.
        Returns {dataset, summary}. The summary is compact; raw series stay on the server.
        """
        try:
            # pi-lens-ignore: python-sql-injection
            return _dump(await service.query(expr, start, end, step, source))
        except SourceError as e:
            raise ToolError(f"{e} (hint: {e.hint})" if e.hint else str(e)) from e
        except ValueError as e:
            raise ToolError(str(e)) from e

    def _source_error(e: SourceError) -> ToolError:
        return ToolError(f"{e} (hint: {e.hint})" if e.hint else str(e))

    @mcp.tool()
    async def source_connect(
        name: str,
        url: str,
        flavor: str = "prometheus",
        resolution: str = "15s",
        auth_env: str | None = None,
        auth_file: str | None = None,
        auth_scheme: str = "bearer",
        max_concurrency: int = 4,
        min_interval: str = "0s",
        timeout: str = "30s",
        replace: bool = False,
    ) -> str:
        """Connect a Prometheus-compatible source at runtime (no daemon restart).

        url: API base without /api/v1, e.g. http://prometheus:9090, a VictoriaMetrics
        /select/0/prometheus path, or a Grafana datasource proxy
        https://<grafana>/api/datasources/proxy/uid/<uid>.
        flavor: "victoriametrics" (MetricsQL rollup) or "prometheus" (also works on VM,
        Thanos, Mimir).
        resolution: the source's scrape interval, e.g. 15s, 30s, 60s.
        Secrets: NEVER pass a token. Ask the user to put it in a file (auth_file, absolute
        path; picked up immediately) or an env var of the daemon (auth_env, the variable
        NAME; needs a daemon restart if set later). auth_scheme: bearer | basic ("user:pass").
        Politeness for shared/public servers: lower max_concurrency, set min_interval
        (e.g. 500ms), raise timeout (e.g. 60s).
        The source is probed before it is saved; it persists across daemon restarts.
        Returns {source, status}.
        """
        try:
            auth = None
            if auth_env is not None or auth_file is not None:
                auth = {"env": auth_env, "file": auth_file, "scheme": auth_scheme}
            spec = SourceSpec.model_validate(
                {
                    "name": name,
                    "url": url,
                    "flavor": flavor,
                    "resolution_ms": parse_duration(resolution),
                    "auth": auth,
                    "politeness": {
                        "max_concurrency": max_concurrency,
                        "min_interval_ms": parse_duration(min_interval),
                        "timeout_s": parse_duration(timeout) / 1000,
                    },
                }
            )
            return _dump(await service.source_connect(spec, replace=replace))
        except ValidationError as e:
            raise _fail(e) from e
        except SourceError as e:
            raise _source_error(e) from e
        except ValueError as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    def source_list() -> str:
        """Connected sources: name, url, flavor, resolution, auth reference (never the
        secret), politeness, whether it is live or broken (e.g. its secret is missing)."""
        return _dump({"sources": service.source_list()})

    @mcp.tool()
    async def source_status(name: str) -> str:
        """Probe a source now: {reachable, latency_ms, application?, version?} or
        {reachable: false, error, hint}."""
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
    def show(dataset: str, question: str, unit: str | None = None) -> str:
        """Draw a dataset as a panel (mean line + min/max envelope) in the shared workspace.

        question is REQUIRED: the explicit question this graph answers, e.g.
        "Did checkout latency rise after the 14:00 deploy?".
        unit: optional y-axis unit (e.g. "s", "B", "ms", "items", "req/s"). Pass it
        when you know the unit from context the metric name doesn't reveal — you read
        the emitting code, or you know the generating tool's conventions. Your unit
        overrides suffix inference and is persisted with provenance ("provided by
        claude"), so only pass a unit you can actually vouch for.
        Returns {panel, url, warnings}.
        """
        try:
            res = service.show(dataset, question, unit=unit)
        except ChartRejected as e:
            raise ToolError(f"chart rejected: {e}") from e
        except (NotFound, ValueError) as e:
            raise ToolError(str(e)) from e
        return _dump(
            {
                "panel": res.panel.id,
                "url": f"{ui_url}/#/panel/{res.panel.id}",
                "warnings": [i.message for i in res.issues],
            }
        )

    ws = service.ws

    def _t(text: str | None) -> int | None:
        return None if text is None else parse_time(str(text), service.clock())

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
    def hypothesis_create(statement: str) -> str:
        """Record a hypothesis to test. Returns {hypothesis: id}."""
        try:
            return _dump({"hypothesis": ws.hypothesis_create(statement, "claude").id})
        except (ValidationError, ValueError) as e:
            raise _fail(e) from e

    @mcp.tool()
    def hypothesis_update(hypothesis: str, status: str, note: str | None = None) -> str:
        """Change a hypothesis status (open, supported, refuted, ...). Refuted ones stay visible.
        Returns {hypothesis, status}."""
        try:
            st = TypeAdapter(HypothesisStatus).validate_python(status)
            h = ws.hypothesis_update(hypothesis, st, "claude", note=note)
            return _dump({"hypothesis": h.id, "status": h.status})
        except (ValidationError, NotFound, ValueError) as e:
            raise _fail(e) from e

    @mcp.tool()
    def finding_create(
        claim: str,
        scope: dict[str, Any],
        evidence: list[dict[str, Any]],
        caveats: list[str] | None = None,
        hypothesis: str | None = None,
        stance: str | None = None,
        answers_panel: str | None = None,
    ) -> str:
        """Record a scoped, evidenced claim. scope: {source, selector, start, end, step,
        aggregation, baseline_start?, baseline_end?} (times: now-2h, epoch ms, ISO).
        evidence: [{kind: panel, panel} | {kind: annotation, annotation} |
        {kind: statistic, dataset, name, value, method, interval: [lo, hi] | exact: true}].
        hypothesis and stance (for|against) go together. Returns {finding, url}."""
        try:
            sc = dict(scope)
            try:
                start, end = sc.pop("start"), sc.pop("end")
            except KeyError as k:
                raise ValueError(f"scope.{k.args[0]}: field required") from k
            bs, be = sc.pop("baseline_start", None), sc.pop("baseline_end", None)
            sc["time_range"] = {"start_ms": _t(start), "end_ms": _t(end)}
            if bs is not None or be is not None:
                sc["baseline_range"] = {"start_ms": _t(bs), "end_ms": _t(be)}
            data = FindingIn.model_validate(
                {
                    "claim": claim,
                    "scope": sc,
                    "evidence": evidence,
                    "caveats": caveats or [],
                    "hypothesis": hypothesis,
                    "stance": stance,
                    "answers_panel": answers_panel,
                }
            )
            f = ws.finding_create(data, "claude")
        except (ValidationError, NotFound, ValueError) as e:
            raise _fail(e) from e
        return _dump({"finding": f.id, "url": f"{ui_url}/#/finding/{f.id}"})

    @mcp.tool()
    def gap_create(missing_signal: str, needed_for: str, suggestion: dict[str, Any]) -> str:
        """Record a signal you need but cannot query. suggestion: {name, type
        (counter|gauge|histogram|summary), labels?}. Returns {gap}."""
        try:
            data = GapIn.model_validate(
                {
                    "missing_signal": missing_signal,
                    "needed_for": needed_for,
                    "suggestion": suggestion,
                }
            )
            return _dump({"gap": ws.gap_create(data, "claude").id})
        except (ValidationError, ValueError) as e:
            raise _fail(e) from e

    @mcp.tool()
    def reply(thread: str, text: str) -> str:
        """Answer a user thread. Returns {message: id}."""
        try:
            return _dump({"message": ws.post_message(thread, text, "claude").id})
        except (ValidationError, NotFound, ValueError) as e:
            raise _fail(e) from e

    @mcp.tool()
    def workspace_get() -> str:
        """Compact workspace brief: panels, hypotheses, findings, open_threads, last_seq."""
        return _dump(ws.brief())

    @mcp.tool()
    def workspace_activity(since: int | None = None) -> str:
        """What happened since sequence number `since` (default: the most recent events)."""
        return _dump(ws.activity(since))

    return mcp
