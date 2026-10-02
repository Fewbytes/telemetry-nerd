# Missing Data: Series Bundles, Localized Caveats, `bucket_state` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make missing and untrusted data visible and machine-checkable. Every time-series and
distribution dataset gets a `bucket_state` companion; caveats become structured and localized;
panels draw stepped lines, a coverage rug, heatmap textures and window coverage badges; Claude's
summaries and the findings validator see coverage.

**Architecture:** `bucket_state` is computed on read from data we already store: bucket `count`,
the dataset's resolution, its grid, and new failed-chunk spans. Pure polars functions handle
compute, coarsen and merge. A small companion registry declares how each op carries each
companion. Structured `Caveat` objects carry `where` (time spans, series). They travel next to
the existing string caveat codes, so nothing breaks. The UI turns them into a rug, textures,
badges and footer↔graph highlighting.

**Tech Stack:** Python 3.14, polars 1.44, pyarrow 25, pydantic, pytest + hypothesis, DuckDB;
Svelte 5 + TypeScript, uPlot, vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-02-series-bundles-missing-data-design.md`
(read it first). Background: `docs/telemetry-graphing-guide.md` §5a, `docs/data-source-quirks.md`.

**Scope:** spec phases 1–4 (§10). Not in this plan:
- phase 5, source profiles: needs spike `telemetry-nerd-1h9.10`
- phase 6, group cloud: needs the group-view design

The `RESET`, `INTERVAL_CHANGE`, `STALE_MARKER` and `SOURCE_FILLED` flags are defined here, but
nothing sets them until phase 5.

## Global Constraints

- Run Python with `uv run`. Tests: `uv run pytest tests/unit -q`. Lint: `just lint`. UI:
  `cd ui && npx vitest run`, `just ui-check` (svelte-check, fails on warnings), e2e `cd ui && npx playwright test` (needs `just dev-up` + daemon).
- Search with `rg` and `fd`, never `grep` or `find`.
- Track work with `bd`, never TodoWrite. Commit after each task. Work on `master`, no branches.
- End every commit message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **The working tree may contain someone else's uncommitted changes** (seen in `core/service.py`,
  `core/panel_payloads.py`, `api/app.py`, `charts/spec.py`, `mcp/server.py`, `core/workspace_service.py`).
  Never revert or stage them. Stage only the files your task names, using `git add <path>`; when
  a file has foreign hunks, use `git add -p`. Find code by function name, not line number.
- Bulk companion data never enters Claude's context: summaries are compact (spec §3.4).
- No colour-only encoding. States are told apart by pattern. Neutral greys must be ≥ 3:1
  against `--bg` in both themes (spec §7.5).
- An all-ok `bucket_state` draws nothing and adds no caveat (spec §3.3).

## Decisions this plan makes beyond the spec (review these)

1. **Leading vs trailing silence.** Before a series' first sample in the window it is `absent`
   (not born yet). After its last sample it is `empty` (silent), not `absent`. We cannot prove a
   series ended until phase 5 adds staleness markers. Silence is the conservative reading, and it
   is the case we care about most: the OOM-killed pod.
2. **Partial tolerates jitter.** A bucket is `partial` when observed < expected − max(1,
   0.1·expected). The rule allows one sample of scrape jitter, so 3 of 4 counts as ok while 2 of
   4 is partial. Spec §5.1 says "< 0.9"; this is the jitter-safe form of the same rule.
3. **Coarsen classifies from sub-bucket states, not from sums.** Spec §5.3 says "worst inside".
   Re-applying the jitter tolerance to summed observed/expected would turn all-`ok` data
   `partial` (each sub-bucket is within jitter, the sum of their shortfalls is not). So `observed`
   and `expected` are summed (absent sub-buckets add nothing) but the state comes from the
   sub-bucket states: `unknown` if any is unknown (it is left out of the `observed`/`expected` sums:
   neither present nor missing), `absent` only when every one is, `empty` when none is `ok`/`partial`, `partial`
   when any alive one is `partial` or `empty`, else `ok`. This keeps coarsening associative and
   consistent with merge; a 10-minute bucket missing one minute reads partial, not empty. The spec
   (§5.3) says the same.
4. **`Bundle` is built on read; `FetchResult` is not replaced.** Bundles are assembled where
   panels and summaries are built. The only change to `FetchResult` is a new `failed` field.
   This avoids touching every source and cache path in this plan.
5. **Window views get a badge with a tooltip, not a mini rug.** Spec §7.3 mentions "click →
   mini rug". The tooltip lists the missing spans instead; the mini rug waits for evidence that
   we need it. Task 13 updates the spec.
6. **Source-side aggregation.** A query that aggregates at the source (`sum by (...) (...)`) hides
   member coverage. Such panels get an info caveat `member_coverage_unknown`.

## File Structure

| file | responsibility |
|---|---|
| `src/telemetry_nerd/model/bucket_state.py` (new) | `State`, `Flag`, schemas, `compute`, `coarsen`, `merge` |
| `src/telemetry_nerd/model/caveats.py` (new) | `Caveat`, `Where`, `runs`, `series_name`, `from_bucket_state` |
| `src/telemetry_nerd/model/companions.py` (new) | companion registry, op policies, `Bundle`, `dataset_bundle` |
| `src/telemetry_nerd/model/series.py` | `FetchResult.failed` |
| `src/telemetry_nerd/datasets/cache.py` | partial chunk failure → failed spans |
| `src/telemetry_nerd/datasets/store.py` | `DatasetMeta.failed_spans` |
| `src/telemetry_nerd/core/summary.py` | per-series `coverage`, `untrusted_data`/`missing_data` codes |
| `src/telemetry_nerd/core/panel_payloads.py` | `state_payload`, heatmap state, window coverage |
| `src/telemetry_nerd/core/service.py` | time panel `bucket_state` + `located` |
| `src/telemetry_nerd/core/workspace_service.py` | findings coverage check |
| `ui/src/lib/api.ts` | `BucketStatePayload`, `Caveat`, payload fields |
| `ui/src/lib/panelNotes.ts` | located caveats → notes with `where` |
| `ui/src/chart/toUplot.ts` | stepped data transform |
| `ui/src/chart/rug.ts` (new) | rug layout, drawing, hit test, hint text |
| `ui/src/Panel.svelte` | rug canvas, hover hint, footer↔graph focus, window badges |
| `ui/src/components/HeatmapPlot.svelte` | state textures, rug, focus |
| `src/telemetry_nerd/devtools/synthetic.py` | gappy + late-born demo series for e2e |
| `ui/e2e/missing-data.spec.ts` (new) | e2e |

---

### Task 1: `bucket_state` compute

**Files:**
- Create: `src/telemetry_nerd/model/bucket_state.py`
- Test: `tests/unit/test_bucket_state.py`

**Interfaces:**
- Produces:
  - `State(IntEnum)`: OK=0, PARTIAL=1, EMPTY=2, ABSENT=3, UNKNOWN=4
  - `Flag(IntFlag)`: RESET=1, INTERVAL_CHANGE=2, STALE_MARKER=4, SOURCE_FILLED=8
  - `STATE_SCHEMA`: (ts_ms int64, series_id string, observed float64, expected float64, state uint8, flags uint16)
  - `FailedSpan = tuple[int, int, str]`: inclusive bucket-ts range plus reason
  - `grid(start_ms, end_ms, step_ms) -> list[int]`
  - `compute(buckets: pa.Table, series_ids: Sequence[str], *, start_ms: int, end_ms: int, step_ms: int, resolution_ms: int, mode: Literal["samples", "presence"], failed: Sequence[FailedSpan] = ()) -> pa.Table`
  - `PARTIAL_RATIO = 0.9`

Bucket timestamps are bucket **ends**; a bucket covers `(ts − step, ts]`. The grid is inclusive
on both ends, like `summary.py` and cache reads. `samples` mode compares `count` (samples seen)
with `step / resolution`. `presence` mode (quantile datasets, distribution columns) treats a
non-null value as observed = expected = 1.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_bucket_state.py
import pyarrow as pa
import pytest

from telemetry_nerd.model.bucket_state import STATE_SCHEMA, State, compute, grid
from telemetry_nerd.model.series import BUCKET_SCHEMA

STEP, RES = 60_000, 15_000  # expected 4 samples per bucket


def buckets(rows):
    """rows: (ts_ms, series_id, avg, count)"""
    return pa.table(
        {
            "ts_ms": [r[0] for r in rows],
            "series_id": [r[1] for r in rows],
            "avg": [r[2] for r in rows],
            "min": [r[2] for r in rows],
            "max": [r[2] for r in rows],
            "count": [r[3] for r in rows],
        },
        schema=BUCKET_SCHEMA,
    )


def states(table, sid="a"):
    return [s for s, i in zip(table["state"].to_pylist(), table["series_id"].to_pylist()) if i == sid]


def run(rows, sids=("a",), mode="samples", failed=(), start=STEP, end=5 * STEP):
    return compute(buckets(rows), sids, start_ms=start, end_ms=end, step_ms=STEP,
                   resolution_ms=RES, mode=mode, failed=failed)


def test_grid_is_inclusive_and_step_aligned():
    assert grid(STEP + 1, 3 * STEP, STEP) == [2 * STEP, 3 * STEP]


def test_full_buckets_are_ok_and_one_sample_of_jitter_is_tolerated():
    out = run([(t * STEP, "a", 1.0, 4 if t != 3 else 3) for t in range(1, 6)])
    assert out.schema == STATE_SCHEMA
    assert states(out) == [State.OK] * 5


def test_two_of_four_samples_is_partial():
    out = run([(t * STEP, "a", 1.0, 2 if t == 2 else 4) for t in range(1, 6)])
    assert states(out)[1] == State.PARTIAL


def test_hole_is_empty_and_leading_absence_is_absent():
    rows = [(t * STEP, "a", 1.0, 4) for t in (3, 5)]  # born at 3, hole at 4
    assert states(run(rows)) == [State.ABSENT, State.ABSENT, State.OK, State.EMPTY, State.OK]


def test_trailing_silence_is_empty_not_absent():
    rows = [(t * STEP, "a", 1.0, 4) for t in (1, 2)]
    assert states(run(rows))[2:] == [State.EMPTY] * 3


def test_failed_span_is_unknown_with_zero_observed():
    rows = [(t * STEP, "a", 1.0, 4) for t in range(1, 6)]
    out = run(rows, failed=[(2 * STEP, 3 * STEP, "timeout")])
    assert states(out) == [State.OK, State.UNKNOWN, State.UNKNOWN, State.OK, State.OK]
    assert out["observed"].to_pylist()[1] == 0.0


def test_presence_mode_uses_non_null_values():
    rows = [(STEP, "a", 1.0, 0), (2 * STEP, "a", None, 7), (3 * STEP, "a", 2.0, 0)]
    assert states(run(rows, mode="presence", end=3 * STEP)) == [State.OK, State.EMPTY, State.OK]


def test_series_never_seen_is_empty_everywhere():
    out = run([(STEP, "a", 1.0, 4)], sids=("a", "b"), end=2 * STEP)
    assert states(out, "b") == [State.EMPTY, State.EMPTY]


@pytest.mark.parametrize("sids", [(), ("a",)])
def test_empty_inputs(sids):
    out = compute(buckets([]), sids, start_ms=STEP, end_ms=STEP - 1 if sids else STEP,
                  step_ms=STEP, resolution_ms=RES, mode="samples")
    assert out.num_rows == 0 and out.schema == STATE_SCHEMA
```

- [ ] **Step 2: Run and check they fail**

Run: `uv run pytest tests/unit/test_bucket_state.py -q`
Expected: FAIL, `ModuleNotFoundError: telemetry_nerd.model.bucket_state`.

- [ ] **Step 3: Implement**

```python
# src/telemetry_nerd/model/bucket_state.py
"""bucket_state: per series and bucket, how much of the expected data arrived and whether it can
be trusted (spec 2026-10-02 §5). Computed on read from bucket counts; never stored."""

from __future__ import annotations

from collections.abc import Sequence
from enum import IntEnum, IntFlag
from typing import Literal

import polars as pl
import pyarrow as pa


class State(IntEnum):
    OK = 0
    PARTIAL = 1
    EMPTY = 2  # alive, no samples (also: silent after the last sample; spec decision 1)
    ABSENT = 3  # before the first sample in the window: not alive yet
    UNKNOWN = 4  # fetch failed / source cannot tell


class Flag(IntFlag):
    NONE = 0
    RESET = 1
    INTERVAL_CHANGE = 2
    STALE_MARKER = 4
    SOURCE_FILLED = 8


PARTIAL_RATIO = 0.9

STATE_SCHEMA = pa.schema(
    [
        ("ts_ms", pa.int64()),
        ("series_id", pa.string()),
        ("observed", pa.float64()),
        ("expected", pa.float64()),
        ("state", pa.uint8()),
        ("flags", pa.uint16()),
    ]
)

Mode = Literal["samples", "presence"]
FailedSpan = tuple[int, int, str]  # inclusive bucket-ts range [a, b], reason


def grid(start_ms: int, end_ms: int, step_ms: int) -> list[int]:
    """Bucket end timestamps: multiples of step within [start, end], inclusive."""
    first = -(-start_ms // step_ms) * step_ms
    return list(range(first, end_ms + 1, step_ms))


def short(observed: pl.Expr, expected: pl.Expr) -> pl.Expr:
    """Below expected by more than jitter: one sample, or (1 - PARTIAL_RATIO) of a long bucket."""
    return observed < expected - pl.max_horizontal(pl.lit(1.0), (1 - PARTIAL_RATIO) * expected)


def compute(
    buckets: pa.Table,
    series_ids: Sequence[str],
    *,
    start_ms: int,
    end_ms: int,
    step_ms: int,
    resolution_ms: int,
    mode: Mode,
    failed: Sequence[FailedSpan] = (),
) -> pa.Table:
    ts = grid(start_ms, end_ms, step_ms)
    if not ts or not series_ids:
        return STATE_SCHEMA.empty_table()
    expected = max(1.0, step_ms / resolution_ms) if mode == "samples" else 1.0
    base = pl.DataFrame({"series_id": list(series_ids)}, schema={"series_id": pl.String}).join(
        pl.DataFrame({"ts_ms": ts}, schema={"ts_ms": pl.Int64}), how="cross"
    )
    rows = pl.from_arrow(buckets)
    seen = (
        pl.col("count").cast(pl.Float64)
        if mode == "samples"
        else pl.col("avg").fill_nan(None).is_not_null().cast(pl.Float64)
    )
    obs = rows.select("ts_ms", "series_id", seen.alias("observed"))
    df = base.join(obs, on=["series_id", "ts_ms"], how="left").with_columns(
        pl.col("observed").fill_null(0.0), pl.lit(expected).alias("expected")
    )
    first = (
        df.filter(pl.col("observed") > 0)
        .group_by("series_id")
        .agg(pl.col("ts_ms").min().alias("first_seen"))
    )
    df = df.join(first, on="series_id", how="left")
    unknown = pl.lit(False)
    for a, b, _reason in failed:
        unknown = unknown | pl.col("ts_ms").is_between(a, b)
    state = (
        pl.when(unknown)
        .then(int(State.UNKNOWN))
        .when(pl.col("first_seen").is_not_null() & (pl.col("ts_ms") < pl.col("first_seen")))
        .then(int(State.ABSENT))
        .when(pl.col("observed") == 0)
        .then(int(State.EMPTY))
        .when(short(pl.col("observed"), pl.col("expected")))
        .then(int(State.PARTIAL))
        .otherwise(int(State.OK))
    )
    out = (
        df.with_columns(
            state.cast(pl.UInt8).alias("state"),
            pl.lit(0, pl.UInt16).alias("flags"),
            pl.when(unknown).then(0.0).otherwise(pl.col("observed")).alias("observed"),
        )
        .sort("series_id", "ts_ms")
        .select(STATE_SCHEMA.names)
    )
    return out.to_arrow().cast(STATE_SCHEMA)
```

