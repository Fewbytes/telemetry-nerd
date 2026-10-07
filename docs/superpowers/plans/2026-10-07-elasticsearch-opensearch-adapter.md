# Elasticsearch / OpenSearch Source Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect Elasticsearch and OpenSearch clusters directly as sources whose `expr` is the backend's native Query DSL, producing the same `FetchResult` / `DistResult` every PromQL source produces, so the existing analysis tools, distributions and Little's law work on access-log indices.

**Architecture:** A pure parser/request builder (`sources/esquery.py: EsQuery`) validates the caller's request body and grafts its aggregation under an adapter-built `date_histogram`; `sources/elasticsearch.py: ElasticsearchSource` implements the `Source` protocol over one index pattern (search, `_field_caps` field checks and discovery, flavor-aware probe, the full error mapping). A new `Source.query_language` (`"promql" | "es_dsl"`) attribute lets the service, `DatasetMeta`, profiles and `check_littles_law` dispatch instead of assuming PromQL; distributions gain a query-chosen `"linear"` bucket scheme with lower-inclusive edges.

**Tech Stack:** Python 3.12, httpx (`AsyncClient`, `MockTransport` in tests), pyarrow, pydantic v2, pytest + pytest-asyncio (auto mode), testcontainers (Elasticsearch 8.x, OpenSearch 2.x), TypeScript + vitest (UI text).

**Spec:** `docs/superpowers/specs/2026-10-07-elasticsearch-opensearch-adapter-design.md` (approved). Read it alongside this plan; section names below refer to it.

## Global Constraints

- `expr` for an ES source is the caller's JSON request body, stored and shown **verbatim** (never canonicalized, never rewritten). Keys: only `query` and `aggs` (or `aggregations`, not both). No time bounds and no `date_histogram` in it.
- Query forms: `rate` (no aggs), `field_rate` (`value_count`), `stats`, `percentile` (`percentiles` with exactly one `percents` value), `histogram` (only via `query_distribution`); the first four optionally inside exactly one `terms`. Everything else (`avg`, `sum`, `min`, `max`, `cardinality`, `date_histogram`, `composite`, pipelines, any `script`) is refused with `SourceError` and a hint listing the forms.
- Adapter-owned aggregation names start with `__tn_` (`__tn_time`, `__tn_n`, `__tn_by`); caller names with that prefix are refused.
- A query bucket carries its **end** time: `ts_ms = key + step_ms`. Range filter `gte = rng.start_ms − step_ms`, `lt = rng.end_ms`, `format: epoch_millis`. `date_histogram`: `fixed_interval: "<step_ms>ms"`, `offset: "+<rng.start_ms mod step_ms>ms"`, `min_doc_count: 0`, **no** `extended_bounds`.
- Interior empty buckets of `rate`/`field_rate` are **0 per second** (count 1); leading/trailing empty buckets produce **no rows**. `stats.count == 0` and empty percentiles produce no row.
- Rate forms are **per second** (`doc_count / step_s`); count column 1. `stats` count = `stats.count`. `percentile` count = the adapter's sibling `value_count` (`__tn_n`).
- `sum_other_doc_count > 0` in any query bucket → `LimitExceeded`. More than `Limits.max_series` (500) series → `LimitExceeded`. `MAX_STEPS_PER_QUERY` (11,000) checked before sending.
- Dataset caveats: `zero_is_no_documents` (rate forms), `approximate_percentile` (percentile form), `query_chosen_buckets` (every ES distribution).
- `SourceSpec.flavor ∈ {"prometheus","victoriametrics","elasticsearch","opensearch"}`; ES flavors require `index_pattern` and `time_field` (no default), PromQL flavors forbid them; `profile_source` must be `None` for ES flavors. `index_pattern`: lowercase, ≤ 255 chars, no whitespace or `/ \ " < > | #`, never bare `*` or `_all`.
- `AuthRef.scheme ∈ {"bearer","basic","apikey"}`; `apikey` sends `Authorization: ApiKey <secret>` with the secret **verbatim** (not encoded).
- ES `resolution_ms` = finest query step the source accepts; `None` → 1000 ms (assumed); never learned. `scrape_interval` returns `None`. `semantics` is `None`.
- `identity = f"{flavor}|{url}|{index_pattern}|{time_field}|{resolution_ms}"`.
- Supported versions: Elasticsearch ≥ 7.10, OpenSearch ≥ 1.0. Flavor mismatch is reported (`flavor_mismatch`), not fatal. A 403 on `/` is tolerated.
- ES errors are reported as the first root cause's `type: reason`, never the whole body.
- `BucketScheme("linear", width=..., offset=...)`; edges `[lo, hi)`; `describe()` = `"fixed-width buckets of <width> (offset <offset>), chosen by the query; each [lo, hi)"`; `lower_inclusive` is the single place the `>=` vs `>` decision is made.
- Tests: unit and e2e never touch a real backend (fixtures + `httpx.MockTransport`); `tests/integration` uses containers (`-m integration`, CI-required); `-m network` is non-blocking. No flaky tests (a flake is a P0 bug: root-cause it, never retry it away).
- Text Claude and the user read uses `docs/glossary.md` terms; `tests/unit/test_glossary_terms.py` bans "scrape interval(s)" (unless the line says "scraped"), "source interval", "native resolution", "source resolution", "rate window(s)", "display step" in `mcp/server.py` and `ui/src/**/*.ts|svelte`.
- Gates after every task: `just lint && uv run pytest tests/unit -q`; tasks touching `ui/` also `cd ui && npx vitest run && cd .. && just ui-check`. Run `just fmt` before `just lint`. Line length 100.
- Commit after each task on the task's branch (this project uses one worktree + branch per change, ff-merged to `master`; follow the executing skill's workspace setup). Commit messages: conventional (`feat(es): ...`, `test(es): ...`), ending with the session's attribution trailer.

## File Map

| File | Responsibility | Task |
|---|---|---|
| `src/telemetry_nerd/sources/spec.py` | ES flavors, `index_pattern`, `time_field`, validation; `apikey` auth scheme | 1 |
| `src/telemetry_nerd/sources/esquery.py` (new) | `EsQuery`: parse/validate `expr`, build the request body (pure) | 2 |
| `src/telemetry_nerd/model/distribution.py` | `linear` kind, `width`, `offset`, `lower_inclusive`, `DIST_SCHEMA` comment | 3 |
| `src/telemetry_nerd/analysis/fraction.py` | `comparison()` (`>=` on lower-inclusive schemes) | 3 |
| `src/telemetry_nerd/sources/base.py` | `QueryLanguage`, `Source.query_language`, `language_of()` | 4 |
| `src/telemetry_nerd/sources/promql.py` | `query_language = "promql"` | 4 |
| `src/telemetry_nerd/model/discovery.py` | `Discovery.naming` | 4 |
| `src/telemetry_nerd/datasets/store.py` | `DatasetMeta.query_language`; `put(..., caveats, query_language)`; `put_distribution(..., query_language)` | 4 |
| `src/telemetry_nerd/sources/elasticsearch.py` (new) | `ElasticsearchSource`: transport, errors, probe (5), fetch (6), histogram (7), discover (8) | 5–8 |
| `tests/unit/es_fake.py` (new), `tests/fixtures/elasticsearch/*.json` (new) | MockTransport cluster + hand-written responses | 5–8 |
| `src/telemetry_nerd/core/workspace_service.py`, `src/telemetry_nerd/catalog/rules.py` | `catalog_learn` with `naming="fields"`; `"b"` unit alias | 9 |
| `src/telemetry_nerd/core/bootstrap.py`, `src/telemetry_nerd/mcp/server.py` (`source_connect`), `src/telemetry_nerd/sources/grafana.py`, `pyproject.toml` | factory dispatch, MCP wiring, Grafana hint, marker text | 10 |
| `src/telemetry_nerd/core/service.py`, `src/telemetry_nerd/core/summary.py`, `src/telemetry_nerd/mcp/server.py` (`query`, `query_distribution`, `fraction_over`) | `query`/`query_distribution` dispatch, summary wording, `compare` | 11 |
| `src/telemetry_nerd/core/service.py`, `src/telemetry_nerd/core/profiles.py` | PromQL-only features on ES datasets (show, show_auto, units, profiles, overlays, analyze) | 12 |
| `src/telemetry_nerd/core/littles_ops.py`, `src/telemetry_nerd/core/service.py`, `src/telemetry_nerd/mcp/server.py` (`check_littles_law`), `tests/unit/littles_sim.py` | cross-source Little's law | 13 |
| `ui/src/lib/api.ts`, `ui/src/lib/panelNotes.ts` | `BucketSchemeInfo.width/offset`; linear-scheme and CCDF wording; new caveat texts | 14 |
| `tests/integration/conftest.py`, `tests/integration/es_seed.py` (new), `tests/integration/test_es_containers.py` (new) | ES 8.x + OpenSearch 2.x containers, synthetic ECS index, end to end | 15 |
| `scripts/cern_es_probe.py` (new), `tests/integration/test_es_cern_network.py` (new, conditional) | best-effort live CERN tier | 16 |

---

### Task 1: `SourceSpec` / `AuthRef` for Elasticsearch and OpenSearch

**Files:**
- Modify: `src/telemetry_nerd/sources/spec.py`
- Test: `tests/unit/test_source_spec.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `ES_FLAVORS: frozenset[str]` (`{"elasticsearch", "opensearch"}`) in `sources/spec.py`; `SourceSpec.flavor: Literal["prometheus","victoriametrics","elasticsearch","opensearch"]`; `SourceSpec.index_pattern: str | None`; `SourceSpec.time_field: str | None`; `AuthRef.scheme: Literal["bearer","basic","apikey"]`; `AuthRef.headers()` returns `{"Authorization": "ApiKey <secret>"}` for `apikey`.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_source_spec.py`)

```python
ES = "https://es.example:9200"


def es_spec(**kw):
    base = {"name": "logs", "url": ES, "flavor": "elasticsearch",
            "index_pattern": "access-logs-*", "time_field": "@timestamp"}
    return SourceSpec.model_validate({**base, **kw})


@pytest.mark.parametrize("flavor", ["elasticsearch", "opensearch"])
def test_es_flavors_take_index_pattern_and_time_field(flavor):
    spec = es_spec(flavor=flavor)
    assert (spec.flavor, spec.index_pattern, spec.time_field) == (
        flavor, "access-logs-*", "@timestamp"
    )
    assert spec.public()["index_pattern"] == "access-logs-*"


@pytest.mark.parametrize("missing", ["index_pattern", "time_field"])
def test_es_flavors_require_both_fields(missing):
    with pytest.raises(ValidationError, match=missing):
        es_spec(**{missing: None})


def test_promql_flavors_refuse_es_fields():
    with pytest.raises(ValidationError, match="elasticsearch/opensearch only"):
        SourceSpec(name="p", url=GRAFANA, index_pattern="x-*")
    with pytest.raises(ValidationError, match="elasticsearch/opensearch only"):
        SourceSpec(name="p", url=GRAFANA, time_field="@timestamp")


def test_es_flavors_refuse_profile_source():
    with pytest.raises(ValidationError, match="PromQL-only"):
        es_spec(profile_source="other")


@pytest.mark.parametrize(
    "pattern",
    ["*", "_all", "logs-*,*", "Access-*", "has space", "a/b", 'a"b', "a<b", "a|b", "a#b",
     "x" * 256, ""],
)
def test_index_pattern_rules(pattern):
    with pytest.raises(ValidationError):
        es_spec(index_pattern=pattern)


@pytest.mark.parametrize("pattern", ["access-logs-*", "logs-a,logs-b", "esnet_*", "a" * 255])
def test_index_pattern_accepts_names_lists_and_wildcards(pattern):
    assert es_spec(index_pattern=pattern).index_pattern == pattern


@pytest.mark.parametrize("field", ["", " ", "has space"])
def test_time_field_must_be_a_field_path(field):
    with pytest.raises(ValidationError):
        es_spec(time_field=field)


def test_apikey_scheme_sends_the_encoded_key_verbatim():
    ref = AuthRef(env="ES_API_KEY", scheme="apikey")
    assert ref.headers({"ES_API_KEY": "VnVhQ2ZHY0JDZGJrUW0tZTVhT3g6dWkybHAyYXhUTm1zeWFrdzl0dk5udw=="}) == {
        "Authorization": "ApiKey VnVhQ2ZHY0JDZGJrUW0tZTVhT3g6dWkybHAyYXhUTm1zeWFrdzl0dk5udw=="
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_source_spec.py -q`
Expected: FAIL (`flavor` literal rejects `elasticsearch`; `index_pattern` is an extra field; `apikey` not a valid scheme).

- [ ] **Step 3: Implement**

In `src/telemetry_nerd/sources/spec.py`:

```python
ES_FLAVORS = frozenset({"elasticsearch", "opensearch"})
_INDEX_FORBIDDEN = re.compile(r'[\s/\\"<>|#]')
```

`AuthRef`:

```python
    scheme: Literal["bearer", "basic", "apikey"] = "bearer"
```

and in `AuthRef.headers`, before the `bearer` return:

```python
        if self.scheme == "apikey":
            # the encoded key Elasticsearch hands out (base64 of id:api_key), used as is
            return {"Authorization": f"ApiKey {secret}"}
```

`SourceSpec` (fields after `timezone`, validators next to the existing ones):

```python
    flavor: Literal["prometheus", "victoriametrics", "elasticsearch", "opensearch"] = "prometheus"
    ...
    #: Elasticsearch/OpenSearch: the index pattern this source reads (one source = one pattern)
    index_pattern: str | None = None
    #: Elasticsearch/OpenSearch: the date field documents are bucketed by; no default (indices
    #: use @timestamp, timestamp, metadata.timestamp, ...: any guess is wrong somewhere)
    time_field: str | None = None

    @field_validator("index_pattern")
    @classmethod
    def _index_pattern(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not 1 <= len(v) <= 255:
            raise ValueError("index_pattern must be 1-255 characters")
        if v != v.lower():
            raise ValueError("index_pattern must be lowercase (index names are)")
        if _INDEX_FORBIDDEN.search(v):
            raise ValueError('index_pattern must not contain whitespace or / \\ " < > | #')
        if any(part in ("*", "_all") for part in v.split(",")):
            raise ValueError(
                "index_pattern '*' / '_all' includes system indices: name the pattern, "
                "e.g. access-logs-*"
            )
        return v

    @field_validator("time_field")
    @classmethod
    def _time_field(cls, v: str | None) -> str | None:
        if v is not None and (not v or re.search(r"\s", v)):
            raise ValueError("time_field must be a field path like @timestamp")
        return v

    @model_validator(mode="after")
    def _flavor_fields(self) -> SourceSpec:
        if self.flavor in ES_FLAVORS:
            missing = [f for f in ("index_pattern", "time_field") if getattr(self, f) is None]
            if missing:
                raise ValueError(
                    f"flavor {self.flavor} needs {' and '.join(missing)} (e.g. "
                    "index_pattern='access-logs-*', time_field='@timestamp'; there is no default "
                    "time field)"
                )
            if self.profile_source is not None:
                raise ValueError(
                    "profile_source is PromQL-only: operating profiles are not available on "
                    "Elasticsearch/OpenSearch sources"
                )
        elif self.index_pattern is not None or self.time_field is not None:
            raise ValueError("index_pattern and time_field are for flavor elasticsearch/opensearch only")
        return self
```

In `PromQLSource.from_spec` (`sources/promql.py`) nothing changes: the factory (Task 10) only passes PromQL flavors to it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_source_spec.py -q`
Expected: PASS.

- [ ] **Step 5: Gates and commit**

```bash
just fmt && just lint && uv run pytest tests/unit -q
git add src/telemetry_nerd/sources/spec.py tests/unit/test_source_spec.py
git commit -m "feat(es): SourceSpec flavors, index_pattern/time_field and apikey auth"
```

---

### Task 2: `EsQuery` — parse, validate and build the request (pure)

**Files:**
- Create: `src/telemetry_nerd/sources/esquery.py`
- Test: `tests/unit/test_esquery.py`

**Interfaces:**
- Consumes: `TimeRange` (`model/time.py`), `SourceError` (`sources/base.py`).
- Produces (used by Tasks 6, 7, 11, 12, 13):
  - constants `TIME_AGG = "__tn_time"`, `COUNT_AGG = "__tn_n"`, `GROUP_AGG = "__tn_by"`, `RESERVED_PREFIX = "__tn_"`, `FORMS_HINT: str`
  - `Form = Literal["rate", "field_rate", "stats", "percentile", "histogram"]`
  - `@dataclass(frozen=True) class EsQuery` with fields `query: dict`, `form: Form`, `metric_name: str | None`, `metric: dict | None` (`{kind: params}`), `field: str | None`, `group_name: str | None`, `group: dict | None` (the terms params), `group_field: str | None`, `q: float | None` (percentile as a fraction), `interval: float | None`, `offset: float`
  - `EsQuery.parse(expr: str) -> EsQuery` (raises `SourceError` with `hint=FORMS_HINT`)
  - `EsQuery.body(rng: TimeRange, step_ms: int, time_field: str, timeout_s: float) -> dict`
  - `EsQuery.with_group(field: str, size: int) -> EsQuery`
  - `EsQuery.fields() -> list[tuple[str, Literal["numeric", "aggregatable"]]]`

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_esquery.py`)

```python
"""EsQuery: the Elasticsearch expr contract (parse, validate, build the request; no I/O)."""

import json

import pytest

from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.base import SourceError
from telemetry_nerd.sources.esquery import COUNT_AGG, GROUP_AGG, TIME_AGG, EsQuery

QS = {"query_string": {"query": "service.name:checkout"}}
START = 1_700_000_040_000  # a multiple of 60 s
RNG = TimeRange(START, START + 240_000)
STATS = {"lat": {"stats": {"field": "event.duration"}}}


def expr(**doc) -> str:
    return json.dumps(doc)


def test_query_without_aggs_is_the_rate_form():
    q = EsQuery.parse(expr(query=QS))
    assert (q.form, q.query, q.field, q.group) == ("rate", QS, None, None)


def test_absent_query_means_all_documents():
    assert EsQuery.parse("{}").query == {"match_all": {}}


@pytest.mark.parametrize(
    ("agg", "form"),
    [
        ({"n": {"value_count": {"field": "event.duration"}}}, "field_rate"),
        (STATS, "stats"),
        ({"p": {"percentiles": {"field": "event.duration", "percents": [99]}}}, "percentile"),
        ({"h": {"histogram": {"field": "event.duration", "interval": 25}}}, "histogram"),
    ],
)
def test_metric_forms(agg, form):
    q = EsQuery.parse(expr(query=QS, aggs=agg))
    assert (q.form, q.field, q.metric_name, q.group) == (form, "event.duration", next(iter(agg)), None)
    assert q.metric == next(iter(agg.values()))


def test_aggregations_is_the_es_synonym_of_aggs():
    assert EsQuery.parse(expr(aggregations=STATS)).form == "stats"


@pytest.mark.parametrize(("percents", "q"), [([99], 0.99), ([50], 0.5), ([99.9], 0.999)])
def test_percentile_q_is_a_fraction(percents, q):
    p = EsQuery.parse(expr(aggs={"p": {"percentiles": {"field": "x", "percents": percents}}}))
    assert p.q == pytest.approx(q)


def test_histogram_interval_and_offset():
    h = EsQuery.parse(expr(aggs={"h": {"histogram": {"field": "x", "interval": 25, "offset": 5}}}))
    assert (h.interval, h.offset) == (25.0, 5.0)
    plain = EsQuery.parse(expr(aggs={"h": {"histogram": {"field": "x", "interval": 25}}}))
    assert plain.offset == 0.0


def test_terms_without_sub_aggregation_is_the_grouped_rate_form():
    q = EsQuery.parse(expr(aggs={"by_status": {"terms": {"field": "http.response.status_code"}}}))
    assert (q.form, q.group_name, q.group_field, q.metric) == (
        "rate", "by_status", "http.response.status_code", None
    )


def test_terms_wrapping_a_metric_groups_it():
    q = EsQuery.parse(expr(aggs={"by_status": {
        "terms": {"field": "http.response.status_code", "size": 50}, "aggs": STATS}}))
    assert (q.form, q.metric_name, q.field) == ("stats", "lat", "event.duration")
    assert (q.group_name, q.group, q.group_field) == (
        "by_status", {"field": "http.response.status_code", "size": 50}, "http.response.status_code"
    )


def _terms(sub):
    return {"t": {"terms": {"field": "s"}, "aggs": sub}}


REFUSALS = [
    ("not json", "not JSON"),
    ("[1, 2]", "JSON object"),
    (expr(query=QS, size=0), "unexpected keys"),
    (expr(aggs=STATS, aggregations=STATS), "not both"),
    (expr(query="service:x"), "query must be a JSON object"),
    (expr(aggs={}), "exactly one"),
    (expr(aggs={"a": {"stats": {"field": "x"}}, "b": {"stats": {"field": "y"}}}), "exactly one"),
    (expr(aggs={"a": {"avg": {"field": "x"}}}), "avg is not supported"),
    (expr(aggs={"a": {"date_histogram": {"field": "@timestamp"}}}), "date_histogram is not supported"),
    (expr(aggs={"a": {"stats": {"script": {"source": "doc['x'].value"}}}}), "scripts"),
    (expr(aggs={"a": {"stats": {}}}), "needs a field"),
    (expr(aggs={"a": {"terms": {"size": 5}}}), "needs a field"),
    (expr(aggs={"p": {"percentiles": {"field": "x", "percents": [50, 99]}}}), "exactly one percents"),
    (expr(aggs={"p": {"percentiles": {"field": "x"}}}), "exactly one percents"),
    (expr(aggs={"p": {"percentiles": {"field": "x", "percents": [100]}}}), "exactly one percents"),
    (expr(aggs={"__tn_x": {"stats": {"field": "x"}}}), "reserved"),
    (expr(aggs=_terms({"__tn_n": {"stats": {"field": "x"}}})), "reserved"),
    (expr(aggs=_terms({"u": {"terms": {"field": "v"}}})), "one terms level"),
    (expr(aggs=_terms({"h": {"histogram": {"field": "x", "interval": 5}}})), "query_distribution's by"),
    (expr(aggs=_terms({"l": {"stats": {"field": "x"}, "aggs": STATS}})), "nest at most"),
    (expr(aggs={"l": {"stats": {"field": "x"}, "aggs": STATS}}), "takes no sub-aggregations"),
    (expr(aggs={"h": {"histogram": {"field": "x"}}}), "positive numeric interval"),
    (expr(aggs={"h": {"histogram": {"field": "x", "interval": 5, "keyed": True}}}), "keyed"),
]


@pytest.mark.parametrize(("text", "match"), REFUSALS)
def test_unsupported_shapes_are_refused_with_the_accepted_forms(text, match):
    with pytest.raises(SourceError, match=match) as e:
        EsQuery.parse(text)
    assert "value_count" in e.value.hint and "percentiles" in e.value.hint


def test_body_grafts_the_callers_aggregation_under_the_time_buckets():
    q = EsQuery.parse(expr(query=QS, aggs=STATS))
    assert q.body(RNG, 60_000, "@timestamp", 30.0) == {
        "size": 0,
        "track_total_hits": False,
        "timeout": "30000ms",
        "query": {"bool": {"filter": [
            QS,
            {"range": {"@timestamp": {
                "gte": START - 60_000, "lt": START + 240_000, "format": "epoch_millis"}}},
        ]}},
        "aggs": {TIME_AGG: {
            "date_histogram": {"field": "@timestamp", "fixed_interval": "60000ms",
                               "offset": "+0ms", "min_doc_count": 0},
            "aggs": STATS,
        }},
    }


def test_rate_form_sends_no_sub_aggregation():
    body = EsQuery.parse(expr(query=QS)).body(RNG, 60_000, "@timestamp", 30.0)
    assert "aggs" not in body["aggs"][TIME_AGG]


def test_offset_aligns_the_interval_grid_to_the_range():
    body = EsQuery.parse("{}").body(TimeRange(START + 15_000, START + 75_000), 60_000, "ts", 30.0)
    assert body["aggs"][TIME_AGG]["date_histogram"]["offset"] == "+15000ms"


def test_percentile_adds_a_value_count_sibling_for_the_count():
    p = EsQuery.parse(expr(aggs={"p": {"percentiles": {"field": "event.duration", "percents": [99]}}}))
    inner = p.body(RNG, 60_000, "@timestamp", 30.0)["aggs"][TIME_AGG]["aggs"]
    assert inner == {
        "p": {"percentiles": {"field": "event.duration", "percents": [99]}},
        COUNT_AGG: {"value_count": {"field": "event.duration"}},
    }


def test_grouped_form_nests_the_metric_under_terms():
    q = EsQuery.parse(expr(aggs={"by_status": {
        "terms": {"field": "http.response.status_code", "size": 50}, "aggs": STATS}}))
    inner = q.body(RNG, 60_000, "@timestamp", 30.0)["aggs"][TIME_AGG]["aggs"]
    assert inner == {"by_status": {
        "terms": {"field": "http.response.status_code", "size": 50}, "aggs": STATS}}


def test_with_group_builds_the_terms_level_from_by():
    h = EsQuery.parse(expr(aggs={"h": {"histogram": {"field": "x", "interval": 5}}}))
    g = h.with_group("service.name", 501)
    assert (g.group_name, g.group, g.group_field) == (
        GROUP_AGG, {"field": "service.name", "size": 501}, "service.name"
    )
    assert g.form == "histogram" and h.group is None  # frozen: a new query


def test_fields_names_every_aggregated_field_with_what_it_needs():
    grouped = EsQuery.parse(expr(aggs={"t": {"terms": {"field": "code"}, "aggs": STATS}}))
    assert grouped.fields() == [("event.duration", "numeric"), ("code", "aggregatable")]
    count = EsQuery.parse(expr(aggs={"n": {"value_count": {"field": "user.id"}}}))
    assert count.fields() == [("user.id", "aggregatable")]
    assert EsQuery.parse(expr(query=QS)).fields() == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_esquery.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'telemetry_nerd.sources.esquery'`.

- [ ] **Step 3: Implement** (`src/telemetry_nerd/sources/esquery.py`)

