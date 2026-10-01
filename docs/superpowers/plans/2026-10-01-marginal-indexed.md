# Marginal Histogram and Indexed View: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking. Beads: `telemetry-nerd-4ok.6` (marginal histogram, spec §6.4) and `telemetry-nerd-4ok.14` (indexed view, guide §1/§3), both under epic M4 `telemetry-nerd-4ok`. They share one new concept, the **panel reference window**, so 4ok.6 is built first and 4ok.14 reuses it.

**Goal:**
- **4ok.6:** a time-series panel can show a **marginal histogram** on the right of its plot. It shares the plot's y scale and compares the current window with a reference window (the previous window of equal length, or the same window last week).
  - For histogram-backed panels it is a distribution of **observations (requests)**, built from distribution datasets.
  - For plain series it is a distribution of the **per-step values**, labelled as such.
  - n is always shown. The user toggles it; Claude can preselect it with a reason.
- **4ok.14:** a new y-view mode `indexed`. Each series is drawn as a ratio to a common baseline on a log axis with 1 centred. The baseline is either each series' own mean over the window, or the same series in the previous window or last week, point by point.
  - The view labels what 1.0 means and draws a rule at 1.
  - It refuses series whose baseline is missing or ≤ 0, and refuses the window mean for percentile series (that would average percentiles).

**Architecture:**
- **Pure server modules:**
  - `analysis/reference.py`: `reference_window()`, which picks the reference and is unit-tested as the acceptance asks.
  - `analysis/marginal.py`: per-step sample values and fine sample bins.
  - `charts/indexed.py`: window baselines, pointwise baselines and `check_index`.
- **Spec:** `ChartSpec` gains:
  - `references: dict[mode, Reference]`, the reference datasets per mode, fetched once and then reused;
  - `marginal: Marginal | None`.
  - `YView` gains mode `indexed` and a `baseline` field.
- **Service:**
  - `TelemetryService.ensure_reference` (async) fetches reference datasets through the normal `query` / `query_distribution` paths, so they get provenance and ids that Claude can cite.
  - `WorkspaceService.set_marginal` and `select_y_view`/`suggest_y_view` store the reference and the choice in one `@atomic` write, plus one event.
  - `panel_data` adds `marginal` and `index` payloads to time panels.
- **Client:**
  - `ui/src/chart/marginal.ts` (pure layout plus a thin canvas draw);
  - `ui/src/chart/indexed.ts` (pure ratio transform, range and ticks);
  - `yview.ts` gains an `indexed` branch;
  - `Panel.svelte` gets the marginal canvas, a `marginal:` row, the indexed buttons in the `y:` row, and notes.

**Tech Stack:** Python ≥3.12, uv, pydantic v2, polars, pyarrow, pytest; Svelte 5 + TypeScript + uPlot, vitest, Playwright.

**Spec and guide:**
- spec §6.4: marginal histogram on the y axis, now vs reference.
- spec §6.2: reference overlays (baseline ghost is "same window last week"); data mode is always labelled.
- spec §6.3: no dual y-axes.
- guide §1: indexed chart, ratios on a log axis with 1 in the middle (Wilke).
- guide §3: "Ratio vs baseline: log axis, 1 centred"; "Shape comparison now vs reference: histogram".
- guide §5: bins are source buckets, never split; variable-width bins are drawn as density; open-ended buckets get a strip, never a fake range; low n is faded.
- guide §2 rule 4: never average percentiles.

## Global Constraints

- Work on `master`. Commit after every task with the trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- After every task run `just lint` and `just test`. UI tasks also run `just ui-test`, `just ui-check` and `just ui-build`.
- **Never run `just e2e`**: it wipes the dev VictoriaMetrics volume. Run only the new spec, as `cd ui && npx playwright test e2e/marginal.spec.ts`, with `just dev-up` already running.
- **Never average percentiles.**
  - The marginal of a percentile panel uses the histogram behind it (a distribution of observations). If there is no histogram, it uses per-step percentile values with n ≥ n_min only, labelled "a distribution of p95 values per step, not of requests".
  - Indexed `window` is refused for quantile datasets.
- **Bins:**
  - Distribution marginals draw source buckets, merged only by whole adjacent buckets (counts are additive).
  - Sample marginals use fixed fine bins computed on the server over the union of both windows, so now and reference share their edges. They are merged the same way.
  - Bar length is **share per pixel of y** (density), so unequal buckets and windows with different n compare fairly.
- **Honesty marks cannot be turned off:**
  - the marginal header always shows `n` for both windows;
  - the basis line ("observations" vs "per-step values, not requests") is always shown in the notes;
  - mass outside the view or in open-ended buckets is shown as "≥ x% above/below", never drawn as a bar;
  - in indexed mode the badge and axis label always say what 1 means, and a rule is drawn at 1.
- **Time panels only** (layers all `line+envelope`), as for y-views. A heatmap marginal is a follow-up bead.
- **The marginal is off in indexed mode.** Indexed y is a ratio, and the marginal shows values. The toggle is disabled with a tooltip, not hidden.

## Decisions (justified; flagged for the user at the end)

1. **Basis is chosen automatically:**
   - **distribution** if `meta.histogram` is set (quantile panels built from `histogram_quantile` over a `_bucket` metric);
   - **samples** otherwise.

   For samples, the values are the panel dataset's **stored per-step `avg`** at the dataset step (not LOD), pooled across the drawn series. They are labelled `per-step values (1m means of scrape samples), 2 series pooled: not requests`. min/max are not used, because they would double-count each step.
2. **Reference selection** uses `reference_window(start, end, step, mode)`. A dataset's buckets cover `(ts−step, ts]` for ts in `[start, end]`, so the window length is `span = end − start + step`.
   - `previous`: shift = span. The reference ends exactly one step before the panel starts, with the same number of buckets and no overlap.
   - `week`: shift = 7 d. It is refused when span > 7 d (it would overlap) or when the step does not divide a week (the bucket grids would not align).
   - `profile`: refused with a hint pointing at the catalog operating profile (2as.7). The mode name is reserved for that.
3. **References are datasets, fetched once and stored in the spec.** `ensure_reference` calls the existing `query(...)`, and `query_distribution(...)` twice for histogram-backed panels (current and reference window, step `max(step, 2×resolution)`, as `distribution_panel` does).
   - This gives `dataset.created` provenance; Claude can run `fraction_over` on the reference distribution.
   - The marginal and the indexed view share these datasets per mode (`references["week"]` serves both).
   - No new table and no migration: old specs validate with defaults.
4. **Layout:**
   - The marginal is an 84 px canvas absolutely positioned at the right of `.plot`. The uPlot width shrinks by 84 px while it is on, so the plot is rebuilt on toggle.
   - It draws from the uPlot draw hook with `u.valToPos(v, "y")` and the plot bbox, so it follows every y-view (zero, band, log, meaningful) exactly.
   - Encoding (colour is never the only cue): now = filled bars; reference = dashed outline steps.
   - A two-line HTML header sits above the canvas: `now n=…` and `prev n=…`.
5. **Toggle and persistence:**
   - A `marginal: off | vs previous | vs last week` row under the `y:` row.
   - The choice is stored in `spec.marginal` and logged as `panel.marginal_set`: **ambient** for the user, internal for Claude (the y-view precedent).
   - Claude preselects with a new MCP tool `show_marginal(panel, reference, reason)`. Its reason appears in the row and in the notes.
6. **Indexed baselines:**
   - `window`: the count-weighted mean of bucket `avg`, which equals the mean of all samples in the window (mergeable).
   - `previous`/`week`: pointwise. The reference buckets are shifted onto the panel grid by `shift_ms` and then go through the **same LOD** as the panel (same range and step, so the grid is identical). The client divides avg, min and max by the base avg. Percentile series are gated on n ≥ n_min on **both** sides.
   - Series whose baseline is missing or ≤ 0 are **not indexed and are named**. If no series is left, the view is refused.
   - Steps without a usable baseline are gaps, counted in the badge.
   - Ratios ≤ 0 (current value ≤ 0) cannot be drawn on a log axis: they are hidden and counted.
7. **Indexed axis:**
   - The range is symmetric in log around 1, on nice multiples (1.1, 1.25, 1.5, 2, 3, 5, 10, …).
   - Ticks are `×0.5 … ×1 … ×2`.
   - The axis label is the baseline sentence, e.g. `ratio · 1 = each series' mean 12:00–15:00Z (log)`.
   - **Multiple datasets per panel are out of scope.** The real "latency vs throughput without a dual axis" case needs multi-dataset time panels; it is proposed as a new bead below. In this slice, indexed works across the series of one dataset (pods, endpoints, `by` breakdowns, percentile vs last week).

## File Structure

```
src/telemetry_nerd/analysis/reference.py      # NEW RefWindow, reference_window, WEEK_MS
src/telemetry_nerd/analysis/marginal.py       # NEW step_values, sample_bins, histogram_of, pooled_window
src/telemetry_nerd/charts/indexed.py          # NEW window_baselines, shifted, pointwise, check_index
src/telemetry_nerd/charts/yview.py            # MOD YMode += indexed, YView.baseline, INDEX_LABELS
src/telemetry_nerd/charts/spec.py             # MOD Reference, Marginal, ChartSpec.references/.marginal
src/telemetry_nerd/core/service.py            # MOD ensure_reference, set_marginal, panel_data marginal/index
src/telemetry_nerd/core/workspace_service.py  # MOD set_marginal, _check(indexed), reference= on y-view writes, brief
src/telemetry_nerd/core/events.py             # MOD AMBIENT_TYPES += panel.marginal_set
src/telemetry_nerd/channel/format.py          # MOD describe_event panel.marginal_set
src/telemetry_nerd/api/app.py                 # MOD POST /api/panels/{id}/marginal; y-view route accepts baseline
src/telemetry_nerd/mcp/server.py              # MOD show_marginal tool; suggest_y_view async + baseline; INSTRUCTIONS
ui/src/chart/marginal.ts                      # NEW marginalBars, scaleBars, marginalHeader, drawMarginal
ui/src/chart/indexed.ts                       # NEW indexSeries, ratioRange, ratioTicks, fmtRatio
ui/src/chart/yview.ts                         # MOD indexed branch in resolveY/offeredViews/badgeText
ui/src/lib/api.ts                             # MOD types, setMarginal(), selectYView baseline
ui/src/lib/panelNotes.ts                      # MOD marginal + indexed notes
ui/src/lib/workspace.svelte.ts                # MOD RELOAD_TYPES += panel.marginal_set
ui/src/Panel.svelte                           # MOD marginal canvas + row, indexed transform, rule at 1, refetch key
ui/src/index.css                              # MOD .marginal, .marginal-head
tests/unit/test_reference.py                  # NEW (acceptance: reference selection)
tests/unit/test_marginal.py                   # NEW
tests/unit/test_indexed.py                    # NEW
tests/unit/test_service_marginal.py           # NEW
tests/unit/test_workspace_service.py, test_api_workspace.py, test_mcp.py, test_yview.py, test_service.py  # MOD
ui/src/chart/marginal.test.ts, ui/src/chart/indexed.test.ts            # NEW
ui/src/chart/yview.test.ts, ui/src/lib/panelNotes.test.ts               # MOD
ui/e2e/marginal.spec.ts                       # NEW (marginal + indexed)
```

