"""Metric context from a repo's code, docs and dashboards (bead 2as.18). Pure: text in, definitions out.

The daemon never reads the filesystem: Claude reads files in its own session and sends their text.
Extractors are deterministic and conservative: a name that is not a plain string (an f-string, a
concatenation) is reported as skipped, never guessed.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Literal

from telemetry_nerd.catalog.rules import normalize_unit

MAX_FILES = 50
MAX_BYTES = 1_000_000
SOURCE_CONFIDENCE = {"code": 0.85, "dashboard": 0.7, "docs": 0.6}

Kind = Literal["counter", "gauge", "histogram", "summary", "info", "enum"]


@dataclass(frozen=True)
class Definition:
    kind: Kind
    base: str  # the registered name as exposed, before type suffixes
    names: tuple[str, ...]  # series names this definition produces (histogram: bucket/sum/count)
    help: str | None
    unit: str | None  # canonical, or None
    labels: tuple[str, ...]
    buckets: tuple[float, ...] | None
    path: str
    line: int
    source_kind: Literal["code", "dashboard", "docs"]
    how: str  # e.g. "python prometheus_client Counter"
    #: (OTLP name, UCUM unit) for an OpenTelemetry instrument, so a collector's renames and
    #: exporter settings can be replayed over it (bead 2as.27)
    otel: tuple[str, str | None] | None = None

    @property
    def confidence(self) -> float:
        return SOURCE_CONFIDENCE[self.source_kind]

    @property
    def citation(self) -> str:
        return f"{self.source_kind}: {self.path}:{self.line} ({self.how})"


@dataclass(frozen=True)
class Skipped:
    path: str
    line: int
    reason: str


@dataclass(frozen=True)
class PanelInfo:
    """A dashboard panel: what it calls itself and the unit Grafana shows for its expressions."""

    title: str
    description: str | None
    unit_id: str | None
    exprs: tuple[str, ...]
    path: str


@dataclass
class Extraction:
    #: collector / relabel rules that change what a metric is called or measures between the code
    #: and the source (bead 2as.27); see context_yaml
    transforms: list = field(default_factory=list)
    definitions: list[Definition] = field(default_factory=list)
    panels: list[PanelInfo] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)


# naming -------------------------------------------------------------------------------------
_HIST = ("_bucket", "_sum", "_count")
_SUMMARY = ("_sum", "_count")


def prom_name(parts: list[str | None], unit: str | None) -> str:
    """prometheus_client / Go: namespace_subsystem_name, plus `_unit` when not already there."""
    name = "_".join(p for p in parts if p)
    if unit and not name.endswith(f"_{unit}"):
        name = f"{name}_{unit}"
    return name


def series_names(kind: Kind, base: str, *, client: str) -> tuple[str, ...]:
    """The exposed series names of a registered metric. Python's client appends `_total` to
    counters (Go's does not); histograms and summaries expose members; Info appends `_info`."""
    if kind == "counter":
        if client == "python":
            return (base.removesuffix("_total") + "_total",)
        return (base,)
    if kind == "histogram":
        return tuple(base + s for s in _HIST)
    if kind == "summary":
        return tuple(base + s for s in _SUMMARY)
    if kind == "info":
        return (base.removesuffix("_info") + "_info",)
    return (base,)


# OTLP -> Prometheus (the collector's translation) ------------------------------------------
_UCUM_SUFFIX = {
    "s": "seconds", "ms": "milliseconds", "us": "microseconds", "ns": "nanoseconds",
    "By": "bytes", "KiBy": "kibibytes", "MiBy": "mebibytes", "GiBy": "gibibytes",
    "Hz": "hertz", "Cel": "celsius", "W": "watts", "J": "joules", "V": "volts", "A": "amperes",
    "%": "percent",
}  # fmt: skip
_OTEL_KIND = {
    "counter": "counter", "up_down_counter": "gauge", "gauge": "gauge", "histogram": "histogram",
    "observable_counter": "counter", "observable_up_down_counter": "gauge", "observable_gauge": "gauge",
}  # fmt: skip


def otel_name(name: str, unit: str | None, kind: Kind) -> tuple[str, tuple[str, ...]]:
    base = re.sub(r"[^a-zA-Z0-9_:]", "_", name)
    suffix = _UCUM_SUFFIX.get((unit or "").strip())
    if suffix and not base.endswith(f"_{suffix}"):
        base = f"{base}_{suffix}"
    if kind == "counter":
        return base, (base.removesuffix("_total") + "_total",)
    if kind == "histogram":
        return base, tuple(base + s for s in _HIST)
    return base, (base,)


def _snake(camel: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", camel).lower()


# Python -------------------------------------------------------------------------------------
_PROM_CLASSES: dict[str, Kind] = {
    "Counter": "counter", "Gauge": "gauge", "Histogram": "histogram", "Summary": "summary",
    "Info": "info", "Enum": "enum",
}  # fmt: skip


def _module_strings(tree: ast.Module) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            out[node.targets[0].id] = node.value.value
    return out


def _str(node: ast.AST | None, consts: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in consts:
        return consts[node.id]
    return None


def _strings(node: ast.AST | None, consts: dict[str, str]) -> tuple[str, ...] | None:
    if isinstance(node, ast.List | ast.Tuple):
        vals = [_str(e, consts) for e in node.elts]
        return tuple(v for v in vals if v is not None) if all(v is not None for v in vals) else None
    return None


def _numbers(node: ast.AST | None) -> tuple[float, ...] | None:
    if isinstance(node, ast.List | ast.Tuple):
        vals = []
        for e in node.elts:
            if isinstance(e, ast.Constant) and isinstance(e.value, int | float):
                vals.append(float(e.value))
            elif isinstance(e, ast.Attribute | ast.Name | ast.Call):
                continue  # float("inf") / math.inf / INF: the +Inf bucket is implicit
            else:
                return None
        return tuple(vals)
    return None


def _arg(call: ast.Call, pos: int, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return call.args[pos] if len(call.args) > pos else None


def extract_python(path: str, text: str, out: Extraction) -> None:
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        out.skipped.append(Skipped(path, e.lineno or 0, f"not valid Python: {e.msg}"))
        return
    uses_prom = "prometheus_client" in text
    uses_otel = ".create_" in text
    consts = _module_strings(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        fname = (
            f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
        )
        if uses_prom and fname in _PROM_CLASSES:
            _python_prom(path, node, _PROM_CLASSES[fname], fname, consts, out)
        elif uses_otel and fname and fname.startswith("create_") and fname[7:] in _OTEL_KIND:
            _python_otel(path, node, fname, consts, out)


def _python_prom(path, call: ast.Call, kind: Kind, cls: str, consts, out: Extraction) -> None:
    name = _str(_arg(call, 0, "name"), consts)
    if name is None:
        out.skipped.append(Skipped(path, call.lineno, f"{cls}: the name is not a plain string"))
        return
    parts = []
    for key in ("namespace", "subsystem"):
        node = next((k.value for k in call.keywords if k.arg == key), None)
        val = _str(node, consts) if node is not None else None
        if node is not None and val is None:
            out.skipped.append(
                Skipped(path, call.lineno, f"{cls} {name}: {key} is not a plain string")
            )
            return
        parts.append(val)
    unit = _str(next((k.value for k in call.keywords if k.arg == "unit"), None), consts) or None
    base = prom_name([parts[0], parts[1], name], unit)
    labels = _strings(_arg(call, 2, "labelnames"), consts) or ()
    buckets = _numbers(next((k.value for k in call.keywords if k.arg == "buckets"), None))
    out.definitions.append(
        Definition(
            kind, base, series_names(kind, base, client="python"), _str(_arg(call, 1, "documentation"), consts),
            normalize_unit(unit), labels, buckets, path, call.lineno, "code",
            f"python prometheus_client {cls}",
        )
    )  # fmt: skip


def _python_otel(path, call: ast.Call, fname: str, consts, out: Extraction) -> None:
    name = _str(_arg(call, 0, "name"), consts)
    if name is None:
        out.skipped.append(Skipped(path, call.lineno, f"{fname}: the name is not a plain string"))
        return
    unit = _str(_arg(call, 1, "unit"), consts)
    kind = _OTEL_KIND[fname[7:]]
    base, names = otel_name(name, unit, kind)  # type: ignore[arg-type]
    out.definitions.append(
        Definition(
            kind, base, names, _str(_arg(call, 2, "description"), consts),  # type: ignore[arg-type]
            normalize_unit(unit), (), None, path, call.lineno, "code", f"python opentelemetry {fname}",
            (name, unit),
        )
    )  # fmt: skip


# calls in C-like languages (Go, JS/TS) ----------------------------------------------------
def call_args(text: str, open_paren: int) -> tuple[str, int] | None:
    """The text between a call's parentheses (strings and nesting respected), and where it ends."""
    depth, i, quote = 0, open_paren, ""
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote != "`":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "\"'`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1 : i], i
        i += 1
    return None


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _go_field(args: str, key: str) -> tuple[str | None, bool]:
    """(value, present): `Key: "literal"` gives the literal; any other expression is present but dynamic."""
    m = re.search(rf"\b{key}\s*:\s*([^,\n}}]+)", args)
    if not m:
        return None, False
    lit = re.fullmatch(r'\s*"((?:[^"\\]|\\.)*)"\s*', m.group(1)) or re.fullmatch(
        r"\s*`([^`]*)`\s*", m.group(1)
    )
    return (lit.group(1) if lit else None), True


_GO_PROM = re.compile(
    r"\b(?:prometheus|promauto)\.(?:\w+\.)?New(Counter|Gauge|Histogram|Summary)(Vec)?\s*\("
)
_GO_OTEL = re.compile(
    r"\.(?:Int64|Float64)(Counter|UpDownCounter|Histogram|Gauge|ObservableCounter|"
    r"ObservableUpDownCounter|ObservableGauge)\s*\("
)
_GO_KIND: dict[str, Kind] = {
    "Counter": "counter", "UpDownCounter": "gauge", "Histogram": "histogram", "Gauge": "gauge",
    "ObservableCounter": "counter", "ObservableUpDownCounter": "gauge", "ObservableGauge": "gauge",
}  # fmt: skip


def extract_go(path: str, text: str, out: Extraction) -> None:
    for m in _GO_PROM.finditer(text):
        got = call_args(text, m.end() - 1)
        if got is None:
            continue
        args, _ = got
        kind: Kind = m.group(1).lower()  # type: ignore[assignment]
        line = _line(text, m.start())
        name, has = _go_field(args, "Name")
        if not has or name is None:
            out.skipped.append(
                Skipped(path, line, f"New{m.group(1)}: the Name is not a plain string")
            )
            continue
        ns, ns_has = _go_field(args, "Namespace")
        sub, sub_has = _go_field(args, "Subsystem")
        if (ns_has and ns is None) or (sub_has and sub is None):
            out.skipped.append(
                Skipped(
                    path, line, f"New{m.group(1)} {name}: Namespace/Subsystem is not a plain string"
                )
            )
            continue
        help_, _ = _go_field(args, "Help")
        labels = tuple(
            re.findall(
                r'"([^"]+)"', (re.search(r"\[\]string\s*\{([^}]*)\}", args) or [None, ""])[1]
            )
        )
        bm = re.search(r"Buckets\s*:\s*\[\]float64\s*\{([^}]*)\}", args)
        buckets = (
            tuple(float(x) for x in re.findall(r"-?\d+(?:\.\d+)?(?:e-?\d+)?", bm.group(1)))
            if bm
            else None
        )
        base = prom_name([ns, sub, name], None)
        out.definitions.append(
            Definition(kind, base, series_names(kind, base, client="go"), help_, None, labels, buckets,
                       path, line, "code", f"go prometheus New{m.group(1)}{m.group(2) or ''}")
        )  # fmt: skip
    for m in _GO_OTEL.finditer(text):
        got = call_args(text, m.end() - 1)
        if got is None:
            continue
        args, _ = got
        line = _line(text, m.start())
        first = re.match(r'\s*"((?:[^"\\]|\\.)*)"', args)
        if not first:
            out.skipped.append(Skipped(path, line, f"{m.group(1)}: the name is not a plain string"))
            continue
        unit = (re.search(r'WithUnit\(\s*"([^"]*)"', args) or [None, None])[1]
        desc = (re.search(r'WithDescription\(\s*"((?:[^"\\]|\\.)*)"', args) or [None, None])[1]
        kind = _GO_KIND[m.group(1)]
        base, names = otel_name(first.group(1), unit, kind)
        out.definitions.append(
            Definition(kind, base, names, desc, normalize_unit(unit), (), None, path, line, "code",
                       f"go opentelemetry {m.group(1)}", (first.group(1), unit))
        )  # fmt: skip


_JS_OTEL = re.compile(
    r"\.create(Counter|UpDownCounter|Histogram|Gauge|ObservableCounter|ObservableUpDownCounter|ObservableGauge)\s*\("
)


def extract_js(path: str, text: str, out: Extraction) -> None:
    for m in _JS_OTEL.finditer(text):
        got = call_args(text, m.end() - 1)
        if got is None:
            continue
        args, _ = got
        line = _line(text, m.start())
        first = re.match(r"""\s*(['"`])((?:[^'"`\\]|\\.)*)\1""", args)
        if not first or "${" in first.group(2):
            out.skipped.append(
                Skipped(path, line, f"create{m.group(1)}: the name is not a plain string")
            )
            continue
        unit = (re.search(r"""\bunit\s*:\s*(['"])([^'"]*)\1""", args) or [None, None, None])[2]
        desc = (
            re.search(r"""\bdescription\s*:\s*(['"])((?:[^'"\\]|\\.)*)\1""", args)
            or [None, None, None]
        )[2]
        kind = _GO_KIND[m.group(1)]
        base, names = otel_name(first.group(2), unit, kind)
        out.definitions.append(
            Definition(kind, base, names, desc, normalize_unit(unit), (), None, path, line, "code",
                       f"javascript opentelemetry create{m.group(1)}", (first.group(2), unit))
        )  # fmt: skip


# Grafana dashboards -------------------------------------------------------------------------
def extract_dashboard(path: str, text: str, out: Extraction) -> bool:
    """True if `text` is a Grafana dashboard (a JSON object with panels)."""
    try:
        doc = json.loads(text)
    except ValueError:
        return False
    if not isinstance(doc, dict) or not isinstance(doc.get("panels"), list):
        return False

    def walk(panels):
        for p in panels:
            if not isinstance(p, dict):
                continue
            yield p
            yield from walk(p.get("panels") or [])

    for p in walk(doc["panels"]):
        targets = [
            t
            for t in p.get("targets") or []
            if isinstance(t, dict) and isinstance(t.get("expr"), str)
        ]
        if not targets:
            continue
        unit = ((p.get("fieldConfig") or {}).get("defaults") or {}).get("unit")
        if unit is None and isinstance(p.get("yaxes"), list) and p["yaxes"]:
            unit = (p["yaxes"][0] or {}).get("format")
        out.panels.append(
            PanelInfo(
                str(p.get("title") or ""),
                p.get("description") or None,
                unit,
                tuple(t["expr"] for t in targets),
                path,
            )
        )
    return True


#: Grafana unit id -> (canonical unit, per second). A "per second" unit applies to a rate: the
#: metric's own unit is the numerator.
GRAFANA_UNITS: dict[str, tuple[str, bool]] = {
    "s": ("s", False), "ms": ("ms", False), "µs": ("us", False), "ns": ("ns", False),
    "bytes": ("B", False), "decbytes": ("B", False), "bits": ("bit", False),
    "percent": ("%", False), "percentunit": ("ratio", False), "hertz": ("Hz", False),
    "celsius": ("°C", False), "watt": ("W", False), "joule": ("J", False),
    "reqps": ("count", True), "ops": ("count", True), "cps": ("count", True), "Bps": ("B", True),
    "binBps": ("B", True), "pps": ("count", True),
}  # fmt: skip


# Markdown tables ----------------------------------------------------------------------------
_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_TYPES = {"counter", "gauge", "histogram", "summary"}


def extract_markdown(path: str, text: str, out: Extraction) -> None:
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line.startswith("|") or set(line) <= set("|-: "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        name = next(
            (c.strip("`") for c in cells if _NAME.fullmatch(c.strip("`")) and "_" in c.strip("`")),
            None,
        )
        kind = next((c.lower() for c in cells if c.lower() in _TYPES), None)
        if name is None or kind is None:
            continue
        rest = [c for c in cells if c.strip("`") != name and c.lower() not in _TYPES and c]
        help_ = max(rest, key=len, default="") or None
        out.definitions.append(
            Definition(
                kind, name, (name,), help_, None, (), None, path, i, "docs", "markdown metric table"
            )  # type: ignore[arg-type]
        )


# dispatch -----------------------------------------------------------------------------------
def extract(files: list[dict]) -> Extraction:
    """Run every extractor that applies to each `{path, text}` (by extension, and by content for
    JSON dashboards)."""
    out = Extraction()
    for f in files:
        path, text = str(f["path"]), str(f["text"])
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext == "py":
            extract_python(path, text, out)
        elif ext == "go":
            extract_go(path, text, out)
        elif ext in ("js", "jsx", "ts", "tsx", "mjs", "cjs"):
            extract_js(path, text, out)
        elif ext == "json":
            if not extract_dashboard(path, text, out):
                out.skipped.append(Skipped(path, 0, "JSON that is not a Grafana dashboard"))
        elif ext in ("md", "markdown"):
            extract_markdown(path, text, out)
        elif ext in ("yaml", "yml"):
            from telemetry_nerd.catalog.context_yaml import extract_yaml

            extract_yaml(path, text, out)
        else:
            out.skipped.append(
                Skipped(path, 0, f"no extractor for .{ext or 'files without an extension'}")
            )
    return out
