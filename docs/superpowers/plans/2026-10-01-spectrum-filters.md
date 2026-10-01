# Spectrum, Spectrogram and Filters: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to work through this plan task by task. Steps use checkbox (`- [ ]`) syntax. Beads: `telemetry-nerd-4ok.7` (spectrum), `4ok.8` (spectrogram, blocked by 4ok.7), `4ok.9` (filters, builds on 2as.17 y-views). All three are in epic M4 `telemetry-nerd-4ok`.

**Goal**
- Claude can ask which periods a series contains. The answer is a list of dominant periods, each with an interval, a false-alarm probability and the range of periods the data can resolve. The same analysis can be drawn as a **spectrum** panel (power against a log period axis in human units) or a **spectrogram** panel (time × period heatmap).
- Claude can derive a **low-, high- or band-pass filtered** dataset whose cutoff is stated as a period. Showing it creates a panel with the views filtered / raw / overlay, and the user switches between them.
- The rule "never aggregate or filter percentiles over time" still holds everywhere.

**Architecture**
- **Pure numeric code** lives in `analysis/spectrum.py` (Lomb-Scargle, Baluev false-alarm probability, peaks, spectrogram) and `analysis/filters.py` (zero-phase Gaussian low/high/band-pass, segment splitting, edge marking). Neither does I/O.
- **Preconditions** live in `analysis/timeops.py`: refuse percentile and distribution datasets and raw counters, each with a hint. Raw counters are detected through the catalog by `charts/units.raw_counters`.
- **Ops glue** is `core/signal_ops.py` (`SignalOps`). It loads the dataset, checks preconditions, coarsens to a point cap, memoizes the spectrum, and writes derived datasets.
- **Derived datasets:**
  - They use the same `BUCKET_SCHEMA` and the **same series ids** as their parent, so lineage is trivial.
  - `DatasetMeta.derived = {op, from, label, reason, period_ms, period_hi_ms, edges}`.
  - They are written by `DatasetStore.put_derived`.
- **Spectra are not stored.** They are a deterministic view of an immutable dataset: the layer stores the parameters, and `panel_data` recomputes them through a small LRU.
- **Access points:**
  - MCP: `spectrum(dataset)` gives a summary plus `evidence`. `filter(dataset, kind, period, reason)` gives a derived dataset.
  - Drawing still goes through `show`, which gains:
    - `mark="spectrum" | "spectrogram"`, with `segment` and `overlap`;
    - `view=` for filtered datasets.
- **Views:**
  - `ChartSpec.signal: SignalViews` holds the offered views, Claude's default with its reason, and the user's `selected` view.
  - Selection follows the y-view pattern exactly: `WorkspaceService.select_data_view` (`@atomic`, spec write plus event), the ambient event `panel.data_view_selected`, and `POST /api/panels/{id}/data-view`.
- **Client:**
  - `toUplot` gains a `context` series (faint raw underneath, or a dashed removed part) and dashed edge spans.
  - New `SpectrumPlot.svelte` (uPlot, log-x) and `SpectrogramPlot.svelte` (canvas). The spectrogram reuses `valueAxis`/`cellSpan`/`colormap`/`setupCanvas` from the heatmap.

**Tech stack:** Python ≥3.12, uv, pydantic v2, polars, **numpy (new)**, pytest; Svelte 5 + TypeScript + uPlot, vitest, Playwright.

**Spec references**
- §3.2: representation, provenance.
- §3.3: invariants. A statistic needs an interval.
- §5.1: preconditions, uncertainty propagation, caveats, autocorrelation.
- §6.1: `spectrogram` mark.
- §6.3: no dual axes, no interpolation across gaps.
- §6.4: metric card spectrum thumbnail (follow-up bead).
- Guide §3, §7 #6/#15: smoothing label, with raw/envelope kept underneath; Nyquist cut at 2×step (Hartmann SfE "beam effect").

## Global Constraints

- Work on `master`. Commit after every task with the trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Run `just lint && just test` after every task; UI tasks also run `just ui-test && just ui-check && just ui-build`.
- **Never run `just e2e`**: it wipes the dev VictoriaMetrics volume. Run only the new spec: `cd ui && npx playwright test e2e/signal.spec.ts`.
- **Hard refusals.** Each one is a `ValueError` whose text states the reason and contains a hint:
  - **Percentile datasets (`representation="quantile"`)** are refused by spectrum, spectrogram and filter. Hint: apply the op to the rate, or to threshold counts from the histogram (`query_distribution` + `fraction_over`), or to `histogram_sum/histogram_count` rates.
  - **Distribution datasets** are refused by all three ops.
  - **Raw counters** (a catalog type `counter` used outside a rate-like call) are refused by all three ops. Hint: `rate(x[$__rate_interval])`.
  - **Filtering a filtered dataset** is refused. Hint: band-pass.
- **No interpolation, ever.**
  - Lomb-Scargle evaluates only at observed bucket times.
  - Filters split a series at every missing step, and no kernel crosses a gap.
  - Points within 3σ of a segment edge are flagged `edge`. They are drawn dashed and excluded from the summary statistics.
- **Resolution honesty:**
  - The shortest period examined is 2 × effective step (Nyquist). The longest is range/2 (two cycles).
  - The spectrum x axis shows hatched zones beyond both limits, labelled with the reason.
  - When the input was coarsened to the point cap, a `coarsened` caveat names the new shortest period.
- **Uncertainty on every period:**
  - The interval is the half-power width of the peak, widened to at least the 1/range resolution.
  - Each peak also carries `fap`, `local_ratio` and `significant`.
  - Only significant peaks get an `evidence` statistic: `exact=false`, with an interval.
- Cutoffs are periods (durations), never coefficients. A filter carries a required one-line `reason`, which is rendered on the panel.

## Decisions (justified; flagged for the user at the end)

1. **Add numpy, not scipy or astropy.**
   - Lomb-Scargle is O(N·F) trigonometry. Pure Python takes minutes at N=4096 and F≈10k; numpy takes about 1 s, chunked to bound memory.
   - FFT convolution makes the filters O(N log N).
   - scipy (about 40 MB plus Fortran) would only give `signal.lombscargle` (no false-alarm probability) and `filtfilt`, which we deliberately do not use (see 3).
   - astropy's `LombScargle` is a large dependency for about 60 lines of code. We port its Baluev false-alarm formula and cite it.
   - numpy also unlocks `polars.to_numpy`.
2. **Lomb-Scargle (generalised, floating mean, standard normalisation) over FFT/Welch.**
   - Telemetry has gaps and partial buckets. Lomb-Scargle needs no resampling, so nothing is interpolated.
   - Its power is "share of variance a sinusoid at f explains", in [0, 1]. That is comparable across series and across spectrogram windows, and readable by a user.
   - A linear trend is removed first (bead requirement); it otherwise leaks power into long periods.
3. **Zero-phase Gaussian filters, not Butterworth `filtfilt`.**
   - A Gaussian has no ringing or overshoot, so it never draws a dip before a step change that is not in the data. It has zero phase shift (centred kernel) and a closed-form response, so "cutoff = half-power period" is exact:
     - low-pass: σ = 0.1325·P;
     - high-pass (raw − low-pass): σ = 0.2494·P;
     - band-pass: difference of the two, with `period_hi ≥ 4·period` so the edges stay honest.
   - Its kernel has finite support (±3σ), which makes the warm-up/edge zones exact and markable.
   - The cost is a softer roll-off than Butterworth. That is acceptable for views, and the label says "Gaussian".
4. **Ops are separate MCP tools, drawing stays in `show`.** This mirrors `query` → `show` and `query_distribution` → `show(mark=...)`.
   - `filter` creates an immutable derived dataset (§3.3), which can be cited, spectrum-analysed and shown.
   - `spectrum` returns numbers with intervals for findings, without forcing a panel.
   - Spectra are not stored, because they recompute deterministically from immutable inputs. Persistence can come with the M4 op registry/node model.
5. **The filtered/raw view is a separate `signal` choice, not a `YView` mode.**
   - A y-view chooses a scale; a data view chooses which series are drawn. They combine freely, for example "raw" plus "log".
   - It reuses the 2as.17 mechanics one to one: a spec field, an atomic write plus an ambient event, an optimistic client, and a control row styled like `.y-views`.
   - Claude sets the **default view** when calling `show(view=...)`; the filter's `reason` is the argument for it.
6. **Which views each filter offers.** Both use one y scale, so there are no dual axes:
   - **Low-pass:** `overlay` (filtered line over the faint raw line and envelope; the default, per guide §3 "smoothed line **over** raw/envelope"), `filtered`, `raw`.
   - **High-pass and band-pass:** `filtered` (the residual, around 0; default), `removed` (raw with the removed slow part dashed over it), `raw`.
   - Overlaying a residual around 0 on raw around its baseline would squash one of them, so high/band-pass get `removed` instead of `overlay`.
   - The client switches views instantly, with no refetch: the payload carries filtered + raw (+ removed).
