"""bucket_state: per series and bucket, how much of the expected data arrived and whether it can
be trusted (spec 2026-10-02 §5). Computed on read from bucket counts; never stored."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
    SOURCE_FILLED = 8  # the expression's counts are subquery evaluations, not samples (UNKNOWN)
    POST_GAP = 16  # value right after a gap is computed from the sample before it (OK/PARTIAL)


PARTIAL_RATIO = 0.9
COUNT_SLACK = 0.1  # error of an estimated expected count, on top of one sample of jitter
CHANGE_RATIO = 2.0  # a series' sample rate halves or doubles within the window
CHANGE_MIN_BUCKETS = 3  # non-zero buckets each side of a rate change needs before it can be judged
CHANGE_WINDOW = 5  # non-zero buckets in the centred rate that localises a rate change
LOCAL_GAPS = 16  # non-zero-bucket gaps in each one-sided neighbourhood the cadence is judged from
LOCAL_MIN_GAPS = 2  # gaps a neighbourhood needs to stand on its own (else the other side, global)
HOLE_RATIO = 2.0  # a gap over this many times its neighbourhood's median gap is a hole, not cadence
SLOW_MIN_LONG = 2  # gaps longer than the step a neighbourhood needs before it can read as slow
SLOW_MARGIN = 1.25  # interval this far above the step: slower than the step, not a step-rate one
BASELINE_PASSES = 2  # re-estimates of the faster-than-step baseline without its short buckets
SLOW_MISS_RATIO = 1.5  # a slower-than-step series misses its cadence after this many intervals
SPILL_RATE = (0.8, 1.2)  # samples per bucket at which a scrape can spill into the next bucket

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
    """Below expected by more than jitter: one sample, or (1 - PARTIAL_RATIO) of a long bucket.
    `expected` is an estimate from the series' own spacing: COUNT_SLACK more for its error."""
    tolerance = pl.max_horizontal(pl.lit(1.0), (1 - PARTIAL_RATIO) * expected) + COUNT_SLACK
    return observed < expected - tolerance


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
    source_filled: bool = False,
    post_gap_buckets: int = 0,
) -> pa.Table:
    """`source_filled`: the counts are not observed samples (expression cannot tell), so every
    bucket is UNKNOWN + SOURCE_FILLED. `post_gap_buckets`: the source's increase/rate after a gap
    reaches back over the gap, so the first n OK/PARTIAL buckets after an EMPTY/UNKNOWN one
    (stopping at the next gap) are flagged POST_GAP (state unchanged); 0 turns it off."""
    ts = grid(start_ms, end_ms, step_ms)
    if not ts or not series_ids:
        return STATE_SCHEMA.empty_table()
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
        pl.col("observed").fill_null(0.0)
    )
    unknown = pl.lit(source_filled)
    for a, b, _reason in failed:
        unknown = unknown | pl.col("ts_ms").is_between(a, b)
    df = df.with_columns(unknown.alias("_unk"))
    first = (
        df.filter(pl.col("observed") > 0)
        .group_by("series_id")
        .agg(pl.col("ts_ms").min().alias("first_seen"))
    )
    df = df.join(first, on="series_id", how="left").sort("series_id", "ts_ms")
    if mode == "samples":
        df = _cadence(df, step_ms)
    else:
        df = df.with_columns(
            pl.lit(1.0).alias("expected"),
            pl.lit(False).alias("_slow"),
            pl.lit(True).alias("_missed"),
        )
    state = (
        pl.when(pl.col("_unk"))
        .then(int(State.UNKNOWN))
        .when(pl.col("first_seen").is_not_null() & (pl.col("ts_ms") < pl.col("first_seen")))
        .then(int(State.ABSENT))
        .when(pl.col("observed") == 0)
        .then(pl.when(pl.col("_missed")).then(int(State.EMPTY)).otherwise(int(State.OK)))
        .when(pl.col("_slow"))
        .then(int(State.OK))  # a sample on a slower-than-step series holds its cadence
        .when(short(pl.col("observed"), pl.col("expected")))
        .then(int(State.PARTIAL))
        .otherwise(int(State.OK))
    )
    df = df.with_columns(
        state.cast(pl.UInt8).alias("state"),
        pl.when(pl.col("_unk")).then(0.0).otherwise(pl.col("observed")).alias("observed"),
    )
    flags = pl.lit(int(Flag.SOURCE_FILLED) if source_filled else 0)
    if mode == "samples" and not source_filled:
        # a series with a slower-than-step stretch shows its rate in the gaps between samples (at
        # any step); the others in their counts, when the step holds several samples
        df = df.with_columns(pl.col("_slow").any().over(_S).alias("_by_gap"))
        df, changed = _gap_interval_change(df)
        flags = flags | changed
        if step_ms >= resolution_ms:
            df, changed = _interval_change(df)
            flags = flags | changed
    df = df.sort("series_id", "ts_ms")
    if post_gap_buckets > 0:
        is_gap = pl.col("state").is_in([int(State.EMPTY), int(State.UNKNOWN)])
        df = df.with_columns(is_gap.cum_sum().over("series_id").alias("_gap_no"))
        # position after the gap bucket that opened this run (the gap bucket itself is 0)
        pos = pl.int_range(pl.len()).over(["series_id", "_gap_no"])
        after = (
            ~is_gap
            & (pl.col("_gap_no") > 0)
            & (pl.col("state") != int(State.ABSENT))
            & (pos <= post_gap_buckets)
        )
        flags = flags | pl.when(after).then(int(Flag.POST_GAP)).otherwise(0)
    out = df.with_columns(flags.cast(pl.UInt16).alias("flags")).select(STATE_SCHEMA.names)
    return out.to_arrow().cast(STATE_SCHEMA)


