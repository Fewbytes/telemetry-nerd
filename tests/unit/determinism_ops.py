"""Representative ops over a fixed synthetic fleet, printed as one JSON document (zek0.3, n3sv).

`test_determinism.py` runs this module in subprocesses under different POLARS_MAX_THREADS,
PYTHONHASHSEED and source row orders and asserts the documents are identical: polars group_by /
unique order (random per process), set iteration and thread partitioning must never reach output.

    python -m tests.unit.determinism_ops [--shuffle SEED]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pyarrow as pa

from telemetry_nerd.core.service import ChartRejected
from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult, labels_json
from telemetry_nerd.model.series import series_id as make_series_id
from tests.unit.fakes import FakeSource, make_service

H, DAY = 3_600_000, 86_400_000
NOW = 1_790_000_000_000 - 1_790_000_000_000 % DAY + 15 * H  # a fixed 15:00 UTC
M = 12  # members; pairs share their values so every ranking has ties to break


def _noise(key: str, t: np.ndarray) -> np.ndarray:
    return (
        np.array(
            [
                int.from_bytes(hashlib.blake2b(f"{key}|{x}".encode(), digest_size=4).digest())
                for x in t
            ]
        )
        / 2**32
    )


class GridSource(FakeSource):
    """M series of a daily load with deterministic noise, member pairs identical; the pair (4, 5)
    is silent for an hour of every day, member 11 runs at 3x. `shuffle` permutes the returned
    rows and series (a source or cache owes no row order)."""

    def __init__(self, shuffle: int | None) -> None:
        super().__init__(n_series=M)
        self.shuffle = shuffle

    def _perm(self, n: int, salt: int) -> np.ndarray:
        if self.shuffle is None:
            return np.arange(n)
        return np.random.default_rng([self.shuffle, salt]).permutation(n)

    async def fetch(self, expr, rng, step_ms):
        self.calls += 1
        ts = np.arange(rng.start_ms, rng.end_ms + 1, step_ms, dtype=np.int64)
        hour = (ts % DAY) / H
        daily = 100 * (1 + 2.0 * np.exp(-(((hour - 14) / 3.5) ** 2)))
        cols: dict[str, list] = {k: [] for k in BUCKET_SCHEMA.names}
        m = 3 if 'job="web"' in expr else M  # a few series: line, spc and spectrum panels
        labels = [{"pod": f"api-{k:02d}", "job": "api"} for k in range(m)]
        for k, lb in enumerate(labels):
            y = daily * (1 + 0.1 * (_noise(f"pair{k // 2}", ts) - 0.5)) * (3 if k == 11 else 1)
            keep = ~(((ts % DAY) >= 10 * H) & ((ts % DAY) < 11 * H)) if k in (4, 5) else ts >= 0
            n = int(keep.sum())
            cols["ts_ms"] += ts[keep].tolist()
            cols["series_id"] += [make_series_id(self.name, lb)] * n
            cols["avg"] += y[keep].tolist()
            cols["min"] += (y[keep] * 0.9).tolist()
            cols["max"] += (y[keep] * 1.1).tolist()
            cols["count"] += [max(1, step_ms // self.resolution_ms)] * n
        buckets = pa.table(cols, schema=BUCKET_SCHEMA)
        buckets = buckets.take(self._perm(buckets.num_rows, rng.start_ms // step_ms))
        series = pa.table(
            {
                "series_id": [make_series_id(self.name, lb) for lb in labels],
                "labels": [labels_json(lb) for lb in labels],
            },
            schema=SERIES_SCHEMA,
        )
        series = series.take(self._perm(m, rng.start_ms // step_ms + 1))
        return FetchResult(buckets, series)

    async def fetch_histogram(self, selector, by, rng, step_ms):
        res = await super().fetch_histogram(selector, by, rng, step_ms)
        rows, cols = res.rows, res.columns
        return type(res)(
            rows.take(self._perm(rows.num_rows, 2)),
            cols.take(self._perm(cols.num_rows, 3)),
            *[
                getattr(res, f)
                for f in type(res).__dataclass_fields__
                if f not in ("rows", "columns")
            ],
        )


async def ops(shuffle: int | None, tmp: Path) -> dict:
    svc = make_service(tmp, source=GridSource(shuffle), clock=lambda: NOW)
    out: dict = {}
    q = await svc.query("sum by (pod, job) (rate(http_requests_total[5m]))", "now-6h", "now", "5m")
    d = q["dataset"]
    out["query"] = q
    out["analyze"] = svc.analyze(d)
    out["fleet"] = svc.fleet(d)
    out["compare_seasonal"] = await svc.compare_seasonal(d, cycles=["1d"])
    out["spectrum"] = svc.spectrum(d)
    for mark in ("auto", "fleet", "spc", "seasonal", "spectrum"):
        try:  # a refusal is output too: its text must be as stable
            res = svc.show(d, f"how do the pods compare ({mark})?", mark=mark)
        except (ValueError, ChartRejected) as e:
            out[f"show_{mark}"] = str(e)
            continue
        out[f"panel_data_{mark}"] = svc.panel_data(res.panel.id, width_px=300)
        out[f"panel_card_{mark}"] = await svc.panel_card(res.panel.id)
    few = await svc.query('rate(http_requests_total{job="web"}[5m])', "now-6h", "now", "5m")
    out["analyze_few"] = svc.analyze(few["dataset"])
    for mark in ("auto", "spc", "spectrum"):
        res = svc.show(few["dataset"], f"is web steady ({mark})?", mark=mark)
        out[f"panel_data_few_{mark}"] = svc.panel_data(res.panel.id, width_px=300)
    out["filter"] = svc.filter(d, "lowpass", "1h", "smooth the daily load")
    out["operating_profile"] = await svc.operating_profile("rate(http_requests_total[5m])")
    dist = await svc.query_distribution("http_request_duration_seconds", by=["instance"],
                                        start="now-2h", end="now", step="5m")  # fmt: skip
    out["distribution"] = dist
    out["fraction_over"] = svc.fraction_over(dist["dataset"], 1.0, by_series=True)
    out["compare_seasonal_dist"] = await svc.compare_seasonal(dist["dataset"], cycles=["1d"])
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--shuffle", type=int, default=None)
    args = p.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        doc = asyncio.run(ops(args.shuffle, Path(tmp)))
    json.dump(doc, sys.stdout, default=str, allow_nan=True)


if __name__ == "__main__":
    main()