7. **Significance has two parts.**
   - The white-noise false-alarm probability (Baluev 2008) is overconfident for autocorrelated telemetry (§5.1). Red noise inflates long periods.
   - A peak is therefore `significant` only when FAP < 1% **and** its power is ≥ 10× the median power within ±½ octave. The ratio is skipped when that band has fewer than 8 grid points, which is typical for periods near range/2.
   - A `red_noise` caveat fires when low-frequency median power is ≥ 5× the high-frequency median.
   - A `sampling_artifact` flag fires when the spectral window |Σe^{2πift}|²/N² exceeds 0.1 at the peak, which catches periodic gaps such as scrape outages.
8. **Caps.**
   - Spectrum: at most 4096 points per series. Larger inputs are coarsened with `lod()` (count-weighted mean, which is a proper anti-alias low-pass), with a caveat.
   - Spectrogram: at most 8192 points, at most 400 columns. A larger request is refused with a hint (longer segment or less overlap).
   - The row count is reduced by max-power merging on the log-period pixel grid. The legend says "max within row", the same honesty as M4.
9. **Preconditions per series:** at least 32 points, at most 50% gaps, not constant. A failing series is skipped and listed with a caveat. If no series qualifies, the op is refused.

## File Structure

```
pyproject.toml, uv.lock                        # MOD numpy
src/telemetry_nerd/devtools/synthetic.py      # MOD periodic_buckets (known periods, onsets, gaps)
src/telemetry_nerd/analysis/spectrum.py       # NEW lomb_scargle, fap, level, Peak, spectrum, spectrogram, log_bins
src/telemetry_nerd/analysis/filters.py        # NEW FilterSpec, segments, apply, filter_buckets, removed_table
src/telemetry_nerd/analysis/timeops.py        # NEW time_op_problem (representation/counter refusals + hints)
src/telemetry_nerd/charts/units.py            # MOD raw_counters(expr, lookup)
src/telemetry_nerd/charts/dataview.py         # NEW DataView, VIEW_LABELS, offered_views, SignalViews
src/telemetry_nerd/charts/spec.py             # MOD marks spectrum/spectrogram, Layer.role/segment_ms/overlap, ChartSpec.signal, validate
src/telemetry_nerd/datasets/store.py          # MOD DatasetMeta.derived, put_derived (+ _insert_rows refactor)
src/telemetry_nerd/core/signal_ops.py         # NEW SignalOps: spectrum (memo), filter, payload builders
src/telemetry_nerd/core/service.py            # MOD spectrum/filter wrappers, show(view, segment, overlap), panel_data kinds
src/telemetry_nerd/core/workspace_service.py  # MOD select_data_view, _check uses drawn tables, brief data_view
src/telemetry_nerd/core/events.py             # MOD AMBIENT_TYPES += panel.data_view_selected
src/telemetry_nerd/channel/format.py          # MOD describe_event
src/telemetry_nerd/api/app.py                 # MOD POST /api/panels/{id}/data-view
src/telemetry_nerd/mcp/server.py              # MOD spectrum, filter tools; show(view, segment, overlap); INSTRUCTIONS
ui/src/chart/period.ts (+test)                # NEW fmtPeriod, periodTicks
ui/src/chart/spectrum.ts (+test)              # NEW toSpectrumUplot, limitZones, peakMarks
ui/src/chart/spectrogram.ts (+test)           # NEW layoutSpectrogram
ui/src/chart/toUplot.ts (+test)               # MOD context series, edge spans
ui/src/chart/dataview.ts (+test)              # NEW drawnFor(view, payload)
ui/src/components/SpectrumPlot.svelte         # NEW
ui/src/components/SpectrogramPlot.svelte      # NEW
ui/src/Panel.svelte, lib/api.ts, lib/panelNotes.ts  # MOD
tests/unit/test_spectrum.py, test_filters.py, test_signal_service.py, test_synthetic.py  # NEW/MOD
ui/e2e/signal.spec.ts                         # NEW
```

---

### Task 1: numpy + synthetic signals with known periods

**Files:** `pyproject.toml`, `uv.lock`, `devtools/synthetic.py`, `tests/unit/test_synthetic.py`.

- [ ] **Step 1:** `uv add numpy`, then confirm that `uv run python -c "import numpy"` works.
- [ ] **Step 2: Failing test**

```python
# tests/unit/test_synthetic.py (append)
from telemetry_nerd.devtools.synthetic import periodic_buckets

def test_periodic_buckets_known_components_gaps_and_onset():
    r = periodic_buckets(0, 3_600_000, 60_000, [(600_000, 2.0, None), (120_000, 1.0, 1_800_000)],
                         base=10.0, gaps=[(600_000, 900_000)])
    ts = r.buckets.column("ts_ms").to_pylist()
    assert 660_000 not in ts and 600_000 not in ts and 0 in ts and 3_600_000 in ts
    avg = dict(zip(ts, r.buckets.column("avg").to_pylist()))
    assert abs(avg[0] - 10.0) < 1e-9                      # sin(0)=0, onset not reached
    assert r.buckets.column("min").to_pylist() == r.buckets.column("avg").to_pylist()
```

- [ ] **Step 3: Implement**

```python
# devtools/synthetic.py (append)
from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult, labels_json, series_id

def periodic_buckets(start_ms, end_ms, step_ms, components, *, base=0.0, noise=0.0, seed=0,
                     gaps=(), labels=None, source="synthetic") -> FetchResult:
    """Ground truth for spectrum/filter tests. components: (period_ms, amplitude, onset_ms|None).
    gaps: [(t0, t1)) with no rows at all (never zero-filled)."""
    rnd = random.Random(seed)
    lb = labels or {"job": "synthetic"}
    sid = series_id(source, lb)
    ts, vals = [], []
    for t in range(start_ms, end_ms + 1, step_ms):
        if any(a <= t < b for a, b in gaps):
            continue
        v = base + rnd.gauss(0, noise) if noise else base
        for period, amp, onset in components:
            if onset is None or t >= onset:
                v += amp * math.sin(2 * math.pi * t / period)
        ts.append(t)
        vals.append(v)
    n = len(ts)
    table = pa.table({"ts_ms": ts, "series_id": [sid] * n, "avg": vals, "min": vals, "max": vals,
                      "count": [1] * n}, schema=BUCKET_SCHEMA)
    return FetchResult(table, pa.table({"series_id": [sid], "labels": [labels_json(lb)]}, schema=SERIES_SCHEMA))
```

- [ ] **Step 4:** Run `uv run pytest tests/unit/test_synthetic.py -q`. Commit: `chore: numpy; synthetic periodic series with known periods (4ok.7)`.

---

### Task 2: Lomb-Scargle, false-alarm probability, peaks, spectrogram (pure)

**Files:** create `analysis/spectrum.py` and `tests/unit/test_spectrum.py`.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_spectrum.py
import numpy as np
from telemetry_nerd.analysis.spectrum import fap, level, lomb_scargle, spectrogram, spectrum
from telemetry_nerd.devtools.synthetic import periodic_buckets

DAY, M = 86_400_000, 60_000

def arrays(r):
    return np.array(r.buckets.column("ts_ms").to_pylist()), np.array(r.buckets.column("avg").to_pylist())

def near(peaks, period_ms, tol):
    return [p for p in peaks if abs(p.period_ms - period_ms) <= tol]

def test_recovers_5m_and_24h_above_significance():
    ts, y = arrays(periodic_buckets(0, 4 * DAY, 2 * M, [(5 * M, 3, None), (DAY, 5, None)], noise=1, seed=1))
    sp = spectrum(ts, y, 2 * M, top=5)
    assert (sp.shortest_ms, sp.longest_ms) == (4 * M, 2 * DAY)
    five, day = near(sp.peaks, 5 * M, 10_000), near(sp.peaks, DAY, 6 * 3_600_000)
    assert five and five[0].significant and five[0].lo_ms <= 5 * M <= five[0].hi_ms
    assert day and day[0].significant and day[0].lo_ms <= DAY <= day[0].hi_ms
    assert day[0].hi_ms - day[0].lo_ms >= DAY / 8                # resolution floor: 4 cycles cannot be sharp

def test_gappy_input_without_interpolation():
    r = periodic_buckets(0, 4 * DAY, 2 * M, [(5 * M, 3, None), (DAY, 5, None)], noise=1, seed=2,
                         gaps=[(DAY, DAY + 6 * 3_600_000), (3 * DAY, 3 * DAY + 3_600_000)])
    ts, y = arrays(r)
    sp = spectrum(ts, y, 2 * M)
    assert near(sp.peaks, 5 * M, 10_000)[0].significant and near(sp.peaks, DAY, 6 * 3_600_000)

def test_white_noise_false_alarms_are_calibrated():
    rng = np.random.default_rng(3)
    t = np.arange(300) * 60_000
    alarms = sum(spectrum(t, rng.normal(size=300), 60_000, top=1).peaks[0].fap < 0.05 for _ in range(100))
    assert alarms <= 12                                           # nominal 5

def test_level_inverts_fap():
    t = np.arange(500.0)
    z = level(0.01, 500, 0.5, t)
    assert abs(float(fap(z, 500, 0.5, t)) - 0.01) < 1e-4

def test_power_is_share_of_variance():
    t = np.arange(1000.0)
    p = lomb_scargle(t, np.sin(2 * np.pi * t / 50), np.array([1 / 50, 1 / 7]))
    assert p[0] > 0.99 and p[1] < 0.01

