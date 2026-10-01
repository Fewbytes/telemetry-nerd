# Source.discover() (2as.2)

`Source.discover() -> Discovery` and `Source.scrape_interval(selector) -> int | None`
(spec §4.1). `Discovery` (model/discovery.py) is one **origin** (`source-metadata`) of metric
knowledge; the catalog (2as.3) also takes source code / collector docs / knowledge packs
(see the external-context bead) and must not treat Discovery as the only truth.

## PromQLSource.discover()
No per-metric loops. Calls: `/label/__name__/values` (fatal on failure; capped at
`Limits.max_metrics` -> `metrics_truncated` + partial), `/metadata` (retried 3x and unioned:
Thanos is non-deterministic; `metadata_coverage:NN%` caveat; missing = unknown type, never
"untyped"; `metadata_unavailable` if all fail), `/labels`, `/status/tsdb` (top-N cardinality;
`cardinality_unavailable` or `cardinality_top_only`). Optional steps degrade to caveats and
`partial=True`. `Limits.discover_timeout_s` (120s) applies to these listings.

Histogram families: classic = `_bucket` with `_sum` and `_count`; native = metadata type
histogram with no `_bucket` series (use histogram_quantile on the base name).

## scrape_interval
Lazy, per metric (resolution varies per job): `selector[10m]` for one series (`limit=1`),
median sample spacing; None under 3 samples.

## Not included
MCP exposure and catalog writes (2as.3); vmrange detection (needs per-histogram series call);
committed Wikimedia recordings (8.7 MB names listing; fixtures belong to 1h9.6).