---

### Task 1: Reference window selection (Python, pure) [4ok.6]

**Files:** Create `src/telemetry_nerd/analysis/reference.py`, `tests/unit/test_reference.py`.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_reference.py
import pytest

from telemetry_nerd.analysis.reference import WEEK_MS, reference_window

H, M = 3_600_000, 60_000
START, END = 100 * H, 101 * H  # buckets at START..END inclusive, each covering (ts - step, ts]


def buckets(s, e, step):
    return set(range(s, e + 1, step))


def test_previous_is_adjacent_equal_length_and_never_overlaps():
    r = reference_window(START, END, M, "previous")
    assert (r.mode, r.shift_ms) == ("previous", H + M)
    assert r.end_ms + M == START  # last reference bucket ends one step before the first panel bucket
    assert len(buckets(r.start_ms, r.end_ms, M)) == len(buckets(START, END, M)) == 61
    assert not buckets(r.start_ms, r.end_ms, M) & buckets(START, END, M)
    assert r.label == "previous window"


def test_week_shifts_by_seven_days_on_the_same_grid():
    r = reference_window(START + 7 * 24 * H, END + 7 * 24 * H, 5 * M, "week")
    assert (r.start_ms, r.end_ms, r.shift_ms) == (START, END, WEEK_MS)
    assert r.label == "same window last week"


def test_week_refused_when_it_would_overlap_or_misalign():
    with pytest.raises(ValueError, match="longer than a week"):
        reference_window(0, 8 * 24 * H, H, "week")
    with pytest.raises(ValueError, match="does not divide a week"):
        reference_window(START, END, 11 * M, "week")


def test_profile_waits_for_the_catalog_and_bad_input_is_refused():
    with pytest.raises(ValueError, match="2as.7"):
        reference_window(START, END, M, "profile")
    with pytest.raises(ValueError, match="unknown reference"):
        reference_window(START, END, M, "yesterday")
    with pytest.raises(ValueError, match="window"):
        reference_window(END, START, M, "previous")
```

- [ ] **Step 2:** `uv run pytest tests/unit/test_reference.py -q` should fail with an ImportError.

- [ ] **Step 3: Implement**

```python
# src/telemetry_nerd/analysis/reference.py
"""Reference windows for "now vs reference" comparisons (spec §6.2/§6.4; bead 4ok.6).

Pure. A dataset's buckets sit at ts in [start, end] and each covers (ts - step, ts], so the
window is end - start + step long. A reference is the same grid shifted back by shift_ms."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RefMode = Literal["previous", "week"]
REF_MODES: tuple[str, ...] = ("previous", "week")
WEEK_MS = 7 * 86_400_000


@dataclass(frozen=True)
class RefWindow:
    mode: str
    start_ms: int
    end_ms: int
    shift_ms: int
    label: str


def reference_window(start_ms: int, end_ms: int, step_ms: int, mode: str) -> RefWindow:
    if step_ms <= 0 or end_ms < start_ms:
        raise ValueError("the panel window must have end >= start and a positive step")
    span = end_ms - start_ms + step_ms
    if mode == "previous":
        shift, label = span, "previous window"
    elif mode == "week":
        if WEEK_MS % step_ms:
            raise ValueError("the step does not divide a week, so last week's buckets do not "
                             "line up; use reference=previous or re-query at a standard step")
        if span > WEEK_MS:
            raise ValueError("the window is longer than a week, so last week overlaps it; "
                             "use reference=previous")
        shift, label = WEEK_MS, "same window last week"
    elif mode == "profile":
        raise ValueError("an operating-profile reference needs the learned profile "
                         "(catalog, bead 2as.7); use previous or week for now")
    else:
        raise ValueError(f"unknown reference {mode!r}: use previous or week")
    return RefWindow(mode, start_ms - shift, end_ms - shift, shift, label)
```

- [ ] **Step 4:** `uv run pytest tests/unit/test_reference.py -q && just lint`
- [ ] **Step 5: Commit:** `feat(analysis): reference window selection, previous/week (4ok.6)`

---

### Task 2: Marginal histograms (Python, pure) [4ok.6]

**Files:** Create `src/telemetry_nerd/analysis/marginal.py`, `tests/unit/test_marginal.py`.

**Produces:**
- `step_values(buckets, representation, n_min) -> (values, excluded, n_series)`
- `sample_bins(cur, ref, bins=96) -> list[(lo, hi)]`
- `histogram_of(values, edges) -> list[int]`
- `pooled_window(rows, cols, step_ms, start_ms, end_ms) -> dict | None`: one window over all series, counts summed.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_marginal.py
import math

import polars as pl
import pyarrow as pa

from telemetry_nerd.analysis.marginal import histogram_of, pooled_window, sample_bins, step_values
from telemetry_nerd.model.series import BUCKET_SCHEMA


def tbl(avg, count, sids=None):
    n = len(avg)
    return pa.table({"ts_ms": list(range(n)), "series_id": sids or ["a"] * n, "avg": avg,
                     "min": avg, "max": avg, "count": count}, schema=BUCKET_SCHEMA)


def test_step_values_pool_series_and_drop_low_n_percentiles():
    vals, excluded, k = step_values(tbl([0.4, None, 34.0, 0.9], [300, 5, 13, 400], ["a", "a", "b", "b"]),
                                    "quantile", 200)
    assert (sorted(vals), excluded, k) == ([0.4, 0.9], 1, 2)  # null is not a value; n=13 is excluded
    vals, excluded, _ = step_values(tbl([1.0, float("nan"), 2.0], [4, 4, 4]), "bucket_agg", None)
    assert (vals, excluded) == ([1.0, 2.0], 0)


def test_sample_bins_share_edges_and_go_log_past_two_decades():
    lin = sample_bins([1.0, 2.0], [3.0], bins=4)
    assert lin[0][0] == 1.0 and lin[-1][1] == 3.0 and len(lin) == 4
    log = sample_bins([0.01, 1.0], [5.0], bins=10)
    widths = [h / lo for lo, h in log]
    assert all(math.isclose(w, widths[0]) for w in widths)  # equal ratio = log-spaced
    assert sample_bins([2.0, 2.0], [], bins=8) == [(1.98, 2.02)]
    assert sample_bins([], []) == []


def test_histogram_counts_every_value_once_edges_inclusive():
    edges = sample_bins([1.0, 2.0, 3.0], [], bins=2)
    assert histogram_of([1.0, 2.0, 3.0], edges) == [2, 1]  # (lo, hi]; first bin also takes lo


def test_pooled_window_sums_series_counts():
    rows = pl.DataFrame({"series_id": ["a", "b"], "ts_ms": [60_000, 60_000], "bucket_lo": [0.0, 0.0],
                         "bucket_hi": [0.1, 0.1], "count": [3.0, 5.0]})
    cols = pl.DataFrame({"series_id": ["a", "b"], "ts_ms": [60_000, 60_000], "n": [3.0, 5.0]})
    w = pooled_window(rows, cols, 60_000, 0, 60_000)
    assert (w["n"], w["c"], w["series"]) == (8.0, [8.0], 2)
```

- [ ] **Step 2:** verify the tests fail.

- [ ] **Step 3: Implement**

```python
# src/telemetry_nerd/analysis/marginal.py
"""Marginal histograms: current vs reference value distribution (spec §6.4; bead 4ok.6).

