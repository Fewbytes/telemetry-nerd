"""Record/replay httpx transports so tests run against real source responses offline.

A fixture is one JSON file per exchange, keyed by method, path and the sorted query
parameters. Replaying a request that was never recorded raises ConnectError, so a test
can never reach the network by accident.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import httpx


def _identity(request: httpx.Request) -> dict:
    return {
        "method": request.method,
        "path": request.url.path,
        "params": sorted(request.url.params.multi_items()),
    }


def fixture_name(request: httpx.Request) -> str:
    ident = _identity(request)
    digest = hashlib.sha256(json.dumps(ident, sort_keys=True).encode()).hexdigest()[:10]
    api = ident["path"][ident["path"].find("/api/") :]  # drop the proxy prefix
    slug = re.sub(r"[^a-z0-9]+", "-", api.lower()).strip("-")
    return f"{slug}-{digest}.json"


class RecordingTransport(httpx.AsyncBaseTransport):
    """Forward to `inner` and save each response under `directory`."""

    def __init__(self, inner: httpx.AsyncBaseTransport, directory: Path) -> None:
        self._inner = inner
        self._dir = directory
        directory.mkdir(parents=True, exist_ok=True)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        resp = await self._inner.handle_async_request(request)
        await resp.aread()
        record = {
            "request": _identity(request),
            "response": {
                "status": resp.status_code,
                "content_type": resp.headers.get("content-type", "application/json"),
                "body": resp.text,
            },
        }
        (self._dir / fixture_name(request)).write_text(json.dumps(record, indent=1) + "\n")
        return httpx.Response(
            resp.status_code,
            headers={"content-type": record["response"]["content_type"]},
            content=resp.content,
            request=request,
        )


class ReplayTransport(httpx.AsyncBaseTransport):
    def __init__(self, directory: Path) -> None:
        self._dir = directory

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = self._dir / fixture_name(request)
        if not path.exists():
            raise httpx.ConnectError(
                f"no recorded fixture for {request.method} {request.url} (expected {path.name})",
                request=request,
            )
        out = json.loads(path.read_text())["response"]
        return httpx.Response(
            out["status"],
            headers={"content-type": out["content_type"]},
            content=out["body"].encode(),
            request=request,
        )
