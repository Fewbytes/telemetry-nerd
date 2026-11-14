# Visualization service for Claude: spec

Epic `telemetry-nerd-rnkq`, requested by the king on 2026-11-14. Spec only; nothing here is
implemented except where marked **exists**. Rules from `docs/telemetry-graphing-guide.md`
(the "graphing guide") and `docs/principles.md` win over this document on any conflict.

## 1. What this mode is

Claude holds numbers (a computation, a table from the conversation, a CSV the user pasted)
and wants the user to see them. **Plot mode** draws caller-supplied data as panels in the
shared workspace. No telemetry source is queried.

| | Investigation flow | Plot mode |
|---|---|---|
| Data comes from | a source, via `query` / `show` | the caller, in the tool call |
| Re-fetch, re-scope, `reframe`, `preview` | yes | refused (`refuse_requery`): the data is fixed |
| Evidence claims | scoped, sourced, with caveats from the source | none about any system; labelled "supplied by claude" |
| Provenance | source + expression | pseudo source `inline`, node `inline` (**exists**) |
| Typical question | "why is p99 up?" | "show me these numbers" |

Plot mode is not evidence about a system (principle 3: claims are scoped). A plot-mode panel
says only "these are the values the caller gave"; the footer, the panel title and the
tool result all carry that label.

## 2. Input contract

### 2.1 Entry points

One MCP tool family, one core function each; the CSV path is a parser in front of the same
core, not a second pipeline.

| Entry | Takes | Notes |
|---|---|---|
| `plot_data` (**exists**) | `question`, `series: [{label, ts, values}]`, `unit?` | time series only; stays as is |
| `plot_values` (new) | `question`, `kind`, `data`, `unit?`, `labels?` | non-time-series forms; `kind` in section 3 |
| `plot_csv` (new) | `question`, `csv` (text), `kind?`, `unit?` | CSV text → the same parsed table → `plot_data` / `plot_values` |

`question` is required on every entry (what the panel answers; principle 1). No entry
takes a source, expression or PromQL.

### 2.2 Shapes of `data`

Parsed once at the boundary into a typed `PlotTable` (named columns of finite floats, an
optional label column, an optional time column). Everything after the parser takes the
validated type, never raw dicts.

1. **Array** `[1.2, 3.4, ...]`: one anonymous column. A distribution kind uses it as the
   sample; a bar kind needs `labels` of equal length; a time-series kind is refused
   (no time axis: say `ts` or use `plot_data`).
2. **Labelled columns** `{"latency_ms": [...], "other": [...]}`: one entry per column, all
   columns the same length. Keys are the labels. This is the form for comparing samples
   (Q-Q, ECDF, histogram) and for several bar series.
3. **Matrix** `[[...], [...]]` (rows of equal length) with optional `labels` (row labels,
   length = rows) and `columns` (column labels, length = columns). Without `columns` the
   columns are `c0, c1, ...`. A matrix is read as a table: the first column is the time or
   category axis when `kind` says so, otherwise every column is a sample.
4. **Time series** `[{label, ts, values}]` as in `plot_data`.

CSV (`plot_csv`): first row is the header. Delimiter is `,` (a leading `sep=` line is not
supported; `;` and tab are refused with a reason, not guessed). Column types: a column
named `ts`, `time` or `timestamp` (case-insensitive) is the time axis (epoch ms or ISO-8601
with timezone, as `plot_data`); a column whose cells are not numbers is the label column
(at most one); every other column is numeric. Empty cells are missing values (see 2.4).
Quoting follows RFC 4180. Encoding is UTF-8 text in the tool argument; there is no file or
URL input (nothing fetched by the service, no path read: no SSRF or file-read surface).

### 2.3 Size limits

Limits are named constants in one module (one source of truth), echoed in the refusal text.

| Limit | Value | Why |
|---|---|---|
| Series (columns) per panel | 8 | graphing guide §3: readability collapses past ~8; `plot_data` already budgets lines at `LINE_SERIES_BUDGET` = 5 and routes groups to a fleet view |
| Points per series, time series | 100 000 | M4-style reduction draws any more than the canvas shows; the grid limit `MAX_GRID_BUCKETS` = 20 000 of `plot_data` still applies and wins |
| Values per sample, distributions | 1 000 000 | histogram/ECDF/Q-Q are computed server side from the whole sample |
| Bars per bar chart | 50 | beyond that, a sorted top-N plus an explicit "other" bar, or a distribution (refuse, suggest) |
| CSV size | 5 MiB of text | bounds the parse; the tool argument is the whole payload |
| Label length | 80 chars | titles and legends |

