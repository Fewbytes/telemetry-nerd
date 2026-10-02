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
    t = table(
        [
            (STEP, "a", 0, 4, State.UNKNOWN, 0),
            (2 * STEP, "a", 4, 4, State.OK, 0),
            (3 * STEP, "a", 0, 4, State.ABSENT, 0),
            (4 * STEP, "a", 0, 4, State.ABSENT, 0),
        ]
    )
    out = as_rows(coarsen(t, 2 * STEP))
    assert [r[4] for r in out] == [State.UNKNOWN, State.ABSENT]


def test_merge_counts_alive_reporting_and_silent():
    t = table(
        [
            (STEP, "a", 4, 4, State.OK, 0),
            (STEP, "b", 0, 4, State.EMPTY, 0),
            (STEP, "c", 0, 4, State.ABSENT, 0),
        ]
    )
    [row] = merge(t, {"a": "g", "b": "g", "c": "g"}).to_pylist()
    assert (row["alive"], row["reporting"], row["silent"]) == (2, 1, ["b"])
    assert row["state"] == State.PARTIAL and row["expected"] == 8.0


def test_all_ok_jitter_members_merge_to_ok():
    t = table([(STEP, k, 3, 4, State.OK, 0) for k in "abc"])
    [row] = merge(t, {k: "g" for k in "abc"}).to_pylist()
    assert row["state"] == State.OK and row["observed"] == 9.0 and row["expected"] == 12.0


def test_all_ok_jitter_subbuckets_coarsen_to_ok():
    t = table([(i * STEP, "a", 3, 4, State.OK, 0) for i in range(1, 5)])
    [row] = as_rows(coarsen(t, 4 * STEP))
    assert row[4] == State.OK


def test_coarsen_ok_plus_partial_is_partial():
    t = table([(STEP, "a", 4, 4, State.OK, 0), (2 * STEP, "a", 2, 4, State.PARTIAL, 0)])
    [row] = as_rows(coarsen(t, 2 * STEP))
    assert row[4] == State.PARTIAL


def test_coarsen_unknown_is_neither_present_nor_missing():
    """An UNKNOWN fine bucket carries 0 observed / 0 expected (compute): the coarse bucket reports
    the coverage of the sub-buckets that could tell, and is UNKNOWN all the same."""
    t = table([(STEP, "a", 0, 0, State.UNKNOWN, 0), (2 * STEP, "a", 4, 4, State.OK, 0)])
    [row] = as_rows(coarsen(t, 2 * STEP))
    assert row[4] == State.UNKNOWN and row[2:4] == (4.0, 4.0)


def test_coarsen_one_failed_minute_of_ten_keeps_the_other_nine_at_full_coverage():
    t = table(
        [(i * STEP, "a", 0 if i == 4 else 4, 0 if i == 4 else 4,
          State.UNKNOWN if i == 4 else State.OK, 0) for i in range(1, 11)]
    )  # fmt: skip
    [row] = as_rows(coarsen(t, 10 * STEP))
    assert row[4] == State.UNKNOWN and row[2] / row[3] == 1.0 and row[3] == 36.0


def test_merge_one_unknown_member_of_44_leaves_the_coverage_of_the_other_43():
    rows = [(STEP, f"s{k}", 4, 4, State.OK, 0) for k in range(43)]
    rows.append((STEP, "s43", 0, 0, State.UNKNOWN, 0))
    [row] = merge(table(rows), {f"s{k}": "g" for k in range(44)}).to_pylist()
    assert (row["state"], row["alive"], row["reporting"]) == (State.UNKNOWN, 44, 43)
    assert row["observed"] == 43 * 4.0 and row["expected"] == 43 * 4.0


states_st = st.sampled_from([State.OK, State.PARTIAL, State.EMPTY, State.ABSENT, State.UNKNOWN])


@st.composite
def state_tables(draw, n_series=3, n_buckets=8):
    rows = []
    for k in range(n_series):
        for i in range(1, n_buckets + 1):
            s = draw(states_st)
            obs = {
                State.OK: 4,
                State.PARTIAL: 2,
                State.EMPTY: 0,
                State.ABSENT: 0,
                State.UNKNOWN: 0,
            }[s]
            exp = 0.0 if s == State.UNKNOWN else 4.0  # compute(): unknown counts as neither
            rows.append((i * STEP, f"s{k}", float(obs), exp, int(s), draw(st.integers(0, 15))))
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
    assert (
        merge(t, g).sort_by("ts_ms").to_pylist() == merge(shuffled, g).sort_by("ts_ms").to_pylist()
    )


def _reference_group(members):
    """§5.2 spelled out for one bucket: members are (series, observed, expected, state)."""
    alive = [m for m in members if m[3] != State.ABSENT]
    reporting = [m for m in alive if m[3] in (State.OK, State.PARTIAL)]
    if not alive:
        state = State.ABSENT
    elif any(m[3] == State.UNKNOWN for m in alive):
        state = State.UNKNOWN
    elif not reporting:
        state = State.EMPTY
    elif len(reporting) < len(alive):
        state = State.PARTIAL
    else:
        state = State.OK
    return {
        "alive": len(alive),
        "reporting": len(reporting),
        "state": state,
        "observed": float(sum(m[1] for m in alive)),
        "expected": float(sum(m[2] for m in alive)),
        "silent": sorted(m[0] for m in alive if m[3] == State.EMPTY),
    }


@settings(max_examples=60)
@given(state_tables())
def test_merge_matches_the_per_bucket_rules(t):
    """Every group bucket (none missing, none extra) follows §5.2: unknown absorbs, absent is
    outside the denominator, unknown buckets report no samples."""
    g = {f"s{k}": "g" for k in range(3)}
    by_ts: dict[int, list] = {}
    for r in t.to_pylist():
        by_ts.setdefault(r["ts_ms"], []).append(
            (r["series_id"], r["observed"], r["expected"], r["state"])
        )
    got = {r["ts_ms"]: r for r in merge(t, g).to_pylist()}
    assert got.keys() == by_ts.keys()
    for ts, members in by_ts.items():
        want = _reference_group(members)
        assert {k: got[ts][k] for k in want} == want


@settings(max_examples=60)
@given(state_tables())
def test_coarsen_unknown_absorbs_and_sums_what_could_tell(t):
    """Per coarse bucket: unknown iff some sub-bucket is; the sums are over the alive ones (an
    unknown one carries 0 / 0, so it is neither present nor missing)."""
    rows = t.to_pylist()
    for r in coarsen(t, 4 * STEP).to_pylist():
        subs = [
            x
            for x in rows
            if x["series_id"] == r["series_id"] and r["ts_ms"] - 4 * STEP < x["ts_ms"] <= r["ts_ms"]
        ]
        has_unknown = any(x["state"] == State.UNKNOWN for x in subs)
        assert (r["state"] == State.UNKNOWN) is has_unknown
        alive = [x for x in subs if x["state"] != State.ABSENT]
        assert r["observed"] == sum(x["observed"] for x in alive)
        assert r["expected"] == sum(x["expected"] for x in alive)


@settings(max_examples=60)
@given(state_tables())
def test_all_absent_member_changes_nothing(t):
    g = {f"s{k}": "g" for k in range(3)}
    ghost = table([(i * STEP, "ghost", 0.0, 4.0, int(State.ABSENT), 0) for i in range(1, 9)])
    both = pa.concat_tables([t, ghost])
    assert (
        merge(both, {**g, "ghost": "g"}).sort_by("ts_ms").to_pylist()
        == merge(t, g).sort_by("ts_ms").to_pylist()
    )
