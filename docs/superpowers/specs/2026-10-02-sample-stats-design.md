# T1 sample statistics and contradictions (2as.6)

`catalog_scan(source, metrics?, prefix?, limit=25, window="30m", refresh=false)` (MCP) /
`TelemetryService.scan_metrics`: an explicit, budgeted measurement, never "all metrics".

## Budget
Targets: named metrics, else a prefix (hot first), else this workspace's hot metrics; catalogued
metrics only. At most 100 queries per call, a 60s wall-clock budget, window 5m-6h, metrics scanned
in the last day skipped unless `refresh`, a metric over the source's series cap skipped with the
reason. Result: scanned / skipped / failed, `stopped` (limit|budget) and `remaining`.
One query per metric (bare selector, step = series interval, coarser past ~120 points) through the
normal path (politeness gate, series cache, a dataset usable as evidence).

## Measurements (`analysis/samples.py`, pure)
Per series then pooled: samples, min/max, negatives, increases, decreases, **resets** (decrease to
< half the previous value), **small decreases** (a counter never does that), integrality,
constancy. Series with < 10 samples do not vote. Observations are stored per metric
(`catalog_samples`) and drive the metric card's "counter resets" row.

## Claims (origin `stats`, conservative)
- type counter (>= 5 increases, no small decreases, no negatives) / gauge (small decreases in >= half
  the voting series), confidence 0.6; bounds >= 0 (no negatives in >= 30 samples), 0.4, only to fill
  a gap so a known [0,1] is never degraded.
- `stats` outranks packs and declared metadata in the precedence order, so a scan **never writes a
  type over a pack, Claude or user claim that disagrees**: that is filed as a finding instead. It may
  replace a name rule or declared metadata (observed behaviour beats a name or a declaration).

## Contradictions -> system findings
Against the winning non-stats claim: a declared gauge that only grows (>= 5 increases, no
decrease), a counter with small decreases, negative values under a counter or a non-negative bound.
One finding per (metric, kind) (`catalog_findings`), actor `system`, evidence an exact integer
statistic (increases / small_decreases / negatives) on the scan's dataset, caveat `short_window`;
the text says when the claim rests on a name convention only.

Out of scope: automatic scanning, long windows (the operating profile), integrality claims.