Over a limit: refuse; never silently truncate or subsample (principle 4: no silent
fallbacks). The one allowed reduction is drawing-time decimation of a time series that
fits the limit, which is already how panels render and is stated in the panel.

### 2.4 Validation and error reasons

All validation happens in the parser; every failure is a `ValueError` subclass carrying a
stable machine-readable `reason` code plus a human sentence naming the offending series,
column and index. The MCP layer maps it to `ToolError(str(e))` (the existing `plot_data`
pattern).

| `reason` | Raised when |
|---|---|
| `empty` | no series, no columns, or a zero-length column |
| `ragged` | columns / matrix rows differ in length; `labels` length mismatch |
| `not_finite` | `NaN`, `±Inf`, a boolean or a non-number in a numeric column. Missing values are expressed as `null` (JSON) or an empty CSV cell, never as `NaN` |
| `duplicate_label` | two series or columns share a label |
| `missing_label` | blank label, or a bar kind with no `labels` |
| `ts_not_increasing`, `ts_unparseable` | time axis not strictly increasing / not epoch ms or ISO-8601 with timezone (as `plot_data`) |
| `grid_too_fine`, `grid_too_long` | the existing inline grid checks |
| `over_limit` | any limit in 2.3; names the limit and the value received |
| `unknown_kind` | `kind` not in section 3 (see 4 for `pie`) |
| `kind_shape_mismatch` | the data shape cannot be drawn as `kind`, e.g. `qq` with one column, negative values for a log axis the caller requested |
| `csv_unsupported_delimiter`, `csv_malformed` | CSV problems |
| `refused_form` | a form the style guide forbids (pie, 3-D, dual axis, section 4) |

**Missing values** (`null` / empty cell) are kept as gaps in time series (drawn as gaps,
`bucket_state` missing, as `plot_data` does today). In distribution kinds they are dropped
and the count is stated in the panel ("n = 980 of 1 000, 20 missing"); in bar kinds a
missing bar is drawn as "no value", not as zero.

## 3. Plot types and phases

Each kind maps to an existing mark where one exists (reuse, section 5). `kind` is a closed
enum, a literal table in code, not a string-built dispatch.

| Phase | `kind` | Input shape | Mark | Status |
|---|---|---|---|---|
| 1 | `line` | time series | `line+envelope` | **exists** (`plot_data`); phase 1 adds CSV in and matrix in |
| 2 | `histogram` | array or columns (≤4) | `histogram` | mark **exists** (window mark) |
| 2 | `ecdf` | array or columns (≤4) | `ecdf` | mark **exists** |
| 2 | `qq` | exactly 2 columns | `qq` | mark **exists** |
| 2 | `quantile_curve`, `ccdf` | array or columns | same names | marks **exist**; for tails |
| 3 | `bar` | `labels` + one column, or columns | `bar` | **new mark** |
| 3 | `bar_grouped` | `labels` + 2..4 columns | `bar` | new mark, shared baseline |
| 4 | `heatmap` | matrix (rows × columns) | `heatmap` | mark exists for source data; matrix input is new |
| later | `scatter`, `box`-like quantile strips | columns | none yet | out of this spec; needs its own style review |

Phase 1 is time series, as the king asked. Distributions follow, then bar charts.
Phase order inside the epic is a dependency order of cards (section 7).

Rules per kind, from the graphing guide:

- **Time series**: at most 5 lines in one plot (`LINE_SERIES_BUDGET`); 6..8 columns are
  refused with a hint to split into small multiples with an identical y-scale (§3); more
  than 8 is `over_limit`. Mean-only lines carry no envelope since each point is one sample
  (as `plot_data` today). Axis rules §4: y-range per the guide, not forced to zero for
  lines.
- **Histogram / ECDF / Q-Q**: bins and quantile levels are chosen by the existing marks
  and stated on the panel (no hidden bin choice, §3). At most 4 samples (`MAX_WINDOWS`).
  Q-Q draws the y = x reference and states both sample sizes.
- **Bar**: the baseline is always zero (bars encode length), never truncated, never log.
  Sorted descending by value unless `labels` carry an inherent order and the caller sets
  `order="given"`. Horizontal when any label exceeds 12 characters or there are more than
  12 bars. A negative value draws a bar below the baseline. One hue per series, the same
  colour for the same label across panels. Value labels on the bars when ≤ 12 bars.
