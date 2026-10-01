import httpx
import pytest

from telemetry_nerd.sources.replay import RecordingTransport, ReplayTransport, fixture_name


def _upstream(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "path": request.url.path})


async def test_recorded_exchange_replays_without_the_network(tmp_path):
    rec = httpx.AsyncClient(transport=RecordingTransport(httpx.MockTransport(_upstream), tmp_path))
    live = await rec.get("https://h/api/v1/query", params={"query": "up", "b": "2"})

    replay = httpx.AsyncClient(transport=ReplayTransport(tmp_path))
    # parameter order does not matter
    again = await replay.get("https://h/api/v1/query", params={"b": "2", "query": "up"})
    assert again.status_code == live.status_code == 200
    assert again.json() == live.json()


async def test_unrecorded_request_fails_instead_of_reaching_the_network(tmp_path):
    replay = httpx.AsyncClient(transport=ReplayTransport(tmp_path))
    with pytest.raises(httpx.ConnectError, match="no recorded fixture"):
        await replay.get("https://h/api/v1/query", params={"query": "other"})


def test_fixture_name_is_readable_and_depends_on_params():
    a = httpx.Request("GET", "https://h/p/api/v1/query_range", params={"query": "a"})
    b = httpx.Request("GET", "https://h/p/api/v1/query_range", params={"query": "b"})
    assert fixture_name(a).startswith("api-v1-query-range-")
    assert fixture_name(a) != fixture_name(b)