Two bases, always labelled: observations from a histogram (source buckets, additive) or
the per-step values drawn on a time panel (scrape-derived samples, NOT requests)."""

from __future__ import annotations

import bisect

import polars as pl
import pyarrow as pa

from telemetry_nerd.analysis.distlod import window_histogram

SAMPLE_BINS = 96
SAMPLE_N_MIN = 20  # fewer step values than this: the shape is noise (faded, caveat)


def step_values(buckets: pa.Table, representation: str, n_min: int | None) -> tuple[list[float], int, int]:
    df = pl.from_arrow(buckets)
    assert isinstance(df, pl.DataFrame)
    df = df.filter(pl.col("avg").is_not_null() & pl.col("avg").is_finite())
    excluded = 0
    if representation == "quantile" and n_min is not None:
        ok = df["count"].fill_null(0) >= n_min
        excluded, df = int((~ok).sum()), df.filter(ok)
    return df["avg"].to_list(), excluded, df["series_id"].n_unique()


def sample_bins(cur: list[float], ref: list[float], bins: int = SAMPLE_BINS) -> list[tuple[float, float]]:
    vals = cur + ref
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    if lo == hi:
        pad = abs(lo) * 0.01 or 0.5
        return [(lo - pad, hi + pad)]
    if lo > 0 and hi / lo > 100:
        r = (hi / lo) ** (1 / bins)
        edges = [lo * r**i for i in range(bins + 1)]
    else:
        w = (hi - lo) / bins
        edges = [lo + w * i for i in range(bins + 1)]
    edges[0], edges[-1] = lo, hi
    return list(zip(edges[:-1], edges[1:], strict=True))


def histogram_of(values: list[float], edges: list[tuple[float, float]]) -> list[int]:
    his = [h for _, h in edges]
    c = [0] * len(edges)
    for v in values:
        c[min(bisect.bisect_left(his, v), len(edges) - 1)] += 1
    return c


def pooled_window(rows: pl.DataFrame, cols: pl.DataFrame, step_ms: int, start_ms: int, end_ms: int) -> dict | None:
    """All series summed (counts are additive): the distribution of every observation."""
    k = cols["series_id"].n_unique()
    rows = rows.with_columns(pl.lit("all").alias("series_id"))
    cols = cols.group_by("ts_ms").agg(pl.col("n").sum()).with_columns(pl.lit("all").alias("series_id"))
    w = window_histogram(rows, cols, step_ms, start_ms, end_ms).get("all")
    return None if w is None else {**w, "series": k}
```

`TelemetryService.fraction_over` pools series with the same three lines. Refactor it to call `pooled_window` in the same commit, so the pooling is written once.

- [ ] **Step 4:** `uv run pytest tests/unit/test_marginal.py tests/unit/test_fraction.py tests/unit/test_service_distribution.py -q && just lint`
- [ ] **Step 5: Commit:** `feat(analysis): marginal sample bins and pooled distribution window (4ok.6)`

---

### Task 3: Spec, reference datasets, set_marginal, panel data [4ok.6]

**Files:** Modify `charts/spec.py`, `core/service.py`, `core/workspace_service.py`, `core/events.py`, `channel/format.py`, `tests/unit/test_service.py` (spec equality gets `references: {}`, `marginal: None`). Create `tests/unit/test_service_marginal.py`.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_service_marginal.py
import pytest

from telemetry_nerd.channel.format import describe_event
from tests.unit.fakes import FakeSource, make_service


async def _time_panel(svc, expr="up"):
    ds = (await svc.query(expr, start="now-2h", end="now-1h", step="1m"))["dataset"]
    return ds, svc.show(ds, "Is it up?").panel.id


async def test_samples_marginal_vs_previous_is_labelled_and_reused(tmp_path):
    src = FakeSource()
    svc = make_service(tmp_path, src)
    ds, pid = await _time_panel(svc)
    out = await svc.set_marginal(pid, "previous", "user")
    assert out["basis"] == "samples" and "not requests" in out["what"]
    meta, ref = svc.datasets.meta(ds), svc.workspace.get_panel(pid).spec["references"]["previous"]
    assert ref["end_ms"] + meta.step_ms == meta.start_ms and ref["series"] != ds
    now, prev = svc.panel_data(pid, 800)["marginal"]["windows"]
    assert now["n"] == prev["n"] == 2 * 61 and sum(now["c"]) == now["n"]  # 2 series x 61 steps pooled
    assert now["lo"] == prev["lo"]  # shared edges
    ev = svc.log.since(0)[-1]
    assert (ev.type, ev.klass) == ("panel.marginal_set", "ambient")
    assert "marginal vs previous window" in describe_event(ev)
    calls = src.calls
    await svc.set_marginal(pid, "previous", "user")
    assert src.calls == calls  # the reference dataset is reused, not refetched


async def test_percentile_panel_uses_the_histogram_behind_it(tmp_path):
    svc = make_service(tmp_path, FakeSource())
    _, pid = await _time_panel(svc, "histogram_quantile(0.95, sum by (le) (rate(lat_seconds_bucket[5m])))")
    out = await svc.set_marginal(pid, "previous", "user")
    assert out["basis"] == "distribution" and "observations" in out["what"]
    m = svc.panel_data(pid, 800)["marginal"]
    assert m["windows"][0]["hi"][-1] is None or m["windows"][0]["hi"][-1] == float("inf")  # +Inf kept, never faked


async def test_off_heatmap_refusal_and_brief(tmp_path):
    svc = make_service(tmp_path, FakeSource())
    _, pid = await _time_panel(svc)
    await svc.set_marginal(pid, "week", "claude", reason="compare with last Tuesday")
    assert svc.ws.brief()["panels"][0]["marginal"] == "same window last week"
    await svc.set_marginal(pid, None, "user")
    assert svc.panel_data(pid, 800).get("marginal") is None
    d = (await svc.query_distribution("lat_seconds_bucket", start="now-2h", end="now-1h"))["dataset"]
    hp = svc.show(d, "How is latency distributed?").panel.id
    with pytest.raises(ValueError, match="time-series"):
        await svc.set_marginal(hp, "previous", "user")
```

Check first that `histogram_source()` maps the expression in the second test to `lat_seconds_bucket` (see `test_exprkind.py`), and adapt the selector if needed. JSON infinity: assert against whatever `window_histogram` emits for the `+Inf` edge and keep the intent: the open bucket is present, not dropped.

- [ ] **Step 2:** verify the tests fail.

- [ ] **Step 3: Implement.** `charts/spec.py`:

```python
class Reference(BaseModel):
    mode: Literal["previous", "week"]
    label: str
    start_ms: int
    end_ms: int
    shift_ms: int
    series: str  # time dataset over the reference window (same expr, step, source)
    dist: str | None = None  # distribution over the reference window (histogram-backed panels)
    dist_current: str | None = None  # distribution over the panel window


class Marginal(BaseModel):
    reference: Literal["previous", "week"]
    author: Literal["claude", "user"] = "user"
    reason: str | None = Field(default=None, max_length=160)

# ChartSpec:
    references: dict[str, Reference] = Field(default_factory=dict)
    marginal: Marginal | None = None
```

`core/service.py` (imports `reference_window`, `RefWindow`, the marginal helpers, `Reference`, `Marginal`, `ChartSpec`):

```python
    async def ensure_reference(self, panel_id: str, mode: str, actor: Actor) -> Reference:
        p = self.workspace.get_panel(panel_id)
        spec = ChartSpec.model_validate(p.spec)
        if any(layer.mark != "line+envelope" for layer in spec.layers):
            raise ValueError(f"marginals and indexed views apply to time-series panels; {p.id} is not one")
        if (ref := spec.references.get(mode)) is not None:
            return ref
        meta = self.datasets.meta(p.dataset_ids[0])
        rw = reference_window(meta.start_ms, meta.end_ms, meta.step_ms, mode)
        common = {"source": meta.source, "actor": actor}
        series = (await self.query(meta.expr, start=str(rw.start_ms), end=str(rw.end_ms),
                                   step=format_duration(meta.step_ms), **common))["dataset"]
        dist = dist_cur = None
        if meta.histogram:
            h, src = meta.histogram, self._source(meta.source)
            dstep = format_duration(max(meta.step_ms, 2 * src.resolution_ms))
            dist_cur = (await self.query_distribution(h["selector"], h["by"], start=str(meta.start_ms),
                                                      end=str(meta.end_ms), step=dstep, **common))["dataset"]
            dist = (await self.query_distribution(h["selector"], h["by"], start=str(rw.start_ms),
                                                  end=str(rw.end_ms), step=dstep, **common))["dataset"]
        return Reference(mode=mode, label=rw.label, start_ms=rw.start_ms, end_ms=rw.end_ms,  # type: ignore[arg-type]
                         shift_ms=rw.shift_ms, series=series, dist=dist, dist_current=dist_cur)

    async def set_marginal(self, panel_id: str, reference: str | None, actor: Actor,
                           reason: str | None = None) -> dict:
        if reference is None:
            self.ws.set_marginal(panel_id, None, None, actor)
            return {"panel": panel_id, "marginal": None}
        ref = await self.ensure_reference(panel_id, reference, actor)
        p = self.ws.set_marginal(panel_id, Marginal(reference=ref.mode, author=actor, reason=reason), ref, actor)  # type: ignore[arg-type]
        m = self._marginal(ChartSpec.model_validate(p.spec), self.datasets.meta(p.dataset_ids[0]))
        assert m is not None
        now, prev = m["windows"]
        return {"panel": p.id, "basis": m["basis"], "what": m["what"], "reference": ref.label,
                "n": {"now": now["n"], "reference": prev["n"]}, "datasets": [ref.series, ref.dist]}

    def _marginal(self, spec: ChartSpec, meta) -> dict | None:
        m = spec.marginal
        ref = spec.references.get(m.reference) if m else None
        if m is None or ref is None:
            return None
        head = {"reference": {"mode": ref.mode, "label": ref.label, "start_ms": ref.start_ms, "end_ms": ref.end_ms},
                "author": m.author, "reason": m.reason}
        if ref.dist and ref.dist_current:
            wins = []
            for did, label in ((ref.dist_current, "now"), (ref.dist, ref.label)):
                dm, dist = self.datasets.get_distribution(did)
                w = pooled_window(pl.from_arrow(dist.rows), pl.from_arrow(dist.columns), dm.step_ms,
                                  dm.start_ms - dm.step_ms, dm.end_ms)  # fmt: skip
                wins.append({"label": label, "start_ms": dm.start_ms, "end_ms": dm.end_ms, "n": 0.0,
                             "columns": 0, "lo": [], "hi": [], "c": [], **(w or {})})
            k = max(w.get("series", 1) for w in wins)
            what = (f"observations (requests) of {meta.histogram['selector']}"
                    + (f", {k} series summed" if k > 1 else ""))
            return {"basis": "distribution", "what": what, "n_min": DIST_N_MIN, "windows": wins,
                    "excluded": [0, 0], **head}
        _, cur = self.datasets.get(meta.id)
        rmeta, rres = self.datasets.get(ref.series)
        cv, cx, k = step_values(cur.buckets, meta.representation, meta.n_min)
        rv, rx, _ = step_values(rres.buckets, rmeta.representation, rmeta.n_min)
        edges = sample_bins(cv, rv)
        lo, hi = [a for a, _ in edges], [b for _, b in edges]
        step = format_duration(meta.step_ms)
        kind = (f"p{meta.quantile * 100:g} values per {step} step (n ≥ {meta.n_min} only): "
                "a distribution of percentile values, not of requests"
                if meta.representation == "quantile" else
                f"per-step values ({step} means of scrape samples): scrape samples, not requests")
        what = kind + (f"; {k} series pooled" if k > 1 else "")
        wins = [{"label": label, "start_ms": s, "end_ms": e, "n": float(len(v)), "columns": len(v),
                 "lo": lo, "hi": hi, "c": histogram_of(v, edges)}
                for label, s, e, v in (("now", meta.start_ms, meta.end_ms, cv),
                                       (ref.label, ref.start_ms, ref.end_ms, rv))]  # fmt: skip
        return {"basis": "samples", "what": what, "n_min": SAMPLE_N_MIN, "windows": wins,
                "excluded": [cx, rx], **head}
```

In `panel_data`'s time branch, add `"marginal": self._marginal(ChartSpec.model_validate(panel.spec), meta)`. Task 7 adds `"index"`. If `DatasetMeta` has no `id`, pass the dataset id explicitly.

`core/workspace_service.py`:

```python
    @atomic
    def set_marginal(self, panel_id: str, marginal: Marginal | None, reference: Reference | None,
                     actor: Actor) -> Panel:
        p, spec = self._time_spec(panel_id)
        if reference is not None:
            spec.references[reference.mode] = reference
        spec.marginal = marginal
        p = self.workspace.set_spec(p.id, spec.model_dump())
        self.log.append(actor, "panel.marginal_set", p.id, {
            "reference": marginal.reference if marginal else None,
            "label": reference.label if (marginal and reference) else None,
            "reason": marginal.reason if marginal else None,
        })
        return p
```

`brief()` panel entry: add `**({"marginal": ref["label"]} if (mg := p.spec.get("marginal")) and (ref := p.spec.get("references", {}).get(mg["reference"])) else {})`.

`events.py`: add `"panel.marginal_set"` to `AMBIENT_TYPES`.

`format.py`:

```python
        case "panel.marginal_set":
            if not p.get("reference"):
                return f"{who} turned off the marginal histogram on {e.object_id}"
            why = f": {p['reason']}" if p.get("reason") else ""
            return f"{who} showed {e.object_id} marginal vs {p['label']}{why}"
```

- [ ] **Step 4:** `just test && just lint`
- [ ] **Step 5: Commit:** `feat(service): panel reference datasets and marginal histogram data (4ok.6)`

---

### Task 4: HTTP route and MCP `show_marginal` [4ok.6]

**Files:** Modify `api/app.py`, `mcp/server.py`, `tests/unit/test_api_workspace.py`, `tests/unit/test_mcp.py` (and `test_mcp_instructions.py` if it pins tool names).

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_api_workspace.py
def test_marginal_route(client):
    pid = _seed_panel(client)
    r = client.post(f"/api/panels/{pid}/marginal", json={"reference": "previous"})
    assert r.status_code == 200 and r.json()["spec"]["marginal"]["reference"] == "previous"
    assert client.post(f"/api/panels/{pid}/marginal", json={"reference": "profile"}).status_code == 400
    assert client.post(f"/api/panels/{pid}/marginal", json={"reference": None}).json()["spec"]["marginal"] is None
    assert client.post("/api/panels/p99/marginal", json={"reference": "week"}).status_code == 404

# tests/unit/test_mcp.py
async def test_show_marginal(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://127.0.0.1:7070")
    ds = json.loads(text_of(await call(mcp, "query", {"expr": "up", "start": "now-2h", "end": "now-1h"})))["dataset"]
    pid = json.loads(text_of(await call(mcp, "show", {"dataset": ds, "question": "Up?"})))["panel"]
    out = json.loads(text_of(await call(mcp, "show_marginal",
                                        {"panel": pid, "reference": "previous", "reason": "did the level shift?"})))
    assert out["basis"] == "samples" and out["n"]["now"] > 0 and "not requests" in out["what"]
    bad = await call(mcp, "show_marginal", {"panel": pid, "reference": "profile", "reason": "r"})
    assert bad.is_error and "2as.7" in text_of(bad)
```

- [ ] **Step 2:** verify the tests fail.

- [ ] **Step 3: Implement.** Route, registered before `/api/panels/{id}/data`:

```python
    async def panel_marginal(request: Request) -> JSONResponse:
        body = await request.json() if await request.body() else {}
        ref = body.get("reference")
        if ref is not None and not isinstance(ref, str):
            return _error(400, "reference must be previous, week or null")
        try:
            await service.set_marginal(request.path_params["id"], ref, "user")
        except NotFound as e:
            return _error(404, str(e))
        except SourceError as e:
            return _error(400, str(e), hint=e.hint)
        except ValueError as e:
            return _error(400, str(e))
        return JSONResponse(service.workspace.get_panel(request.path_params["id"]).to_dict())
    ...
        Route("/api/panels/{id}/marginal", panel_marginal, methods=["POST"]),
```

Use `_body`/`_api` if they fit an async service call; the shape above matches `panel_distribution`.

MCP tool, after `suggest_y_view`:

```python
    @mcp.tool()
    async def show_marginal(panel: str, reference: str = "previous", reason: str = "", off: bool = False) -> str:
        """Show a marginal histogram beside a time-series panel: the current window's value
        distribution vs a reference window, on the panel's own y scale. reference: previous
        (window of equal length just before) or week (same window 7 days earlier).
        Histogram-backed panels compare OBSERVATIONS (requests); plain series compare
        per-step values (scrape samples, NOT requests): say which when you cite it, with n.
        reason: one line shown to the user. off=true hides it. Returns {basis, what, n, datasets}:
        the reference datasets are normal handles (e.g. fraction_over on the distribution one)."""
        try:
            out = await service.set_marginal(panel, None if off else reference, "claude", reason=reason or None)
            return _dump(out)
        except (ValidationError, NotFound, ValueError, SourceError) as e:
            raise _fail(e) from e
```

Add one `INSTRUCTIONS` line: "- To ask "is now different from before?" about a time panel, `show_marginal(panel, reference=previous|week)`; cite n for both windows and whether it is requests or per-step samples."

- [ ] **Step 4:** `just test && just lint`
- [ ] **Step 5: Commit:** `feat(api,mcp): marginal route and show_marginal tool (4ok.6)`

---

### Task 5: Client marginal layout (pure, `ui/src/chart/marginal.ts`) [4ok.6]

- [ ] **Step 1: Failing tests**

```ts
// ui/src/chart/marginal.test.ts
import { describe, expect, it } from "vitest";
import type { WindowHist } from "../lib/api";
import { marginalBars, marginalHeader, scaleBars } from "./marginal";

const W = (lo: (number | null)[], hi: (number | null)[], c: number[]): WindowHist => ({
  label: "now", start_ms: 0, end_ms: 1, n: c.reduce((a, b) => a + b, 0), columns: 1, lo, hi, c,
});
const lin = (v: number) => 100 - v * 3; // value 0 at px 100 (bottom), 30 at px 10 (top)

describe("marginal", () => {
  it("bars follow the y scale and shares are exact", () => {
    const m = marginalBars(W([0, 10, 20], [10, 20, 30], [1, 2, 1]), lin, 10, 100);
    expect(m.bars.map((b) => [b.y0, b.y1, b.share])).toEqual([[70, 100, 0.25], [40, 70, 0.5], [10, 40, 0.25]]);
    expect([m.above, m.below]).toEqual([0, 0]);
  });
  it("open-ended and out-of-view buckets are counted, never drawn with a fake range", () => {
    const m = marginalBars(W([0, 30, 40], [10, 40, null], [2, 1, 1]), lin, 10, 100);
    expect(m.bars).toHaveLength(1);
    expect([m.above, m.below]).toEqual([0.5, 0]); // (30,40] above the view + (40,+Inf)
  });
  it("thin buckets merge into whole-bucket unions of at least minPx", () => {
    const m = marginalBars(W([0, 0.25, 0.5, 0.75], [0.25, 0.5, 0.75, 1], [1, 1, 1, 1]), lin, 10, 100, 3);
    expect(m.bars).toEqual([{ y0: 97, y1: 100, share: 1, density: 1 / 3 }]);
  });
  it("a bucket straddling the view edge is clipped but keeps its true density", () => {
    const m = marginalBars(W([20], [40], [1]), lin, 10, 100);
    expect(m.bars[0]).toMatchObject({ y0: 10, y1: 40, density: 1 / 60 });
  });
  it("log axis: buckets reaching zero or below are counted below the view", () => {
    const log = (v: number) => (v > 0 ? 100 - 30 * Math.log10(v) : NaN);
    const m = marginalBars(W([0, 1], [1, 10], [1, 3]), log, 10, 100);
    expect(m.below).toBe(0.25);
    expect(m.bars).toHaveLength(1);
  });
  it("now and reference share one length scale; header always carries n", () => {
    const a = marginalBars(W([0], [10], [10]), lin, 10, 100), b = marginalBars(W([0, 10], [10, 20], [1, 1]), lin, 10, 100);
    const [la, lb] = scaleBars([a, b], 60);
    expect(la[0]).toBeCloseTo(60);
    expect(lb[0]).toBeCloseTo(30);
    expect(marginalHeader([{ ...W([0], [1], [1200]) }, { ...W([0], [1], [7]), label: "previous window" }], 20))
      .toEqual(["now n=1.2k", "previous window n=7 (too few)"]);
  });
});
```

- [ ] **Step 2:** `cd ui && npx vitest run src/chart/marginal.test.ts` should fail.

- [ ] **Step 3: Implement**

```ts
// ui/src/chart/marginal.ts — marginal histogram beside a time panel (spec §6.4, bead 4ok.6).
// Pure layout + a thin canvas draw. Bars are whole source buckets (or fine sample bins), merged
// only by whole adjacent buckets; length = share per px of y (density), so windows compare.
import type { WindowHist } from "../lib/api";

export interface MBar { y0: number; y1: number; share: number; density: number }
export interface MLayout { bars: MBar[]; above: number; below: number; n: number }

export function marginalBars(w: WindowHist, toPx: (v: number) => number, top: number, bottom: number, minPx = 3): MLayout {
  const t = w.c.reduce((a, b) => a + b, 0);
  const out: MLayout = { bars: [], above: 0, below: 0, n: w.n };
  if (!(t > 0)) return out;
  const idx = w.c.map((_, i) => i).sort((i, j) => (w.lo[i] ?? -Infinity) - (w.lo[j] ?? -Infinity));
  let acc: { top: number; bot: number; c: number } | null = null;
  const flush = () => {
    if (acc && acc.c > 0) {
      const share = acc.c / t;
      out.bars.push({ y0: Math.max(top, acc.top), y1: Math.min(bottom, acc.bot), share, density: share / (acc.bot - acc.top) });
    }
    acc = null;
  };
  for (const i of idx) {
    const c = w.c[i], lo = w.lo[i], hi = w.hi[i];
    if (hi === null) { out.above += c / t; continue; } // (e, +Inf): no range to draw
    if (lo === null) { out.below += c / t; continue; }
    const pLo = toPx(lo), pHi = toPx(hi); // screen y grows downward: pHi < pLo
    if (!Number.isFinite(pLo) || !Number.isFinite(pHi) || pHi > bottom) { flush(); out.below += c / t; continue; }
    if (pLo < top) { flush(); out.above += c / t; continue; }
    if (acc && acc.bot - acc.top < minPx) { acc.top = pHi; acc.c += c; continue; }
    flush();
    acc = { top: pHi, bot: pLo, c };
  }
  flush();
  return out;
}

/** One length scale for every window: the largest density fills `width`. */
export function scaleBars(layouts: MLayout[], width: number): number[][] {
  const max = Math.max(0, ...layouts.flatMap((l) => l.bars.map((b) => b.density)));
  return layouts.map((l) => l.bars.map((b) => (max > 0 ? (b.density / max) * width : 0)));
}

const fmtN = (n: number) => (n >= 1000 ? `${Number((n / 1000).toPrecision(2))}k` : `${Math.round(n)}`);
export const marginalHeader = (ws: WindowHist[], nMin: number): string[] =>
  ws.map((w) => `${w.label} n=${fmtN(w.n)}${w.n < nMin ? " (too few)" : ""}`);

export function drawMarginal(
  ctx: CanvasRenderingContext2D, ws: WindowHist[], nMin: number,
  o: { toPx: (v: number) => number; top: number; bottom: number; width: number; fg: string; muted: string },
): void {
  const layouts = ws.map((w) => marginalBars(w, o.toPx, o.top, o.bottom));
  const lens = scaleBars(layouts, o.width - 6);
  layouts.forEach((l, k) => {
    ctx.globalAlpha = ws[k].n < nMin ? 0.35 : 1;
    if (k === 0) { // now: filled
      ctx.fillStyle = o.fg;
      l.bars.forEach((b, i) => { ctx.globalAlpha *= 0.5; ctx.fillRect(2, b.y0, lens[k][i], b.y1 - b.y0); ctx.globalAlpha = ws[k].n < nMin ? 0.35 : 1; });
    } else { // reference: dashed outline steps
      ctx.strokeStyle = o.muted; ctx.setLineDash([3, 2]); ctx.beginPath();
      l.bars.forEach((b, i) => { ctx.moveTo(2, b.y1); ctx.lineTo(2 + lens[k][i], b.y1); ctx.lineTo(2 + lens[k][i], b.y0); ctx.lineTo(2, b.y0); });
      ctx.stroke(); ctx.setLineDash([]);
    }
    ctx.fillStyle = k === 0 ? o.fg : o.muted; ctx.font = "10px sans-serif"; ctx.globalAlpha = 1;
    if (l.above > 0) ctx.fillText(`▲≥${(100 * l.above).toPrecision(2)}%`, 2, o.top + 10 + 11 * k);
    if (l.below > 0) ctx.fillText(`▼≥${(100 * l.below).toPrecision(2)}%`, 2, o.bottom - 2 - 11 * k);
  });
}
```

Adjust the `toEqual` floats to the implementation (for example `density: 1/3` comes from `1 / (100 - 97)`), keeping each test's intent.

- [ ] **Step 4:** `just ui-test && just ui-check`
- [ ] **Step 5: Commit:** `feat(ui): pure marginal layout, merged whole buckets, shared length scale (4ok.6)`

---

### Task 6: Panel wiring for the marginal [4ok.6]

**Files:** Modify `ui/src/lib/api.ts`, `ui/src/lib/panelNotes.ts` (+ test), `ui/src/lib/workspace.svelte.ts`, `ui/src/Panel.svelte`, `ui/src/index.css`.

- [ ] **Step 1: Failing notes test**

```ts
  it("an active marginal always states its basis, n and who chose it", () => {
    const notes = panelNotes([], { yScaledToData: false, nMin: null,
      marginal: { what: "per-step values (1m means of scrape samples): scrape samples, not requests",
                  ref: "previous window", n: [122, 7], nMin: 20, author: "claude", reason: "did it shift?" } });
    expect(notes.map((n) => [n.kind, n.key])).toEqual([["info", "marginal"], ["caveat", "marginal_low_n"]]);
    expect(notes[0].text).toBe("Marginal (right): per-step values (1m means of scrape samples): scrape samples, not requests. Filled = now (n=122), dashed = previous window (n=7). Chosen by Claude: did it shift?");
  });
```

- [ ] **Step 2: Implement**

`api.ts`:
- `ChartSpec` gains `references?: Record<string, { mode: string; label: string; start_ms: number; end_ms: number; shift_ms: number; series: string; dist?: string | null }>` and `marginal?: { reference: "previous" | "week"; author?: string; reason?: string | null } | null`.
- Add `export interface MarginalData { basis: "distribution" | "samples"; what: string; n_min: number; windows: WindowHist[]; excluded: number[]; reference: { mode: string; label: string; start_ms: number; end_ms: number }; author: string; reason: string | null }`.
- `TimePanelData` gains `marginal?: MarginalData | null`.
- Add `export const setMarginal = (id: string, reference: "previous" | "week" | null) => postJSON<Panel>(\`/api/panels/${id}/marginal\`, { reference });`.

`panelNotes.ts`: add optional `marginal` to `opts`.
- Push info `marginal` with the text above.
- Push caveat `marginal_low_n` when either n < nMin: "The marginal has fewer than {nMin} values in a window; its shape is noise (drawn faded)."
- For samples with `excluded` > 0, the `what` text already says "n ≥ n_min only".

`workspace.svelte.ts`: add `"panel.marginal_set"` to `RELOAD_TYPES`, so Claude's preselection reloads the snapshot.

`Panel.svelte`:
1. **State.**
   ```ts
   const MARGINAL_W = 84;
   let margEl = $state<HTMLCanvasElement | null>(null);
   let margBusy = $state(false);
   const indexedOn = $derived(chosen?.mode === "indexed");
   const marg = $derived(data?.kind === "time" && !indexedOn ? (data.marginal ?? null) : null);
   const margW = $derived(marg ? MARGINAL_W : 0);
   // refetch panel data when what the server computes for it changes (marginal, indexed baseline)
   const dataKey = $derived(JSON.stringify([panel.spec.marginal ?? null, panel.spec.y.selected?.mode === "indexed" ? panel.spec.y.selected : null]));
   let lastKey: string | null = null;
   $effect(() => { const k = dataKey; if (lastKey !== null && k !== lastKey && fetchWidth) untrack(() => load(fetchWidth)); lastKey = k; });
   const toggleMarginal = (ref: "previous" | "week" | null) => {
     margBusy = true;
     setMarginal(panel.id, ref).catch((e) => (error = String(e))).finally(() => (margBusy = false));
   };
   ```
2. **Plot effect.**
   - Read `const m = untrack(() => marg)` and track `void margW`.
   - Use `width = (el.clientWidth || 800) - margW`; the ResizeObserver `setSize` also subtracts `margW`.
   - In the `draw` hook, after annotations:
     ```ts
     if (m && margEl) {
       const top = u.bbox.top / dpr, bottom = top + u.bbox.height / dpr;
       const ctx = setupCanvas(margEl, MARGINAL_W, el.clientHeight || 260);
       if (ctx) drawMarginal(ctx, m.windows, m.n_min, { toPx: (v) => u.valToPos(v, "y"), top, bottom, width: MARGINAL_W, fg: stroke, muted: grid });
     }
     ```
3. **Markup** inside `.plot`:
   ```svelte
   {#if marg}
     <canvas class="marginal" bind:this={margEl} data-marginal data-marginal-basis={marg.basis}
       title="{marg.what} · filled: now · dashed: {marg.reference.label}"></canvas>
     <span class="marginal-head" data-marginal-n>{#each marginalHeader(marg.windows, marg.n_min) as line (line)}<span>{line}</span>{/each}</span>
   {/if}
   ```
   Then a row after the `y:` row:
   ```svelte
   {#if data?.kind === "time"}
     <div class="legend y-views" role="group" aria-label="Marginal histogram">
       marginal:
       <button type="button" class:on={!panel.spec.marginal} onclick={() => toggleMarginal(null)}>off</button>
       {#each [["previous", "vs previous window"], ["week", "vs last week"]] as [r, label] (r)}
         <button type="button" disabled={indexedOn || margBusy} title={indexedOn ? "the marginal shows values; it is off in the indexed view" : ""}
           class:on={panel.spec.marginal?.reference === r} data-marginal-ref={r}
           onclick={() => toggleMarginal(r as "previous" | "week")}>{label}</button>
       {/each}
       {#if panel.spec.marginal?.author === "claude"}<span class="hint">Claude: {panel.spec.marginal.reason}</span>{/if}
       {#if margBusy}<span class="hint">fetching reference…</span>{/if}
     </div>
   {/if}
   ```
4. **Notes.** Pass `marginal: marg ? { what: marg.what, ref: marg.reference.label, n: marg.windows.map((w) => w.n), nMin: marg.n_min, author: marg.author, reason: marg.reason } : null`.

`index.css`:

```css
.panel .marginal { position: absolute; top: 0; right: 0; }
.panel .marginal-head { position: absolute; top: -2px; right: 2px; width: 80px; font-size: 10px; line-height: 11px; color: var(--muted); display: flex; flex-direction: column; text-align: right; }
.panel .plot:has(.marginal) .y-badge, .panel .plot:has(.marginal) .y-strip { right: 88px; }
```

- [ ] **Step 3:** `just ui-test && just ui-check && just ui-build`, then the visual check (see Task 9, step 2).
- [ ] **Step 4: Commit:** `feat(ui): marginal histogram beside time panels, now vs reference, n always shown (4ok.6)`

---

### Task 7: Indexed view, server side [4ok.14]

**Files:**
- Create `src/telemetry_nerd/charts/indexed.py` and `tests/unit/test_indexed.py`.
- Modify:
  - `charts/yview.py`;
  - `core/workspace_service.py` (`_check`, plus `reference=` on `suggest_y_view`/`select_y_view`);
  - `core/service.py` (`panel_data` `index` payload);
  - `api/app.py` (the y-view route takes `baseline` and ensures the reference first);
  - `mcp/server.py` (`suggest_y_view` becomes async and takes `baseline`);
  - `tests/unit/test_yview.py`, `tests/unit/test_workspace_service.py`.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_indexed.py
import pyarrow as pa
import pytest
from pydantic import ValidationError

from telemetry_nerd.analysis.reference import WEEK_MS
from telemetry_nerd.charts.indexed import check_index, shifted, window_baselines
from telemetry_nerd.charts.yview import YView
from telemetry_nerd.model.series import BUCKET_SCHEMA


def tbl(rows):  # (ts, series, avg, count)
    return pa.table({"ts_ms": [r[0] for r in rows], "series_id": [r[1] for r in rows],
                     "avg": [r[2] for r in rows], "min": [r[2] for r in rows], "max": [r[2] for r in rows],
                     "count": [r[3] for r in rows]}, schema=BUCKET_SCHEMA)


def test_indexed_view_needs_a_baseline_and_only_indexed_takes_one():
    YView(mode="indexed", label="÷ own mean", baseline="window")
    with pytest.raises(ValidationError, match="baseline"):
        YView(mode="indexed", label="i")
    with pytest.raises(ValidationError, match="baseline"):
        YView(mode="log", label="l", baseline="week")


def test_window_baseline_is_the_count_weighted_mean():
    assert window_baselines(tbl([(1, "a", 1.0, 1), (2, "a", 4.0, 3)])) == {"a": 3.25}


def test_window_refused_for_percentiles():
    with pytest.raises(ValueError, match="average percentiles"):
        check_index("window", "quantile", 200, tbl([(1, "a", 1.0, 300)]), None, {})


def test_non_positive_or_missing_baseline_is_named_and_all_bad_is_refused():
    t = tbl([(1, "a", 0.0, 4), (1, "b", 2.0, 4)])
    assert check_index("window", "bucket_agg", None, t, None, {"a": 'instance="a"'}) == [
        'not indexed (baseline missing or ≤ 0): instance="a"']
    with pytest.raises(ValueError, match="nothing to index"):
        check_index("window", "bucket_agg", None, tbl([(1, "a", -1.0, 4)]), None, {})


def test_pointwise_week_baseline_shifts_onto_the_grid_and_gates_n():
    cur = tbl([(WEEK_MS + 1, "a", 2.0, 300), (WEEK_MS + 2, "a", 2.0, 300)])
    ref = shifted(tbl([(1, "a", 1.0, 300), (2, "a", 1.0, 50)]), WEEK_MS)
    assert ref["ts_ms"].to_pylist() == [WEEK_MS + 1, WEEK_MS + 2]
    assert check_index("week", "quantile", 200, cur, ref, {}) == ["1 step(s) without a usable baseline are not drawn"]
    with pytest.raises(ValueError, match="no .*week.* reference"):
        check_index("week", "bucket_agg", None, cur, None, {})
```

Append to `test_workspace_service.py`:

```python
async def test_indexed_selection_window_and_week(svc):
    pid = await _panel(svc)
    p = svc.ws.select_y_view(pid, "user", mode="indexed", baseline="window")
    assert p.spec["y"]["selected"] == {**p.spec["y"]["selected"], "mode": "indexed", "baseline": "window", "label": "÷ own mean"}
    data = svc.panel_data(pid, 800)["index"]
    assert data["baseline"] == "window" and data["label"].startswith("1 = each series' mean over")
    ref = await svc.ensure_reference(pid, "previous", "user")
    svc.ws.select_y_view(pid, "user", mode="indexed", baseline="previous", reference=ref)
    ix = svc.panel_data(pid, 800)["index"]
    assert ix["series"][0]["ts"] == svc.panel_data(pid, 800)["series"][0]["ts"]  # same grid after shift + LOD
```

- [ ] **Step 2:** verify the tests fail.

- [ ] **Step 3: Implement.** `charts/yview.py`:
- `YMode` gains `"indexed"`.
- `YView` gains `baseline: Literal["window", "previous", "week"] | None = None`.
- In `_shape`, add: `if (self.mode == "indexed") != (self.baseline is not None): raise ValueError("indexed views need a baseline (window, previous or week); other views take none")`.
- Add `INDEX_LABELS = {"window": "÷ own mean", "previous": "÷ previous window", "week": "÷ last week"}`.
- `check_view` returns `[]` for `indexed`; its checks need the data (see `check_index`).

```python
# src/telemetry_nerd/charts/indexed.py
"""Indexed view: each series as a ratio to a common baseline, log axis, 1 centred (bead 4ok.14;
guide §1, §3). The sanctioned alternative to dual y-axes. Pure.

