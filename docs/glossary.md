# Telemetry Nerd — Glossary

One vocabulary for specs, skills, MCP tool descriptions and panel text. Where a doc, tool
description or panel string uses another word for one of these, it is a bug in that text. To
change a term, change it here first (same rule as [`principles.md`](principles.md)). Code
identifiers and JSON keys keep their historical names (`resolution_ms`, `scrape_interval_ms`,
`step_ms`, `window`): their *descriptions* use these terms.

## Time intervals

| term | definition | in code / payloads |
|---|---|---|
| **series interval** | The natural spacing of a series' samples: the scrape interval for pulled (Prometheus) metrics, the push or OTel export interval otherwise. A property of the data, learned per source and job. | `resolution_ms` (source), `scrape_interval_ms` (metric card), `learn_resolutions`, `Source.scrape_interval()` |
| **query window** | The `[w]` of a range function (`rate`, `increase`, `*_over_time`): how far back each evaluation looks. A query artifact, written by the user or filled by us (`$__rate_interval`, a rewrite). | `range_windows_ms`, `rate_window_ms`, `window_ms` of a merge kind |
| **query resolution** | How often the expression is evaluated inside one query bucket: the `res` of `rollup((expr)[step:res])` / a subquery's resolution. | `res` in `sources/promql.py` |
| **query step** | The spacing of fetched points. Equals the width of the **query bucket** `(t − step, t]`; a bucket carries its end time. | `step_ms` of a dataset |
| **query bucket** | One fetched point: the interval `(t − step, t]` and its `(avg, min, max, count)`. | rows of a dataset table |
| **display bucket** | The width of a drawn point after level of detail or zoom merges k query buckets. A display artifact. | `effective_step_ms` of a panel |
| **time range** | `start..end` of a query, dataset or panel. Never called a bare "window". | `start_ms`/`end_ms`, `TimeRange` |
| **tile** | A query bucket holding exactly one non-overlapping evaluation: query window = query step **and** one evaluation per query bucket (query resolution = query step, or values fetched at the step). The only case where values add across buckets: `increase(x[1m])` at a 1 m step evaluated once per bucket is a tile; at a 15 s query resolution it is the mean of 4 overlapping 1 m windows, not a tile. | `_TILE_FUNCS` (`model/companions.py`) |
| **effective time resolution** | max(query window, query step, display bucket), never finer than the series interval. What a panel means by "detail shorter than X is smoothed". | — |

### Relationships

- **Query window vs series interval.** `rate`/`increase` need at least 2 samples in the window;
  the convention is 4 series intervals. `$__rate_interval` = max(4 × series interval,
  query step + series interval) (`analysis/exprkind.py: rate_interval_ms`).
- **Query step vs series interval.** A step shorter than the series interval leaves buckets
  **empty** between samples and invents detail (`fake_resolution`; query_distribution refuses a
  step under two series intervals). A step close to the series interval (0.8–1.2 samples per
  bucket) makes samples **spill** into the neighbouring bucket (a 2 next to a 0;
  `SPILL_RATE`, `model/bucket_state.py`). A step of several series intervals is **stable**.
- **Query window vs query step.** Window = step with one evaluation per bucket: **tiles** (each sample counted once; values sum).
  Window > step: **sliding** windows, a smoothing filter of the window's width (neighbouring
  points share samples, so effective n drops). Window < step: **partial**, part of each bucket is
  not looked at (between evaluations).
- **Display bucket vs query step.** Level of detail merges k query buckets into one display
  bucket; the line is a merged value and the **min–max envelope** keeps every peak (principle 3).
  How the line merges depends on the expression (bucket-merging design, bead `7jme`).

## Related terms

Only terms that recur across specs; each points at where it is defined.