- [ ] **Step 4: Run and check they pass**

Run: `uv run pytest tests/unit/test_bucket_state.py -q`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/telemetry_nerd/model/bucket_state.py tests/unit/test_bucket_state.py
git add src/telemetry_nerd/model/bucket_state.py tests/unit/test_bucket_state.py
git commit -m "feat(model): bucket_state compute from counts, grid and failed spans

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `coarsen` and `merge` algebra

**Files:**
- Modify: `src/telemetry_nerd/model/bucket_state.py`
- Modify: `docs/superpowers/specs/2026-10-02-series-bundles-missing-data-design.md` §5.3
- Test: `tests/unit/test_bucket_state_algebra.py`

**Interfaces:**
- Consumes: `State`, `STATE_SCHEMA`, `short` (Task 1)
- Produces:
  - `coarsen(states: pa.Table, new_step_ms: int) -> pa.Table` (STATE_SCHEMA)
  - `GROUP_SCHEMA`: (ts_ms int64, group string, alive int32, reporting int32, observed float64, expected float64, state uint8, flags uint16, silent list<string>)
  - `merge(states: pa.Table, group_of: Mapping[str, str]) -> pa.Table` (GROUP_SCHEMA)

Classification shared by both: if no member/sub-bucket is alive → ABSENT. Else, if any is UNKNOWN →
UNKNOWN. Else, if summed observed == 0 → EMPTY. Else, if partial (in merge also: reporting < alive)
→ PARTIAL. Else OK. Absent members/sub-buckets contribute neither observed nor expected.

- [ ] **Step 1: Write the failing tests (examples + properties)**

```python
# tests/unit/test_bucket_state_algebra.py
import random

import pyarrow as pa
from hypothesis import given, settings
from hypothesis import strategies as st

from telemetry_nerd.model.bucket_state import STATE_SCHEMA, State, coarsen, merge

STEP = 60_000


def table(rows):
    """rows: (ts_ms, series_id, observed, expected, state, flags)"""
    cols = list(zip(*rows)) if rows else [[]] * 6
    return pa.table(dict(zip(STATE_SCHEMA.names, cols)), schema=STATE_SCHEMA)


def as_rows(t):
    return sorted(zip(*(t[c].to_pylist() for c in t.schema.names)), key=lambda r: (r[1], r[0]))


def test_coarsen_sums_and_reclassifies():
    t = table([(STEP, "a", 4, 4, State.OK, 0), (2 * STEP, "a", 0, 4, State.EMPTY, 1)])
    [row] = as_rows(coarsen(t, 2 * STEP))
    assert row[:4] == (2 * STEP, "a", 4.0, 8.0) and row[4] == State.PARTIAL and row[5] == 1


def test_coarsen_unknown_absorbs_and_absent_needs_all():
    t = table([(STEP, "a", 0, 4, State.UNKNOWN, 0), (2 * STEP, "a", 4, 4, State.OK, 0),
               (3 * STEP, "a", 0, 4, State.ABSENT, 0), (4 * STEP, "a", 0, 4, State.ABSENT, 0)])
    out = as_rows(coarsen(t, 2 * STEP))
    assert [r[4] for r in out] == [State.UNKNOWN, State.ABSENT]


def test_merge_counts_alive_reporting_and_silent():
    t = table([(STEP, "a", 4, 4, State.OK, 0), (STEP, "b", 0, 4, State.EMPTY, 0),
               (STEP, "c", 0, 4, State.ABSENT, 0)])
    [row] = merge(t, {"a": "g", "b": "g", "c": "g"}).to_pylist()
    assert (row["alive"], row["reporting"], row["silent"]) == (2, 1, ["b"])
    assert row["state"] == State.PARTIAL and row["expected"] == 8.0


states_st = st.sampled_from([State.OK, State.PARTIAL, State.EMPTY, State.ABSENT, State.UNKNOWN])


@st.composite
def state_tables(draw, n_series=3, n_buckets=8):
    rows = []
    for k in range(n_series):
        for i in range(1, n_buckets + 1):
            s = draw(states_st)
            obs = {State.OK: 4, State.PARTIAL: 2, State.EMPTY: 0, State.ABSENT: 0, State.UNKNOWN: 0}[s]
            rows.append((i * STEP, f"s{k}", float(obs), 4.0, int(s), draw(st.integers(0, 15))))
    return table(rows)


@settings(max_examples=60)
@given(state_tables())
def test_coarsen_is_associative(t):
    assert as_rows(coarsen(coarsen(t, 2 * STEP), 4 * STEP)) == as_rows(coarsen(t, 4 * STEP))


@settings(max_examples=60)
@given(state_tables())
def test_merge_ignores_row_order(t):
    rows = t.to_pylist()
    random.Random(1).shuffle(rows)
    shuffled = pa.Table.from_pylist(rows, schema=STATE_SCHEMA)
    g = {f"s{k}": "g" for k in range(3)}
    assert merge(t, g).sort_by("ts_ms").to_pylist() == merge(shuffled, g).sort_by("ts_ms").to_pylist()


@settings(max_examples=60)
@given(state_tables())
def test_unknown_member_makes_group_unknown(t):
    g = {f"s{k}": "g" for k in range(3)}
    by_ts = {}
    for r in t.to_pylist():
        by_ts.setdefault(r["ts_ms"], []).append(r["state"])
    for r in merge(t, g).to_pylist():
        members = by_ts[r["ts_ms"]]
        if State.UNKNOWN in members and any(m != State.ABSENT for m in members):
            assert r["state"] == State.UNKNOWN


@settings(max_examples=60)
@given(state_tables())
def test_all_absent_member_changes_nothing(t):
    g = {f"s{k}": "g" for k in range(3)}
    ghost = table([(i * STEP, "ghost", 0.0, 4.0, int(State.ABSENT), 0) for i in range(1, 9)])
    both = pa.concat_tables([t, ghost])
    assert merge(both, {**g, "ghost": "g"}).sort_by("ts_ms").to_pylist() == merge(t, g).sort_by("ts_ms").to_pylist()
```

- [ ] **Step 2: Run and check they fail**

Run: `uv run pytest tests/unit/test_bucket_state_algebra.py -q`
Expected: FAIL, `ImportError: cannot import name 'coarsen'`.

- [ ] **Step 3: Implement (append to `bucket_state.py`)**

```python
from collections.abc import Mapping

GROUP_SCHEMA = pa.schema(
    [
        ("ts_ms", pa.int64()),
        ("group", pa.string()),
        ("alive", pa.int32()),
        ("reporting", pa.int32()),
        ("observed", pa.float64()),
        ("expected", pa.float64()),
        ("state", pa.uint8()),
        ("flags", pa.uint16()),
        ("silent", pa.list_(pa.string())),
    ]
)

_ALIVE = pl.col("state") != int(State.ABSENT)


def _classify(any_alive: pl.Expr, any_unknown: pl.Expr, partial: pl.Expr) -> pl.Expr:
    return (
        pl.when(~any_alive)
        .then(int(State.ABSENT))
        .when(any_unknown)
        .then(int(State.UNKNOWN))
        .when(pl.col("observed") == 0)
        .then(int(State.EMPTY))
        .when(partial | short(pl.col("observed"), pl.col("expected")))
        .then(int(State.PARTIAL))
        .otherwise(int(State.OK))
        .cast(pl.UInt8)
    )


def coarsen(states: pa.Table, new_step_ms: int) -> pa.Table:
    """Merge buckets into coarser ones ending at multiples of new_step_ms (as resample.rebucket)."""
    if states.num_rows == 0:
        return states
    k = new_step_ms
    out = (
        pl.from_arrow(states)
        .with_columns(((pl.col("ts_ms") + k - 1) // k * k).alias("ts_ms"))
        .group_by(["series_id", "ts_ms"])
        .agg(
            pl.col("observed").filter(_ALIVE).sum().alias("observed"),
            pl.col("expected").filter(_ALIVE).sum().alias("expected"),
            _ALIVE.any().alias("_alive"),
            (pl.col("state") == int(State.UNKNOWN)).any().alias("_unknown"),
            pl.col("flags").bitwise_or().alias("flags"),
        )
        .with_columns(_classify(pl.col("_alive"), pl.col("_unknown"), pl.lit(False)).alias("state"))
        .sort("series_id", "ts_ms")
        .select(STATE_SCHEMA.names)
    )
    return out.to_arrow().cast(STATE_SCHEMA)


def merge(states: pa.Table, group_of: Mapping[str, str]) -> pa.Table:
    """Combine member series per bucket (spec §5.2). Absent members are not in the denominator."""
    if states.num_rows == 0:
        return GROUP_SCHEMA.empty_table()
    reporting = pl.col("state").is_in([int(State.OK), int(State.PARTIAL)])
    out = (
        pl.from_arrow(states)
        .with_columns(
            pl.col("series_id").replace_strict(dict(group_of), default=None).alias("group")
        )
        .drop_nulls("group")
        .group_by(["group", "ts_ms"])
        .agg(
            _ALIVE.sum().cast(pl.Int32).alias("alive"),
            reporting.sum().cast(pl.Int32).alias("reporting"),
            pl.col("observed").filter(_ALIVE).sum().alias("observed"),
            pl.col("expected").filter(_ALIVE).sum().alias("expected"),
            (pl.col("state") == int(State.UNKNOWN)).any().alias("_unknown"),
            pl.col("flags").bitwise_or().alias("flags"),
            pl.col("series_id").filter(pl.col("state") == int(State.EMPTY)).sort().alias("silent"),
        )
        .with_columns(
            _classify(
                pl.col("alive") > 0, pl.col("_unknown"), pl.col("reporting") < pl.col("alive")
            ).alias("state")
        )
        .sort("group", "ts_ms")
        .select(GROUP_SCHEMA.names)
    )
    return out.to_arrow().cast(GROUP_SCHEMA)
```

- [ ] **Step 4: Run and check they pass**

Run: `uv run pytest tests/unit/test_bucket_state_algebra.py tests/unit/test_bucket_state.py -q`
Expected: all pass. If `bitwise_or` is missing on the polars version, use
`pl.col("flags").fold(...)`. But polars 1.44 has `Expr.bitwise_or()` as an aggregation, so check
the version before changing anything.

- [ ] **Step 5: Update the spec's coarsen rule**

In `docs/superpowers/specs/2026-10-02-series-bundles-missing-data-design.md` §5.3, replace
"state = worst inside, except `absent` only if the whole coarse bucket is absent. Order (worst first):
`unknown` > `empty` > `partial` > `ok`." with:

"state is re-classified from the summed observed/expected (same rule as §5.1), except: `unknown` if
any sub-bucket is `unknown`, and `absent` only if every sub-bucket is absent (absent sub-buckets add
nothing to the sums). This keeps coarsening associative and consistent with §5.2."

- [ ] **Step 6: Commit**

