"""`telemetry-nerd serve`: MCP over stdio + HTTP/WS UI server in one process.
stdout belongs to MCP; all logging goes to stderr."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import anyio
import uvicorn
from starlette.types import ASGIApp

from telemetry_nerd.api.app import create_app
from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.mcp.server import build_mcp


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="telemetry-nerd")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run MCP (stdio) and the workspace UI server")
    serve.add_argument("--data-dir", type=Path)
    serve.add_argument("--source-url")
    serve.add_argument("--source-flavor", choices=["victoriametrics", "prometheus"])
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--ui-dir", type=Path)
    serve.add_argument("--no-mcp", action="store_true", help="HTTP only (development, E2E)")
    return parser.parse_args(argv)


def _settings(args: argparse.Namespace) -> Settings:
    s = Settings.from_env()
    for attr in ("data_dir", "source_url", "source_flavor", "host", "port", "ui_dir"):
        value = getattr(args, attr)
        if value is not None:
            setattr(s, attr, value)
    return s


def _configure_logging() -> None:
    # stdout belongs to MCP. Log to stderr, and keep httpx's per-request INFO lines
    # (which include query URLs) out of the log.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _uvicorn_config(app: ASGIApp, settings: Settings) -> uvicorn.Config:
    # The access log writes to stdout, which would corrupt the MCP stdio stream.
    return uvicorn.Config(
        app, host=settings.host, port=settings.port, log_level="warning", access_log=False
    )


async def _serve(settings: Settings, with_mcp: bool) -> None:
    service = build_service(settings)
    app = create_app(service, settings.ui_dir)
    server = uvicorn.Server(_uvicorn_config(app, settings))
    async with anyio.create_task_group() as tg:
        tg.start_soon(server.serve)
        if with_mcp:
            await build_mcp(service, settings.ui_url).run_stdio_async()
            server.should_exit = True


def main(argv: list[str] | None = None) -> None:
    _configure_logging()
    args = _parse(argv)
    if args.command == "serve":
        settings = _settings(args)
        logging.getLogger(__name__).info("workspace UI at %s", settings.ui_url)
        anyio.run(_serve, settings, not args.no_mcp)