A ratio needs a baseline > 0: series without one are not indexed and are named. Percentile
series are never indexed to a window mean (that averages percentiles), only pointwise with
n >= n_min on both sides."""

from __future__ import annotations

import polars as pl
import pyarrow as pa
import pyarrow.compute as pc


def _drawn(t: pa.Table) -> pl.DataFrame:
    df = pl.from_arrow(t)
    assert isinstance(df, pl.DataFrame)
    return df.filter(pl.col("avg").is_not_null() & pl.col("avg").is_finite())


def window_baselines(buckets: pa.Table) -> dict[str, float | None]:
    """Count-weighted mean of bucket means = the mean of every sample in the window (mergeable)."""
    df = _drawn(buckets).with_columns(pl.col("count").fill_null(0))
    n = pl.col("count").sum()
    g = df.group_by("series_id").agg(
        pl.when(n > 0).then((pl.col("avg") * pl.col("count")).sum() / n).otherwise(None).alias("b"))
    return dict(zip(g["series_id"].to_list(), g["b"].to_list(), strict=True))


def shifted(ref: pa.Table, shift_ms: int) -> pa.Table:
    """Reference buckets moved onto the panel's time grid."""
    i = ref.schema.get_field_index("ts_ms")
    return ref.set_column(i, ref.schema.field(i), pc.add(ref["ts_ms"], shift_ms))


