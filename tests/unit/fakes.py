import pyarrow as pa

from telemetry_nerd.core.events import EventLog
from telemetry_nerd.core.service import TelemetryService
from telemetry_nerd.core.workspace_service import WorkspaceService
from telemetry_nerd.datasets.cache import SeriesCache
from telemetry_nerd.datasets.db import open_duckdb
from telemetry_nerd.datasets.store import DatasetStore
from telemetry_nerd.model.series import (
    BUCKET_SCHEMA,
    SERIES_SCHEMA,
    FetchResult,
    labels_json,
    series_id,
)
from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.workspace.db import open_workspace_db
from telemetry_nerd.workspace.objects import ObjectStore
from telemetry_nerd.workspace.store import WorkspaceStore

NOW = 6_000_000_000


class FakeSource:
    def __init__(self, name="fake", n_series=2, resolution_ms=15_000, identity=None):
        self.name = name
        self.identity = identity or f"fake|{name}|{resolution_ms}"
        self.n_series = n_series
        self.resolution_ms = resolution_ms
        self.calls = 0

    async def fetch(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult:
        self.calls += 1
        ts = list(range(rng.start_ms, rng.end_ms + 1, step_ms))
        labels = [{"instance": f"i{k}"} for k in range(self.n_series)]
        sids = [series_id(self.name, lb) for lb in labels]
        rows = [(t, sid, float(k + 1)) for k, sid in enumerate(sids) for t in ts]
        buckets = pa.table(
            {
                "ts_ms": [r[0] for r in rows],
                "series_id": [r[1] for r in rows],
                "avg": [r[2] for r in rows],
                "min": [r[2] - 0.5 for r in rows],
                "max": [r[2] + 0.5 for r in rows],
                "count": [4] * len(rows),
            },
            schema=BUCKET_SCHEMA,
        )
        series = pa.table(
            {"series_id": sids, "labels": [labels_json(lb) for lb in labels]}, schema=SERIES_SCHEMA
        )
        return FetchResult(buckets, series)


class NonFiniteSource(FakeSource):
    """Emits NaN/Inf values (defense in depth: a source adapter that forgot to null them)."""

    async def fetch(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult:
        res = await super().fetch(expr, rng, step_ms)
        b = res.buckets
        n = b.num_rows
        cols = {name: b.column(name).to_pylist() for name in b.schema.names}
        cols["avg"] = [float("nan")] * n
        cols["min"] = [float("-inf")] * n
        cols["max"] = [float("inf")] * n
        return FetchResult(pa.table(cols, schema=BUCKET_SCHEMA), res.series)


class PartialSource(FakeSource):
    async def fetch(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult:
        res = await super().fetch(expr, rng, step_ms)
        return FetchResult(res.buckets, res.series, partial=1)


def make_service(tmp_path, source=None, clock=lambda: NOW) -> TelemetryService:
    source = source or FakeSource()
    con = open_duckdb(tmp_path / "series.duckdb")
    wcon = open_workspace_db(tmp_path / "workspace.db")
    workspace = WorkspaceStore(wcon, clock=clock)
    datasets = DatasetStore(con, workspace.next_id, clock=clock)
    log = EventLog(wcon, clock=clock)
    objects = ObjectStore(wcon, workspace.next_id, clock=clock)
    return TelemetryService(
        sources={"default": source},
        cache=SeriesCache(con, clock=clock),
        datasets=datasets,
        workspace=workspace,
        log=log,
        ws=WorkspaceService(workspace, objects, datasets, log),
        clock=clock,
    )