```python
"""Elasticsearch / OpenSearch `expr`: parse, validate, build the request (pure, no I/O).

`expr` is the caller's native request body: a `query` clause plus at most one metric
aggregation, optionally inside one `terms` grouping. Time never appears in it: the adapter wraps
the aggregation in a `date_histogram` built from the time range and query step, as the PromQL
adapter adds start/end/step. Design:
docs/superpowers/specs/2026-10-07-elasticsearch-opensearch-adapter-design.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Literal

from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.base import SourceError

Form = Literal["rate", "field_rate", "stats", "percentile", "histogram"]
Need = Literal["numeric", "aggregatable"]

RESERVED_PREFIX = "__tn_"
TIME_AGG = "__tn_time"  # the adapter's date_histogram
COUNT_AGG = "__tn_n"  # the adapter's value_count beside a percentiles aggregation
GROUP_AGG = "__tn_by"  # the adapter's terms built from query_distribution's `by`
_METRIC_FORMS: dict[str, Form] = {
    "value_count": "field_rate",
    "stats": "stats",
    "percentiles": "percentile",
    "histogram": "histogram",
}
_KINDS = frozenset({"terms", *_METRIC_FORMS})

FORMS_HINT = (
    'accepted: {"query": {...}} alone (documents per second); or "aggs" with ONE of '
    "value_count (a field's values per second), stats (mean/min/max/count of a numeric field), "
    'percentiles with exactly one "percents" value; optionally inside one terms aggregation '
    "(one series per term). histogram only in query_distribution. No time range or "
    "date_histogram in expr: time is start/end/step"
)


def _refuse(message: str) -> SourceError:
    return SourceError(f"unsupported Elasticsearch expr: {message}", hint=FORMS_HINT)


def _one(aggs: object) -> tuple[str, dict]:
    if not isinstance(aggs, dict) or len(aggs) != 1:
        raise _refuse("aggs must hold exactly one aggregation")
    [(name, body)] = aggs.items()
    if str(name).startswith(RESERVED_PREFIX):
        raise _refuse(f"aggregation names starting with {RESERVED_PREFIX} are reserved ({name})")
    if not isinstance(body, dict):
        raise _refuse(f"{name}: an aggregation is a JSON object")
    return str(name), body


def _agg(name: str, body: dict) -> tuple[str, dict, object | None]:
    """(aggregation type, its params, its sub-aggregations or None)."""
    if "aggs" in body and "aggregations" in body:
        raise _refuse(f"{name}: give aggs or aggregations, not both")
    sub = body.get("aggs", body.get("aggregations"))
    kinds = [k for k in body if k not in ("aggs", "aggregations", "meta")]
    if len(kinds) != 1:
        raise _refuse(f"{name}: expected one aggregation type, got {kinds}")
    kind = kinds[0]
    if kind not in _KINDS:
        raise _refuse(f"{name}: {kind} is not supported in v1")
    params = body[kind]
    if not isinstance(params, dict):
        raise _refuse(f"{name}: {kind} takes a JSON object")
    if "script" in params:
        raise _refuse(f"{name}: scripts are not supported")
    return kind, params, sub


def _field(name: str, kind: str, params: dict) -> str:
    field = params.get("field")
    if not isinstance(field, str) or not field:
        raise _refuse(f"{name}: {kind} needs a field")
    return field


def _number(x: object) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool)


@dataclass(frozen=True)
class EsQuery:
    query: dict
    form: Form
    metric_name: str | None = None
    metric: dict | None = None  # {kind: params}, sent as written
    field: str | None = None
    group_name: str | None = None
    group: dict | None = None  # the terms params, sent as written
    group_field: str | None = None
    q: float | None = None  # percentile form: percents[0] / 100
    interval: float | None = None  # histogram form
    offset: float = 0.0  # histogram form

    @classmethod
    def parse(cls, expr: str) -> EsQuery:
        try:
            doc = json.loads(expr)
        except (TypeError, ValueError) as e:
            raise _refuse(f"expr is not JSON ({e})") from e
        if not isinstance(doc, dict):
            raise _refuse("expr must be a JSON object with query and/or aggs")
        extra = sorted(set(doc) - {"query", "aggs", "aggregations"})
        if extra:
            raise _refuse(f"unexpected keys {extra}: expr holds only query and aggs")
        if "aggs" in doc and "aggregations" in doc:
            raise _refuse("give aggs or aggregations, not both")
        query = doc.get("query", {"match_all": {}})
        if not isinstance(query, dict):
            raise _refuse("query must be a JSON object (Query DSL)")
        if "aggs" not in doc and "aggregations" not in doc:
            return cls(query=query, form="rate")
        name, body = _one(doc.get("aggs", doc.get("aggregations")))
        kind, params, sub = _agg(name, body)
        if kind != "terms":
            if sub is not None:
                raise _refuse(f"{name}: a {kind} aggregation takes no sub-aggregations")
            return cls._metric(query, name, kind, params, None)
        group = (name, params, _field(name, kind, params))
        if sub is None:
            return cls(query=query, form="rate", group_name=name, group=params,
                       group_field=group[2])  # fmt: skip
        mname, mbody = _one(sub)
        mkind, mparams, msub = _agg(mname, mbody)
        if mkind == "terms":
            raise _refuse(f"{mname}: one terms level only (multi-field grouping is later work)")
        if mkind == "histogram":
            raise _refuse(f"{mname}: group a histogram with query_distribution's by, not terms")
        if msub is not None:
            raise _refuse(f"{mname}: aggregations nest at most terms -> metric")
        return cls._metric(query, mname, mkind, mparams, group)

    @classmethod
    def _metric(
        cls, query: dict, name: str, kind: str, params: dict, group: tuple[str, dict, str] | None
    ) -> EsQuery:
        field = _field(name, kind, params)
        grouping = (
            {"group_name": group[0], "group": group[1], "group_field": group[2]} if group else {}
        )
        base: dict[str, Any] = {"query": query, "metric_name": name, "metric": {kind: params},
                                "field": field, **grouping}  # fmt: skip
        if kind == "percentiles":
            p = params.get("percents")
            if not (isinstance(p, list) and len(p) == 1 and _number(p[0]) and 0 < p[0] < 100):
                raise _refuse(
                    f"{name}: percentiles needs exactly one percents value in (0, 100), e.g. "
                    "[99]; several percentiles are several queries"
                )
            return cls(form="percentile", q=float(p[0]) / 100, **base)
        if kind == "histogram":
            interval = params.get("interval")
            if not _number(interval) or interval <= 0:
                raise _refuse(f"{name}: histogram needs a positive numeric interval")
            if params.get("keyed") is True:
                raise _refuse(f"{name}: keyed histograms are not supported")
            offset = params.get("offset", 0)
            if not _number(offset):
                raise _refuse(f"{name}: histogram offset must be a number")
            return cls(form="histogram", interval=float(interval), offset=float(offset), **base)
        return cls(form=_METRIC_FORMS[kind], **base)

    def with_group(self, field: str, size: int) -> EsQuery:
        """This query grouped by `field` (query_distribution's `by`): one adapter terms level."""
        return replace(
            self, group_name=GROUP_AGG, group={"field": field, "size": size}, group_field=field
        )

    def fields(self) -> list[tuple[str, Need]]:
        """Every field an aggregation names, with what the field check requires of it."""
        out: list[tuple[str, Need]] = []
        if self.field is not None:
            out.append((self.field, "aggregatable" if self.form == "field_rate" else "numeric"))
        if self.group_field is not None:
            out.append((self.group_field, "aggregatable"))
        return out

    def body(self, rng: TimeRange, step_ms: int, time_field: str, timeout_s: float) -> dict:
        """The request sent: the caller's query and aggregation under a date_histogram of
        `step_ms` buckets whose END times run from rng.start_ms to rng.end_ms."""
        metrics: dict[str, Any] = {}
        if self.metric is not None and self.metric_name is not None:
            metrics[self.metric_name] = self.metric
        if self.form == "percentile":
            metrics[COUNT_AGG] = {"value_count": {"field": self.field}}
        inner: dict[str, Any] = metrics
        if self.group is not None and self.group_name is not None:
            terms: dict[str, Any] = {"terms": self.group}
            if metrics:
                terms["aggs"] = metrics
            inner = {self.group_name: terms}
        time_agg: dict[str, Any] = {
            "date_histogram": {
                "field": time_field,
                "fixed_interval": f"{step_ms}ms",
                "offset": f"+{rng.start_ms % step_ms}ms",
                "min_doc_count": 0,
            }
        }
        if inner:
            time_agg["aggs"] = inner
        window = {"gte": rng.start_ms - step_ms, "lt": rng.end_ms, "format": "epoch_millis"}
        return {
            "size": 0,
            "track_total_hits": False,
            "timeout": f"{round(timeout_s * 1000)}ms",
            "query": {"bool": {"filter": [self.query, {"range": {time_field: window}}]}},
            "aggs": {TIME_AGG: time_agg},
        }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_esquery.py -q`
Expected: PASS.

- [ ] **Step 5: Gates and commit**

```bash
just fmt && just lint && uv run pytest tests/unit -q
git add src/telemetry_nerd/sources/esquery.py tests/unit/test_esquery.py
git commit -m "feat(es): EsQuery parses the native Query DSL expr and builds the bucketed request"
```

---

### Task 3: `linear` bucket scheme and lower-inclusive comparison

**Files:**
- Modify: `src/telemetry_nerd/model/distribution.py`
- Modify: `src/telemetry_nerd/analysis/fraction.py`
- Test: `tests/unit/test_histogram.py` (scheme), `tests/unit/test_fraction.py` (comparison)

