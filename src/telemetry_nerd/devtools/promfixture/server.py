"""Fixture PromQL source for the UI e2e suite (bead y7hb): `python -m telemetry_nerd.devtools.promfixture`.

Serves the subset of the Prometheus HTTP API the daemon calls, over an in-memory store seeded
with the synthetic demo series anchored at the server's start, so every run reads the same data
relative to "now" and nothing depends on an external VictoriaMetrics or its history:

* GET|POST /api/v1/query, /api/v1/query_range        (engine.Engine)
* GET /api/v1/labels, /api/v1/label/<name>/values, /api/v1/series, /api/v1/metadata
* GET /api/v1/status/buildinfo, /api/v1/status/tsdb
* POST /api/v1/import/prometheus, /api/v1/admin/tsdb/delete_series, GET /internal/force_flush
  (the VictoriaMetrics import API: specs add their own uniquely named series per test)

Unsupported PromQL is answered as an execution error naming the construct (HTTP 422), and logged.
"""

from __future__ import annotations

import argparse
import logging
import math
import re
from datetime import datetime

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from telemetry_nerd.devtools.promfixture.dataset import seed
from telemetry_nerd.devtools.promfixture.engine import Engine, EvalError, Unsupported, parse
from telemetry_nerd.devtools.promfixture.engine import VSel as _VSel
from telemetry_nerd.devtools.promfixture.store import Matcher, Store
from telemetry_nerd.model.time import now_ms

log = logging.getLogger("promfixture")

_UNITS_MS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}


def fmt(v: float) -> str:
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "+Inf" if v > 0 else "-Inf"
    return repr(float(v))


def parse_time(text: str) -> int:
    try:
        return round(float(text) * 1000)
    except ValueError:
        return round(datetime.fromisoformat(text).timestamp() * 1000)


def parse_step(text: str) -> int:
    try:
        return round(float(text) * 1000)
    except ValueError:
        pass
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w)", text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise ValueError(f"invalid duration {text!r}")
    return round(sum(float(n) * _UNITS_MS[u] for n, u in parts))


def _ok(data: object) -> JSONResponse:
    return JSONResponse({"status": "success", "data": data})


def _err(kind: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"status": "error", "errorType": kind, "error": message}, status_code=status
    )


async def _params(request: Request) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for k, v in request.query_params.multi_items():
        out.setdefault(k, []).append(v)
    if request.method == "POST" and request.headers.get("content-type", "").startswith(
        "application/x-www-form-urlencoded"
    ):
        form = await request.form()
        for k, v in form.multi_items():
            out.setdefault(k, []).append(str(v))
    return out


def _one(p: dict[str, list[str]], key: str, default: str | None = None) -> str | None:
    return p.get(key, [default])[-1]


def _selector_matchers(text: str) -> list[Matcher]:
    node = parse(text)
    if not isinstance(node, _VSel):
        raise EvalError(f"match[] must be a series selector: {text!r}")
    return node.matchers


