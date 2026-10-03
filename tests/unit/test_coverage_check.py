import pyarrow as pa

from telemetry_nerd.core.coverage_check import claim_coverage
from telemetry_nerd.model.bucket_state import STATE_SCHEMA, State

STEP = 60_000


def table(states, obs=None):
    n = len(states)
    obs = obs or [4.0 if s == State.OK else 0.0 for s in states]
    return pa.table({"ts_ms": [(i + 1) * STEP for i in range(n)], "series_id": ["a"] * n,
                     "observed": obs, "expected": [4.0] * n, "state": [int(s) for s in states],
                     "flags": [0] * n}, schema=STATE_SCHEMA)  # fmt: skip


def test_clean_window_passes():
    assert claim_coverage(table([State.OK] * 4), 0, 4 * STEP, STEP) == []


def test_unknown_in_window_blocks():
    [c] = claim_coverage(table([State.OK, State.UNKNOWN, State.OK]), 0, 3 * STEP, STEP)
    assert (c.code, c.severity) == ("untrusted_data", "blocks_claim")


def test_unknown_outside_window_is_ignored():
    assert claim_coverage(table([State.OK, State.OK, State.UNKNOWN]), 0, 2 * STEP, STEP) == []


def test_mostly_missing_blocks_and_little_missing_warns():
    [c] = claim_coverage(table([State.EMPTY, State.EMPTY, State.OK]), 0, 3 * STEP, STEP)
    assert c.severity == "blocks_claim"
    [c] = claim_coverage(table([State.EMPTY, State.OK, State.OK, State.OK]), 0, 4 * STEP, STEP)
    assert (c.code, c.severity) == ("missing_data", "warn")


def test_window_outside_the_data_blocks():
    [c] = claim_coverage(table([State.OK] * 3), 10 * STEP, 12 * STEP, STEP)
    assert (c.code, c.severity) == ("missing_data", "blocks_claim")


def test_window_over_only_absent_buckets_blocks():
    [c] = claim_coverage(table([State.ABSENT, State.ABSENT, State.OK]), 0, 2 * STEP, STEP)
    assert (c.code, c.severity) == ("missing_data", "blocks_claim")


def test_claim_inside_one_ok_bucket_overlaps_it():
    # bucket (60s, 120s] is OK; claim (70s, 110s] lies strictly inside it
    assert claim_coverage(table([State.OK, State.OK]), 70_000, 110_000, STEP) == []


def test_claim_touching_a_bucket_only_at_its_edge_does_not_overlap():
    [c] = claim_coverage(table([State.OK]), STEP, 2 * STEP, STEP)  # bucket (0,60s] ends at start
    assert c.severity == "blocks_claim"


# --- per series over scope.selector (telemetry-nerd-wog) ---------------------------------------
N = 30  # buckets of one minute
PODS = [f"p{i:02d}" for i in range(20)]
LABELS = {p: {"job": "api", "pod": p} for p in PODS}


def fleet(**special):
    """20 pods x N one-minute buckets, all OK unless `special[pod]` = list of N states."""
    rows = {"ts_ms": [], "series_id": [], "observed": [], "expected": [], "state": [], "flags": []}
    for p in PODS:
        for i, s in enumerate(special.get(p, [State.OK] * N)):
            rows["ts_ms"].append((i + 1) * STEP)
            rows["series_id"].append(p)
            rows["observed"].append(4.0 if s == State.OK else 0.0)
            rows["expected"].append(4.0)
            rows["state"].append(int(s))
            rows["flags"].append(0)
    return pa.Table.from_pydict(rows, schema=STATE_SCHEMA)


SILENT = [State.OK] * 5 + [State.EMPTY] * 20 + [State.OK] * 5  # buckets 6..25 empty
W = (5 * STEP, 25 * STEP)  # claim window: exactly the silent stretch


def check(states, selector, window=W):
    return claim_coverage(states, *window, STEP, labels=LABELS, selector=selector, metric="up")


def test_claim_on_a_silent_pod_blocks_and_on_a_healthy_one_passes():
    states = fleet(p03=SILENT)
    [c] = check(states, 'up{pod="p03"}')
    assert c.severity == "blocks_claim" and 'pod="p03"} has no samples' in c.message
    assert check(states, 'up{pod="p04"}') == []


def test_fleet_claim_with_two_silent_pods_warns_naming_both():
    c, m = check(fleet(p03=SILENT, p11=SILENT), 'up{job="api"}')
    assert m.code == "membership" and "left and rejoined" in m.message  # bounded 20 min gaps
    assert (c.code, c.severity) == ("missing_data", "warn")
    assert '{pod="p03"}' in c.message and '{pod="p11"}' in c.message
    assert c.message.startswith("2 of 20 series")
    assert c.where.series == ["p03", "p11"]