def test_spectrogram_shows_2m_oscillation_appearing_mid_range():
    r = periodic_buckets(0, 12 * 3_600_000, 15_000, [(2 * M, 3, 6 * 3_600_000)], noise=1, seed=4)
    ts, y = arrays(r)
    sg = spectrogram(ts, y, 15_000, segment_ms=30 * M, overlap=0.5)
    k = int(np.argmin(abs(1000 / sg.freqs - 120)))                # row nearest a 2m period
    before = sg.power[sg.centres_ms + 15 * M <= 6 * 3_600_000, k]
    after = sg.power[sg.centres_ms - 15 * M >= 6 * 3_600_000, k]
    assert before.max() < 0.1 and after.min() > 0.5
    assert (sg.segment_ms, sg.hop_ms) == (30 * M, 15 * M)
```

- [ ] **Step 2:** Run the tests and confirm they fail with ImportError.
- [ ] **Step 3: Implement**

```python
# src/telemetry_nerd/analysis/spectrum.py
"""Periodicity of bucket series (beads 4ok.7, 4ok.8). Pure numpy, no I/O.

Generalised Lomb-Scargle (Zechmeister & Kürster 2009): evaluated only at observed times, so
gaps are never interpolated. Power = share of variance a sinusoid explains, in [0, 1].
False-alarm probability: Baluev (2008), as astropy's fap_baluev, standard normalisation."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

OVERSAMPLE, CHUNK = 5, 256
MIN_POINTS, MAX_GAP_FRACTION = 32, 0.5
FAP_LEVEL, LOCAL_RATIO, WINDOW_ARTIFACT = 0.01, 10.0, 0.1


def lomb_scargle(t: np.ndarray, y: np.ndarray, freqs: np.ndarray) -> np.ndarray:
    Y = y.mean()
    YY = ((y - Y) ** 2).mean()
    out = np.empty(freqs.size)
    for a in range(0, freqs.size, CHUNK):  # chunk: an F x N matrix would not fit in memory
        arg = 2 * np.pi * freqs[a : a + CHUNK, None] * t[None, :]
        c, s = np.cos(arg), np.sin(arg)
        C, S = c.mean(1), s.mean(1)
        YC, YS = c @ y / t.size - Y * C, s @ y / t.size - Y * S
        CC, SS = (c * c).mean(1) - C * C, (s * s).mean(1) - S * S
        CS = (c * s).mean(1) - C * S
        out[a : a + CHUNK] = (SS * YC**2 + CC * YS**2 - 2 * CS * YC * YS) / (YY * (CC * SS - CS**2))
    return np.clip(out, 0.0, 1.0)


def fap(z, n: int, fmax: float, t: np.ndarray):
    """P(highest peak >= z | white noise) over frequencies up to fmax (t in the same unit as 1/fmax)."""
    z = np.clip(np.asarray(z, float), 0.0, 1 - 1e-12)
    nh, nk = n - 1, n - 3
    gamma = math.sqrt(2 / nh) * math.exp(math.lgamma(nh / 2) - math.lgamma((nh - 1) / 2))
    tau = gamma * fmax * math.sqrt(4 * math.pi * float(np.var(t))) \
        * (1 - z) ** ((nk - 1) / 2) * np.sqrt(nh * z / 2)
    return np.clip(-np.expm1(nk / 2 * np.log1p(-z) - tau), 0.0, 1.0)


def level(p: float, n: int, fmax: float, t: np.ndarray) -> float:
    """Power a peak must reach for FAP = p (bisection; fap is decreasing in z)."""
    lo, hi = 0.0, 1 - 1e-12
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if float(fap(mid, n, fmax, t)) > p else (lo, mid)
    return hi


def detrend(t: np.ndarray, y: np.ndarray) -> np.ndarray:
    return y - np.polyval(np.polyfit(t, y, 1), t)


@dataclass(frozen=True)
class Peak:
    period_ms: float
    lo_ms: float  # half-power width, never narrower than the 1/range resolution
    hi_ms: float
    power: float
    fap: float
    local_ratio: float | None
    window: float  # spectral-window power at f: high = periodic sampling gaps, not the signal
    significant: bool
    at_limit: bool  # the half-power walk hit the edge of the resolvable range


@dataclass(frozen=True)
class Spectrum:
    freqs: np.ndarray  # Hz
    power: np.ndarray
    peaks: list[Peak]
    n: int
    shortest_ms: int
    longest_ms: int
    level: float  # power at FAP 1%
    caveats: list[str] = field(default_factory=list)


def spectrum(ts_ms, y, step_ms: int, *, top: int = 5, min_period_ms=None, max_period_ms=None) -> Spectrum:
    t = (ts_ms - ts_ms[0]) / 1000.0
    span = (ts_ms[-1] - ts_ms[0] + step_ms) / 1000.0
    v = detrend(t, np.asarray(y, float))
    fmin, fmax = 2 / span, 500 / step_ms  # longest: two cycles; shortest: Nyquist (2 x step)
    if max_period_ms:
        fmin = max(fmin, 1000 / max_period_ms)
    if min_period_ms:
        fmax = min(fmax, 1000 / min_period_ms)
    if fmin >= fmax:
        raise ValueError("no resolvable periods: the range is too short for this step")
    df = 1 / (OVERSAMPLE * span)
    f = np.arange(fmin, fmax + df / 2, df)
    p = lomb_scargle(t, v, f)
    caveats = []
    thirds = np.array_split(p, 3)
    if np.median(thirds[0]) >= 5 * max(np.median(thirds[-1]), 1e-12):
        caveats.append("red_noise")
    peaks = _peaks(f, p, 1 / span, top, t)
    if any(pk.window > WINDOW_ARTIFACT for pk in peaks):
        caveats.append("sampling_artifact")
    return Spectrum(f, p, peaks, t.size, round(1000 / f[-1]), round(1000 / f[0]),
                    level(FAP_LEVEL, t.size, f[-1], t), caveats)


def _peaks(f, p, res, top, t) -> list[Peak]:
    is_max = np.r_[p[0] > p[1], (p[1:-1] >= p[:-2]) & (p[1:-1] > p[2:]), p[-1] > p[-2]]
    chosen: list[int] = []
    for i in sorted(np.flatnonzero(is_max), key=lambda i: -p[i]):
        if all(abs(f[i] - f[j]) > res for j in chosen):  # one peak per resolution element
            chosen.append(int(i))
        if len(chosen) == top:
            break
    return [_peak(f, p, i, res, t) for i in chosen]


def _peak(f, p, i, res, t) -> Peak:
    a = b = i
    while a > 0 and p[a - 1] >= p[i] / 2:
        a -= 1
    while b < p.size - 1 and p[b + 1] >= p[i] / 2:
        b += 1
    f_lo, f_hi = f[a], f[b]
    if f_hi - f_lo < res:
        f_lo, f_hi = f[i] - res / 2, f[i] + res / 2
    band = (f > f[i] / math.sqrt(2)) & (f < f[i] * math.sqrt(2)) & (np.abs(f - f[i]) > res)
    ratio = float(p[i] / max(np.median(p[band]), 1e-12)) if band.sum() >= 8 else None
    fa = float(fap(p[i], t.size, f[-1], t))
    win = float(abs(np.exp(2j * np.pi * f[i] * t).mean()) ** 2)
    return Peak(1000 / f[i], 1000 / f_hi, 1000 / max(f_lo, f[0] / 2), float(p[i]), fa, ratio, win,
                fa < FAP_LEVEL and (ratio is None or ratio >= LOCAL_RATIO), a == 0 or b == p.size - 1)


@dataclass(frozen=True)
class Spectrogram:
    centres_ms: np.ndarray  # column = window (centre - segment/2, centre + segment/2]
    hop_ms: int
    segment_ms: int
    freqs: np.ndarray  # spacing 1/segment: the honest resolution, no oversampling
    power: np.ndarray  # columns x freqs; NaN = window with < 50% coverage
    level: np.ndarray  # per column, power at FAP 1%


def spectrogram(ts_ms, y, step_ms: int, *, segment_ms: int, overlap: float) -> Spectrogram:
    hop = max(step_ms, round(segment_ms * (1 - overlap) / step_ms) * step_ms)
    df = 1000 / segment_ms
    f = np.arange(2 * df, 500 / step_ms + df / 2, df)
    need = max(MIN_POINTS // 2, (1 - MAX_GAP_FRACTION) * segment_ms / step_ms)
    centres, rows, levels = [], [], []
    for end in range(int(ts_ms[0]) + segment_ms - step_ms, int(ts_ms[-1]) + 1, hop):
        m = (ts_ms > end - segment_ms) & (ts_ms <= end)
        centres.append(end - segment_ms // 2)
        tw = (ts_ms[m] - end) / 1000.0
        if m.sum() < need or np.ptp(y[m]) == 0:
            rows.append(np.full(f.size, np.nan))
            levels.append(np.nan)
            continue
        rows.append(lomb_scargle(tw, detrend(tw, y[m]), f))
        levels.append(level(FAP_LEVEL, int(m.sum()), f[-1], tw))
    return Spectrogram(np.array(centres), hop, segment_ms, f, np.vstack(rows), np.array(levels))


def log_bins(periods_ms: np.ndarray, values: np.ndarray, n_bins: int):
    """Max-preserving reduction onto a log-period grid (one value per ~2 px): peaks survive."""
    edges = np.geomspace(periods_ms.min(), periods_ms.max() * (1 + 1e-9), n_bins + 1)
    idx = np.digitize(periods_ms, edges) - 1
    out = np.full((n_bins, *values.shape[1:]), np.nan)
    for k in np.unique(idx):
        out[k] = np.nanmax(values[idx == k], axis=0)
    return edges, out
```

- [ ] **Step 4:** Run `uv run pytest tests/unit/test_spectrum.py -q` and make it pass.
  - If the white-noise test is flaky, fix the seeds.
  - If timing exceeds about 3 s, lower the run count to 60.
  - Commit: `feat(analysis): Lomb-Scargle spectrum with FAP, peak intervals, spectrogram (4ok.7, 4ok.8)`.

---

### Task 3: Zero-phase Gaussian filters (pure)

**Files:** create `analysis/filters.py` and `tests/unit/test_filters.py`.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_filters.py
import numpy as np, pytest
from telemetry_nerd.analysis.filters import FilterSpec, apply, filter_buckets, segments
from telemetry_nerd.devtools.synthetic import periodic_buckets

M = 60_000
t = np.arange(0, 2000) * M

def amp(spec, period_ms):
    f = apply(spec, t, np.sin(2 * np.pi * t / period_ms), M)
    return np.abs(f.values[~f.edge]).max()

def test_lowpass_half_power_at_cutoff_and_passes_long_periods():
    lp = FilterSpec("lowpass", 30 * M)
    assert amp(lp, 120 * M) > 0.95 and amp(lp, int(7.5 * M)) < 0.01
    assert abs(amp(lp, 30 * M) - 2**-0.5) < 0.03

def test_highpass_removes_long_periods():
    hp = FilterSpec("highpass", 30 * M)
    assert amp(hp, 120 * M) < 0.1 and amp(hp, int(7.5 * M)) > 0.95
    assert abs(amp(hp, 30 * M) - 2**-0.5) < 0.03

def test_bandpass_isolates_band():
    bp = FilterSpec("bandpass", 10 * M, 120 * M)
    assert amp(bp, 30 * M) > 0.8 and amp(bp, 3 * M) < 0.05 and amp(bp, 600 * M) < 0.1

def test_gap_splits_and_nothing_crosses_it():
    ts = np.r_[np.arange(0, 100), np.arange(150, 250)] * M
    y = np.r_[np.zeros(100), np.full(100, 10.0)]
    assert segments(ts, M) == [(0, 100), (100, 200)]
    f = apply(FilterSpec("lowpass", 20 * M), ts, y, M)
    assert np.allclose(f.values[:100], 0) and np.allclose(f.values[100:], 10)  # no bleeding, no ramp
    assert f.edge[0] and f.edge[99] and f.edge[100] and not f.edge[50]

def test_checks_nyquist_range_and_band_shape():
    with pytest.raises(ValueError, match="Nyquist"):
        FilterSpec("lowpass", M).check(M, 100 * M)
    with pytest.raises(ValueError, match="half the range"):
        FilterSpec("lowpass", 60 * M).check(M, 100 * M)
    with pytest.raises(ValueError, match="4 x"):
        FilterSpec("bandpass", 10 * M, 20 * M).check(M, 1000 * M)
    assert FilterSpec("lowpass", 3 * M).check(M, 100 * M) == ["weak_filter"]

def test_filter_buckets_keeps_series_ids_and_reports_edges():
    r = periodic_buckets(0, 600 * M, M, [(5 * M, 1, None), (200 * M, 3, None)], gaps=[(300 * M, 310 * M)])
    out = filter_buckets(FilterSpec("lowpass", 20 * M), r.buckets, M)
    assert out.buckets.column("series_id").unique().to_pylist() == r.buckets.column("series_id").unique().to_pylist()
    sid = r.buckets.column("series_id")[0].as_py()
    assert len(out.edges[sid]) == 4 and 0 < out.removed_share[sid] < 0.2
```

- [ ] **Step 2: Implement**

```python
# src/telemetry_nerd/analysis/filters.py
"""Zero-phase Gaussian filters for bucket series (bead 4ok.9). Pure numpy/polars, no I/O.

