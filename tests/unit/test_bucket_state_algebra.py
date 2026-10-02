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
    assert (
        merge(t, g).sort_by("ts_ms").to_pylist() == merge(shuffled, g).sort_by("ts_ms").to_pylist()
    )


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
    assert (
        merge(both, {**g, "ghost": "g"}).sort_by("ts_ms").to_pylist()
        == merge(t, g).sort_by("ts_ms").to_pylist()
    )