- **No dual axis** in any kind; separate scales mean separate panels (existing rule).
- **Units**: `unit` is recorded as "provided by claude", as for `plot_data`.

## 4. Pie charts: never rendered

This is a hard rule, not a preference, and has no flag, option or override.

- No `pie`, `donut`, `radial`, `sunburst` or 3-D variant exists in the mark enum, the
  tool schemas, the UI renderer or the CSV `kind` values. A regression test asserts the
  mark enum, the `kind` enum and the renderer registry contain none of these names.
- A request that asks for one (`kind` in `pie`, `donut`, `doughnut`, `radial`, `sunburst`,
  `3d_pie`, case-insensitive; the rule keys on `kind` only, `question` text is not
  inspected) fails with `reason = refused_form` and the text:
  "Pie charts are never rendered: angle and area are read less accurately than length, and
  slices are hard to compare. Use `kind=bar` (sorted, zero baseline) for parts of a
  whole." Nothing is drawn and no panel is created.
- **Refuse, do not silently map.** Mapping a pie request to a bar chart without being
  asked would be a silent fallback and would change what the caller thinks they got.
  Instead the refusal text names the bar alternative, and the caller (Claude) re-issues the
  call with `kind=bar`. The one exception is explicit: `kind="bar"` is the mapping.
- Parts of a whole: a bar chart of the shares with the total stated in the title; when the
  parts are over time, a stacked-area is **not** offered either (stacked areas hide the
  non-baseline series; graphing guide §3); use lines or small multiples.
- The MCP tool docstrings state the rule so Claude reads it before calling.

## 5. Reuse of existing panels, difference from the investigation flow

- **Storage and panels**: every plot-mode call stores a dataset with source `inline`
  (**exists**: `INLINE_SOURCE`, `plot_inline`) and shows it through `TelemetryService.show`,
  so panels, workspace, layout, selection, y-view suggestions, export and the UI renderers
  are unchanged. A new kind adds a mark and a parser, not a parallel panel system.
