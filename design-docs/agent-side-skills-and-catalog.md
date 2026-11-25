# Agent-side skills, tools and catalog for the UI layer: design note

Card `telemetry-nerd-h4es.1`, companion to the spec `design-docs/ui-layer-and-compute-server.md`
(card `telemetry-nerd-h4es`, landed). Section numbers below ("spec 3.4") refer to that spec. This
note covers what section 7 of the spec leaves agent-side: material built on the contract that may
change without a contract revision. Rules from `docs/principles.md` win on any conflict.

## 1. Rules for everything agent-side

1. **Relies on the contract, never extends it.** A skill calls `plot_data`, `plot_csv`, `query`
   and `show` as they are. It cannot add a chart kind, relax a limit or skip a refusal (spec 3.4).
2. **No contract behaviour depends on a skill.** With every skill uninstalled the UI layer still
   draws every kind and metric set (spec 7).
3. **Agent-only mode is the baseline.** A skill works with no compute server. A skill that wants
   a compute feature says so, names the `compute` opt-in (spec 4.3), and has a plain-chart path
   when the opt-in is absent.
4. **A skill only cites tools that are registered.** A test per skill enforces it, as
   `tests/unit/test_plot_mode_skill.py` does for `plot-mode`.
5. **A skill states a number only if the code states it.** Limits come from `plot_table.py` and
   the skill's test compares them; a skill never carries its own copy of a limit unchecked.
6. **SPC and other advanced models are not used** (spec 4.2).

## 2. Skills

Skills live in `skills/<name>/SKILL.md`. Existing ones are kept; this note adds two and
amends one. The contract calls each skill uses are listed so a contract change can be traced
to the skills it touches.

| Skill | Status | Teaches | Contract calls used |
|---|---|---|---|
| `plot-mode` | exists | Which kind answers which question, why pie is refused, the limits | `plot_data`, `plot_csv`, `workspace_promote` |
| `charting` | exists | Which view answers which question for queried datasets | `query`, `show`, `fleet`, `show_marginal` |
| `metric-sets` | new | Choosing members for a question, one recipe per standard set (section 4), when to split a set over 6 members | `plot_data` / `show` with the `set` form (spec 6) |
| `gang-analysis` | new | Fleet analysis with PromQL only (section 5) | `query`, `plot_data` (`line`, `bar`) |
| `metric-learning` | exists | Filling the daemon-side catalog | `catalog_*` tools; see section 3 |

`metric-sets` waits on the metric-set contract implementation (spec 9, item 3); until it lands the
skill is not written, and `gang-analysis` draws its members as separate panels.

## 3. Catalog

The spec says the catalog can live agent-side. Today the daemon holds one: claims about each
metric with an origin and precedence (`src/telemetry_nerd/catalog/`, `metric-learning` skill).
That one answers "what does this metric mean" and stays as it is; the UI layer needs only its
output (unit, type) as the `unit` on a call.

The **agent-side catalog** is a different thing: what the *agent* knows about where to look. It
is a repository file, so a team can review and version it, and an agent reads it with ordinary
file tools. No daemon change and no new tool.

Proposed file: `.telemetry-nerd/catalog.toml` in the project being investigated.

| Section | Holds | Example |
|---|---|---|
| `[[source]]` | Name, kind (grafana, prometheus, elasticsearch, csv, mcp), what it covers, who owns it, known caveats | `name = "prod-prom"`, `covers = "all services, 15s step"`, `caveats = ["no data before 2026-09"]` |
| `[[metric_set]]` | A named set (spec 6): question, members as `{name, role, expr, unit}`, source | `request rate, error rate, p99 of one service` |
| `[[query]]` | A saved query: name, source, expression with named parameters, unit, question it answers | `error_ratio(service)` |

Rules:

- Entries are **recipes, not data**. The catalog stores expressions and questions; never values,
  and never a result presented as evidence (spec 3.4.5).
- Every `[[metric_set]]` has at most 6 members (spec 6) and `unit` per member; a test loads the
  file shipped as an example and checks both.
- A member's `expr` is run by the agent against the named source and the result passed to the UI
  layer. The UI layer never reads the catalog and never executes an `expr` (spec 3.1).
- Provenance on the panel is the source and expression the agent actually ran; a catalog name is
  not provenance.
- A missing catalog is not an error: the agent asks the user or browses the source.
- Secrets (tokens, URLs with credentials) are never in the catalog; a source entry names a
  source the daemon already knows (`source_connect`).