Cutoff = the period at half power. Gaps split a series into segments: no kernel crosses a gap
and nothing is interpolated. Points within 3 sigma of a segment edge see a truncated
(renormalised) kernel and are flagged `edge` (filter warm-up/tail: unreliable)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
import polars as pl
import pyarrow as pa

from telemetry_nerd.model.series import BUCKET_SCHEMA
from telemetry_nerd.model.time import format_duration as fmt

Kind = Literal["lowpass", "highpass", "bandpass"]
LOW_SIGMA = math.sqrt(math.log(2)) / (2 * math.pi)            # H(1/P) = 1/sqrt2
HIGH_SIGMA = math.sqrt(-math.log(1 - 2**-0.5) / 2) / math.pi  # 1 - H(1/P) = 1/sqrt2
RADIUS, WEAK_STEPS = 3.0, 8
_NAMES = {"lowpass": "low-pass", "highpass": "high-pass", "bandpass": "band-pass"}


@dataclass(frozen=True)
class FilterSpec:
    kind: Kind
    period_ms: int
    period_hi_ms: int | None = None

    def label(self) -> str:
        cut = fmt(self.period_ms) + (f"–{fmt(self.period_hi_ms)}" if self.period_hi_ms else "")
        return f"{cut} {_NAMES[self.kind]} (Gaussian, zero-phase)"

    def check(self, step_ms: int, span_ms: int) -> list[str]:
        if self.period_ms < 2 * step_ms:
            raise ValueError(f"cutoff {fmt(self.period_ms)} is below 2 x step ({fmt(2 * step_ms)}): "
                             "shorter periods do not exist at this step (Nyquist)")
        if self.kind == "bandpass":
            if self.period_hi_ms is None or self.period_hi_ms < 4 * self.period_ms:
                raise ValueError("band-pass needs period_hi >= 4 x period so both edges stay at half power")
        elif self.period_hi_ms is not None:
            raise ValueError("period_hi is only for band-pass")
        longest = self.period_hi_ms or self.period_ms
        if longest > span_ms // 2:
            raise ValueError(f"cutoff {fmt(longest)} is longer than half the range ({fmt(span_ms // 2)}); "
                             "query a longer range")
        return ["weak_filter"] if self.kind != "highpass" and self.period_ms < WEAK_STEPS * step_ms else []


def _conv_same(x: np.ndarray, k: np.ndarray) -> np.ndarray:
    n = x.size + k.size - 1
    m = 1 << (n - 1).bit_length()
    full = np.fft.irfft(np.fft.rfft(x, m) * np.fft.rfft(k, m), m)[:n]
    r = (k.size - 1) // 2
    return full[r : r + x.size]


def _gauss(y: np.ndarray, sigma_steps: float) -> tuple[np.ndarray, int]:
    r = max(1, math.ceil(RADIUS * sigma_steps))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / max(sigma_steps, 1e-9)) ** 2)
    return _conv_same(y, k) / _conv_same(np.ones_like(y), k), r


def segments(ts: np.ndarray, step_ms: int) -> list[tuple[int, int]]:
    cuts = (np.flatnonzero(np.diff(ts) != step_ms) + 1).tolist()
    b = [0, *cuts, ts.size] if ts.size else []
    return list(zip(b[:-1], b[1:]))


@dataclass(frozen=True)
class Filtered:
    values: np.ndarray
    edge: np.ndarray


def apply(spec: FilterSpec, ts: np.ndarray, y: np.ndarray, step_ms: int) -> Filtered:
    out, edge = np.empty(y.size), np.zeros(y.size, bool)
    lo_s, hi_s = LOW_SIGMA * spec.period_ms / step_ms, HIGH_SIGMA * spec.period_ms / step_ms
    for a, b in segments(ts, step_ms):
        seg = y[a:b]
        if spec.kind == "lowpass":
            v, r = _gauss(seg, lo_s)
        elif spec.kind == "highpass":
            base, r = _gauss(seg, hi_s)
            v = seg - base
        else:
            short, _ = _gauss(seg, lo_s)
            long_, r = _gauss(seg, HIGH_SIGMA * spec.period_hi_ms / step_ms)  # type: ignore[operator]
            v = short - long_
        out[a:b] = v
        edge[a : min(b, a + r)] = True
        edge[max(a, b - r) : b] = True
    return Filtered(out, edge)


@dataclass(frozen=True)
class FilterOutput:
    buckets: pa.Table
    edges: dict[str, list[list[int]]]  # series_id -> [[t0, t1], ...] unreliable spans
    removed_share: dict[str, float]    # var(raw - filtered) / var(raw), interior points
    edge_share: float


def _spans(ts: np.ndarray, mask: np.ndarray) -> list[list[int]]:
    out: list[list[int]] = []
    for i in np.flatnonzero(mask):
        if out and ts[i - 1] == out[-1][1] and mask[i - 1]:
            out[-1][1] = int(ts[i])
        else:
            out.append([int(ts[i]), int(ts[i])])
    return out


