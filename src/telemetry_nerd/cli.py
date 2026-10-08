"""`telemetry-nerd` CLI: `serve` runs the daemon (HTTP/WS API, UI, MCP over streamable
HTTP at /mcp); `bridge` runs the per-session stdio MCP bridge for Claude Code;
`ensure` (SessionStart hook) and `pending` (UserPromptSubmit hook) are the plugin
hooks — they are the only commands allowed to print hook output to stdout.
Logging goes to stderr."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import anyio
import duckdb
import httpx
import uvicorn
from starlette.types import ASGIApp

from telemetry_nerd import daemon
from telemetry_nerd.api.app import create_app
from telemetry_nerd.bridge.proxy import run_bridge
from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.core.consumer import consumer_id
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
    sub.add_parser(
        "ensure", help="hook: make sure the daemon is running and print the workspace URL"
    )
    pending = sub.add_parser(
        "pending", help="hook: print pending user workspace events when no channel is live"
    )
    pending.add_argument(
        "--consumer", default=None, help="channel consumer (default: per session, dtk)"
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


# Idle keep-alive connections are held well past clients' own pool expiry (Node's agent, which
# Playwright's request context uses, and httpx both drop idle sockets after 5s, uvicorn's
# default). With equal timeouts a request sent on a pooled socket can cross the server's close
# and get ECONNRESET (3szb); a longer server timeout leaves closing idle sockets to the client.
_KEEP_ALIVE_S = 75


def _uvicorn_config(app: ASGIApp, settings: Settings) -> uvicorn.Config:
    return uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level="warning",
        access_log=False,
        timeout_keep_alive=_KEEP_ALIVE_S,
    )


def _is_lock_conflict(e: duckdb.IOException) -> bool:
    """DuckDB's message for a file already locked by another process/connection (observed on
    duckdb 1.5.6): 'Could not set lock on file "...": Conflicting lock is held ...'. Matched
    narrowly so unrelated IOExceptions (disk-full, permission-denied, corrupt file) propagate."""
    msg = str(e)
    return "Could not set lock on file" in msg or "Conflicting lock is held" in msg


async def _serve(settings: Settings) -> None:
    try:
        service = build_service(settings)
    except duckdb.IOException as e:
        if not _is_lock_conflict(e):
            raise
        # telemetry-nerd-au99: ensure_daemon() holds an flock around check-then-spawn,
        # but a daemon started directly (bypassing ensure_daemon) can still lose this
        # race. Fail quietly instead of a full traceback; the winner already owns the
        # data dir.
        logging.getLogger(__name__).info("another daemon already holds the lock, exiting")
        return
    # telemetry-nerd-vrtd: only claim daemon.json after the DuckDB open actually succeeded,
    # so a loser never overwrites the winner's state file with its own (doomed) pid.
    daemon.write_state(settings.data_dir, settings.daemon_url, os.getpid())
    mcp = build_mcp(service, settings.ui_url)
    app = create_app(service, settings.ui_dir, allowed_hosts=settings.allowed_hosts, mcp=mcp)
    server = uvicorn.Server(_uvicorn_config(app, settings))
    await server.serve()


def _already_running(settings: Settings) -> str | None:
    state = daemon.read_state(settings.data_dir)
    if state is not None and daemon.healthy(state["url"]):
        return state["url"]
    return None


def _remote_daemon_url() -> str | None:
    """TN_DAEMON_URL: an externally managed daemon (e.g. the container image). Never spawn one."""
    return os.environ.get("TN_DAEMON_URL") or None


def _bridge_url(args: argparse.Namespace, settings: Settings, log: logging.Logger) -> str:
    """Resolve the daemon URL for the bridge, spawning one unless told not to."""
    explicit = args.daemon_url or _remote_daemon_url()
    if explicit is not None:
        # An explicit URL points at an existing (often remote) daemon: never spawn.
        if not daemon.healthy(explicit):
            log.error(
                "daemon not healthy at %s (hint: start it, e.g. `telemetry-nerd serve` or the "
                "container image, or unset TN_DAEMON_URL / drop --daemon-url to autostart one)",
                explicit,
            )
            sys.exit(1)
        return explicit
    if args.no_autostart:
        return settings.daemon_url
    try:
        return daemon.ensure_daemon(settings)
    except RuntimeError as e:
        log.error("cannot start telemetry-nerd daemon: %s", e)
        sys.exit(1)


def _hook_session_id() -> str | None:
    """Claude Code passes hook input JSON (with session_id) on stdin; absent when run manually."""
    if sys.stdin is None or sys.stdin.isatty():
        return None
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        return None
    sid = data.get("session_id") if isinstance(data, dict) else None
    return str(sid) if sid else None


def _hook_consumer(explicit: str | None) -> str:
    """Per-session consumer (dtk): explicit flag, then TN_CONSUMER, then claude-<session>."""
    if explicit:
        return explicit
    if env := os.environ.get("TN_CONSUMER"):
        return env
    if sid := _hook_session_id():
        return consumer_id("claude", sid)
    return "claude"


def _bridge_consumer() -> str:
    """Per-session consumer for the bridge pump (dtk): TN_CONSUMER or claude-<session>.

    Falls back to the legacy plain `claude` when the client gives the stdio server no
    session id; deliveries then stay shared across such sessions (one-active-session
    assumption, see the connection-status design).
    """
    if env := os.environ.get("TN_CONSUMER"):
        return env
    if sid := os.environ.get("CLAUDE_SESSION_ID"):
        return consumer_id("claude", sid)
    return "claude"


_HOOK_HTTP_TIMEOUT_S = 1.0
"""Hooks must finish well under the 2 s budget even when the daemon is wedged."""


def _daemon_url(settings: Settings) -> str | None:
    """State-file URL first (mirrors ensure_daemon), then the default; None if none healthy.

    TN_DAEMON_URL, when set, is the only candidate."""
    if remote := _remote_daemon_url():
        return remote if daemon.healthy(remote) else None
    state = daemon.read_state(settings.data_dir)
    urls = [state["url"]] if state is not None else []
    if settings.daemon_url not in urls:
        urls.append(settings.daemon_url)
    for url in urls:
        if daemon.healthy(url):
            return url
    return None


def _cmd_ensure(settings: Settings) -> None:
    try:
        if remote := _remote_daemon_url():
            if not daemon.healthy(remote):
                raise RuntimeError(f"no healthy daemon at TN_DAEMON_URL={remote}")
            url = remote
        else:
            url = daemon.ensure_daemon(settings)
    except RuntimeError as e:
        # SessionStart stdout becomes session context; a missing daemon must not
        # fail the session, so report and exit 0.
        print(f"Telemetry Nerd daemon not running: {e}")
        return
    print(f"Telemetry Nerd workspace: {url}")
    for line in _retro_lines(url):
        print(line)


def _retro_lines(url: str) -> list[str]:
    """At most two lines for session context (spec 2026-10-04 R9): lesson counts per connected
    source, never their text (lessons surface only through a scoped lessons_for call), and
    proposals awaiting the user's review. Silent on any error: a hook never blocks a session."""
    try:
        r = httpx.get(f"{url}/api/proposals/summary", timeout=_HOOK_HTTP_TIMEOUT_S)
        data = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return []
    out = []
    counts = data.get("lessons") or {}
    if isinstance(counts, dict) and counts:
        per = ", ".join(f"{src} ({n})" for src, n in counts.items())
        out.append(
            f"Telemetry Nerd lessons on file for {per}: call lessons_for(source, services) once "
            "the investigation's source and services are known; they apply only within their scope."
        )
    pending = data.get("pending")
    if isinstance(pending, int) and pending > 0:
        out.append(
            f"Telemetry Nerd: {pending} retrospective proposal(s) await the user's review in the "
            "UI (Proposals view)."
        )
    return out


