"""`telemetry-nerd` CLI: `serve` runs the daemon (HTTP/WS API, UI, MCP over streamable
HTTP at /mcp); `bridge` runs the per-session stdio MCP bridge for Claude Code.
Logging goes to stderr."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import anyio
import uvicorn
from starlette.types import ASGIApp

from telemetry_nerd import daemon
from telemetry_nerd.api.app import create_app
from telemetry_nerd.bridge.proxy import run_bridge
from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.mcp.server import build_mcp


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="telemetry-nerd")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the daemon: workspace UI, HTTP API and MCP at /mcp")
    serve.add_argument("--data-dir", type=Path)
    serve.add_argument("--source-url")
    serve.add_argument("--source-flavor", choices=["victoriametrics", "prometheus"])
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--ui-dir", type=Path)
    serve.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="extra Host/Origin accepted by the HTTP server (repeatable)",
    )
    serve.add_argument("--no-mcp", action="store_true", help="deprecated no-op")
    bridge = sub.add_parser(
        "bridge", help="stdio MCP bridge for Claude Code: daemon tools + channel delivery"
    )
    bridge.add_argument("--daemon-url", help="daemon URL (default from settings; skips autostart)")
    bridge.add_argument(
        "--no-autostart", action="store_true", help="do not spawn the daemon if it is not running"
    )
    return parser.parse_args(argv)


def _settings(args: argparse.Namespace) -> Settings:
    s = Settings.from_env()
    for attr in ("data_dir", "source_url", "source_flavor", "host", "port", "ui_dir"):
        value = getattr(args, attr)
        if value is not None:
            setattr(s, attr, value)
    s.allowed_hosts = [*s.allowed_hosts, *args.allowed_host]
    return s


def _configure_logging() -> None:
    # Log to stderr, and keep httpx's per-request INFO lines
    # (which include query URLs) out of the log.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _uvicorn_config(app: ASGIApp, settings: Settings) -> uvicorn.Config:
    return uvicorn.Config(
        app, host=settings.host, port=settings.port, log_level="warning", access_log=False
    )


async def _serve(settings: Settings) -> None:
    service = build_service(settings)
    mcp = build_mcp(service, settings.ui_url)
    app = create_app(service, settings.ui_dir, allowed_hosts=settings.allowed_hosts, mcp=mcp)
    server = uvicorn.Server(_uvicorn_config(app, settings))
    await server.serve()


def _already_running(settings: Settings) -> str | None:
    state = daemon.read_state(settings.data_dir)
    if state is not None and daemon.healthy(state["url"]):
        return state["url"]
    return None


def _bridge_url(args: argparse.Namespace, settings: Settings, log: logging.Logger) -> str:
    """Resolve the daemon URL for the bridge, spawning one unless told not to."""
    if args.daemon_url is not None:
        # An explicit URL points at an existing (often remote) daemon: never spawn.
        if not daemon.healthy(args.daemon_url):
            log.error(
                "daemon not healthy at %s (hint: start it with `telemetry-nerd serve` "
                "or drop --daemon-url to autostart one)",
                args.daemon_url,
            )
            sys.exit(1)
        return args.daemon_url
    if args.no_autostart:
        return settings.daemon_url
    try:
        return daemon.ensure_daemon(settings)
    except RuntimeError as e:
        log.error("cannot start telemetry-nerd daemon: %s", e)
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    _configure_logging()
    args = _parse(argv)
    log = logging.getLogger(__name__)
    if args.command == "serve":
        settings = _settings(args)
        if args.no_mcp:
            log.warning("--no-mcp is deprecated and ignored: serve is daemon-only since M2")
        if running := _already_running(settings):
            print(
                f"telemetry-nerd daemon already running at {running} for {settings.data_dir}",
                file=sys.stderr,
            )
            sys.exit(1)
        if settings.ui_dir is None or not (settings.ui_dir / "index.html").exists():
            log.warning("UI not built: run `just ui-build`")
        log.info("workspace UI at %s, MCP at %s/mcp", settings.ui_url, settings.ui_url)
        pid = os.getpid()
        daemon.write_state(settings.data_dir, settings.daemon_url, pid)
        try:
            anyio.run(_serve, settings)
        finally:
            daemon.remove_state(settings.data_dir, pid)
    elif args.command == "bridge":
        url = _bridge_url(args, Settings.from_env(), log)
        log.info("bridge: daemon at %s", url)
        anyio.run(run_bridge, url)


if __name__ == "__main__":
    main()
