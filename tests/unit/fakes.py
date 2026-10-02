import pyarrow as pa

from telemetry_nerd.analysis.histogram import from_matrix, histogram_expr
from telemetry_nerd.catalog.relation_store import RelationStore
from telemetry_nerd.catalog.sample_store import SampleStore
from telemetry_nerd.catalog.store import CatalogStore, FamilyStore
from telemetry_nerd.core.events import EventLog
from telemetry_nerd.core.service import TelemetryService
from telemetry_nerd.core.workspace_service import WorkspaceService
from telemetry_nerd.datasets.cache import SeriesCache
from telemetry_nerd.datasets.db import open_duckdb
from telemetry_nerd.datasets.store import DatasetStore
from telemetry_nerd.model.discovery import Discovery
from telemetry_nerd.model.series import (
    BUCKET_SCHEMA,
    SERIES_SCHEMA,
    FetchResult,
    labels_json,
    series_id,
)
from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.registry import SourceRegistry
from telemetry_nerd.sources.spec import SourceSpec
from telemetry_nerd.workspace.db import open_workspace_db
from telemetry_nerd.workspace.objects import ObjectStore
from telemetry_nerd.workspace.store import WorkspaceStore

NOW = 6_000_000_000


class FakeSource:
    def __init__(
        self,
        name="fake",
        n_series=2,
        resolution_ms=15_000,
        identity=None,
        values=None,
        cumulative=None,
        discovery=None,
    ):
        self.name = name
        self.identity = identity or f"fake|{name}|{resolution_ms}"
        self.n_series = n_series
        self.resolution_ms = resolution_ms
        self.calls = 0
        self.values = values or {}
        self.value_exprs: list[str] = []
        self.cumulative = cumulative or {"0.1": 90.0, "1": 99.0, "10": 100.0, "+Inf": 100.0}
        self.hist_selectors: list[str] = []
        self.discovery = discovery

    async def probe(self) -> dict:
        return {"reachable": True, "latency_ms": 0, "version": "fake"}

    async def discover(self) -> Discovery:
        return self.discovery or Discovery((), (), {}, None, 1.0, (), False)

    async def scrape_interval(self, selector: str) -> int | None:
        return self.resolution_ms

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
                "count": [max(1, step_ms // self.resolution_ms)] * len(rows),
            },
            schema=BUCKET_SCHEMA,
        )
        series = pa.table(
            {"series_id": sids, "labels": [labels_json(lb) for lb in labels]}, schema=SERIES_SCHEMA
        )
        return FetchResult(buckets, series)

    async def fetch_histogram(self, selector, by, rng, step_ms):
        self.calls += 1
        self.hist_selectors.append(selector)
        ts = range(rng.start_ms, rng.end_ms + 1, step_ms)
        result = [
            {
                "metric": {"instance": f"i{k}", "le": le},
                "values": [[t / 1000, str(c * (k + 1))] for t in ts],
            }
            for k in range(self.n_series)
            for le, c in self.cumulative.items()
        ]
        return from_matrix(self.name, result, expr=histogram_expr(selector, by, step_ms))

    async def fetch_values(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult:
        self.calls += 1
        self.value_exprs.append(expr)
        v = next((val for key, val in self.values.items() if key in expr), 1.0)
        ts = list(range(rng.start_ms, rng.end_ms + 1, step_ms))
        labels = [{"instance": f"i{k}"} for k in range(self.n_series)]
        sids = [series_id(self.name, lb) for lb in labels]
        rows = [(t, sid) for sid in sids for t in ts]
        buckets = pa.table(
            {
                "ts_ms": [r[0] for r in rows],
                "series_id": [r[1] for r in rows],
                "avg": [v] * len(rows),
                "min": [v] * len(rows),
                "max": [v] * len(rows),
                "count": [1] * len(rows),
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


def fake_factory(spec: SourceSpec) -> FakeSource:
    return FakeSource(name=spec.name, identity=f"fake|{spec.url}")


def make_service(
    tmp_path, source=None, clock=lambda: NOW, factory=fake_factory
) -> TelemetryService:
    source = source or FakeSource()
    con = open_duckdb(tmp_path / "series.duckdb")
    wcon = open_workspace_db(tmp_path / "workspace.db")
    workspace = WorkspaceStore(wcon, clock=clock)
    datasets = DatasetStore(con, workspace.next_id, clock=clock)
    log = EventLog(wcon, clock=clock)
    objects = ObjectStore(wcon, workspace.next_id, clock=clock)
    sources = SourceRegistry(wcon, factory, clock=clock)
    if source.name == "fake":
        source.name = "default"  # dataset.source is looked up by registry name, as in production
    sources.attach("default", source)
    return TelemetryService(
        sources=sources,
        cache=SeriesCache(con, clock=clock),
        datasets=datasets,
        workspace=workspace,
        log=log,
        ws=WorkspaceService(
            workspace,
            objects,
            datasets,
            log,
            CatalogStore(wcon),
            RelationStore(wcon),
            SampleStore(wcon),
            FamilyStore(wcon),
            clock,
        ),
        clock=clock,
    )