def create_app(store: Store, clock=now_ms) -> Starlette:
    engine = Engine(store, clock=clock)

    def matched(p: dict[str, list[str]]):
        start = _one(p, "start")
        end = _one(p, "end")
        lo = parse_time(start) if start else None
        # never list a series whose samples are all still in the future
        hi = min(parse_time(end), clock()) if end else clock()
        sels = p.get("match[]") or []
        if not sels:
            return store.select([], lo, hi)
        seen: dict[int, object] = {}
        for sel in sels:
            for s in store.select(_selector_matchers(sel), lo, hi):
                seen[id(s)] = s
        return list(seen.values())

    def guarded(handler):
        async def run(request: Request) -> Response:
            try:
                return await handler(request)
            except Unsupported as e:
                log.warning("unsupported PromQL (%s): %s", e, dict(request.query_params))
                return _err("execution", f"fixture engine does not support {e}", 422)
            except (EvalError, ValueError, KeyError) as e:
                log.warning("bad request (%s): %s", e, dict(request.query_params))
                return _err("bad_data", str(e), 400)

        return run

    async def query(request: Request) -> Response:
        p = await _params(request)
        expr = _one(p, "query") or ""
        t = parse_time(_one(p, "time") or str(clock() / 1000))
        kind, result = engine.instant(expr, t)
        limit = int(_one(p, "limit") or 0)
        if kind == "scalar":
            return _ok({"resultType": kind, "result": [t / 1000, fmt(result)]})  # type: ignore[arg-type]
        if kind == "string":
            return _ok({"resultType": kind, "result": [t / 1000, result]})
        if kind == "matrix":
            rows = [
                {"metric": lb, "values": [[x / 1000, fmt(v)] for x, v in zip(ts, vs, strict=True)]}
                for lb, ts, vs in result  # type: ignore[union-attr]
            ]
        else:
            rows = [{"metric": lb, "value": [t / 1000, fmt(v)]} for lb, v in result]  # type: ignore[union-attr]
        return _ok({"resultType": kind, "result": rows[:limit] if limit else rows})

    async def query_range(request: Request) -> Response:
        p = await _params(request)
        start = parse_time(_one(p, "start") or "")
        end = parse_time(_one(p, "end") or "")
        step = parse_step(_one(p, "step") or "")
        if step <= 0 or end < start:
            raise EvalError("bad start/end/step")
        if (end - start) // step > 11_000:
            raise EvalError(
                "exceeded maximum resolution of 11,000 points per timeseries. "
                "Try decreasing the query resolution (?step=XX)"
            )
        result = engine.range(_one(p, "query") or "", start, end, step)
        rows = [
            {"metric": lb, "values": [[x / 1000, fmt(v)] for x, v in zip(ts, vs, strict=True)]}
            for lb, ts, vs in result
        ]
        return _ok({"resultType": "matrix", "result": rows})

    async def labels(request: Request) -> Response:
        names = {k for s in matched(await _params(request)) for k in s.labels}
        return _ok(sorted(names))

    async def label_values(request: Request) -> Response:
        p = await _params(request)
        name = request.path_params["name"]
        vals = sorted({s.labels[name] for s in matched(p) if name in s.labels})
        limit = int(_one(p, "limit") or 0)
        return _ok(vals[:limit] if limit else vals)

    async def series(request: Request) -> Response:
        return _ok([s.labels for s in matched(await _params(request))])

    async def metadata(request: Request) -> Response:
        p = await _params(request)
        want = _one(p, "metric")
        return _ok(
            {
                name: [{"type": m["type"], "help": m["help"], "unit": m["unit"]}]
                for name, m in sorted(store.metadata.items())
                if want is None or name == want
            }
        )

    async def buildinfo(request: Request) -> Response:
        return _ok({"application": "telemetry-nerd-fixture", "version": "fixture"})

    async def tsdb(request: Request) -> Response:
        p = await _params(request)
        n = int(_one(p, "topN") or _one(p, "limit") or 10)
        counts: dict[str, int] = {}
        for s in store:
            name = s.labels.get("__name__", "")
            counts[name] = counts.get(name, 0) + 1
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
        return _ok(
            {
                "totalSeries": len(store),
                "seriesCountByMetricName": [{"name": k, "value": v} for k, v in top],
            }
        )

    async def import_prometheus(request: Request) -> Response:
        text = (await request.body()).decode()
        store.import_text(text, clock())
        return Response(status_code=204)

    async def delete_series(request: Request) -> Response:
        p = await _params(request)
        for sel in p.get("match[]") or []:
            store.delete(_selector_matchers(sel))
        return Response(status_code=204)

    async def flush(request: Request) -> Response:
        return Response(status_code=200)

    both = ["GET", "POST"]
    routes = [
        Route("/api/v1/query", guarded(query), methods=both),
        Route("/api/v1/query_range", guarded(query_range), methods=both),
        Route("/api/v1/labels", guarded(labels), methods=both),
        Route("/api/v1/label/{name}/values", guarded(label_values), methods=["GET"]),
        Route("/api/v1/series", guarded(series), methods=both),
        Route("/api/v1/metadata", guarded(metadata), methods=["GET"]),
        Route("/api/v1/status/buildinfo", buildinfo, methods=["GET"]),
        Route("/api/v1/status/tsdb", guarded(tsdb), methods=["GET"]),
        Route("/api/v1/import/prometheus", guarded(import_prometheus), methods=["POST"]),
        Route("/api/v1/admin/tsdb/delete_series", guarded(delete_series), methods=["POST"]),
        Route("/internal/force_flush", flush, methods=["GET"]),
    ]
    return Starlette(routes=routes)


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7079)
    parser.add_argument("--hours", type=int, default=6, help="demo history before the anchor")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="promfixture: %(message)s")
    store = Store()
    anchor = seed(store, now_ms(), hours=args.hours)
    log.info("seeded %d series anchored at %d on :%d", len(store), anchor, args.port)
    # keep-alive past clients' 5s idle pools, as the daemon does (cli._KEEP_ALIVE_S, 3szb)
    uvicorn.run(
        create_app(store),
        host=args.host,
        port=args.port,
        log_level="warning",
        timeout_keep_alive=75,
    )