```bash
git add src/telemetry_nerd/model/bucket_state.py tests/unit/test_bucket_state_algebra.py docs/superpowers/specs/2026-10-02-series-bundles-missing-data-design.md
git commit -m "feat(model): bucket_state coarsen/merge with property tests

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Structured, localized caveats

**Files:**
- Create: `src/telemetry_nerd/model/caveats.py`
- Test: `tests/unit/test_caveats.py`

**Interfaces:**
- Consumes: `State`, `FailedSpan`, `STATE_SCHEMA` (Task 1); `format_duration` from `model/time.py`
- Produces:
  - `Where(BaseModel)`: `spans: list[tuple[int, int]] | None`, `series: list[str] | None`
  - `Caveat(BaseModel)`: `code: str`, `severity: Literal["info","warn","blocks_claim"]`, `message: str`, `where: Where | None`, `source: str`
  - `runs(ts: Sequence[int], step_ms: int) -> list[tuple[int, int]]`: contiguous bucket ends → real-time spans `(first − step, last)`
  - `series_name(labels: Mapping[str, str]) -> str`
  - `from_bucket_state(states: pa.Table, names: Mapping[str, str], step_ms: int, failed: Sequence[FailedSpan] = ()) -> list[Caveat]`
  - Codes: `missing_data` (empty/partial, per series, warn), `untrusted_data` (unknown, warn), `absent_part` (info: series alive only part of the window)

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_caveats.py
import pyarrow as pa

from telemetry_nerd.model.bucket_state import STATE_SCHEMA, State
from telemetry_nerd.model.caveats import Caveat, from_bucket_state, runs, series_name

STEP = 60_000


def table(rows):
    cols = list(zip(*rows))
    return pa.table(dict(zip(STATE_SCHEMA.names, cols)), schema=STATE_SCHEMA)


def row(t, sid, s, obs=4.0):
    return (t * STEP, sid, obs, 4.0, int(s), 0)


def test_runs_merge_contiguous_buckets_into_time_spans():
    assert runs([2 * STEP, 3 * STEP, 5 * STEP], STEP) == [(STEP, 3 * STEP), (4 * STEP, 5 * STEP)]


def test_series_name_is_promql_style():
    assert series_name({"instance": "a", "job": "x"}) == '{instance="a", job="x"}'
    assert series_name({}) == "{}"


def test_all_ok_gives_no_caveats():
    assert from_bucket_state(table([row(1, "a", State.OK)]), {"a": "a"}, STEP) == []


def test_missing_data_is_per_series_with_spans():
    t = table([row(1, "a", State.OK), row(2, "a", State.EMPTY, 0), row(3, "a", State.PARTIAL, 2),
               row(1, "b", State.OK), row(2, "b", State.OK), row(3, "b", State.OK)])
    [c] = from_bucket_state(t, {"a": "A", "b": "B"}, STEP)
    assert c.code == "missing_data" and c.severity == "warn" and c.source == "bucket_state"
    assert c.where.series == ["a"] and c.where.spans == [(STEP, 3 * STEP)]
    assert "A" in c.message and "1m" in c.message


def test_unknown_is_one_caveat_with_reason_and_no_series_when_all_affected():
    t = table([row(1, "a", State.UNKNOWN, 0), row(1, "b", State.UNKNOWN, 0)])
    [c] = from_bucket_state(t, {"a": "A", "b": "B"}, STEP, failed=[(STEP, STEP, "timeout")])
    assert c.code == "untrusted_data" and c.where.series is None
    assert "timeout" in c.message


def test_late_born_series_is_info_not_warning():
    t = table([row(1, "a", State.ABSENT, 0), row(2, "a", State.OK)])
    [c] = from_bucket_state(t, {"a": "A"}, STEP)
    assert c.code == "absent_part" and c.severity == "info"


def test_caveat_round_trips_through_json():
    c = Caveat(code="x", message="m", source="validator")
    assert Caveat.model_validate_json(c.model_dump_json()) == c
```

- [ ] **Step 2: Run and check they fail**

Run: `uv run pytest tests/unit/test_caveats.py -q`
Expected: FAIL, `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/telemetry_nerd/model/caveats.py
"""Structured caveats that know where they apply (spec 2026-10-02 §4)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

import polars as pl
import pyarrow as pa
from pydantic import BaseModel

from telemetry_nerd.model.bucket_state import FailedSpan, State
from telemetry_nerd.model.time import format_duration

Severity = Literal["info", "warn", "blocks_claim"]


class Where(BaseModel):
    spans: list[tuple[int, int]] | None = None  # real time [start, end] in ms
    series: list[str] | None = None  # series ids; None = all series


class Caveat(BaseModel):
    code: str
    severity: Severity = "warn"
    message: str
    where: Where | None = None  # None = whole panel
    source: str


def runs(ts: Sequence[int], step_ms: int) -> list[tuple[int, int]]:
    """Contiguous bucket ends -> time spans; a bucket ending at t covers (t - step, t]."""
    out: list[tuple[int, int]] = []
    for t in sorted(ts):
        if out and t - out[-1][1] == step_ms:
            out[-1] = (out[-1][0], t)
        else:
            out.append((t - step_ms, t))
    return out


def series_name(labels: Mapping[str, str]) -> str:
    return "{" + ", ".join(f'{k}="{v}"' for k, v in sorted(labels.items())) + "}"


def _total(spans: list[tuple[int, int]]) -> str:
    return format_duration(sum(b - a for a, b in spans))


def from_bucket_state(
    states: pa.Table, names: Mapping[str, str], step_ms: int, failed: Sequence[FailedSpan] = ()
) -> list[Caveat]:
    if states.num_rows == 0:
        return []
    df = pl.from_arrow(states)
    out: list[Caveat] = []
    unknown = df.filter(pl.col("state") == int(State.UNKNOWN))
    if unknown.height:
        affected = sorted(unknown["series_id"].unique().to_list())
        everyone = len(affected) == df["series_id"].n_unique()
        spans = runs(unknown["ts_ms"].unique().to_list(), step_ms)
        reasons = sorted({r for *_ab, r in failed}) or ["source could not tell"]
        out.append(
            Caveat(
                code="untrusted_data",
                message=f"Data unknown for {_total(spans)} ({'; '.join(reasons)}).",
                where=Where(spans=spans, series=None if everyone else affected),
                source="bucket_state",
            )
        )
    for (sid,), g in df.sort("ts_ms").group_by("series_id", maintain_order=True):
        name = names.get(sid, sid)
        empty_ts = g.filter(pl.col("state") == int(State.EMPTY))["ts_ms"].to_list()
        partial_ts = g.filter(pl.col("state") == int(State.PARTIAL))["ts_ms"].to_list()
        empty, partial = runs(empty_ts, step_ms), runs(partial_ts, step_ms)
        if empty or partial:
            parts = [f"no samples for {_total(empty)}"] if empty else []
            parts += [f"fewer samples than expected for {_total(partial)}"] if partial else []
            out.append(
                Caveat(
                    code="missing_data",
                    message=f"{name}: {', '.join(parts)}.",
                    # one highlight per contiguous stretch of trouble, whatever its kind
                    where=Where(spans=runs(empty_ts + partial_ts, step_ms), series=[sid]),
                    source="bucket_state",
                )
            )
        absent = runs(g.filter(pl.col("state") == int(State.ABSENT))["ts_ms"].to_list(), step_ms)
        if absent:
            out.append(
                Caveat(
                    code="absent_part",
                    severity="info",
                    message=f"{name}: first seen {_total(absent)} after the window starts.",
                    where=Where(spans=absent, series=[sid]),
                    source="bucket_state",
                )
            )
    return out
```

- [ ] **Step 4: Run and check they pass**

Run: `uv run pytest tests/unit/test_caveats.py -q`
Expected: all pass. If `format_duration(60000)` doesn't render `"1m"`, check `model/time.py`
and adjust the test's expected string to its real format. Do not change `format_duration`.

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/model/caveats.py tests/unit/test_caveats.py
git commit -m "feat(model): structured localized caveats from bucket_state

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Companion registry, op policies, `Bundle`

**Files:**
- Create: `src/telemetry_nerd/model/companions.py`
- Test: `tests/unit/test_companions.py`

**Interfaces:**
- Consumes: `compute`, `coarsen`, `STATE_SCHEMA` (Tasks 1–2); `from_bucket_state`, `Caveat` (Task 3); `DatasetStore`, `DatasetMeta` (`datasets/store.py`); `FetchResult`; `Kind` literal of `analysis/filters.py`
- Produces:
  - `Policy = Literal["carry", "recompute", "derive", "drop"]`
  - `KINDS: dict[str, CompanionKind]` (`"bucket_state"`)
  - `OPS: dict[str, dict[str, Policy]]` keyed by op name: `query`, `query_distribution`, `lod`, `dist_rebucket`, `lowpass`, `highpass`, `bandpass`
  - `policy(op: str, kind: str) -> Policy` (undeclared → `"drop"`)
  - `Bundle(primary: pa.Table, series: pa.Table, companions: dict[str, pa.Table], caveats: list[Caveat])`
  - `dataset_bundle(store: DatasetStore, meta: DatasetMeta, result: FetchResult) -> Bundle`
  - `SOURCE_AGGREGATED`: compiled regex for source-side aggregations

`dataset_bundle` rules:
- `meta.derived` is `None` → op `query`, which derives: mode `samples` for `bucket_agg`, `presence` for `quantile`; failed spans come from `meta.failed_spans` (Task 5 adds that field; until then read it with `getattr(meta, "failed_spans", [])`).
- `meta.derived["op"]` is a filter kind → policy `carry`: take the source dataset's bundle states, restricted to this result's series ids.
- Policy `drop` → no companion, plus a caveat `companion_dropped` (info).
- An expression aggregated at the source → plus an info caveat `member_coverage_unknown`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_companions.py
from typing import get_args

import pytest

from telemetry_nerd.analysis.filters import Kind as FilterKind
from telemetry_nerd.model.bucket_state import State
from telemetry_nerd.model.companions import KINDS, OPS, SOURCE_AGGREGATED, dataset_bundle, policy
from tests.unit.fakes import make_service

DATASET_OPS = {"query", "query_distribution", "lod", "dist_rebucket", *get_args(FilterKind)}


def test_every_op_declares_every_companion_kind():
    for op in DATASET_OPS:
        for kind in KINDS:
            assert kind in OPS.get(op, {}), f"op {op!r} does not declare companion {kind!r}"


def test_undeclared_op_drops():
    assert policy("some_future_op", "bucket_state") == "drop"


@pytest.mark.parametrize(
    ("expr", "agg"),
    [("sum by (job) (rate(x[5m]))", True), ("avg(up)", True), ("rate(x[5m])", False), ("up", False)],
)
def test_source_aggregation_detection(expr, agg):
    assert bool(SOURCE_AGGREGATED.match(expr)) is agg


async def test_query_dataset_derives_bucket_state(tmp_path):
    svc = make_service(tmp_path)
    ds = (await svc.query("up", start="now-2h", end="now-1h", step="1m"))["dataset"]
    meta, result = svc.datasets.get(ds)
    b = dataset_bundle(svc.datasets, meta, result)
    states = b.companions["bucket_state"]
    assert states.num_rows == result.buckets.num_rows  # full grid, both series
    assert set(states["state"].to_pylist()) == {State.OK}
    assert b.caveats == []


async def test_aggregated_query_carries_member_caveat(tmp_path):
    svc = make_service(tmp_path)
    ds = (await svc.query("sum(up)", start="now-2h", end="now-1h", step="1m"))["dataset"]
    meta, result = svc.datasets.get(ds)
    codes = [c.code for c in dataset_bundle(svc.datasets, meta, result).caveats]
    assert codes == ["member_coverage_unknown"]
