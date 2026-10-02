"""Collector and relabel configs as context (bead 2as.27). Deterministic; YAML is parsed with
`safe_load` and nothing in it is executed.

Between the code and the source, a pipeline may rename a metric, scale its values, prefix it or
change how OTLP names become Prometheus names. A code definition then no longer matches the series
the source holds. This reads those rules and replays them over code definitions, so
`http.server.duration` registered in code still finds `acme_http_server_request_duration_seconds`.

Understood (everything else is reported as skipped, never guessed):
- OpenTelemetry Collector `metricstransform`: `update`/`insert` with `new_name` (strict or regexp
  `include`), `experimental_scale_value` operations.
- Collector `transform` (OTTL), only `set(name, "x") where name == "y"` and `set(unit, "u") where name == "y"`.
- `prometheus` / `prometheusremotewrite` exporters: `namespace`, `add_metric_suffixes`.
- Prometheus `metric_relabel_configs` / `write_relabel_configs`: `replace` on `__name__`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import yaml

from telemetry_nerd.catalog.rules import normalize_unit

if TYPE_CHECKING:
    from telemetry_nerd.catalog.context import Definition, Extraction

CONFIDENCE_PENALTY = 0.05  # a longer inference chain than a plain registration
_TIME = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0}


@dataclass(frozen=True)
class NameTransform:
    kind: str  # rename | prefix | suffixes | unit
    path: str  # file and key path, the citation
    how: str  # "collector metricstransform", ...
    old: str | None = None  # rename: exact name or regex
    new: str | None = None  # rename: replacement (regex: `\1` / `$1` groups)
    regex: bool = False
    value: Any = None  # prefix: str; suffixes: bool; unit: str
    scale: float | None = None  # rename: values are multiplied by this
    stage: str = "otel"  # "otel": before OTLP->Prometheus naming; "prom": on exposed names
    group: str = ""  # exporter name: its namespace and suffix setting belong together

    @property
    def cite(self) -> str:
        return f"{self.how} {self.path}"


# extraction ---------------------------------------------------------------------------------
def extract_yaml(path: str, text: str, out: Extraction) -> None:
    from telemetry_nerd.catalog.context import Skipped

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        out.skipped.append(Skipped(path, 0, f"invalid YAML: {str(e).splitlines()[0]}"))
        return
    if not isinstance(doc, dict):
        out.skipped.append(Skipped(path, 0, "YAML that is not a mapping"))
        return
    found = 0
    if any(k in doc for k in ("processors", "exporters", "receivers", "service")):
        found += _collector(path, doc, out)
    if "scrape_configs" in doc or "remote_write" in doc:
        found += _prometheus(path, doc, out)
    if not found:
        out.skipped.append(Skipped(path, 0, "YAML with no collector or relabel rules this reads"))


def _collector(path: str, doc: dict, out: Extraction) -> int:
    from telemetry_nerd.catalog.context import Skipped

    n = 0
    for name, cfg in (doc.get("processors") or {}).items():
        cfg = cfg or {}
        kind = name.split("/", 1)[0]
        where = f"{path}#processors.{name}"
        if kind == "metricstransform":
            for i, t in enumerate(cfg.get("transforms") or []):
                n += _metricstransform(f"{where}.transforms[{i}]", t, out)
        elif kind == "transform":
            for ci, c in enumerate(cfg.get("metric_statements") or []):
                for si, stmt in enumerate((c or {}).get("statements") or []):
                    got = _ottl(f"{where}.metric_statements[{ci}].statements[{si}]", str(stmt), out)
                    n += got
                    if not got:
                        out.skipped.append(
                            Skipped(
                                path, 0, f"processors.{name}: OTTL not understood: {str(stmt)[:80]}"
                            )
                        )
    for name, cfg in (doc.get("exporters") or {}).items():
        cfg = cfg or {}
        kind = name.split("/", 1)[0]
        if kind not in ("prometheus", "prometheusremotewrite"):
            continue
        where = f"{path}#exporters.{name}"
        if isinstance(cfg.get("namespace"), str) and cfg["namespace"]:
            out.transforms.append(
                NameTransform(
                    "prefix",
                    f"{where}.namespace",
                    f"collector {kind} exporter",
                    value=cfg["namespace"],
                    group=name,
                )
            )
            n += 1
        if cfg.get("add_metric_suffixes") is False:
            out.transforms.append(
                NameTransform(
                    "suffixes",
                    f"{where}.add_metric_suffixes",
                    f"collector {kind} exporter",
                    value=False,
                    group=name,
                )
            )
            n += 1
    return n


def _metricstransform(where: str, t: Any, out: Extraction) -> int:
    from telemetry_nerd.catalog.context import Skipped

    if not isinstance(t, dict) or not isinstance(t.get("include"), str):
        return 0
    regex = t.get("match_type") == "regexp"
    scale = None
    for op in t.get("operations") or []:
        if isinstance(op, dict) and op.get("action") == "experimental_scale_value":
            s = op.get("experimental_scale")
            if isinstance(s, int | float) and not isinstance(s, bool) and s > 0:
                scale = float(s)
    new = t.get("new_name")
    if t.get("action") in ("update", "insert") and isinstance(new, str) and new:
        if regex:
            try:
                re.compile(t["include"])
            except re.error:
                out.skipped.append(
                    Skipped(where, 0, "metricstransform: include is not a valid regex")
                )
                return 0
        out.transforms.append(
            NameTransform(
                "rename", where, "collector metricstransform", t["include"], new, regex, scale=scale
            )
        )
        return 1
    if scale is not None:
        out.transforms.append(
            NameTransform(
                "rename",
                where,
                "collector metricstransform",
                t["include"],
                t["include"],
                regex,
                scale=scale,
            )
        )
        return 1
    return 0


_OTTL = re.compile(
    r"""set\(\s*(?P<what>metric\.name|name|metric\.unit|unit)\s*,\s*(?P<v>"[^"]*")\s*\)\s*
        (?:where\s+(?:metric\.)?name\s*==\s*(?P<w>"[^"]*"))?\s*$""",
    re.VERBOSE,
)


def _ottl(where: str, stmt: str, out: Extraction) -> int:
    m = _OTTL.match(stmt.strip())
    if m is None or m.group("w") is None:
        return 0
    old, value = m.group("w")[1:-1], m.group("v")[1:-1]
    if m.group("what").endswith("name"):
        out.transforms.append(NameTransform("rename", where, "collector transform", old, value))
    else:
        out.transforms.append(NameTransform("unit", where, "collector transform", old, value=value))
    return 1


def _prometheus(path: str, doc: dict, out: Extraction) -> int:
    n = 0
    blocks: list[tuple[str, list]] = []
    for i, sc in enumerate(doc.get("scrape_configs") or []):
        blocks.append(
            (
                f"scrape_configs[{i}].metric_relabel_configs",
                (sc or {}).get("metric_relabel_configs") or [],
            )
        )
    for i, rw in enumerate(doc.get("remote_write") or []):
        blocks.append(
            (
                f"remote_write[{i}].write_relabel_configs",
                (rw or {}).get("write_relabel_configs") or [],
            )
        )
    for key, rules in blocks:
        for j, r in enumerate(rules):
            if not isinstance(r, dict) or r.get("target_label") != "__name__":
                continue
            if (r.get("action") or "replace") != "replace":
                continue
            if r.get("source_labels") not in (["__name__"], "__name__"):
                continue
            regex, repl = str(r.get("regex", "(.*)")), str(r.get("replacement", "$1"))
            try:
                re.compile(regex)
            except re.error:
                continue
            out.transforms.append(
                NameTransform(
                    "rename",
                    f"{path}#{key}[{j}]",
                    "prometheus relabel",
                    regex,
                    repl,
                    True,
                    stage="prom",
                )
            )
            n += 1
    return n


# replay -------------------------------------------------------------------------------------
def _sub(pattern: str, repl: str, name: str) -> str | None:
    """A relabel/collector rename on one name (full match; `$1` and `\\1` both mean group 1)."""
    m = re.fullmatch(pattern, name)
    if m is None:
        return None
    r = re.sub(r"\$\{?(\d+)\}?", r"\\\1", repl)
    return m.expand(r)


def _renamed(t: NameTransform, name: str) -> str | None:
    if t.regex:
        return _sub(t.old or "", t.new or "", name)
    return t.new if name == t.old else None


def _scaled_unit(unit: str | None, factor: float) -> str | None:
    """The unit after values are multiplied by `factor`: only where it is again a canonical unit."""
    if unit is None:
        return None
    if unit in _TIME:
        scaled = _TIME[unit] * factor
        return next((u for u, m in _TIME.items() if abs(m - scaled) < 1e-12 * max(m, 1)), None)
    return None


def _exporter_configs(transforms: list[NameTransform]) -> list[dict[str, NameTransform]]:
    """One config per exporter that sets something (an exporter's namespace and suffix setting go
    together, and which exporter carries a metric is not known): [{}] when none does."""
    by: dict[str, dict[str, NameTransform]] = {}
    for t in transforms:
        if t.kind in ("prefix", "suffixes"):
            by.setdefault(t.group, {})[t.kind] = t
    return list(by.values()) or [{}]


def _unit_word(unit: str | None) -> str | None:
    from telemetry_nerd.catalog.context import _UCUM_SUFFIX

    return _UCUM_SUFFIX.get(unit or "")


def variants(d: Definition, transforms: list[NameTransform]) -> list[Definition]:
    """What `d` would be called (and measure) after the pipeline's rules: one extra definition per
    way the pipeline could carry it (each exporter), saying which rules produced it. The original
    is not among them."""
    from telemetry_nerd.catalog.context import otel_name

    if not transforms:
        return []
    out: list[Definition] = []
    for exporter in _exporter_configs(transforms) if d.otel is not None else [{}]:
        via: list[str] = []
        unit, scale, unit_stated = d.unit, 1.0, False
        base, names = d.base, d.names
        if d.otel is not None:
            # collector rules work on the OTLP name; then the exporter turns it into a Prometheus name
            raw_name, raw_unit = d.otel
            original = raw_name
            for t in (t for t in transforms if t.stage == "otel"):
                if t.kind == "unit" and t.old in (original, raw_name):
                    raw_unit, unit, unit_stated = t.value, normalize_unit(t.value), True
                    via.append(t.cite)
                elif t.kind == "rename" and (new := _renamed(t, raw_name)) is not None:
                    raw_name = new
                    scale *= t.scale or 1.0
                    via.append(t.cite)
            if "suffixes" in exporter:
                base = re.sub(r"[^a-zA-Z0-9_:]", "_", raw_name)
                names = (
                    tuple(base + s for s in ("_bucket", "_sum", "_count"))
                    if d.kind == "histogram"
                    else (base,)
                )
                via.append(exporter["suffixes"].cite)
            else:
                base, names = otel_name(raw_name, raw_unit, d.kind)
            if "prefix" in exporter:  # the exporter's namespace applies to OTLP metrics only
                ns = exporter["prefix"].value
                base, names = f"{ns}_{base}", tuple(f"{ns}_{n}" for n in names)
                via.append(exporter["prefix"].cite)
        # renames of exposed names: relabel rules, and collector rules on a metric that is not OTLP
        for t in transforms:
            if t.kind != "rename" or not (t.stage == "prom" or d.otel is None):
                continue
            moved = [(n, _renamed(t, n)) for n in names]
            if any(new for _, new in moved):
                names = tuple(new or n for n, new in moved)
                base = _renamed(t, base) or base
                scale *= t.scale or 1.0
                via.append(t.cite)
        if scale != 1.0 and not unit_stated:  # a stated unit is the config's own word
            # values were scaled, but a name suffix still follows the instrument's unit metadata:
            # claim the new unit only where the name agrees with it, never against its own name
            scaled = _scaled_unit(unit, scale)
            word = _unit_word(scaled)
            agrees = d.otel is None or (word is not None and word in names[0])
            unit = scaled if scaled and agrees else None
        if not via or (names == d.names and unit == d.unit):
            continue
        v = replace(
            d, base=base, names=names, unit=unit,
            how=f"{d.how}; through {', '.join(dict.fromkeys(via))}", otel=None,
        )  # fmt: skip
        if all(v.names != o.names or v.unit != o.unit for o in out):
            out.append(v)
    return out


def confidence_of(d: Definition, via_pipeline: bool) -> float:
    """A match through the pipeline's rules is a longer inference than a plain registration."""
    return d.confidence - CONFIDENCE_PENALTY if via_pipeline else d.confidence
