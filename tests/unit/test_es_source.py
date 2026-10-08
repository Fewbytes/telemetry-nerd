"""ElasticsearchSource: construction, transport errors and probe (fixtures, MockTransport)."""

import time

import httpx
import pytest

from telemetry_nerd.sources.base import SourceError, SourceUnavailable
from telemetry_nerd.sources.elasticsearch import ElasticsearchSource
from telemetry_nerd.sources.oauth import TokenState, issuer_key, save_token, token_path
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


async def test_static_auth_source_401_falls_through_to_the_generic_error_unchanged():
    def fake(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    src = ElasticsearchSource.from_spec(
        spec(auth=AuthRef(env="ES_KEY", scheme="apikey")), environ={"ES_KEY": "abc=="},
        client=client,
    )  # fmt: skip
    with pytest.raises(SourceError, match="authentication failed") as exc_info:
        await src.probe()
    assert "apikey" in exc_info.value.hint
    assert "source_connect" not in exc_info.value.hint


def oauth_spec(**kw) -> SourceSpec:
    return spec(
        auth={
            "authorize_url": "https://idp.example.com/authorize",
            "token_url": "https://idp.example.com/token",
            "client_id": "tn-client",
        },
        **kw,
    )


def _seed_oauth_token(tmp_path, name: str = "logs") -> None:
    s = oauth_spec()
    key = issuer_key(s.auth, s.url)
    save_token(token_path(tmp_path, name), TokenState("AT0", "RT0", time.time() + 3600, key))


async def test_from_spec_requires_data_dir_for_oauth_sources(tmp_path):
    with pytest.raises(ValueError, match="data_dir"):
        ElasticsearchSource.from_spec(oauth_spec())


async def test_oauth_source_sends_bearer_token_per_request(tmp_path):
    _seed_oauth_token(tmp_path)
    fake = FakeEs()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    src = ElasticsearchSource.from_spec(oauth_spec(), client=client, data_dir=tmp_path)
    await src.probe()
    assert fake.requests[0].headers["Authorization"] == "Bearer AT0"


async def test_oauth_source_refreshes_and_retries_once_on_401(tmp_path):
    _seed_oauth_token(tmp_path)
    calls = {"query": 0, "token": 0}
    fake_es = FakeEs()

    def fake(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            calls["token"] += 1
            return httpx.Response(
                200,
                json={"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600},
            )
        if request.url.path != "/":
            return fake_es(request)
        calls["query"] += 1
        if request.headers.get("Authorization") == "Bearer AT0":
            return es_error(401, "security_exception", "unauthorized")
        return fake_es(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    src = ElasticsearchSource.from_spec(oauth_spec(), client=client, data_dir=tmp_path)
    await src.probe()
    assert calls["token"] == 1 and calls["query"] == 2
