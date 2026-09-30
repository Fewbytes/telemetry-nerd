"""Shared fixtures. `live_daemon` runs the real ASGI app under uvicorn in a thread."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
import uvicorn

from telemetry_nerd.api.app import create_app
from telemetry_nerd.core.service import TelemetryService
from telemetry_nerd.mcp.server import build_mcp
from tests.unit.fakes import make_service


@dataclass
class LiveDaemon:
    url: str  # http://127.0.0.1:<port>
    service: TelemetryService

    @property
    def mcp_url(self) -> str:
        return f"{self.url}/mcp"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_daemon(tmp_path) -> Iterator[LiveDaemon]:
    service = make_service(tmp_path)
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    app = create_app(service, None, mcp=build_mcp(service, url))
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                if httpx.get(f"{url}/api/health", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("live_daemon did not become healthy")
            time.sleep(0.05)
        yield LiveDaemon(url, service)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
