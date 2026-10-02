"""Seasonal comparison (bead lkn.2): alignment, reference choice, band calibration, verdicts."""

from datetime import UTC, datetime

import numpy as np
import pytest

from telemetry_nerd.analysis.seasonal import (
    Cycle,
    choose,
    compare,
    cycle_shifts,
    t_quantile,
)

H, M, DAY = 3_600_000, 60_000, 86_400_000
WEEK = 7 * DAY
MON = int(datetime(2026, 9, 28, tzinfo=UTC).timestamp() * 1000)  # a Monday 00:00 UTC


def ms(*a, tz=UTC):
    return int(datetime(*a, tzinfo=tz).timestamp() * 1000)


# --- alignment ---------------------------------------------------------------------
def test_utc_shifts_are_nominal():
    assert cycle_shifts(MON + 9 * H, "1d", 3, "UTC", 6 * H, 5 * M) == [DAY, 2 * DAY, 3 * DAY]
    assert cycle_shifts(MON + 9 * H, "1w", 2, "UTC", 6 * H, 5 * M) == [WEEK, 2 * WEEK]
    assert cycle_shifts(MON, "previous", 2, "UTC", 6 * H, 5 * M) == [6 * H, 12 * H]


def test_local_day_across_spring_dst_is_23h():
    # Europe/Berlin springs forward on Sun 2026-03-29 02:00 -> 03:00
    start = ms(2026, 3, 30, 7)  # Mon 09:00 CEST = 07:00 UTC
    s = cycle_shifts(start, "1d", 2, "Europe/Berlin", 2 * H, 5 * M)
    assert s == [DAY, 2 * DAY - H]  # Sun 09:00 CEST, then Sat 09:00 CET = 08:00 UTC
    w = cycle_shifts(start, "1w", 1, "Europe/Berlin", 2 * H, 5 * M)
    assert w == [WEEK - H]
    assert cycle_shifts(start, "1w", 1, "UTC", 2 * H, 5 * M) == [WEEK]


def test_local_day_across_autumn_dst_is_25h():
    start = ms(2026, 10, 26, 8)  # Mon 09:00 CET (Berlin falls back Sun 2026-10-25)
    assert cycle_shifts(start, "1d", 2, "Europe/Berlin", 2 * H, 5 * M) == [DAY, 2 * DAY + H]


def test_alignment_refusals():
    with pytest.raises(ValueError, match="longer than"):
        cycle_shifts(MON, "1d", 3, "UTC", 2 * DAY, 5 * M)
    with pytest.raises(ValueError, match="step"):
        cycle_shifts(MON, "1d", 3, "UTC", H, 7 * M)
    with pytest.raises(ValueError, match="timezone"):
        cycle_shifts(MON, "1d", 3, "Mars/Olympus", H, 5 * M)


def test_t_quantile_small_df_exact():
    assert t_quantile(0.995, 2) == pytest.approx(9.925, abs=2e-3)
    assert t_quantile(0.975, 1) == pytest.approx(12.706, abs=2e-3)
    assert t_quantile(0.995, 3) == pytest.approx(5.841, abs=0.06)