_S = "series_id"


def _cadence(df: pl.DataFrame, step_ms: int) -> pl.DataFrame:
    """Adds `expected` (samples per bucket), `_slow` (bucket in a slower-than-step stretch) and
    `_missed` (a 0 bucket here is a miss). df is sorted by series, ts.

    Judged locally (spec §5.1), from the gaps between the series' non-zero buckets: the zero
    buckets between two non-zero buckets p and n, and n itself, take the interval I of the
    neighbourhood before p or the one after n, whichever is nearer the gap's own time per sample
    (so either side of a rate change reads at its own rate, and a hole at the slower one), each
    being Σgap/Σsamples over its LOCAL_GAPS gaps, holes (gaps over HOLE_RATIO x the median) left
    out. A neighbourhood is slower than the step when I > SLOW_MARGIN x step with SLOW_MIN_LONG
    gaps over the step; a gap is slower when either neighbourhood is. Slower: a sample bucket is OK, a 0 bucket is a miss only once the time since
    the last sample (or UNKNOWN bucket) exceeds max(1.5 I, I + step), and `expected` = step / I.
    Otherwise every 0 bucket is a miss, except at about one sample per bucket a lone 0 paired with
    a 2 (a scrape that spilled into the next bucket), and `expected` is the series' samples per
    bucket over its at-or-faster-than-step gaps (holes, missed buckets' time and buckets short of
    the estimate left out; at least 1), so a series whose count drops for a stretch reads partial
    there, as before, and coverage shows the loss.
    `interval_differs` reads the interval back as step / expected."""
    nz_b = (pl.col("observed") > 0) & ~pl.col("_unk")
    nz = df.filter(nz_b).select(
        _S,
        "ts_ms",
        pl.col("observed").alias("_c"),
        pl.col("ts_ms").diff().over(_S).cast(pl.Float64).alias("_g"),
    )
    est = _local_interval(nz, "_g", step_ms)
    df = _spread(df, est, step_ms)
    alive = pl.col("first_seen").is_not_null() & (pl.col("ts_ms") >= pl.col("first_seen"))
    # an UNKNOWN bucket resets the cadence reference: what happened inside it is not known
    ref = pl.when(nz_b | pl.col("_unk")).then(pl.col("ts_ms")).forward_fill().over(_S)
    miss_after = pl.max_horizontal(SLOW_MISS_RATIO * pl.col("_I"), pl.col("_I") + step_ms)
    zero = (pl.col("observed") == 0) & ~pl.col("_unk") & alive
    rate = step_ms / pl.col("_I")
    near_one = ~pl.col("_slow") & rate.is_between(*SPILL_RATE)
    lone = nz_b.shift(1).over(_S).fill_null(False) & nz_b.shift(-1).over(_S).fill_null(True)
    df = df.with_columns(
        (zero & lone & near_one).alias("_ev0"),
        (nz_b & (pl.col("observed") >= 2) & near_one).alias("_ev2"),
        ((zero & ~lone) | pl.col("_unk")).alias("_evx"),
    )
    run0 = (pl.col("_evx") & ~pl.col("_unk")).fill_null(False)
    two = lambda k: run0.shift(k).over(_S).fill_null(False)
    # the end of a run of exactly two 0 buckets: one lost scrape next to one that spilled
    df = df.with_columns((run0 & two(1) & ~two(2) & ~two(-1) & near_one).alias("_ev00"))
    spilled = _spilled(df.filter(pl.col("_ev0") | pl.col("_ev2") | pl.col("_evx")))
    df = df.join(spilled, on=[_S, "ts_ms"], how="left").sort(_S, "ts_ms")
    df = df.with_columns(
        pl.when(pl.col("_slow"))
        .then((pl.col("ts_ms") - ref > miss_after).fill_null(True))
        .otherwise(~pl.col("_spill").fill_null(False))
        .alias("_missed")
    )
    # at or faster than the step a missed bucket is lost time, not cadence: take it out of I
    gap_of = pl.when(nz_b).then(pl.col("ts_ms")).backward_fill().over(_S)
    lost = (
        df.filter(zero & pl.col("_missed") & ~pl.col("_slow"))
        .group_by(_S, gap_of.alias("ts_ms"))
        .agg(pl.len().alias("_lost"))
        .drop_nulls("ts_ms")
    )
    g2 = pl.col("_g") - step_ms * pl.col("_lost").fill_null(0)
    med = g2.rolling_median(window_size=2 * LOCAL_GAPS + 1, center=True, min_samples=1)
    use = g2.is_not_null() & (g2 <= HOLE_RATIO * med.over(_S)) & ~pl.col("_sl")
    gaps = (
        nz.join(lost, on=[_S, "ts_ms"], how="left")
        .join(est.select(_S, "ts_ms", "_sl"), on=[_S, "ts_ms"], how="left")
        .sort(_S, "ts_ms")
        .with_columns(use.alias("_use"), g2.alias("_g2"))
    )

    def per_bucket(gaps: pl.DataFrame) -> pl.DataFrame:
        return (
            gaps.group_by(_S)
            .agg(
                pl.col("_c").filter("_use").sum().alias("_n"),
                pl.col("_g2").filter("_use").sum().alias("_t"),
                pl.col("_c").median().alias("_typ"),
            )
            .select(
                _S,
                pl.when(pl.col("_t") > 0)
                .then(step_ms * pl.col("_n") / pl.col("_t"))
                .otherwise(pl.col("_typ"))
                .clip(lower_bound=1.0)
                .alias("_fast"),
            )
        )

    # robust to sustained loss: buckets short of the estimate are left out and it is re-estimated
    # from the rest (a stretch keeping 1/4 of its samples would otherwise pull the baseline down
    # and read as most of what was expected)
    fast = per_bucket(gaps)
    for _ in range(BASELINE_PASSES):
        fast = per_bucket(
            gaps.join(fast, on=_S, how="left").with_columns(
                pl.col("_use") & ~short(pl.col("_c"), pl.col("_fast"))
            )
        )
    df = df.join(fast, on=_S, how="left").sort(_S, "ts_ms")
    expected = (
        pl.when(pl.col("_slow"))
        .then(step_ms / pl.col("_I"))
        .otherwise(pl.col("_fast").fill_null(1.0))
    )
    return df.with_columns(expected.alias("expected"))


