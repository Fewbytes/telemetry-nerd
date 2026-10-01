"""HTTP + WebSocket API for the UI, the sandbox (M5) and future front doors."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse as _JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from telemetry_nerd.channel.dispatch import ChannelDispatcher
from telemetry_nerd.config import DEFAULT_ALLOWED_HOSTS
from telemetry_nerd.core.presence import MODES
from telemetry_nerd.core.service import ChartRejected, TelemetryService
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.jsonsafe import finite
from telemetry_nerd.sources.base import SourceError
from telemetry_nerd.workspace.models import AnnotationIn, HypothesisStatus, TimeSpan, Verdict

log = logging.getLogger(__name__)

RENDER_BUDGET_MS = 100
RENDER_POINTS_PER_PX = 2
UI_CONSUMER = "claude"
"""The channel consumer the UI reports presence and delivery state for."""


class JSONResponse(_JSONResponse):
    """Never emits NaN/Inf (invalid JSON): non-finite floats become null."""

    def render(self, content: object) -> bytes:
        return super().render(finite(content))


def _error(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


class _BadRequest(Exception):
    def __init__(self, message: str, hint: str, status: int = 400) -> None:
        super().__init__(message)
        self.hint = hint
        self.status = status


async def _body(request: Request, **required: type) -> dict:
    """Parse a JSON object body; require the named keys with the given types."""
    # Requiring JSON keeps "simple" cross-site form posts (text/plain, urlencoded) out.
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise _BadRequest(
            "unsupported content type",
            "send the body with Content-Type: application/json",
            status=415,
        )
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise _BadRequest("invalid JSON body", "send a JSON object with Content-Type JSON") from e
    if not isinstance(body, dict):
        raise _BadRequest("body must be a JSON object", "send a JSON object, e.g. {...}")
    for key, typ in required.items():
        if not isinstance(body.get(key), typ):
            raise _BadRequest(
                f"missing or invalid field {key!r}",
                f"{key!r} is required and must be a {typ.__name__}",
            )
    return body


class _HypothesisUpdate(BaseModel):
    status: HypothesisStatus


class _VerdictIn(BaseModel):
    verdict: Verdict


def _validated[M: BaseModel](model: type[M], body: dict) -> M:
    try:
        return model.model_validate(body)
    except ValidationError as e:
        raise _BadRequest(
            "invalid request body",
            "fix the listed fields; unknown fields are rejected",
            status=422,
        ) from e


def _int_param(request: Request, name: str, default: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise _BadRequest(f"{name} must be an integer", f"pass ?{name}=<integer>") from e


def _api(handler: Callable[[Request], Awaitable[object]]):
    """Map domain and validation errors to the documented HTTP statuses."""

    async def wrapped(request: Request) -> JSONResponse:
        try:
            return JSONResponse(await handler(request))
        except _BadRequest as e:
            extra: dict = {"hint": e.hint}
            cause = e.__cause__
            if isinstance(cause, ValidationError):
                extra["issues"] = [
                    {"loc": list(err["loc"]), "msg": err["msg"]} for err in cause.errors()
                ]
            return _error(e.status, str(e), **extra)
        except NotFound as e:
            return _error(404, str(e))
        except ValueError as e:
            return _error(400, str(e))

    return wrapped


def create_app(
    service: TelemetryService,
    ui_dir: Path | None = None,
    allowed_hosts: Sequence[str] = DEFAULT_ALLOWED_HOSTS,
    mcp: MCPServer | None = None,
) -> Starlette:
    hosts = frozenset(h.strip("[]").lower() for h in allowed_hosts)
    presence = service.presence
    dispatch = ChannelDispatcher(service.log, presence)

    def origin_allowed(origin: str) -> bool:
        try:
            host = urlsplit(origin).hostname
        except ValueError:
            return False
        return host is not None and host.lower() in hosts

    async def list_panels(request: Request) -> JSONResponse:
        return JSONResponse([p.to_dict() for p in service.ws.list_panels()])

    async def panel_data(request: Request) -> JSONResponse:
        try:
            width = min(4000, max(50, int(request.query_params.get("width", "800"))))
        except ValueError:
            return _error(400, "width must be an integer")
        try:
            out = service.panel_data(request.path_params["id"], width)
        except NotFound as e:
            return _error(404, str(e))
        return JSONResponse(out)

    async def query(request: Request) -> JSONResponse:
        try:
            body = await _body(request, expr=str)
        except _BadRequest as e:
            return _error(e.status, str(e), hint=e.hint)
        args = {k: body[k] for k in ("expr", "start", "end", "step", "source") if k in body}
        try:
            out = await service.query(**args, actor="user")
        except SourceError as e:
            return _error(400, str(e), hint=e.hint)
        except ValueError as e:
            return _error(400, str(e))
        return JSONResponse(out)

    async def show(request: Request) -> JSONResponse:
        try:
            body = await _body(request, dataset=str, question=str)
        except _BadRequest as e:
            return _error(e.status, str(e), hint=e.hint)
        unit = body.get("unit")
        if unit is not None and not isinstance(unit, str):
            return _error(400, "unit must be a string", hint='e.g. "s", "B", "req/s"')
        try:
            res = service.show(body["dataset"], body["question"], actor="user", unit=unit)
        except ChartRejected as e:
            return _error(422, "chart rejected", issues=[i.model_dump() for i in e.issues])
        except NotFound as e:
            return _error(404, str(e))
        except ValueError as e:
            return _error(400, str(e))
        return JSONResponse(
            {"panel": res.panel.to_dict(), "issues": [i.model_dump() for i in res.issues]}
        )

    async def render_report(request: Request) -> JSONResponse:
        try:
            body = await _body(request)
        except _BadRequest as e:
            return _error(e.status, str(e), hint=e.hint)
        try:
            exceeded = (
                body["render_ms"] > RENDER_BUDGET_MS
                or body["points"] > RENDER_POINTS_PER_PX * body["width_px"]
            )
        except (KeyError, TypeError):
            return _error(
                400,
                "render_ms, points and width_px are required numbers",
                hint="send numeric render_ms, points and width_px",
            )
        if exceeded:
            log.warning("render budget exceeded: %s", body)
            panel_id = body.get("panel_id")
            object_id = panel_id if isinstance(panel_id, str) else None
            service.log.append("user", "render.budget_exceeded", object_id, {"report": body})
        return JSONResponse({"budget_exceeded": exceeded})

    async def events(websocket: WebSocket) -> None:
        # Browsers do not apply the same-origin policy to WebSockets: check Origin ourselves.
        origin = websocket.headers.get("origin")
        if origin is not None and not origin_allowed(origin):
            await websocket.close(code=1008)
            return
        raw_since = websocket.query_params.get("since")
        if raw_since is None:
            since = service.log.last_seq
        else:
            try:
                since = int(raw_since)
            except ValueError:
                since = -1
            if since < 0:
                await websocket.close(code=1008)
                return
        await websocket.accept()
        queue = service.log.subscribe()
        changes = presence.subscribe()
        # Subscribed first, so nothing is lost between replay and live; dedupe by seq.
        last_sent = since
        while batch := service.log.since(last_sent):
            for event in batch:
                await websocket.send_json(event.to_dict())
                last_sent = event.seq
        # Presence frames are control messages: no seq, never logged or replayed.
        await websocket.send_json(dispatch.presence_frame(UI_CONSUMER))

        async def until_disconnect() -> None:
            while (await websocket.receive())["type"] != "websocket.disconnect":
                pass

        reader = asyncio.ensure_future(until_disconnect())
        getter = asyncio.ensure_future(queue.get())
        changed = asyncio.ensure_future(changes.get())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {reader, getter, changed}, return_when=asyncio.FIRST_COMPLETED
                )
                if reader in done:
                    break
                if getter in done:
                    event = getter.result()
                    getter = asyncio.ensure_future(queue.get())
                    if event["seq"] > last_sent:
                        await websocket.send_json(event)
                        last_sent = event["seq"]
                if changed in done:
                    consumer = changed.result()
                    changed = asyncio.ensure_future(changes.get())
                    if consumer == UI_CONSUMER:
                        await websocket.send_json(dispatch.presence_frame(UI_CONSUMER))
        except WebSocketDisconnect:
            pass
        finally:
            for task in (reader, getter, changed):
                task.cancel()
            service.log.unsubscribe(queue)
            presence.unsubscribe(changes)

    async def bridge(websocket: WebSocket) -> None:
        """One stdio bridge's lifetime socket: presence reports in, channel deliveries out."""
        origin = websocket.headers.get("origin")
        if origin is not None and not origin_allowed(origin):
            await websocket.close(code=1008)
            return
        consumer = websocket.query_params.get("consumer", UI_CONSUMER)
        await websocket.accept()
        events_q = service.log.subscribe()
        changes = presence.subscribe()
        conn: int | None = None
        receiver = asyncio.ensure_future(websocket.receive())
        getter = asyncio.ensure_future(events_q.get())
        changed = asyncio.ensure_future(changes.get())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {receiver, getter, changed}, return_when=asyncio.FIRST_COMPLETED
                )
                if receiver in done:
                    message = receiver.result()
                    if message["type"] == "websocket.disconnect":
                        break
                    receiver = asyncio.ensure_future(websocket.receive())
                    try:
                        conn = _bridge_frame(consumer, conn, json.loads(message.get("text") or ""))
                    except (ValueError, KeyError, TypeError) as e:
                        log.warning("bridge: bad frame %r: %s", message.get("text"), e)
                        await websocket.close(code=1003)
                        break
                if getter in done:
                    getter = asyncio.ensure_future(events_q.get())
                if changed in done:
                    changed = asyncio.ensure_future(changes.get())
                # Every wake-up is a chance to deliver; next_delivery guards readiness,
                # deliverer choice and the one-in-flight rule.
                if conn is not None and (frame := dispatch.next_delivery(consumer, conn)):
                    await websocket.send_json(frame)
        except WebSocketDisconnect:
            pass
        finally:
            for task in (receiver, getter, changed):
                task.cancel()
            service.log.unsubscribe(events_q)
            presence.unsubscribe(changes)
            if conn is not None:
                dispatch.dropped(conn)
                presence.disconnect(conn)

    def _bridge_frame(consumer: str, conn: int | None, frame: dict) -> int:
        """Apply one bridge → daemon frame; returns the connection id."""
        kind = frame["type"]
        if kind == "hello":
            mode = frame["mode"]
            if mode not in MODES:
                raise ValueError(f"unknown mode {mode!r}")
            if conn is None:
                return presence.connect(consumer, mode)
            presence.update(conn, mode=mode)
            return conn
        if conn is None:
            raise ValueError(f"{kind!r} before hello")
        if kind == "mode":
            if frame["mode"] not in MODES:
                raise ValueError(f"unknown mode {frame['mode']!r}")
            presence.update(conn, mode=frame["mode"])
        elif kind == "ready":
            presence.update(conn, ready=True)
        elif kind == "ack":
            up_to = frame["up_to"]
            if not isinstance(up_to, int) or isinstance(up_to, bool):
                raise TypeError("ack up_to must be an integer")
            dispatch.ack(consumer, conn, up_to)
        else:
            raise ValueError(f"unknown frame type {kind!r}")
        return conn

    ws = service.ws

    @_api
    async def health(request: Request) -> object:
        try:
            ver = version("telemetry-nerd")
        except PackageNotFoundError:
            ver = "unknown"
        return {"ok": True, "version": ver, "last_seq": service.log.last_seq}

    @_api
    async def workspace(request: Request) -> object:
        return ws.snapshot()

    @_api
    async def list_events(request: Request) -> object:
        since = _int_param(request, "since", 0)
        limit = min(1000, max(1, _int_param(request, "limit", 1000)))
        batch = service.log.since(since, limit)
        return {
            "events": [e.to_dict() for e in batch],
            "last_seq": batch[-1].seq if batch else since,
        }

    @_api
    async def annotation_create(request: Request) -> object:
        data = _validated(AnnotationIn, await _body(request))
        return ws.annotate(data, "user").model_dump()

    @_api
    async def annotation_delete(request: Request) -> object:
        await _body(request)
        return ws.delete_annotation(request.path_params["id"], "user").model_dump()

    @_api
    async def hypothesis_status(request: Request) -> object:
        body = await _body(request, status=str)
        note = body.get("note")
        if note is not None and not isinstance(note, str):
            raise _BadRequest("invalid field 'note'", "'note' must be a string")
        model = _validated(_HypothesisUpdate, {"status": body["status"]})
        return ws.hypothesis_update(
            request.path_params["id"], model.status, "user", note=note
        ).model_dump()

    @_api
    async def finding_verdict(request: Request) -> object:
        body = await _body(request, verdict=str)
        comment = body.get("comment")
        if comment is not None and not isinstance(comment, str):
            raise _BadRequest("invalid field 'comment'", "'comment' must be a string")
        model = _validated(_VerdictIn, {"verdict": body["verdict"]})
        return ws.finding_verdict(
            request.path_params["id"], model.verdict, "user", comment=comment
        ).model_dump()

    @_api
    async def thread_create(request: Request) -> object:
        body = await _body(request, text=str)
        anchor = body.get("anchor")
        if anchor is not None and not isinstance(anchor, str):
            raise _BadRequest("invalid field 'anchor'", "'anchor' must be a string")
        selection = body.get("selection")
        span = _validated(TimeSpan, selection) if selection is not None else None
        return ws.ask(body["text"], "user", anchor=anchor, selection=span).model_dump()

    @_api
    async def thread_message(request: Request) -> object:
        body = await _body(request, text=str)
        return ws.post_message(request.path_params["id"], body["text"], "user").model_dump()

    @_api
    async def panel_close(request: Request) -> object:
        await _body(request)
        return ws.close_panel(request.path_params["id"], "user").to_dict()

    @_api
    async def focus(request: Request) -> object:
        body = await _body(request)
        ws.set_focus(_validated(TimeSpan, body), "user")
        return {"ok": True}

    @_api
    async def channel_claim(request: Request) -> object:
        body = await _body(request, consumer=str)
        return dispatch.claim(body["consumer"])

    @_api
    async def channel_status(request: Request) -> object:
        consumer = request.query_params.get("consumer", UI_CONSUMER)
        frame = dispatch.presence_frame(consumer)
        return {
            "channel_active": frame["status"] == "live",
            "status": frame["status"],
            "mode": frame["mode"],
            "delivered_up_to": frame["delivered_up_to"],
        }

    routes = [
        Route("/api/health", health),
        Route("/api/workspace", workspace),
        Route("/api/events", list_events),
        Route("/api/annotations", annotation_create, methods=["POST"]),
        Route("/api/annotations/{id}/delete", annotation_delete, methods=["POST"]),
        Route("/api/hypotheses/{id}/status", hypothesis_status, methods=["POST"]),
        Route("/api/findings/{id}/verdict", finding_verdict, methods=["POST"]),
        Route("/api/threads", thread_create, methods=["POST"]),
        Route("/api/threads/{id}/messages", thread_message, methods=["POST"]),
        Route("/api/panels/{id}/close", panel_close, methods=["POST"]),
        Route("/api/focus", focus, methods=["POST"]),
        Route("/api/channel/claim", channel_claim, methods=["POST"]),
        Route("/api/channel/status", channel_status),
        Route("/api/panels", list_panels),
        Route("/api/panels/{id}/data", panel_data),
        Route("/api/query", query, methods=["POST"]),
        Route("/api/show", show, methods=["POST"]),
        Route("/api/render-report", render_report, methods=["POST"]),
        WebSocketRoute("/ws", events),
        WebSocketRoute("/ws/bridge", bridge),
    ]
    if ui_dir is not None and (ui_dir / "index.html").exists():
        routes.append(Mount("/", app=StaticFiles(directory=ui_dir, html=True)))
    lifespan: Callable[[Starlette], contextlib.AbstractAsyncContextManager[None]] | None = None
    if mcp is not None:
        names = [h.strip("[]") for h in allowed_hosts]
        bracket = lambda h: f"[{h}]" if ":" in h else h
        mcp_app = mcp.streamable_http_app(
            streamable_http_path="/mcp",
            transport_security=TransportSecuritySettings(
                allowed_hosts=[f"{bracket(h)}:*" for h in names] + [bracket(h) for h in names],
                allowed_origins=[f"http://{bracket(h)}:*" for h in names]
                + [f"http://{bracket(h)}" for h in names],
            ),
        )
        routes[:0] = list(mcp_app.routes)  # before the static catch-all mount

        @contextlib.asynccontextmanager
        async def _lifespan(app: Starlette):
            async with mcp.session_manager.run():
                yield

        lifespan = _lifespan

    return Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))],
    )