def filter_buckets(spec: FilterSpec, buckets: pa.Table, step_ms: int) -> FilterOutput:
    df = pl.from_arrow(buckets).with_columns(pl.col("avg").fill_nan(None)).drop_nulls("avg")
    parts, edges, removed, n_edge = [], {}, {}, 0
    for (sid,), g in df.sort("ts_ms").group_by("series_id", maintain_order=True):
        ts, y = g["ts_ms"].to_numpy(), g["avg"].to_numpy()
        f = apply(spec, ts, y, step_ms)
        inner = ~f.edge
        var = float(np.var(y[inner])) if inner.sum() > 1 else 0.0
        removed[sid] = float(np.var((y - f.values)[inner]) / var) if var > 0 else 0.0
        edges[sid], n_edge = _spans(ts, f.edge), n_edge + int(f.edge.sum())
        parts.append(g.with_columns(*(pl.Series(c, f.values) for c in ("avg", "min", "max"))))
    out = pl.concat(parts).select(BUCKET_SCHEMA.names) if parts else pl.from_arrow(BUCKET_SCHEMA.empty_table())
    return FilterOutput(out.to_arrow().cast(BUCKET_SCHEMA), edges, removed, n_edge / max(df.height, 1))


def removed_table(raw: pa.Table, filtered: pa.Table) -> pa.Table:
    """raw - filtered on the same grid: the part a high/band-pass took out (drawn dashed over raw)."""
    r = pl.from_arrow(raw).select("ts_ms", "series_id", "avg", "count")
    f = pl.from_arrow(filtered).select("ts_ms", "series_id", pl.col("avg").alias("f"))
    d = r.join(f, on=["series_id", "ts_ms"]).with_columns((pl.col("avg") - pl.col("f")).alias("avg"))
    return d.with_columns(pl.col("avg").alias("min"), pl.col("avg").alias("max")) \
        .select(BUCKET_SCHEMA.names).sort(["series_id", "ts_ms"]).to_arrow().cast(BUCKET_SCHEMA)
```

Filtered rows set `min = max = avg`. A filtered value has no within-bucket spread; the raw envelope drawn underneath carries it. This also keeps `summarize`/`value_stats` free of false `non_finite` caveats.

- [ ] **Step 3:** Run the tests. Commit: `feat(analysis): zero-phase Gaussian low/high/band-pass with gap segments and edge marking (4ok.9)`.

---

### Task 4: Preconditions, derived datasets, SignalOps, MCP `spectrum` and `filter`

**Files:**
- Create: `analysis/timeops.py`, `core/signal_ops.py`, `tests/unit/test_signal_service.py`.
- Modify: `charts/units.py`, `datasets/store.py`, `core/service.py`, `mcp/server.py`, `tests/unit/test_mcp.py`.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_signal_service.py
import pytest
from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.devtools.synthetic import periodic_buckets
from tests.unit.fakes import make_service

M, DAY = 60_000, 86_400_000

def put(svc, expr, r, step, rep="bucket_agg", rng=TimeRange(0, 4 * DAY)):
    return svc.datasets.put(source="default", expr=expr, rng=rng, step_ms=step, resolution_ms=15_000,
                            result=r, representation=rep).id

def periodic(**kw):
    return periodic_buckets(0, 4 * DAY, M, [(5 * M, 3, None), (DAY, 5, None)], noise=1, seed=1, **kw)

def test_spectrum_summary_coarsens_and_cites_evidence(tmp_path):
    svc = make_service(tmp_path)
    out = svc.spectrum(put(svc, "queue_depth", periodic(), M))
    assert out["effective_step"] == "2m" and "coarsened" in out["caveats"]   # 5761 > 4096 points
    assert out["limits"] == {"shortest": "4m", "longest": "2d"}
    peaks = out["series"][0]["peaks"]
    ev = next(p["evidence"] for p in peaks if p["significant"] and abs(p["period_s"] - 300) < 10)
    assert ev["name"] == "dominant_period" and ev["interval"][0] <= 300 <= ev["interval"][1]
    assert len(str(out)) < 2048

@pytest.mark.parametrize("expr,rep,match", [
    ("histogram_quantile(0.99, sum(rate(x_bucket[5m])) by (le))", "quantile", "percentile"),
    ("http_requests_total", "bucket_agg", "raw counter"),
])
def test_refusals_carry_hints(tmp_path, expr, rep, match):
    svc = make_service(tmp_path)
    d = put(svc, expr, periodic(), M, rep)
    for call in (lambda: svc.spectrum(d), lambda: svc.filter(d, "lowpass", "1h", "trend")):
        with pytest.raises(ValueError, match=match) as e:
            call()
        assert "hint" in str(e.value)

def test_rate_of_counter_is_fine(tmp_path):
    svc = make_service(tmp_path)
    assert svc.spectrum(put(svc, "rate(http_requests_total[5m])", periodic(), M))["series"]

def test_filter_creates_derived_dataset_with_provenance(tmp_path):
    svc = make_service(tmp_path)
    d = put(svc, "queue_depth", periodic(), M)
    out = svc.filter(d, "lowpass", "1h", "is the daily cycle growing?")
    meta = svc.datasets.meta(out["dataset"])
    assert meta.derived["from"] == d and meta.derived["label"] == "1h low-pass (Gaussian, zero-phase)"
    assert meta.expr == svc.datasets.meta(d).expr and meta.step_ms == M
    assert out["summary"]["views"] == ["overlay", "filtered", "raw"]
    with pytest.raises(ValueError, match="already filtered"):
        svc.filter(out["dataset"], "highpass", "1h", "x")
    with pytest.raises(ValueError, match="Nyquist"):
        svc.filter(d, "lowpass", "1m", "x")
```

- [ ] **Step 2: Implement.** Only the key code is shown; the rest follows existing patterns.

```python
# charts/units.py (append)
_COUNTER_SAFE = {"rate", "irate", "increase", "delta", "idelta", "deriv", "resets", "changes",
                 "rollup_rate", "rollup_increase", "rollup_deriv", "histogram_quantile",
                 "histogram_count", "histogram_sum", "count_over_time", "absent"}

def raw_counters(expr: str, lookup: Lookup = facts_from_name) -> list[str]:
    """Counters used outside any rate-like call: a running total, not a signal."""
    calls: list[str | None] = []
    out: set[str] = set()
    prev: str | None = None
    for token in _TOKEN.findall(_STRING.sub(" ", expr)):
        if token == "(":
            calls.append(prev); prev = None
        elif token == ")":
            if calls: calls.pop()
            prev = None
        else:
            prev = token
            if token not in _KEYWORDS and lookup(token).type == "counter" \
                    and not _COUNTER_SAFE.intersection(c for c in calls if c):
                out.add(token)
    return sorted(out)
```

```python
# analysis/timeops.py
"""Semantic preconditions for time ops (spec §5.1). Refusals carry a hint for Claude."""
from analysis.exprkind import QUANTILE_HINT  # (absolute import in code)

def time_op_problem(op: str, representation: str, counters: list[str]) -> str | None:
    if representation == "quantile":
        return (f"{op} refused on a percentile series: it would aggregate percentiles over time "
                "(hint: apply it to the request rate, to histogram_sum/histogram_count rates, or to "
                "threshold counts from query_distribution + fraction_over)")
    if representation == "distribution":
        return (f"{op} needs a time series, this is a distribution "
                "(hint: query a rate or a threshold count over time)")
    if counters:
        return (f"{op} refused on raw counter {', '.join(counters)}: a running total has no periods "
                "(hint: use rate(x[$__rate_interval]))")
    return None
```

`datasets/store.py`:
- Add `derived: dict | None = None` to `DatasetMeta`.
- Factor `_insert_rows(meta, buckets, series)` out of `put`.
- Add:

```python
def put_derived(self, parent: DatasetMeta, buckets: pa.Table, series: pa.Table, derived: dict) -> DatasetMeta:
    meta = replace(parent, id=self._new_id("d"), created_at_ms=self._clock(), derived=derived)
    ...  # begin; _insert_meta; _insert_rows; commit/rollback, as in put
```

`core/signal_ops.py`:
- `SignalOps(datasets, facts)` with:
  - `_load(dataset_id, op, cap)`: meta → `time_op_problem` (counters via `raw_counters(meta.expr, lambda m: facts(meta.source, m))`) → `datasets.get` → `lod(buckets, step, rng, cap)`. The result is the effective step plus a `coarsened` caveat.
  - Per-series arrays with gates. Each gate failure skips the series with a reason:
    - `n < MIN_POINTS` gives `too_few_points`;
    - more than 50% gaps gives `too_gappy`;
    - a constant series gives `constant`.
  - A series with more than 20% gaps adds a `gaps` caveat.
  - More than `FACET_BUDGET` series is refused (hint: aggregate first).
- `spectrum(dataset_id, top, min_p, max_p)` is memoized in an `OrderedDict` (32 entries, key = dataset id + params; datasets are immutable).
- `summary(...)`: per series `labels, n, peaks[{period, period_s, interval_s, power, fap, local_ratio, significant, sampling_artifact}]`. Significant peaks also carry `evidence`:

```python
{"kind": "statistic", "dataset": d, "name": "dominant_period", "value": period_s,
 "interval": [lo_s, hi_s], "exact": False,
 "method": "Lomb-Scargle peak, half-power width (>= 1/range)",
 "params": {"fap": fap, "local_ratio": ratio, "n": n, "step": eff_step, "detrended": "linear"}}
```

  - It also returns `limits: {shortest, longest}`, `effective_step` and `caveats`.
  - Default `top=3` keeps the summary within the 2 KB budget.
