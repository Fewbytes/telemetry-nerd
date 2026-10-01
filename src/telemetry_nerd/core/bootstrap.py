from __future__ import annotations

from telemetry_nerd.catalog.store import CatalogStore
from telemetry_nerd.config import Settings
from telemetry_nerd.core.events import EventLog
from telemetry_nerd.core.service import TelemetryService
from telemetry_nerd.core.workspace_service import WorkspaceService
from telemetry_nerd.datasets.cache import SeriesCache
from telemetry_nerd.datasets.db import open_duckdb
from telemetry_nerd.datasets.store import DatasetStore
from telemetry_nerd.sources.promql import PromQLSource
from telemetry_nerd.sources.registry import SourceRegistry
from telemetry_nerd.sources.spec import SourceSpec
from telemetry_nerd.workspace.db import open_workspace_db
from telemetry_nerd.workspace.objects import ObjectStore
from telemetry_nerd.workspace.store import WorkspaceStore


def build_service(settings: Settings) -> TelemetryService:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    con = open_duckdb(settings.data_dir / "series.duckdb")
    wcon = open_workspace_db(settings.data_dir / "workspace.db")
    workspace = WorkspaceStore(wcon)
    log = EventLog(wcon)
    datasets = DatasetStore(con, workspace.next_id)
    objects = ObjectStore(wcon, workspace.next_id)
    sources = SourceRegistry(wcon, PromQLSource.from_spec)
    sources.load()
    # `default` is owned by daemon settings: rebuilt from them on every start, never persisted.
    default = SourceSpec(
        name="default",
        url=settings.source_url,
        flavor=settings.source_flavor,  # type: ignore[arg-type]
        resolution_ms=settings.resolution_ms,
    )
    sources.attach("default", PromQLSource.from_spec(default), default)
    return TelemetryService(
        sources=sources,
        cache=SeriesCache(con),
        datasets=datasets,
        workspace=workspace,
        log=log,
        ws=WorkspaceService(workspace, objects, datasets, log, CatalogStore(wcon)),
    )
