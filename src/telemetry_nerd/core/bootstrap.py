from __future__ import annotations

from telemetry_nerd.catalog.relation_store import RelationStore
from telemetry_nerd.catalog.sample_store import SampleStore
from telemetry_nerd.catalog.store import CatalogStore, FamilyStore
from telemetry_nerd.config import Settings
from telemetry_nerd.core.events import EventLog
from telemetry_nerd.core.service import TelemetryService
from telemetry_nerd.core.workspace_service import WorkspaceService
from telemetry_nerd.datasets.cache import SeriesCache
from telemetry_nerd.datasets.db import open_duckdb
from telemetry_nerd.datasets.store import DatasetStore
from telemetry_nerd.exchange.run import runs_root
from telemetry_nerd.kernels.manager import KernelConfig, KernelManager
from telemetry_nerd.model.time import now_ms
from telemetry_nerd.sources.base import Source
from telemetry_nerd.sources.elasticsearch import ElasticsearchSource
from telemetry_nerd.sources.promql import PromQLSource
from telemetry_nerd.sources.registry import SourceRegistry
from telemetry_nerd.sources.spec import ES_FLAVORS, SourceSpec
from telemetry_nerd.workspace.db import open_workspace_db
from telemetry_nerd.workspace.objects import ObjectStore
from telemetry_nerd.workspace.registry import WorkspaceRegistry
from telemetry_nerd.workspace.scope import ActiveWorkspace
from telemetry_nerd.workspace.store import WorkspaceStore


def source_factory(spec: SourceSpec) -> Source:
    """The live adapter for a spec (resolves its secret reference now: raises MissingSecret)."""
    if spec.flavor in ES_FLAVORS:
        return ElasticsearchSource.from_spec(spec)
    return PromQLSource.from_spec(spec)


def build_service(settings: Settings) -> TelemetryService:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    con = open_duckdb(settings.data_dir / "series.duckdb")
    wcon = open_workspace_db(settings.data_dir / "workspace.db")
    # one scope for every workspace store (spec D4); the registry's ids share the counters
    registry = WorkspaceRegistry(wcon, lambda prefix: workspace.next_id(prefix), now_ms)
    active = ActiveWorkspace(registry.active_id())
    workspace = WorkspaceStore(wcon, scope=active, registry=registry)
    log = EventLog(wcon, scope=active)
    datasets = DatasetStore(con, workspace.next_id)
    objects = ObjectStore(wcon, workspace.next_id, scope=active)
    sources = SourceRegistry(wcon, source_factory)
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
        ws=WorkspaceService(
            workspace,
            objects,
            datasets,
            log,
            CatalogStore(wcon),
            RelationStore(wcon),
            SampleStore(wcon),
            FamilyStore(wcon),
        ),
        active=active,
        registry=registry,
        auto_profile=True,
        kernels=KernelManager(KernelConfig.from_settings(settings)),
        runs_root=runs_root(settings.data_dir),
    )