- `filter(dataset_id, spec, reason)`:
  - The reason must be one line of at most 160 characters.
  - `meta.derived` set → "already filtered (hint: filter its source {from}; band-pass for two cutoffs)".
  - Then `time_op_problem`, `spec.check(step, span)`, `filter_buckets`, and `put_derived(meta, out.buckets, result.series, {"op": kind, "from": id, "label": spec.label(), "reason": reason, "period_ms", "period_hi_ms", "edges": out.edges})`.
  - The summary holds `{filter, removed_share (per series, 3 sig), edge_share, views: offered_views(kind), default_view, caveats: warnings + ["filter_edges"] if edge_share>0 + ["mostly_edge"] if edge_share>0.5}`.

`TelemetryService`:
- Gains `signal: SignalOps`, built in `__post_init__` from `datasets` and `ws.catalog_facts`.
- `spectrum()` delegates.
- `filter()` delegates and appends `dataset.created` with `{"expr", "derived": {op, from, label}}`.

MCP:
- Add `spectrum` and `filter` (via `@mcp.tool(name="filter")` on `def filter_tool`). Docstrings must state:
  - the cutoff is a half-power **period**;
  - Nyquist and range/2 are limits;
  - the views;
  - the refusals and their hints;
  - "low-pass is for trends and sustained shifts, never for spike detection".
- `ValueError` maps to `ToolError(str(e))`.
- Add two INSTRUCTIONS bullets:

```
- Periodicity (cron, GC, retries, scrape artefacts, diurnal): `spectrum(dataset)`. Report only
  `significant` peaks with their interval, cite `evidence`; say which periods cannot be seen
  (shorter than `limits.shortest` = 2 x step, longer than range/2). red_noise: long periods look
  more significant than they are. Draw: show(mark="spectrum") or show(mark="spectrogram",
  segment="30m", overlap=0.5) for periods that appear/disappear.
- Filters: `filter(dataset, kind, period, reason)` only when the question needs it: lowpass for
  trend/sustained shift, highpass to remove baseline/diurnal before looking for spikes/steps,
  bandpass around a spectrum peak. Never filter percentiles or raw counters. `show` the result;
  state the filter and why in your answer; raw stays one click away for the user.
```

- [ ] **Step 3:** Run the tests. In `test_mcp.py`, add that `spectrum` and `filter` are listed and that a quantile dataset gives a ToolError containing "hint". Commit: `feat: spectrum and filter ops with preconditions, derived datasets, MCP tools (4ok.7, 4ok.9)`.

---

### Task 5: Spec, show, panel_data kinds, data-view selection

**Files:**
- Create: `charts/dataview.py`.
- Modify: `charts/spec.py`, `core/service.py`, `core/workspace_service.py`, `core/events.py`, `channel/format.py`, `api/app.py`, `mcp/server.py`.
- Tests: `test_signal_service.py`, `test_chart_spec.py`, `test_api_workspace.py`, `test_service.py` (the spec equality test gains `signal: None`).

- [ ] **Step 1: Failing tests (append)**

```python
def test_show_filtered_builds_views_and_payload(tmp_path):
    svc = make_service(tmp_path)
    d = put(svc, "queue_depth", periodic(), M)
    f = svc.filter(d, "highpass", "2h", "remove the daily cycle to see bursts")["dataset"]
    p = svc.show(f, "Are there bursts beyond the daily cycle?").panel
    assert p.dataset_ids == [f, d] and [l["role"] for l in p.spec["layers"]] == ["main", "context"]
    assert p.spec["signal"]["offered"] == ["filtered", "removed", "raw"] and p.spec["signal"]["default"] == "filtered"
    data = svc.panel_data(p.id, 800)
    assert data["kind"] == "time" and data["raw"] and data["removed"] and data["filter"]["edges"]
    assert "filtered" in data["caveats"]

def test_select_data_view_is_ambient_and_persists(tmp_path):
    svc = make_service(tmp_path)
    d = put(svc, "queue_depth", periodic(), M)
    p = svc.show(svc.filter(d, "lowpass", "1h", "trend")["dataset"], "Trend?").panel
    svc.ws.select_data_view(p.id, "raw", "user")
    assert svc.workspace.get_panel(p.id).spec["signal"]["selected"] == "raw"
    ev = svc.log.since(0)[-1]
    assert ev.type == "panel.data_view_selected" and ev.klass == "ambient"
    with pytest.raises(ValueError, match="offered"):
        svc.ws.select_data_view(p.id, "removed", "user")

def test_spectrum_and_spectrogram_panels(tmp_path):
    svc = make_service(tmp_path)
    d = put(svc, "queue_depth", periodic(), M)
    sp = svc.panel_data(svc.show(d, "Which periods?", mark="spectrum").panel.id, 800)
    assert sp["kind"] == "spectrum" and sp["limits"]["shortest_s"] == 240 and len(sp["periods_s"]) <= 400
    sg_panel = svc.show(d, "When do periods change?", mark="spectrogram", segment="6h", overlap=0.5).panel
    assert sg_panel.spec["layers"][0]["segment_ms"] == 6 * 3_600_000      # explicit in provenance
    sg = svc.panel_data(sg_panel.id, 800)
    assert sg["kind"] == "spectrogram" and sg["hop_ms"] == 3 * 3_600_000 and sg["series"][0]["level"]
    q = put(svc, "histogram_quantile(0.9, sum(rate(x_bucket[5m])) by (le))", periodic(), M, "quantile")
    with pytest.raises(ValueError, match="percentile"):
        svc.show(q, "periods?", mark="spectrum")
```

- [ ] **Step 2: Implement**

```python
# charts/dataview.py
"""Filtered/raw data views of a panel drawn from filter() (bead 4ok.9). Same mechanics as y-views."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

DataView = Literal["overlay", "filtered", "removed", "raw"]
VIEW_LABELS = {"overlay": "filtered over raw", "filtered": "filtered", "removed": "raw + removed part", "raw": "raw"}

def offered_views(kind: str) -> list[DataView]:
    return ["overlay", "filtered", "raw"] if kind == "lowpass" else ["filtered", "removed", "raw"]

class SignalViews(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filter: str                                    # "15m low-pass (Gaussian, zero-phase)"
    kind: Literal["lowpass", "highpass", "bandpass"]
    reason: str = Field(min_length=1, max_length=160)
    offered: list[DataView]
    default: DataView
    selected: DataView | None = None

    @model_validator(mode="after")
    def _ok(self):
        if "\n" in self.reason:
            raise ValueError("reason must be one line")
        for v in (self.default, self.selected):
            if v is not None and v not in self.offered:
                raise ValueError(f"view {v!r} is not offered ({', '.join(self.offered)})")
        return self
```

`charts/spec.py`:
- `Mark` gains `"spectrum"` and `"spectrogram"`; `SPECTRAL_MARKS = {...}`.
- `Layer` gains:
  - `role: Literal["main","context"] = "main"`;
  - `segment_ms: int | None`;
  - `overlap: float | None = Field(None, ge=0, lt=0.95)`;
  - `min_period_ms/max_period_ms: int | None`.
- `ChartSpec.signal: SignalViews | None = None`.
- `validate()` changes:
  - line-budget datasets = main layers with mark in {`line+envelope`, `spectrum`};
  - `spectrogram` joins the `FACET_BUDGET` rule;
  - `mark_representation` already lets spectral marks through on non-distribution data. Quantile refusal happens in `show` via `time_op_problem`, so the message carries the hint.

`TelemetryService.show(..., view=None, segment=None, overlap=None)`:
- **Spectral marks:**
  - Run `self.signal.check(dataset_id, mark)` (refusals).
  - Segment: `segment_ms = parse_duration(segment)` or `auto_segment(span, step)`, which is the nice duration nearest span/16, clamped to [16·step, span/4]. Refuse outside that range with a hint.
  - Overlap defaults to 0.5. Both values are written into the layer, so they are visible in provenance.
- **`meta.derived` with `mark="auto"`:**
  - layers = `[Layer(line+envelope, data=d), Layer(line+envelope, data=derived.from, role="context")]`;
  - `spec.signal = SignalViews(filter=label, kind=op, reason=derived.reason, offered=..., default=view or offered[0])`;
  - `create_panel(..., [d, derived.from])`.

`panel_data`:
- Dispatches on `layers[0].mark` **before** the representation branch.
- **`spectrum`:**
  - From the memoized spectrum, build a shared log-period grid (`log_bins`, `n = width_px // 2`, max-preserving).
  - Payload: `{kind, periods_s (bin centres), series[{id, labels, power[], level, peaks[], caveats}], limits{shortest_s, longest_s}, effective_step_ms, caveats}`.
- **`spectrogram`:**
  - `SignalOps.spectrogram`: coarsen to 8192 points; refuse more than 400 columns; reduce rows with `log_bins` to `facet_height // PX_PER_ROW`.
  - Payload: `{kind, segment_ms, hop_ms, overlap, effective_step_ms, series[{id, labels, ts (centres), rows{lo_s, hi_s}, power[col][row] (None = missing), level[col]}], limits, caveats}`.