```

`make_service`'s `FakeSource.fetch` currently returns `count=4` for every step. With 1m steps and a
15s resolution, 4 is exactly what we expect. Task 6 makes that count realistic for all steps.

- [ ] **Step 2: Run and check they fail**

Run: `uv run pytest tests/unit/test_companions.py -q`
Expected: FAIL, `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/telemetry_nerd/model/companions.py
"""Companion series and how ops carry them (spec 2026-10-02 §3)."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import polars as pl
import pyarrow as pa

from telemetry_nerd.model.bucket_state import STATE_SCHEMA, coarsen, compute
from telemetry_nerd.model.caveats import Caveat, from_bucket_state
from telemetry_nerd.model.series import FetchResult

Policy = Literal["carry", "recompute", "derive", "drop"]

SOURCE_AGGREGATED = re.compile(
    r"^\s*(sum|avg|min|max|count|group|stddev|stdvar|topk|bottomk|quantile|count_values)\b\s*"
    r"(by|without)?\s*(\([^)]*\))?\s*\(",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CompanionKind:
    name: str
    schema: pa.Schema
    coarsen: Callable[[pa.Table, int], pa.Table]
    caveats: Callable[..., list[Caveat]]


KINDS: dict[str, CompanionKind] = {
    "bucket_state": CompanionKind("bucket_state", STATE_SCHEMA, coarsen, from_bucket_state),
}

OPS: dict[str, dict[str, Policy]] = {
    "query": {"bucket_state": "derive"},
    "query_distribution": {"bucket_state": "derive"},
    "lod": {"bucket_state": "recompute"},
    "dist_rebucket": {"bucket_state": "recompute"},
    # filters change values, not which buckets were observed
    "lowpass": {"bucket_state": "carry"},
    "highpass": {"bucket_state": "carry"},
    "bandpass": {"bucket_state": "carry"},
}


def policy(op: str, kind: str) -> Policy:
    return OPS.get(op, {}).get(kind, "drop")


@dataclass(frozen=True)
class Bundle:
    primary: pa.Table
    series: pa.Table
    companions: dict[str, pa.Table] = field(default_factory=dict)
    caveats: list[Caveat] = field(default_factory=list)


def _derive_states(meta, result: FetchResult) -> pa.Table:
    mode = "presence" if meta.representation == "quantile" else "samples"
    failed = [tuple(f) for f in getattr(meta, "failed_spans", [])]
    return compute(
        result.buckets,
        result.series["series_id"].to_pylist(),
        start_ms=meta.start_ms,
        end_ms=meta.end_ms,
        step_ms=meta.step_ms,
        resolution_ms=meta.resolution_ms,
        mode=mode,
        failed=failed,
    )


def dataset_bundle(store, meta, result: FetchResult) -> Bundle:
    op = meta.derived["op"] if meta.derived else "query"
    caveats: list[Caveat] = []
    companions: dict[str, pa.Table] = {}
    p = policy(op, "bucket_state")
    if p == "derive":
        companions["bucket_state"] = _derive_states(meta, result)
    elif p == "carry":
        src_meta, src_result = store.get(meta.derived["from"])
        src = dataset_bundle(store, src_meta, src_result).companions.get("bucket_state")
        if src is not None:
            ids = result.series["series_id"].to_pylist()
            companions["bucket_state"] = (
                pl.from_arrow(src).filter(pl.col("series_id").is_in(ids)).to_arrow().cast(STATE_SCHEMA)
            )
    else:
        caveats.append(
            Caveat(
                code="companion_dropped",
                severity="info",
                message=f"Coverage is not tracked through {op}.",
                source=f"op:{op}",
            )
        )
    if SOURCE_AGGREGATED.match(meta.expr):
        caveats.append(
            Caveat(
                code="member_coverage_unknown",
                severity="info",
                message="Aggregated at the source: missing member series cannot be seen.",
                source="bucket_state",
            )
        )
    return Bundle(result.buckets, result.series, companions, caveats)
```

`dataset_bundle` builds `bucket_state` and the source-level caveats. `from_bucket_state` caveats are
added where the step is final (panel payload, summary), because coarsening changes their spans.

- [ ] **Step 4: Run and check they pass**

Run: `uv run pytest tests/unit/test_companions.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/model/companions.py tests/unit/test_companions.py
git commit -m "feat(model): companion registry, op policies and dataset bundles

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Partial fetch failures become `unknown` spans

**Files:**
- Modify: `src/telemetry_nerd/model/series.py` (`FetchResult`)
- Modify: `src/telemetry_nerd/datasets/cache.py` (`SeriesCache.get`)
- Modify: `src/telemetry_nerd/datasets/store.py` (`DatasetMeta`, `DatasetStore.put`)
- Test: `tests/unit/test_cache.py`, `tests/unit/test_dataset_store.py`

**Interfaces:**
- Produces:
  - `FetchResult.failed: tuple[tuple[int, int, str], ...] = ()`: inclusive bucket-ts spans of chunks that failed, with reasons
  - `DatasetMeta.failed_spans: list[list] = []`: JSON form of the same spans `[a, b, reason]`
  - `SeriesCache.get` raises only when **every** chunk it had to fetch failed **and** no fresh chunk is cached. A failed chunk is never stored.
- Only `SourceError` subclasses other than `LimitExceeded` count as chunk failures. Every other
  exception, including `CancelledError` and `LimitExceeded`, propagates unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_cache.py`:

```python
from telemetry_nerd.sources.base import LimitExceeded, SourceUnavailable


class FlakyFetcher(FakeFetcher):
    """Fails every chunk whose start is in `bad`."""

    def __init__(self, bad, exc=SourceUnavailable("store down")):
        super().__init__()
        self.bad, self.exc = set(bad), exc

    async def __call__(self, rng):
        if rng.start_ms in self.bad:
            self.calls.append(rng)
            raise self.exc
        return await super().__call__(rng)


async def test_failed_chunk_becomes_failed_span_and_is_not_cached(cache):
    rng = TimeRange(NOW - 3 * SPAN, NOW - SPAN - STEP)
    bad = NOW - 2 * SPAN
    out = await cache.get("src", "up", rng, STEP, FlakyFetcher([bad]))
    assert out.failed == ((bad, bad + SPAN - STEP, "SourceUnavailable: store down"),)
    assert all(not (bad <= t < bad + SPAN) for t in out.buckets["ts_ms"].to_pylist())
    retry = FakeFetcher()
    again = await cache.get("src", "up", rng, STEP, retry)
    assert [r.start_ms for r in retry.calls] == [bad]  # only the failed chunk is refetched
    assert again.failed == ()


async def test_all_chunks_failing_raises(cache):
    rng = TimeRange(NOW - 2 * SPAN, NOW - SPAN - STEP)
    with pytest.raises(SourceUnavailable):
        await cache.get("src", "up", rng, STEP, FlakyFetcher([NOW - 2 * SPAN]))


async def test_limit_exceeded_propagates(cache):
    rng = TimeRange(NOW - 3 * SPAN, NOW - SPAN - STEP)
    with pytest.raises(LimitExceeded):
        await cache.get("src", "up", rng, STEP, FlakyFetcher([NOW - 2 * SPAN], LimitExceeded("too many")))
```

Append to `tests/unit/test_dataset_store.py`. First look at how existing tests there build a
store and a `FetchResult`, and reuse those fixtures:

```python
def test_failed_spans_round_trip(store, result):
    failed = ((1000, 2000, "SourceUnavailable: x"),)
    meta = store.put(source="s", expr="up", rng=TimeRange(0, 60_000), step_ms=1000,
                     resolution_ms=1000, result=FetchResult(result.buckets, result.series, failed=failed))
    assert store.meta(meta.id).failed_spans == [[1000, 2000, "SourceUnavailable: x"]]


def test_old_meta_without_failed_spans_loads(store, result):
    meta = store.put(source="s", expr="up", rng=TimeRange(0, 60_000), step_ms=1000,
                     resolution_ms=1000, result=result)
    assert store.meta(meta.id).failed_spans == []
```

If `test_dataset_store.py` has no `store`/`result` fixtures, define them at the top of the new
tests the way its existing tests build them.

- [ ] **Step 2: Run and check they fail**

Run: `uv run pytest tests/unit/test_cache.py tests/unit/test_dataset_store.py -q`
Expected: the new tests FAIL (`failed` attribute / unexpected keyword).

- [ ] **Step 3: Implement**

`model/series.py`:

```python
@dataclass(frozen=True)
class FetchResult:
    buckets: pa.Table  # BUCKET_SCHEMA
    series: pa.Table  # SERIES_SCHEMA
    partial: int = 0  # incomplete source cells dropped (a bucket the source only half-returned)
    # chunks that failed: inclusive bucket-ts span and "ErrorClass: message" (bucket_state UNKNOWN)
    failed: tuple[tuple[int, int, str], ...] = ()
```

`datasets/store.py`: add to `DatasetMeta`, after `derived`:

```python
    failed_spans: list[list] = field(default_factory=list)  # [[a, b, reason]] fetch failures
```

In `DatasetStore.put`, pass `failed_spans=[list(f) for f in result.failed]` to `DatasetMeta(...)`.

`datasets/cache.py`: import `from telemetry_nerd.sources.base import LimitExceeded, SourceError`
and replace the gather/store section of `get` with:

```python
            results = await asyncio.gather(
                *(bounded(cs) for cs in missing), return_exceptions=True
            )
            failed: list[tuple[int, int, str]] = []
            for cs, result in zip(missing, results, strict=True):
                if isinstance(result, BaseException):
                    if not isinstance(result, SourceError) or isinstance(result, LimitExceeded):
                        raise result
                    failed.append((cs, cs + span - step_ms, f"{type(result).__name__}: {result}"))
                    continue
                immutable = cs + span <= now - self.settle_ms
                self._store(qkey, cs, cs + span - step_ms, result, now, immutable)
            starts = self.chunk_starts(rng, step_ms)
            if failed and len(failed) == len(starts):
                raise next(r for r in results if isinstance(r, BaseException))
```

The function's final return becomes
`FetchResult(read.buckets, read.series, partial=..., failed=tuple(failed))`.

- [ ] **Step 4: Run and check they pass, then run the whole unit suite**

Run: `uv run pytest tests/unit -q`
Expected: all pass. `DatasetMeta(**json)` must accept old rows that have no `failed_spans`;
the default covers that.

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/model/series.py src/telemetry_nerd/datasets/cache.py src/telemetry_nerd/datasets/store.py tests/unit/test_cache.py tests/unit/test_dataset_store.py
git commit -m "feat(cache): failed chunks become unknown spans instead of failing the query

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Time panel payload and Claude summary carry coverage

**Files:**
- Modify: `tests/unit/fakes.py` (`FakeSource.fetch`: realistic count)
- Modify: `src/telemetry_nerd/core/panel_payloads.py` (add `state_payload`)
- Modify: `src/telemetry_nerd/core/service.py` (`TelemetryService.panel_data`)
- Modify: `src/telemetry_nerd/core/summary.py` (`summarize`)
- Modify: `src/telemetry_nerd/mcp/server.py` (instructions text, one sentence)
- Test: `tests/unit/test_service.py`, `tests/unit/test_summary.py`

**Interfaces:**
- Consumes: `dataset_bundle` (Task 4), `coarsen`, `State` (Tasks 1–2), `from_bucket_state`, `series_name`, `runs` (Task 3)
- Produces:
  - `state_payload(states: pa.Table) -> list[dict]`: one entry per series with any non-OK bucket: `{"id", "ts", "state", "observed", "expected", "flags"}`
  - Time panel payload adds `"bucket_state": list[dict]` and `"located": list[dict]` (`Caveat.model_dump()`). It drops the legacy `"gaps"` code when a `missing_data` caveat is present. Codes from located caveats with severity ≥ warn are appended to `caveats` (deduplicated) so existing note rendering keeps working.
  - Summary: each series gains `"coverage": {"pct": float, "missing": str, "longest_gap": str | None}`; the dataset gains `"unknown_spans": [[iso, iso, reason], ...]`; codes `missing_data`/`untrusted_data` join `caveats`.

- [ ] **Step 1: Make FakeSource counts realistic**

In `tests/unit/fakes.py` `FakeSource.fetch`, replace `"count": [4] * len(rows),` with:

```python
                "count": [max(1, step_ms // self.resolution_ms)] * len(rows),
```

Run: `uv run pytest tests/unit -q`. Expected: still green. Count is no longer constant: if a test
asserted `count == 4`, update its expected value to `step_ms // 15_000`. Don't weaken any other
assertion.

- [ ] **Step 2: Write the failing tests**

Append to `tests/unit/test_service.py`:

```python
class HoleySource(FakeSource):
    """Series i1 has no samples in the 3rd..5th buckets."""

    async def fetch(self, expr, rng, step_ms):
        res = await super().fetch(expr, rng, step_ms)
        b = res.buckets.to_pylist()
        sid1 = sorted({r["series_id"] for r in b})[1]
        ts = sorted({r["ts_ms"] for r in b})
        hole = set(ts[2:5])
        kept = [r for r in b if not (r["series_id"] == sid1 and r["ts_ms"] in hole)]
        return FetchResult(pa.Table.from_pylist(kept, schema=BUCKET_SCHEMA), res.series)


async def test_panel_data_reports_missing_buckets(tmp_path):
    svc = make_service(tmp_path, HoleySource())
    ds = (await svc.query("up", start="now-2h", end="now-1h", step="1m"))["dataset"]
    panel = svc.show(ds, "Holes?").panel
    data = svc.panel_data(panel.id, width_px=2000)
    assert [s["id"] for s in data["bucket_state"]] == [sorted(s["id"] for s in data["series"])[1]]
    [c] = [c for c in data["located"] if c["code"] == "missing_data"]
    assert len(c["where"]["spans"]) == 1
    assert "missing_data" in data["caveats"] and "gaps" not in data["caveats"]


async def test_clean_panel_has_no_bucket_state(tmp_path):
    svc = make_service(tmp_path)
    ds = (await svc.query("up", start="now-2h", end="now-1h", step="1m"))["dataset"]
    data = svc.panel_data(svc.show(ds, "Clean?").panel.id, width_px=2000)
    assert data["bucket_state"] == [] and data["located"] == []
```

Add the imports `pyarrow as pa`, `BUCKET_SCHEMA` and `FetchResult` to the test module if they
aren't there yet.

Append to `tests/unit/test_summary.py`. Check how existing tests there build `meta`/`result`
and use the same helpers. This test drives the service directly:

```python
async def test_summary_reports_coverage(tmp_path):
    from tests.unit.test_service import HoleySource
    svc = make_service(tmp_path, HoleySource())
    out = await svc.query("up", start="now-2h", end="now-1h", step="1m")
    s = out["summary"]
    assert "missing_data" in s["caveats"]
    cov = {tuple(x["labels"].items()): x["coverage"] for x in s["series"]}
    worst = min(cov.values(), key=lambda c: c["pct"])
    assert worst["longest_gap"] == "3m" and worst["pct"] < 1.0
    assert s["unknown_spans"] == []
```

- [ ] **Step 3: Run and check they fail**

Run: `uv run pytest tests/unit/test_service.py tests/unit/test_summary.py -q`
Expected: the new tests FAIL with `KeyError: 'bucket_state'` and `'coverage'`.

- [ ] **Step 4: Implement `state_payload` (panel_payloads.py)**

```python
from telemetry_nerd.model.bucket_state import State


def state_payload(states) -> list[dict]:
    """bucket_state for series with any non-OK bucket (an all-ok panel draws no rug)."""
    df = pl.from_arrow(states)
    if df.is_empty():
        return []
    bad = df.filter(pl.col("state") != int(State.OK))["series_id"].unique()
    out = []
    for (sid,), g in (
        df.filter(pl.col("series_id").is_in(bad)).sort("ts_ms").group_by("series_id", maintain_order=True)
    ):
        out.append({
            "id": sid, "ts": g["ts_ms"].to_list(), "state": g["state"].to_list(),
            "observed": g["observed"].to_list(), "expected": g["expected"].to_list(),
            "flags": g["flags"].to_list(),
        })  # fmt: skip
    return sorted(out, key=lambda s: s["id"])
```

- [ ] **Step 5: Wire into `TelemetryService.panel_data` (service.py)**

Imports: `from telemetry_nerd.model.bucket_state import coarsen`,
`from telemetry_nerd.model.caveats import from_bucket_state, series_name`,
`from telemetry_nerd.model.companions import dataset_bundle`, and `state_payload` from
`panel_payloads`.

After `caveats = self._time_summary(...)["caveats"]` and before `extra = ...`, insert:

```python
        bundle = dataset_bundle(self.datasets, meta, result)
        states = bundle.companions.get("bucket_state")
        located = list(bundle.caveats)
        state_rows: list[dict] = []
        if states is not None:
            if effective_step != meta.step_ms:
                states = coarsen(states, effective_step)
            names = {sid: series_name(lb) for sid, lb in labels.items()}
            failed = [tuple(f) for f in meta.failed_spans]
            located += from_bucket_state(states, names, effective_step, failed)
            state_rows = state_payload(states)
        if any(c.code == "missing_data" for c in located) and "gaps" in caveats:
            caveats.remove("gaps")
        for c in located:
            if c.severity != "info" and c.code not in caveats:
                caveats.append(c.code)
```

In the returned dict, add `"bucket_state": state_rows,` and
`"located": [c.model_dump() for c in located],`.

- [ ] **Step 6: Coverage in `summarize` (summary.py)**

Imports: `from telemetry_nerd.model.bucket_state import State, compute`,
`from telemetry_nerd.model.caveats import runs`.

Add a helper:

```python
def _coverage(meta: DatasetMeta, result: FetchResult) -> tuple[dict[str, dict], list[list[str]]]:
    """Per series: observed/expected share, total missing, longest gap; dataset unknown spans."""
    mode = "presence" if meta.representation == "quantile" else "samples"
    st = compute(result.buckets, result.series["series_id"].to_pylist(), start_ms=meta.start_ms,
                 end_ms=meta.end_ms, step_ms=meta.step_ms, resolution_ms=meta.resolution_ms,
                 mode=mode, failed=[tuple(f) for f in meta.failed_spans])  # fmt: skip
    df = pl.from_arrow(st)
    bad = [int(State.EMPTY), int(State.PARTIAL), int(State.UNKNOWN)]
    out: dict[str, dict] = {}
    for (sid,), g in df.group_by("series_id"):
        alive = g.filter(pl.col("state") != int(State.ABSENT))
        exp = alive["expected"].sum()
        gaps = runs(alive.filter(pl.col("state").is_in(bad))["ts_ms"].to_list(), meta.step_ms)
        longest = max((b - a for a, b in gaps), default=0)
        out[sid] = {
            "pct": _round(min(1.0, alive["observed"].sum() / exp) if exp else 0.0),
            "missing": format_duration(sum(b - a for a, b in gaps)) if gaps else "0s",
            "longest_gap": format_duration(longest) if longest else None,
        }
    unknown = runs(df.filter(pl.col("state") == int(State.UNKNOWN))["ts_ms"].unique().to_list(),
                   meta.step_ms)  # fmt: skip
    return out, [[iso(a), iso(b)] for a, b in unknown]
```

In `summarize`, after the empty-result early return, compute
`coverage, unknown_spans = _coverage(meta, result)`. Then:
- add `"unknown_spans": unknown_spans` to `base`;
- if `unknown_spans`, append `"untrusted_data"` to caveats;
- if any `coverage[sid]["pct"] < 1.0`, append `"missing_data"` to caveats.

Each per-series dict gets `"coverage": coverage.get(r["series_id"])`. In `summarize`, `per` must
keep `series_id` (it does: `group_by("series_id")`). In `_summarize_quantile`, pass `coverage`
in and add the same key.

If `format_duration(0)` raises, use the `"0s"` literal as shown.

- [ ] **Step 7: MCP instructions**

In `src/telemetry_nerd/mcp/server.py`, in the instructions text next to the `finding_create`
lines, add one line:

```
- Summaries carry `coverage` per series (share of expected samples, longest gap) and `unknown_spans` ([start, end, reason]), `silent_members` (+ `silent_more`); missing data is evidence too — scope claims around it.
```

If `tests/unit/test_mcp_instructions.py` checks length or exact content, update it to match.

- [ ] **Step 8: Run all unit tests**

Run: `uv run pytest tests/unit -q`
Expected: all pass.

- [ ] **Step 9: Commit**

Use `git add -p` for `service.py` and `mcp/server.py` if they hold foreign hunks.

```bash
git add tests/unit/fakes.py tests/unit/test_service.py tests/unit/test_summary.py src/telemetry_nerd/core/panel_payloads.py src/telemetry_nerd/core/summary.py
git add -p src/telemetry_nerd/core/service.py src/telemetry_nerd/mcp/server.py
git commit -m "feat(core): time panels and summaries carry bucket_state and located caveats

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Distribution payloads: heatmap column state and window coverage

**Files:**
- Modify: `src/telemetry_nerd/core/panel_payloads.py` (`heatmap_panel_data`, `histogram_panel_data`)
- Test: `tests/unit/test_service_distribution.py`

**Interfaces:**
- Consumes: `compute`, `coarsen` (Tasks 1–2), `state_payload` (Task 6), `grid`
- Produces:
  - Each heatmap series gains `"state": dict | None` (the `state_payload` entry for that series, or `None` when all ok). A column is observed when the source returned it, **including n = 0** (measured, nothing happened).
  - Each histogram window gains `"expected_columns": int` (grid columns in the window) and `"unknown": bool`.

- [ ] **Step 1: Write the failing tests**

First look at how `test_service_distribution.py` runs `query_distribution` + `show` + `panel_data`,
and copy that pattern. Add a fake source whose `fetch_histogram` drops some timestamps for one
instance, by subclassing `FakeSource` and filtering the matrix:

```python
class HoleyHistSource(FakeSource):
    async def fetch_histogram(self, selector, by, rng, step_ms):
        self.calls += 1
        ts = list(range(rng.start_ms, rng.end_ms + 1, step_ms))
        hole = set(ts[2:4])
        result = [
            {"metric": {"instance": f"i{k}", "le": le},
             "values": [[t / 1000, str(c * (k + 1))] for t in ts if not (k == 1 and t in hole)]}
            for k in range(self.n_series) for le, c in self.cumulative.items()
        ]
        return from_matrix(self.name, result, expr=histogram_expr(selector, by, step_ms))


async def test_heatmap_series_carry_column_state(tmp_path):
    svc = make_service(tmp_path, HoleyHistSource())
    ds = (await svc.query_distribution("lat_bucket", start="now-2h", end="now-1h", step="1m"))["dataset"]
    data = svc.panel_data(svc.show(ds, "Dist?").panel.id, width_px=4000)
    states = {s["labels"]["instance"]: s["state"] for s in data["series"]}
    assert states["i0"] is None
    assert states["i1"]["state"].count(2) == 2  # State.EMPTY
```

Then a window test: show the same dataset as a `histogram` mark over one window (use the spec
shape in existing tests) and assert `w["columns"] < w["expected_columns"]` for `i1`, and
`w["unknown"] is False`.

Imports `from_matrix` and `histogram_expr` come from where `tests/unit/fakes.py` imports them;
copy that import line.

- [ ] **Step 2: Run and check they fail**

Run: `uv run pytest tests/unit/test_service_distribution.py -q`
Expected: the new tests FAIL with `KeyError: 'state'`.

- [ ] **Step 3: Implement**

In `panel_payloads.py` add:

```python
from telemetry_nerd.model.bucket_state import State, coarsen, compute, grid


def column_states(meta, dist):
    """Distribution columns as presence buckets: a returned column is observed even when n = 0."""
    cols = pl.from_arrow(dist.columns).select(
        "ts_ms", "series_id", pl.col("n").cast(pl.Float64).alias("avg"),
        pl.col("n").cast(pl.Float64).alias("min"), pl.col("n").cast(pl.Float64).alias("max"),
        pl.lit(1, pl.Int64).alias("count"),
    )  # fmt: skip
    return compute(
        cols.to_arrow(), dist.series["series_id"].to_pylist(), start_ms=meta.start_ms,
        end_ms=meta.end_ms, step_ms=meta.step_ms, resolution_ms=meta.step_ms, mode="presence",
        failed=[tuple(f) for f in meta.failed_spans],
    )  # fmt: skip
```

`n` is the per-column observation total. Check the real column name in `dist.columns`
(`rg -n "columns" src/telemetry_nerd/model/distribution.py`). If it isn't `n`, use the real name.
The value only has to be non-null.

In `heatmap_panel_data`, after `step` is known:

```python
    states = column_states(meta, dist)
    if factor > 1:
        states = coarsen(states, step)
    by_sid = {s["id"]: s for s in state_payload(states)}
```

and add `"state": by_sid.get(sid),` to each series dict.

In `histogram_panel_data`, compute `states = pl.from_arrow(column_states(meta, dist))` once.
Then extend `window(k, w, sid)`:

```python
        g = states.filter(
            (pl.col("series_id") == sid) & pl.col("ts_ms").is_between(w["start_ms"], w["end_ms"])
        )
        out["expected_columns"] = len(grid(w["start_ms"], w["end_ms"], meta.step_ms))
        out["unknown"] = bool((g["state"] == int(State.UNKNOWN)).any())
```

- [ ] **Step 4: Run and check they pass**

Run: `uv run pytest tests/unit -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -p src/telemetry_nerd/core/panel_payloads.py
git add tests/unit/test_service_distribution.py
git commit -m "feat(core): heatmap column state and window coverage in distribution payloads

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Findings check coverage of their claim window

**Files:**
- Modify: `src/telemetry_nerd/core/workspace_service.py` (`finding_create`)
- Create: `src/telemetry_nerd/core/coverage_check.py`
- Test: `tests/unit/test_coverage_check.py`, `tests/unit/test_workspace_service.py`

**Interfaces:**
- Consumes: `dataset_bundle` (Task 4), `State`, `Caveat`
- Produces:
  - `BLOCK_BELOW = 0.5`
  - `claim_coverage(states: pa.Table, start_ms: int, end_ms: int) -> list[Caveat]`: within the claim window, any UNKNOWN → `blocks_claim` `untrusted_data`; overall coverage (Σobserved/Σexpected over alive) < 0.5 → `blocks_claim` `missing_data`; any other non-OK → `warn` `missing_data`.
  - `finding_create` raises `ValueError` for blocking caveats, with a hint to narrow `scope.time_range` or `selector`. Warn messages are appended to `data.caveats` (strings) before the finding is stored.

Datasets come from `PanelRef` (the panel's `dataset_ids[0]`) and `StatisticRef` (`dataset`).
Only `bucket_agg` and `quantile` datasets are checked here; distribution datasets wait for a
follow-up. `WorkspaceService` already holds `self.datasets` (see how `finding_create` uses
`self.datasets.exists`).

- [ ] **Step 1: Write the failing unit tests**

```python
# tests/unit/test_coverage_check.py
import pyarrow as pa

from telemetry_nerd.core.coverage_check import claim_coverage
from telemetry_nerd.model.bucket_state import STATE_SCHEMA, State

STEP = 60_000


def table(states, obs=None):
    n = len(states)
    obs = obs or [4.0 if s == State.OK else 0.0 for s in states]
    return pa.table({"ts_ms": [(i + 1) * STEP for i in range(n)], "series_id": ["a"] * n,
                     "observed": obs, "expected": [4.0] * n, "state": [int(s) for s in states],
                     "flags": [0] * n}, schema=STATE_SCHEMA)


def test_clean_window_passes():
    assert claim_coverage(table([State.OK] * 4), 0, 4 * STEP) == []


def test_unknown_in_window_blocks():
    [c] = claim_coverage(table([State.OK, State.UNKNOWN, State.OK]), 0, 3 * STEP)
    assert (c.code, c.severity) == ("untrusted_data", "blocks_claim")


def test_unknown_outside_window_is_ignored():
    assert claim_coverage(table([State.OK, State.OK, State.UNKNOWN]), 0, 2 * STEP) == []


def test_mostly_missing_blocks_and_little_missing_warns():
    [c] = claim_coverage(table([State.EMPTY, State.EMPTY, State.OK]), 0, 3 * STEP)
    assert c.severity == "blocks_claim"
    [c] = claim_coverage(table([State.EMPTY, State.OK, State.OK, State.OK]), 0, 4 * STEP)
    assert (c.code, c.severity) == ("missing_data", "warn")
```

Add to `tests/unit/test_workspace_service.py` (copy its fixtures for creating a finding with a
`StatisticRef`). These tests use `HoleySource` from `tests/unit/test_service.py`: query → a
finding scoped to the hole window is rejected (`pytest.raises(ValueError, match="coverage")`),
and a finding scoped to a clean window succeeds with no added caveats.

- [ ] **Step 2: Run and check they fail**

Run: `uv run pytest tests/unit/test_coverage_check.py -q`
Expected: FAIL, `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/telemetry_nerd/core/coverage_check.py
"""Does a claim window have the data to support a claim? (spec 2026-10-02 §4.4)"""

from __future__ import annotations

import polars as pl
import pyarrow as pa

from telemetry_nerd.model.bucket_state import State
from telemetry_nerd.model.caveats import Caveat, Where

BLOCK_BELOW = 0.5


def claim_coverage(states: pa.Table, start_ms: int, end_ms: int) -> list[Caveat]:
    df = pl.from_arrow(states).filter(pl.col("ts_ms").is_between(start_ms + 1, end_ms))
    alive = df.filter(pl.col("state") != int(State.ABSENT))
    if alive.is_empty():
        return []
    where = Where(spans=[(start_ms, end_ms)])
    if (alive["state"] == int(State.UNKNOWN)).any():
        return [Caveat(code="untrusted_data", severity="blocks_claim", where=where,
                       source="validator", message="The claim window contains data the source "
                       "could not return (fetch failed or unknown).")]  # fmt: skip
    exp = alive["expected"].sum()
    share = min(1.0, alive["observed"].sum() / exp) if exp else 0.0
    if share < BLOCK_BELOW:
        return [Caveat(code="missing_data", severity="blocks_claim", where=where,
                       source="validator", message=f"Only {share:.0%} of expected samples in the "
                       "claim window.")]  # fmt: skip
    if (alive["state"] != int(State.OK)).any():
        return [Caveat(code="missing_data", severity="warn", where=where, source="validator",
                       message=f"{share:.0%} of expected samples in the claim window.")]  # fmt: skip
    return []
```

In `finding_create`, after the existing evidence loop:

```python
        blocking, warnings = [], []
        for did in self._evidence_datasets(data):
            meta = self.datasets.meta(did)
            if meta.representation not in ("bucket_agg", "quantile"):
                continue
            meta, result = self.datasets.get(did)
            states = dataset_bundle(self.datasets, meta, result).companions.get("bucket_state")
            if states is None:
                continue
            for c in claim_coverage(states, data.scope.time_range.start_ms, data.scope.time_range.end_ms):
                (blocking if c.severity == "blocks_claim" else warnings).append(f"{did}: {c.message}")
        if blocking:
            raise ValueError(
                "insufficient coverage for this claim: " + " ".join(blocking)
                + " (hint: narrow scope.time_range or the selector to data that exists)"
            )
        if warnings:
            data = data.model_copy(update={"caveats": [*data.caveats, *warnings]})
```

with the helper:

```python
    def _evidence_datasets(self, data: FindingIn) -> list[str]:
        out: list[str] = []
        for ref in data.evidence:
            match ref:
                case PanelRef(panel=pid):
                    out.extend(self.workspace.get_panel(pid).dataset_ids[:1])
                case StatisticRef(dataset=did):
                    out.append(did)
        return list(dict.fromkeys(out))
```

`FindingIn` is a strict pydantic model; `model_copy(update=...)` keeps it valid. If the
`@atomic` decorator or the store needs `data` unchanged, assign to a new variable and pass that
on.

- [ ] **Step 4: Run and check they pass**

Run: `uv run pytest tests/unit -q`
Expected: all pass. Existing finding tests use `FakeSource` data that is fully covered (Task 6
made counts realistic), so none of them should start failing. If one does, fix the test data,
not the threshold.

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/core/coverage_check.py tests/unit/test_coverage_check.py tests/unit/test_workspace_service.py
git add -p src/telemetry_nerd/core/workspace_service.py
git commit -m "feat(findings): reject claims over windows without the data to support them

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: UI types and located caveats in panel notes

**Files:**
- Modify: `ui/src/lib/api.ts`
- Modify: `ui/src/lib/panelNotes.ts`
- Test: `ui/src/lib/panelNotes.test.ts`

**Interfaces:**
- Produces (TS):
  - `export interface BucketStatePayload { id: string; ts: number[]; state: number[]; observed: number[]; expected: number[]; flags: number[] }`
  - `export interface Where { spans?: [number, number][] | null; series?: string[] | null }`
  - `export interface Caveat { code: string; severity: "info" | "warn" | "blocks_claim"; message: string; where?: Where | null; source: string }`
  - `PanelDataBase` gains `located?: Caveat[]`; `TimePanelData` gains `bucket_state?: BucketStatePayload[]`; `HeatSeries` gains `state?: BucketStatePayload | null`; `WindowHist` gains `expected_columns?: number; unknown?: boolean`
  - `Note` gains `where?: Where | null`
  - `panelNotes(caveats, opts)` accepts `opts.located?: Caveat[]`. Each located caveat becomes a note with `key: \`${code}:${i}\``, `kind: severity === "info" ? "info" : "caveat"`, `text: message`, `where`. Legacy string notes whose key equals a located code are dropped (the located one is more specific).

- [ ] **Step 1: Write the failing test** (append to `panelNotes.test.ts`, following its existing `panelNotes(...)` calls for the other opts)

```ts
it("turns located caveats into notes with where, superseding the bare code", () => {
  const located = [{ code: "missing_data", severity: "warn" as const, message: "i1: no samples for 3m.",
    where: { spans: [[1, 2]] as [number, number][], series: ["s1"] }, source: "bucket_state" }];
  const notes = panelNotes(["missing_data", "settling"], { yScaledToData: false, located });
  expect(notes.map((n) => n.key)).toEqual(["settling", "missing_data:0"]);
  expect(notes[1]).toMatchObject({ kind: "caveat", text: "i1: no samples for 3m.", where: { series: ["s1"] } });
});
```

Adjust the `opts` literal to whatever other required fields `panelNotes` has: copy them from an
existing test in the same file.

- [ ] **Step 2: Run and check it fails**

Run: `cd ui && npx vitest run src/lib/panelNotes.test.ts`
Expected: FAIL (key `missing_data` present, no `missing_data:0`).

- [ ] **Step 3: Implement**

Add the interfaces to `api.ts` as listed above. In `panelNotes.ts`, extend `Note` with
`where?: Where | null`, add `located?: Caveat[]` to `opts`. Inside `panelNotes`, filter the
legacy keys before mapping them:

```ts
  const locatedCodes = new Set((opts.located ?? []).map((c) => c.code));
  const notes: Note[] = caveats.filter((key) => !locatedCodes.has(key)).map((key) => ({
```

Then, before the function's final `return`, append:

```ts
  (opts.located ?? []).forEach((c, i) =>
    notes.push({ kind: c.severity === "info" ? "info" : "caveat", key: `${c.code}:${i}`, text: c.message, where: c.where ?? null }),
  );
```

In `ui/src/Panel.svelte`, pass `located: data.located` into the `panelNotes(data.caveats, {...})`
call.

Also add a text for `member_coverage_unknown`, `missing_data` and `untrusted_data` to the
`CAVEATS` map. These are used when a code arrives without a located twin (e.g. from a summary):

```ts
  missing_data: () => "Some series have buckets with no or too few samples (see the coverage rug).",
  untrusted_data: () => "Part of the window could not be fetched or judged; it is hatched.",
  member_coverage_unknown: () => "Aggregated at the source: missing member series cannot be seen.",
```

- [ ] **Step 4: Run and check it passes; type-check**

Run: `cd ui && npx vitest run && cd .. && just ui-check`
Expected: pass, no warnings.

- [ ] **Step 5: Commit**

```bash
git add ui/src/lib/api.ts ui/src/lib/panelNotes.ts ui/src/lib/panelNotes.test.ts ui/src/Panel.svelte
git commit -m "feat(ui): located caveats become notes with where

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Stepped lines

**Files:**
- Modify: `ui/src/chart/toUplot.ts`
- Test: `ui/src/chart/toUplot.test.ts`

**Interfaces:**
- Consumes: the existing `toUplot(series, grid?, opts)` and `Grid`
- Produces:
  - `ToUplotOpts.stepped?: boolean` (default `true` when a `grid` is given)
  - `export function stepify(data: (number | null)[][], stepS: number): (number | null)[][]`: each bucket `i` with end `x` becomes two x points, `x − stepS + ε` and `x`, both carrying bucket `i`'s value, in every column. `ε = 0.001` s.
  - `UplotModel.points` stays the bucket count (render budget semantics unchanged).

Linear paths through the doubled points draw one horizontal segment per bucket across its
interval, with near-vertical risers between adjacent buckets. A lone bucket between gaps is a
visible segment. Bands keep working because they are plain linear series.

- [ ] **Step 1: Write the failing tests**

```ts
import { stepify } from "./toUplot";

describe("stepify", () => {
  it("draws each bucket across its interval and keeps lone buckets visible", () => {
    const out = stepify([[60, 120, 180], [1, null, 3]], 60);
    expect(out[0]).toEqual([0.001, 60, 60.001, 120, 120.001, 180]);
    expect(out[1]).toEqual([1, 1, null, null, 3, 3]);
  });
});

it("steps by default when a grid is given, but counts buckets for the render budget", () => {
  const grid = { start: 60_000, end: 120_000, step: 60_000 };
  const m = toUplot([s("a", { i: "a" }, [60_000, 120_000], [1, 2])], grid);
  expect(m.data[0]).toEqual([0.001, 60, 60.001, 120]);
  expect(m.data[1]).toEqual([1, 1, 2, 2]);
  expect(m.points).toBe(2);
});
```

The existing tests that pass a `grid` and assert `data[0]`/`data[1]` exact values describe the
unstepped layout. Change them to pass `{ stepped: false }` as the third argument, so they keep
testing alignment, and keep the new test above for stepping. Tests without a grid are
unaffected.

- [ ] **Step 2: Run and check it fails**

Run: `cd ui && npx vitest run src/chart/toUplot.test.ts`
Expected: FAIL, `stepify` is not exported.

- [ ] **Step 3: Implement**

In `toUplot.ts`:

```ts
const EPS_S = 0.001;

/** Bucket ends -> interval edges: value i spans (x_i - step, x_i]; lone buckets stay visible. */
export function stepify(data: (number | null)[][], stepS: number): (number | null)[][] {
  const [xs, ...cols] = data;
  const x2: number[] = [];
  for (const x of xs as number[]) x2.push(x - stepS + EPS_S, x);
  return [x2, ...cols.map((c) => c.flatMap((v) => [v, v]))];
}
```

Add `stepped?: boolean;` to `ToUplotOpts`. At the end of `toUplot`, replace the final `return` with:

```ts
  const stepped = grid !== undefined && opts.stepped !== false;
  const out = stepped ? stepify(data, grid.step / 1000) : data;
  return { data: out as uPlot.AlignedData, series: uSeries, bands, legendHidden, points: xs.length * series.length };
```

The legend reads `data[avgIdx + 1][i]`. After stepping, `i` indexes the doubled array, and since
each column is doubled the lookup still reads the same bucket's min/max. Check this manually
(Step 4) by hovering a line.

- [ ] **Step 4: Run tests, type-check, and look at it**

Run: `cd ui && npx vitest run && cd .. && just ui-check`
Expected: pass. Then `just serve` with the synthetic data (`just dev-up && just seed`), open a
panel, and confirm: lines are flat per bucket, the envelope is stepped, and the legend values on
hover match the bucket.

- [ ] **Step 5: Commit**

```bash
git add ui/src/chart/toUplot.ts ui/src/chart/toUplot.test.ts
git commit -m "feat(ui): stepped lines: each bucket drawn across its interval

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Coverage rug with hover hints (time panels)

**Files:**
- Create: `ui/src/chart/rug.ts`
- Test: `ui/src/chart/rug.test.ts`
- Modify: `ui/src/Panel.svelte`

**Interfaces:**
- Consumes: `BucketStatePayload` (Task 9), `setupCanvas`, `PALETTE`, `fmtRange` (`lib/format.ts`)
- Produces:
  - `export const STATE = { OK: 0, PARTIAL: 1, EMPTY: 2, ABSENT: 3, UNKNOWN: 4 } as const`
  - `export const ROW_H = 8, ROW_GAP = 2, MAX_ROWS = 5`
  - `export interface RugCell { row: number; x: number; w: number; state: number; missing: number; ts: number; i: number }`
  - `export function rugHeight(rows: number): number`
  - `export function rugCells(states: BucketStatePayload[], stepMs: number, toX: (ms: number) => number): RugCell[]`
  - `export interface RugStyle { tint: (row: number) => string; grey: string; line: string }`
  - `export function drawRug(ctx: CanvasRenderingContext2D, cells: RugCell[], style: RugStyle): void`
  - `export function hitRug(cells: RugCell[], px: number, py: number): RugCell | null`
  - `export function rugHint(cell: RugCell, s: BucketStatePayload, stepMs: number, name: string, resolutionMs: number): string`

Encoding (spec §7.1):
- OK: series tint (colour at α 0.2)
- PARTIAL: tint plus grey at α = missing share
- EMPTY: solid grey
- ABSENT: dotted grey midline
- UNKNOWN: grey diagonal hatch

The grey is `--muted` from CSS. It already passes 3:1 in both themes; check it with
`relativeLuminance` in the test.

- [ ] **Step 1: Write the failing tests**

```ts
// ui/src/chart/rug.test.ts
import { describe, expect, it } from "vitest";
import { hitRug, MAX_ROWS, ROW_GAP, ROW_H, rugCells, rugHeight, rugHint, STATE } from "./rug";
import type { BucketStatePayload } from "../lib/api";

const st = (id: string, state: number[], observed: number[]): BucketStatePayload => ({
  id, ts: state.map((_, i) => (i + 1) * 60_000), state, observed, expected: state.map(() => 4), flags: state.map(() => 0),
});

describe("rug", () => {
  const toX = (ms: number) => ms / 1000; // 1 px per second

  it("lays out one cell per bucket per row, across the bucket interval", () => {
    const cells = rugCells([st("a", [STATE.OK, STATE.PARTIAL], [4, 1])], 60_000, toX);
    expect(cells).toHaveLength(2);
    expect(cells[1]).toMatchObject({ row: 0, x: 60, w: 60, state: STATE.PARTIAL, missing: 0.75 });
  });

  it("caps rows and sizes the canvas", () => {
    const many = Array.from({ length: 7 }, (_, k) => st(`s${k}`, [STATE.EMPTY], [0]));
    expect(new Set(rugCells(many, 60_000, toX).map((c) => c.row)).size).toBe(MAX_ROWS);
    expect(rugHeight(0)).toBe(0);
    expect(rugHeight(2)).toBe(2 * (ROW_H + ROW_GAP) + 2);
  });

  it("hit-tests by row and x", () => {
    const cells = rugCells([st("a", [STATE.OK], [4]), st("b", [STATE.EMPTY], [0])], 60_000, toX);
    expect(hitRug(cells, 10, ROW_H + ROW_GAP + 2)?.row).toBe(1);
    expect(hitRug(cells, 10, 500)).toBeNull();
  });

  it("explains a cell in words", () => {
    const s = st("a", [STATE.EMPTY], [0]);
    const [cell] = rugCells([s], 60_000, toX);
    const text = rugHint(cell, s, 60_000, '{instance="a"}', 15_000);
    expect(text).toContain("no samples");
    expect(text).toContain("0 of 4 expected");
    expect(text).toContain('{instance="a"}');
  });
});
```

- [ ] **Step 2: Run and check it fails**

Run: `cd ui && npx vitest run src/chart/rug.test.ts`
Expected: FAIL, module not found.

- [ ] **Step 3: Implement `rug.ts`**

```ts
// ui/src/chart/rug.ts
import type { BucketStatePayload } from "../lib/api";
import { fmtRange } from "../lib/format";

/** bucket_state codes (src/telemetry_nerd/model/bucket_state.py). */
export const STATE = { OK: 0, PARTIAL: 1, EMPTY: 2, ABSENT: 3, UNKNOWN: 4 } as const;
export const ROW_H = 8;
export const ROW_GAP = 2;
export const MAX_ROWS = 5;

export interface RugCell { row: number; x: number; w: number; state: number; missing: number; ts: number; i: number }
export interface RugStyle { tint: (row: number) => string; grey: string; line: string }

export const rugHeight = (rows: number): number => (rows === 0 ? 0 : Math.min(rows, MAX_ROWS) * (ROW_H + ROW_GAP) + 2);

export function rugCells(states: BucketStatePayload[], stepMs: number, toX: (ms: number) => number): RugCell[] {
  const out: RugCell[] = [];
  states.slice(0, MAX_ROWS).forEach((s, row) => {
    s.ts.forEach((t, i) => {
      const x0 = toX(t - stepMs), x1 = toX(t);
      const exp = s.expected[i] || 1;
      const missing = Math.max(0, Math.min(1, 1 - s.observed[i] / exp));
      out.push({ row, x: x0, w: Math.max(1, x1 - x0), state: s.state[i], missing, ts: t, i });
    });
  });
  return out;
}

const rowY = (row: number) => row * (ROW_H + ROW_GAP) + 1;

function hatch(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number) {
  ctx.save(); ctx.beginPath(); ctx.rect(x, y, w, h); ctx.clip(); ctx.beginPath();
  for (let d = -h; d < w; d += 4) { ctx.moveTo(x + d, y + h); ctx.lineTo(x + d + h, y); }
  ctx.stroke(); ctx.restore();
}

export function drawRug(ctx: CanvasRenderingContext2D, cells: RugCell[], style: RugStyle): void {
  ctx.lineWidth = 1;
  ctx.strokeStyle = style.line;
  for (const c of cells) {
    const y = rowY(c.row);
    if (c.state === STATE.OK || c.state === STATE.PARTIAL) {
      ctx.fillStyle = style.tint(c.row);
      ctx.fillRect(c.x, y, c.w, ROW_H);
      if (c.state === STATE.PARTIAL) {
        ctx.globalAlpha = Math.max(0.25, c.missing);
        ctx.fillStyle = style.grey;
        ctx.fillRect(c.x, y, c.w, ROW_H);
        ctx.globalAlpha = 1;
      }
    } else if (c.state === STATE.EMPTY) {
      ctx.fillStyle = style.grey;
      ctx.fillRect(c.x, y, c.w, ROW_H);
    } else if (c.state === STATE.ABSENT) {
      ctx.save(); ctx.setLineDash([1, 2]); ctx.beginPath();
      ctx.moveTo(c.x, y + ROW_H / 2); ctx.lineTo(c.x + c.w, y + ROW_H / 2); ctx.stroke(); ctx.restore();
    } else {
      hatch(ctx, c.x, y, c.w, ROW_H);
    }
  }
}

export function hitRug(cells: RugCell[], px: number, py: number): RugCell | null {
  for (const c of cells) {
    const y = rowY(c.row);
    if (px >= c.x && px < c.x + c.w && py >= y && py < y + ROW_H + ROW_GAP) return c;
  }
  return null;
}

const WORDS: Record<number, string> = {
  [STATE.OK]: "ok", [STATE.PARTIAL]: "fewer samples than expected", [STATE.EMPTY]: "no samples",
  [STATE.ABSENT]: "series not seen yet", [STATE.UNKNOWN]: "unknown (fetch failed or source could not tell)",
};

export function rugHint(cell: RugCell, s: BucketStatePayload, stepMs: number, name: string, resolutionMs: number): string {
  const obs = s.observed[cell.i], exp = s.expected[cell.i];
  const lines = [`${name}`, `${fmtRange(cell.ts - stepMs, cell.ts)} · ${WORDS[cell.state] ?? cell.state}`];
  if (cell.state !== STATE.ABSENT && cell.state !== STATE.UNKNOWN)
    lines.push(`${obs} of ${exp} expected samples (every ${resolutionMs / 1000}s)`);
  const seen = s.ts.filter((t, k) => t <= cell.ts && s.observed[k] > 0).at(-1);
  if (cell.state === STATE.EMPTY && seen !== undefined) lines.push(`last seen in bucket ending ${fmtRange(seen - stepMs, seen)}`);
  return lines.join("\n");
}
```

Check `fmtRange`'s real signature in `lib/format.ts` (two ms timestamps, one string) before
using it. Adapt the call if it differs.

- [ ] **Step 4: Integrate into `Panel.svelte`**

1. State and element: `let rugEl = $state<HTMLCanvasElement | null>(null); let rugCellsNow: RugCell[] = []; let rugTip = $state<{ x: number; y: number; text: string } | null>(null);`
2. Markup: directly after the `.plot` div for time panels:

```svelte
{#if data.kind === "time" && (data.bucket_state?.length ?? 0) > 0}
  <canvas class="rug" bind:this={rugEl} data-rug aria-label="Coverage rug: where data is missing"
    onmousemove={onRugMove} onmouseleave={() => (rugTip = null)}></canvas>
  {#if rugTip}<div class="rug-tip" style="left:{rugTip.x}px;top:{rugTip.y}px">{rugTip.text}</div>{/if}
{/if}
```

3. In the uPlot `draw` hook (next to the marginal drawing):

```ts
if (rugEl && d.kind === "time" && d.bucket_state?.length) {
  const left = u.bbox.left / dpr;
  const ctx2 = setupCanvas(rugEl, width, rugHeight(d.bucket_state.length));
  const order = new Map(d.series.map((s, k) => [s.id, k]));
  if (ctx2) {
    rugCellsNow = rugCells(d.bucket_state, d.effective_step_ms, (ms) => left + u.valToPos(ms / 1000, "x"));
    const css = getComputedStyle(el);
    drawRug(ctx2, rugCellsNow, {
      tint: (row) => rgbaOf(PALETTE[(order.get(d.bucket_state![row].id) ?? row) % PALETTE.length], 0.2),
      grey: css.getPropertyValue("--muted").trim(), line: css.getPropertyValue("--muted").trim(),
    });
  }
}
```

`rgbaOf` is the same helper `toUplot.ts` uses (`rgba`). Export it from `toUplot.ts` if it isn't
exported yet, and import it.

4. Hover handler:

```ts
function onRugMove(e: MouseEvent) {
  if (data?.kind !== "time" || !data.bucket_state) return;
  const c = hitRug(rugCellsNow, e.offsetX, e.offsetY);
  if (!c) { rugTip = null; return; }
  const s = data.bucket_state[c.row];
  const sd = data.series.find((x) => x.id === s.id);
  rugTip = { x: e.offsetX + 8, y: e.offsetY + 12, text: rugHint(c, s, data.effective_step_ms, sd ? seriesName(sd.labels) : s.id, data.dataset.resolution_ms) };
}
```

5. CSS: `.rug { display: block; } .rug-tip { position: absolute; white-space: pre; font-size: 11px; background: var(--fg); color: var(--bg); padding: 4px 6px; border-radius: 4px; pointer-events: none; z-index: 5; }`. The tip's parent must be `position: relative`; wrap the plot and rug in the existing `.plot` container if needed.

- [ ] **Step 5: Run tests, type-check, and look at it**

Run: `cd ui && npx vitest run && cd .. && just ui-check`
Expected: pass. Check manually against a dataset with a hole (Task 15 seeds one; until then, use
`HoleySource` through a unit-test daemon, or query with a step finer than resolution). The rug
appears only for the holey series, and the hover text reads correctly.

- [ ] **Step 6: Commit**

```bash
git add ui/src/chart/rug.ts ui/src/chart/rug.test.ts ui/src/Panel.svelte ui/src/chart/toUplot.ts
git commit -m "feat(ui): coverage rug with hover hints under time panels

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Heatmap state textures and rug

**Files:**
- Modify: `ui/src/chart/heatmap.ts` (`layoutHeatmap`)
- Modify: `ui/src/chart/heatmap.test.ts`
- Modify: `ui/src/components/HeatmapPlot.svelte`

**Interfaces:**
- Consumes: `HeatSeries.state` (Task 9), `STATE`, `rugCells`, `drawRug`, `hitRug`, `rugHint`, `rugHeight` (Task 11)
- Produces: `HeatLayout.missing` becomes `{ x: number; w: number; kind: "empty" | "unknown" }[]`; `HeatLayout.partial: Span[]` (columns observed below expected).

Rules (spec §7.2):
- no column, and the state says UNKNOWN → hatch
- no column otherwise (EMPTY, ABSENT, or no state) → dot texture
- a PARTIAL column's cells are drawn at α 0.45
- a blank cell inside an observed column means measured zero, so nothing is drawn there

- [ ] **Step 1: Write the failing test** (in `heatmap.test.ts`, reuse its series fixture)

```ts
it("marks missing columns by kind and partial columns", () => {
  const series = { ...fixture, ts: [120_000], n: [5], cover: [1],
    state: { id: fixture.id, ts: [60_000, 120_000, 180_000], state: [4, 1, 2], observed: [0, 1, 0], expected: [2, 2, 2], flags: [0, 0, 0] } };
  const l = layoutHeatmap(series, { width: 300, height: 100, startMs: 60_000, endMs: 180_000, stepMs: 60_000, nMin: 0, color: "count" });
  expect(l.missing.map((m) => m.kind)).toEqual(["unknown", "empty"]);
  expect(l.partial).toHaveLength(1);
});
```

Use the file's real fixture name for `fixture`. The cells part can be empty: `cells: { ts: [], lo: [], hi: [], c: [] }`.

- [ ] **Step 2: Run and check it fails**

Run: `cd ui && npx vitest run src/chart/heatmap.test.ts`
Expected: FAIL (`kind` undefined, `partial` undefined).

- [ ] **Step 3: Implement**

In `layoutHeatmap`:

```ts
  const stateAt = new Map((s.state?.ts ?? []).map((t, i) => [t, s.state!.state[i]]));
  const missing: (Span & { kind: "empty" | "unknown" })[] = [];
  for (let t = x0Ms + k; t <= x1Ms; t += k)
    if (!nAt.has(t)) missing.push({ ...col(t), kind: stateAt.get(t) === 4 ? "unknown" : "empty" });
  const partial = [...stateAt].filter(([, v]) => v === 1).map(([t]) => col(t));
  const partialTs = new Set([...stateAt].filter(([, v]) => v === 1).map(([t]) => t));
```

Add `partial` to the returned object and to `HeatLayout`. In the rect mapping, set
`lowN: (n > 0 && n < o.nMin) || partialTs.has(ts)` so partial columns fade through the existing
`lowN` alpha. (Rename the field only if a reviewer asks; its draw behaviour is exactly "dim".)
Use `STATE.UNKNOWN`/`STATE.PARTIAL` from `rug.ts` instead of 4/1 literals.

In `HeatmapPlot.svelte`:
- Replace `for (const m of l.missing) hatch(...)` with
  `for (const m of l.missing) m.kind === "unknown" ? hatch(ctx, m.x, m.w, plotH) : dots(ctx, m.x, m.w, plotH);`
- Add the dot texture next to `hatch`:

```ts
  function dots(ctx: CanvasRenderingContext2D, x: number, w: number, h: number) {
    ctx.save(); ctx.fillStyle = ctx.strokeStyle as string;
    for (let yy = 3; yy < h; yy += 6) for (let xx = x + 3; xx < x + w; xx += 6) ctx.fillRect(xx - 0.75, yy - 0.75, 1.5, 1.5);
    ctx.restore();
  }
```

- Add a rug under the heatmap when `series.state` is set: a `<canvas class="rug" data-rug>` below
  the heatmap canvas, sized `plotW × rugHeight(1)`, with `margin-left: AXIS_LEFT px`. `rugCells`
  needs `toX(ms)`, the x of a bucket edge. `timeColumns(...).col(t)` returns `{ x, w }` for the
  column whose bucket *ends* at `t`, so the column's right edge `col(t).x + col(t).w` is the x of
  time `t`:

```ts
const { col } = timeColumns(data.dataset.start_ms, data.dataset.end_ms, data.effective_step_ms, plotW);
const toX = (ms: number) => { const c = col(ms); return c.x + c.w; };
const cells = rugCells([series.state], data.effective_step_ms, toX);
```

  Read `timeColumns` in `heatmap.ts` once to confirm `col(t)` covers `(t − step, t]`. If it
  covers `[t, t + step)` instead, use `toX = (ms) => col(ms).x`. Hover works as in Task 11, with
  tooltip text from `rugHint`.

- [ ] **Step 4: Run tests, type-check, look**

Run: `cd ui && npx vitest run && cd .. && just ui-check`
Expected: pass. Check a heatmap manually: zero-traffic columns are blank, missing columns have
dots, and the rug appears only when a state is present.

- [ ] **Step 5: Commit**

```bash
git add ui/src/chart/heatmap.ts ui/src/chart/heatmap.test.ts ui/src/components/HeatmapPlot.svelte
git commit -m "feat(ui): heatmap missing columns textured by state, partial columns dimmed, rug

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Window coverage badge

**Files:**
- Create: `ui/src/lib/coverage.ts`
- Test: `ui/src/lib/coverage.test.ts`
- Modify: `ui/src/Panel.svelte` (histogram/ECDF/CCDF branch)
- Modify: spec §7.3 (decision 5)

**Interfaces:**
- Consumes: `WindowHist.columns`, `.expected_columns`, `.unknown` (Tasks 7, 9)
- Produces: `export function windowBadge(w: WindowHist, stepMs: number): { text: string; title: string } | null`, which returns `null` when the window is fully covered and has no unknown data.

- [ ] **Step 1: Write the failing test**

```ts
// ui/src/lib/coverage.test.ts
import { describe, expect, it } from "vitest";
import { windowBadge } from "./coverage";

const w = (columns: number, expected: number, unknown = false) =>
  ({ label: "now", start_ms: 0, end_ms: 600_000, n: 10, columns, expected_columns: expected, unknown }) as never;

describe("windowBadge", () => {
  it("is silent when the window is complete", () => expect(windowBadge(w(10, 10), 60_000)).toBeNull());
  it("states coverage and missing time", () => {
    expect(windowBadge(w(8, 10), 60_000)).toEqual({ text: "now: covers 80% · 2m missing", title: expect.stringContaining("2 of 10 steps") });
  });
  it("flags unknown data", () => expect(windowBadge(w(10, 10, true), 60_000)?.text).toContain("unknown"));
});
```

- [ ] **Step 2: Run and check it fails**

Run: `cd ui && npx vitest run src/lib/coverage.test.ts`
Expected: FAIL, module not found.

- [ ] **Step 3: Implement**

```ts
// ui/src/lib/coverage.ts
import type { WindowHist } from "./api";
import { fmtStep } from "./format";

/** Coverage of a window view (no time axis): a badge only when something is missing. */
export function windowBadge(w: WindowHist, stepMs: number): { text: string; title: string } | null {
  const exp = w.expected_columns ?? w.columns;
  const miss = Math.max(0, exp - w.columns);
  if (miss === 0 && !w.unknown) return null;
  const parts = [`${w.label}: covers ${Math.round((100 * w.columns) / Math.max(1, exp))}%`];
  if (miss > 0) parts.push(`${fmtStep(miss * stepMs)} missing`);
  if (w.unknown) parts.push("part unknown");
  return {
    text: parts.join(" · "),
    title: `${miss} of ${exp} steps have no data${w.unknown ? "; some could not be fetched" : ""}. Fractions and counts in this window cover only the observed steps.`,
  };
}
```

Check that `fmtStep` lives in `lib/format.ts` (Panel.svelte uses it). If it lives elsewhere,
import it from there. If `fmtStep(120000)` doesn't produce `"2m"`, adjust the test string to its
real output.

In `Panel.svelte`, for `data.kind === "histogram"`, render the badges above the plot:

```svelte
{#each data.series.flatMap((s) => s.windows.map((w) => windowBadge(w, data.effective_step_ms)).filter((b) => b !== null)) as b, i (i)}
  <span class="chip caveat" data-window-coverage title={b.title}>{b.text}</span>
{/each}
```

- [ ] **Step 4: Update the spec (decision 5)**

In spec §7.3, replace "Coverage badge (...), click → mini rug of the window." with
"Coverage badge (...) with a tooltip listing missing steps; a mini rug only if real use shows the
tooltip is not enough."

- [ ] **Step 5: Run, type-check, commit**

Run: `cd ui && npx vitest run && cd .. && just ui-check`

```bash
git add ui/src/lib/coverage.ts ui/src/lib/coverage.test.ts ui/src/Panel.svelte docs/superpowers/specs/2026-10-02-series-bundles-missing-data-design.md
git commit -m "feat(ui): coverage badge on window views

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 14: Footer ↔ graph highlighting

**Files:**
- Create: `ui/src/chart/focus.ts`
- Test: `ui/src/chart/focus.test.ts`
- Modify: `ui/src/Panel.svelte`, `ui/src/components/HeatmapPlot.svelte`

**Interfaces:**
- Consumes: `Note.where` (Task 9), rug cells (Task 11)
- Produces:
  - `export function focusRects(where: Where | null | undefined, toX: (ms: number) => number, x0: number, x1: number): { x: number; w: number }[]`: spans clipped to the plot's x range; `[]` for no spans.
  - `export function notesAt(notes: Note[], seriesId: string, ms: number): string[]`: keys of notes whose `where` covers that series and time. Used to light up footer entries while hovering the rug.

Behaviour:
- hovering a footer note with `where.spans` paints a translucent `--warn` band (α 0.15) over those spans on the plot
- hovering a rug cell adds class `active` to the footer notes covering it

- [ ] **Step 1: Write the failing tests**

```ts
// ui/src/chart/focus.test.ts
import { describe, expect, it } from "vitest";
import { focusRects, notesAt } from "./focus";

describe("focus", () => {
  it("maps spans to clipped rects", () => {
    const toX = (ms: number) => ms / 1000;
    expect(focusRects({ spans: [[0, 60_000], [100_000, 400_000]] }, toX, 10, 300)).toEqual([{ x: 10, w: 50 }, { x: 100, w: 200 }]);
    expect(focusRects(null, toX, 0, 100)).toEqual([]);
  });
  it("finds notes covering a series and time", () => {
    const notes = [
      { kind: "caveat" as const, key: "missing_data:0", text: "", where: { spans: [[0, 60_000]] as [number, number][], series: ["a"] } },
      { kind: "caveat" as const, key: "untrusted_data:1", text: "", where: { spans: [[0, 60_000]] as [number, number][], series: null } },
      { kind: "caveat" as const, key: "settling", text: "" },
    ];
    expect(notesAt(notes, "a", 30_000)).toEqual(["missing_data:0", "untrusted_data:1"]);
    expect(notesAt(notes, "b", 30_000)).toEqual(["untrusted_data:1"]);
  });
});
```

- [ ] **Step 2: Run and check it fails**

Run: `cd ui && npx vitest run src/chart/focus.test.ts`
Expected: FAIL, module not found.

- [ ] **Step 3: Implement**

```ts
// ui/src/chart/focus.ts
import type { Where } from "../lib/api";
import type { Note } from "../lib/panelNotes";

export function focusRects(where: Where | null | undefined, toX: (ms: number) => number, x0: number, x1: number): { x: number; w: number }[] {
  return (where?.spans ?? []).flatMap(([a, b]) => {
    const l = Math.max(x0, toX(a)), r = Math.min(x1, toX(b));
    return r > l ? [{ x: l, w: r - l }] : [];
  });
}

export function notesAt(notes: Note[], seriesId: string, ms: number): string[] {
  return notes
    .filter((n) => n.where?.spans?.some(([a, b]) => ms > a && ms <= b) && (!n.where.series || n.where.series.includes(seriesId)))
    .map((n) => n.key);
}
```

In `Panel.svelte`:
- `let focus = $state<Where | null>(null); let activeNotes = $state<string[]>([]);`
- On each footer `<li>`: `onmouseenter={() => { focus = note.where ?? null; plot?.redraw(false); }}`, `onmouseleave={() => { focus = null; plot?.redraw(false); }}`, and `class:active={activeNotes.includes(note.key)}`.
- In the uPlot `draw` hook, before `drawAnnotations`:

```ts
if (focus) {
  const c = u.ctx, l = u.bbox.left, r = l + u.bbox.width;
  c.save(); c.fillStyle = getComputedStyle(el).getPropertyValue("--warn").trim(); c.globalAlpha = 0.15;
  for (const f of focusRects(focus, (ms) => u.valToPos(ms / 1000, "x", true), l, r)) c.fillRect(f.x, u.bbox.top, f.w, u.bbox.height);
  c.restore();
}
```

- In `onRugMove`, after computing the cell, set `activeNotes = notesAt(notes, s.id, c.ts)`. On leave, set `activeNotes = []`.
- CSS: `.note.active { outline: 1px solid var(--warn); }`.
- Pass `focus` to `HeatmapPlot` as a prop (`focusWhere`) and paint the same band in its draw effect, using its `col()`-based x mapping.

`focus` is read inside the draw hook through a `$state` variable. The hook closure reads the
current value at draw time; `untrack` is not needed. Check that svelte-check doesn't warn about
it.

- [ ] **Step 4: Run, type-check, look, commit**

Run: `cd ui && npx vitest run && cd .. && just ui-check`. Check manually: hover a missing-data
note and its span lights up; hover the rug and the note gets an outline.

```bash
git add ui/src/chart/focus.ts ui/src/chart/focus.test.ts ui/src/Panel.svelte ui/src/components/HeatmapPlot.svelte
git commit -m "feat(ui): footer caveats highlight where they apply; rug lights its caveats

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 15: Demo data with gaps, e2e, and bookkeeping

**Files:**
- Modify: `src/telemetry_nerd/devtools/synthetic.py` (`demo_text`)
- Modify: `tests/unit/test_synthetic.py`
- Create: `ui/e2e/missing-data.spec.ts`
- Modify: `docs/telemetry-graphing-guide.md` §10 row 17 (mark done)

**Interfaces:**
- Produces: `demo_text` emits `tn_demo_gappy_seconds{instance="d"}` with a 15-minute hole starting at 1/3 of the range, and `tn_demo_gappy_seconds{instance="e"}` starting at 1/2 of the range (late born).

- [ ] **Step 1: Write the failing synthetic test**

```python
def test_demo_text_has_gappy_and_late_series():
    start, end = 0, 6 * 3_600_000
    lines = [l for l in demo_text(start, end).splitlines() if l.startswith("tn_demo_gappy_seconds")]
    d = [int(l.rsplit(" ", 1)[1]) for l in lines if 'instance="d"' in l]
    e = [int(l.rsplit(" ", 1)[1]) for l in lines if 'instance="e"' in l]
    hole = (end - start) // 3
    assert not any(hole <= t < hole + 15 * 60_000 for t in d) and d
    assert min(e) >= (end - start) // 2
```

Check the exposition line format in `exposition()` first (the timestamp position). Adapt the
parsing if the timestamp is not the last token.

- [ ] **Step 2: Run and check it fails; implement; run and check it passes**

Run: `uv run pytest tests/unit/test_synthetic.py -q` → FAIL.

In `demo_text`, before `return`:

```python
    hole_start = start_ms + (end_ms - start_ms) // 3
    hole_start -= hole_start % interval_ms
    hole_end = hole_start + 15 * 60_000
    born = start_ms + (end_ms - start_ms) // 2
    born -= born % interval_ms
    gappy_d = [(ts, 0.05) for ts in range(start_ms, end_ms, interval_ms) if not hole_start <= ts < hole_end]
    gappy_e = [(ts, 0.07) for ts in range(born, end_ms, interval_ms)]
    parts.append(exposition("tn_demo_gappy_seconds", {"instance": "d"}, gappy_d))
    parts.append(exposition("tn_demo_gappy_seconds", {"instance": "e"}, gappy_e))
```

Run again → PASS. Also run `uv run pytest tests/unit -q` and check that no test counts demo
series exactly; fix any that do.

- [ ] **Step 3: Write the e2e spec**

```ts
// ui/e2e/missing-data.spec.ts
import { expect, test } from "@playwright/test";

test("a series with a hole shows a coverage rug and a located caveat", async ({ page, request }) => {
  const q = await request.post("/api/query", {
    data: { expr: "tn_demo_gappy_seconds", start: "now-6h", end: "now-10m", step: "1m" },
  });
  expect(q.ok()).toBeTruthy();
  const { dataset, summary } = await q.json();
  expect(summary.caveats).toContain("missing_data");
  const s = await request.post("/api/show", { data: { dataset, question: "Where is data missing?" } });
  const { panel } = await s.json();
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("[data-rug]")).toBeVisible();
  const note = el.locator('[data-note^="missing_data:"]').first();
  await expect(note).toContainText("no samples");
  await note.hover();
  const box = await el.locator("[data-rug]").boundingBox();
  if (!box) throw new Error("rug not laid out");
  await page.mouse.move(box.x + box.width / 2 - 40, box.y + 4);
  // the hint text is checked loosely: the hole sits at 1/3 of the 6h range
  await expect(el.locator(".rug-tip")).toBeVisible();
});
```

The rug position assumption (hovering near the middle-left hits the hole region) is loose; the
assertion only needs the tip to appear. If the hole lands outside the hover point, compute the x
of the first `missing_data` span from the panel data API instead
(`/api/panels/{id}/data`, see `api/app.py` `panel_data`).

- [ ] **Step 4: Run e2e**

Run: `just dev-up` (if not running), then `cd ui && npx playwright test e2e/missing-data.spec.ts`.
Global setup seeds the synthetic data.
Expected: PASS. If the daemon start for e2e follows a different command, use the one in
`ui/playwright.config.ts`.

- [ ] **Step 5: Full gates**

Run: `just lint && uv run pytest tests/unit -q && cd ui && npx vitest run && cd .. && just ui-check`
Expected: all green.

- [ ] **Step 6: Bookkeeping**

- In `docs/telemetry-graphing-guide.md` §10, change row 17's bead column to
  `spec 2026-10-02; phases 1–4 done (plan 2026-10-02-missing-data-bucket-state)`.
- `bd create "Source missing-data profiles (bucket_state phase 5)" -t feature -p 2 --parent telemetry-nerd-1h9 --deps "blocked-by:telemetry-nerd-1h9.10" -d "Spec 2026-10-02 §6: MissingDataSemantics per adapter, set source_filled/stale_marker/interval_change/reset flags, fixture-pinned. Turns trailing silence into absent where staleness proves an end."`
- `bd create "Group density cloud with silent members (bucket_state phase 6)" -t feature -p 2 -d "Spec 2026-10-02 §7.1: cloud mark normalized by alive members, silent members as outliers, group rug row from merge(). Needs its own visual brainstorm."`

- [ ] **Step 7: Commit**

```bash
git add src/telemetry_nerd/devtools/synthetic.py tests/unit/test_synthetic.py ui/e2e/missing-data.spec.ts docs/telemetry-graphing-guide.md
git commit -m "test(e2e): missing data rug and caveat on a gappy demo series

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Spec coverage (self-review)

| spec section | task |
|---|---|
| §3.1 bundle shape | 4 (on-read `Bundle`; decision 4) |
| §3.2 registry | 4 |
| §3.3 chaining, undeclared → drop + caveat, completeness test | 4 |
| §3.4 Claude summaries | 6 |
| §4.1 caveat model | 3 |
| §4.2 visual forms | 11 (rug), 12 (textures), 13 (badge); low_count/y_zoomed already exist; reset/interval markers need phase 5 flags |
| §4.3 footer index + linking | 9, 14 |
| §4.4 findings | 8 |
| §4.5 migration | 6, 9 (codes kept, located added) |
| §5.1 fields | 1 |
| §5.2 merge | 2 (used by group views in phase 6) |
| §5.3 coarsen | 2 (decision 3) |
| §5.4 fetch failures | 5 |
| §6 source semantics | phase 5 bead (Task 15) |
| §7.1 stepped, rug | 10, 11; group cloud → phase 6 bead |
| §7.2 heatmaps | 7, 12 |
| §7.3 window views | 7, 13 (decision 5) |
| §7.4 hover hints | 11, 12 |
| §7.5 accessibility | 11 (pattern encodings, `--muted`), 12 |
| §8 error handling | 5 (partial failure, all-fail raises); unverified-profile rule → phase 5 |
| §9 testing | 1–15 |