- **Non-time datasets**: window marks (`histogram`, `ecdf`, `qq`, ...) today take their
  windows from a time range of a source dataset. Plot mode feeds them a `PlotTable` sample
  instead: the card that adds distributions must define a windowless sample dataset (a new
  dataset kind whose "windows" are the caller's columns) rather than faking timestamps.
  This is the main design risk and is its own card.
- **Differences enforced in code**: no re-query, no `reframe`, no `preview`, no time-range
  rescope, no catalog or `show(signal)` auto-chart, no source caveats, no
  `bucket_state` claims about a system (only "gap: missing value supplied as null"); the
  panel is marked "supplied by claude"; the data can be exported but never presented as
  telemetry. `refuse_requery` already provides the refusal path; each new dataset kind
  must call it.
- **Ambient events and the user**: the user can still pick y-views, zoom, and read the
  table view (accessibility, §8); user selections arrive as ambient events as for any
  panel.

## 6. Style-guide conformance

Conformance is checked, not asserted: each kind has a test that renders its panel spec and
asserts the rules below. Rules are those of the graphing guide, restated here only as
checkable properties.

| Property | Rule (guide section) | Check |
|---|---|---|
| ≤5 lines per plot, ≤4 compared samples | §3, `MAX_WINDOWS` | spec validation rejects more |
| Question on every panel | principle 1 | tool requires it; empty → `missing_label`-style refusal |
| Colour | §6: colour-blind-safe categorical set, same label same hue, light/dark variants meet contrast | reuse the existing palette module; test asserts no literal colours in the new marks |
| Axes | §4: bars zero-based, no dual axis, log only with labelled variable + unit, y-range stated | spec-level assertions |
| Honest sizes | n and missing counts stated on every distribution; bin rule stated | panel text test |
| No pie | section 4 | enum test |
| Accessibility | §8: ARIA name and description, view-as-table | each new mark supplies a table view of the supplied values |
| Provenance | principle 3 | footer reads "supplied by claude; not queried"; unit "provided by claude" |
| Resolution / decimation | §7 | existing renderer; the panel states when drawn points < supplied points |

New marks (`bar`) get an entry in guide §3 (table row) and §10 (gaps) in the card that
adds them; the guide stays the single rulebook. Where this spec adds a rule the guide lacks
(bars: zero baseline, sort order, label rules; the no-pie rule), the same card moves it
into the guide.

## 7. Proposed implementation cards

Order is a dependency chain unless marked parallel. Each card is small and its acceptance
criteria are mechanically checkable (a test, a command or a grep with a named expected
result). Each ships its own tests per the testing guide.

1. **Plot-mode parser and error reasons.** New `PlotTable` type, parsers for array,
   labelled columns, matrix, and the reason codes of 2.4; limits as named constants.
   *Accepts when*: `uv run pytest tests/unit/test_plot_table.py` passes and has a test per
   `reason` code that asserts both the code and that the message names the offending
   column; no function in the module takes `dict[str, Any]` after the parse.
2. **CSV input: `plot_csv` for time series.** Parser per 2.2, wired to the existing
   `plot_inline`. *Accepts when*: a test feeds a 3-column CSV with an ISO `ts` column and an
   empty cell and gets a line panel with one gap and the "supplied by claude" label;
   `;`-delimited CSV returns `csv_unsupported_delimiter`; a 5 MiB+1 CSV returns
   `over_limit`. Depends on 1.
3. **`plot_data` accepts matrix / columns and enforces the series budget.** *Accepts
   when*: 6..8 series are refused with the small-multiples hint, 9 gives `over_limit`;
   existing `tests/unit/test_inline_plot.py` still passes unchanged. Depends on 1.
4. **No-pie guard.** `refused_form` for the `kind` values of section 4, tool docstrings
   state the rule, and an enum test. *Accepts when*: a test asserts every pie-like `kind`
   is refused with the exact text containing `kind=bar`, creates no panel (panel count
   before = after), and that `rg -i "pie|donut|doughnut|sunburst"` over `src/` and `ui/src`
   matches only the guard's refusal table and its tests. May go in parallel with 1.
5. **Windowless sample dataset.** The design risk of section 5: a dataset kind holding
   caller samples that window marks can draw. *Accepts when*: `histogram` over a supplied
   array produces a panel spec that validates, with n and bin rule in the panel text, and
   `refuse_requery` refuses re-scope. Depends on 1.
6. **`plot_values` kinds `histogram`, `ecdf`.** *Accepts when*: a 1 000-value array and a
   2-column comparison both produce panels; 5 columns return `over_limit` (`MAX_WINDOWS`).
   Depends on 5.
7. **`plot_values` kind `qq`** (and `quantile_curve`, `ccdf`). *Accepts when*: 2 columns of
   unequal length draw with both n stated; 1 or 3 columns return `kind_shape_mismatch`.
   Depends on 5; parallel with 6.
8. **`bar` mark: spec, renderer, table view.** New mark in `charts/spec.py` and the UI,
   zero baseline, sort, horizontal switch, value labels per section 3. *Accepts when*: a
   spec test rejects a bar spec with a non-zero or log axis; a UI test (`cd ui && npm
   test`) renders 5 bars and asserts sort order, baseline at zero and a view-as-table with
   5 rows; a negative value draws below the baseline. Depends on 1.
9. **`plot_values` kinds `bar`, `bar_grouped`.** Parser to mark, ≤50 bars, `labels`
   required. *Accepts when*: 51 bars → `over_limit`; missing `labels` → `missing_label`;
   grouped with 5 series → `over_limit`. Depends on 8.
10. **Style-guide update.** Add bar-chart rows to guide §3 and §10, the no-pie rule and
    the plot-mode note to `docs/principles.md` or the guide §9. *Accepts when*: the guide
    contains a "pie" row in §9 with the refusal wording and `just ci` passes. Depends on 8
    and 4.
11. **Matrix heatmap (phase 4, optional).** Matrix input drawn as `heatmap`. *Accepts
    when*: a 10×10 matrix produces a heatmap panel with row and column labels and a legend
    stating the colour mapping. Depends on 5.
12. **Plot-mode skill text.** A skill/instructions section for Claude: when to use
    `plot_data` / `plot_values` / `plot_csv` vs `query` + `show`, the no-pie rule, the
    limits. *Accepts when*: the skill file exists under `skills/`, names all three tools,
    and a test asserts the tool names it cites exist in the MCP server's registered tools.
    Depends on 4, 6, 9.

## 8. Open questions for the king

- `plot_csv` takes CSV text in the tool argument. Should it also read a file path the
  user names? Recommendation: no (file-read surface, section 2.2); the text goes in the
  call.
- Should `scatter` be in phase 2? The epic says "bar charts and similar"; scatter needs its
  own style review (overplotting, §3 has no row for it), so it is left out here.
