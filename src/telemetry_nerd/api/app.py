"""HTTP + WebSocket API for the UI, the sandbox (M5) and future front doors."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse as _JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from telemetry_nerd.channel.format import format_channel
from telemetry_nerd.config import DEFAULT_ALLOWED_HOSTS
from telemetry_nerd.core.service import ChartRejected, TelemetryService
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.jsonsafe import finite
from telemetry_nerd.sources.base import SourceError
from telemetry_nerd.workspace.models import AnnotationIn, HypothesisStatus, TimeSpan, Verdict

log = logging.getLogger(__name__)

RENDER_BUDGET_MS = 100
RENDER_POINTS_PER_PX = 2


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
) -> Starlette:
    hosts = frozenset(h.strip("[]").lower() for h in allowed_hosts)

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
        try:
            res = service.show(body["dataset"], body["question"], actor="user")
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
        # Subscribed first, so nothing is lost between replay and live; dedupe by seq.
        last_sent = since
        while batch := service.log.since(last_sent):
            for event in batch:
                await websocket.send_json(event.to_dict())
                last_sent = event.seq

        async def until_disconnect() -> None:
            while (await websocket.receive())["type"] != "websocket.disconnect":
                pass

        reader = asyncio.ensure_future(until_disconnect())
        try:
            while True:
                getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({reader, getter}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    event = getter.result()
                    if event["seq"] > last_sent:
                        await websocket.send_json(event)
                        last_sent = event["seq"]
                else:
                    getter.cancel()
                    break
        except WebSocketDisconnect:
            pass
        finally:
            reader.cancel()
            service.log.unsubscribe(queue)

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
        intentional, ambient = service.log.claim(body["consumer"])
        if not intentional:
            return {"content": None}
        content, meta = format_channel(intentional, ambient)
        return {"content": content, "meta": meta, "seqs": [e.seq for e in intentional]}

    @_api
    async def channel_heartbeat(request: Request) -> object:
        body = await _body(request, consumer=str)
        service.log.heartbeat(body["consumer"])
        return {"ok": True}

    @_api
    async def channel_status(request: Request) -> object:
        consumer = request.query_params.get("consumer", "claude")
        return {"channel_active": service.log.channel_active(consumer)}

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
        Route("/api/channel/heartbeat", channel_heartbeat, methods=["POST"]),
        Route("/api/channel/status", channel_status),
        Route("/api/panels", list_panels),
        Route("/api/panels/{id}/data", panel_data),
        Route("/api/query", query, methods=["POST"]),
        Route("/api/show", show, methods=["POST"]),
        Route("/api/render-report", render_report, methods=["POST"]),
        WebSocketRoute("/ws", events),
    ]
    if ui_dir is not None and (ui_dir / "index.html").exists():
        routes.append(Mount("/", app=StaticFiles(directory=ui_dir, html=True)))
    return Starlette(
        routes=routes,
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))],
    )