def check_index(baseline: str, representation: str, n_min: int | None, cur: pa.Table,
                ref_on_grid: pa.Table | None, names: dict[str, str]) -> list[str]:
    quantile = representation == "quantile"
    ids = sorted(set(cur["series_id"].to_pylist()))
    hidden = 0
    if baseline == "window":
        if quantile:
            raise ValueError("a percentile series cannot be indexed to its mean over the window: that "
                             "would average percentiles; use baseline=previous or week (pointwise, n-gated)")
        b = window_baselines(cur)
        bad = [s for s in ids if (b.get(s) or 0) <= 0]
    else:
        if ref_on_grid is None:
            raise ValueError(f"no {baseline} reference fetched for this panel yet")
        c = _drawn(cur).select("series_id", "ts_ms")
        r = _drawn(ref_on_grid).select("series_id", "ts_ms", pl.col("avg").alias("base"), pl.col("count").alias("bn"))
        ok = pl.col("base").fill_null(0) > 0
        if quantile and n_min is not None:
            ok = ok & (pl.col("bn").fill_null(0) >= n_min)
        j = c.join(r, on=["series_id", "ts_ms"], how="left").with_columns(ok.alias("ok"))
        per = dict(j.group_by("series_id").agg(pl.col("ok").sum()).iter_rows())
        bad = [s for s in ids if per.get(s, 0) == 0]
        hidden = int(j.filter(~pl.col("series_id").is_in(bad) & ~pl.col("ok")).height)
    if len(bad) == len(ids):
        raise ValueError("no series has a baseline > 0 (missing, zero or negative): nothing to index")
    out = []
    if bad:
        out.append(f"not indexed (baseline missing or ≤ 0): {', '.join(names.get(s, s) for s in bad)}")
    if hidden:
        out.append(f"{hidden} step(s) without a usable baseline are not drawn")
    return out