- **Time with `spec.signal`:**
  - Add `raw` (the parent, LOD'd the same way) and, for high/band-pass, `removed` (`removed_table` at native step, then `lod`).
  - Add `filter: {**spec.signal, edges, period_ms, period_hi_ms}`.
  - Add the caveat `"filtered"`.
  - Factor out `_series_payload(table, labels)` and reuse it.

`WorkspaceService`:
- `select_data_view(panel_id, view, actor)` is `@atomic`:
  - validate through `SignalViews` (an unknown view gives "not offered");
  - a panel without a signal is refused with "data views apply to panels drawn from filter()";
  - write `set_spec` and append `panel.data_view_selected` with `{view, default, filter}`.
- `_check` (y-views) now ranges over the **drawn** tables of the current view: overlay → filtered+raw, filtered → filtered, raw → raw, removed → raw+removed (via `pa.concat_tables`).
- `brief()` adds `data_view` when one is selected.
- `AMBIENT_TYPES` gains `panel.data_view_selected`.
- `describe_event` text: `user switched p7 to "raw" (Claude's default: filtered, 2h high-pass)`.

Route: `POST /api/panels/{id}/data-view {view: str}`, with a type check like `panel_y_view`.

MCP `show`: add parameters `view`, `segment`, `overlap`, and document them.

- [ ] **Step 3:** Run `just test && just lint`. Commit: `feat: spectrum/spectrogram panels, filtered panels with data views and ambient selection (4ok.7–9)`.

---

### Task 6: Client pure modules (period axis, spectrum, spectrogram, context and edges)

**Files:** create `ui/src/chart/{period,spectrum,spectrogram,dataview}.ts` with tests. Modify `toUplot.ts` and its test, and `lib/api.ts` (types `SpectrumPanelData`, `SpectrogramPanelData`, `TimePanelData.raw?/removed?/filter?`, `ChartSpec.signal`, `selectDataView`).

- [ ] **Step 1: Failing tests**

```ts
// ui/src/chart/period.test.ts
import { expect, test } from "vitest";
import { fmtPeriod, periodTicks } from "./period";
test("human periods", () => {
  expect([30, 90, 300, 3600, 86400, 604800].map(fmtPeriod)).toEqual(["30s", "1.5m", "5m", "1h", "1d", "1w"]);
  expect(periodTicks(240, 172800)).toEqual([300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800]);
});

// ui/src/chart/spectrogram.test.ts
import { layoutSpectrogram } from "./spectrogram";
test("columns centred, missing windows hatched, non-significant cells faint", () => {
  const s = { id: "a", labels: {}, ts: [1800e3, 3600e3, 5400e3], rows: { lo_s: [60, 120], hi_s: [120, 240] },
              power: [[0.9, 0.1], null, [0.05, 0.02]], level: [0.2, null, 0.2] };
  const l = layoutSpectrogram(s, { width: 300, height: 100, startMs: 0, endMs: 7200e3, hopMs: 1800e3 });
  expect(l.missing).toHaveLength(1);
  expect(l.rects.filter((r) => r.faint)).toHaveLength(3);
  expect(l.rects[0].t).toBeCloseTo(0.9);              // linear 0..1: share of variance, comparable across columns
});

// ui/src/chart/toUplot.test.ts (append)
test("raw context is drawn first and faint; edge spans become a dashed twin", () => {
  const s = { id: "a", labels: { job: "x" }, ts: [0, 60e3, 120e3], avg: [1, 2, 3], min: [1, 2, 3], max: [1, 2, 3], count: [1, 1, 1] };
  const m = toUplot([s], undefined, { context: { role: "raw", series: [{ ...s, avg: [0, 5, 0] }] }, edges: [[0, 0]] });
  expect(m.series[1].label).toContain("raw");
  expect(String(m.series[1].stroke)).toMatch(/rgba\(.*0\.3/);
  expect(m.series.some((x) => x.dash && String(x.label).includes("edge"))).toBe(true);
});
```

- [ ] **Step 2: Implement**

```ts
// ui/src/chart/period.ts
const UNITS: [string, number][] = [["w", 604800], ["d", 86400], ["h", 3600], ["m", 60], ["s", 1]];
export function fmtPeriod(sec: number): string {
  for (const [u, s] of UNITS) if (sec >= s) return `${Number((sec / s).toPrecision(sec / s < 10 ? 2 : 3))}${u}`;
  return `${Number((sec * 1000).toPrecision(2))}ms`;
}
const NICE = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200,
  86400, 172800, 604800, 1209600, 2419200];
export const periodTicks = (minS: number, maxS: number): number[] => NICE.filter((v) => v >= minS && v <= maxS);
```

```ts
// ui/src/chart/spectrogram.ts
import { cellSpan, valueAxis, type ValueAxis, type Span } from "./heatmap";
export interface SpectroSeries { id: string; labels: Record<string, string>; ts: number[];
  rows: { lo_s: number[]; hi_s: number[] }; power: ((number | null)[] | null)[]; level: (number | null)[] }
export interface SpectroRect { x: number; y: number; w: number; h: number; t: number; faint: boolean; col: number; row: number }
export function layoutSpectrogram(s: SpectroSeries, o: { width: number; height: number; startMs: number; endMs: number; hopMs: number }) {
  const axis: ValueAxis = valueAxis(s.rows.lo_s, s.rows.hi_s, o.height, "log"); // period, long periods up
  const toX = (ms: number) => ((ms - o.startMs) * o.width) / (o.endMs - o.startMs);
  const rects: SpectroRect[] = [], missing: Span[] = [];
  s.ts.forEach((c, col) => {
    const x = toX(c - o.hopMs / 2), w = toX(c + o.hopMs / 2) - x;
    const p = s.power[col];
    if (!p) { missing.push({ x, w }); return; }
    p.forEach((v, row) => {
      if (v === null) return;
      const [p0, p1] = cellSpan(axis, s.rows.lo_s[row], s.rows.hi_s[row]);
      rects.push({ x, w, y: o.height - p1, h: p1 - p0, t: v, faint: v < (s.level[col] ?? 1), col, row });
    });
  });
  return { axis, rects, missing };
}
```

`spectrum.ts`:
- `toSpectrumUplot(d)` returns x = `periods_s`, one power series per series (Okabe-Ito `PALETTE`), and a dashed grey "FAP 1%" constant series at max(level).
- Scales: `x: {time:false, distr:3, log:10, range: [shortest/1.6, longest*1.6]}`, `y: {range: [0,1]}`.
- `limitZones(limits, xmin, xmax)` returns `[{from, to, text: "< 2×step: invisible at this step"}, {..., "> range/2: needs a longer range"}]`.
- `peakMarks(series)` returns significant peaks as `{x, lo, hi, y, text: "5m [4.9m–5.1m]"}`.

`toUplot` changes:
- With `opts.context.role === "raw"`, push the raw avg line (alpha 0.35, width 1, legend `${name} raw`) and its envelope band (alpha 0.08) **before** the main series. uPlot draws in order, so raw ends up underneath.
- With `role === "removed"`, push a dashed line in the same colour **after** the main series (`${name} removed part`).
- `opts.edges`: avg points whose ts lies in a span move to a dashed twin `${name} (filter edge, unreliable)`, legend hidden. This mirrors the low-n twin.

`dataview.ts`:
- `drawnFor(view, d)` returns `{series, context?}`:
  - overlay → `series=filtered`, `context=raw`;
  - filtered → filtered;
  - raw → `series=raw` (normal style);
  - removed → `series=raw`, `context={role:"removed", series: d.removed}`.
- `yStats` takes the concatenation of the drawn series.

- [ ] **Step 3:** Run `just ui-test && just ui-check`. Commit: `feat(ui): period axis, spectrum/spectrogram layout, raw context and filter-edge twins (4ok.7–9)`.

---

### Task 7: Components and Panel wiring

**Files:** create `ui/src/components/SpectrumPlot.svelte` and `SpectrogramPlot.svelte`. Modify `ui/src/Panel.svelte`, `lib/panelNotes.ts` (+test) and `index.css`.

- [ ] **Step 1: SpectrumPlot.** A uPlot instance from `toSpectrumUplot`, with:
  - x axis `values: (_, ticks) => ticks.map(fmtPeriod)` and `splits: () => periodTicks(...)`;
  - y label "share of variance explained";
  - a `draw` hook that hatches `limitZones` (reusing the heatmap hatch routine) with their text, and draws `peakMarks` as a horizontal whisker [lo, hi] at the peak plus a label.
  - Legend line: "Lomb-Scargle, linear trend removed · step {eff} · n {n} · dashed: 1% false-alarm level (white noise){red_noise ? '; long periods overstated (autocorrelation)' : ''}".
  - `data-spectrum-peaks` = number of significant peaks.
- [ ] **Step 2: SpectrogramPlot.** A canvas following HeatmapPlot's structure (`setupCanvas`, `colormap("viridis")`, hatch for `missing`):
  - fill rects at `globalAlpha = r.faint ? 0.35 : 1`;
  - period ticks via `periodTicks` and `axis.pos`;
  - time ticks as in the heatmap.
  - The legend states that the window, hop and resolution define what the plot can show: "window {segment} · hop {hop} ({overlap}% overlap): each column summarizes one window · period resolution 1/{segment}, rows show the max within the row · periods {shortest}–{segment/2} · colour: share of window variance, linear 0–1 · faint: below 1% false-alarm level · hatched: window < 50% covered".
  - Tooltip: `{window range} · period {lo}–{hi} · power {p} {≥ level ? '' : '(not significant)'}`.
- [ ] **Step 3: Panel.svelte.**
  - Branch on `data.kind === "spectrum" | "spectrogram"`. Spectrograms facet like heatmaps, reporting through `onFacetRendered`.
  - For time panels with `data.filter`, use the data-view logic below.
  - The optimistic `pendingView` is reset when `panel.spec.signal?.selected` changes, the same pattern as the y-view `pending`.

```svelte
  const viewNow = $derived(pendingView ?? panel.spec.signal?.selected ?? panel.spec.signal?.default ?? null);
  const drawn = $derived(data?.kind === "time" && data.filter && viewNow ? drawnFor(viewNow, data) : null);
  ...
  {#if data?.kind === "time" && data.filter}
    <div class="legend y-views" role="group" aria-label="Data view">
      data:
      {#each data.filter.offered as v (v)}
        <button type="button" class:on={viewNow === v} data-view={v}
          title={v === data.filter.default ? `Claude's pick: ${data.filter.reason}` : ""}
          onclick={() => pickView(v)}>{VIEW_LABELS[v]}{v === data.filter.default ? " · Claude" : ""}</button>
      {/each}
      <span class="hint">{data.filter.filter}; dashed: filter edge (unreliable)</span>
    </div>
  {/if}
```

  - The plot effect tracks `viewNow`, calls `toUplot(drawn.series, grid, {...opts, context: drawn.context, edges: viewNow === "raw" ? [] : edgesOf(data.filter)})`, and feeds `yStats` from the drawn series.
- [ ] **Step 4: panelNotes.**
  - Info note `filter`: `Filtered: {label} — Claude: {reason}. Raw is one click away ("raw").`
  - Caveat texts:
    - `filtered`: "values are filtered, not raw; cite the filter";
    - `filter_edges`: "dashed spans saw a truncated kernel (series start/end, around gaps)";
    - `mostly_edge`;
    - `weak_filter`: "cutoff under 8 steps barely smooths";
    - `coarsened`: "averaged to {step} first; shorter periods not examined";
    - `red_noise`;
    - `sampling_artifact`: "a peak matches the sampling pattern (periodic gaps), not the signal";
    - `too_few_points`, `too_gappy`, `constant`.
  - `describeShown`: for derived datasets, "{label} of {expr}"; for spectrum, "periodogram of {expr}"; for spectrogram, "spectrogram of {expr} (window {segment})".
- [ ] **Step 5:** Run `just ui-test && just ui-check && just ui-build`. Commit: `feat(ui): spectrum and spectrogram panels, filtered/raw data-view row (4ok.7–9)`.

---

### Task 8: e2e, live Claude check, close beads

- [ ] **Step 1: Demo data.** `devtools/synthetic.demo_text` gains `tn_demo_queue_depth{instance}` with a 5-minute cron sawtooth, plus a 2-minute retry oscillation that starts at 2/3 of the range.
- [ ] **Step 2: `ui/e2e/signal.spec.ts`.** Seed through the HTTP API: query, then `POST` spectrum and filter. These go through the MCP-equivalent routes, or through `scripts/mcp_call.py` in `beforeAll`. Then assert:
  - the spectrum panel shows `[data-spectrum-peaks]` ≥ 1, and the x tick text contains "5m";
  - the spectrogram canvas renders, and `data-render-ms` is within budget;
  - on the filtered panel, `data:` buttons switch views; after a reload "raw" is still `on`; `/api/events` shows an ambient `panel.data_view_selected`.
- [ ] **Step 3: Live Claude.** With the daemon restarted:
  - ask "is there a periodic pattern in queue depth?". Expect `spectrum`, with significant peaks cited with intervals and the limits stated, then `show(mark="spectrum")`.
  - ask "is the trend rising?". Expect `filter(lowpass, period ≥ the cron period, reason)`, then `show(view="overlay")`.
  - ask for p99 periodicity. Expect a refusal with a hint, after which Claude switches to the rate or the histogram.
- [ ] **Step 4:** Run `just test && just lint && just ui-test && just ui-check`. Commit `test(e2e): spectrum, spectrogram and data views (4ok.7–9)`. Then close the beads:
  - `bd close telemetry-nerd-4ok.7 --reason "..."`
  - `bd close telemetry-nerd-4ok.8 --reason "..."`
  - `bd close telemetry-nerd-4ok.9 --reason "..."`

## Acceptance

- **4ok.7:**
  - A synthetic series with 5m + 24h components recovers both as significant, each interval containing the truth. This holds on gappy input too.
  - The x axis is in human periods. Periods outside [2×step, range/2] are hatched and labelled.
  - Percentile, distribution and raw-counter inputs are refused with hints.
  - The metric card thumbnail is split out (no metric card exists yet; see Beads).
- **4ok.8:** a 2m oscillation starting mid-range appears in the spectrogram. The window, hop and overlap are in the layer spec and in the legend; missing windows are hatched; non-significant cells are faint.
- **4ok.9:**
  - Filters:
    - the cutoff is a period at half power;
    - refused below Nyquist and above range/2;
    - zero-phase, no ringing, no kernel across gaps, edges dashed;
    - recorded as derived-dataset provenance and cited through the label;
    - refused on percentiles and raw counters with a hint.
  - The panel offers Claude's default view, and raw is one click away. Selection is persisted and logged as an ambient event, and y-views range over what is drawn.

## Beads

- **4ok.7** covers Tasks 1, 2 (spectrum part), 4, and 5–7 (spectrum parts). **4ok.8** covers Task 2 (spectrogram) and Tasks 5–7 (spectrogram parts). **4ok.9** covers Tasks 3–7 (filter parts). Task 8 closes all three.
- **New, P3:** "Metric card spectrum thumbnail": the top 3 significant periods with intervals, using `SignalOps.spectrum` over the operating-profile window. Blocked by the metric card bead (§6.4); it also feeds 2as.7 (operating profile seasonality).
- **New, P3:** "Cutoff from spectrum": `filter(period="from-spectrum:<peak>")` derives a band-pass around a cited peak and links the evidence.
- **New, P3:** "Spectrum at scrape resolution". When the dataset step is greater than the resolution and Claude asks about short periods, offer a follow-up that re-queries at `step = resolution` over a shorter range. The re-query is automatic, with a `coarsened`-style note.
- **M4 op registry (epic):** fold `time_op_problem`, the gates and the memo into the typed registry with node records, so spectra become `op` nodes.

## Risks and decisions for the user

1. **New dependency, numpy (~20 MB wheel).** No scipy or astropy. The alternative is pure Python with a hard cap of about 1k points, which is too coarse for a 4-day / 5-minute question. Recommended: accept numpy.
2. **Spectrum on percentile series is refused outright.** "Does p99 have a GC period?" is a fair question. The alternative is to allow it over meaningful buckets only (n ≥ n_min) with a `percentile_series` caveat, on the grounds that a periodogram is not a percentile estimate. The recommended path is the hint (spectrum `histogram_sum/histogram_count` rates or threshold-fraction counts), which keeps one simple invariant. Filters stay refused either way.
3. **Significance rule.** FAP < 1% plus local ratio ≥ 10 is a heuristic guard against red noise, not a fitted red-noise null such as AR(1). An AR(1) null with a block bootstrap is a better follow-up, at about 50× the compute.
4. **Gaussian rather than Butterworth.** The roll-off is softer, so periods near the cutoff leak more. It never rings or invents dips. The label says "Gaussian" and the cutoff is stated at half power.
5. **Strict gap splitting.** A single missing bucket creates two edge zones. That is honest but noisy on flaky scrapes. An option: treat gaps of ≤ 1 bucket as joinable, with a `bridged_gaps` caveat. Not recommended for the MVP.
6. **Spectra are not persisted.** Evidence cites `dataset + method + params`, which reproduce the result exactly because datasets are immutable. If findings must survive dataset deletion, persist the spectrum peaks as a node output later.
7. **uPlot log-x on a non-time scale** (`distr: 3`, custom `splits`) and the hatch overlay are untested here. Verify them first in Task 7; the fallback is a small canvas renderer like PercentilePlot.
8. **Default spectrogram segment of span/16** is a heuristic. Claude should pass `segment` explicitly, and the panel always shows it.

### Critical Files for Implementation
- /Users/avishai/code/telemetry-nerd/src/telemetry_nerd/core/service.py (show, panel_data dispatch; new core/signal_ops.py beside it)
- /Users/avishai/code/telemetry-nerd/src/telemetry_nerd/charts/spec.py (marks, Layer params, ChartSpec.signal; new charts/dataview.py)
- /Users/avishai/code/telemetry-nerd/src/telemetry_nerd/datasets/store.py (DatasetMeta.derived, put_derived)
- /Users/avishai/code/telemetry-nerd/src/telemetry_nerd/core/workspace_service.py (select_data_view, _check on drawn tables)
- /Users/avishai/code/telemetry-nerd/ui/src/Panel.svelte and /Users/avishai/code/telemetry-nerd/ui/src/chart/toUplot.ts (data-view row, context and edge series)
