"""An OpenTelemetry-demo-shaped fake source (tests/fixtures/demo/otel-demo.json): discovery with
the demo's metric names and metadata, a label index (`label_values`), and a tiny evaluator that
answers `sum by (...) (rate(metric{matchers}[w]))` from the fixture's constant per-series rates
(a fault rate from the onset; error series born at the onset, as the span-metrics connector does).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

import pyarrow as pa

from telemetry_nerd.model.discovery import Discovery, MetricInfo
from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult, labels_json
from telemetry_nerd.model.series import series_id as make_series_id
from telemetry_nerd.model.time import TimeRange

from .fakes import NOW

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "demo" / "otel-demo.json"
_MATCHER = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"')
_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_BY = re.compile(r"\bby\s*\(([^)]*)\)")


def load() -> dict:
    return json.loads(FIXTURE.read_text())


def _matches(labels: dict[str, str], matchers: list[tuple[str, str, str]]) -> bool:
    for k, op, v in matchers:
        got = labels.get(k, "")
        v = v.replace('\\"', '"').replace("\\\\", "\\")
        ok = {
            "=": got == v,
            "!=": got != v,
            "=~": re.fullmatch(v, got) is not None,
            "!~": re.fullmatch(v, got) is None,
        }[op]
        if not ok:
            return False
    return True


class DemoSource:
    semantics = None

    def __init__(self, name: str = "default", resolution_ms: int = 15_000, end_ms: int = NOW):
        self.doc = load()
        self.name = name
        self.identity = f"demo|{name}|{resolution_ms}"
        self.resolution_ms = resolution_ms
        self.end_ms = end_ms
        self.onset_ms = end_ms - self.doc["onset_s"] * 1000
        self.label_calls: list[tuple[str, tuple[str, ...]]] = []
        self.series = [
            {**s, "labels": {"__name__": s["metric"], **s["labels"]}} for s in self.doc["series"]
        ]

    # discovery ---------------------------------------------------------------------------
    async def probe(self) -> dict:
        return {"reachable": True, "latency_ms": 0, "version": "demo"}

    async def discover(self) -> Discovery:
        meta = self.doc["metadata"]
        names = sorted({s["metric"] for s in self.series})
        hist = {m for m, e in meta.items() if e[0]["type"] == "histogram"}
        infos = []
        for n in [*names, *sorted(hist)]:
            e = meta.get(n, [{}])[0]
            infos.append(
                MetricInfo(n, e.get("type") or None, e.get("help") or None, e.get("unit") or None)
            )
        labels = sorted({k for s in self.series for k in s["labels"]} - {"__name__"})
        return Discovery(
            tuple(infos), tuple(labels), {h: "classic" for h in hist}, None, 1.0, (), False
        )

    async def scrape_interval(self, selector: str, at_ms: int | None = None) -> int | None:
        return self.resolution_ms

    # the label index -----------------------------------------------------------------------
    def _alive(self, s: dict, a: int, b: int) -> bool:
        first = self.onset_ms if s.get("born_at_onset") else -(10**15)
        last = self.end_ms - 1000 * s.get("last_seen_before_end_s", 0)
        return first <= b and last >= a

    def _selected(self, selector: str) -> list[dict]:
        sel = selector.strip()
        name = None
        if not sel.startswith("{"):
            name = _NAME.match(sel)[0]  # type: ignore[index]
        body = sel[sel.index("{") + 1 : sel.rindex("}")] if "{" in sel else ""
        ms = _MATCHER.findall(body)
        return [
            s
            for s in self.series
            if (name is None or s["metric"] == name) and _matches(s["labels"], ms)
        ]

    async def label_values(
        self,
        label: str,
        match: Sequence[str] = (),
        rng: TimeRange | None = None,
        limit: int | None = None,
    ) -> list[str]:
        self.label_calls.append((label, tuple(match)))
        pool = [s for m in match for s in self._selected(m)] if match else self.series
        a, b = (rng.start_ms, rng.end_ms) if rng else (-(10**15), 10**15)
        vals = sorted(
            {s["labels"][label] for s in pool if label in s["labels"] and self._alive(s, a, b)}
        )
        return vals[:limit] if limit is not None else vals

    # samples -------------------------------------------------------------------------------
    def _value(self, s: dict, t: int) -> float | None:
        if not self._alive(s, t, t):
            return None
        rate = s.get("fault_rate", s["rate"]) if t >= self.onset_ms else s["rate"]
        # deterministic +-10% noise per series and time: real rates are never flat
        h = hashlib.blake2b(f"{s['labels']}|{t}".encode(), digest_size=4).digest()
        return rate * (1 + 0.2 * (int.from_bytes(h, "big") / 2**32 - 0.5))

    async def fetch(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult:
        known = {s["metric"] for s in self.series}
        metric = next((n for n in _NAME.findall(expr) if n in known), None)
        if metric is None:
            return self._table({}, rng, step_ms)
        brace = expr.find("{", expr.index(metric))
        body = ""
        if brace == expr.index(metric) + len(metric):
            body = expr[brace + 1 : expr.index("}", brace)]
        ms = _MATCHER.findall(body)
        picked = [s for s in self.series if s["metric"] == metric and _matches(s["labels"], ms)]
        by = _BY.search(expr)
        keys = [k.strip() for k in by[1].split(",") if k.strip()] if by else None
        groups: dict[str, list[dict]] = defaultdict(list)
        for s in picked:
            lab = (
                {k: s["labels"][k] for k in keys if k in s["labels"]}
                if keys is not None
                else ({} if expr.lstrip().startswith("sum") else s["labels"])
            )
            groups[labels_json(lab)].append(s)
        return self._table(groups, rng, step_ms)

    def _table(self, groups: dict[str, list[dict]], rng: TimeRange, step_ms: int) -> FetchResult:
        rows, sids, labs = [], [], []
        for lab_json, members in groups.items():
            sid = make_series_id(self.name, json.loads(lab_json))
            sids.append(sid)
            labs.append(lab_json)
            for t in range(rng.start_ms, rng.end_ms + 1, step_ms):
                vs = [v for v in (self._value(s, t) for s in members) if v is not None]
                if vs:
                    rows.append((t, sid, float(sum(vs))))
        n = max(1, step_ms // self.resolution_ms)
        buckets = pa.table(
            {
                "ts_ms": [r[0] for r in rows],
                "series_id": [r[1] for r in rows],
                "avg": [r[2] for r in rows],
                "min": [r[2] for r in rows],
                "max": [r[2] for r in rows],
                "count": [n] * len(rows),
            },
            schema=BUCKET_SCHEMA,
        )
        series = pa.table({"series_id": sids, "labels": labs}, schema=SERIES_SCHEMA)
        return FetchResult(buckets, series)

    async def fetch_values(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult:
        return await self.fetch(expr, rng, step_ms)

    async def fetch_histogram(self, selector, by, rng, step_ms):
        raise NotImplementedError("the demo fixture has no bucket layouts")