```

`workspace_service.py`:
- `_check(p, view, spec)`:
  - for `indexed`, take `ref = spec.references.get(view.baseline)`;
  - shift it with `shifted(self.datasets.get(ref.series)[1].buckets, ref.shift_ms)` when present;
  - names come from `result.series` labels (reuse `_series_labels` logic);
  - then call `check_index(...)`.
- `select_y_view` gains `baseline: str | None = None, reference: Reference | None = None`.
  - Store `spec.references[reference.mode] = reference` before `_check`.
  - Build the builtin view with `label = INDEX_LABELS[baseline]` when mode is indexed.
  - The event payload gains `baseline`.
- `suggest_y_view` gains `reference` the same way.

`service.py` `panel_data` time branch: add `"index": self._index(spec, meta, result, width_px)`.

```python
    def _index(self, spec: ChartSpec, meta, result, width_px: int) -> dict | None:
        v = spec.y.selected
        if v is None or v.mode != "indexed":
            return None
        if v.baseline == "window":
            a, b = iso(meta.start_ms - meta.step_ms)[11:16], iso(meta.end_ms)[11:16]
            return {"baseline": "window", "label": f"1 = each series' mean over {a}–{b}Z",
                    "values": window_baselines(result.buckets)}
        ref = spec.references.get(v.baseline)
        if ref is None:
            return {"baseline": v.baseline, "label": "", "refused": f"no {v.baseline} reference fetched"}
        _, rres = self.datasets.get(ref.series)
        table = shifted(rres.buckets, ref.shift_ms)
        if meta.representation != "quantile":  # same range and step as the panel => identical LOD grid
            table, _ = lod(table, meta.step_ms, TimeRange(meta.start_ms, meta.end_ms), width_px)
        series = [{"id": sid, "ts": g["ts_ms"].to_list(), "avg": g["avg"].to_list(), "count": g["count"].to_list()}
                  for (sid,), g in pl.DataFrame(table).group_by("series_id", maintain_order=True)]
        when = "last week" if ref.mode == "week" else "in the previous window"
        return {"baseline": v.baseline, "label": f"1 = the same series {when} (point by point)", "series": series}