| term | definition | defined in |
|---|---|---|
| **sample** | One raw `(ts, value)` the source stored for a series. | MVP spec §3.2 |
| **count** | Samples of the underlying selector inside a query bucket (not evaluations of a derived expression). | MVP spec §3.2; `sources/observed.py` |
| **dataset** (`d*`) | A signal or code output evaluated over a time range and query step; a set of series; immutable. | MVP spec §3.1 |
| **panel** (`p*`) | A drawn view of one or more datasets in the workspace. | MVP spec §3.3, §6.4 |
| **representation** | `sample`, `bucket_agg`, `distribution`, `quantile`, `estimate`: what a bucket's numbers mean. | MVP spec §3.2 |
| **envelope** (min–max band) | The band from each bucket's min to max drawn behind the line; never smoothed away. | MVP spec §6.5; principle 3 |
| **LOD** (level of detail) | Server-side min/max-preserving merge to about one display bucket per pixel. | MVP spec §6.5; `analysis/resample.py: lod` |
| **bucket_state** | Per series and query bucket: `ok`, `partial`, `empty`, `absent`, `unknown` (+ flags `reset`, `interval_change`, `stale_marker`, `source_filled`, `post_gap`, `cadence`: an `unknown` bucket that is a skip of a series interval a little over the query step, or a scrape lost at that spacing). | series-bundles spec §5; `model/bucket_state.py` |
| **coverage rug** | Strip under a time panel marking buckets whose bucket_state is not `ok`. | series-bundles spec §7.1 |
| **settling** | The newest buckets, which the source may still change. | MVP spec §2.2 |
| **operating profile** | What a signal normally looks like over a long time range (hour-of-week statistics at a 1 h step). | operating-profile spec |
| **normal band** | Per-series band from the operating profile for the same hour, drawn on a time panel. | reference-overlays spec |
| **ghost** | The same time range one week earlier, faint and dashed. | reference-overlays spec |
| **indexed view** / **baseline** | Y view as a ratio to a baseline (`window` = the series' own mean over the time range, `previous`, `week`) on a log axis. | `ui/src/chart/yview.ts`; graphing guide §2 |
| **reference time range** | The earlier time range a verdict or comparison judges against (`previous`, `day`, `week`). Payloads may say "reference window". | principle 14; binding-verdicts spec |
| **point model** | The plain (or bias-corrected) estimate a two-model test reports as context; optimistic. Its p is `p` (or `p_poisson`, `p_baseline_model`, `p_point_model` in evidence). A result only it supports is **undetermined**. | principle 16; e.g. `stability.shifts[].p` (`analysis/stability.py: SHIFT_METHOD`) |
| **cautious model** | The more conservative alternative of a two-model test (autocorrelation, clustering, overdispersion, heavier tails, a parameter at the edge of its uncertainty); a label rests on it (`label_rests_on: cautious`). | principle 16; `p_cautious`, `p_clustered` |
| **Little's law window** | The sub-range of the time range over which L and λW are averaged (`window`, default ~range/12). | Little's law spec |

## Words we don't use

| don't write | write | why |
|---|---|---|
| "scrape interval" in general text | **series interval** | Not every series is scraped (push, OTel). Say "scrape interval" only for the Prometheus-specific case or the `scrape_interval` config. |
| "source interval" | **series interval** | "Source" is the data source; the interval belongs to the series (it differs per job). |
| bare "window" for `start..end` | **time range** | "Window" is the query window. Qualified analysis windows (Little's law window, spectrogram segment) are fine. |
| "rate window", "rate interval" | **query window** (`$__rate_interval` when it is that template) | One name for the `[w]` of every range function. |
| "resolution" for the step | **query step** | Resolution is query resolution (evaluations per bucket) or the series interval. |
| "native resolution", "source resolution" | **series interval** | Same thing; one name. |
| "display step" | **display bucket** | It is a width of merged buckets, not a fetch spacing. |
| "bucket" alone where it matters which | **query bucket** / **display bucket** / **value bucket** (histogram `le` bucket) | Three different things. |
