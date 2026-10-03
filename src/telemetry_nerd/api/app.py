"""HTTP + WebSocket API for the UI, the tier-2 `tn` library and future front doors."""

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

from telemetry_nerd.analysis.distlod import PX_PER_CELL
from telemetry_nerd.catalog.browse import Browse
from telemetry_nerd.channel.dispatch import ChannelDispatcher
from telemetry_nerd.config import DEFAULT_ALLOWED_HOSTS
from telemetry_nerd.core.code_ops import CodeDisabled
from telemetry_nerd.core.consumer import kind_of
from telemetry_nerd.core.presence import MODES
from telemetry_nerd.core.service import ChartRejected, TelemetryService
from telemetry_nerd.core.workspace_service import code_brief
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


def mcp_transport_security(allowed_hosts: Sequence[str]) -> TransportSecuritySettings:
    """Host/Origin allowlist for the MCP endpoint, derived from the daemon's allowed hosts.

    Origins cover http and https: a non-loopback daemon behind a TLS-terminating proxy is
    reached from https:// origins, which an http-only list would reject.
    """
    names = [h.strip("[]") for h in allowed_hosts]
    bracket = lambda h: f"[{h}]" if ":" in h else h
    return TransportSecuritySettings(
        allowed_hosts=[f"{bracket(h)}:*" for h in names] + [bracket(h) for h in names],
        allowed_origins=[
            f"{scheme}://{bracket(h)}{port}"
            for scheme in ("http", "https")
            for h in names
            for port in (":*", "")
        ],
    )


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

    async def code_list(request: Request) -> JSONResponse:
        return JSONResponse([code_brief(c) for c in service.code.list()])

    async def code_get(request: Request) -> JSONResponse:
        """A code node in full (code, streams, traceback, outputs) for the UI; read-only."""
        try:
            node = service.code.get(request.path_params["id"])
        except NotFound as e:
            return _error(404, str(e))
        return JSONResponse(node.model_dump())

    @_api
    async def code_rerun(request: Request) -> object:
        """Run a finished node's code again on the same inputs: a NEW node (rerun_of). Blocks
        until the run ends (bounded by the node's timeout); the answer is the full new node."""
        await _body(request)
        try:
            node = await service.code.rerun(request.path_params["id"], actor="user")
        except CodeDisabled as e:
            raise _BadRequest(
                str(e), "start the daemon with tier-2 execution enabled", status=409
            ) from e
        return node.model_dump()

    async def panel_data(request: Request) -> JSONResponse:
        try:
            width = min(4000, max(50, int(request.query_params.get("width", "800"))))
        except ValueError:
            return _error(400, "width must be an integer")
        try:
            out = service.panel_data(request.path_params["id"], width)
        except NotFound as e:
            return _error(404, str(e))
        except ValueError as e:  # e.g. a spectrum panel whose series no longer qualifies
            return _error(400, str(e))
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

    async def compare_seasonal(request: Request) -> JSONResponse:
        try:
            body = await _body(request, dataset=str)
        except _BadRequest as e:
            return _error(e.status, str(e), hint=e.hint)
        args = {k: body[k] for k in ("cycles", "tz", "exclude", "threshold") if k in body}
        try:
            out = await service.compare_seasonal(body["dataset"], **args, actor="user")
        except NotFound as e:
            return _error(404, str(e))
        except SourceError as e:
            return _error(400, str(e), hint=e.hint)
        except ValueError as e:
            return _error(400, str(e))
        return JSONResponse(out)

    async def query_distribution(request: Request) -> JSONResponse:
        try:
            body = await _body(request, selector=str)
        except _BadRequest as e:
            return _error(e.status, str(e), hint=e.hint)
        by = body.get("by", [])
        if not isinstance(by, list) or not all(isinstance(x, str) for x in by):
            return _error(400, "by must be a list of label names", hint='e.g. ["cloud_region"]')
        args = {k: body[k] for k in ("start", "end", "step", "source") if k in body}
        try:
            out = await service.query_distribution(body["selector"], by, **args, actor="user")
        except SourceError as e:
            return _error(400, str(e), hint=e.hint)
        except ValueError as e:
            return _error(400, str(e))
        return JSONResponse(out)

    async def panel_distribution(request: Request) -> JSONResponse:
        try:
            body = await _body(request, start_ms=int, end_ms=int)
        except _BadRequest as e:
            return _error(e.status, str(e), hint=e.hint)
        baseline = body.get("baseline", "previous")
        try:
            panel = await service.distribution_panel(
                request.path_params["id"], body["start_ms"], body["end_ms"],
                baseline if baseline in ("previous", "none") else "previous",
            )  # fmt: skip
        except NotFound as e:
            return _error(404, str(e))
        except ChartRejected as e:
            return _error(422, "chart rejected", issues=[i.model_dump() for i in e.issues])
        except SourceError as e:
            return _error(400, str(e), hint=e.hint)
        except ValueError as e:
            return _error(400, str(e))
        return JSONResponse({"panel": panel.to_dict()})

    async def show(request: Request) -> JSONResponse:
        try:
            body = await _body(request, dataset=str, question=str)
        except _BadRequest as e:
            return _error(e.status, str(e), hint=e.hint)
        unit = body.get("unit")
        if unit is not None and not isinstance(unit, str):
            return _error(400, "unit must be a string", hint='e.g. "s", "B", "req/s"')
        bounds: dict[str, float] = {}
        for k in ("bounds_lo", "bounds_hi"):
            if body.get(k) is not None:
                if isinstance(body[k], bool) or not isinstance(body[k], int | float):
                    return _error(400, f"{k} must be a number")
                bounds[k] = float(body[k])
        mark = body.get("mark", "auto")
        if not isinstance(mark, str):
            return _error(
                400, "mark must be a string", hint='e.g. "auto", "spectrum", "seasonal", "fleet"'
            )
        try:
            res = await service.show_auto(
                body["dataset"],
                body["question"],
                actor="user",
                unit=unit,
                raw=body.get("raw") is True,
                mark=mark,
                **bounds,
            )
        except ChartRejected as e:
            return _error(422, "chart rejected", issues=[i.model_dump() for i in e.issues])
        except NotFound as e:
            return _error(404, str(e))
        except ValueError as e:
            return _error(400, str(e))
        await service.y_context(res.panel.id, "user")
        return JSONResponse(
            {
                "panel": service.workspace.get_panel(res.panel.id).to_dict(),
                "issues": [i.model_dump() for i in res.issues],
            }
        )

    @_api
    async def catalog_list(request: Request) -> object:
        qp = request.query_params
        source = qp.get("source")
        if not source:
            raise _BadRequest(
                "missing parameter 'source'", "pass ?source=<name> (see /api/sources)"
            )

        def flag(name: str) -> bool:
            return qp.get(name, "").lower() in ("1", "true", "yes")

        reviewed = qp.get("reviewed")
        if reviewed not in (None, "", "yes", "no"):
            raise _BadRequest("invalid parameter 'reviewed'", "use reviewed=yes or reviewed=no")
        try:
            maxc = float(qp["max_confidence"]) if qp.get("max_confidence") else None
        except ValueError as e:
            raise _BadRequest("invalid parameter 'max_confidence'", "give a number in 0..1") from e
        b = Browse(
            q=qp.get("q") or None,
            prefix=qp.get("prefix") or None,
            origin=qp.get("origin") or None,
            max_confidence=maxc,
            conflicts=flag("conflicts"),
            findings=flag("findings"),
            reviewed=None if not reviewed else reviewed == "yes",
            removed=flag("removed"),
            members=flag("members"),
            family=qp.get("family") or None,
            sort=qp.get("sort", "name"),
            offset=max(0, _int_param(request, "offset", 0)),
            limit=_int_param(request, "limit", 50),
        )
        return service.ws.catalog_browse(source, b)

    @_api
    async def catalog_family_members(request: Request) -> object:
        return service.ws.family_members(
            request.path_params["source"],
            request.path_params["template"],
            max(0, _int_param(request, "offset", 0)),
            _int_param(request, "limit", 50),
        )

    @_api
    async def catalog_family_decide(request: Request) -> object:
        body = await _body(request, source=str, template=str, action=str)
        return service.ws.catalog_family_decide(
            body["source"],
            body["template"],
            body["action"],
            "user",
            "user",
            basis=body.get("basis"),
        )

    @_api
    async def catalog_metric(request: Request) -> object:
        return service.ws.metric_section(
            request.path_params["source"], request.path_params["metric"]
        )

    @_api
    async def panel_card(request: Request) -> object:
        return await service.panel_card(request.path_params["id"])

    @_api
    async def catalog_claim_create(request: Request) -> object:
        """The user confirms or edits a catalog field: origin user, confidence 1."""
        body = await _body(request, source=str, metric=str, field=str)
        if "value" not in body:
            raise _BadRequest("missing field 'value'", "send the value to record")
        if not service.ws.catalog.has_metric(body["source"], body["metric"]):
            raise NotFound(f"unknown metric {body['metric']!r} on {body['source']!r}")
        claim = service.ws.catalog_claim(
            body["source"], body["metric"], body["field"], body["value"], "user", "user"
        )
        return claim.model_dump()

    @_api
    async def panel_overlays(request: Request) -> object:
        body = await _body(request)
        flags = {}
        for name in ("normal", "limit", "ghost"):
            if name in body:
                if not isinstance(body[name], bool):
                    raise _BadRequest(f"invalid field {name!r}", f"{name!r} must be true or false")
                flags[name] = body[name]
        if not flags:
            raise _BadRequest("nothing to set", "give normal, limit and/or ghost as booleans")
        return await service.set_overlays(request.path_params["id"], "user", **flags)

    @_api
    async def panel_split_outcome(request: Request) -> object:
        """Follow-up: this latency histogram for successful and for failed requests, as two panels."""
        await _body(request)
        p = service.workspace.get_panel(request.path_params["id"])
        return await service.split_outcome(p.dataset_ids[0], "user")

    @_api
    async def panel_reframe(request: Request) -> object:
        """Accept a proposed reframing: a new panel from the suggestion at `index`."""
        body = await _body(request)
        index = body.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            raise _BadRequest("index must be an integer", "pick one from y.context.reframes")
        res = await service.reframe(request.path_params["id"], index, "user")
        return {"panel": res.panel.to_dict(), "issues": [i.model_dump() for i in res.issues]}

    @_api
    async def panel_y_context(request: Request) -> object:
        """Recompute a panel's y context (e.g. once its operating profile has finished)."""
        await _body(request)
        await service.y_context(request.path_params["id"], "user")
        return service.workspace.get_panel(request.path_params["id"]).to_dict()

    async def render_report(request: Request) -> JSONResponse:
        try:
            body = await _body(request)
        except _BadRequest as e:
            return _error(e.status, str(e), hint=e.hint)
        try:
            height = body.get("height_px")
            limit = (
                body["width_px"] * height / PX_PER_CELL
                if isinstance(height, int | float) and height > 0
                else RENDER_POINTS_PER_PX * body["width_px"]
            )
            exceeded = body["render_ms"] > RENDER_BUDGET_MS or body["points"] > limit
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
        await websocket.send_json(_ui_presence_frame())

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
                    # any consumer's change can alter the session list the UI shows (dtk)
                    if consumer == UI_CONSUMER or kind_of(consumer) != "ui":
                        await websocket.send_json(_ui_presence_frame())
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
    async def panel_y_view(request: Request) -> object:
        body = await _body(request)
        for key, typ in (
            ("mode", str),
            ("suggestion", str),
            ("lo", (int, float)),
            ("hi", (int, float)),
            ("baseline", str),
        ):
            if body.get(key) is not None and (
                not isinstance(body[key], typ) or isinstance(body[key], bool)
            ):
                raise _BadRequest(
                    f"invalid field {key!r}", "mode/suggestion are strings, lo/hi numbers"
                )
        pid = request.path_params["id"]
        baseline = body.get("baseline")
        ref = None
        if body.get("mode") == "indexed" and baseline in ("previous", "week"):
            ref = await service.ensure_reference(pid, baseline, "user")
        return ws.select_y_view(
            pid,
            "user",
            mode=body.get("mode"),
            suggestion=body.get("suggestion"),
            lo=body.get("lo"),
            hi=body.get("hi"),
            baseline=baseline,
            reference=ref,
        ).to_dict()

    async def panel_marginal(request: Request) -> JSONResponse:
        body = await request.json() if await request.body() else {}
        ref = body.get("reference")
        if ref is not None and not isinstance(ref, str):
            return _error(400, "reference must be previous, week, profile or null")
        try:
            await service.set_marginal(request.path_params["id"], ref, "user")
        except NotFound as e:
            return _error(404, str(e))
        except SourceError as e:
            return _error(400, str(e), hint=e.hint)
        except ValueError as e:
            return _error(400, str(e))
        return JSONResponse(service.workspace.get_panel(request.path_params["id"]).to_dict())

    @_api
    async def panel_data_view(request: Request) -> object:
        body = await _body(request)
        if not isinstance(body.get("view"), str):
            raise _BadRequest(
                "invalid field 'view'", "view is a string: overlay, filtered, removed, raw"
            )
        return ws.select_data_view(request.path_params["id"], body["view"], "user").to_dict()

    @_api
    async def list_sources(request: Request) -> object:
        return {"sources": service.source_list()}

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
    async def highlight_create(request: Request) -> object:
        body = await _body(request, object=str)
        note = body.get("note")
        if note is not None and not isinstance(note, str):
            raise _BadRequest("invalid field 'note'", "'note' must be a string")
        # user pins stay until the user clears them
        ws.highlight(body["object"], "user", note=note, ttl_ms=None)
        return {"ok": True}

    @_api
    async def highlight_clear(request: Request) -> object:
        await _body(request)
        ws.unhighlight(request.path_params["id"], "user")
        return {"ok": True}

    @_api
    async def panel_close(request: Request) -> object:
        await _body(request)
        return ws.close_panel(request.path_params["id"], "user").to_dict()

    @_api
    async def group_close(request: Request) -> object:
        """Close a panel group and all of its panels (bead czt.3)."""
        await _body(request)
        return ws.close_group(request.path_params["id"], "user").model_dump()

    @_api
    async def group_reframe(request: Request) -> object:
        """The same panel group over the selected window: a new group; this one stays."""
        body = await _body(request)
        a, b = body.get("start_ms"), body.get("end_ms")
        if not all(isinstance(x, int) and not isinstance(x, bool) for x in (a, b)) or a >= b:
            raise _BadRequest(
                "start_ms and end_ms must be integers, start before end",
                "send the selection window in epoch ms",
            )
        g = await service.reframe_group(request.path_params["id"], a, b, "user")
        return g.model_dump()

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

    @_api
    async def channel_sessions(request: Request) -> object:
        """Every connected consumer/session with its presence (dtk) — UI session list."""
        return {"sessions": presence.sessions()}

    def _ui_presence_frame() -> dict:
        """The UI's presence frame plus the full session list (per-session consumers, dtk)."""
        frame = dispatch.presence_frame(UI_CONSUMER)
        frame["sessions"] = presence.sessions()
        return frame

    routes = [
        Route("/api/health", health),
        Route("/api/workspace", workspace),
        Route("/api/sources", list_sources),
        Route("/api/events", list_events),
        Route("/api/annotations", annotation_create, methods=["POST"]),
        Route("/api/annotations/{id}/delete", annotation_delete, methods=["POST"]),
        Route("/api/hypotheses/{id}/status", hypothesis_status, methods=["POST"]),
        Route("/api/findings/{id}/verdict", finding_verdict, methods=["POST"]),
        Route("/api/threads", thread_create, methods=["POST"]),
        Route("/api/threads/{id}/messages", thread_message, methods=["POST"]),
        Route("/api/panels/{id}/close", panel_close, methods=["POST"]),
        Route("/api/groups/{id}/close", group_close, methods=["POST"]),
        Route("/api/groups/{id}/reframe", group_reframe, methods=["POST"]),
        Route("/api/focus", focus, methods=["POST"]),
        Route("/api/highlights", highlight_create, methods=["POST"]),
        Route("/api/highlights/{id}/clear", highlight_clear, methods=["POST"]),
        Route("/api/channel/claim", channel_claim, methods=["POST"]),
        Route("/api/channel/status", channel_status),
        Route("/api/channel/sessions", channel_sessions),
        Route("/api/panels", list_panels),
        Route("/api/code", code_list),
        Route("/api/code/{id}", code_get),
        Route("/api/code/{id}/rerun", code_rerun, methods=["POST"]),
        Route("/api/panels/{id}/y-view", panel_y_view, methods=["POST"]),
        Route("/api/panels/{id}/y-context", panel_y_context, methods=["POST"]),
        Route("/api/panels/{id}/reframe", panel_reframe, methods=["POST"]),
        Route("/api/panels/{id}/split-outcome", panel_split_outcome, methods=["POST"]),
        Route("/api/panels/{id}/overlays", panel_overlays, methods=["POST"]),
        Route("/api/panels/{id}/card", panel_card),
        Route("/api/catalog/claims", catalog_claim_create, methods=["POST"]),
        Route("/api/catalog", catalog_list),
        Route("/api/catalog/families", catalog_family_decide, methods=["POST"]),
        Route("/api/catalog/{source}/families/{template}/members", catalog_family_members),
        Route("/api/catalog/{source}/{metric}", catalog_metric),
        Route("/api/panels/{id}/marginal", panel_marginal, methods=["POST"]),
        Route("/api/panels/{id}/data-view", panel_data_view, methods=["POST"]),
        Route("/api/panels/{id}/data", panel_data),
        Route("/api/query", query, methods=["POST"]),
        Route("/api/query-distribution", query_distribution, methods=["POST"]),
        Route("/api/compare-seasonal", compare_seasonal, methods=["POST"]),
        Route("/api/show", show, methods=["POST"]),
        Route("/api/panels/{id}/distribution", panel_distribution, methods=["POST"]),
        Route("/api/render-report", render_report, methods=["POST"]),
        WebSocketRoute("/ws", events),
        WebSocketRoute("/ws/bridge", bridge),
    ]
    if ui_dir is not None and (ui_dir / "index.html").exists():
        routes.append(Mount("/", app=StaticFiles(directory=ui_dir, html=True)))
    if mcp is not None:
        mcp_app = mcp.streamable_http_app(
            streamable_http_path="/mcp",
            transport_security=mcp_transport_security(allowed_hosts),
        )
        routes[:0] = list(mcp_app.routes)  # before the static catch-all mount

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        try:
            service.code.startup()  # fail runs a previous daemon left running; GC run dirs
        except Exception:
            log.warning("run_code startup housekeeping failed", exc_info=True)
        # the sources' scrape spacing (bead wbw), in the background: startup never waits on it
        learning = asyncio.create_task(service.learn_resolutions())
        try:
            async with mcp.session_manager.run() if mcp is not None else contextlib.nullcontext():
                yield
        finally:
            learning.cancel()
            # Inside uvicorn's shutdown, before it re-raises SIGTERM/SIGINT: kill tier-2 kernels.
            if service.kernels is not None:
                await service.kernels.aclose()

    return Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))],
    )