def test_fleet_claim_with_most_pods_silent_blocks():
    [c] = check(fleet(**{p: SILENT for p in PODS[:11]}), "up")
    assert c.severity == "blocks_claim" and c.message.startswith("11 of 20 series")
    assert "and 1 more" in c.message  # names capped at MAX_NAMED
    c, _ = check(fleet(**{p: SILENT for p in PODS[:10]}), "up")  # exactly half: rests on 10
    assert c.severity == "warn"


def test_late_born_and_early_ended_pods_are_not_missing_in_a_fleet_claim():
    born = [State.ABSENT] * 10 + [State.OK] * 20
    ended = [State.OK] * 15 + [State.EMPTY] * 15
    out = check(fleet(p01=born, p02=ended), "up")
    assert [c.severity for c in out] == ["warn"]
    [c] = out
    assert c.code == "membership"
    assert "may not have existed before" in c.message
    assert "last sample at" in c.message and "may return after the window" in c.message
    assert "left" not in c.message.split("may have left")[0]  # never asserts it left
    assert "count by (pod) (up)" in c.message  # the membership hint


def test_single_pod_claim_counts_its_unobserved_tail_and_head_as_missing():
    ended = [State.OK] * 8 + [State.EMPTY] * 22
    [c] = check(fleet(p02=ended), 'up{pod="p02"}')
    assert c.severity == "blocks_claim" and "last sample at" in c.message
    born = [State.ABSENT] * 20 + [State.OK] * 10
    [c] = check(fleet(p01=born), 'up{pod="p01"}')
    assert c.severity == "blocks_claim"


def test_unknown_on_another_pod_does_not_block_a_single_pod_claim():
    states = fleet(p07=[State.OK] * 10 + [State.UNKNOWN] * 5 + [State.OK] * 15)
    assert check(states, 'up{pod="p01"}') == []
    [c] = check(states, 'up{pod="p07"}')
    assert (c.code, c.severity) == ("untrusted_data", "blocks_claim")
    [c] = check(states, "up")
    assert (c.code, c.severity) == ("untrusted_data", "warn") and '{pod="p07"}' in c.message


def test_unparseable_selector_falls_back_to_every_series_and_says_so():
    c, _ = check(fleet(p03=SILENT), 'up{pod="p03"} / on(pod) other{pod="p03"}')
    assert c.severity == "warn" and c.message.startswith("1 of 20 series")
    assert "judged over every evidence series" in c.message


def test_selector_matching_no_evidence_series_blocks():
    [c] = check(fleet(), 'up{pod="nope"}')
    assert c.severity == "blocks_claim" and "does not contain" in c.message
    [c] = check(fleet(), 'down{pod="p01"}')
    assert c.severity == "blocks_claim" and "metric 'up'" in c.message


def test_selector_forms():
    states = fleet(p03=SILENT)
    assert check(states, 'rate(up{pod=~"p0[4-9]"}[5m])') == []  # anchored regex, inside rate
    assert check(states, 'sum by (pod) (rate(up{pod!="p03"}[5m]))') == []
    assert check(states, 'up{pod=~"p0"}')[0].message.startswith("The claim names")  # anchored
    [c] = check(states, 'up{pod!~"p0[0-24-9]|p1."}')  # only p03
    assert c.severity == "blocks_claim"
    [c] = check(states, 'up{pod="p03", zone="eu"}')  # zone carried by no series: not applied
    assert c.severity == "blocks_claim" and "zone" in c.message


def test_long_mid_gap_reads_as_loss_or_leave_and_rejoin():
    gap = [State.OK] * 8 + [State.EMPTY] * 6 + [State.OK] * 16  # 6 min, bounded by samples
    [c] = check(fleet(p05=gap), 'up{pod="p05"}', (0, N * STEP))
    assert c.severity == "warn"
    assert "has no samples 1970-01-01T00:08:00+00:00–00:14:00+00:00" in c.message
    assert "scrape loss, or the series left and rejoined; not distinguished yet" in c.message
    short = [State.OK] * 8 + [State.EMPTY] * 2 + [State.OK] * 20
    [c] = check(fleet(p05=short), 'up{pod="p05"}', (0, N * STEP))
    assert "rejoined" not in c.message
    [c, m] = check(fleet(p05=gap), "up", (0, N * STEP))
    assert c.message.startswith("1 of 20 series have fewer samples")
    assert m.code == "membership" and "rejoined" in m.message