def _local_interval(nz: pl.DataFrame, gap: str, step_ms: int) -> pl.DataFrame:
    """Per non-zero bucket n (rows sorted by series, ts; `_c` samples, `gap` ms since the previous
    non-zero bucket): `_I`/`_sl`, the interval and slowness that judge the gap ending at n, and
    `_It`/`_slt`, those for the open gap after n (trailing silence)."""
    k = LOCAL_GAPS
    g = pl.col(gap)
    med = g.rolling_median(window_size=2 * k + 1, center=True, min_samples=1).over(_S)
    kept = g.is_not_null() & (g <= HOLE_RATIO * med)
    parts = {
        "G": pl.when(kept).then(g).otherwise(0.0),
        "C": pl.when(kept).then(pl.col("_c")).otherwise(0.0),
        "L": (kept & (g > step_ms)).cast(pl.Float64),
        "N": kept.cast(pl.Float64),
    }
    nz = nz.with_columns(e.alias(f"_k{p}") for p, e in parts.items())
    glob = nz.group_by(_S).agg(
        *(pl.col(f"_k{p}").sum().alias(f"_glob{p}") for p in parts),
        pl.col("_c").median().alias("_typ"),
    )
    nz = nz.join(glob, on=_S, how="left").sort(_S, "ts_ms")
    nz = nz.with_columns(pl.col(f"_k{p}").cum_sum().over(_S).alias(f"_s{p}") for p in parts)
    nz = nz.with_columns(
        *(  # the k gaps ending here
            (pl.col(f"_s{p}") - pl.col(f"_s{p}").shift(k).over(_S).fill_null(0.0)).alias(f"_b{p}")
            for p in parts
        ),
        *(  # the k gaps after this one
            (
                pl.col(f"_s{p}").shift(-k).over(_S).fill_null(pl.col(f"_s{p}").last().over(_S))
                - pl.col(f"_s{p}")
            ).alias(f"_a{p}")
            for p in parts
        ),
    )
    nz = nz.with_columns(pl.col(f"_b{p}").shift(1).over(_S).alias(f"_p{p}") for p in parts)

    def side(x: str, min_gaps: float = LOCAL_MIN_GAPS) -> tuple[pl.Expr, pl.Expr, pl.Expr]:
        c = pl.col(f"_{x}C")
        valid = (pl.col(f"_{x}N") >= min_gaps).fill_null(False) & (c > 0).fill_null(False)
        i = pl.when(valid).then(pl.col(f"_{x}G") / c)
        slow = valid & (i > SLOW_MARGIN * step_ms) & (pl.col(f"_{x}L") >= SLOW_MIN_LONG)
        return valid, i, slow.fill_null(False)

    _, gi, gs = side("glob", 1)
    gi = pl.coalesce(gi, step_ms / pl.max_horizontal(pl.lit(1.0), pl.col("_typ")))
    pv, pi, ps = side("p")  # before the previous non-zero bucket
    av, ai, as_ = side("a")  # after this one
    bv, bi, bs = side("b")
    # I: of the side nearer this gap's own time per sample (a gap at the new rate after a change,
    # or the old rate before it; a hole is further above both: the slower one). Slower than the
    # step if either side is, so estimates near SLOW_MARGIN do not flicker
    x = g / pl.col("_c")
    off = lambda i: (i / x).log().abs()
    after = ~pv | (av & (x.is_null() | (off(ai) < off(pi))).fill_null(False))
    return nz.select(
        _S,
        "ts_ms",
        pl.coalesce(pl.when(after).then(ai).otherwise(pi), gi).alias("_I"),
        pl.when(pv | av).then(ps | as_).otherwise(gs).alias("_sl"),
        pl.coalesce(bi, gi).alias("_It"),
        pl.when(bv).then(bs).otherwise(gs).alias("_slt"),
    )