```

Route `/api/panels/{id}/y-view`: accept `baseline` (a string). If `mode == "indexed"` and the baseline is `previous` or `week`, call `ref = await service.ensure_reference(id, baseline, "user")` first, then `ws.select_y_view(..., baseline=baseline, reference=ref)`.

MCP `suggest_y_view` becomes `async def` with `baseline: str | None = None`. It does the same `ensure_reference(…, "claude")` before `ws.suggest_y_view(..., reference=ref)`. Add to the docstring: "indexed (needs baseline: window = each series' own mean, previous/week = same series point by point; log axis, 1 centred; THE way to compare series of different scales or units. Never a dual axis)".

- [ ] **Step 4:** `just test && just lint`
- [ ] **Step 5: Commit:** `feat(charts,service): indexed y-view with window/previous/week baselines (4ok.14)`

---

### Task 8: Indexed view, client [4ok.14]

**Files:** Create `ui/src/chart/indexed.ts`, `ui/src/chart/indexed.test.ts`. Modify `ui/src/chart/yview.ts` (+ test), `ui/src/lib/api.ts`, `ui/src/lib/panelNotes.ts` (+ test), `ui/src/Panel.svelte`.

- [ ] **Step 1: Failing tests**

```ts
// ui/src/chart/indexed.test.ts
import { describe, expect, it } from "vitest";
import type { SeriesData } from "../lib/api";
import { fmtRatio, indexSeries, ratioRange, ratioTicks } from "./indexed";

const s = (id: string, avg: (number | null)[], count = avg.map(() => 300)): SeriesData => ({
  id, labels: { instance: id }, ts: avg.map((_, i) => i * 1000), avg,
  min: avg.map((v) => (v === null ? null : v - 0.5)), max: avg.map((v) => (v === null ? null : v + 0.5)), count,
});
const Q = { quantile: false, nMin: null };

describe("indexed", () => {
  it("window baseline divides value and envelope by the series' own mean", () => {
    const r = indexSeries([s("a", [1, 2, 3])], { baseline: "window", label: "1 = mean", values: { a: 2 } }, Q);
    expect(r.series[0].avg).toEqual([0.5, 1, 1.5]);
    expect(r.series[0].min).toEqual([0.25, 0.75, 1.25]);
  });
  it("series without a baseline > 0 are skipped and named; none left is refused", () => {
    const r = indexSeries([s("a", [1]), s("b", [1])], { baseline: "window", label: "", values: { a: 0, b: 2 } }, Q);
    expect(r.series.map((x) => x.id)).toEqual(["b"]);
    expect(r.skipped).toEqual(['{instance="a"}']);
    expect(indexSeries([s("a", [1])], { baseline: "window", label: "", values: {} }, Q).refused).toMatch(/baseline/);
  });
  it("pointwise: matched by series and time; missing, ≤ 0 or low-n baselines are gaps and counted", () => {
    const base = [{ ...s("a", [1, 0, 2, 4], [300, 300, 300, 10]) }];
    const r = indexSeries([s("a", [2, 2, 2, 2])], { baseline: "week", label: "", series: base }, { quantile: true, nMin: 200 });
    expect(r.series[0].avg).toEqual([2, null, 1, null]);
    expect(r.hidden).toBe(2);
  });
  it("ratios ≤ 0 cannot sit on a log axis: hidden and counted", () => {
    const r = indexSeries([s("a", [-1, 2])], { baseline: "window", label: "", values: { a: 1 } }, Q);
    expect(r.series[0].avg).toEqual([null, 2]);
    expect(r.nonPositive).toBe(1);
  });
  it("range is symmetric around 1 on nice multiples; ticks and labels read as multipliers", () => {
    const [lo, hi] = ratioRange([0.8, 1.4]);
    expect(hi).toBe(1.5);
    expect(lo).toBeCloseTo(1 / 1.5);
    expect(ratioTicks(2).map((t) => +t.toFixed(2))).toEqual([0.5, 0.67, 0.8, 1, 1.25, 1.5, 2]);
    expect([fmtRatio(1.5), fmtRatio(2 / 3), fmtRatio(1)]).toEqual(["×1.5", "×0.67", "×1"]);
  });
});
```

Append to `yview.test.ts`:

```ts
  it("indexed: log, symmetric around 1, badge says what 1 means", () => {
    const st = yStats([q([0.8, 1.4], [500, 500])], { quantile: false, nMin: null });
    const r = resolveY(view("indexed", { baseline: "window" }), st);
    expect(r).toMatchObject({ log: true, zoomed: false, refused: null });
    expect(r.range![1]).toBe(1.5);
    expect(badgeText(view("indexed", { baseline: "window" }), r, null, "1 = each series' mean over 12:00–15:00Z"))
      .toBe("indexed · 1 = each series' mean over 12:00–15:00Z");
  });
```

- [ ] **Step 2:** verify the tests fail.

- [ ] **Step 3: Implement**

```ts
// ui/src/chart/indexed.ts — indexed view (bead 4ok.14): series ÷ common baseline, log axis, 1 centred.
import type { SeriesData } from "../lib/api";
import { seriesName } from "./toUplot";

export interface IndexPayload {
  baseline: "window" | "previous" | "week"; label: string; refused?: string;
  values?: Record<string, number | null>;
  series?: { id: string; ts: number[]; avg: (number | null)[]; count: (number | null)[] }[];
}
export interface Indexed { series: SeriesData[]; skipped: string[]; hidden: number; nonPositive: number; refused: string | null }

export function indexSeries(series: SeriesData[], ix: IndexPayload, o: { quantile: boolean; nMin: number | null }): Indexed {
  const out: Indexed = { series: [], skipped: [], hidden: 0, nonPositive: 0, refused: ix.refused ?? null };
  if (out.refused) return out;
  const refs = new Map((ix.series ?? []).map((r) => [r.id, new Map(r.ts.map((t, i) => [t, i]))]));
  const refById = new Map((ix.series ?? []).map((r) => [r.id, r]));
  for (const s of series) {
    let base: (i: number) => number | null;
    if (ix.values) {
      const b = ix.values[s.id];
      if (b == null || !(b > 0)) { out.skipped.push(seriesName(s.labels)); continue; }
      base = () => b;
    } else {
      const r = refById.get(s.id), at = refs.get(s.id);
      if (!r || !at) { out.skipped.push(seriesName(s.labels)); continue; }
      base = (i) => {
        const j = at.get(s.ts[i]);
        if (j === undefined) return null;
        const v = r.avg[j], n = r.count[j] ?? 0;
        return v !== null && v > 0 && (!o.quantile || o.nMin === null || n >= o.nMin) ? v : null;
      };
    }
    const div = (arr: (number | null)[], count: boolean) => arr.map((v, i) => {
      if (v === null) return null;
      const b = base(i);
      if (b === null) { if (count) out.hidden++; return null; }
      const r = v / b;
      if (!(r > 0)) { if (count) out.nonPositive++; return null; }
      return r;
    });
    out.series.push({ ...s, avg: div(s.avg, true), min: div(s.min, false), max: div(s.max, false) });
  }
  if (!out.series.length) out.refused = "no series has a baseline > 0 (missing, zero or negative)";
  return out;
}

const NICE = [1.1, 1.25, 1.5, 2, 3, 5, 10, 20, 50, 100, 1000];
export function ratioRange(values: number[]): [number, number] {
  const m = Math.max(1, ...values.filter((v) => v > 0).map((v) => Math.max(v, 1 / v))) * 1.02;
  const n = NICE.find((x) => x >= m) ?? 10 ** Math.ceil(Math.log10(m));
  return [1 / n, n];
}
export function ratioTicks(n: number): number[] {
  const up = NICE.filter((x) => x <= n * 1.0001).slice(-3);
  return [...up.map((x) => 1 / x).reverse(), 1, ...up];
}
export const fmtRatio = (r: number): string => `×${Number(r.toPrecision(r >= 1 ? 3 : 2))}`;
```

`yview.ts`:
- `resolveY` gets `case "indexed": range = ratioRange(st.values); break;` and `log: v.mode === "log" || v.mode === "indexed"`.
- `zoomed` is false for indexed.
- `badgeText(v, r, unit, indexLabel?: string)` returns `indexed · ${indexLabel}` for indexed, with clipped counts appended as today.
- `offeredViews` adds three offers with `baseline`:
  - `÷ own mean`: disabled for quantile, with the title "percentiles cannot be averaged over the window; use ÷ last week";
  - `÷ previous window`;
  - `÷ last week`.
  - Each title reads: "each series as a ratio to a common baseline; log axis, 1 centred (instead of a second y axis)".
- `Offer` gains `baseline?`, and the Panel loop key becomes `o.mode + (o.baseline ?? "")`.

`api.ts`:
- `YView.mode` adds `"indexed"` and `baseline?`;
- `TimePanelData.index?: IndexPayload | null`;
- `selectYView`'s body takes `baseline?`.

`panelNotes.ts`: add `indexed?: { label: string; skipped: string[]; hidden: number; nonPositive: number } | null`.
- Info `indexed`: `Indexed: ${label}. Log ratio axis, 1 = no change; ×2 and ×0.5 are equally far from 1.`
- Caveat `indexed_skipped` when `skipped` is non-empty, naming the series.
- Caveat `indexed_gaps` when `hidden + nonPositive > 0`.

`Panel.svelte`:
- `const ix = $derived(data?.kind === "time" && indexedOn && data.index ? indexSeries(data.series, data.index, { quantile: …, nMin: … }) : null);`
- `const drawn = $derived(ix && !ix.refused ? ix.series : data?.kind === "time" ? data.series : []);`
- `yst` and `toUplot` read `drawn`.
- A refused index sets `yres.refused`, so the view falls back to auto plus a caveat, the existing path.
- In the indexed plot config:
  - y axis `label: \`ratio · ${data.index.label} (log)\``;
  - `values: (_u, ts) => ts.map(fmtRatio)`;
  - `splits: () => ratioTicks(yres.range![1])`.