def _cmd_pending(settings: Settings, consumer: str) -> None:
    url = _daemon_url(settings)
    if url is None:
        return
    try:
        r = httpx.get(
            f"{url}/api/channel/status",
            params={"consumer": consumer},
            timeout=_HOOK_HTTP_TIMEOUT_S,
        )
        if r.status_code != 200 or r.json().get("channel_active"):
            # A ready channel bridge is connected: it delivers (the claim would be refused).
            return
        r = httpx.post(
            f"{url}/api/channel/claim",
            json={"consumer": consumer},
            timeout=_HOOK_HTTP_TIMEOUT_S,
        )
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return
    content = data.get("content")
    if not content:
        return
    seqs = ",".join(str(s) for s in data.get("seqs") or ())
    print(f'<telemetry-nerd-ui-events seqs="{seqs}">\n{content}\n</telemetry-nerd-ui-events>')


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
        try:
            anyio.run(_serve, settings)
        finally:
            daemon.remove_state(settings.data_dir, pid)
    elif args.command == "bridge":
        url = _bridge_url(args, Settings.from_env(), log)
        consumer = _bridge_consumer()
        log.info("bridge: daemon at %s, consumer %s", url, consumer)
        anyio.run(run_bridge, url, consumer)
    elif args.command == "ensure":
        _cmd_ensure(Settings.from_env())
    elif args.command == "pending":
        _cmd_pending(Settings.from_env(), _hook_consumer(args.consumer))


if __name__ == "__main__":
    main()
