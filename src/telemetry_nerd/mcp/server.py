"""MCP tools. Results are compact JSON: handles, summaries, caveats, links. Never raw series."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import TypeAdapter, ValidationError

from telemetry_nerd.charts.spec import Window
from telemetry_nerd.charts.yview import YView
from telemetry_nerd.core.service import ChartRejected, TelemetryService
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.jsonsafe import dumps
from telemetry_nerd.model.time import parse_duration, parse_time
from telemetry_nerd.sources.base import SourceError
from telemetry_nerd.sources.public import PUBLIC_SOURCES
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
  A plain counter selector is drawn as its rate (the answer's `auto` says so); `raw=true` draws
  the running total. A metric the catalog bounds on both sides gets its natural axis.
- When one outlier or a faded low-n bucket squashes a panel's y range, `suggest_y_view` (e.g.
  mode=meaningful) with a one-line reason; do not re-query to hide data.
- To ask "is now different from before?" about a time panel, `show_marginal(panel,
  reference=previous|week)`; cite n for both windows and whether it is requests or per-step samples.
- Periodicity (cron, GC, retries, scrape artefacts, diurnal): `spectrum(dataset)`. Report only
  `significant` peaks with their interval, cite `evidence`; say which periods cannot be seen
  (shorter than `limits.shortest` = 2 x step, longer than range/2). red_noise: long periods look
  more significant than they are.
- "Is this metric stable / drifting / shifted / periodic / in control?": `analyze(dataset)`, then
  `show(dataset, question, mark="spc")`. State the verdict with its reasons and numbers, the
  baseline window the control limits came from, n_eff, and caveats; cite `evidence`.
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
- Histograms first: for latency, `query_distribution` the histogram (the `_bucket` metric or
  native histogram, no functions) and `show` it (heatmap). Use `show(mark="histogram",
  windows=[spike, baseline])` to compare windows and cite n per window. Quantiles only on
  request; distribution summaries give them as the BUCKET holding them, never a value.
  For "p95 over time" when a histogram exists, prefer `query_distribution` +
  `show(mark="percentiles", quantiles=[0.5, 0.95])` over `histogram_quantile`: each step shows
  the bucket holding q, only where n is enough. For tails use `show(mark="ccdf", windows=[...])`.
- "What fraction was slower than X?" is `fraction_over` on a distribution dataset: exact at
  bucket edges, bounded inside a bucket; cite its `evidence` statistic.
- Write $__rate_interval as the rate window for quantiles so each value covers one display step.
- Never look at latency alone: show it with throughput, and with concurrency when relevant
  (Little's law: mean concurrency L = throughput λ x mean latency W; W from
  histogram_sum/histogram_count, not from a percentile).
- Scope every claim: source, selector, time range, step. Do not generalize beyond it.
- `workspace_get` shows open threads (user questions awaiting you), hypotheses, findings.
  `reply` answers a thread. `hypothesis_create`/`hypothesis_update` track explanations.
- A thread reply cannot be evidence. When data or a user reply contradicts a hypothesis,
  also call `finding_create(hypothesis=<id>, stance="against", ...)` and, if the verdict
  changes, `hypothesis_update`: that is how the contradiction surfaces on the hypothesis.
- `finding_create` needs a scope and evidence; a statistic needs an interval unless exact.
- Summaries carry `coverage` per series (share of expected samples, longest gap) and `unknown_spans`; missing data is evidence too — scope claims around it.
- `gap_create` records a signal you wish existed. `annotate` marks events/regions/thresholds.
- When you tell the user to look at an object ("see p5"), also call `highlight(object, note?)`
  so it is accented in their UI; `unhighlight` clears it. Mention ids like p5/f2 in text: they
  become hoverable chips.
- Before charting unfamiliar metrics, check what the catalog knows: `catalog_search`/`catalog_get`
  (run `source_learn` once per source). When you work out what a metric is (unit, type, role,
  bounds), record it with `catalog_write` and a basis; never claim a unit you cannot justify.
  Relations (`catalog_relate`: bounded_by, part_of, ...) and model bindings (`catalog_bind`:
  littles_law, RED, USE) go the same way; a binding role with no signal raises a Gap.
- `catalog_scan` measures a bounded set of metrics over a short window (resets, small decreases,
  negatives) and files contradictions with declared types or bounds as system findings; a short
  window only suggests, so say so when you cite it.
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
        Quantiles: write histogram_quantile(q, sum by (...) (rate(x[$__rate_interval])))
        as the WHOLE expression. It is evaluated per step (never rolled up) and each bucket
        carries n, the observations behind it; buckets with n < 10/(1-q) are flagged
        low_count. Wrapping a quantile in sum/avg/max/*_over_time is refused.
        $__rate_interval expands to max(4 x scrape interval, step + scrape interval).
        Returns {dataset, summary}. The summary is compact; raw series stay on the server.
        """
        try:
            # pi-lens-ignore: python-sql-injection
            return _dump(await service.query(expr, start, end, step, source))
        except SourceError as e:
            raise ToolError(f"{e} (hint: {e.hint})" if e.hint else str(e)) from e
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
    def analyze(
        dataset: str, baseline_start: str | None = None, baseline_end: str | None = None
    ) -> str:
        """Diagnose a time series in one call: is it stable, drifting, level-shifted, periodic,
        noisy, or is there too little data to say?

        Runs: periods (Lomb-Scargle, confirmed against AR(1) red noise, not just white noise);
        trend (99% interval, autocorrelation-adjusted); level shifts (CUSUM changepoints with
        p and a 99% interval on the size); KPSS stationarity; variance change; shape (skew,
        tails, zeros, bimodality); and a control chart (SPC). Control limits come ONLY from the
        baseline window (default: first half of the range; or baseline_start/baseline_end, e.g.
        a calm day before an incident), never from the data being judged; with autocorrelation
        the run rules/EWMA/CUSUM use AR(1) residuals. Intervals use the effective sample size
        n_eff, not n. Gaps are never interpolated.
        Returns per series: verdict, also, reasons (with numbers), and the sections; headline
        numbers carry an `evidence` statistic for finding_create. Caveats: coarsened, gaps,
        red_noise, short_baseline, near_random_walk, seasonal_not_in_baseline.
        Refused with a hint on percentile series, distributions and raw counters (use a rate).
        Draw it with show(dataset, question, mark="spc", windows=[{start, end}] = the baseline)."""
        try:
            return _dump(service.analyze(dataset, baseline_start, baseline_end))
        except (NotFound, ValueError) as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    async def compare_seasonal(
        dataset: str,
        cycles: list[str] | None = None,
        tz: str = "UTC",
        exclude: list[str] | None = None,
    ) -> str:
        """Is now unusual for this time of day / week? Compares the dataset's window with the
        same phase of previous cycles.

        cycles: which references to consider, of "previous" (the 4 preceding windows: the
        non-seasonal baseline), "1d" (same window on the previous 7 days), "1w" (previous 4
        weeks); default all. Each previous cycle is fetched (cached). The reference is chosen by
        leave-one-cycle-out error (a more specific cycle must win by 5%) and stated.
        tz: IANA timezone for alignment (default UTC): "same hour yesterday" in local time across
        DST is 23h/25h back. exclude: local dates (e.g. ["2026-12-25"]) whose cycles to drop
        (holidays); missing cycles and atypical ones (t prediction interval) are dropped too,
        each listed with its reason.
        The band is the median of the kept cycles +- quantiles of leave-one-cycle-out residuals
        (the spread ACROSS cycles, never one reference week). Verdict per series: usual |
        unusual (direction higher/lower/mixed) | insufficient_history (< 3 usable cycles), from
        three detectors at 1% each: window level (ratio vs a t interval with k-1 df), extreme
        points (Sidak over the window), share of points outside the band. `ratio.evidence` is
        a statistic for finding_create. Refused on percentile series (hint: histograms per
        cycle), distributions and raw counters. Draw with show(dataset, question,
        mark="seasonal")."""
        try:
            return _dump(await service.compare_seasonal(dataset, cycles, tz, exclude))
        except SourceError as e:
            raise ToolError(f"{e} (hint: {e.hint})" if e.hint else str(e)) from e
        except (NotFound, ValueError) as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    def fleet(
        dataset: str,
        by: list[str] | None = None,
        scale: str = "auto",
        normalise: str = "none",
    ) -> str:
        """Analyse many series of one metric as a group (a fleet: pods, nodes, instances) instead
        of drawing them as lines: spread across members per step, outlying members, churn.

        Spread: median, quartiles, 10/90% and min/max across the members that reported at each
        step (descriptive; missing members reduce n, never imputed), n per step and missing
        share. Outliers are judged against the OTHER members (leave-one-out median / MAD),
        robust to the outliers themselves, by: level (consistently off), change (drifting or
        shifted over the window) and excursions (spikes, 5- and 15-step episodes). Family-wise
        error 1% across members, tests and steps (Bonferroni); heavy-tailed noise is detected
        and thresholds follow the fleet's own peaks. Each outlier: member labels, kind
        (persistent | drifting | shifted | transient), direction, score (>= 1 = beyond the
        threshold), since (start of the current deviation; since_window_start = at least
        since the window began), effect (ratio or difference with 99% interval, from n_eff),
        transient episodes (sustained vs momentary relative to the autocorrelation time) and
        an `evidence` statistic for finding_create. Churn: members that appeared or stopped
        reporting (silent members may be the sick ones).
        by: label names that identify members (must be unique per series; default: the labels
        that vary). scale: auto (log = ratios when every value > 0) | log | linear.
        normalise: none | member (each member relative to its own median: compares shapes of
        members with different sizes; the level test is off). Ranges over 1440 steps are
        averaged per member first. Refused on percentile series (median of p99s is not the
        fleet p99), distributions, raw counters, < 5 members, members with different units.
        Draw with show(dataset, question, mark="fleet")."""
        try:
            return _dump(service.fleet(dataset, by, scale, normalise))
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
        Counts are increase() over a window equal to the step (never from quantiles), so
        they add up over time and across adjacent buckets. step: auto (~300 columns), never
        shorter than two scrape intervals.
        Returns {dataset, summary}: n per series, columns with/without data, low-n columns,
        the bucket scheme (the resolution limit) and the BUCKET holding p50/p90/p99 where n
        is large enough. Draw it with `show` (heatmap), or `show(mark="histogram",
        windows=[...])` to compare windows.
        """
        try:
            return _dump(
                await service.query_distribution(selector, by or [], start, end, step, source)
            )
        except SourceError as e:
            raise ToolError(f"{e} (hint: {e.hint})" if e.hint else str(e)) from e
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

        x in the metric's unit (e.g. 0.25 for 250 ms). start/end: optional window (default the
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

    def _source_error(e: SourceError) -> ToolError:
        return ToolError(f"{e} (hint: {e.hint})" if e.hint else str(e))

    @mcp.tool()
    async def source_connect(
        name: str,
        url: str | None = None,
        flavor: str = "prometheus",
        resolution: str = "15s",
        auth_env: str | None = None,
        auth_file: str | None = None,
        auth_scheme: str = "bearer",
        max_concurrency: int = 4,
        min_interval: str = "0s",
        timeout: str = "30s",
        replace: bool = False,
        profile_source: str | None = None,
    ) -> str:
        """Connect a Prometheus-compatible source at runtime (no daemon restart).

        Public demo sources connect by name alone, e.g. source_connect(name="grafana-play"):
        url, flavor, resolution and politeness come from the registry (see public_sources);
        only `replace` applies then. Give `url` to connect anything else.
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
        profile_source: name of another source with downsampled data of the same series (e.g. a
        Thanos downsample-1h datasource, connected with resolution=1h); long-window operating
        profiles are read from it.
        The source is probed before it is saved; it persists across daemon restarts.
        Returns {source, status}.
        """
        try:
            if url is None:
                entry = PUBLIC_SOURCES.get(name)
                if entry is None:
                    raise ToolError(
                        f"no url given and {name!r} is not a public source "
                        f"(known: {', '.join(sorted(PUBLIC_SOURCES))})"
                    )
                return _dump(await service.source_connect(entry.to_spec(), replace=replace))
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
                    "profile_source": profile_source,
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
    def public_sources() -> str:
        """Public demo/test sources you can connect by name with source_connect(name=...):
        backend, flavor, politeness limits (concurrency, min interval, timeout, max query
        range) and suggested use. None is connected until you ask. They are shared servers
        run by third parties: interactive volume only, narrow queries, never broad scans."""
        return _dump({"sources": [e.describe() for e in PUBLIC_SOURCES.values()]})

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
    async def source_learn(source: str = "default") -> str:
        """Discover a source's metrics and learn what is cheap to know (declared metadata, naming
        conventions, knowledge packs for node_exporter/Kubernetes). Slow on huge sources (it lists
        every metric name); run it once per source, then use catalog_search. Returns counts,
        caveats and a family overview (least-reviewed families first)."""
        try:
            out = await service.learn(source, "claude")
            return _dump({**out, "families": service.ws.catalog_overview(source)})
        except SourceError as e:
            raise _source_error(e) from e

    @mcp.tool()
    def catalog_search(
        source: str,
        query: str | None = None,
        prefix: str | None = None,
        needs_review: bool = False,
        limit: int = 50,
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
    def catalog_get(source: str, metric: str) -> str:
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
    def catalog_write(source: str, claims: list[dict[str, Any]]) -> str:
        """Record what you have learned about metrics, up to 200 claims per call. Each claim:
        {metric, field, value, confidence, basis}. field is one of type, unit, bounds,
        additivity_series, additivity_time, role, description, histogram_family. Relations between
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
    async def catalog_scan(
        source: str,
        metrics: list[str] | None = None,
        prefix: str | None = None,
        limit: int = 25,
        window: str = "30m",
        refresh: bool = False,
    ) -> str:
        """Measure what a short window of raw samples says about catalogued metrics: negatives,
        monotonic growth, counter resets, small decreases (a counter never does that). Writes
        origin=stats claims (type counter/gauge when the evidence is strong, bounds >=0 to fill a
        gap; never over a pack, Claude or user claim) and files contradictions (a declared gauge
        that only grows, a counter that decreases, negative values) as system findings.
        Targets: `metrics`, else `prefix`, else the metrics this workspace already queried. Bounded:
        <= 100 queries per call, a time budget, metrics scanned in the last day skipped, metrics
        above the source's series cap skipped. A short window only suggests: say so."""
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
    def catalog_relate(source: str, claims: list[dict[str, Any]], level: str = "catalog") -> str:
        """Record typed edges between metrics (level=catalog) or datasets (level=workspace), up to
        200 per call. Each claim: {subject, kind, object, confidence, basis, params?, retract?}.
        kinds: derived_from, part_of (errors part_of requests), same_quantity, upstream_of,
        bounded_by (subject never exceeds object at the same labels: avail bounded_by size),
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
        source: str,
        kind: str,
        key: str,
        roles: dict[str, str | None],
        confidence: float,
        basis: str,
        join_on: list[str] | None = None,
        retract: bool = False,
        level: str = "catalog",
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
    def catalog_relations(
        source: str,
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
        daily; refresh=true recomputes now. Counters are profiled as rates; rate windows widen to
        the 1h step; quantile expressions are profiled as per-hour quantiles (never averaged).
        Returns kind, coverage, robust range over hourly values (p0.5..p99.5, median, MAD),
        envelope (robust tails of intra-hour min/max), absolute min/max, and per series the
        seasonal model chosen by leave-one-out error (none | hour_of_day | hour_of_week, UTC)
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
    ) -> str:
        """Draw a dataset as a panel (mean line + min/max envelope) in the shared workspace.

        question is REQUIRED: the explicit question this graph answers, e.g.
        "Did checkout latency rise after the 14:00 deploy?".
        unit: optional y-axis unit (e.g. "s", "B", "ms", "items", "req/s"). Pass it
        when you know the unit from context the metric name doesn't reveal — you read
        the emitting code, or you know the generating tool's conventions. Your unit
        overrides suffix inference and is persisted with provenance ("provided by
        claude"), so only pass a unit you can actually vouch for.
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
        spectrum / spectrogram (time series): periodicity panels; spectrogram needs `segment`
        (e.g. "30m", state it in your answer) and takes `overlap` (default 0.5). view: for a panel
        drawn from filter(): the default view (overlay | filtered | removed | raw).
        windows (histogram/ecdf): 1-4 [{start, end, label}] compared on one chart, e.g. the
        spike vs the preceding baseline; each window sums whole steps, n is shown per window.
        A plain selector of a counter (a running total) is drawn as its rate, from a new dataset
        over the same window; the answer says so under `auto`. raw=true draws exactly the dataset.
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
        if hint := service.seasonal_suggestion(dataset, mark):
            out["suggest"] = hint
        return _dump(out)

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
        """Offer the user another y-axis view of a time-series panel; the USER picks.

        mode: zero (include 0), data (fit the data), meaningful (percentile panels: range
        only over buckets with n >= n_min, so a faded low-n outlier does not squash the
        real values), band (lo..hi), log (all values must be > 0; good when data spans
        more than 2 decades), indexed (needs baseline: window = each series' own mean,
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
        ghost: the same window last week as a faint dashed line (fetches it, so off by default).
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
        """Show a marginal histogram beside a time-series panel: the current window's value
        distribution vs a reference window, on the panel's own y scale. reference: previous
        (window of equal length just before) or week (same window 7 days earlier).
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
        """Compact workspace brief: panels, hypotheses, findings, open_threads, last_seq."""
        return _dump(ws.brief())

    @mcp.tool()
    def workspace_activity(since: int | None = None) -> str:
        """What happened since sequence number `since` (default: the most recent events)."""
        return _dump(ws.activity(since))

    return mcp