- In the `draw` hook, when indexed, stroke a 1 px solid `stroke` line at `u.valToPos(1, "y")` across the plot and label it "1 = baseline".
- Indexed buttons call `pick({ mode: "indexed", baseline }, undefined)`. There is no optimistic state: the ratios need the server payload, so show the "fetching baseline…" hint until the snapshot and refetch arrive. Use `pending` only for non-indexed views.

- [ ] **Step 4:** `just ui-test && just ui-check && just ui-build`, then the visual check (Task 9).
- [ ] **Step 5: Commit:** `feat(ui): indexed view, ratio to baseline on a log axis with 1 centred (4ok.14)`

---

### Task 9: e2e, live checks, close

- [ ] **Step 1: e2e spec**

```ts
// ui/e2e/marginal.spec.ts
import { expect, test } from "@playwright/test";
import { seedPanel } from "./helpers.js";

test("marginal now vs previous, n shown, ambient event; indexed view labels 1", async ({ page, request }) => {
  const panel = await seedPanel(request, "Did demo latency's level shift against the previous window?");
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  const row = el.getByRole("group", { name: "Marginal histogram" });
  await row.getByRole("button", { name: "vs previous window" }).click();
  await expect(el.locator("[data-marginal]")).toHaveAttribute("data-marginal-basis", "samples");
  await expect(el.locator("[data-marginal-n]")).toContainText("now n=");
  await expect(el.locator('[data-note="marginal"]')).toContainText("not requests");
  const events = await (await request.get("/api/events?since=0")).json();
  expect(events.events.filter((e: { type: string }) => e.type === "panel.marginal_set").at(-1))
    .toMatchObject({ klass: "ambient", object_id: panel.id, payload: { reference: "previous" } });
  await page.reload();
  await expect(el.locator("[data-marginal]")).toBeVisible();

  const y = el.getByRole("group", { name: "Y-axis view" });
  await y.getByRole("button", { name: "÷ own mean" }).click();
  await expect(el.locator("[data-y-badge]")).toContainText("indexed · 1 = each series' mean over");
  await expect(el.locator("[data-marginal]")).toHaveCount(0); // marginal is off in indexed view
  await expect(row.getByRole("button", { name: "vs previous window" })).toBeDisabled();
  await y.getByRole("button", { name: "auto" }).click();
  await expect(el.locator("[data-marginal]")).toBeVisible();
});
```

Run (VictoriaMetrics up, no volume wipe): `just ui-build && cd ui && npx playwright test e2e/marginal.spec.ts`.

- [ ] **Step 2: Live check with dev data.**
  - Seed more than a week so `week` has data: `just dev-up && just seed 192`. That is 8 days at 15 s. If it is slow, accept it once; the dev volume persists.
  - Restart the daemon (`just serve`).
  - In the UI or through `/api`:
    1. `query tn_demo_latency_seconds` over `now-3h..now-10m` at 1m, then show. Toggle `vs previous window` and then `vs last week`.
       - The marginal follows the y-view: switch to `log`, then `band`; the "▲≥x% above" markers appear when the band clips.
       - Light and dark themes both look right.
       - The badge and strip do not overlap the marginal.
    2. `query histogram_quantile(0.95, sum by (le) (rate(tn_demo_request_duration_seconds_bucket[$__rate_interval])))`, then show.
       - The marginal basis is `distribution`, and the note says "observations (requests) of tn_demo_request_duration_seconds_bucket".
       - The top marker shows the +Inf share, if any.
       - `÷ own mean` is disabled, and `÷ last week` works point by point with low-n gaps counted.
    3. `query rate(tn_demo_requests_total[5m])` (several instances), then `÷ own mean`.
       - The axis label reads "ratio · 1 = each series' mean over …".
       - The rule sits at 1 and the ticks read ×0.67 … ×1.5.
  - **Claude path:** run `scripts/mcp_call.py --url http://127.0.0.1:7070/mcp` and call:
    - `show_marginal(panel=<p>, reference="week", reason="is today's level normal for this hour?")`: the row shows "Claude: …", and the result carries n and `datasets`;
    - then `suggest_y_view(panel=<p>, mode="indexed", baseline="week", label="vs last week", reason="compare instances relative to their own last-week level")`.
    - Clicking the pill delivers an ambient `panel.y_view_selected` with the next intentional event.
- [ ] **Step 3: Full suite:** `just test && just lint && just ui-test && just ui-check`
- [ ] **Step 4: Commit and close**

```bash
git add ui/e2e/marginal.spec.ts
git commit -m "test(e2e): marginal now vs reference and indexed view (4ok.6, 4ok.14)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
bd close telemetry-nerd-4ok.6 --reason "Marginal histogram (distribution for histogram-backed panels, labelled per-step samples otherwise), previous/week reference, toggle + show_marginal; reference selection unit-tested"
bd close telemetry-nerd-4ok.14 --reason "Indexed y-view: window/previous/week baselines, log axis 1 centred, refusals for baseline <= 0/missing and percentile window means"
```

---

## Acceptance

- **4ok.6:**
  - A time panel shows a toggleable marginal histogram (off / vs previous window / vs last week) on the right. It shares the plot's y scale and follows every y-view.
  - Now is filled and the reference is a dashed outline. n is shown for both windows; low n is faded with a caveat.
  - Mass outside the view or in open buckets is shown as "≥ x% above/below".
  - The basis is always stated: "observations (requests)" for histogram-backed panels, or "per-step values … scrape samples, not requests".
  - The choice persists and is an ambient `panel.marginal_set`. Claude preselects with `show_marginal`.
  - `reference_window` is unit-tested: adjacent and equal length, no overlap, week shift, week refusals, profile deferred to 2as.7.
- **4ok.14:**
  - `÷ own mean`, `÷ previous window` and `÷ last week` views draw ratios on a log axis symmetric around 1, with a rule at 1, ×-ticks, and an axis label plus badge saying what 1 means.
  - Series with a baseline ≤ 0 or missing are named and not indexed; if none is left, the view is refused.
  - `÷ own mean` is refused for percentiles.
  - Claude suggests it through `suggest_y_view(mode="indexed", baseline=…)`.

## Beads

- **4ok.6** covers Tasks 1–6 and the 4ok.6 half of Task 9. **4ok.14** covers Tasks 7–8 and its half of Task 9; it depends on 4ok.6 Tasks 1 and 3 (the reference datasets). Add `bd dep add telemetry-nerd-4ok.14 telemetry-nerd-4ok.6`.
- **New, P2:** "Multi-dataset time panels (`show(dataset, also=[…])`); mixed units only in the indexed view". This is the real latency-vs-throughput dual-axis replacement. Validator rule `mixed_units`: an error unless `y.selected.mode == "indexed"`, otherwise small multiples.
- **New, P3:** "Marginal on heatmap panels" (Hartmann's HDR slide: heatmap plus marginal), reusing `pooled_window` and `marginal.ts`.
- **New, P3, blocked by 2as.7:** "Operating-profile reference" (mode `profile` in `reference_window`).
- **New, P3:** "Index to a brushed window": a SelectionMenu action that sets `baseline=window` with lo/hi.
- **2as.10 (existing):** the baseline ghost overlay ("same window last week") can reuse `spec.references["week"]` with no extra fetch.

## Risks and decisions for the user

1. **Samples basis is per-step means, not raw scrapes.** At step > scrape interval each value is a mean of samples, and the note says so. The alternative is to fetch the reference and current at the scrape resolution: truer samples, but up to 40× more points for a week. Recommended: keep the step and the honest label.
2. **Pooling series.** Sample marginals pool every drawn series ("k series pooled"); distribution marginals sum counts, which is exact. The alternative is one marginal per series (up to 5 × 2 outlines in 84 px), which is unreadable. Your call if you want a series picker.
3. **Reference datasets are real datasets.** Each toggle that needs new data creates one to three datasets and their `dataset.created` events (actor user or claude). They are reused per mode afterwards. The upside is provenance and Claude can cite them; the downside is a bit of noise in `workspace_activity`.
4. **Marginal persists in the spec (shared with Claude) rather than local UI state** like the heatmap colour controls. That is needed for Claude to preselect it. The user's toggle is ambient, so Claude learns of it without being interrupted.
5. **Indexed `window` baseline is the whole panel window**, so 1 = the series' own mean over what you see. That is easy to read but moves with the range. "Index to a brushed window" is a follow-up bead.
6. **Indexed is one dataset in this slice.** Its headline use (different units on one axis) needs the multi-dataset bead. Within one dataset it already covers "which pod moved most relative to its own normal" and "p95 vs last week, point by point".
7. **uPlot specifics to verify early in Task 8:** custom `splits` and `values` on a `distr: 3` scale with a non-decade range (for example [0.67, 1.5]). The fallback is linear in log10(ratio) with formatted ticks, which looks the same.
8. **Dev data for `week`** needs `just seed 192` (more than a week). The e2e spec only uses `previous`, so it does not depend on a week of history.

### Critical Files for Implementation
- /Users/avishai/code/telemetry-nerd/src/telemetry_nerd/core/service.py (ensure_reference, set_marginal, panel_data `marginal`/`index`)
- /Users/avishai/code/telemetry-nerd/src/telemetry_nerd/core/workspace_service.py (set_marginal, `_check` for indexed, reference= on y-view writes)
- /Users/avishai/code/telemetry-nerd/src/telemetry_nerd/charts/yview.py and charts/spec.py (YView `indexed`/`baseline`, Reference, Marginal), plus new analysis/reference.py, analysis/marginal.py, charts/indexed.py
- /Users/avishai/code/telemetry-nerd/ui/src/Panel.svelte (marginal canvas/row, indexed transform, rule at 1, refetch key), with new ui/src/chart/marginal.ts and ui/src/chart/indexed.ts
- /Users/avishai/code/telemetry-nerd/src/telemetry_nerd/mcp/server.py (show_marginal, async suggest_y_view with baseline, INSTRUCTIONS)
