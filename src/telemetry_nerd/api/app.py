"""HTTP + WebSocket API for the UI, the sandbox (M5) and future front doors."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse as _JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from telemetry_nerd.core.service import ChartRejected, TelemetryService
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.jsonsafe import finite
from telemetry_nerd.sources.base import SourceError

log = logging.getLogger(__name__)

RENDER_BUDGET_MS = 100
RENDER_POINTS_PER_PX = 2


class JSONResponse(_JSONResponse):
    """Never emits NaN/Inf (invalid JSON): non-finite floats become null."""

    def render(self, content: object) -> bytes:
        return super().render(finite(content))


def _error(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


DEFAULT_ALLOWED_HOSTS = ("127.0.0.1", "localhost", "[::1]")


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
        return JSONResponse([p.to_dict() for p in service.workspace.list_panels()])

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
            out = await service.query(**args)
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
            res = service.show(body["dataset"], body["question"])
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
            service.events.publish({"type": "render.budget_exceeded", "report": body})
        return JSONResponse({"budget_exceeded": exceeded})

    async def events(websocket: WebSocket) -> None:
        # Browsers do not apply the same-origin policy to WebSockets: check Origin ourselves.
        origin = websocket.headers.get("origin")
        if origin is not None and not origin_allowed(origin):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        queue = service.events.subscribe()

        async def until_disconnect() -> None:
            while (await websocket.receive())["type"] != "websocket.disconnect":
                pass

        reader = asyncio.ensure_future(until_disconnect())
        try:
            while True:
                getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({reader, getter}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    await websocket.send_json(getter.result())
                else:
                    getter.cancel()
                    break
        except WebSocketDisconnect:
            pass
        finally:
            reader.cancel()
            service.events.unsubscribe(queue)

    routes = [
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