Tooling: a catalog reader is a skill instruction ("read `.telemetry-nerd/catalog.toml` if it
exists"), not a tool. If a validation command is wanted later it is one small CLI check, filed
separately.

## 4. Standard metric-set recipes (for the `metric-sets` skill)

Each recipe gives the question, the members, and the PromQL shape. Names are placeholders for
the user's own metrics; the skill tells the agent to look them up, not assume them.

| Set | Question | Members (role, unit) |
|---|---|---|
| RED, one service | Is this service serving well? | request rate (req/s), error ratio (ratio), p99 latency (s) |
| USE, one resource | Is this resource limiting? | utilisation (ratio), saturation (queue length or wait), errors (count/s) |
| Instance health | What is one instance doing? | CPU (ratio), memory (bytes), GC pause (s) |

The p99 member is read from a histogram with `histogram_quantile` over `rate(..._bucket[...])`
of **one** aggregation level; percentiles are never averaged (principle 10).

## 5. Worked example: gang analysis with PromQL (agent-only mode)

**Question**: "Is one instance of this service behaving differently from the others?" A gang is
the instances of one service at one time. No model and no compute server is needed.

Steps the skill gives the agent:

1. **Per-instance series**, one query, grouped by instance:
   `sum by (instance) (rate(http_requests_total{service="X"}[5m]))`. State the window and step.
2. **Count the gang.** `count(...)` per step, so the number of members and any that stopped
   reporting are stated (n and missing, spec 3.4.1). A member with no samples is "no value",
   never zero.
3. **Fleet median per step.** `quantile(0.5, <step 1 expr>)`. The median across instances is a
   descriptive statistic of the gang at that step, not a percentile of requests, and the skill
   says so.
4. **Ratio to the median.** `<step 1 expr> / scalar(<step 3 expr>)` per instance. A ratio of 1 is
   typical; the spread of ratios is the gang's spread.
5. **Rank, do not chart 100 lines.** Take the instance mean ratio over the window, sorted. Draw
   with `plot_data`:
   - `bar` of mean ratio per instance (zero baseline, at most 50 bars, spec 5). Over 50
     instances: draw the 50 farthest from 1 and say how many were left out and why. This is the
     agent's choice and is stated in the `question`; the UI layer itself never truncates.
   - `line` of the (at most 5) farthest-from-median instances against the median, as ratios.
6. **State what it does and does not say.** Ratios show *which* instances differ and *when*;
   they do not say whether the difference is noise or cause. No significance claim is made in
   agent-only mode. Differences that persist across the whole window and across restarts are
   leads for a human or a later compute opt-in, not verdicts.

Failure paths the skill tells the agent to handle: one instance only (no gang, say so and stop);
all values equal (ratio 1, say "no spread", draw nothing); a counter reset inside the window
(drop that step for that instance and count it; never a negative rate).

Relation to the existing `fleet` tool: `fleet` runs the daemon's statistical outlier tests
(`analysis/fleet.py`) and is the richer path when a daemon dataset exists. The PromQL skill
works against any Prometheus-compatible source, needs no dataset and makes no test claim.
`gang-analysis` points to `fleet` for users who want the tests, and never requires it.

## 6. Tests for skills and catalog

Business claims, one test each, all hermetic:

- Each new skill cites only registered tools (pattern of `test_plot_mode_skill.py`).
- Each skill that quotes a limit matches `plot_table`'s constant.
- The shipped example catalog parses, every metric set has at most 6 members and a unit per
  member, and no entry holds a credential-shaped field.
- The `gang-analysis` PromQL templates contain no `avg(` or `histogram_quantile(avg` across
  instances and no `/ 0`-prone form; checked as text against the skill file.

## 7. Out of scope

- The compute server and any model (spec 4.2, 8).
- New adapters; the MCP-result extractor is a one-paragraph instruction in `plot-mode`, not a
  tool.
- Changing the daemon-side catalog or `metric-learning`.

## 8. Follow-up implementation cards (to file after this lands)

1. `gang-analysis` skill and its tests (section 5). Independent of the metric-set contract.
2. Example `.telemetry-nerd/catalog.toml`, its loader check and the `charting` /
   `plot-mode` pointer to it (section 3).
3. `metric-sets` skill with the section 4 recipes, after spec item 3 (metric set contract) lands.
4. `plot-mode`: MCP-result extraction paragraph (spec 3.2 row "MCP tool result").
