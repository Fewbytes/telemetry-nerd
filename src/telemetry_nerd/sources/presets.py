"""Ready-made specs for public sources (see docs/superpowers/specs/*public-test-sources.md)."""

from __future__ import annotations

from telemetry_nerd.sources.spec import Politeness, SourceSpec

# Wikimedia's Thanos behind Grafana's anonymous datasource proxy. robots.txt disallows
# crawling, so this is interactive-volume only: one request at a time, spaced out, and a
# long timeout (the server allows 2 min per query).
_WIKIMEDIA_POLITENESS = Politeness(max_concurrency=1, min_interval_ms=1000, timeout_s=90)
WIKIMEDIA = SourceSpec(
    name="wikimedia",
    url="https://grafana.wikimedia.org/api/datasources/proxy/uid/000000026",
    flavor="prometheus",
    politeness=_WIKIMEDIA_POLITENESS,
    profile_source="wikimedia-1h",
)
# The same Thanos through its 1h-downsampled datasource: 90d x 1h answers in < 1s, so long-window
# operating profiles read from here (bead 2as.7).
WIKIMEDIA_1H = SourceSpec(
    name="wikimedia-1h",
    url="https://grafana.wikimedia.org/api/datasources/proxy/uid/PA7DE9A562EF40E24",
    flavor="prometheus",
    resolution_ms=3_600_000,
    politeness=_WIKIMEDIA_POLITENESS,
)

# Grafana Play: hosted OpenTelemetry demo (native histograms: traces_spanmetrics_latency,
# classic: http_server_request_duration_seconds_bucket). Public demo: stay polite.
PLAY = SourceSpec(
    name="play",
    url="https://play.grafana.org/api/datasources/proxy/uid/grafanacloud-prom",
    flavor="prometheus",
    resolution_ms=20_000,
    politeness=Politeness(max_concurrency=1, min_interval_ms=1000, timeout_s=60),
)

PRESETS: dict[str, SourceSpec] = {
    "wikimedia": WIKIMEDIA,
    "wikimedia-1h": WIKIMEDIA_1H,
    "play": PLAY,
}