def _spread(df: pl.DataFrame, est: pl.DataFrame, step_ms: int) -> pl.DataFrame:
    """Per-gap estimates onto every bucket: a bucket takes the gap it lies in (the next non-zero
    bucket's), trailing buckets the open gap after the last one. Adds `_I` and `_slow`."""
    df = df.join(est, on=[_S, "ts_ms"], how="left").sort(_S, "ts_ms")
    pick = lambda gap, tail: pl.coalesce(
        pl.col(gap).backward_fill().over(_S), pl.col(tail).forward_fill().over(_S)
    )
    return df.with_columns(
        pick("_I", "_It").fill_null(float(step_ms)).alias("_I"),
        pick("_sl", "_slt").fill_null(False).alias("_slow"),
    ).drop("_sl", "_It", "_slt")


def _spilled(events: pl.DataFrame) -> pl.DataFrame:
    """At about one sample per bucket a scrape near a bucket boundary lands in the next or previous
    bucket: a 0 bucket and a 2 bucket, in either order with only 1s between (`_ev0`/`_ev2`; rows
    sorted by series, ts). Pairs each 0 with an adjacent 2; a 0 left unpaired is a lost scrape,
    except a last one still waiting for its 2 in a series seen spilling (the 2 may lie past the
    window end or inside a hole or UNKNOWN span, `_evx`, which also restarts the pairing). A 2
    opening a run (window start, after a reset) pairs with a 0 before it, out of sight: it is
    evidence of spilling but no credit for a later 0, unless the reset was a run of exactly two 0s
    (`_ev00`): a lost scrape next to a spilled one, whose 0 it pairs. Returns the paired 0 buckets with `_spill`
    = True."""
    ok_s: list[str] = []
    ok_t: list[int] = []
    cur, credit, pending, paired, fresh = None, False, None, False, True
    run_end = None
    rows = events.select(_S, "ts_ms", "_ev0", "_evx", "_ev00").iter_rows()
    for sid, t, is_zero, reset, pair_end in (*rows, (None,) * 5):
        if sid != cur:
            if pending is not None and paired:
                ok_s.append(cur)
                ok_t.append(pending)
            cur, credit, pending, paired, fresh = sid, False, None, False, True
            if sid is None:
                break
        if reset:
            if pending is not None and paired:
                ok_s.append(sid)
                ok_t.append(pending)
            credit, pending, fresh = False, None, True
            run_end = t if pair_end else None
            continue
        if not is_zero:
            if pending is not None:
                ok_s.append(sid)
                ok_t.append(pending)
                pending, paired = None, True
            elif fresh:
                if run_end is not None:  # its 0 is the spilled one of the two
                    ok_s.append(sid)
                    ok_t.append(run_end)
                paired = True
            else:
                credit = True
        elif credit:
            ok_s.append(sid)
            ok_t.append(t)
            credit, paired = False, True
        else:
            pending = t  # an earlier unpaired 0 stays a miss
        fresh, run_end = False, None
    return pl.DataFrame(
        {_S: ok_s, "ts_ms": ok_t, "_spill": [True] * len(ok_s)},
        schema={_S: pl.String, "ts_ms": pl.Int64, "_spill": pl.Boolean},
    )