**Interfaces:**
- Consumes: nothing new.
- Produces: `SchemeKind` gains `"linear"`; `BucketScheme(kind, edges=(), schema=None, per_decade=None, width: float | None = None, offset: float | None = None)`; `BucketScheme.lower_inclusive -> bool` (property); `to_dict()` carries `width`/`offset`; `from_dict()` reads them. `analysis.fraction.comparison(lower_inclusive: bool) -> str` (`">="` / `">"`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_histogram.py` (it already tests `BucketScheme`; add the import if missing: `from telemetry_nerd.model.distribution import BucketScheme`):

```python
def test_linear_scheme_describes_query_chosen_lower_inclusive_buckets():
    s = BucketScheme("linear", width=25.0, offset=5.0)
    assert s.describe() == "fixed-width buckets of 25 (offset 5), chosen by the query; each [lo, hi)"
    assert s.growth is None and s.edges == () and s.lower_inclusive


def test_linear_scheme_without_offset_says_offset_0():
    assert BucketScheme("linear", width=0.5).describe().startswith(
        "fixed-width buckets of 0.5 (offset 0)"
    )


def test_prometheus_kinds_are_upper_inclusive_and_describe_as_before():
    classic = BucketScheme("classic", (0.1, 1.0))
    assert not classic.lower_inclusive
    assert classic.describe() == "classic le buckets: 0.1, 1"
    assert not BucketScheme("vmrange", per_decade=18).lower_inclusive


def test_linear_scheme_round_trips_through_its_dict():
    s = BucketScheme("linear", width=25.0, offset=5.0)
    d = s.to_dict()
    assert (d["kind"], d["width"], d["offset"], d["description"]) == (
        "linear", 25.0, 5.0, s.describe()
    )
    assert BucketScheme.from_dict(d) == s


def test_a_stored_scheme_without_width_still_loads():
    stored = {"kind": "classic", "edges": [0.1, 1.0], "schema": None, "per_decade": None}
    assert BucketScheme.from_dict(stored) == BucketScheme("classic", (0.1, 1.0))
```

Append to `tests/unit/test_fraction.py`:

```python
from telemetry_nerd.analysis.fraction import comparison


def test_a_bucket_starting_at_x_counts_as_at_or_above_x():
    # linear [lo, hi) buckets: [10, 20) holds values >= 10, so the edge result is P(X >= 10)
    r = fraction_over([0.0, 10.0], [10.0, 20.0], [3.0, 1.0], 10.0)
    assert r is not None and r.exact and r.lo == 0.25


def test_comparison_follows_the_bucket_edge_closure():
    assert comparison(lower_inclusive=True) == ">="
    assert comparison(lower_inclusive=False) == ">"
```

(`fraction_over` is already imported at the top of `test_fraction.py`; if not, add `from telemetry_nerd.analysis.fraction import fraction_over`.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_histogram.py tests/unit/test_fraction.py -q`
Expected: FAIL (`BucketScheme` has no `width`; `comparison` not importable).

- [ ] **Step 3: Implement**

`src/telemetry_nerd/model/distribution.py`:

```python
        ("count", pa.float64()),  # observations in the value bucket over one query step:
        # increase() for Prometheus (fractional when extrapolated), doc_count for Elasticsearch
```

```python
SchemeKind = Literal["none", "classic", "native", "vmrange", "custom", "linear"]


@dataclass(frozen=True)
class BucketScheme:
    kind: SchemeKind
    edges: tuple[float, ...] = ()  # classic/custom: finite edges, ascending
    schema: int | None = None  # native exponential schema: growth 2^(2^-schema)
    per_decade: int | None = None  # vmrange
    width: float | None = None  # linear: bucket width chosen by the query (edges unbounded)
    offset: float | None = None  # linear: edges are offset + k * width

    @property
    def lower_inclusive(self) -> bool:
        """[lo, hi) buckets (Elasticsearch histogram) vs Prometheus' (lo, hi]: a bucket starting
        at x holds values >= x, so counts at an edge read P(X >= x), not P(X > x)."""
        return self.kind == "linear"
```

In `describe()`, first branch:

```python
        if self.kind == "linear" and self.width is not None:
            return (
                f"fixed-width buckets of {self.width:g} (offset {self.offset or 0:g}), chosen by "
                "the query; each [lo, hi)"
            )
```

`to_dict()` adds `"width": self.width, "offset": self.offset,` before `"description"`; `from_dict()`:

```python
        return cls(
            d["kind"], tuple(d.get("edges") or ()), d.get("schema"), d.get("per_decade"),
            d.get("width"), d.get("offset"),
        )  # fmt: skip
```

`src/telemetry_nerd/analysis/fraction.py` — module docstring gains a sentence, and:

```python
"""... (existing text) ...

On lower-inclusive buckets ([lo, hi), the Elasticsearch `linear` scheme) a bucket starting at x
holds values >= x, so the same counting yields P(X >= x); `comparison` names which one it is.
"""


def comparison(lower_inclusive: bool) -> str:
    """The comparison `fraction_over` answers on a scheme: ">=" on [lo, hi) buckets, else ">"."""
    return ">=" if lower_inclusive else ">"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_histogram.py tests/unit/test_fraction.py -q`
Expected: PASS.

- [ ] **Step 5: Gates and commit**

```bash
just fmt && just lint && uv run pytest tests/unit -q
git add src/telemetry_nerd/model/distribution.py src/telemetry_nerd/analysis/fraction.py \
  tests/unit/test_histogram.py tests/unit/test_fraction.py
git commit -m "feat(dist): linear bucket scheme with lower-inclusive edges"
```

---

### Task 4: Protocol and model plumbing (`query_language`, `Discovery.naming`, `DatasetMeta.query_language`)

**Files:**
- Modify: `src/telemetry_nerd/sources/base.py`, `src/telemetry_nerd/sources/promql.py`, `src/telemetry_nerd/model/discovery.py`, `src/telemetry_nerd/datasets/store.py`, `tests/unit/fakes.py`
- Test: `tests/unit/test_sources.py`, `tests/unit/test_dataset_store.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `sources/base.py`: `QueryLanguage = Literal["promql", "es_dsl"]`; `Source.query_language: QueryLanguage`; `def language_of(src: object) -> QueryLanguage` (sources without the attribute, i.e. replay/fake sources, read as `"promql"`).
  - `PromQLSource.query_language = "promql"` (class attribute).
  - `Discovery.naming: Literal["prometheus", "fields"] = "prometheus"` (last field, so positional construction keeps working).
  - `DatasetMeta.query_language: str = "promql"` (rows stored before this change load as `"promql"`); `DatasetStore.put(..., caveats: Sequence[str] = (), query_language: str = "promql")` (caveats join `source_caveats`); `DatasetStore.put_distribution(..., query_language: str = "promql")`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_sources.py`:

```python
from telemetry_nerd.model.discovery import Discovery
from telemetry_nerd.sources.base import language_of
from telemetry_nerd.sources.promql import PromQLSource


def test_promql_sources_speak_promql_and_sources_without_the_attribute_default_to_it():
    assert PromQLSource("p", "http://vm.test").query_language == "promql"
    assert language_of(PromQLSource("p", "http://vm.test")) == "promql"
    assert language_of(object()) == "promql"


def test_discovery_naming_defaults_to_prometheus_conventions():
    assert Discovery((), (), {}, None, 1.0, (), False).naming == "prometheus"
```

Append to `tests/unit/test_dataset_store.py`:

```python
import json


def test_query_language_and_extra_caveats_are_recorded(store):
    meta = store.put(
        source="es", expr='{"query": {"match_all": {}}}', rng=TimeRange(60_000, 120_000),
        step_ms=60_000, resolution_ms=1_000, result=result(),
        caveats=["zero_is_no_documents"], query_language="es_dsl",
    )  # fmt: skip
    got = store.meta(meta.id)
    assert got.query_language == "es_dsl"
    assert got.source_caveats == ["zero_is_no_documents"]


def test_datasets_stored_before_query_language_existed_read_as_promql(store):
    meta = store.put(
        source="vm", expr="up", rng=TimeRange(60_000, 120_000), step_ms=60_000,
        resolution_ms=15_000, result=result(),
    )  # fmt: skip
    old = {k: v for k, v in meta.to_dict().items() if k != "query_language"}
    store._con.execute("UPDATE datasets SET meta = $m WHERE id = $id",
                       {"m": json.dumps(old), "id": meta.id})  # fmt: skip
    assert store.meta(meta.id).query_language == "promql"


def test_distribution_records_its_query_language(store):
    from telemetry_nerd.model.distribution import BucketScheme, DistResult, COLUMN_SCHEMA, DIST_SCHEMA

    dist = DistResult(DIST_SCHEMA.empty_table(), COLUMN_SCHEMA.empty_table(),
                      SERIES_SCHEMA.empty_table(), BucketScheme("linear", width=5.0), "{}")  # fmt: skip
    meta = store.put_distribution(
        source="es", rng=TimeRange(60_000, 120_000), step_ms=60_000, resolution_ms=1_000,
        dist=dist, histogram=None, n_min=20, query_language="es_dsl",
    )  # fmt: skip
    assert store.meta(meta.id).query_language == "es_dsl"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_sources.py tests/unit/test_dataset_store.py -q`
Expected: FAIL (`language_of` missing; `put()` has no `caveats`/`query_language`).

- [ ] **Step 3: Implement**

`src/telemetry_nerd/sources/base.py`:

```python
from typing import Literal, Protocol

QueryLanguage = Literal["promql", "es_dsl"]
```

In `class Source(Protocol)` after `semantics`:

```python
    #: the backend's native language `expr` is written in (promql: Prometheus, Thanos, Mimir,
    #: VictoriaMetrics; es_dsl: Elasticsearch/OpenSearch request bodies)
    query_language: QueryLanguage
```

and at module end:

```python
def language_of(src: object) -> QueryLanguage:
    """The source's query language; replay and fake sources that predate it speak PromQL."""
    return getattr(src, "query_language", "promql")
```

`src/telemetry_nerd/sources/promql.py`, first line of `class PromQLSource:` body:

```python
    query_language: Literal["promql"] = "promql"  # MetricsQL is a PromQL superset
```

`src/telemetry_nerd/model/discovery.py`, last field of `Discovery`:

```python
    #: how metric names are formed: Prometheus naming conventions (name rules, families and
    #: knowledge packs apply) or document field paths (Elasticsearch: declared metadata only)
    naming: Literal["prometheus", "fields"] = "prometheus"
```

`src/telemetry_nerd/datasets/store.py`: add `from collections.abc import Callable, Sequence`; in `DatasetMeta` after `fit`:

```python
    #: the language `expr` is written in, so show, notes and analysis never re-parse an
    #: Elasticsearch expr as PromQL (even after the source is disconnected)
    query_language: str = "promql"
```

`put()` gains keyword parameters `caveats: Sequence[str] = ()` and `query_language: str = "promql"`; inside the `DatasetMeta(...)` call add `query_language=query_language,` and change `source_caveats` to:

```python
            source_caveats=_union(
                lineage.caveats if lineage else (),
                caveats,
                [f"source_warning:{n}" for n in result.notes],
            ),
```

`put_distribution()` gains `query_language: str = "promql"` and passes `query_language=query_language` into `DatasetMeta(...)`.

`tests/unit/fakes.py`, `class FakeSource:` body gains `query_language = "promql"` next to `semantics = None`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_sources.py tests/unit/test_dataset_store.py -q`
Expected: PASS.

- [ ] **Step 5: Gates and commit**

```bash
just fmt && just lint && uv run pytest tests/unit -q
git add src/telemetry_nerd/sources/base.py src/telemetry_nerd/sources/promql.py \
  src/telemetry_nerd/model/discovery.py src/telemetry_nerd/datasets/store.py tests/unit/fakes.py \
  tests/unit/test_sources.py tests/unit/test_dataset_store.py
git commit -m "feat(es): Source.query_language, Discovery.naming and DatasetMeta.query_language"
```

---

### Task 5: `ElasticsearchSource` — construction, transport errors and `probe`

**Files:**
- Create: `src/telemetry_nerd/sources/elasticsearch.py`
- Create: `tests/unit/es_fake.py`
- Create: `tests/fixtures/elasticsearch/root_es8.json`, `tests/fixtures/elasticsearch/root_os2.json`
- Test: `tests/unit/test_es_source.py`

**Interfaces:**
- Consumes: `SourceSpec`, `ES_FLAVORS`, `AuthRef` (Task 1); `language_of` contract (Task 4); `Gate` (`sources/gate.py`), `USER_AGENT`, `MAX_STEPS_PER_QUERY` (`sources/promql.py`), `Limits`, `SourceError`, `LimitExceeded`, `SourceUnavailable`.
- Produces (used by Tasks 6–8, 10, 15, 16):
  - `class ElasticsearchSource` with `query_language = "es_dsl"`, `semantics = None`
  - `ElasticsearchSource(name: str, base_url: str, *, index_pattern: str, time_field: str, flavor: Literal["elasticsearch","opensearch"] = "elasticsearch", resolution_ms: int | None = None, limits: Limits = Limits(), client: httpx.AsyncClient | None = None, headers: Mapping[str, str] | None = None, gate: Gate | None = None)`
  - `ElasticsearchSource.from_spec(spec: SourceSpec, environ: Mapping[str, str] = os.environ, client: httpx.AsyncClient | None = None) -> ElasticsearchSource`
  - attributes `name`, `base_url`, `flavor`, `index_pattern`, `time_field`, `resolution_ms`, `resolution_origin` (`"configured" | "assumed"`), `limits`; property `identity`; `resolution_info() -> dict`; `async aclose()`; `async scrape_interval(selector, at_ms=None) -> None`; `async probe() -> dict` (keys `reachable`, `latency_ms`, `distribution`, `version`, optional `flavor_mismatch`, optional `version_unavailable`, `index_pattern`, `time_field`, `indices`)
  - module: `DEFAULT_RESOLUTION_MS = 1_000`, `NUMERIC_TYPES`, `DATE_TYPES`, `LABEL_TYPES`, `_request(...)`, `_raise_for(...)`, `_causes(...)`, `_malformed(...)`, `class _Forbidden(SourceError)`
  - tests: `tests/unit/es_fake.py` with `URL`, `PATTERN`, `START`, `RNG`, `CAPS`, `fixture(name)`, `es_error(status, etype, reason, *, root=None, caused_by=None) -> httpx.Response`, `search_response(buckets, *, timed_out=False, total=1, failed=0, failures=None) -> dict`, `tb(key, doc_count, **aggs) -> dict`, `class FakeEs` (`__call__`, `source(flavor="elasticsearch", **kw)`, `searches()`, `caps_requests()`)

- [ ] **Step 1: Write the fixtures and the test helper**

`tests/fixtures/elasticsearch/root_es8.json`:

```json
{
  "name": "es01",
  "cluster_name": "docker-cluster",
  "cluster_uuid": "3pQ1bq0JQwWq0p1h3j1vZg",
  "version": {
    "number": "8.15.3",
    "build_flavor": "default",
    "build_type": "docker",
    "lucene_version": "9.11.1",
    "minimum_wire_compatibility_version": "7.17.0",
    "minimum_index_compatibility_version": "7.0.0"
  },
  "tagline": "You Know, for Search"
}
```

`tests/fixtures/elasticsearch/root_os2.json`:

```json
{
  "name": "os01",
  "cluster_name": "docker-cluster",
  "cluster_uuid": "Zc2b7m1qR3e8xvM4kY9n0A",
  "version": {
    "distribution": "opensearch",
    "number": "2.17.1",
    "build_type": "tar",
    "lucene_version": "9.11.1",
    "minimum_wire_compatibility_version": "7.10.0",
    "minimum_index_compatibility_version": "7.0.0"
  },
  "tagline": "The OpenSearch Project: https://opensearch.org/"
}
```

`tests/unit/es_fake.py`:

```python
"""An Elasticsearch/OpenSearch cluster for one index pattern, served through httpx.MockTransport,
and hand-written responses (unit tests never touch a real backend)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx

from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.elasticsearch import ElasticsearchSource

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "elasticsearch"
URL = "http://es.test:9200"
PATTERN = "tn-access-*"
START = 1_700_000_040_000  # a multiple of 60 s
RNG = TimeRange(START, START + 240_000)  # query buckets ending START .. START + 240 s


def _agg(kind: str, **extra) -> dict:
    return {kind: {"type": kind, "searchable": True, "aggregatable": True, **extra}}


#: field -> {type: info}, as _field_caps answers for one field
CAPS: dict[str, dict] = {
    "@timestamp": _agg("date"),
    "event.duration": _agg("long", meta={"unit": ["nanos"]}),
    "http.response.status_code": _agg("long"),
    "service.name": _agg("keyword"),
    "url.path": _agg("keyword"),
    "message": {"text": {"type": "text", "searchable": True, "aggregatable": False}},
    "message.keyword": _agg("keyword"),
}


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def es_error(status: int, etype: str, reason: str, *, root: tuple[str, str] | None = None,
             caused_by: dict | None = None) -> httpx.Response:  # fmt: skip
    rtype, rreason = root or (etype, reason)
    err: dict = {"type": etype, "reason": reason,
                 "root_cause": [{"type": rtype, "reason": rreason}]}  # fmt: skip
    if caused_by is not None:
        err["caused_by"] = caused_by
    return httpx.Response(status, json={"error": err, "status": status})


def search_response(buckets: list[dict], *, timed_out: bool = False, total: int = 1,
                    failed: int = 0, failures: list | None = None) -> dict:  # fmt: skip
    shards: dict = {"total": total, "successful": total - failed, "skipped": 0, "failed": failed}
    if failures is not None:
        shards["failures"] = failures
    return {"took": 3, "timed_out": timed_out, "_shards": shards, "hits": {"hits": []},
            "aggregations": {"__tn_time": {"buckets": buckets}}}  # fmt: skip


def tb(key: int, doc_count: int, **aggs) -> dict:
    """One date_histogram bucket (key = bucket START, as Elasticsearch returns it)."""
    return {"key_as_string": str(key), "key": key, "doc_count": doc_count, **aggs}


def _respond(out: dict | httpx.Response) -> httpx.Response:
    return out if isinstance(out, httpx.Response) else httpx.Response(200, json=out)


class FakeEs:
    """`search`: a response dict, an httpx.Response, or a callable(request body) returning one.
    `root`: the `GET /` answer. `caps`: field -> {type: info}. `indices`: what PATTERN matches."""

    def __init__(
        self,
        search: dict | httpx.Response | Callable[[dict], dict | httpx.Response] | None = None,
        root: dict | httpx.Response | None = None,
        caps: dict[str, dict] | None = None,
        indices: tuple[str, ...] = ("tn-access-1",),
    ) -> None:
        self.search = search if search is not None else search_response([])
        self.root = root if root is not None else fixture("root_es8.json")
        self.caps = CAPS if caps is None else caps
        self.indices = list(indices)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/":
            return _respond(self.root)
        if path == f"/{PATTERN}/_field_caps":
            want = request.url.params["fields"]
            fields = dict(self.caps) if want == "*" else {
                k: v for k, v in self.caps.items() if k == want
            }  # fmt: skip
            return httpx.Response(200, json={"indices": self.indices, "fields": fields})
        if path == f"/{PATTERN}/_search":
            body = json.loads(request.content)
            return _respond(self.search(body) if callable(self.search) else self.search)
        return es_error(404, "index_not_found_exception", f"no such index [{path}]")

    def source(self, flavor: str = "elasticsearch", **kw) -> ElasticsearchSource:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self))
        return ElasticsearchSource("es", URL, index_pattern=PATTERN, time_field="@timestamp",
                                   flavor=flavor, client=client, **kw)  # fmt: skip

    def searches(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith("/_search")]

    def caps_requests(self) -> list[str]:
        return [r.url.params["fields"] for r in self.requests if r.url.path.endswith("/_field_caps")]
```

- [ ] **Step 2: Write the failing tests** (`tests/unit/test_es_source.py`)

```python
"""ElasticsearchSource: construction, transport errors and probe (fixtures, MockTransport)."""

import httpx
import pytest

from telemetry_nerd.sources.base import SourceError, SourceUnavailable
from telemetry_nerd.sources.elasticsearch import ElasticsearchSource
from telemetry_nerd.sources.promql import USER_AGENT
from telemetry_nerd.sources.spec import AuthRef, SourceSpec

from .es_fake import CAPS, PATTERN, URL, FakeEs, es_error, fixture


def spec(**kw) -> SourceSpec:
    return SourceSpec.model_validate({"name": "logs", "url": URL, "flavor": "elasticsearch",
                                      "index_pattern": PATTERN, "time_field": "@timestamp", **kw})  # fmt: skip


def test_identity_changes_with_index_pattern_and_time_field():
    a = ElasticsearchSource.from_spec(spec())
    b = ElasticsearchSource.from_spec(spec(time_field="event.created"))
    assert a.identity == f"elasticsearch|{URL}|{PATTERN}|@timestamp|1000"
    assert a.identity != b.identity
    assert a.query_language == "es_dsl" and a.semantics is None


def test_resolution_is_assumed_1s_unless_configured():
    assumed = ElasticsearchSource.from_spec(spec())
    assert (assumed.resolution_ms, assumed.resolution_origin) == (1_000, "assumed")
    assert "finest query step" in assumed.resolution_info()["note"]
    configured = ElasticsearchSource.from_spec(spec(resolution_ms=10_000))
    assert (configured.resolution_ms, configured.resolution_origin) == (10_000, "configured")


async def test_scrape_interval_is_none_documents_have_no_series_interval():
    assert await FakeEs().source().scrape_interval('{"query": {}}') is None


async def test_from_spec_sends_the_api_key_and_user_agent():
    fake = FakeEs()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    src = ElasticsearchSource.from_spec(
        spec(auth=AuthRef(env="ES_KEY", scheme="apikey")), environ={"ES_KEY": "abc=="},
        client=client,
    )  # fmt: skip
    await src.probe()
    assert fake.requests[0].headers["Authorization"] == "ApiKey abc=="
    assert fake.requests[0].headers["User-Agent"] == USER_AGENT


async def test_probe_reads_the_elasticsearch_version_and_checks_the_time_field():
    out = await FakeEs().source().probe()
    assert out["reachable"] is True
    assert (out["distribution"], out["version"]) == ("elasticsearch", "8.15.3")
    assert (out["index_pattern"], out["time_field"], out["indices"]) == (PATTERN, "@timestamp", 1)
    assert "flavor_mismatch" not in out and out["latency_ms"] >= 0


async def test_probe_reads_the_opensearch_distribution():
    out = await FakeEs(root=fixture("root_os2.json")).source(flavor="opensearch").probe()
    assert (out["distribution"], out["version"]) == ("opensearch", "2.17.1")
    assert "flavor_mismatch" not in out


async def test_a_flavor_mismatch_is_reported_not_fatal():
    out = await FakeEs(root=fixture("root_os2.json")).source(flavor="elasticsearch").probe()
    assert out["reachable"] is True
    assert "opensearch" in out["flavor_mismatch"]


@pytest.mark.parametrize(
    ("root", "ok"),
    [
        ({"version": {"number": "7.9.3"}}, False),
        ({"version": {"number": "7.10.2"}}, True),
        ({"version": {"number": "1.0.0", "distribution": "opensearch"}}, True),
        ({"version": {"number": "0.9.0", "distribution": "opensearch"}}, False),
    ],
)
async def test_supported_versions(root, ok):
    flavor = root["version"].get("distribution", "elasticsearch")
    src = FakeEs(root=root).source(flavor=flavor)
    if ok:
        assert (await src.probe())["version"] == root["version"]["number"]
    else:
        with pytest.raises(SourceError, match=root["version"]["number"]):
            await src.probe()


async def test_a_403_on_root_is_tolerated():
    root = es_error(403, "security_exception", "action [cluster:monitor/main] is unauthorized")
    out = await FakeEs(root=root).source().probe()
    assert out["reachable"] is True and "403" in out["version_unavailable"]


async def test_an_index_pattern_matching_nothing_fails_the_probe():
    with pytest.raises(SourceError, match="matches no index"):
        await FakeEs(indices=()).source().probe()


async def test_a_missing_time_field_names_the_date_fields_that_exist():
    caps = {k: v for k, v in CAPS.items() if k != "@timestamp"}
    caps["event.created"] = {"date": {"type": "date", "searchable": True, "aggregatable": True}}
    with pytest.raises(SourceError, match="not in the mapping.*event.created"):
        await FakeEs(caps=caps).source().probe()


async def test_a_time_field_that_is_not_a_date_is_refused():
    caps = {**CAPS, "@timestamp": {"keyword": {"type": "keyword", "aggregatable": True}}}
    with pytest.raises(SourceError, match="is keyword, not a date"):
        await FakeEs(caps=caps).source().probe()


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _src(handler) -> ElasticsearchSource:
    return ElasticsearchSource("es", URL, index_pattern=PATTERN, time_field="@timestamp",
                               client=_client(handler))  # fmt: skip


async def test_unreachable_cluster_is_source_unavailable():
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(SourceUnavailable, match="cannot reach"):
        await _src(refuse).probe()


async def test_client_timeout_is_source_unavailable():
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(SourceUnavailable, match="timed out after 30s"):
        await _src(slow).probe()


async def test_401_is_an_authentication_failure():
    with pytest.raises(SourceError, match="authentication failed") as e:
        await _src(lambda r: httpx.Response(401, text="Unauthorized")).probe()
    assert "apikey" in e.value.hint


async def test_a_non_json_body_is_not_an_es_endpoint():
    with pytest.raises(SourceUnavailable, match="non-JSON") as e:
        await _src(lambda r: httpx.Response(200, text="<html>login</html>")).probe()
    assert "proxy or login page" in e.value.hint


async def test_5xx_is_source_unavailable():
    with pytest.raises(SourceUnavailable, match="HTTP 503"):
        await _src(lambda r: es_error(503, "master_not_discovered_exception", "no master")).probe()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_es_source.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'telemetry_nerd.sources.elasticsearch'`.

- [ ] **Step 4: Implement** (`src/telemetry_nerd/sources/elasticsearch.py`)

```python
"""Elasticsearch / OpenSearch adapter: the caller's native Query DSL, bucketed by the adapter.

`expr` is an Elasticsearch request body (sources/esquery.py); the adapter adds the time range and a
date_histogram of query-step buckets, the way PromQLSource adds start/end/step. OpenSearch speaks
the same search API, so one adapter serves both flavors; `flavor` only changes how probe() reads
the version.

Query buckets carry their END time, but an Elasticsearch bucket is [t - step, t) where a
Prometheus one is (t - step, t]: a document stamped exactly on a boundary lands one bucket later
than a sample would. Design: docs/superpowers/specs/2026-10-07-elasticsearch-opensearch-adapter-design.md.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from typing import Any, Literal

import httpx

from telemetry_nerd.model.time import format_duration
from telemetry_nerd.sources.base import LimitExceeded, Limits, SourceError, SourceUnavailable
from telemetry_nerd.sources.gate import Gate
from telemetry_nerd.sources.promql import USER_AGENT
from telemetry_nerd.sources.spec import SourceSpec

EsFlavor = Literal["elasticsearch", "opensearch"]
#: finest query step assumed when none is configured (documents have no series interval)
DEFAULT_RESOLUTION_MS = 1_000
_DEFAULT_LIMITS = Limits()
NUMERIC_TYPES = frozenset({"long", "integer", "short", "byte", "double", "float", "half_float",
                           "scaled_float", "unsigned_long"})  # fmt: skip
DATE_TYPES = frozenset({"date", "date_nanos"})
LABEL_TYPES = frozenset({"keyword", "constant_keyword", "ip", "boolean"})
MIN_VERSION: dict[str, tuple[int, int]] = {"elasticsearch": (7, 10), "opensearch": (1, 0)}


class _Forbidden(SourceError):
    """403: the credentials lack a privilege (probe() tolerates it on `/`)."""


def _malformed(message: str) -> SourceError:
    return SourceError(
        message,
        hint="retry; if it persists, check the url points at an Elasticsearch/OpenSearch cluster",
    )


def _no_index(pattern: str) -> SourceError:
    return SourceError(
        f"index pattern {pattern} matches no index",
        hint="check index_pattern (source_connect(..., index_pattern=..., replace=true))",
    )


def _causes(err: object) -> list[tuple[str, str]]:
    """(type, reason) of an Elasticsearch error: root causes first, then the error itself and its
    caused_by chain."""
    out: list[tuple[str, str]] = []

    def walk(e: object) -> None:
        if not isinstance(e, dict):
            return
        for rc in e.get("root_cause") or []:
            walk(rc)
        if "type" in e:
            out.append((str(e["type"]), str(e.get("reason", ""))))
        walk(e.get("caused_by"))

    walk(err)
    return out


def _raise_for(status: int, body: dict, pattern: str) -> None:
    """Map an Elasticsearch error response to the typed source errors (spec: Errors)."""
    causes = _causes(body.get("error"))
    if not causes and isinstance(body.get("error"), str):
        causes = [("error", body["error"])]
    kinds = {t for t, _ in causes}
    first = f"{causes[0][0]}: {causes[0][1]}" if causes else f"HTTP {status}"
    if "too_many_buckets_exception" in kinds:
        raise LimitExceeded(
            f"too many buckets: {first}",
            hint="use a coarser step, a shorter range, a larger histogram interval or fewer groups",
        )
    if "circuit_breaking_exception" in kinds:
        raise LimitExceeded(
            f"the query needs too much memory: {first}",
            hint="narrow the query: shorter range, coarser step, fewer groups",
        )
    if status == 403:
        raise _Forbidden(
            f"permission denied: {first}",
            hint=f"the credentials need read and view_index_metadata on {pattern}",
        )
    if "index_not_found_exception" in kinds:
        raise _no_index(pattern)
    if status == 429 or status >= 500:
        raise SourceUnavailable(
            f"source returned HTTP {status}: {first}",
            hint="the cluster is overloaded or failing; retry shortly or narrow the query",
        )
    if any("fielddata" in reason for _, reason in causes):
        raise SourceError(
            f"cannot aggregate a text field: {first}",
            hint="aggregate its keyword sub-field instead (e.g. message.keyword); source_learn "
            "lists the aggregatable fields",
        )
    raise SourceError(
        f"query failed: {first}",
        hint="check the Query DSL / Lucene syntax and the field names; source_learn lists the fields",
    )


class ElasticsearchSource:
    query_language: Literal["es_dsl"] = "es_dsl"
    #: no verified missing-data profile yet (spec: later work)
    semantics = None

    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        index_pattern: str,
        time_field: str,
        flavor: EsFlavor = "elasticsearch",
        resolution_ms: int | None = None,
        limits: Limits = _DEFAULT_LIMITS,
        client: httpx.AsyncClient | None = None,
        headers: Mapping[str, str] | None = None,
        gate: Gate | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.flavor = flavor
        self.index_pattern = index_pattern
        self.time_field = time_field
        #: the finest query step the source accepts; never learned
        self.resolution_ms = resolution_ms or DEFAULT_RESOLUTION_MS
        self.resolution_origin: Literal["configured", "assumed"] = (
            "configured" if resolution_ms else "assumed"
        )
        self.limits = limits
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient()
        self._headers = {"User-Agent": USER_AGENT, **(headers or {})}
        self._gate = gate or Gate()
        self._caps: dict[str, dict[str, dict]] = {}  # field -> {type: info}, until discover()

    @classmethod
    def from_spec(
        cls,
        spec: SourceSpec,
        environ: Mapping[str, str] = os.environ,
        client: httpx.AsyncClient | None = None,
    ) -> ElasticsearchSource:
        """Build a live source; resolves the secret reference now (raises MissingSecret)."""
        assert spec.index_pattern is not None and spec.time_field is not None  # spec validates
        return cls(
            spec.name,
            spec.url,
            index_pattern=spec.index_pattern,
            time_field=spec.time_field,
            flavor=spec.flavor,  # type: ignore[arg-type]
            resolution_ms=spec.resolution_ms,
            limits=Limits(timeout_s=spec.politeness.timeout_s),
            client=client,
            headers=spec.auth.headers(environ) if spec.auth else {},
            gate=Gate(spec.politeness.max_concurrency, spec.politeness.min_interval_ms),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def identity(self) -> str:
        # the cache key changes when the index pattern or the time field does
        return (
            f"{self.flavor}|{self.base_url}|{self.index_pattern}|{self.time_field}|"
            f"{self.resolution_ms}"
        )

    def resolution_info(self) -> dict:
        note = "documents have no series interval: this is the finest query step the source accepts"
        if self.resolution_origin == "assumed":
            note += (
                f" (assumed {format_duration(DEFAULT_RESOLUTION_MS)}; connect with "
                "resolution=... to change it)"
            )
        return {
            "resolution": format_duration(self.resolution_ms),
            "origin": self.resolution_origin,
            "note": note,
        }

    async def scrape_interval(self, selector: str, at_ms: int | None = None) -> int | None:
        return None  # documents are events, not samples of a series

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: dict | None = None,
        timeout_s: float | None = None,
    ) -> dict:
        url = f"{self.base_url}{path}"
        timeout_s = timeout_s or self.limits.timeout_s
        try:
            async with self._gate.slot():
                resp = await self._client.request(
                    method, url, params=params, json=body, headers=self._headers,
                    timeout=timeout_s,
                )  # fmt: skip
        except httpx.TimeoutException as e:
            raise SourceUnavailable(
                f"query timed out after {timeout_s:g}s",
                hint="narrow the query, shorten the range, or use a coarser step",
            ) from e
        except httpx.HTTPError as e:
            raise SourceUnavailable(
                f"cannot reach {self.base_url}: {e}",
                hint="check the source url and that the cluster is up",
            ) from e
        if resp.status_code == 401:
            raise SourceError(
                "authentication failed",
                hint="check the secret reference and auth_scheme: apikey for Elasticsearch API "
                "keys, bearer for service-account tokens and JWTs, basic for user:pass",
            )
        try:
            data = resp.json()
        except ValueError as e:
            if resp.status_code == 429 or resp.status_code >= 500:
                raise SourceUnavailable(
                    f"source returned HTTP {resp.status_code}",
                    hint="the cluster is overloaded or failing; retry shortly",
                ) from e
            raise SourceUnavailable(
                f"non-JSON response from source (HTTP {resp.status_code})",
                hint="check the url points at an Elasticsearch/OpenSearch cluster, not a proxy "
                "or login page",
            ) from e
        if not isinstance(data, dict):
            raise _malformed(f"unexpected response body {type(data).__name__}")
        if resp.status_code >= 400:
            _raise_for(resp.status_code, data, self.index_pattern)
        return data

    async def probe(self) -> dict:
        """Version (flavor-aware) and the time field: `GET /`, then `_field_caps` of time_field."""
        t0 = time.monotonic()
        out: dict[str, Any] = {"reachable": True}
        try:
            root = await self._request("GET", "/")
        except _Forbidden:
            out["version_unavailable"] = "403 on / (no monitor privilege): version not checked"
        else:
            out |= self._version(root)
        caps = await self._request(
            "GET", f"/{self.index_pattern}/_field_caps", params={"fields": self.time_field}
        )
        indices = caps.get("indices")
        if isinstance(indices, list) and not indices:
            raise _no_index(self.index_pattern)
        fields = caps.get("fields") if isinstance(caps.get("fields"), dict) else {}
        types = set(fields.get(self.time_field) or {}) - {"unmapped"}
        if not types or not types <= DATE_TYPES:
            what = "is not in the mapping" if not types else f"is {', '.join(sorted(types))}, not a date"
            dates = await self._date_fields()
            raise SourceError(
                f"time field {self.time_field} {what} (index pattern {self.index_pattern}; "
                f"date fields: {', '.join(dates) or 'none'})",
                hint="reconnect with time_field set to one of the date fields",
            )
        out |= {
            "latency_ms": round((time.monotonic() - t0) * 1000),
            "index_pattern": self.index_pattern,
            "time_field": self.time_field,
        }
        if isinstance(indices, list):
            out["indices"] = len(indices)
        return out

    def _version(self, root: dict) -> dict:
        v = root.get("version") if isinstance(root.get("version"), dict) else {}
        number = str(v.get("number", ""))
        found: EsFlavor = "opensearch" if v.get("distribution") == "opensearch" else "elasticsearch"
        try:
            major, minor = (int(x) for x in number.split(".")[:2])
        except ValueError:
            return {"distribution": found, "version": number or None}
        if (major, minor) < MIN_VERSION[found]:
            need = ".".join(str(x) for x in MIN_VERSION[found])
            raise SourceError(
                f"{found} {number} is not supported (needs >= {need})",
                hint="supported: Elasticsearch >= 7.10, OpenSearch >= 1.0",
            )
        out: dict[str, Any] = {"distribution": found, "version": number}
        if found != self.flavor:
            out["flavor_mismatch"] = (
                f"connected as {self.flavor} but the cluster reports {found} (the search API is "
                f"the same; reconnect with flavor={found!r} to silence this)"
            )
        return out

    async def _date_fields(self) -> list[str]:
        caps = await self._request(
            "GET", f"/{self.index_pattern}/_field_caps", params={"fields": "*"}
        )
        fields = caps.get("fields") if isinstance(caps.get("fields"), dict) else {}
        return sorted(f for f, t in fields.items() if not f.startswith("_") and set(t) & DATE_TYPES)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_es_source.py -q`
Expected: PASS.

- [ ] **Step 6: Gates and commit**

```bash
just fmt && just lint && uv run pytest tests/unit -q
git add src/telemetry_nerd/sources/elasticsearch.py tests/unit/es_fake.py \
  tests/fixtures/elasticsearch/root_es8.json tests/fixtures/elasticsearch/root_os2.json \
  tests/unit/test_es_source.py
git commit -m "feat(es): ElasticsearchSource transport, error mapping and flavor-aware probe"
```

---

### Task 6: `fetch` / `fetch_values` — forms, grouping, field checks, partial responses, error table

**Files:**
- Modify: `src/telemetry_nerd/sources/elasticsearch.py`
- Create: `tests/fixtures/elasticsearch/search_stats_by_status.json`
- Test: `tests/unit/test_es_fetch.py`

**Interfaces:**
- Consumes: `EsQuery`, `TIME_AGG`, `COUNT_AGG` (Task 2); `ElasticsearchSource._request`, `_no_index`, `_malformed`, `_causes`, `NUMERIC_TYPES` (Task 5); `MAX_STEPS_PER_QUERY` (`sources/promql.py`); `BUCKET_SCHEMA`, `SERIES_SCHEMA`, `FetchResult`, `labels_json`, `series_id` (`model/series.py`).
- Produces (used by Tasks 7, 11, 15):
  - `async ElasticsearchSource.fetch(expr: str, rng: TimeRange, step_ms: int) -> FetchResult` (refuses `percentile` and `histogram`)
  - `async ElasticsearchSource.fetch_values(expr: str, rng: TimeRange, step_ms: int) -> FetchResult` (every form but `histogram`)
  - `async ElasticsearchSource._search(q: EsQuery, rng: TimeRange, step_ms: int) -> tuple[dict, tuple[tuple[int, int, str], ...]]`
  - `async ElasticsearchSource._check_fields(q: EsQuery) -> None`; `async _field_types(field: str) -> dict[str, dict]` (cached in `self._caps`)
  - module helpers `_time_buckets(resp) -> list[dict]`, `_interior(buckets) -> list[dict]`, `_bucket_ts(b, step_ms) -> int`, `_groups(q, b) -> Iterator[tuple[dict[str, str], dict]]`, `_cell(q, parent, step_s) -> tuple[float, float, float, int] | None`, `_percentile_value(agg) -> float | None`

- [ ] **Step 1: Write the fixture**

`tests/fixtures/elasticsearch/search_stats_by_status.json` (grouped `stats`; keys are bucket starts: START − 60 s leading empty, START and START + 60 s with data, START + 120 s documents without the field, START + 180 s trailing empty):

```json
{
  "took": 5,
  "timed_out": false,
  "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0},
  "hits": {"hits": []},
  "aggregations": {"__tn_time": {"buckets": [
    {"key_as_string": "1699999980000", "key": 1699999980000, "doc_count": 0,
     "by_status": {"doc_count_error_upper_bound": 0, "sum_other_doc_count": 0, "buckets": []}},
    {"key_as_string": "1700000040000", "key": 1700000040000, "doc_count": 12,
     "by_status": {"doc_count_error_upper_bound": 0, "sum_other_doc_count": 0, "buckets": [
       {"key": 200, "doc_count": 10, "lat": {"count": 10, "min": 40.0, "max": 40.0, "avg": 40.0, "sum": 400.0}},
       {"key": 500, "doc_count": 2, "lat": {"count": 2, "min": 90.0, "max": 110.0, "avg": 100.0, "sum": 200.0}}
     ]}},
    {"key_as_string": "1700000100000", "key": 1700000100000, "doc_count": 10,
     "by_status": {"doc_count_error_upper_bound": 0, "sum_other_doc_count": 0, "buckets": [
       {"key": 200, "doc_count": 10, "lat": {"count": 10, "min": 30.0, "max": 50.0, "avg": 41.0, "sum": 410.0}}
     ]}},
    {"key_as_string": "1700000160000", "key": 1700000160000, "doc_count": 3,
     "by_status": {"doc_count_error_upper_bound": 0, "sum_other_doc_count": 0, "buckets": [
       {"key": 500, "doc_count": 3, "lat": {"count": 0, "min": null, "max": null, "avg": null, "sum": 0.0}}
     ]}},
    {"key_as_string": "1700000220000", "key": 1700000220000, "doc_count": 0,
     "by_status": {"doc_count_error_upper_bound": 0, "sum_other_doc_count": 0, "buckets": []}}
  ]}}
}
```

- [ ] **Step 2: Write the failing tests** (`tests/unit/test_es_fetch.py`)

```python
"""ElasticsearchSource.fetch / fetch_values: forms, grouping, field checks and the error table."""

import json

import httpx
import pytest

from telemetry_nerd.model.series import series_id
from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.base import LimitExceeded, Limits, SourceError, SourceUnavailable
from telemetry_nerd.sources.elasticsearch import _Forbidden

from .es_fake import CAPS, PATTERN, RNG, START, FakeEs, es_error, fixture, search_response, tb

QS = {"query_string": {"query": "service.name:checkout"}}
RATE = json.dumps({"query": QS})
STATS = json.dumps({"query": QS, "aggs": {"lat": {"stats": {"field": "event.duration"}}}})
COUNT = json.dumps({"aggs": {"n": {"value_count": {"field": "event.duration"}}}})
P99 = json.dumps({"aggs": {"p": {"percentiles": {"field": "event.duration", "percents": [99]}}}})
BY_STATUS_RATE = json.dumps({"aggs": {"by_status": {"terms": {"field": "http.response.status_code"}}}})
BY_STATUS_STATS = json.dumps({"aggs": {"by_status": {
    "terms": {"field": "http.response.status_code"},
    "aggs": {"lat": {"stats": {"field": "event.duration"}}}}}})
HIST = json.dumps({"aggs": {"h": {"histogram": {"field": "event.duration", "interval": 25}}}})
M = 60_000


def rows(result) -> list[dict]:
    return result.buckets.to_pylist()


def labels_of(result) -> dict[str, dict]:
    return {r["series_id"]: json.loads(r["labels"]) for r in result.series.to_pylist()}


RATE_BUCKETS = [tb(START - M, 0), tb(START, 120), tb(START + M, 0), tb(START + 2 * M, 60),
                tb(START + 3 * M, 0)]  # fmt: skip


async def test_rate_is_documents_per_second_at_bucket_end_times_with_interior_zeros():
    fake = FakeEs(search=search_response(RATE_BUCKETS))
    res = await fake.source().fetch(RATE, RNG, M)
    sid = series_id("es", {})
    assert rows(res) == [
        {"ts_ms": START + M, "series_id": sid, "avg": 2.0, "min": 2.0, "max": 2.0, "count": 1},
        {"ts_ms": START + 2 * M, "series_id": sid, "avg": 0.0, "min": 0.0, "max": 0.0, "count": 1},
        {"ts_ms": START + 3 * M, "series_id": sid, "avg": 1.0, "min": 1.0, "max": 1.0, "count": 1},
    ]  # leading (ends START) and trailing (ends START + 4 m) empty buckets: no rows
    assert labels_of(res) == {sid: {}}
    [body] = fake.searches()
    assert body["aggs"]["__tn_time"]["date_histogram"]["fixed_interval"] == "60000ms"
    window = body["query"]["bool"]["filter"][1]["range"]["@timestamp"]
    assert (window["gte"], window["lt"]) == (START - M, START + 4 * M)
    assert body["query"]["bool"]["filter"][0] == QS


async def test_rate_needs_no_field_check():
    fake = FakeEs(search=search_response(RATE_BUCKETS))
    await fake.source().fetch(RATE, RNG, M)
    assert fake.caps_requests() == []


async def test_field_rate_is_value_count_per_second():
    buckets = [tb(START, 30, n={"value": 30}), tb(START + M, 0, n={"value": 0}),
               tb(START + 2 * M, 6, n={"value": 3})]  # fmt: skip
    res = await FakeEs(search=search_response(buckets)).source().fetch(COUNT, RNG, M)
    assert [(r["ts_ms"], r["avg"], r["count"]) for r in rows(res)] == [
        (START + M, 0.5, 1), (START + 2 * M, 0.0, 1), (START + 3 * M, 0.05, 1)
    ]


async def test_stats_keeps_mean_min_max_and_the_documents_that_had_the_field():
    buckets = [
        tb(START, 12, lat={"count": 12, "min": 40.0, "max": 100.0, "avg": 50.0, "sum": 600.0}),
        tb(START + M, 4, lat={"count": 0, "min": None, "max": None, "avg": None, "sum": 0.0}),
        tb(START + 2 * M, 1, lat={"count": 1, "min": 7.0, "max": 7.0, "avg": 7.0, "sum": 7.0}),
    ]
    res = await FakeEs(search=search_response(buckets)).source().fetch(STATS, RNG, M)
    assert [(r["ts_ms"], r["avg"], r["min"], r["max"], r["count"]) for r in rows(res)] == [
        (START + M, 50.0, 40.0, 100.0, 12), (START + 3 * M, 7.0, 7.0, 7.0, 1)
    ]  # count 0: no observation, no row


async def test_fetch_refuses_a_percentile_and_fetch_values_reads_it_with_its_count():
    buckets = [
        tb(START, 40, p={"values": {"99.0": 812.5}}, __tn_n={"value": 40}),
        tb(START + M, 5, p={"values": {"99.0": None}}, __tn_n={"value": 0}),
    ]
    fake = FakeEs(search=search_response(buckets))
    src = fake.source()
    with pytest.raises(SourceError, match="never rolled up"):
        await src.fetch(P99, RNG, M)
    res = await src.fetch_values(P99, RNG, M)
    assert [(r["ts_ms"], r["avg"], r["count"]) for r in rows(res)] == [(START + M, 812.5, 40)]
    inner = fake.searches()[-1]["aggs"]["__tn_time"]["aggs"]
    assert inner["__tn_n"] == {"value_count": {"field": "event.duration"}}


async def test_percentile_values_also_parse_the_unkeyed_list_shape():
    buckets = [tb(START, 3, p={"values": [{"key": 99.0, "value": 9.0}]}, __tn_n={"value": 3})]
    res = await FakeEs(search=search_response(buckets)).source().fetch_values(P99, RNG, M)
    assert [r["avg"] for r in rows(res)] == [9.0]


async def test_grouped_stats_are_one_series_per_term_and_absent_terms_have_no_row():
    res = await FakeEs(search=fixture("search_stats_by_status.json")).source().fetch(
        BY_STATUS_STATS, RNG, M
    )
    s200 = series_id("es", {"http.response.status_code": "200"})
    s500 = series_id("es", {"http.response.status_code": "500"})
    assert labels_of(res) == {s200: {"http.response.status_code": "200"},
                              s500: {"http.response.status_code": "500"}}  # fmt: skip
    got = {(r["series_id"], r["ts_ms"]): (r["avg"], r["count"]) for r in rows(res)}
    assert got == {
        (s200, START + M): (40.0, 10), (s200, START + 2 * M): (41.0, 10),
        (s500, START + M): (100.0, 2),  # absent at START + 2 m; count 0 at START + 3 m
    }  # fmt: skip


async def test_grouped_rate_reads_an_absent_term_in_an_interior_bucket_as_zero():
    def terms(*pairs):
        return {"sum_other_doc_count": 0, "buckets": [{"key": k, "doc_count": n} for k, n in pairs]}

    buckets = [tb(START, 12, by_status=terms((200, 10), (500, 2))),
               tb(START + M, 6, by_status=terms((200, 6)))]  # fmt: skip
    res = await FakeEs(search=search_response(buckets)).source().fetch(BY_STATUS_RATE, RNG, M)
    s500 = series_id("es", {"http.response.status_code": "500"})
    got = {(r["series_id"], r["ts_ms"]): r["avg"] for r in rows(res)}
    assert got[(s500, START + M)] == pytest.approx(2 / 60)
    assert got[(s500, START + 2 * M)] == 0.0


async def test_truncated_terms_are_refused_not_silently_dropped():
    buckets = [tb(START, 12, by_status={"sum_other_doc_count": 3,
                                        "buckets": [{"key": 200, "doc_count": 9}]})]  # fmt: skip
    with pytest.raises(LimitExceeded, match="other terms") as e:
        await FakeEs(search=search_response(buckets)).source().fetch(BY_STATUS_RATE, RNG, M)
    assert "size" in e.value.hint


async def test_more_series_than_the_limit_is_refused():
    buckets = [tb(START, 3, by_status={"sum_other_doc_count": 0, "buckets": [
        {"key": 200, "doc_count": 2}, {"key": 500, "doc_count": 1}]})]  # fmt: skip
    src = FakeEs(search=search_response(buckets)).source(limits=Limits(max_series=1))
    with pytest.raises(LimitExceeded, match="2 series"):
        await src.fetch(BY_STATUS_RATE, RNG, M)


async def test_too_many_steps_are_refused_before_sending():
    fake = FakeEs()
    with pytest.raises(LimitExceeded, match="steps exceeds"):
        await fake.source().fetch(RATE, TimeRange(START, START + 12_000_000), 1_000)
    assert fake.requests == []


async def test_a_server_side_timeout_keeps_the_data_and_marks_the_span_partial():
    resp = search_response(RATE_BUCKETS, timed_out=True)
    res = await FakeEs(search=resp).source().fetch(RATE, RNG, M)
    assert len(rows(res)) == 3
    [(a, b, why)] = res.failed
    assert (a, b) == (RNG.start_ms, RNG.end_ms) and why.startswith("PartialResponse:")
    assert "timed out" in why


async def test_shard_failures_mark_the_span_partial_with_the_reason():
    failures = [{"shard": 0, "index": "tn-access-1",
                 "reason": {"type": "node_disconnected_exception", "reason": "node left"}}]  # fmt: skip
    resp = search_response(RATE_BUCKETS, total=2, failed=1, failures=failures)
    res = await FakeEs(search=resp).source().fetch(RATE, RNG, M)
    assert "1 of 2 shards failed" in res.failed[0][2]
    assert "node_disconnected_exception" in res.failed[0][2]


async def test_a_wildcard_matching_no_index_answers_200_with_zero_shards():
    resp = search_response([], total=0)
    with pytest.raises(SourceError, match="matches no index"):
        await FakeEs(search=resp).source().fetch(RATE, RNG, M)


async def test_an_unmapped_aggregated_field_is_refused_before_searching():
    fake = FakeEs(caps={k: v for k, v in CAPS.items() if k != "event.duration"})
    with pytest.raises(SourceError, match=f"event.duration is not in the mapping of {PATTERN}"):
        await fake.source().fetch(STATS, RNG, M)
    assert fake.searches() == []


async def test_a_metric_on_a_non_numeric_field_is_refused():
    expr = json.dumps({"aggs": {"lat": {"stats": {"field": "service.name"}}}})
    with pytest.raises(SourceError, match="service.name is keyword, not numeric"):
        await FakeEs().source().fetch(expr, RNG, M)


async def test_terms_on_a_text_field_is_refused_with_the_keyword_hint():
    expr = json.dumps({"aggs": {"by_msg": {"terms": {"field": "message"}}}})
    with pytest.raises(SourceError, match="not aggregatable") as e:
        await FakeEs().source().fetch(expr, RNG, M)
    assert "message.keyword" in e.value.hint


async def test_a_field_mapped_differently_across_indices_is_refused():
    caps = {**CAPS, "event.duration": {"long": {"type": "long", "aggregatable": True},
                                       "keyword": {"type": "keyword", "aggregatable": True}}}  # fmt: skip
    with pytest.raises(SourceError, match="mapped as keyword, long"):
        await FakeEs(caps=caps).source().fetch(STATS, RNG, M)


async def test_field_checks_are_cached_per_source():
    fake = FakeEs(search=search_response([]))
    src = fake.source()
    await src.fetch(STATS, RNG, M)
    await src.fetch(STATS, RNG, M)
    assert fake.caps_requests() == ["event.duration"]


async def test_an_unsupported_shape_lists_the_accepted_forms():
    with pytest.raises(SourceError, match="avg is not supported") as e:
        await FakeEs().source().fetch(json.dumps({"aggs": {"a": {"avg": {"field": "x"}}}}), RNG, M)
    assert "value_count" in e.value.hint


async def test_a_histogram_belongs_to_query_distribution():
    with pytest.raises(SourceError, match="distribution") as e:
        await FakeEs().source().fetch_values(HIST, RNG, M)
    assert "query_distribution" in e.value.hint


TOO_MANY = {"type": "too_many_buckets_exception",
            "reason": "Trying to create too many buckets. Must be less than or equal to: [65536]"}
FIELDDATA = ("Text fields are not optimised for operations that require per-document field data "
             "like aggregations and sorting, so these operations are disabled by default. Please "
             "use a keyword field instead. Alternatively, set fielddata=true on [message]")


@pytest.mark.parametrize(
    ("response", "error", "match"),
    [
        (es_error(400, "parsing_exception", "unknown query [query_strin]"), SourceError,
         "query failed: parsing_exception: unknown query"),
        (es_error(400, "x_content_parse_exception", "[1:10] unknown field [qury]"), SourceError,
         "x_content_parse_exception"),
        (es_error(400, "search_phase_execution_exception", "all shards failed",
                  root=("query_shard_exception", "failed to create query: For input string")),
         SourceError, "query_shard_exception: failed to create query"),
        (es_error(400, "illegal_argument_exception", FIELDDATA), SourceError, "text field"),
        (es_error(503, "search_phase_execution_exception", "all shards failed", caused_by=TOO_MANY),
         LimitExceeded, "too many buckets"),
        (es_error(400, "too_many_buckets_exception", TOO_MANY["reason"]), LimitExceeded,
         "too many buckets"),
        (es_error(429, "circuit_breaking_exception", "[parent] Data too large"), LimitExceeded,
         "too much memory"),
        (es_error(429, "es_rejected_execution_exception", "rejected execution of coordinating"),
         SourceUnavailable, "HTTP 429"),
        (es_error(500, "null_pointer_exception", "boom"), SourceUnavailable, "HTTP 500"),
        (es_error(403, "security_exception", "action [indices:data/read/search] is unauthorized"),
         _Forbidden, "permission denied: security_exception"),
        (es_error(404, "index_not_found_exception", "no such index [tn-access-x]"), SourceError,
         "matches no index"),
        (httpx.Response(200, text="<html>Sign in</html>"), SourceUnavailable, "non-JSON"),
    ],
)  # fmt: skip
async def test_search_errors_map_to_typed_source_errors(response, error, match):
    with pytest.raises(SourceError, match=match) as e:
        await FakeEs(search=response).source().fetch(RATE, RNG, M)
    assert type(e.value) is error
    assert e.value.hint
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_es_fetch.py -q`
Expected: FAIL with `AttributeError: 'ElasticsearchSource' object has no attribute 'fetch'`.

- [ ] **Step 4: Implement** (add to `src/telemetry_nerd/sources/elasticsearch.py`)

Imports to add:

```python
from collections.abc import Iterator, Mapping

import pyarrow as pa

from telemetry_nerd.model.series import (
    BUCKET_SCHEMA,
    SERIES_SCHEMA,
    FetchResult,
    labels_json,
    series_id,
)
from telemetry_nerd.model.time import TimeRange, format_duration
from telemetry_nerd.sources.esquery import TIME_AGG, COUNT_AGG, EsQuery
from telemetry_nerd.sources.promql import MAX_STEPS_PER_QUERY, USER_AGENT
```

Module helpers (after `_raise_for`):

```python
Cell = tuple[float, float, float, int]  # avg, min, max, count
Failed = tuple[tuple[int, int, str], ...]


def _time_buckets(resp: dict) -> list[dict]:
    buckets = ((resp.get("aggregations") or {}).get(TIME_AGG) or {}).get("buckets")
    if not isinstance(buckets, list):
        raise _malformed("response has no time buckets (aggregations.__tn_time.buckets)")
    return buckets


def _interior(buckets: list[dict]) -> list[dict]:
    """First..last non-empty time bucket: before the first document is the retention edge and
    after the last the future, unknown, never zero (min_doc_count 0 fills only between them)."""
    full = [i for i, b in enumerate(buckets) if (b.get("doc_count") or 0) > 0]
    return buckets[full[0] : full[-1] + 1] if full else []


def _bucket_ts(b: dict, step_ms: int) -> int:
    try:
        return int(b["key"]) + step_ms  # keys are bucket starts; a query bucket carries its end
    except (KeyError, TypeError, ValueError) as e:
        raise _malformed(f"malformed time bucket {b!r}") from e


def _groups(q: EsQuery, b: dict) -> Iterator[tuple[dict[str, str], dict]]:
    """(labels, the bucket holding the metric) per series in one time bucket."""
    if q.group_name is None or q.group_field is None:
        yield {}, b
        return
    agg = b.get(q.group_name)
    if not isinstance(agg, dict) or not isinstance(agg.get("buckets"), list):
        raise _malformed(f"time bucket without its terms aggregation {q.group_name!r}")
    other = agg.get("sum_other_doc_count") or 0
    if other > 0:
        size = (q.group or {}).get("size", 10)
        raise LimitExceeded(
            f"terms on {q.group_field} returned only the top {size} terms in a query bucket "
            f"({other} documents fell in other terms)",
            hint="raise the terms size (up to the 500-series limit) or narrow the query",
        )
    for tb in agg["buckets"]:
        key = tb.get("key_as_string", tb.get("key"))
        yield {q.group_field: str(key)}, tb


def _percentile_value(agg: dict) -> float | None:
    values = agg.get("values")
    if isinstance(values, dict):
        v = next(iter(values.values()), None)
    elif isinstance(values, list) and values:
        v = values[0].get("value")
    else:
        v = None
    return None if v is None else float(v)


def _cell(q: EsQuery, parent: dict, step_s: float) -> Cell | None:
    """One query bucket's value of the form, or None when it holds no observation."""
    try:
        if q.form == "rate":
            v = float(parent["doc_count"]) / step_s
            return v, v, v, 1
        agg = parent[q.metric_name]
        if q.form == "field_rate":
            v = float(agg.get("value") or 0) / step_s
            return v, v, v, 1
        if q.form == "stats":
            n = int(agg.get("count") or 0)
            if n == 0:
                return None
            return float(agg["avg"]), float(agg["min"]), float(agg["max"]), n
        n = int((parent.get(COUNT_AGG) or {}).get("value") or 0)
        v = _percentile_value(agg)
        if n == 0 or v is None:
            return None
        return v, v, v, n
    except (KeyError, TypeError, ValueError) as e:
        raise _malformed(f"malformed {q.form} bucket {parent!r}") from e
```

Methods on `ElasticsearchSource`:

```python
    async def fetch(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult:
        q = EsQuery.parse(expr)
        if q.form == "percentile":
            raise SourceError(
                "a percentile is read per query bucket, never rolled up",
                hint="query() routes a percentiles aggregation to fetch_values",
            )
        return await self._fetch(q, rng, step_ms)

    async def fetch_values(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult:
        """Every non-histogram form, one value per query bucket (percentiles included)."""
        return await self._fetch(EsQuery.parse(expr), rng, step_ms)

    async def _fetch(self, q: EsQuery, rng: TimeRange, step_ms: int) -> FetchResult:
        if q.form == "histogram":
            raise SourceError(
                "a histogram aggregation is a distribution, not a time series",
                hint="use query_distribution(selector=<this expr>, source=...) for counts per "
                "value bucket",
            )
        resp, failed = await self._search(q, rng, step_ms)
        step_s = step_ms / 1000
        cells: dict[tuple[str, int], Cell] = {}
        labels_by_sid: dict[str, dict[str, str]] = {}
        interior = _interior(_time_buckets(resp))
        for b in interior:
            ts = _bucket_ts(b, step_ms)
            for labels, parent in _groups(q, b):
                sid = series_id(self.name, labels)
                labels_by_sid[sid] = labels
                if (cell := _cell(q, parent, step_s)) is not None:
                    cells[(sid, ts)] = cell
        if len(labels_by_sid) > self.limits.max_series:
            raise LimitExceeded(
                f"query returned {len(labels_by_sid)} series (limit {self.limits.max_series})",
                hint="group by a field with fewer values, or narrow the query",
            )
        if q.form in ("rate", "field_rate") and q.group is not None:
            # a term absent from an interior query bucket had no matching documents there
            for sid in labels_by_sid:
                for b in interior:
                    cells.setdefault((sid, _bucket_ts(b, step_ms)), (0.0, 0.0, 0.0, 1))
        keys = sorted(cells)
        sids = sorted({sid for sid, _ in keys})
        buckets = pa.table(
            {
                "ts_ms": [ts for _, ts in keys],
                "series_id": [sid for sid, _ in keys],
                "avg": [cells[k][0] for k in keys],
                "min": [cells[k][1] for k in keys],
                "max": [cells[k][2] for k in keys],
                "count": [cells[k][3] for k in keys],
            },
            schema=BUCKET_SCHEMA,
        )
        series = pa.table(
            {"series_id": sids, "labels": [labels_json(labels_by_sid[s]) for s in sids]},
            schema=SERIES_SCHEMA,
        )
        return FetchResult(buckets, series, failed=failed)

    async def _search(self, q: EsQuery, rng: TimeRange, step_ms: int) -> tuple[dict, Failed]:
        steps = (rng.end_ms - rng.start_ms) // step_ms + 1
        if steps > MAX_STEPS_PER_QUERY:
            raise LimitExceeded(
                f"{steps} steps exceeds {MAX_STEPS_PER_QUERY} per query",
                hint="use a coarser step or a shorter range",
            )
        await self._check_fields(q)
        body = q.body(rng, step_ms, self.time_field, self.limits.timeout_s)
        resp = await self._request("POST", f"/{self.index_pattern}/_search", body=body)
        shards = resp.get("_shards") if isinstance(resp.get("_shards"), dict) else {}
        if shards.get("total") == 0:  # a wildcard that matches nothing answers 200
            raise _no_index(self.index_pattern)
        reasons: list[str] = []
        if resp.get("timed_out") is True:
            reasons.append(
                f"the cluster timed out after {self.limits.timeout_s:g}s and returned partial results"
            )
        if (shards.get("failed") or 0) > 0:
            fails = shards.get("failures") or []
            why = _causes(fails[0].get("reason")) if fails and isinstance(fails[0], dict) else []
            reasons.append(
                f"{shards['failed']} of {shards.get('total')} shards failed"
                + (f" ({why[0][0]}: {why[0][1]})" if why else "")
            )
        failed: Failed = (
            ((rng.start_ms, rng.end_ms, "PartialResponse: " + "; ".join(reasons)),) if reasons else ()
        )
        return resp, failed

    async def _field_types(self, field: str) -> dict[str, dict]:
        if field not in self._caps:
            caps = await self._request(
                "GET", f"/{self.index_pattern}/_field_caps", params={"fields": field}
            )
            if isinstance(caps.get("indices"), list) and not caps["indices"]:
                raise _no_index(self.index_pattern)
            fields = caps.get("fields") if isinstance(caps.get("fields"), dict) else {}
            types = fields.get(field) or {}
            self._caps[field] = {t: i for t, i in types.items() if t != "unmapped"}
        return self._caps[field]

    async def _check_fields(self, q: EsQuery) -> None:
        """Elasticsearch answers an aggregation on an unmapped field with empty results, which
        would read as "no data": every aggregated field is checked against the mapping first."""
        for field, need in q.fields():
            types = await self._field_types(field)
            if not types:
                raise SourceError(
                    f"field {field} is not in the mapping of {self.index_pattern}",
                    hint="source_learn lists the fields (then catalog_search)",
                )
            if len(types) > 1:
                raise SourceError(
                    f"field {field} is mapped as {', '.join(sorted(types))} across the indices "
                    f"of {self.index_pattern}",
                    hint="narrow index_pattern to indices that agree, or aggregate another field",
                )
            [(kind, info)] = types.items()
            if need == "numeric" and kind not in NUMERIC_TYPES:
                raise SourceError(
                    f"field {field} is {kind}, not numeric",
                    hint="stats, percentiles and histogram need a numeric field; source_learn "
                    "lists the numeric fields as metrics",
                )
            if need == "aggregatable" and not info.get("aggregatable"):
                raise SourceError(
                    f"field {field} ({kind}) is not aggregatable",
                    hint=f"use its keyword sub-field, e.g. {field}.keyword",
                )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_es_fetch.py tests/unit/test_es_source.py -q`
Expected: PASS.

- [ ] **Step 6: Gates and commit**

```bash
just fmt && just lint && uv run pytest tests/unit -q
git add src/telemetry_nerd/sources/elasticsearch.py \
  tests/fixtures/elasticsearch/search_stats_by_status.json tests/unit/test_es_fetch.py
git commit -m "feat(es): fetch and fetch_values for the rate, field_rate, stats and percentile forms"
```

---

### Task 7: `fetch_histogram` — query-chosen distributions

**Files:**
- Modify: `src/telemetry_nerd/sources/elasticsearch.py`
- Test: `tests/unit/test_es_histogram.py`

**Interfaces:**
- Consumes: `EsQuery.with_group` (Task 2); `BucketScheme("linear", width=, offset=)` (Task 3); `_search`, `_interior`, `_time_buckets`, `_bucket_ts`, `_groups` (Task 6); `DIST_SCHEMA`, `COLUMN_SCHEMA`, `DistResult` (`model/distribution.py`).
- Produces (used by Tasks 11, 15): `async ElasticsearchSource.fetch_histogram(selector: str, by: Sequence[str], rng: TimeRange, step_ms: int) -> DistResult` with `scheme.kind == "linear"`, `expr == selector` (verbatim), `caveats == ("query_chosen_buckets",)`, `failed` from partial responses.

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_es_histogram.py`)

```python
"""ElasticsearchSource.fetch_histogram: query-chosen value buckets mapped onto DistResult."""

import json

import pytest

from telemetry_nerd.model.distribution import BucketScheme
from telemetry_nerd.model.series import series_id
from telemetry_nerd.sources.base import LimitExceeded, SourceError

from .es_fake import RNG, START, FakeEs, search_response, tb

M = 60_000
SELECTOR = json.dumps({"query": {"query_string": {"query": "NOT url.path:\"/healthz\""}},
                       "aggs": {"lat": {"histogram": {"field": "event.duration", "interval": 25}}}})  # fmt: skip


def hist(*pairs):
    return {"buckets": [{"key": k, "doc_count": n} for k, n in pairs]}


async def test_ungrouped_histogram_rows_columns_and_scheme():
    buckets = [
        tb(START - M, 0, lat=hist()),
        tb(START, 13, lat=hist((0.0, 10), (25.0, 0), (50.0, 3))),
        tb(START + M, 0, lat=hist()),
        tb(START + 2 * M, 4, lat=hist((25.0, 4))),
    ]
    dist = await FakeEs(search=search_response(buckets)).source().fetch_histogram(
        SELECTOR, (), RNG, M
    )
    sid = series_id("es", {})
    assert dist.rows.to_pylist() == [
        {"ts_ms": START + M, "series_id": sid, "bucket_lo": 0.0, "bucket_hi": 25.0, "count": 10.0},
        {"ts_ms": START + M, "series_id": sid, "bucket_lo": 50.0, "bucket_hi": 75.0, "count": 3.0},
        {"ts_ms": START + 3 * M, "series_id": sid, "bucket_lo": 25.0, "bucket_hi": 50.0, "count": 4.0},
    ]  # zero-count leaf buckets are not rows
    assert dist.columns.to_pylist() == [
        {"ts_ms": START + M, "series_id": sid, "n": 13.0},
        {"ts_ms": START + 2 * M, "series_id": sid, "n": 0.0},  # interior: zero documents
        {"ts_ms": START + 3 * M, "series_id": sid, "n": 4.0},
    ]
    assert dist.scheme == BucketScheme("linear", width=25.0, offset=0.0)
    assert dist.expr == SELECTOR and dist.caveats == ("query_chosen_buckets",)


async def test_by_builds_one_terms_level_and_fills_absent_groups_with_n_zero():
    def by(*groups):
        return {"sum_other_doc_count": 0,
                "buckets": [{"key": k, "doc_count": sum(n for _, n in h), "lat": hist(*h)}
                            for k, h in groups]}  # fmt: skip

    buckets = [
        tb(START, 15, __tn_by=by(("checkout", [(25.0, 10)]), ("search", [(0.0, 5)]))),
        tb(START + M, 10, __tn_by=by(("checkout", [(25.0, 10)]))),
    ]
    fake = FakeEs(search=search_response(buckets))
    dist = await fake.source().fetch_histogram(SELECTOR, ["service.name"], RNG, M)
    search = series_id("es", {"service.name": "search"})
    cols = {(c["series_id"], c["ts_ms"]): c["n"] for c in dist.columns.to_pylist()}
    assert cols[(search, START + M)] == 5.0 and cols[(search, START + 2 * M)] == 0.0
    inner = fake.searches()[0]["aggs"]["__tn_time"]["aggs"]
    assert inner["__tn_by"]["terms"] == {"field": "service.name", "size": 501}
    assert inner["__tn_by"]["aggs"] == {"lat": {"histogram": {"field": "event.duration", "interval": 25}}}
    assert {json.loads(r["labels"])["service.name"] for r in dist.series.to_pylist()} == {
        "checkout", "search"
    }


async def test_the_scheme_carries_the_histogram_offset():
    sel = json.dumps({"aggs": {"lat": {"histogram": {"field": "event.duration", "interval": 10,
                                                     "offset": 5}}}})  # fmt: skip
    buckets = [tb(START, 2, lat=hist((5.0, 2)))]
    dist = await FakeEs(search=search_response(buckets)).source().fetch_histogram(sel, (), RNG, M)
    assert dist.scheme == BucketScheme("linear", width=10.0, offset=5.0)
    assert dist.rows.to_pylist()[0]["bucket_hi"] == 15.0


async def test_more_than_one_by_field_is_refused():
    with pytest.raises(SourceError, match="at most one field"):
        await FakeEs().source().fetch_histogram(SELECTOR, ["a", "b"], RNG, M)


async def test_a_selector_that_is_not_a_histogram_is_refused():
    stats = json.dumps({"aggs": {"lat": {"stats": {"field": "event.duration"}}}})
    with pytest.raises(SourceError, match="needs a histogram aggregation"):
        await FakeEs().source().fetch_histogram(stats, (), RNG, M)


async def test_truncated_groups_are_refused():
    buckets = [tb(START, 9, __tn_by={"sum_other_doc_count": 4, "buckets": []})]
    with pytest.raises(LimitExceeded, match="other terms"):
        await FakeEs(search=search_response(buckets)).source().fetch_histogram(
            SELECTOR, ["service.name"], RNG, M
        )


async def test_a_partial_response_marks_the_distribution_span():
    buckets = [tb(START, 1, lat=hist((0.0, 1)))]
    resp = search_response(buckets, timed_out=True)
    dist = await FakeEs(search=resp).source().fetch_histogram(SELECTOR, (), RNG, M)
    assert dist.failed and dist.failed[0][2].startswith("PartialResponse:")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_es_histogram.py -q`
Expected: FAIL with `AttributeError: ... has no attribute 'fetch_histogram'`.

- [ ] **Step 3: Implement** (add to `src/telemetry_nerd/sources/elasticsearch.py`)

Imports: `from collections.abc import Iterator, Mapping, Sequence` and
`from telemetry_nerd.model.distribution import COLUMN_SCHEMA, DIST_SCHEMA, BucketScheme, DistResult`.

```python
    async def fetch_histogram(
        self, selector: str, by: Sequence[str], rng: TimeRange, step_ms: int
    ) -> DistResult:
        """Document counts per value bucket per query bucket. The value buckets are chosen by the
        query (interval, offset), not by the source: a finer interval can always be asked for."""
        q = EsQuery.parse(selector)
        if q.form != "histogram":
            raise SourceError(
                "query_distribution needs a histogram aggregation",
                hint='selector = {"query": {...}, "aggs": {"lat": {"histogram": {"field": '
                '"<numeric field>", "interval": 25}}}}; group with by',
            )
        if len(by) > 1:
            raise SourceError(
                f"by takes at most one field on an Elasticsearch source, got {list(by)}",
                hint="one terms level in v1 (multi-field grouping is later work)",
            )
        if by:
            q = q.with_group(by[0], self.limits.max_series + 1)
        resp, failed = await self._search(q, rng, step_ms)
        width = float(q.interval or 0)
        rows: list[tuple[int, str, float, float, float]] = []
        cols: dict[tuple[str, int], float] = {}
        labels_by_sid: dict[str, dict[str, str]] = {}
        interior = _interior(_time_buckets(resp))
        for b in interior:
            ts = _bucket_ts(b, step_ms)
            for labels, parent in _groups(q, b):
                sid = series_id(self.name, labels)
                labels_by_sid[sid] = labels
                leaves = (parent.get(q.metric_name) or {}).get("buckets")
                if not isinstance(leaves, list):
                    raise _malformed(f"bucket without its histogram {q.metric_name!r}")
                n = 0.0
                for leaf in leaves:
                    c = float(leaf.get("doc_count") or 0)
                    if c > 0:
                        lo = float(leaf["key"])
                        rows.append((ts, sid, lo, lo + width, c))
                        n += c
                cols[(sid, ts)] = n
        if len(labels_by_sid) > self.limits.max_series:
            raise LimitExceeded(
                f"histogram has {len(labels_by_sid)} series (limit {self.limits.max_series})",
                hint="group by a field with fewer values (by=[...]) or narrow the query",
            )
        for sid in labels_by_sid:  # a group absent from an interior query bucket: zero documents
            for b in interior:
                cols.setdefault((sid, _bucket_ts(b, step_ms)), 0.0)
        if len(rows) > self.limits.max_points:
            raise LimitExceeded(
                f"histogram has {len(rows)} non-empty cells (limit {self.limits.max_points})",
                hint="use a coarser step, a shorter range or a larger interval",
            )
        rows.sort(key=lambda r: (r[1], r[0], r[2]))
        ckeys = sorted(cols)
        sids = sorted(labels_by_sid)
        return DistResult(
            rows=pa.table(
                {
                    "ts_ms": [r[0] for r in rows],
                    "series_id": [r[1] for r in rows],
                    "bucket_lo": [r[2] for r in rows],
                    "bucket_hi": [r[3] for r in rows],
                    "count": [r[4] for r in rows],
                },
                schema=DIST_SCHEMA,
            ),
            columns=pa.table(
                {
                    "ts_ms": [ts for _, ts in ckeys],
                    "series_id": [sid for sid, _ in ckeys],
                    "n": [cols[k] for k in ckeys],
                },
                schema=COLUMN_SCHEMA,
            ),
            series=pa.table(
                {"series_id": sids, "labels": [labels_json(labels_by_sid[s]) for s in sids]},
                schema=SERIES_SCHEMA,
            ),
            scheme=BucketScheme("linear", width=width, offset=q.offset),
            expr=selector,
            caveats=("query_chosen_buckets",),
            failed=failed,
        )
```

Note: `columns` sorted by `(series_id, ts_ms)` matches the expected list order in the ungrouped test (one series).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_es_histogram.py -q`
Expected: PASS.

- [ ] **Step 5: Gates and commit**

```bash
just fmt && just lint && uv run pytest tests/unit -q
git add src/telemetry_nerd/sources/elasticsearch.py tests/unit/test_es_histogram.py
git commit -m "feat(es): fetch_histogram with query-chosen linear value buckets"
```

---

### Task 8: `discover` — `_field_caps` onto `Discovery`

**Files:**
- Modify: `src/telemetry_nerd/sources/elasticsearch.py`
- Create: `tests/fixtures/elasticsearch/field_caps_all.json`
- Test: `tests/unit/test_es_discover.py`

**Interfaces:**
- Consumes: `Discovery`, `MetricInfo` (`model/discovery.py`, with `naming` from Task 4); `NUMERIC_TYPES`, `LABEL_TYPES`, `_request`, `self._caps` (Tasks 5–6).
- Produces (used by Tasks 9, 15): `async ElasticsearchSource.discover() -> Discovery` with `naming="fields"`; module helpers `_meta(info, key) -> str | None`, `_field_unit(info) -> str | None`, `_field_type(info) -> str | None`.

- [ ] **Step 1: Write the fixture**

`tests/fixtures/elasticsearch/field_caps_all.json`:

```json
{
  "indices": ["tn-access-1", "tn-access-2"],
  "fields": {
    "_id": {"_id": {"type": "_id", "metadata_field": true, "searchable": true, "aggregatable": false}},
    "_index": {"_index": {"type": "_index", "metadata_field": true, "searchable": true, "aggregatable": true}},
    "@timestamp": {"date": {"type": "date", "searchable": true, "aggregatable": true}},
    "event.duration": {"long": {"type": "long", "searchable": true, "aggregatable": true, "meta": {"unit": ["nanos"]}}},
    "http.response.status_code": {"long": {"type": "long", "searchable": true, "aggregatable": true}},
    "http.response.bytes": {"long": {"type": "long", "searchable": true, "aggregatable": true, "meta": {"unit": ["byte"], "metric_type": ["counter"]}}},
    "host.cpu.pct": {"scaled_float": {"type": "scaled_float", "searchable": true, "aggregatable": true, "time_series_metric": "gauge"}},
    "service.name": {"keyword": {"type": "keyword", "searchable": true, "aggregatable": true}},
    "host.ip": {"ip": {"type": "ip", "searchable": true, "aggregatable": true}},
    "is_bot": {"boolean": {"type": "boolean", "searchable": true, "aggregatable": true}},
    "tier": {"constant_keyword": {"type": "constant_keyword", "searchable": true, "aggregatable": true}},
    "message": {"text": {"type": "text", "searchable": true, "aggregatable": false}},
    "message.keyword": {"keyword": {"type": "keyword", "searchable": true, "aggregatable": true}},
    "http": {"object": {"type": "object", "searchable": false, "aggregatable": false}},
    "latency": {
      "long": {"type": "long", "searchable": true, "aggregatable": true, "indices": ["tn-access-1"]},
      "keyword": {"type": "keyword", "searchable": true, "aggregatable": true, "indices": ["tn-access-2"]}
    },
    "spans": {"nested": {"type": "nested", "searchable": false, "aggregatable": false}},
    "spans.duration": {"long": {"type": "long", "searchable": true, "aggregatable": true}},
    "latency_hist": {"histogram": {"type": "histogram", "searchable": false, "aggregatable": true}}
  }
}
```

- [ ] **Step 2: Write the failing tests** (`tests/unit/test_es_discover.py`)

```python
"""ElasticsearchSource.discover: _field_caps mapped onto Discovery."""

import json

import httpx
import pytest

from telemetry_nerd.model.discovery import MetricInfo
from telemetry_nerd.sources.base import Limits

from .es_fake import CAPS, RNG, FakeEs, fixture, search_response


def fake_with(caps_file: dict) -> FakeEs:
    fake = FakeEs(caps=caps_file["fields"], indices=tuple(caps_file["indices"]))
    return fake


async def test_numeric_fields_are_metrics_and_aggregatable_keywords_are_labels():
    fake = fake_with(fixture("field_caps_all.json"))
    d = await fake.source().discover()
    assert d.metrics == (
        MetricInfo("event.duration", None, None, "ns"),
        MetricInfo("host.cpu.pct", "gauge", None, None),
        MetricInfo("http.response.bytes", "counter", None, "B"),
        MetricInfo("http.response.status_code", None, None, None),
    )
    assert d.label_names == ("host.ip", "is_bot", "message.keyword", "service.name", "tier")
    assert d.histograms == {} and d.cardinality is None
    assert d.naming == "fields"
    assert d.metadata_coverage == pytest.approx(3 / 4)
    assert d.caveats == ("cardinality_unavailable", "mapping_conflict:1",
                         "nested_fields_skipped:1", "histogram_fields_skipped:1")  # fmt: skip
    assert d.partial is True
    [req] = [r for r in fake.requests if r.url.path.endswith("/_field_caps")]
    assert req.url.params["fields"] == "*" and req.url.params["include_unmapped"] == "false"


async def test_a_mapping_without_numeric_fields_is_not_an_error():
    caps = {"service.name": CAPS["service.name"], "@timestamp": CAPS["@timestamp"]}
    d = await FakeEs(caps=caps).source().discover()
    assert d.metrics == () and d.label_names == ("service.name",)
    assert d.caveats == ("cardinality_unavailable", "no_numeric_fields")
    assert d.partial is False  # cardinality_unavailable alone is not partial


async def test_too_many_numeric_fields_are_truncated():
    src = fake_with(fixture("field_caps_all.json")).source(limits=Limits(max_metrics=2))
    d = await src.discover()
    assert [m.name for m in d.metrics] == ["event.duration", "host.cpu.pct"]
    assert "metrics_truncated:2/4" in d.caveats and d.partial is True


async def test_discover_clears_the_field_check_cache():
    fake = FakeEs(search=search_response([]))
    src = fake.source()
    stats = json.dumps({"aggs": {"lat": {"stats": {"field": "event.duration"}}}})
    await src.fetch(stats, RNG, 60_000)
    await src.discover()
    await src.fetch(stats, RNG, 60_000)
    assert fake.caps_requests() == ["event.duration", "*", "event.duration"]
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_es_discover.py -q`
Expected: FAIL with `AttributeError: ... has no attribute 'discover'`.

- [ ] **Step 4: Implement** (add to `src/telemetry_nerd/sources/elasticsearch.py`)

Import `from telemetry_nerd.model.discovery import Discovery, MetricInfo`. Module helpers:

```python
_UNITS = {"micros": "us", "nanos": "ns", "byte": "B"}  # mapping meta.unit -> catalog unit


def _meta(info: dict, key: str) -> str | None:
    """A mapping `meta` value; _field_caps returns each as a list (merged across indices)."""
    v = (info.get("meta") or {}).get(key)
    if isinstance(v, list):
        v = v[0] if v else None
    return str(v) if v else None


def _field_unit(info: dict) -> str | None:
    u = _meta(info, "unit")
    return _UNITS.get(u, u) if u else None


def _field_type(info: dict) -> str | None:
    t = info.get("time_series_metric") or _meta(info, "metric_type")
    return t if t in ("gauge", "counter") else None
```

Method:

```python
    async def discover(self) -> Discovery:
        """One _field_caps call: numeric aggregatable fields fill the metric role, aggregatable
        keyword/ip/boolean fields are the candidate `by` / terms fields."""
        self._caps.clear()  # field checks re-read the mapping after a discover
        caps = await self._request(
            "GET", f"/{self.index_pattern}/_field_caps",
            params={"fields": "*", "include_unmapped": "false"},
            timeout_s=self.limits.discover_timeout_s,
        )  # fmt: skip
        fields = caps.get("fields")
        if not isinstance(fields, dict):
            raise _malformed("field_caps response has no fields")
        nested = [f for f, t in fields.items() if isinstance(t, dict) and "nested" in t]
        numeric: list[MetricInfo] = []
        labels: list[str] = []
        conflicts = nested_skipped = histogram_skipped = 0
        for name, types in sorted(fields.items()):
            if name.startswith("_") or not isinstance(types, dict):
                continue  # metadata fields (_id, _index, ...)
            kinds = set(types) - {"object", "nested"}
            if not kinds:
                continue
            if any(name.startswith(p + ".") for p in nested):
                nested_skipped += 1  # needs a nested aggregation (not a v1 form)
                continue
            if len(kinds) > 1:
                conflicts += 1  # mapped with different types across the pattern's indices
                continue
            [kind] = kinds
            info = types[kind]
            if kind == "histogram":
                histogram_skipped += 1  # pre-aggregated histogram field: later work
                continue
            if not info.get("aggregatable"):
                continue
            if kind in NUMERIC_TYPES:
                numeric.append(MetricInfo(name, _field_type(info), None, _field_unit(info)))  # type: ignore[arg-type]
            elif kind in LABEL_TYPES:
                labels.append(name)
        caveats = ["cardinality_unavailable"]
        partial = False
        for count, code in ((conflicts, "mapping_conflict"), (nested_skipped, "nested_fields_skipped"),
                            (histogram_skipped, "histogram_fields_skipped")):  # fmt: skip
            if count:
                caveats.append(f"{code}:{count}")
                partial = True
        coverage = (
            sum(1 for m in numeric if m.unit or m.type) / len(numeric) if numeric else 1.0
        )
        if len(numeric) > self.limits.max_metrics:
            caveats.append(f"metrics_truncated:{self.limits.max_metrics}/{len(numeric)}")
            numeric = numeric[: self.limits.max_metrics]
            partial = True
        if not numeric:
            caveats.append("no_numeric_fields")  # not an error: the rate form needs no field
        return Discovery(
            metrics=tuple(numeric),
            label_names=tuple(labels),
            histograms={},
            cardinality=None,
            metadata_coverage=coverage,
            caveats=tuple(caveats),
            partial=partial,
            naming="fields",
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_es_discover.py -q`
Expected: PASS.

- [ ] **Step 6: Gates and commit**

```bash
just fmt && just lint && uv run pytest tests/unit -q
git add src/telemetry_nerd/sources/elasticsearch.py \
  tests/fixtures/elasticsearch/field_caps_all.json tests/unit/test_es_discover.py
git commit -m "feat(es): discover maps _field_caps onto Discovery (naming=fields)"
```

---

### Task 9: `catalog_learn` with `naming="fields"`

**Files:**
- Modify: `src/telemetry_nerd/core/workspace_service.py` (`catalog_learn`)
- Modify: `src/telemetry_nerd/catalog/rules.py` (`_DECLARED_UNITS`)
- Test: `tests/unit/test_catalog_learn.py`

**Interfaces:**
- Consumes: `Discovery.naming` (Task 4); `derive_claims`, `ClaimSpec.origin` (`catalog/rules.py`).
- Produces: `WorkspaceService.catalog_learn(source, discovery, actor)` writes only `origin == "metadata"` claims, no families, no pack claims and no pack relations when `discovery.naming == "fields"`; the returned summary keeps the same keys (`families` and `family_members` are 0, `relations_changed` 0). `normalize_unit("B") == "B"`.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_catalog_learn.py`)

```python
from telemetry_nerd.catalog.rules import normalize_unit

ES_DISCOVERY = Discovery(
    metrics=(
        MetricInfo("event.duration", None, None, "ns"),
        MetricInfo("requests_total"),  # a field name, not a counter by naming rule
        MetricInfo("http.response.bytes", "counter", None, "B"),
        MetricInfo("http_request_duration_seconds"),  # no _seconds rule, no pack claim
    ),
    label_names=("service.name",),
    histograms={},
    cardinality=None,
    metadata_coverage=0.5,
    caveats=("cardinality_unavailable",),
    partial=False,
    naming="fields",
)


async def test_field_paths_get_declared_metadata_claims_only(tmp_path):
    svc = make_service(tmp_path, FakeSource(name="default", discovery=ES_DISCOVERY))
    out = await svc.learn("default")
    assert (out["metrics"], out["families"], out["family_members"]) == (4, 0, 0)
    assert out["relations_changed"] == 0
    dur = svc.ws.catalog_entry("default", "event.duration").fields["unit"]
    assert (dur.value, dur.origin) == ("ns", "metadata")
    assert svc.ws.catalog_entry("default", "requests_total").fields == {}
    assert svc.ws.catalog_entry("default", "http_request_duration_seconds").fields == {}
    b = svc.ws.catalog_entry("default", "http.response.bytes")
    assert (b.fields["type"].value, b.fields["unit"].value) == ("counter", "B")
    origins = {c.origin for e in svc.ws.catalog_list("default")
               for cs in e.claims.values() for c in cs}  # fmt: skip
    assert origins == {"metadata"}


def test_declared_byte_unit_symbol_normalizes():
    assert normalize_unit("B") == "B"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_catalog_learn.py -q`
Expected: FAIL (`requests_total` gets a rule `type=counter` claim; `normalize_unit("B")` is `None`).

- [ ] **Step 3: Implement**

`src/telemetry_nerd/catalog/rules.py`: in `_DECLARED_UNITS`, after `"by": "B",` add `"b": "B",` (the declared symbol `B`, lowercased).

`src/telemetry_nerd/core/workspace_service.py`, `catalog_learn`: replace everything from `ts = self.clock()` through `relations_changed = self.relations.put_relations(pack_relations)` with:

```python
        ts = self.clock()
        if discovery.naming == "fields":
            # document field paths (Elasticsearch/OpenSearch) are not Prometheus names:
            # name-template families, T0 naming rules (_total -> counter, _seconds -> s) and
            # knowledge packs would misread them, so only declared metadata is claimed
            fam = {"families": 0, "members": 0}
            rows = [
                (m.name, spec.to_claim(ts))
                for m in discovery.metrics
                for spec in derive_claims(m.name, m, {})
                if spec.origin == "metadata"
            ]
            changed = self.catalog.put_claims_bulk(source, rows)
            relations_changed = 0
        else:
            # names that encode a dimension (airflow_ti_finish_<dag>_<task>) collapse into
            # families; a histogram base is a metric, not a dimension value
            bases = {b for b, k in discovery.histograms.items() if k == "classic"}
            detection = detect_families([n for n in names if n not in bases])
            rejected = self.families.rejected(source)
            assignment = {
                n: ta for n, ta in detection.assignment.items() if ta[0] not in rejected
            }
            fam = self.families.apply(source, detection, ts)
            rows = [
                (m.name, spec.to_claim(ts))
                for m in discovery.metrics
                if m.name not in assignment  # a family speaks for its members
                for spec in [
                    *derive_claims(m.name, m, discovery.histograms),
                    *self.packs.claims_for(m.name),
                ]
            ]
            rows += family_claims(discovery.metrics, assignment, detection, rejected, ts)
            changed = self.catalog.put_claims_bulk(source, rows)
            name_set = set(names)
            pack_relations = [
                RelationClaim(
                    source=source,
                    subject=m.name,
                    kind=spec.kind,  # type: ignore[arg-type]
                    object=spec.object,
                    origin="pack",
                    confidence=spec.confidence,
                    basis=spec.basis,
                    params=spec.params,
                    ts_ms=ts,
                )
                for m in discovery.metrics
                for spec in self.packs.relations_for(m.name)
                if spec.needs <= name_set and spec.object != m.name
            ]
            relations_changed = self.relations.put_relations(pack_relations)
```

(The `summary = {...}` block after it is unchanged; it reads `fam`, `changed`, `relations_changed`.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_catalog_learn.py -q`
Expected: PASS (existing tests in the file still pass: the PromQL branch is the old code).

- [ ] **Step 5: Gates and commit**

```bash
just fmt && just lint && uv run pytest tests/unit -q
git add src/telemetry_nerd/core/workspace_service.py src/telemetry_nerd/catalog/rules.py \
  tests/unit/test_catalog_learn.py
git commit -m "feat(catalog): field-path discoveries learn declared metadata only"
```

---

### Task 10: Wiring — factory dispatch, `source_connect`, Grafana hint

**Files:**
- Modify: `src/telemetry_nerd/core/bootstrap.py`
- Modify: `src/telemetry_nerd/mcp/server.py` (`source_connect` tool)
- Modify: `src/telemetry_nerd/sources/grafana.py` (`UNSUPPORTED_HINT`)
- Modify: `pyproject.toml` (integration marker text)
- Test: `tests/unit/test_bootstrap.py`, `tests/unit/test_mcp_sources.py`, `tests/unit/test_grafana.py`

**Interfaces:**
- Consumes: `ES_FLAVORS`, `SourceSpec` (Task 1); `ElasticsearchSource.from_spec` (Task 5).
- Produces: `core.bootstrap.source_factory(spec: SourceSpec) -> Source` (used by `build_service`'s `SourceRegistry` and by Task 15); MCP `source_connect(..., index_pattern: str | None = None, time_field: str | None = None)`; `auth_scheme` accepts `apikey`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_bootstrap.py`:

```python
from telemetry_nerd.core.bootstrap import source_factory
from telemetry_nerd.sources.elasticsearch import ElasticsearchSource
from telemetry_nerd.sources.promql import PromQLSource
from telemetry_nerd.sources.spec import SourceSpec


def test_the_factory_dispatches_on_flavor():
    es = source_factory(SourceSpec(name="logs", url="http://es:9200", flavor="opensearch",
                                   index_pattern="access-*", time_field="@timestamp"))  # fmt: skip
    assert isinstance(es, ElasticsearchSource) and es.flavor == "opensearch"
    prom = source_factory(SourceSpec(name="vm", url="http://vm:8428", flavor="victoriametrics"))
    assert isinstance(prom, PromQLSource)


async def test_runtime_es_sources_survive_rebuild(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    svc = build_service(settings)

    async def ok(self) -> dict:
        return {"reachable": True}

    monkeypatch.setattr(ElasticsearchSource, "probe", ok)
    await svc.source_connect(SourceSpec(name="logs", url="http://es:9200", flavor="elasticsearch",
                                        index_pattern="access-*", time_field="@timestamp"))  # fmt: skip
    rebuilt = build_service(settings)
    assert isinstance(rebuilt.sources["logs"], ElasticsearchSource)
```

Append to `tests/unit/test_mcp_sources.py`:

```python
from telemetry_nerd.core.bootstrap import source_factory
from telemetry_nerd.sources.elasticsearch import ElasticsearchSource


async def test_connect_an_elasticsearch_source_with_an_api_key(tmp_path, monkeypatch):
    async def ok(self) -> dict:
        return {"reachable": True, "distribution": "elasticsearch", "version": "8.15.3"}

    monkeypatch.setattr(ElasticsearchSource, "probe", ok)
    monkeypatch.setenv("ES_API_KEY", "abc==")
    svc = make_service(tmp_path, factory=source_factory)
    r = await call(build_mcp(svc, "http://x"), "source_connect", {
        "name": "logs", "url": "https://es.example:9200", "flavor": "elasticsearch",
        "index_pattern": "access-logs-*", "time_field": "@timestamp",
        "auth_env": "ES_API_KEY", "auth_scheme": "apikey",
    })  # fmt: skip
    assert not r.is_error, text(r)
    out = json.loads(text(r))
    assert (out["source"]["flavor"], out["source"]["index_pattern"]) == (
        "elasticsearch", "access-logs-*"
    )
    assert out["source"]["auth"]["scheme"] == "apikey"
    assert isinstance(svc.sources["logs"], ElasticsearchSource)


async def test_an_es_source_without_time_field_is_refused_with_the_reason(tmp_path):
    svc = make_service(tmp_path, factory=source_factory)
    r = await call(build_mcp(svc, "http://x"), "source_connect", {
        "name": "logs", "url": "https://es.example:9200", "flavor": "elasticsearch",
        "index_pattern": "access-logs-*",
    })  # fmt: skip
    assert r.is_error and "time_field" in text(r)


async def test_source_connect_documents_the_es_flavors(tmp_path):
    async with Client(build_mcp(make_service(tmp_path), "http://x")) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    doc = tools["source_connect"].description or ""
    for word in ("elasticsearch", "opensearch", "index_pattern", "time_field", "apikey"):
        assert word in doc, word
```

Append to `tests/unit/test_grafana.py` (it already builds `GrafanaDatasource` entries; use the same constructor as the existing unsupported-type test at line ~112, with `type="elasticsearch"`):

```python
def test_an_elasticsearch_datasource_points_at_the_direct_connection():
    from telemetry_nerd.sources.grafana import UNSUPPORTED_HINT

    assert "not Prometheus" in UNSUPPORTED_HINT
    assert 'source_connect(url=..., flavor="elasticsearch"' in UNSUPPORTED_HINT
    assert "index_pattern" in UNSUPPORTED_HINT and "time_field" in UNSUPPORTED_HINT
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_bootstrap.py tests/unit/test_mcp_sources.py tests/unit/test_grafana.py -q`
Expected: FAIL (`source_factory` missing; `source_connect` has no `index_pattern`; hint text unchanged).

- [ ] **Step 3: Implement**

`src/telemetry_nerd/core/bootstrap.py`:

```python
from telemetry_nerd.sources.base import Source
from telemetry_nerd.sources.elasticsearch import ElasticsearchSource
from telemetry_nerd.sources.spec import ES_FLAVORS, SourceSpec


def source_factory(spec: SourceSpec) -> Source:
    """The live adapter for a spec (resolves its secret reference now: raises MissingSecret)."""
    if spec.flavor in ES_FLAVORS:
        return ElasticsearchSource.from_spec(spec)
    return PromQLSource.from_spec(spec)
```

and in `build_service`: `sources = SourceRegistry(wcon, source_factory)`.

`src/telemetry_nerd/sources/grafana.py`:

```python
UNSUPPORTED_HINT = (
    "not Prometheus/MetricsQL-compatible, so it cannot be queried through Grafana yet; "
    "Elasticsearch/OpenSearch connect directly: source_connect(url=..., flavor=\"elasticsearch\", "
    "index_pattern=..., time_field=...) (see telemetry-nerd-sgb for other adapters)"
)
```

`src/telemetry_nerd/mcp/server.py`, `source_connect`: add parameters after `timezone`:

```python
        index_pattern: str | None = None,
        time_field: str | None = None,
```

add `"index_pattern": index_pattern, "time_field": time_field,` to the dict in the inner `spec()` builder, and replace the docstring's first line and the `flavor` / `auth_scheme` sentences:

```text
        """Connect a source at runtime (no daemon restart): Prometheus-compatible (Prometheus,
        Thanos, Mimir, VictoriaMetrics) or an Elasticsearch / OpenSearch cluster.
        ...
        flavor: "victoriametrics" (MetricsQL rollup), "prometheus" (also works on VM, Thanos,
        Mimir), "elasticsearch" or "opensearch" (one adapter serves both; flavor only changes how
        the version is read); ignored when grafana+uid detect it.
        Elasticsearch / OpenSearch: url = the cluster base URL (e.g. https://es.example:9200, or
        a reverse-proxy path prefix); index_pattern (required) = the indices this source reads,
        e.g. "access-logs-*" (lowercase; a comma-separated list may use *; never bare * or
        _all); time_field (required, no default) = the date field documents are bucketed by,
        e.g. "@timestamp". resolution: the finest query step accepted (default 1s; documents have
        no series interval). Through Grafana (grafana+uid) is not supported yet.
        ...
        auth_scheme: bearer | basic ("user:pass") | apikey (an Elasticsearch API key as
        Elasticsearch/Kibana hand it out, the encoded id:api_key, sent as "ApiKey <key>").
```

Keep every other sentence of the existing docstring. (The glossary test bans "scrape interval" without "scraped" on the same line: the new text uses "series interval" / "query step".)

`pyproject.toml`: integration marker text becomes
`"integration: needs Docker (testcontainers VictoriaMetrics, Elasticsearch, OpenSearch); required in CI (\`just test-integration\`)"`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_bootstrap.py tests/unit/test_mcp_sources.py tests/unit/test_grafana.py tests/unit/test_glossary_terms.py -q`
Expected: PASS.

- [ ] **Step 5: Gates and commit**

```bash
just fmt && just lint && uv run pytest tests/unit -q
git add src/telemetry_nerd/core/bootstrap.py src/telemetry_nerd/mcp/server.py \
  src/telemetry_nerd/sources/grafana.py pyproject.toml tests/unit/test_bootstrap.py \
  tests/unit/test_mcp_sources.py tests/unit/test_grafana.py
git commit -m "feat(es): connect Elasticsearch/OpenSearch sources with source_connect"
```

---

### Task 11: Service dispatch — `query` and `query_distribution` on ES sources

**Files:**
- Modify: `src/telemetry_nerd/core/service.py` (`query`, new `_query_es`, `query_distribution`, `fraction_over`)
- Modify: `src/telemetry_nerd/core/summary.py` (`summarize_distribution`)
- Modify: `src/telemetry_nerd/mcp/server.py` (`query`, `query_distribution`, `fraction_over` docstrings)
- Modify: `tests/unit/fakes.py` (add `FakeEsSource`)
- Test: `tests/unit/test_service_es.py`

**Interfaces:**
- Consumes: `EsQuery` (Task 2); `BucketScheme.lower_inclusive`, `comparison` (Task 3); `language_of`, `DatasetStore.put(..., caveats, query_language)`, `put_distribution(..., query_language)` (Task 4).
- Produces:
  - `TelemetryService.query(...)` dispatches on `language_of(src)`; `es_dsl` goes to `TelemetryService._query_es(src, source, expr, start, end, step, actor) -> dict` (same `{dataset, summary}` shape)
  - ES datasets: `meta.query_language == "es_dsl"`; caveats `zero_is_no_documents` (rate forms) / `approximate_percentile` (percentile); percentile → `representation="quantile"`, `quantile=q`, `n_min=min_samples(q)`
  - `query_distribution` on ES: step floor = `src.resolution_ms` (no `increase()`), `query_language="es_dsl"`
  - `fraction_over(...)` result gains top-level `"compare": ">" | ">="`
  - `summarize_distribution` for a `linear` scheme: `counts` = `"doc_count per step (documents per value bucket); additive over time and adjacent buckets"`, plus `"bucket_resolution": "the query's bucket width (re-query with a smaller interval for more detail)"`
  - `tests/unit/fakes.py: class FakeEsSource(name="es", resolution_ms=15_000)` with `query_language = "es_dsl"`, `calls: list[tuple[str, str]]`, `fetch`, `fetch_values`, `fetch_histogram`, `probe`, `discover`, `scrape_interval` (used by Task 12)

- [ ] **Step 1: Add the fake** (append to `tests/unit/fakes.py`)

Add `from telemetry_nerd.model.distribution import COLUMN_SCHEMA, DIST_SCHEMA, BucketScheme, DistResult` to its imports, then:

```python
class FakeEsSource:
    """An Elasticsearch-like source (query_language es_dsl) serving fixed rows for service
    tests: fetch -> a varying documents-per-second line, fetch_values -> a percentile with n=40,
    fetch_histogram -> [0, 25) x30 and [25, 50) x10 per column (a linear scheme)."""

    semantics = None
    query_language = "es_dsl"

    def __init__(self, name: str = "es", resolution_ms: int = 15_000) -> None:
        self.name = name
        self.identity = f"fake-es|{name}|{resolution_ms}"
        self.resolution_ms = resolution_ms
        self.calls: list[tuple[str, str]] = []

    async def probe(self) -> dict:
        return {"reachable": True, "distribution": "elasticsearch", "version": "8.15.3"}

    async def discover(self) -> Discovery:
        return Discovery((), (), {}, None, 1.0, ("cardinality_unavailable",), False, naming="fields")

    async def scrape_interval(self, selector: str, at_ms: int | None = None) -> int | None:
        return None

    def _line(self, rng: TimeRange, step_ms: int, values, count: int) -> FetchResult:
        ts = list(range(rng.start_ms, rng.end_ms + 1, step_ms))
        vals = [values(i) for i in range(len(ts))]
        sid = series_id(self.name, {})
        buckets = pa.table(
            {"ts_ms": ts, "series_id": [sid] * len(ts), "avg": vals, "min": vals, "max": vals,
             "count": [count] * len(ts)},
            schema=BUCKET_SCHEMA,
        )  # fmt: skip
        series = pa.table({"series_id": [sid], "labels": [labels_json({})]}, schema=SERIES_SCHEMA)
        return FetchResult(buckets, series)

    async def fetch(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult:
        self.calls.append(("fetch", expr))
        return self._line(rng, step_ms, lambda i: 2.0 + (i % 7) * 0.1, 1)

    async def fetch_values(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult:
        self.calls.append(("fetch_values", expr))
        return self._line(rng, step_ms, lambda i: 0.25, 40)

    async def fetch_histogram(self, selector, by, rng: TimeRange, step_ms: int) -> DistResult:
        self.calls.append(("fetch_histogram", selector))
        ts = list(range(rng.start_ms, rng.end_ms + 1, step_ms))
        sid = series_id(self.name, {})
        k = len(ts)
        rows = pa.table(
            {"ts_ms": ts * 2, "series_id": [sid] * (2 * k), "bucket_lo": [0.0] * k + [25.0] * k,
             "bucket_hi": [25.0] * k + [50.0] * k, "count": [30.0] * k + [10.0] * k},
            schema=DIST_SCHEMA,
        )  # fmt: skip
        cols = pa.table({"ts_ms": ts, "series_id": [sid] * k, "n": [40.0] * k},
                        schema=COLUMN_SCHEMA)  # fmt: skip
        series = pa.table({"series_id": [sid], "labels": [labels_json({})]}, schema=SERIES_SCHEMA)
        return DistResult(rows, cols, series, BucketScheme("linear", width=25.0, offset=0.0),
                          selector, ("query_chosen_buckets",))  # fmt: skip
```

- [ ] **Step 2: Write the failing tests** (`tests/unit/test_service_es.py`)

```python
"""The service on an Elasticsearch source: query / query_distribution dispatch (no PromQL)."""

import json

import pytest

from telemetry_nerd.analysis.exprkind import min_samples
from telemetry_nerd.sources.base import SourceError

from .fakes import FakeEsSource, make_service

QS = {"query_string": {"query": "service.name:checkout"}}
RATE = json.dumps({"query": QS})
STATS = json.dumps({"query": QS, "aggs": {"lat": {"stats": {"field": "event.duration"}}}})
P99 = json.dumps({"query": QS, "aggs": {"p": {"percentiles": {"field": "event.duration",
                                                             "percents": [99]}}}})  # fmt: skip
HIST = json.dumps({"query": QS, "aggs": {"lat": {"histogram": {"field": "event.duration",
                                                              "interval": 25}}}})  # fmt: skip


@pytest.fixture
def svc_es(tmp_path):
    svc = make_service(tmp_path)
    es = FakeEsSource()
    svc.sources.attach("es", es)
    return svc, es


async def test_a_rate_query_is_a_bucket_agg_dataset_in_es_dsl(svc_es):
    svc, es = svc_es
    out = await svc.query(RATE, start="now-1h", end="now", step="1m", source="es")
    meta = svc.datasets.meta(out["dataset"])
    assert (meta.representation, meta.query_language, meta.expr) == ("bucket_agg", "es_dsl", RATE)
    assert "zero_is_no_documents" in meta.source_caveats
    assert {c for c in es.calls} == {("fetch", RATE)}
    assert out["summary"]["series_count"] == 1


async def test_stats_has_no_zero_caveat(svc_es):
    svc, _ = svc_es
    out = await svc.query(STATS, start="now-1h", step="1m", source="es")
    assert "zero_is_no_documents" not in svc.datasets.meta(out["dataset"]).source_caveats


async def test_a_percentile_is_a_quantile_dataset_read_per_query_bucket(svc_es):
    svc, es = svc_es
    out = await svc.query(P99, start="now-1h", step="1m", source="es")
    meta = svc.datasets.meta(out["dataset"])
    assert (meta.representation, meta.quantile, meta.n_min) == ("quantile", 0.99, min_samples(0.99))
    assert "approximate_percentile" in meta.source_caveats
    assert {m for m, _ in es.calls} == {"fetch_values"}


async def test_a_histogram_query_points_at_query_distribution(svc_es):
    svc, es = svc_es
    with pytest.raises(SourceError, match="distribution") as e:
        await svc.query(HIST, start="now-1h", step="1m", source="es")
    assert "query_distribution" in e.value.hint and es.calls == []


async def test_an_unsupported_expr_is_a_source_error(svc_es):
    svc, _ = svc_es
    with pytest.raises(SourceError, match="not JSON"):
        await svc.query("rate(http_requests_total[5m])", start="now-1h", source="es")


async def test_a_step_finer_than_the_source_accepts_is_refused(svc_es):
    svc, _ = svc_es
    with pytest.raises(SourceError, match="finer than"):
        await svc.query(RATE, start="now-1h", step="5s", source="es")


async def test_query_distribution_steps_down_to_the_finest_query_step(svc_es):
    svc, es = svc_es
    # a PromQL source needs >= 2 series intervals per step (increase()); documents need none
    out = await svc.query_distribution(HIST, start="now-1h", step="15s", source="es")
    meta = svc.datasets.meta(out["dataset"])
    assert (meta.representation, meta.query_language) == ("distribution", "es_dsl")
    s = out["summary"]
    assert s["buckets"] == "fixed-width buckets of 25 (offset 0), chosen by the query; each [lo, hi)"
    assert s["counts"].startswith("doc_count per step")
    assert "smaller interval" in s["bucket_resolution"]
    assert "query_chosen_buckets" in s["caveats"]
    assert es.calls == [("fetch_histogram", HIST)]


async def test_fraction_over_on_query_chosen_buckets_is_at_or_above(svc_es):
    svc, _ = svc_es
    out = await svc.query_distribution(HIST, start="now-1h", step="1m", source="es")
    f = svc.fraction_over(out["dataset"], 25.0)
    assert f["compare"] == ">="
    [s] = f["series"]
    assert s["exact"] is True and s["fraction"] == pytest.approx(0.25)


async def test_prometheus_distributions_keep_greater_than(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.query_distribution("lat_seconds_bucket", start="now-1h", step="1m")
    assert svc.fraction_over(out["dataset"], 0.1)["compare"] == ">"


async def test_promql_queries_are_unchanged_and_tagged_promql(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.query("up", start="now-1h", step="1m")
    assert svc.datasets.meta(out["dataset"]).query_language == "promql"


async def test_tool_descriptions_document_the_es_forms(tmp_path):
    from mcp import Client

    from telemetry_nerd.mcp.server import build_mcp

    async with Client(build_mcp(make_service(tmp_path), "http://x")) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    q = tools["query"].description or ""
    assert "Elasticsearch" in q and "percentiles" in q and "value_count" in q
    d = tools["query_distribution"].description or ""
    assert "Elasticsearch" in d and "[lo, hi)" in d
    assert ">=" in (tools["fraction_over"].description or "")
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_service_es.py -q`
Expected: FAIL (the PromQL pipeline runs `analyze()` on the JSON; `compare` missing; descriptions lack ES text).

- [ ] **Step 4: Implement**

`src/telemetry_nerd/core/service.py` imports:

```python
from telemetry_nerd.analysis.fraction import comparison, fraction_over, wilson
from telemetry_nerd.sources.base import (
    LimitExceeded, Source, SourceError, SourceUnavailable, language_of,
)  # fmt: skip
from telemetry_nerd.sources.esquery import EsQuery
```

In `query()`, right after `src = self._source(source)`:

```python
        if language_of(src) == "es_dsl":
            return await self._query_es(src, source, expr, start, end, step, actor)
```

New method (next to `query`):

```python
    async def _query_es(
        self, src: Source, source: str, expr: str, start: str, end: str, step: str, actor: Actor
    ) -> dict:
        """An Elasticsearch/OpenSearch query: the native request body, no PromQL pipeline (no
        family or $__rate_interval expansion, no PromQL analyze, no mergeability checks, no
        settle_unobserved: query buckets are exact tiles with no lookback fill)."""
        q = EsQuery.parse(expr)  # an unsupported shape is a SourceError listing the forms
        if q.form == "histogram":
            raise SourceError(
                "a histogram aggregation is a distribution, not a time series",
                hint="use query_distribution(selector=<this expr>, source=...) for counts per "
                "value bucket",
            )
        now = self.clock()
        rng = TimeRange(parse_time(start, now), parse_time(end, now))
        step_ms = auto_step(rng, src.resolution_ms) if step == "auto" else parse_duration(step)
        if step_ms < src.resolution_ms:
            raise SourceError(
                f"step {format_duration(step_ms)} is finer than source {src.name!r} accepts "
                f"({format_duration(src.resolution_ms)})",
                hint=f"use step >= {format_duration(src.resolution_ms)} or auto",
            )
        rng = _align_within_limit(rng, step_ms, "buckets")
        caveats: list[str] = []
        representation, qv, n_min = "bucket_agg", None, None
        if q.form == "percentile":
            # a percentile is per query bucket, never rolled up; n is the adapter's value_count
            representation, qv = "quantile", q.q
            n_min = min_samples(q.q) if q.q is not None else None
            caveats.append("approximate_percentile")
            result = await self.cache.get(
                src.identity, f"values|{expr}", rng, step_ms,
                lambda r: src.fetch_values(expr, r, step_ms),
            )  # fmt: skip
        else:
            if q.form in ("rate", "field_rate"):
                caveats.append("zero_is_no_documents")
            result = await self.cache.get(
                src.identity, expr, rng, step_ms, lambda r: src.fetch(expr, r, step_ms)
            )
        meta = self.datasets.put(
            source=src.name, expr=expr, rng=rng, step_ms=step_ms,
            resolution_ms=src.resolution_ms, result=result, representation=representation,
            quantile=qv, n_min=n_min, semantics_flags=_semantics_flags(src), caveats=caveats,
            query_language="es_dsl",
        )  # fmt: skip
        summary = self._time_summary(meta, result, now)
        if summary.get("series_count") == 0:
            summary["empty_result"] = absence_note(src.name, expr, summary.get("range"))
        self.workspaces.note_source(source)  # only once the query succeeded
        self.log.append(actor, "dataset.created", meta.id, {"expr": expr})
        return {"dataset": meta.id, "summary": summary}
```

`query_distribution()`: replace the `floor`/step check with

```python
        es = language_of(src) == "es_dsl"
        # increase() needs two samples per window; document counts need none
        floor = src.resolution_ms if es else 2 * src.resolution_ms
        step_ms = (
            auto_step(rng, floor, DIST_TARGET_COLUMNS) if step == "auto" else parse_duration(step)
        )
        if step_ms < floor:
            raise SourceError(
                f"step {format_duration(step_ms)} is shorter than "
                + (f"the finest query step ({format_duration(floor)})" if es
                   else f"two series intervals ({format_duration(floor)})"),
                hint=f"use step >= {format_duration(floor)} or auto"
                + ("" if es else "; counts come from increase() per step"),
            )  # fmt: skip
```

and pass `query_language=language_of(src)` to `self.datasets.put_distribution(...)`.

`fraction_over()`: change the final return to

```python
        return mark_statistics(
            {"dataset": dataset_id, "x": x, "compare": comparison(dist.scheme.lower_inclusive),
             "series": out},
            self.datasets, [dataset_id],
        )  # fmt: skip
```

`src/telemetry_nerd/core/summary.py`, `summarize_distribution`, replace the `"counts"` entry:

```python
    linear = (meta.scheme or {}).get("kind") == "linear"
    base = {
        ...
        "counts": (
            "as produced by the code; additive over time and adjacent buckets"
            if meta.code_node
            else "doc_count per step (documents per value bucket); additive over time and "
            "adjacent buckets"
            if linear
            else "increase() per step; additive over time and adjacent buckets"
        ),
        **(
            {"bucket_resolution": "the query's bucket width (re-query with a smaller interval "
                                  "for more detail)"}
            if linear else {}
        ),  # fmt: skip
        **produced_by(meta),
    }
```

`src/telemetry_nerd/mcp/server.py` docstrings:

- `query`: first line becomes `"""Fetch a query as a dataset of min/max/avg/count buckets: PromQL/MetricsQL, or an Elasticsearch/OpenSearch request body.` and add before `Returns {dataset, summary}`:

```text
        Elasticsearch / OpenSearch sources: expr is the native request body (JSON), shown
        verbatim, with no time range in it (time is start/end/step; the adapter adds the
        date_histogram). Forms: {"query": {...}} alone = documents per second, e.g.
        {"query": {"query_string": {"query": "service.name:checkout AND NOT url.path:\"/healthz\""}}};
        "aggs" with ONE of value_count (a field's values per second), stats (mean/min/max and
        count per query bucket: latency, e.g. {"aggs": {"lat": {"stats": {"field":
        "event.duration"}}}}), percentiles with exactly one "percents" value (a quantile,
        TDigest-approximate, never rolled up); optionally inside ONE terms aggregation (one
        series per term; documents without the field are in no series unless terms sets
        `missing`; a too-small size is refused, never truncated). Query buckets are
        [t - step, t): a document on a boundary lands one bucket later than a Prometheus
        sample. An empty interior bucket reads as 0/s (caveat zero_is_no_documents). Rate forms
        carry no unit: pass unit (e.g. req/s) to show when you know it.
```

- `query_distribution`: add to the `selector` paragraph:

```text
          Elasticsearch / OpenSearch: the request body with ONE histogram aggregation, e.g.
          {"query": {...}, "aggs": {"lat": {"histogram": {"field": "event.duration",
          "interval": 25}}}}; group with by (at most one field), not terms. Its buckets are
          chosen by the query (interval, offset), edges [lo, hi): re-query with a smaller
          interval for more detail. Counts are doc_count per query bucket (exact).
```

- `fraction_over`: add `On query-chosen buckets (Elasticsearch, [lo, hi) edges) the answer is the fraction at or above x: compare is ">=" (">" otherwise).`

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_service_es.py tests/unit/test_service_distribution.py tests/unit/test_glossary_terms.py -q`
Expected: PASS.

- [ ] **Step 6: Gates and commit**

```bash
just fmt && just lint && uv run pytest tests/unit -q
git add src/telemetry_nerd/core/service.py src/telemetry_nerd/core/summary.py \
  src/telemetry_nerd/mcp/server.py tests/unit/fakes.py tests/unit/test_service_es.py
git commit -m "feat(es): service dispatches query and query_distribution on query_language"
```

---

### Task 12: PromQL-only features on ES datasets (show, units, profiles, overlays, analyze)

**Files:**
- Modify: `src/telemetry_nerd/core/service.py` (`show`, `show_auto`, `_verify_unit`, new `_es_unit`, `_time_summary`, `_overlays`, `analyze_profiled`, `analyze_reference`)
- Modify: `src/telemetry_nerd/core/profiles.py` (`ProfileService.target`)
- Test: `tests/unit/test_service_es.py`

**Interfaces:**
- Consumes: `FakeEsSource` (Task 11); `EsQuery` (Task 2); `language_of` (Task 4); `Discovery(naming="fields")` + `catalog_learn` (Task 9).
- Produces: `TelemetryService._es_unit(meta: DatasetMeta) -> tuple[str | None, str | None]`; `ProfileService.target` raises `SourceError("operating profiles are PromQL-only ...")` for an `es_dsl` source (so `operating_profile` refuses, `cached`/`request` return `None`, `_seasonal_centre` reports `unavailable`); `panel_data(...)["overlays"]["normal"]` is unavailable with a reason on ES datasets.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_service_es.py`)

```python
from telemetry_nerd.model.discovery import Discovery, MetricInfo


def learn_duration_unit(svc, unit="ms"):
    svc.ws.catalog_learn("es", Discovery(
        metrics=(MetricInfo("event.duration", None, None, unit),), label_names=(), histograms={},
        cardinality=None, metadata_coverage=1.0, caveats=(), partial=False, naming="fields",
    ))  # fmt: skip


async def test_show_takes_the_unit_of_the_aggregated_field_from_the_catalog(svc_es):
    svc, _ = svc_es
    learn_duration_unit(svc)
    out = await svc.query(STATS, start="now-1h", step="1m", source="es")
    res = svc.show(out["dataset"], "How slow is checkout?")
    assert res.panel.spec["y"]["unit"] == "ms"
    assert "event.duration" in res.panel.spec["y"]["unit_provenance"]


async def test_show_leaves_rate_forms_unitless_and_accepts_a_given_unit(svc_es):
    svc, _ = svc_es
    out = await svc.query(RATE, start="now-1h", step="1m", source="es")
    assert svc.show(out["dataset"], "How busy is checkout?").panel.spec["y"].get("unit") is None
    given = svc.show(out["dataset"], "How busy is checkout?", unit="req/s")
    assert given.panel.spec["y"]["unit"] == "req/s"
    assert not [i for i in given.issues if i.rule == "raw_counter"]


async def test_show_auto_never_rewrites_an_es_query_as_a_rate(svc_es):
    svc, es = svc_es
    out = await svc.query(RATE, start="now-1h", step="1m", source="es")
    calls = len(es.calls)
    res = await svc.show_auto(out["dataset"], "How busy is checkout?")
    assert res.panel.spec.get("auto") is None and len(es.calls) == calls


async def test_operating_profiles_are_refused_on_es_sources(svc_es):
    svc, _ = svc_es
    with pytest.raises(SourceError, match="PromQL-only"):
        await svc.operating_profile(RATE, source="es")


async def test_the_normal_band_overlay_is_unavailable_with_a_reason(svc_es):
    svc, _ = svc_es
    out = await svc.query(RATE, start="now-1h", step="1m", source="es")
    panel = svc.show(out["dataset"], "How busy is checkout?").panel
    normal = svc.panel_data(panel.id, 800)["overlays"]["normal"]
    assert normal["available"] is False and "PromQL-only" in normal["reason"]


async def test_analyze_on_an_es_dataset_says_no_profile_can_shape_the_centre(svc_es):
    svc, _ = svc_es
    out = await svc.query(RATE, start="now-1h", step="1m", source="es")
    res = await svc.analyze_profiled(out["dataset"])
    assert res["seasonal_centre"]["status"] == "unavailable"
    assert "PromQL-only" in res["seasonal_centre"]["reason"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_service_es.py -q`
Expected: FAIL (unit is `None`; `operating_profile` tries a PromQL profile; overlays have no reason).

- [ ] **Step 3: Implement**

`src/telemetry_nerd/core/profiles.py`, top of `ProfileService.target` (after the `is_code_expr` check):

```python
        if language_of(self.sources.get(source)) == "es_dsl":
            raise SourceError(
                "operating profiles are PromQL-only (v1): not available on an "
                "Elasticsearch/OpenSearch source",
                hint="compare time ranges with compare_seasonal or analyze(baseline=...) instead",
            )
```

with `from telemetry_nerd.sources.base import language_of` (keep the existing `SourceError` import).

`src/telemetry_nerd/core/service.py`:

New helper next to `_verify_unit`:

```python
    def _es_unit(self, meta: DatasetMeta) -> tuple[str | None, str | None]:
        """The unit of an Elasticsearch dataset: the aggregated field's catalog unit for the
        stats, percentile and histogram forms; none for the rate forms (a document is not
        necessarily a request: the caller passes unit, e.g. req/s)."""
        q = EsQuery.parse(meta.expr)
        if q.form in ("rate", "field_rate") or q.field is None:
            return None, None
        # the catalog's claims only: catalog_facts falls back to Prometheus name rules, which
        # must never be applied to a field path (spec: name-based inference is skipped)
        claims = self.ws.catalog.claims_for(meta.source, q.field)
        unit = facts_from_claims(claims).unit if claims else None
        return (unit, f"catalog unit of field {q.field}") if unit else (None, None)
```

with `from telemetry_nerd.catalog.rules import facts_from_claims` added to the service imports (`CatalogStore.claims_for(source, metric) -> list[Claim]` is in `catalog/store.py`).

`show()`: after the `if not unit and meta.code_node and meta.unit:` block:

```python
        es = meta.query_language == "es_dsl"
        if not unit and es:
            unit, provenance = self._es_unit(meta)
```

change the `auto_spec(...)` call's `expr=` to `expr=None if (meta.code_node or es) else meta.expr,`; add `and not es` to the `raw_counters(...)` condition (`if spec.layers[0].mark == "line+envelope" and not meta.derived and not meta.code_node and not es`) and to the `self.profiles.request(...)` condition at the end of `show`.

`_verify_unit()`: first branch becomes

```python
        if meta.query_language == "es_dsl":
            expected, why = self._es_unit(meta)
        elif meta.representation == "distribution":
            expected, why = None, None  # the unit labels the value axis of buckets, not a line
        else:
            ...  # unchanged
```

`show_auto()`: add `and meta.query_language == "promql"` to `auto_ok`.

`_time_summary()`: `if meta.representation == "bucket_agg" and meta.query_language == "promql" and looks_like_histogram(meta.expr):`.

`_overlays()`: replace the two lines `profile = self.profiles.cached(...)` … `normal = normal_payload(...)` with

```python
        if meta.query_language == "es_dsl":
            normal = {
                "available": False,
                "reason": "operating profiles are PromQL-only (v1): no normal band on an "
                "Elasticsearch/OpenSearch source",
            }
        else:
            profile = self.profiles.cached(meta.source, meta.expr)
            window = format_duration(profile.window_ms) if profile else ""
            normal = normal_payload(profile, series, step_ms, window)
```

`analyze_profiled()`: guard the PromQL-only sibling/ratio fetches (they parse `expr` as PromQL):

```python
        if self.datasets.meta(dataset_id).query_language == "promql":
            await self.diagnostics.fetch_sibling(dataset_id, "claude")
            await self.diagnostics.fetch_ratio(dataset_id, "claude")
```

and the same guard around the `for d in [...]` loop body in `analyze_reference()`:

```python
        if self.datasets.meta(dataset_id).query_language == "promql":
            for d in [dataset_id, *(r["dataset"] for r in ref["refs"])]:
                await self.diagnostics.fetch_sibling(d, actor)
                await self.diagnostics.fetch_ratio(d, actor)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_service_es.py tests/unit/test_overlays.py tests/unit/test_profiles.py -q`
Expected: PASS.

- [ ] **Step 5: Gates and commit**

```bash
just fmt && just lint && uv run pytest tests/unit -q
git add src/telemetry_nerd/core/service.py src/telemetry_nerd/core/profiles.py \
  tests/unit/test_service_es.py
git commit -m "feat(es): PromQL-only features skip or refuse ES datasets with reasons"
```

---

### Task 13: Little's law with an ES source and a PromQL concurrency source

**Files:**
- Modify: `src/telemetry_nerd/core/littles_ops.py`
- Modify: `src/telemetry_nerd/core/service.py` (`LittlesOps(...)` construction, `check_littles_law`)
- Modify: `src/telemetry_nerd/mcp/server.py` (`check_littles_law` tool)
- Modify: `tests/unit/littles_sim.py` (add `EsSimSource`)
- Test: `tests/unit/test_littles_es.py`

**Interfaces:**
- Consumes: `EsQuery` (Task 2); `language_of` (Task 4); the ES query path of `service.query` (Task 11).
- Produces: `LittlesOps(..., language: Callable[[str], str] = lambda s: "promql")` (new last constructor parameter); `LittlesOps.check(..., concurrency_source: str | None = None, ...)`; `LittlesOps._es_roles(arrival: str, latency: str) -> tuple[EsQuery, EsQuery]`; `LittlesOps._grouped(dataset_id, by, grid, step, value: str = "avg")` (`"avg" | "sum" | "count"`); cfg keys `counter_form = "es_buckets"`, `latency_from_stats: bool`, `sources: {"signals", "concurrency"}`; assumption `sources` (status `assumed`) when the sources differ; MCP `check_littles_law(..., concurrency_source: str | None = None)`; `tests/unit/littles_sim.py: class EsSimSource(SimSource)`.

- [ ] **Step 1: Add the simulated ES source** (append to `tests/unit/littles_sim.py`)

```python
class EsSimSource(SimSource):
    """The arrival and latency signals as an Elasticsearch access-log index serves them: the
    rate form (documents per second) and the stats form (mean latency and count of the requests
    completing in each query bucket), from the same simulation as SimSource."""

    query_language = "es_dsl"

    def __init__(self, sims: dict[str, dict], start_ms: int, **kw) -> None:
        super().__init__(sims, start_ms, **kw)
        self.identity = "es-sim"

    async def fetch(self, expr, rng, step_ms):
        import pyarrow as pa

        from telemetry_nerd.model.series import (
            BUCKET_SCHEMA,
            SERIES_SCHEMA,
            FetchResult,
            labels_json,
            series_id,
        )

        assert step_ms == self.resolution_ms, "the sim serves the scrape step only"
        self.exprs.append(expr)
        stats = '"stats"' in expr
        d = lambda key: sum(np.diff(np.r_[0.0, m[key]]) for m in self.sims.values())  # noqa: E731
        count = d("count")
        values = d("sum") / np.where(count > 0, count, 1) if stats else d("counter") / self.scrape_s
        sid = series_id(self.name, {})
        rows = []
        for k in range(count.size):
            ts = self.start_ms + int((k + 1) * self.scrape_s * 1000)
            if not rng.start_ms <= ts <= rng.end_ms or (stats and count[k] == 0):
                continue
            rows.append((ts, float(values[k]), int(count[k]) if stats else 1))
        vals = [r[1] for r in rows]
        buckets = pa.table(
            {"ts_ms": [r[0] for r in rows], "series_id": [sid] * len(rows), "avg": vals,
             "min": vals, "max": vals, "count": [r[2] for r in rows]},
            schema=BUCKET_SCHEMA,
        )  # fmt: skip
        series = pa.table({"series_id": [sid] if rows else [], "labels": [labels_json({})] if rows else []},
                          schema=SERIES_SCHEMA)  # fmt: skip
        return FetchResult(buckets, series)
```

- [ ] **Step 2: Write the failing tests** (`tests/unit/test_littles_es.py`)

```python
"""check_littles_law with arrivals and latency from an Elasticsearch source and concurrency from
a Prometheus source (cross-source; spec: Little's law on an ES source)."""

import asyncio
import json

import pytest

from .fakes import NOW, make_service
from .littles_sim import CONCURRENCY, EsSimSource, SimSource, simulate

START = NOW - 3_600_000
QS = {"query_string": {"query": "service.name:checkout"}}
ARRIVALS = json.dumps({"query": QS})
LATENCY = json.dumps({"query": QS, "aggs": {"lat": {"stats": {"field": "event.duration"}}}})
P99 = json.dumps({"query": QS, "aggs": {"p": {"percentiles": {"field": "event.duration",
                                                             "percents": [99]}}}})  # fmt: skip


def _setup(tmp_path):
    sims = {"i0": simulate(20, rates=[(0.0, 6.0)], c=10)}
    es = EsSimSource(sims, START)  # registered as "default" by make_service
    prom = SimSource(sims, START)
    prom.name, prom.identity = "prom", "prom-sim"
    svc = make_service(tmp_path, source=es)
    svc.sources.attach("prom", prom)
    return svc, es, prom


def _run(svc, **kw):
    args = {"arrival_rate": ARRIVALS, "latency": LATENCY, "concurrency": CONCURRENCY,
            "concurrency_source": "prom", "latency_unit": "s"}  # fmt: skip
    return asyncio.run(
        svc.check_littles_law(start="now-1h", end="now", window="5m", **{**args, **kw})
    )


def test_es_arrivals_and_latency_with_a_prometheus_gauge_are_consistent(tmp_path):
    svc, es, prom = _setup(tmp_path)
    out = _run(svc)
    assert out["verdict"] == "consistent"
    ds = out["datasets"]
    assert ds["latency_sum"] == ds["latency_count"]  # one stats dataset feeds both roles
    assert set(es.exprs) == {ARRIVALS, LATENCY}  # (the series cache may split a fetch in chunks)
    assert all(CONCURRENCY in e for e in prom.exprs)
    assert prom.probes and not es.probes  # the gauge's series interval is probed on its source
    assumptions = {a["name"]: a for a in out["assumptions"]}
    assert assumptions["sources"]["status"] == "assumed"
    assert "documents" in assumptions["sources"]["detail"]
    assert "concurrency from prom" in assumptions["sources"]["detail"]
    assert "[t − step, t)" in assumptions["window_alignment"]["detail"]
    assert assumptions["label_sets"]["status"] == "ok"


def test_concurrency_is_never_read_from_an_es_source(tmp_path):
    svc, _, _ = _setup(tmp_path)
    with pytest.raises(ValueError, match="PromQL source"):
        _run(svc, concurrency_source="default")


def test_grouping_across_two_sources_is_refused(tmp_path):
    svc, _, _ = _setup(tmp_path)
    with pytest.raises(ValueError, match="two sources"):
        _run(svc, by=["service"])


def test_a_percentile_latency_is_refused(tmp_path):
    svc, _, _ = _setup(tmp_path)
    with pytest.raises(ValueError, match="looks like a percentile"):
        _run(svc, latency=P99)


def test_arrival_rate_must_be_the_rate_form(tmp_path):
    svc, _, _ = _setup(tmp_path)
    with pytest.raises(ValueError, match="rate form"):
        _run(svc, arrival_rate=LATENCY)


def test_a_binding_is_refused_on_an_es_source(tmp_path):
    svc, _, _ = _setup(tmp_path)
    with pytest.raises(ValueError, match="binding"):
        _run(svc, binding="checkout")


async def test_check_littles_law_documents_concurrency_source(tmp_path):
    from mcp import Client

    from telemetry_nerd.mcp.server import build_mcp

    async with Client(build_mcp(make_service(tmp_path), "http://x")) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    doc = tools["check_littles_law"].description or ""
    assert "concurrency_source" in doc and "Elasticsearch" in doc
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_littles_es.py -q`
Expected: FAIL (`check()` got an unexpected keyword argument `concurrency_source`).

- [ ] **Step 4: Implement**

`src/telemetry_nerd/core/littles_ops.py` imports:

```python
from telemetry_nerd.sources.base import SourceError
from telemetry_nerd.sources.esquery import EsQuery
```

Constructor: add last parameter `language: Callable[[str], str] = lambda s: "promql",` and `self._language = language  # the source's query language: promql | es_dsl`.

New method (after `_latency`):

```python
    def _es_roles(self, arrival: str, latency: str) -> tuple[EsQuery, EsQuery]:
        """arrival_rate and latency as native Elasticsearch queries: the rate form (documents per
        second) and the stats form (W = the mean of the documents' duration field)."""
        try:
            arr, lat = EsQuery.parse(arrival), EsQuery.parse(latency)
        except SourceError as e:
            raise ValueError(f"{e} (hint: {e.hint})") from e
        if lat.form == "percentile":
            raise ValueError(
                f"latency={latency!r} looks like a percentile: Little's law needs the MEAN "
                "latency W; a percentile is not a mean (hint: a stats aggregation on the duration "
                'field, e.g. {"aggs": {"lat": {"stats": {"field": "event.duration"}}}})'
            )
        if lat.form != "stats":
            raise ValueError(
                f"latency on an Elasticsearch source is a stats aggregation on the duration "
                f"field (got the {lat.form} form)"
            )
        if arr.form != "rate":
            raise ValueError(
                "arrival_rate on an Elasticsearch source is the rate form: a query with no aggs "
                f"(documents per second); got the {arr.form} form"
            )
        if arr.group is not None or lat.group is not None:
            raise ValueError(
                "terms grouping in arrival_rate/latency is not supported: the check runs on one "
                "total when its signals span two sources"
            )
        return arr, lat
```

`check()`: add `concurrency_source: str | None = None,` after `concurrency`. Replace the body from `roles, by, bound = self._roles(...)` through the end of the `ds` loop with:

```python
        conc_source = concurrency_source or source
        es = self._language(source) == "es_dsl"
        if self._language(conc_source) == "es_dsl":
            raise ValueError(
                f"concurrency_source={conc_source!r} is an Elasticsearch/OpenSearch source: the "
                "in-flight gauge is read from a PromQL source in v1 (hint: "
                "concurrency_source=<the Prometheus source with the gauge>)"
            )
        if es and binding:
            raise ValueError(
                "binding is not supported on an Elasticsearch/OpenSearch source (bindings come "
                "from PromQL rules): pass arrival_rate, latency and concurrency"
            )
        if conc_source != source and by:
            raise ValueError(
                "by is not supported when the signals come from two sources (an Elasticsearch "
                "field and a Prometheus label are different names; joining them needs a label "
                "mapping, later work): drop by"
            )
        roles, by, bound = self._roles(
            source, binding,
            {"arrival_rate": arrival_rate, "latency": latency, "concurrency": concurrency}, by,
        )  # fmt: skip
        for b in by:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", b):
                raise ValueError(f"by: {b!r} is not a label name")
        conc_name, conc_sel = _parse("concurrency", roles["concurrency"])
        if getattr(self._facts(conc_source, conc_name), "type", None) == "counter":
            raise ValueError(
                f"concurrency={conc_name!r} is a counter: L is the number in flight, a gauge "
                "(hint: bind an in-flight / active-requests gauge)"
            )
        if es:
            _, lat_q = self._es_roles(roles["arrival_rate"], roles["latency"])
            lat = {"base": str(lat_q.field), "selector": "", "native": False}
        else:
            lat = self._latency(source, roles["latency"])
            arr_name, arr_sel = _parse("arrival_rate", roles["arrival_rate"])
            arr_type = getattr(self._facts(source, arr_name), "type", None)
        factor, unit, unit_basis = self._unit(source, lat["base"], latency_unit)
        res = max(self._resolution(source), self._resolution(conc_source))
        scrape_ms, scrape_note = await self._probe_scrape(
            conc_source, f"{conc_name}{conc_sel}", res, start, end
        )
        rng, win, step, k = _windows(start, end, window, res, self._clock(), scrape_note)
        skip = 1 + (-(-parse_duration(warmup) // step) if warmup else 0)
        if es:
            # one stats fetch feeds both latency roles (sum = avg x count); query buckets are
            # tiles [t - step, t): no lookback, per-bucket counts divided by the step
            arr_is_rate = True
            exprs = {
                "arrival_rate": roles["arrival_rate"], "latency_sum": roles["latency"],
                "latency_count": roles["latency"], "concurrency": f"sum ({conc_name}{conc_sel})",
            }  # fmt: skip
            counter_form, lookback_ms, tile_s = "es_buckets", 0, step / 1000
            matchers = {"concurrency": _matchers(conc_sel)}
            if arrivals == "auto":
                arrivals = "unknown"
        else:
            arr_is_rate = arr_type == "gauge"
            # a histogram named as the arrival signal: its count (classic _count, native
            # histogram_count) counts completions
            arr_hist = arr_type == "histogram" or arr_name == lat["base"]
            arr_native = arr_hist and self.native_histogram(source, arr_name)
            if arr_is_rate:
                arr_form = "gauge"
            elif arr_native:
                arr_form = "native"
            elif arr_hist:
                arr_form = "classic"
            else:
                arr_form = "counter"
            tiled = self._flavor(source) == "victoriametrics"
            exprs = _exprs(
                by, lat, arr_name, arr_sel, arr_form, f"{conc_name}{conc_sel}",
                format_duration(res) if tiled else None,
            )  # fmt: skip
            # how much further back the counters' window reaches than the gauge reading it is
            # compared with: 0 with increase() tiles (VM); rate(x[ri]) at t covers (t - ri, t]
            # (VM also takes the sample before it), the gauge is read at the last scrape <= t,
            # on average half a scrape back: (ri - scrape) / 2 (measured on VM v1.137: ri / 2
            # behind the bucket's middle at sub-steps of 5s-60s)
            counter_form = "increase_tiles" if tiled else "rate_interval"
            lookback_ms = 0 if tiled else max(0, (rate_interval_ms(step, res) - res) // 2)
            tile_s = res / 1000 if tiled else None
            matchers = {
                "arrival_rate": _matchers(arr_sel), "latency": _matchers(lat["selector"]),
                "concurrency": _matchers(conc_sel),
            }  # fmt: skip
            if arrivals == "auto":
                same = arr_name == f"{lat['base']}_count" or arr_name == lat["base"]
                arrivals = "completions" if same else "unknown"
        ds: dict[str, str] = {}
        fetched: dict[tuple[str, str], str] = {}
        for role, expr in exprs.items():
            role_source = conc_source if role == "concurrency" else source
            if (role_source, expr) not in fetched:
                out = await self._query(
                    expr, start=str(rng.start_ms), end=str(rng.end_ms),
                    step=format_duration(step), source=role_source, actor=actor,
                )  # fmt: skip
                fetched[(role_source, expr)] = out["dataset"]
            ds[role] = fetched[(role_source, expr)]
```

Then replace the `cfg = {...}` literal's affected keys (keep the others as they are):

```python
            "lookback_ms": lookback_ms,
            "counter_form": counter_form,
            "tile_s": tile_s,
            "latency_from_stats": es,
            "sources": {"signals": source, "concurrency": conc_source},
            ...
            "matchers": matchers,
```

(and delete the old `if arrivals == "auto": ...` block that followed the loop: it moved into the branches above).

`_grouped()`: add parameter `value: str = "avg"`; replace `avg = g["avg"].to_numpy().astype(float)` / `v[idx[keep]] = avg[keep]` with

```python
            avg = g["avg"].to_numpy().astype(float)
            cnt = g["count"].to_numpy().astype(float)
            vals = {"avg": avg, "sum": avg * cnt, "count": cnt}[value]
            v[idx[keep]] = vals[keep]
```

`run()`: the dataset loop becomes

```python
        stats = cfg.get("latency_from_stats")
        for role, did in cfg["datasets"].items():
            value = {"latency_sum": "sum", "latency_count": "count"}.get(role, "avg") if stats else "avg"
            per_role[role], nm, lacking[role] = self._grouped(did, by, grid, step, value)
            names.update(nm)
```

`_assumptions()`:

- In the `units` entry replace the arrival phrase with

```python
                + (" (documents per second per query bucket)" if cfg.get("counter_form") == "es_buckets"
                   else " (the arrival signal is a gauge, used as a rate)" if cfg["arrival_is_rate"]
                   else " from the counter's increase")
```

- Replace `tiles = cfg.get("counter_form") == "increase_tiles"` and the inner counters sentence of `window_alignment` with

```python
        form = cfg.get("counter_form")
        counters = (
            "arrivals and latency as document counts per query bucket [t − step, t) (tiles: no "
            "lookback)" if form == "es_buckets" else
            f"counters as increase() over {format_duration(cfg['resolution_ms'])} tiles "
            "that partition them exactly and end at the scrape the gauge is read at: no "
            "lookback" if form == "increase_tiles" else
            "rate() looks back "
            f"{format_duration(cfg['lookback_ms']) if cfg['lookback_ms'] else '0s'} further "
            "than the gauge reading: a bias bound in the interval"
        )  # fmt: skip
```

and use `+ counters` in the `window_alignment` detail where the old conditional expression was.

- Append before `return out`:

```python
        srcs = cfg.get("sources") or {}
        if srcs and srcs["signals"] != srcs["concurrency"]:
            out.append({
                "name": "sources", "status": "assumed",
                "detail": (
                    f"arrivals and latency from {srcs['signals']}"
                    + (" (documents, query buckets [t − step, t))"
                       if cfg.get("counter_form") == "es_buckets" else "")
                    + f", concurrency from {srcs['concurrency']} (gauge read at the last scrape ≤ t)"
                ),
            })  # fmt: skip
```

`src/telemetry_nerd/core/service.py`:

- `LittlesOps(...)` construction: append `lambda s: language_of(self._source(s)),` as the last argument.
- `check_littles_law`:

```python
    async def check_littles_law(self, actor: Actor = "claude", **kw) -> dict:
        """L vs lambda W per window and group, with a propagated interval (czt.2)."""
        await self.ensure_resolution(kw.get("source", "default"))
        if kw.get("concurrency_source"):
            await self.ensure_resolution(kw["concurrency_source"])
        return await self.littles.check(actor=actor, **kw)
```

`src/telemetry_nerd/mcp/server.py`, `check_littles_law` tool: add parameter `concurrency_source: str | None = None,` after `source`, pass `concurrency_source=concurrency_source` into `service.check_littles_law(...)`, and add to the docstring after the Signals paragraph:

```text
        concurrency_source: the source the concurrency gauge is read from (default `source`).
        Elasticsearch / OpenSearch access logs give arrivals and latency, never concurrency:
        source=<the ES source>, arrival_rate = a query with no aggs (documents per second, e.g.
        {"query": {"query_string": {"query": "service.name:checkout"}}}), latency = the same
        query with a stats aggregation on the duration field ({"query": ..., "aggs": {"lat":
        {"stats": {"field": "event.duration"}}}}; W = its mean), concurrency = the in-flight
        gauge name, concurrency_source = the Prometheus source it lives in. by is refused when
        the signals span two sources; binding is refused on an ES source; latency_unit comes
        from the field's catalog unit (mapping meta.unit) when known.
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_littles_es.py tests/unit/test_littles_service.py tests/unit/test_littles.py tests/unit/test_littles_compact.py -q`
Expected: PASS (the existing Little's law tests are unchanged in behaviour).

- [ ] **Step 6: Gates and commit**

```bash
just fmt && just lint && uv run pytest tests/unit -q
git add src/telemetry_nerd/core/littles_ops.py src/telemetry_nerd/core/service.py \
  src/telemetry_nerd/mcp/server.py tests/unit/littles_sim.py tests/unit/test_littles_es.py
git commit -m "feat(littles): arrivals and latency from an ES source, concurrency_source for the gauge"
```

---

### Task 14: UI text — linear schemes, CCDF `≥`, new caveats

**Files:**
- Modify: `ui/src/lib/api.ts` (`BucketSchemeInfo`)
- Modify: `ui/src/lib/panelNotes.ts` (`CAVEATS`, `describeShown`)
- Test: `ui/src/lib/panelNotes.test.ts`

**Interfaces:**
- Consumes: `BucketScheme.to_dict()` with `kind: "linear"`, `width`, `offset`, `description` (Task 3), caveat codes `zero_is_no_documents`, `approximate_percentile`, `query_chosen_buckets` (Tasks 7, 11).
- Produces: `BucketSchemeInfo.width?: number | null`, `BucketSchemeInfo.offset?: number | null`; `describeShown` wording for `linear` schemes (distribution heatmap, histogram bars, CCDF); `caveatText` for the three new codes.

- [ ] **Step 1: Write the failing tests** (append to `ui/src/lib/panelNotes.test.ts`)

```ts
describe("query-chosen (linear) bucket schemes", () => {
  const desc = "fixed-width buckets of 25 (offset 0), chosen by the query; each [lo, hi)";
  const scheme = { kind: "linear", edges: [], schema: null, per_decade: null, width: 25, offset: 0, description: desc };
  const classic = { kind: "classic", edges: [0.1, 1], schema: null, per_decade: null, description: "classic le buckets: 0.1, 1" };
  const dist = { representation: "distribution", quantile: null } as const;

  it("says heatmap bins are chosen by the query", () => {
    expect(describeShown({ ...dist, scheme }, "1m", "heatmap")).toBe(
      `Document counts per 1m column and value bucket (colour); bins chosen by the query (${desc}).`,
    );
  });
  it("says histogram bars are the query's buckets", () => {
    expect(describeShown({ ...dist, scheme }, "1m", "histogram")).toContain(`bars are the query's buckets (${desc})`);
  });
  it("words the CCDF as at or above on lower-inclusive buckets", () => {
    expect(describeShown({ ...dist, scheme }, "1m", "histogram", "ccdf")).toMatch(/^Share of observations at or above each value, P\(X ≥ x\)/);
  });
  it("keeps the Prometheus wording for source buckets", () => {
    expect(describeShown({ ...dist, scheme: classic }, "1m", "heatmap")).toContain("from increase() of the histogram; bins are the source buckets");
    expect(describeShown({ ...dist, scheme: classic }, "1m", "histogram", "ccdf")).toMatch(/^Share of observations above each value/);
  });
  it("explains the Elasticsearch caveats in plain words", () => {
    expect(caveatText("zero_is_no_documents")).toContain("no traffic");
    expect(caveatText("approximate_percentile")).toContain("TDigest");
    expect(caveatText("query_chosen_buckets")).toContain("smaller interval");
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ui && npx vitest run src/lib/panelNotes.test.ts; cd ..`
Expected: FAIL (heatmap text says "from increase() of the histogram"; caveat keys returned verbatim).

- [ ] **Step 3: Implement**

`ui/src/lib/api.ts`:

```ts
export interface BucketSchemeInfo {
  kind: string; edges: number[]; schema: number | null; per_decade: number | null;
  /** linear (Elasticsearch): bucket width and offset chosen by the query */
  width?: number | null; offset?: number | null;
  description: string;
}
```

`ui/src/lib/panelNotes.ts` — add to `CAVEATS`:

```ts
  zero_is_no_documents: () =>
    "A query bucket with no matching documents reads as 0 per second: Elasticsearch cannot tell no traffic from documents that were never ingested.",
  approximate_percentile: () =>
    "Elasticsearch/OpenSearch percentiles are TDigest estimates (approximate), not exact order statistics.",
  query_chosen_buckets: () =>
    "The value buckets were chosen by the query (its interval), not by the source: re-query with a smaller interval for more detail.",
```

Above `describeShown` add:

```ts
/** Elasticsearch histogram buckets: chosen by the query, edges [lo, hi). */
const isLinear = (d: { scheme?: DatasetMeta["scheme"] }): boolean => d.scheme?.kind === "linear";
```

In `describeShown`, replace the `ccdf`, `histogram` and plain `distribution` branches:

```ts
  if (mark === "ccdf") {
    return isLinear(d)
      ? "Share of observations at or above each value, P(X ≥ x) (log-log; the query's buckets are [lo, hi)), exact at bucket edges and bounded inside a bucket; hover reads a threshold, click pins it."
      : "Share of observations above each value (log-log), exact at bucket edges and bounded inside a bucket; hover reads a threshold, click pins it.";
  }
  if (kind === "histogram") {
    const bars = isLinear(d) ? "the query's buckets" : "the source buckets";
    return `Share of observations per value bucket, summed over each selected time range (whole ${step} query buckets); bars are ${bars} (${d.scheme?.description ?? "unknown scheme"}).`;
  }
```

```ts
  if (d.representation === "distribution") {
    return isLinear(d)
      ? `Document counts per ${step} column and value bucket (colour); bins chosen by the query (${d.scheme?.description ?? "unknown scheme"}).`
      : `Counts per ${step} column and value bucket (colour), from increase() of the histogram; bins are the source buckets (${d.scheme?.description ?? "unknown scheme"}).`;
  }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ui && npx vitest run && cd .. && just ui-check && uv run pytest tests/unit/test_glossary_terms.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/src/lib/api.ts ui/src/lib/panelNotes.ts ui/src/lib/panelNotes.test.ts
git commit -m "feat(ui): panel notes for query-chosen buckets, P(X ≥ x) and ES caveats"
```

---

### Task 15: Integration — Elasticsearch 8.x and OpenSearch 2.x containers (primary validation tier)

**Files:**
- Modify: `tests/integration/conftest.py` (session fixtures `es_url`, `os_url`)
- Create: `tests/integration/es_seed.py`
- Create: `tests/integration/test_es_containers.py`

**Interfaces:**
- Consumes: `build_service`, `Settings`, `SourceSpec` with ES flavors, `svc.source_connect`, `svc.query`, `svc.query_distribution`, `svc.fraction_over`, `svc.learn`, `svc.ws.catalog_entry`, `svc.datasets.get/get_distribution/meta` (Tasks 1–11); `ElasticsearchSource.fetch_histogram(selector, by, rng, step_ms)` (Task 7) through the service.
- Produces: CI-required container coverage of every query form, grouping, `query_distribution`, `probe` on both flavors, `discover`, and the field-check / truncation errors end to end.

The seeded data (per minute `m` of 30, minute 10 left empty; timestamps inside the minute, never on a boundary):

| documents | service.name | status | event.duration (ns) | url.path |
|---|---|---|---|---|
| 10 | checkout | 200 | 40 ms = 40,000,000 | /cart |
| 2 | checkout | 500 | 100 ms = 100,000,000 | /pay |
| 5 | search | 200 | 20 ms = 20,000,000 | /q |

Expected per query bucket (1 m step) for `service.name:checkout`: rate 12/60 = 0.2/s (0 in minute 10); stats count 12, avg 50 ms, min 40 ms, max 100 ms (no row in minute 10); grouped rate 200 → 10/60, 500 → 2/60 (both 0 in minute 10); histogram (interval 25 ms) checkout `[25, 50) ms` ×10 and `[100, 125) ms` ×2, search `[0, 25) ms` ×5.

- [ ] **Step 1: Add the container fixtures** (append to `tests/integration/conftest.py`)

```python
# Same Docker Hub caveat as VM_IMAGE; Elastic's own registry for Elasticsearch.
ES_IMAGE = "docker.elastic.co/elasticsearch/elasticsearch:8.15.3"
OS_IMAGE = "opensearchproject/opensearch:2.17.1"


def _wait_green(url: str, container: DockerContainer, what: str, deadline_s: float = 180) -> None:
    deadline = time.monotonic() + deadline_s
    while True:
        try:
            r = httpx.get(f"{url}/_cluster/health?wait_for_status=yellow&timeout=1s", timeout=3)
            if r.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            container.stop()
            raise RuntimeError(f"{what} did not become healthy")
        time.sleep(1)


@pytest.fixture(scope="session")
def es_url():
    container = (
        DockerContainer(ES_IMAGE)
        .with_exposed_ports(9200)
        .with_env("discovery.type", "single-node")
        .with_env("xpack.security.enabled", "false")
        .with_env("ES_JAVA_OPTS", "-Xms512m -Xmx512m")
    )
    container.start()
    url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(9200)}"
    _wait_green(url, container, "Elasticsearch")
    yield url
    container.stop()


@pytest.fixture(scope="session")
def os_url():
    container = (
        DockerContainer(OS_IMAGE)
        .with_exposed_ports(9200)
        .with_env("discovery.type", "single-node")
        .with_env("DISABLE_SECURITY_PLUGIN", "true")
        .with_env("DISABLE_INSTALL_DEMO_CONFIG", "true")
        .with_env("OPENSEARCH_JAVA_OPTS", "-Xms512m -Xmx512m")
    )
    container.start()
    url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(9200)}"
    _wait_green(url, container, "OpenSearch")
    yield url
    container.stop()
```

- [ ] **Step 2: Write the seeding helper** (`tests/integration/es_seed.py`)

```python
"""A synthetic ECS-shaped access-log index with known per-minute counts and latencies."""

from __future__ import annotations

import json

import httpx

from telemetry_nerd.model.time import now_ms

INDEX = "tn-access-1"
PATTERN = "tn-access-*"
MINUTES = 30
GAP_MINUTE = 10  # no documents: an interior empty query bucket
MS = 1_000_000  # event.duration is in nanoseconds (ECS)

MAPPING = {"mappings": {"properties": {
    "@timestamp": {"type": "date"},
    "http": {"properties": {"response": {"properties": {"status_code": {"type": "long"}}}}},
    "event": {"properties": {"duration": {"type": "long", "meta": {"unit": "nanos"}}}},
    "service": {"properties": {"name": {"type": "keyword"}}},
    "url": {"properties": {"path": {"type": "keyword"}}},
    "message": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
}}}  # fmt: skip


#: the series cache fetches in chunks of 720 query buckets (12 h at a 1 m step, aligned to the
#: epoch): a chunk edge next to GAP_MINUTE would make that interior zero a chunk's leading or
#: trailing bucket (no row). Seeding 1 h into a 12 h chunk keeps the 30 minutes in one chunk on
#: every run, so the tests never depend on the wall clock.
CHUNK_MS = 720 * 60_000


def base_ms() -> int:
    """Start of the first seeded minute: 1 h into the 12 h cache chunk that began 24-36 h ago."""
    return (now_ms() - 24 * 3_600_000) // CHUNK_MS * CHUNK_MS + 3_600_000


def _doc(ts: int, service: str, status: int, duration_ns: int, path: str) -> dict:
    return {"@timestamp": ts, "service": {"name": service},
            "http": {"response": {"status_code": status}}, "event": {"duration": duration_ns},
            "url": {"path": path}, "message": f"{service} {path} {status}"}  # fmt: skip


def docs(base: int) -> list[dict]:
    out = []
    for m in range(MINUTES):
        if m == GAP_MINUTE:
            continue
        t = base + m * 60_000
        out += [_doc(t + 500 + j * 1000, "checkout", 200, 40 * MS, "/cart") for j in range(10)]
        out += [_doc(t + 30_500 + j * 1000, "checkout", 500, 100 * MS, "/pay") for j in range(2)]
        out += [_doc(t + 45_500 + j * 1000, "search", 200, 20 * MS, "/q") for j in range(5)]
    return out


def seed(url: str, base: int) -> None:
    httpx.delete(f"{url}/{INDEX}", timeout=30)  # rerun-safe: 404 when absent
    httpx.put(f"{url}/{INDEX}", json=MAPPING, timeout=30).raise_for_status()
    lines = "".join(
        json.dumps({"index": {"_index": INDEX}}) + "\n" + json.dumps(d) + "\n" for d in docs(base)
    )
    r = httpx.post(f"{url}/_bulk?refresh=true", content=lines,
                   headers={"Content-Type": "application/x-ndjson"}, timeout=60)  # fmt: skip
    r.raise_for_status()
    assert not r.json()["errors"], r.json()
```

- [ ] **Step 3: Write the end-to-end tests** (`tests/integration/test_es_containers.py`)

```python
"""Elasticsearch 8.x and OpenSearch 2.x end to end through the service (the epic's primary
validation tier; spec: Testing)."""

import json

import pytest

from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.sources.base import LimitExceeded, SourceError
from telemetry_nerd.sources.spec import SourceSpec
from tests.integration.es_seed import GAP_MINUTE, MINUTES, MS, PATTERN, base_ms, seed

pytestmark = pytest.mark.integration

CHECKOUT = {"query_string": {"query": "service.name:checkout"}}


def expr(**doc) -> str:
    return json.dumps(doc)


@pytest.fixture(params=["elasticsearch", "opensearch"])
def cluster(request):
    return request.param, request.getfixturevalue(
        "es_url" if request.param == "elasticsearch" else "os_url"
    )


@pytest.fixture
async def seeded(cluster, tmp_path):
    flavor, url = cluster
    base = base_ms()
    seed(url, base)
    svc = build_service(Settings(data_dir=tmp_path / "d", source_url="http://127.0.0.1:9"))
    out = await svc.source_connect(SourceSpec(name="es", url=url, flavor=flavor,
                                              index_pattern=PATTERN, time_field="@timestamp"))  # fmt: skip
    return {"svc": svc, "connect": out, "base": base, "flavor": flavor, "url": url}


def window(base: int) -> dict:
    # query buckets ending base + 1 m .. base + 30 m cover the seeded minutes 0 .. 29
    return {"start": str(base + 60_000), "end": str(base + MINUTES * 60_000), "step": "1m",
            "source": "es"}  # fmt: skip


def ends(base: int) -> list[int]:
    return [base + (m + 1) * 60_000 for m in range(MINUTES)]


async def test_probe_reads_each_flavor(seeded):
    status = seeded["connect"]["status"]
    assert status["reachable"] is True and status["distribution"] == seeded["flavor"]
    assert status["version"] and "flavor_mismatch" not in status and status["indices"] >= 1


async def test_opensearch_connected_as_elasticsearch_reports_the_mismatch(cluster, tmp_path):
    flavor, url = cluster
    if flavor != "opensearch":
        pytest.skip("the mismatch is OpenSearch reached as elasticsearch")
    seed(url, base_ms())
    svc = build_service(Settings(data_dir=tmp_path / "d", source_url="http://127.0.0.1:9"))
    out = await svc.source_connect(SourceSpec(name="os", url=url, flavor="elasticsearch",
                                              index_pattern=PATTERN, time_field="@timestamp"))  # fmt: skip
    assert "opensearch" in out["status"]["flavor_mismatch"]


async def test_rate_is_documents_per_second_with_an_interior_zero(seeded):
    svc, base = seeded["svc"], seeded["base"]
    out = await svc.query(expr(query=CHECKOUT), **window(base))
    _, res = svc.datasets.get(out["dataset"])
    got = {r["ts_ms"]: r["avg"] for r in res.buckets.to_pylist()}
    assert sorted(got) == ends(base)
    for m, ts in enumerate(ends(base)):
        assert got[ts] == pytest.approx(0.0 if m == GAP_MINUTE else 12 / 60), m


async def test_field_rate_counts_the_field_per_second(seeded):
    svc, base = seeded["svc"], seeded["base"]
    q = expr(query=CHECKOUT, aggs={"n": {"value_count": {"field": "event.duration"}}})
    _, res = svc.datasets.get((await svc.query(q, **window(base)))["dataset"])
    vals = {r["ts_ms"]: r["avg"] for r in res.buckets.to_pylist()}
    assert vals[ends(base)[0]] == pytest.approx(0.2) and vals[ends(base)[GAP_MINUTE]] == 0.0


async def test_stats_is_the_mean_latency_with_its_count(seeded):
    svc, base = seeded["svc"], seeded["base"]
    q = expr(query=CHECKOUT, aggs={"lat": {"stats": {"field": "event.duration"}}})
    _, res = svc.datasets.get((await svc.query(q, **window(base)))["dataset"])
    rows = {r["ts_ms"]: r for r in res.buckets.to_pylist()}
    assert ends(base)[GAP_MINUTE] not in rows and len(rows) == MINUTES - 1
    r = rows[ends(base)[0]]
    assert (r["count"], r["min"], r["max"]) == (12, 40 * MS, 100 * MS)
    assert r["avg"] == pytest.approx(50 * MS)


async def test_grouped_rate_is_one_series_per_status(seeded):
    svc, base = seeded["svc"], seeded["base"]
    q = expr(query=CHECKOUT, aggs={"by_status": {"terms": {"field": "http.response.status_code"}}})
    _, res = svc.datasets.get((await svc.query(q, **window(base)))["dataset"])
    labels = {r["series_id"]: json.loads(r["labels"])["http.response.status_code"]
              for r in res.series.to_pylist()}  # fmt: skip
    per = {(labels[r["series_id"]], r["ts_ms"]): r["avg"] for r in res.buckets.to_pylist()}
    assert set(labels.values()) == {"200", "500"}
    assert per[("200", ends(base)[0])] == pytest.approx(10 / 60)
    assert per[("500", ends(base)[0])] == pytest.approx(2 / 60)
    assert per[("500", ends(base)[GAP_MINUTE])] == 0.0


async def test_percentile_is_a_quantile_with_its_count(seeded):
    svc, base = seeded["svc"], seeded["base"]
    q = expr(query=CHECKOUT,
             aggs={"p": {"percentiles": {"field": "event.duration", "percents": [50]}}})  # fmt: skip
    out = await svc.query(q, **window(base))
    meta, res = svc.datasets.get(out["dataset"])
    assert meta.representation == "quantile" and "approximate_percentile" in meta.source_caveats
    r = next(r for r in res.buckets.to_pylist() if r["ts_ms"] == ends(base)[0])
    assert r["count"] == 12 and r["avg"] == pytest.approx(40 * MS, rel=0.01)


async def test_query_distribution_is_query_chosen_document_counts(seeded):
    svc, base = seeded["svc"], seeded["base"]
    sel = expr(aggs={"lat": {"histogram": {"field": "event.duration", "interval": 25 * MS}}})
    out = await svc.query_distribution(sel, ["service.name"], **window(base))
    assert out["summary"]["buckets"].startswith("fixed-width buckets of")
    assert out["summary"]["buckets"].endswith("chosen by the query; each [lo, hi)")
    meta, dist = svc.datasets.get_distribution(out["dataset"])
    labels = {r["series_id"]: json.loads(r["labels"])["service.name"]
              for r in dist.series.to_pylist()}  # fmt: skip
    first = [(labels[r["series_id"]], r["bucket_lo"], r["bucket_hi"], r["count"])
             for r in dist.rows.to_pylist() if r["ts_ms"] == ends(base)[0]]  # fmt: skip
    assert sorted(first) == [
        ("checkout", 25 * MS, 50 * MS, 10.0), ("checkout", 100 * MS, 125 * MS, 2.0),
        ("search", 0.0, 25 * MS, 5.0),
    ]  # fmt: skip
    cols = {(labels[c["series_id"]], c["ts_ms"]): c["n"] for c in dist.columns.to_pylist()}
    assert cols[("checkout", ends(base)[GAP_MINUTE])] == 0.0
    assert cols[("search", ends(base)[0])] == 5.0
    f = svc.fraction_over(out["dataset"], 50 * MS)
    assert f["compare"] == ">=" and f["series"][0]["exact"] is True
    assert f["series"][0]["fraction"] == pytest.approx(2 / 17, rel=1e-3)  # 4 significant digits


async def test_discover_learns_fields_and_their_declared_units(seeded):
    svc = seeded["svc"]
    out = await svc.learn("es")
    assert out["metrics"] >= 2 and out["families"] == 0
    dur = svc.ws.catalog_entry("es", "event.duration").fields["unit"]
    assert (dur.value, dur.origin) == ("ns", "metadata")
    assert "http.response.status_code" in {e.metric for e in svc.ws.catalog_list("es")}


async def test_an_unmapped_field_is_refused_not_read_as_no_data(seeded):
    svc, base = seeded["svc"], seeded["base"]
    q = expr(aggs={"lat": {"stats": {"field": "event.durationn"}}})
    with pytest.raises(SourceError, match="not in the mapping"):
        await svc.query(q, **window(base))


async def test_terms_on_a_text_field_points_at_the_keyword_sub_field(seeded):
    svc, base = seeded["svc"], seeded["base"]
    with pytest.raises(SourceError, match="not aggregatable") as e:
        await svc.query(expr(aggs={"m": {"terms": {"field": "message"}}}), **window(base))
    assert "message.keyword" in e.value.hint


async def test_a_too_small_terms_size_is_refused(seeded):
    svc, base = seeded["svc"], seeded["base"]
    q = expr(aggs={"s": {"terms": {"field": "service.name", "size": 1}}})
    with pytest.raises(LimitExceeded, match="other terms"):
        await svc.query(q, **window(base))
```

(`svc.ws.catalog_list(source)` returns `CatalogEntry` objects; `CatalogEntry.metric` is the field path.)

- [ ] **Step 4: Run the integration tests**

Run: `just test-integration -k es_containers`
Expected: PASS for both `[elasticsearch]` and `[opensearch]` parameters (first run pulls the images; allow several minutes). Then run the whole tier: `just test-integration`.

- [ ] **Step 5: Gates and commit**

```bash
just fmt && just lint && uv run pytest tests/unit -q
git add tests/integration/conftest.py tests/integration/es_seed.py \
  tests/integration/test_es_containers.py
git commit -m "test(es): Elasticsearch 8 and OpenSearch 2 containers end to end"
```

---

### Task 16 (best-effort, skippable): CERN live tier

Per the spec this tier is **not required** for the epic. Do Step 1 first; if either probe request is refused, stop here: do not add the test, and report "CERN tier skipped: the Grafana proxy refuses `_search` / `_field_caps` (HTTP <status>)" to the user (CERN then belongs to the Grafana-proxied follow-up; the adapter is not changed to suit the proxy).

**Files:**
- Create: `scripts/cern_es_probe.py`
- Create (only if Step 1 succeeds): `tests/integration/test_es_cern_network.py`

**Interfaces:**
- Consumes: `build_service`, `SourceSpec` with `flavor="elasticsearch"`, `Politeness` (Tasks 1, 10, 11).
- Produces: a `-m network` (non-blocking) live test, or a documented skip.

- [ ] **Step 1: Probe the two paths through the proxy (one request each)**

`scripts/cern_es_probe.py`:

```python
"""One _field_caps GET and one _search POST against CERN's esnet index through the public Grafana
datasource proxy: does the proxy forward the paths the Elasticsearch adapter needs?

Run: uv run scripts/cern_es_probe.py
"""

from __future__ import annotations

import json
import time

import httpx

from telemetry_nerd.sources.promql import USER_AGENT

PROXY = "https://monit-grafana-open.cern.ch/api/datasources/proxy/uid/000007855"
PATTERN = "esnet_*"
TIME_FIELD = "timestamp"


def main() -> int:
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(timeout=60, headers=headers) as c:
        caps = c.get(f"{PROXY}/{PATTERN}/_field_caps", params={"fields": TIME_FIELD})
        print("_field_caps", caps.status_code, caps.text[:300])
        time.sleep(1)  # CERN politeness: one request at a time, >= 1 s apart
        body = {"size": 0, "track_total_hits": False, "query": {"match_all": {}}}
        search = c.post(f"{PROXY}/{PATTERN}/_search", content=json.dumps(body),
                        headers={"Content-Type": "application/json"})  # fmt: skip
        print("_search", search.status_code, search.text[:300])
    ok = caps.status_code == 200 and search.status_code == 200
    print("CERN tier:", "add the live test" if ok else "skip (proxy refuses a path)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

Run: `uv run scripts/cern_es_probe.py`
Expected: either `CERN tier: add the live test` (continue with Step 2) or `CERN tier: skip ...` (stop; commit only the script with `git add scripts/cern_es_probe.py && git commit -m "chore(es): CERN proxy probe script (tier skipped: proxy refuses ES paths)"` and report the skip).

- [ ] **Step 2: Write the live test** (`tests/integration/test_es_cern_network.py`)

```python
"""Live CERN esnet index through the public Grafana datasource proxy (-m network: best-effort,
non-blocking; spec: Testing / Live)."""

import pytest

from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.sources.spec import Politeness, SourceSpec

pytestmark = pytest.mark.network

CERN = "https://monit-grafana-open.cern.ch/api/datasources/proxy/uid/000007855"


async def test_cern_esnet_probe_discover_and_rate(tmp_path):
    svc = build_service(Settings(data_dir=tmp_path / "d", source_url="http://127.0.0.1:9"))
    out = await svc.source_connect(SourceSpec(
        name="cern-esnet", url=CERN, flavor="elasticsearch", index_pattern="esnet_*",
        time_field="timestamp",
        politeness=Politeness(max_concurrency=1, min_interval_ms=1_000, timeout_s=60),
    ))  # fmt: skip
    assert out["status"]["reachable"] is True
    learned = await svc.learn("cern-esnet")
    assert learned["metrics"] > 0
    q = await svc.query('{"query": {"match_all": {}}}', start="now-7d", end="now-1d", step="1h",
                        source="cern-esnet")  # fmt: skip
    meta = svc.datasets.meta(q["dataset"])
    assert (meta.query_language, meta.representation) == ("es_dsl", "bucket_agg")
```

- [ ] **Step 3: Run it**

Run: `just test-network -k cern_esnet`
Expected: PASS. If it fails on data (not on access), fix the assertion to what the live index really holds and say so in the commit message; never add retries.

- [ ] **Step 4: Commit**

```bash
just fmt && just lint
git add scripts/cern_es_probe.py tests/integration/test_es_cern_network.py
git commit -m "test(es): best-effort live CERN esnet tier through the Grafana proxy"
```

---

## Spec coverage map

| Spec section / requirement | Task |
|---|---|
| Framing: `expr` is native, shown verbatim; Query expander unchanged | 2 (no canonicalization), 11 (`meta.expr = expr`) |
| The `expr` contract: same split as PromQL, adapter-built `date_histogram`, wire request | 2 |
| Query forms table (rate, field_rate, stats, percentile, histogram; other aggs refused) | 2, 6, 7 |
| Query buckets and time (end time, range, offset, `[t − step, t)`, tiles, interior zeros, leading/trailing gaps, `zero_is_no_documents`) | 2, 6, 11 |
| Grouping (one `terms`, labels, no `missing`, `sum_other_doc_count`, absent groups, max_series) | 2, 6, 7 |
| Validation (shape refusals, reserved names, field checks via `_field_caps`, cache until discover) | 2, 6, 8 |
| Distributions: request (`by` → terms, size max_series + 1), `DistResult` mapping, `query_chosen_buckets` | 7, 11 |
| Bucket edges: `linear` kind, `width`/`offset`, `describe`, `lower_inclusive`, `>=`, panel notes, summary wording, `DIST_SCHEMA` comment, `BucketSchemeInfo` | 3, 11, 14 |
| Little's law: `concurrency_source`, ES roles, stats → sum/count, tile path, unit from catalog, binding refused, ES concurrency refused, grid `max(res)`, probe on concurrency source, cross-source `by` refused, `sources` assumption, tool description | 13 |
| `SourceSpec` / `AuthRef` (`apikey`) | 1 |
| `source_connect`, Grafana hint, factory dispatch | 10 |
| Adapter member table (identity, resolution, semantics, query_language, fetch/fetch_values, fetch_histogram, discover, probe, scrape_interval), transport conventions | 5, 6, 7, 8 |
| `probe()` (version, distribution, mismatch, 403 tolerated, field_caps of time field, supported versions) | 5 |
| `Source.query_language` | 4 |
| Discovery and the catalog (field_caps mapping table, caveats, partial, `naming="fields"`, catalog_learn) | 8, 9 |
| Service integration (dispatch steps 1–4, `DatasetMeta.query_language`, operations table, tool descriptions) | 11, 12, 13 |
| Errors table — every row | connection/timeout/401/non-JSON/5xx: 5; server timeout/shard failures, 403, 404 + `_shards.total == 0`, field checks, 400 parse/shard, fielddata, unsupported shape, too_many_buckets, truncated groups, 429 circuit breaker, 429 rejected: 6; no numeric fields: 8; time field: 5 |
| Testing: unit, integration (ES 8 + OS 2, ECS index), live CERN (best-effort), UI vitest | 1–14, 15, 16, 14 |
