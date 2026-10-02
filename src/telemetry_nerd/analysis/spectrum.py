"""Periodicity of bucket series (beads 4ok.7, 4ok.8). Pure numpy, no I/O.

Generalised Lomb-Scargle (Zechmeister & Kürster 2009): evaluated only at observed times, so
gaps are never interpolated. Power = share of variance a sinusoid explains, in [0, 1].
False-alarm probability: Baluev (2008), as astropy's fap_baluev, standard normalisation.
`significant` additionally requires the peak to stand out from an AR(1) red-noise background
(stability.red_noise_test, bead lkn.4): the white-noise FAP calls AR(1) wandering and the
1/f^2 power of a level step significant at long periods."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

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
    tau = (
        gamma
        * fmax
        * math.sqrt(4 * math.pi * float(np.var(t)))
        * (1 - z) ** ((nk - 1) / 2)
        * np.sqrt(nh * z / 2)
    )
    # Baluev (2008) eq. 6: FAP = 1 - (1 - FAP_single) * exp(-tau)
    single = np.exp(nk / 2 * np.log1p(-z))
    return np.clip(-np.expm1(np.log1p(-single) - tau), 0.0, 1.0)


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
    significant: bool  # white_significant and fap_red_noise < FAP_LEVEL
    at_limit: bool  # the half-power walk hit the edge of the resolvable range
    white_significant: bool = False  # fap < FAP_LEVEL and local_ratio >= LOCAL_RATIO (or None)
    fap_red_noise: float = 1.0  # against AR(1) red noise, after stronger confirmed peaks removed


@dataclass(frozen=True)
class Spectrum:
    freqs: np.ndarray  # Hz
    power: np.ndarray
    peaks: list[Peak]
    n: int
    shortest_ms: int
    longest_ms: int
    level: float  # power at FAP 1% (white noise)
    caveats: list[str] = field(default_factory=list)
    phi: float = 0.0  # AR(1) coefficient of the red-noise background

    def red_level(self, f_hz: np.ndarray, step_s: float) -> np.ndarray:
        """Power at red-noise FAP 1% per frequency: 2 S(f) z* / N with (1 - e^-z*)^M = 0.99."""
        from telemetry_nerd.analysis.stability import ar1_spectrum

        m = max(1.0, self.n / 2)
        z = -math.log(-math.expm1(math.log1p(-FAP_LEVEL) / m))
        s = np.array([ar1_spectrum(float(x), self.phi, step_s) for x in np.atleast_1d(f_hz)])
        return np.clip(2 * s * z / self.n, 0.0, 1.0)


def spectrum(
    ts_ms, y, step_ms: int, *, top: int = 5, min_period_ms=None, max_period_ms=None
) -> Spectrum:
    t = (ts_ms - ts_ms[0]) / 1000.0
    span = (ts_ms[-1] - ts_ms[0] + step_ms) / 1000.0
    v = detrend(t, np.asarray(y, float))
    fmin = 2000 / max(ts_ms[-1] - ts_ms[0], step_ms)  # longest period: two cycles in the range
    fmax = 500 / step_ms  # shortest period: Nyquist (2 x step)
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
    peaks, phi = _red_noise(ts_ms, t, np.asarray(y, float), step_ms, peaks)
    return Spectrum(
        f,
        p,
        peaks,
        t.size,
        round(1000 / fmax),
        round(1000 / fmin),
        level(FAP_LEVEL, t.size, f[-1], t),
        caveats,
        phi,
    )


def _red_noise(ts_ms, t, y, step_ms, peaks: list[Peak]) -> tuple[list[Peak], float]:
    """Test every peak (strongest first) against AR(1) red noise, as analyze does: on what the
    best structure model (constant / trend / level shifts, by BIC) leaves, so a step's 1/f^2
    power is not read as a period; phi from that residual after removing the white-significant,
    non-artefact peaks."""
    from telemetry_nerd.analysis.autocorr import ar1, positions
    from telemetry_nerd.analysis.diagnostics import structure
    from telemetry_nerd.analysis.stability import red_noise_test

    pos = positions(ts_ms, step_ms)
    v = structure(pos, ts_ms, t, y, int(ts_ms[-1] - ts_ms[0]) + step_ms).resid
    order = sorted(peaks, key=lambda pk: -pk.power)
    bg = [pk.period_ms / 1000 for pk in order if pk.white_significant and pk.window <= WINDOW_ARTIFACT]  # fmt: skip
    if not order:
        return peaks, max(0.0, ar1(pos, v - v.mean()).phi)
    tests = red_noise_test(
        pos, t, v, [pk.period_ms / 1000 for pk in order], step_ms / 1000, max(1.0, t.size / 2), bg
    )
    by_period = {pk.period_ms: r.fap for pk, r in zip(order, tests, strict=True)}
    out = [
        replace(pk, fap_red_noise=by_period[pk.period_ms],
                significant=pk.white_significant and by_period[pk.period_ms] < FAP_LEVEL)
        for pk in peaks
    ]  # fmt: skip
    return out, tests[0].phi


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
    return Peak(
        1000 / f[i],
        1000 / f_hi,
        1000 / max(f_lo, f[0] / 2),
        float(p[i]),
        fa,
        ratio,
        win,
        False,
        a == 0 or b == p.size - 1,
        white_significant=fa < FAP_LEVEL and (ratio is None or ratio >= LOCAL_RATIO),
    )


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