def _interval_change(df: pl.DataFrame) -> tuple[pl.DataFrame, pl.Expr]:
    """Series whose sample rate differs >= CHANGE_RATIO between the first and second half of their
    non-zero buckets: the half further from the series' baseline (typical count) gets
    INTERVAL_CHANGE. Only a flag: the states are unchanged. Returns df with helper bounds and the
    flag expression."""
    # (series with slower-than-step stretches show a 0/1 pattern there: _gap_interval_change)
    nz = (
        df.filter((pl.col("observed") > 0) & ~pl.col("_by_gap"))
        .sort(_S, "ts_ms")
        .with_columns((pl.int_range(pl.len()).over(_S) >= pl.len().over(_S) // 2).alias("_late"))
    )
    half = {
        h: nz.filter(pl.col("_late") == (h == "late"))
        .group_by(_S)
        .agg(
            pl.col("observed").median().alias(f"_m_{h}"),
            pl.col("observed").mean().alias(f"_mean_{h}"),
            pl.col("observed").quantile(0.25).alias(f"_q1_{h}"),
            pl.col("observed").quantile(0.75).alias(f"_q3_{h}"),
            pl.len().alias(f"_n_{h}"),
            pl.col("ts_ms").min().alias(f"_a_{h}"),
            pl.col("ts_ms").max().alias(f"_b_{h}"),
        )
        for h in ("early", "late")
    }
    exp = nz.group_by(_S).agg(pl.col("observed").median().clip(lower_bound=1.0).alias("_exp"))
    st = half["early"].join(half["late"], on=_S).join(exp, on=_S)
    lo = pl.min_horizontal("_mean_early", "_mean_late")
    hi = pl.max_horizontal("_mean_early", "_mean_late")
    # clear separation, not just a ratio: alternating low counts (2,1,2,1) are steady jitter
    separated = (
        pl.when(pl.col("_mean_late") >= pl.col("_mean_early"))
        .then(pl.col("_q1_late") > pl.col("_q3_early"))
        .otherwise(pl.col("_q1_early") > pl.col("_q3_late"))
    )
    dist = lambda m: (m / pl.col("_exp")).log().abs()
    late_is_odd = dist(pl.col("_m_late")) >= dist(pl.col("_m_early"))
    st = (
        st.filter(
            (pl.col("_n_early") >= CHANGE_MIN_BUCKETS)
            & (pl.col("_n_late") >= CHANGE_MIN_BUCKETS)
            & (hi >= CHANGE_RATIO * lo)
            & separated
        )
        .with_columns(
            pl.when(late_is_odd).then(pl.col("_a_late")).otherwise(pl.col("_a_early")).alias("_lo"),
            pl.when(late_is_odd).then(pl.col("_b_late")).otherwise(pl.col("_b_early")).alias("_hi"),
        )
        .select(_S, "_lo", "_hi")
    )
    df = df.join(st, on=_S, how="left")
    changed = pl.when(pl.col("ts_ms").is_between(pl.col("_lo"), pl.col("_hi"))).then(
        int(Flag.INTERVAL_CHANGE)
    )
    return df, changed.otherwise(0)


def _gap_interval_change(df: pl.DataFrame) -> tuple[pl.DataFrame, pl.Expr]:
    """Rate changes in series with a slower-than-step stretch, where 0/1 counts cannot show them:
    from the time per sample over the CHANGE_WINDOW non-zero buckets centred on each (Σgap /
    Σcount, holes left out). When that local interval spans >= CHANGE_RATIO, the buckets split at
    the geometric middle into a faster and a slower side; with CHANGE_MIN_BUCKETS each and clear
    separation (the slower side's lower quartile above the faster side's upper one, as for
    counts) the side covering less of the window gets INTERVAL_CHANGE, with the 0 buckets of its
    gaps. Only a flag."""
    nz = (
        df.filter(pl.col("observed") > 0)
        .sort(_S, "ts_ms")
        .with_columns(pl.col("ts_ms").diff().over(_S).cast(pl.Float64).alias("_gx"))
        .with_columns((pl.col("_gx") / pl.col("observed")).alias("_x"))
        .filter(pl.col("_by_gap") & pl.col("_x").is_not_null())
    )
    # smoothed as a rate (Σgap / Σsamples over the window), holes left out: a median of the
    # per-bucket values flips between the two snapped gaps of a steady ~1.5-step cadence
    g = pl.col("_gx")
    med = g.rolling_median(window_size=2 * LOCAL_GAPS + 1, center=True, min_samples=1)
    kept = g <= HOLE_RATIO * med.over(_S)
    roll = lambda e: (
        pl.when(kept).then(e).otherwise(0.0)
        .rolling_sum(window_size=CHANGE_WINDOW, center=True, min_samples=1)
        .over(_S)
    )  # fmt: skip
    nz = nz.with_columns((roll(g) / roll(pl.col("observed"))).alias("_mx")).filter(
        pl.col("_mx").is_finite()
    )
    nz = nz.with_columns(
        (pl.col("_mx") >= (pl.col("_mx").min() * pl.col("_mx").max()).sqrt())
        .over(_S)
        .alias("_slower")
    )
    sl = pl.col("_slower")
    st = (
        nz.group_by(_S)
        .agg(
            (pl.col("_mx").max() / pl.col("_mx").min()).alias("_ratio"),
            sl.sum().alias("_n_slow"),
            (~sl).sum().alias("_n_fast"),
            pl.col("_x").filter(sl).quantile(0.25).alias("_q1_slow"),
            pl.col("_x").filter(~sl).quantile(0.75).alias("_q3_fast"),
            pl.col("_gx").filter(sl).sum().alias("_t_slow"),
            pl.col("_gx").filter(~sl).sum().alias("_t_fast"),
        )
        .filter(
            (pl.col("_ratio") >= CHANGE_RATIO)
            & (pl.col("_n_slow") >= CHANGE_MIN_BUCKETS)
            & (pl.col("_n_fast") >= CHANGE_MIN_BUCKETS)
            & (pl.col("_q1_slow") > pl.col("_q3_fast"))
        )
        .select(_S, (pl.col("_t_slow") <= pl.col("_t_fast")).alias("_odd_slow"))
    )
    # (the first non-zero bucket has no gap: it takes the next one's side, as 0 buckets do)
    hit = nz.join(st, on=_S, how="left").select(
        _S, "ts_ms", (pl.col("_slower") == pl.col("_odd_slow")).fill_null(False).alias("_gchg")
    )
    df = (
        df.join(hit, on=[_S, "ts_ms"], how="left")
        .sort(_S, "ts_ms")
        .with_columns(
            pl.coalesce(
                pl.col("_gchg").backward_fill().over(_S), pl.col("_gchg").forward_fill().over(_S)
            ).alias("_gchg")
        )
    )
    alive = pl.col("first_seen").is_not_null() & (pl.col("ts_ms") >= pl.col("first_seen"))
    hit_b = pl.col("_gchg").fill_null(False) & alive
    return df, pl.when(hit_b).then(int(Flag.INTERVAL_CHANGE)).otherwise(0)


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


def _classify(
    any_alive: pl.Expr, any_unknown: pl.Expr, nothing_seen: pl.Expr, any_gap: pl.Expr
) -> pl.Expr:
    """Shared by coarsen and merge; works from states, never re-applies jitter to sums."""
    return (
        pl.when(~any_alive)
        .then(int(State.ABSENT))
        .when(any_unknown)
        .then(int(State.UNKNOWN))
        .when(nothing_seen)
        .then(int(State.EMPTY))
        .when(any_gap)
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
            pl.col("state").is_in([int(State.PARTIAL), int(State.EMPTY)]).any().alias("_gap"),
            # state-based, not observed == 0: a slower-than-step series' fine buckets are OK with
            # no samples inside their cadence
            pl.col("state").is_in([int(State.OK), int(State.PARTIAL)]).any().alias("_seen"),
            pl.col("flags").bitwise_or().alias("flags"),
        )
        .with_columns(
            _classify(pl.col("_alive"), pl.col("_unknown"), ~pl.col("_seen"), pl.col("_gap")).alias(
                "state"
            )
        )
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
                pl.col("alive") > 0,
                pl.col("_unknown"),
                pl.col("reporting") == 0,
                pl.col("reporting") < pl.col("alive"),
            ).alias("state")
        )
        .sort("group", "ts_ms")
        .select(GROUP_SCHEMA.names)
    )
    return out.to_arrow().cast(GROUP_SCHEMA)
