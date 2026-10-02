import pyarrow as pa

from telemetry_nerd.core.panel_payloads import RUG_SERIES_CAP, rug_payload, state_payload
from telemetry_nerd.model.bucket_state import STATE_SCHEMA, State

STEP = 60_000


def table(per_series):
    rows = [(t * STEP, sid, s) for sid, ss in per_series.items() for t, s in enumerate(ss, 1)]
    return pa.table(
        {"ts_ms": [r[0] for r in rows], "series_id": [r[1] for r in rows],
         "observed": [0.0] * len(rows), "expected": [4.0] * len(rows),
         "state": [int(r[2]) for r in rows], "flags": [0] * len(rows)},
        schema=STATE_SCHEMA,
    )  # fmt: skip


def test_series_are_ordered_by_worst_state_then_id():
    t = table({
        "a": [State.OK, State.PARTIAL], "b": [State.OK, State.UNKNOWN],
        "c": [State.OK, State.EMPTY], "d": [State.ABSENT, State.OK], "e": [State.OK, State.OK],
        "f": [State.EMPTY, State.PARTIAL],
    })  # fmt: skip
    assert [s["id"] for s in state_payload(t)] == ["b", "c", "f", "a", "d"]  # e is all ok


def test_rug_is_capped_and_reports_how_many_were_left_out():
    ids = [f"s{i}" for i in range(9)]
    per = {sid: [State.OK, State.PARTIAL] for sid in ids}
    per["s8"] = [State.OK, State.EMPTY]  # sorts last by id but is worse than the partials
    rows, more = rug_payload(table(per))
    assert len(rows) == RUG_SERIES_CAP == 5 and more == 4
    assert rows[0]["id"] == "s8"


def test_all_ok_rug_is_empty():
    assert rug_payload(table({"a": [State.OK, State.OK]})) == ([], 0)