# --- synthetic daily + weekly load ------------------------------------------------------
def load(t_ms, rng, jitter):
    """Weekday business-hours peak, low weekends; multiplicative noise; per-cycle jitter."""
    t = np.asarray(t_ms)
    hour = (t % DAY) / H
    dow = ((t // DAY) + 3) % 7  # 0 = Monday
    daily = 1 + 2.0 * np.exp(-(((hour - 14) / 3.5) ** 2))
    weekend = np.where(dow >= 5, 0.35, 1.0)
    level = 100 * (1 + (daily - 1) * weekend)
    return level * jitter * np.exp(rng.normal(0, 0.08, t.size))


def cycles_for(start, n_points, step, scheme, k, seed, tz="UTC", spike=None):
    span = n_points * step
    rng = np.random.default_rng(seed)
    grid = start + np.arange(n_points) * step
    out = []
    for j, sh in enumerate(cycle_shifts(start, scheme, k, tz, span, step), 1):
        vals = load(grid - sh, rng, np.exp(rng.normal(0, 0.04)))
        out.append(Cycle(scheme, j, sh, start - sh, vals))
    now = load(grid, rng, np.exp(rng.normal(0, 0.04)))
    if spike is not None:
        a, b, factor = spike
        now[a:b] *= factor
    return now, out


def test_weekly_reference_chosen_and_weekday_peak_on_saturday_is_unusual():
    sat = MON + 5 * DAY + 8 * H  # Saturday 08:00-20:00: the weekend low
    n, step = 144, 5 * M
    rng = np.random.default_rng(7)
    grid = sat + np.arange(n) * step
    now = load(grid - 5 * DAY, rng, 1.0)  # Monday's shape on a Saturday
    schemes = {}
    for scheme, k in (("previous", 4), ("1d", 7), ("1w", 4)):
        _, cyc = cycles_for(sat, n, step, scheme, k, 11)
        schemes[scheme] = cyc
    pick, scores = choose(now, schemes, step)
    assert pick == "1w", scores
    c = compare(now, schemes["1w"], step)
    assert c.verdict == "unusual" and c.direction == "higher", c.reasons


def test_normal_monday_peak_is_usual():
    start = MON + 8 * H
    now, cyc = cycles_for(start, 144, 5 * M, "1w", 4, 3)
    c = compare(now, cyc, 5 * M)
    assert c.verdict == "usual", c.reasons
    assert c.scale == "log" and len(c.kept) == 4


def test_short_spike_at_unusual_time_is_flagged_as_extreme():
    start = MON + 8 * H
    now, cyc = cycles_for(start, 144, 5 * M, "1w", 4, 5, spike=(60, 63, 2.5))
    c = compare(now, cyc, 5 * M)
    assert c.verdict == "unusual" and c.extremes.flagged
    assert {p for p, _ in c.extremes.points} <= set(range(58, 65))


def test_insufficient_history_says_so():
    now, cyc = cycles_for(MON + 8 * H, 144, 5 * M, "1w", 2, 1)
    c = compare(now, cyc, 5 * M)
    assert c.verdict == "insufficient_history"
    assert "2 usable" in c.reasons[0]


def test_missing_and_user_excluded_cycles_are_stated():
    now, cyc = cycles_for(MON + 8 * H, 144, 5 * M, "1w", 5, 2)
    cyc[1].values[:100] = np.nan  # mostly missing
    c = compare(now, cyc, 5 * M, exclude={3})
    reasons = {cy.j: why for cy, why in c.excluded}
    assert reasons == {2: "missing", 3: "user"}
    assert all(cy.j not in (2, 3) for cy in c.kept)


def test_weekday_window_prefers_weekly_reference_over_daily_with_weekends():
    wed = MON + 2 * DAY + 8 * H  # previous 7 days: Tue, Mon, Sun, Sat, Fri, Thu, Wed
    schemes = {}
    for scheme, k in (("previous", 4), ("1d", 7), ("1w", 4)):
        now, schemes[scheme] = cycles_for(wed, 144, 5 * M, scheme, k, 9)
    pick, scores = choose(now, schemes, 5 * M)
    assert pick == "1w" and scores["1w"] < 0.95 * scores["1d"] < scores["previous"], scores


def test_holiday_cycle_is_excluded_as_atypical():
    now, cyc = cycles_for(MON + 8 * H, 144, 5 * M, "1w", 5, 4)
    cyc[2].values = cyc[2].values * 0.4  # a holiday week in the reference
    c = compare(now, cyc, 5 * M)
    assert [(cy.j, why) for cy, why in c.excluded] == [(3, "atypical")]
    assert c.verdict == "usual", c.reasons


def test_linear_scale_when_values_touch_zero():
    now, cyc = cycles_for(MON + 8 * H, 144, 5 * M, "1w", 4, 6)
    now = now - 100
    for cy in cyc:
        cy.values = cy.values - 100
    assert compare(now, cyc, 5 * M).scale == "linear"


# --- calibration (seeded simulation) ----------------------------------------------------
def test_band_covers_nominal_share_of_normal_cycles_and_false_alarms_are_rare():
    inside, total, alarms, seeds = 0, 0, 0, 40
    for seed in range(seeds):
        start = MON + (seed % 7) * DAY + (seed % 24) * H
        now, cyc = cycles_for(start, 96, 5 * M, "1w", 4, 100 + seed)
        c = compare(now, cyc, 5 * M)
        ok = ~np.isnan(c.lo)
        inside += int(np.sum((now[ok] >= c.lo[ok]) & (now[ok] <= c.hi[ok])))
        total += int(ok.sum())
        alarms += c.verdict == "unusual"
    coverage = inside / total
    print(f"held-out coverage {coverage:.3f} (nominal 0.90), false alarms {alarms}/{seeds}")
    assert 0.86 <= coverage <= 0.95
    assert alarms <= 3  # design: 3 detectors x 1%
