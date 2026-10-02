"""Run-directory layout and meta schema shared by the daemon and the kernel (spec §5.2).

Stdlib only: `telemetry_nerd.tn` imports this in every kernel.

Layout of one run (`<data_dir>/runs/<code-node-id>/`)::

    run.json                      manifest, written last by the daemon: {version, code_node, inputs}
    inputs/<handle>.arrow         Arrow IPC *file* (mmap-able), the dataset rows
    inputs/<handle>.<table>.arrow other tables: "series" (series_id, labels), "columns" (dist n)
    inputs/<handle>.meta.json     dataset meta + handle, unit, caveats, tables
    outputs/<name>[.<table>].arrow
    outputs/<name>.meta.json      written LAST: its presence commits the output
    ingested.json                 {name: dataset id}, rewritten by the daemon after each ingest

Every file is written to a hidden temp name in the same directory, fsynced and renamed, so a
reader never sees a half-written file, and a mmapped file is never truncated under a reader.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

VERSION = 1
ENV_RUN_DIR = "TN_RUN_DIR"
MANIFEST = "run.json"
INPUTS = "inputs"
OUTPUTS = "outputs"
INGESTED = "ingested.json"
META_SUFFIX = ".meta.json"
ARROW_SUFFIX = ".arrow"
TMP_PREFIX = ".tmp-"

TIME_SERIES = ("bucket_agg", "sample")
DISTRIBUTION = "distribution"
ESTIMATE = "estimate"
EXPORTABLE = (*TIME_SERIES, DISTRIBUTION, ESTIMATE)
PUTTABLE = (*TIME_SERIES, DISTRIBUTION)  # estimate only through put_fit
INTERVAL_KINDS = ("confidence", "credible", "prediction", "tolerance")
NO_UNCERTAINTY = "no_uncertainty"

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

USER_KEYS = frozenset(
    {
        "name", "representation", "like", "step_ms", "resolution_ms", "start_ms", "end_ms",
        "unit", "description", "labels", "uncertainty", "exact", "caveats", "parents",
        "scheme", "n_min",
    }
)  # fmt: skip
INTERNAL_KEYS = frozenset(
    {"version", "kind", "files", "rows", "bytes", "semantics_flags", "fit", "prediction_of"}
)


class ExchangeError(ValueError):
    """A run directory, meta or table that breaks the exchange contract (message says how)."""


def check_id(value: str, what: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise ExchangeError(f"{what} {value!r} is not a valid id (letters, digits, _ or -)")
    return value


def check_name(value: Any) -> str:
    if not isinstance(value, str) or not _NAME.match(value):
        raise ExchangeError(
            f"output name {value!r} must be lowercase letters, digits and _ "
            "(start with a letter, at most 64 chars)"
        )
    return value


def table_file(handle: str, table: str = "rows") -> str:
    return f"{handle}{ARROW_SUFFIX}" if table == "rows" else f"{handle}.{table}{ARROW_SUFFIX}"


def meta_file(handle: str) -> str:
    return f"{handle}{META_SUFFIX}"


# --- atomic writes -------------------------------------------------------------------------


def write_atomic(path: Path, write: Callable[[Any], None], mode: str = "wb") -> None:
    """Write via `write(fileobj)` to a temp file beside `path`, fsync, rename over `path`."""
    fd, tmp = tempfile.mkstemp(prefix=f"{TMP_PREFIX}{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, mode) as f:
            write(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_json_atomic(path: Path, data: Mapping) -> None:
    text = json.dumps(data, allow_nan=False, sort_keys=True, indent=1)
    write_atomic(path, lambda f: f.write(text), mode="w")


def read_json(path: Path) -> dict:
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ExchangeError(f"{path.name}: expected a JSON object")
    return data


def read_manifest(run_dir: Path) -> dict:
    path = run_dir / MANIFEST
    if not path.exists():
        raise ExchangeError(f"{run_dir} is not a prepared run (no {MANIFEST})")
    m = read_json(path)
    if m.get("version") != VERSION:
        raise ExchangeError(f"run format version {m.get('version')!r}, expected {VERSION}")
    return m


# --- meta validation ------------------------------------------------------------------------


def _pos_int(meta: Mapping, key: str, *, required: bool) -> int | None:
    v = meta.get(key)
    if v is None:
        if required:
            raise ExchangeError(f"meta.{key} is required (integer milliseconds)")
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ExchangeError(f"meta.{key} must be a positive integer of milliseconds, got {v!r}")
    return v


def _str_list(meta: Mapping, key: str) -> list[str]:
    v = meta.get(key) or []
    if not isinstance(v, list | tuple) or not all(isinstance(x, str) and x for x in v):
        raise ExchangeError(f"meta.{key} must be a list of non-empty strings, got {v!r}")
    return list(dict.fromkeys(v))


def _is_level(v: Any) -> bool:
    """A confidence/credible level: a number strictly between 0 and 1."""
    return not isinstance(v, bool) and isinstance(v, int | float) and 0 < v < 1


def _uncertainty(meta: Mapping, representation: str) -> tuple[dict | None, bool]:
    u, exact = meta.get("uncertainty"), meta.get("exact", False)
    if not isinstance(exact, bool):
        raise ExchangeError(f"meta.exact must be true or false, got {exact!r}")
    if u is not None and exact:
        raise ExchangeError("contradictory: exact=True cannot be combined with an uncertainty")
    if u is None:
        return None, exact
    if representation == DISTRIBUTION:
        raise ExchangeError(
            "a distribution has no lo/hi columns: declare exact=True for exact counts, or leave "
            f"uncertainty out (the output is then tagged {NO_UNCERTAINTY})"
        )
    if not isinstance(u, Mapping) or set(u) - {"method", "level", "kind"}:
        raise ExchangeError(
            "meta.uncertainty must be {'method': str, 'level': float in (0, 1), "
            f"'kind': one of {', '.join(INTERVAL_KINDS)}}}; got {u!r}"
        )
    method, level, kind = u.get("method"), u.get("level"), u.get("kind", "confidence")
    if not isinstance(method, str) or not method.strip():
        raise ExchangeError("meta.uncertainty.method is required, e.g. 'bootstrap percentile'")
    if level is not None and not _is_level(level):
        raise ExchangeError(f"meta.uncertainty.level must be in (0, 1), e.g. 0.95; got {level!r}")
    if kind not in INTERVAL_KINDS:
        raise ExchangeError(f"meta.uncertainty.kind must be one of {INTERVAL_KINDS}, got {kind!r}")
    return {"method": method.strip(), "level": level, "kind": kind}, False


def normalize_output_meta(
    meta: Mapping,
    declared: Sequence[str],
    input_meta: Callable[[str], Mapping],
    *,
    internal: bool = False,
) -> dict:
    """Validate a `tn.put` meta and resolve `like`/defaults into explicit fields.

    Idempotent: the result validates to itself. `internal` admits the keys tn adds when it
    writes the file (the daemon re-validates those)."""
    if not isinstance(meta, Mapping):
        raise ExchangeError(f"meta must be a dict, got {type(meta).__name__}")
    allowed = USER_KEYS | INTERNAL_KEYS if internal else USER_KEYS
    unknown = sorted(set(meta) - allowed)
    if unknown:
        raise ExchangeError(f"unknown meta keys {unknown}; allowed: {', '.join(sorted(USER_KEYS))}")
    like = meta.get("like")
    base: Mapping = {}
    if like is not None:
        if like not in declared:
            raise undeclared_error(like, declared)
        base = input_meta(like)
    rep = meta.get("representation") or base.get("representation") or "bucket_agg"
    if rep not in PUTTABLE:
        hint = " (use tn.put_fit for a fit/estimate)" if rep == ESTIMATE else ""
        raise ExchangeError(
            f"representation {rep!r} cannot be put; supported: {', '.join(PUTTABLE)}{hint}"
        )
    if (
        base
        and base.get("representation") != rep
        and not (base.get("representation") in TIME_SERIES and rep in TIME_SERIES)
    ):
        raise ExchangeError(
            f"like={like!r} is a {base.get('representation')} dataset, output is {rep}"
        )
    merged = {
        k: meta.get(k, base.get(k))
        for k in ("step_ms", "resolution_ms", "start_ms", "end_ms", "unit")
    }
    step = _pos_int(merged, "step_ms", required=True)
    resolution = _pos_int(merged, "resolution_ms", required=False) or step
    start, end = merged["start_ms"], merged["end_ms"]
    for k, v in (("start_ms", start), ("end_ms", end)):
        if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
            raise ExchangeError(f"meta.{k} must be integer epoch milliseconds, got {v!r}")
    if (start is None) != (end is None) or (start is not None and end <= start):
        raise ExchangeError("meta.start_ms and end_ms go together, with end_ms > start_ms")
    unit = merged["unit"]
    if unit is not None and not isinstance(unit, str):
        raise ExchangeError(f"meta.unit must be a string such as 's' or 'By', got {unit!r}")
    uncertainty, exact = _uncertainty(meta, rep)
    parents = meta.get("parents")
    if parents is None:
        parents = list(declared)
    else:
        parents = _str_list(meta, "parents")
        for p in parents:
            if p not in declared:
                raise undeclared_error(p, declared)
    description = meta.get("description")
    if description is not None and not isinstance(description, str):
        raise ExchangeError("meta.description must be a string")
    scheme = meta.get("scheme", base.get("scheme") if rep == DISTRIBUTION else None)
    if scheme is not None and (rep != DISTRIBUTION or not isinstance(scheme, Mapping)):
        raise ExchangeError("meta.scheme is a bucket scheme dict, for distributions only")
    n_min = meta.get("n_min", base.get("n_min") if rep == DISTRIBUTION else None)
    if n_min is not None and (isinstance(n_min, bool) or not isinstance(n_min, int) or n_min < 1):
        raise ExchangeError(f"meta.n_min must be a positive integer, got {n_min!r}")
    out = {
        "name": check_name(meta["name"]) if meta.get("name") is not None else None,
        "representation": rep,
        "like": like,
        "step_ms": step,
        "resolution_ms": resolution,
        "start_ms": start,
        "end_ms": end,
        "unit": unit,
        "description": description,
        "labels": _str_list(meta, "labels"),
        "uncertainty": uncertainty,
        "exact": exact,
        "caveats": _str_list(meta, "caveats"),
        "parents": parents,
        "scheme": dict(scheme) if scheme is not None else None,
        "n_min": n_min,
        "semantics_flags": dict(meta.get("semantics_flags", base.get("semantics_flags")) or {}),
    }
    if internal:
        out.update({k: meta[k] for k in INTERNAL_KEYS - out.keys() if k in meta})
    return out


def undeclared_error(handle: Any, declared: Sequence[str]) -> ExchangeError:
    return ExchangeError(
        f"{handle!r} is not a declared input of this run (declared: "
        f"{', '.join(declared) or 'none'}); pass it in run_code(inputs=[...])"
    )


FIT_META_KEYS = ("parents", "caveats", "start_ms", "end_ms")


def normalize_fit_meta(
    meta: Mapping, declared: Sequence[str], input_meta: Callable[[str], Mapping]
) -> dict:
    """The output-meta fields a fit carries (parents, caveats, start_ms/end_ms), validated by
    the `tn.put` rules; other keys of `meta` are ignored. Used by `tn.put_fit` and by ingest."""
    given = {k: meta[k] for k in FIT_META_KEYS if meta.get(k) is not None}
    # step_ms is required by the put rules but means nothing for a fit
    norm = normalize_output_meta({**given, "step_ms": 1}, declared, input_meta)
    return {k: norm[k] for k in FIT_META_KEYS}


def _finite(v: Any, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
        raise ExchangeError(f"{what} must be a finite number, got {v!r}")
    return float(v)


def _json_safe(v: Any, what: str) -> Any:
    try:
        json.dumps(v, allow_nan=False)
    except (TypeError, ValueError) as e:
        raise ExchangeError(f"{what} must be JSON-serializable with finite numbers: {e}") from e
    return v


def normalize_fit(fit: Mapping) -> dict:
    """A fit (representation `estimate`): params shaped like evidence statistics
    ({value, interval: [lo, hi]} or {value, exact: true}); assumption diagnostics mandatory."""
    model, method = fit.get("model"), fit.get("method")
    if not isinstance(model, str) or not model.strip():
        raise ExchangeError("put_fit: model is required, e.g. 'linear' or 'usl'")
    if not isinstance(method, str) or not method.strip():
        raise ExchangeError("put_fit: method is required, e.g. 'OLS, HC3 standard errors'")
    level = fit.get("level")
    if level is not None and not _is_level(level):
        raise ExchangeError(f"put_fit: level must be in (0, 1), got {level!r}")
    params = fit.get("params")
    if not isinstance(params, Mapping) or not params:
        raise ExchangeError("put_fit: params must be a non-empty {name: {value, interval}} dict")
    out_params: dict[str, dict] = {}
    missing: list[str] = []
    for name, p in params.items():
        if not isinstance(name, str) or not name:
            raise ExchangeError(f"put_fit: parameter names must be strings, got {name!r}")
        if isinstance(p, int | float) and not isinstance(p, bool):
            p = {"value": p}
        if not isinstance(p, Mapping) or set(p) - {"value", "interval", "exact"}:
            raise ExchangeError(
                f"put_fit: param {name!r} must be {{value, interval: [lo, hi]}} or "
                f"{{value, exact: true}}, got {p!r}"
            )
        value = _finite(p.get("value"), f"param {name!r} value")
        interval, exact = p.get("interval"), p.get("exact", False)
        if not isinstance(exact, bool):
            raise ExchangeError(f"param {name!r}: exact must be true or false")
        if exact and interval is not None:
            raise ExchangeError(f"param {name!r}: exact=true cannot be combined with an interval")
        if exact and not value.is_integer():
            raise ExchangeError(f"param {name!r}: exact=true is only for integral quantities")
        if interval is not None:
            if not isinstance(interval, list | tuple) or len(interval) != 2:
                raise ExchangeError(f"param {name!r}: interval must be [lo, hi]")
            lo = _finite(interval[0], f"param {name!r} interval lo")
            hi = _finite(interval[1], f"param {name!r} interval hi")
            if lo > hi:
                raise ExchangeError(f"param {name!r}: interval must be [lo, hi] with lo <= hi")
            interval = [lo, hi]
        elif not exact:
            missing.append(name)
        out_params[name] = {"value": value, "interval": interval, "exact": exact}
    diagnostics = fit.get("diagnostics")
    if not isinstance(diagnostics, Mapping) or not diagnostics:
        raise ExchangeError(
            "put_fit: diagnostics are mandatory (spec §3.5), e.g. "
            "{'residual_autocorr_lag1': 0.08, 'shapiro_p': 0.4}"
        )
    goodness = fit.get("goodness") or {}
    if not isinstance(goodness, Mapping):
        raise ExchangeError("put_fit: goodness must be a dict, e.g. {'r2': 0.93}")
    return {
        "model": model.strip(),
        "method": method.strip(),
        "level": level,
        "params": out_params,
        "goodness": _json_safe(dict(goodness), "goodness"),
        "diagnostics": _json_safe(dict(diagnostics), "diagnostics"),
        "params_without_uncertainty": missing,
    }


# --- column contract -------------------------------------------------------------------------


def _is_int(t: str) -> bool:
    return t.startswith(("int", "uint"))


def _is_num(t: str) -> bool:
    return _is_int(t) or t in ("double", "float", "halffloat")


def _is_str(t: str) -> bool:
    return t in ("string", "large_string", "string_view")


TS_VALUE_COLUMNS = ("avg", "min", "max", "count", "lo", "hi")


def check_columns(table: str, columns: Mapping[str, str], meta: Mapping) -> None:
    """Column names -> Arrow type strings, against the contract for `table` of an output.

    Unknown columns are refused (never silently dropped)."""
    rep = meta["representation"]
    labels = list(meta.get("labels") or [])
    interval = meta.get("uncertainty") is not None
    if rep in TIME_SERIES and table == "rows":
        required = {"ts_ms": _is_int, "avg": _is_num}
        optional = {"min": _is_num, "max": _is_num, "count": _is_int}
        if interval:
            required |= {"lo": _is_num, "hi": _is_num}
    elif rep == DISTRIBUTION and table == "rows":
        required = {"ts_ms": _is_int, "bucket_lo": _is_num, "bucket_hi": _is_num, "count": _is_num}
        optional = {}
    elif rep == DISTRIBUTION and table == "columns":
        required, optional = {"ts_ms": _is_int, "n": _is_num}, {}
    else:
        raise ExchangeError(f"no table {table!r} for a {rep} output")
    if "series_id" in columns and labels:
        raise ExchangeError("give either a series_id column or meta.labels columns, not both")
    for c in labels:
        if c in required or c in optional or c == "series_id":
            raise ExchangeError(f"label column {c!r} clashes with a value column")
        required[c] = _is_str
    optional["series_id"] = _is_str
    for c, ok in required.items():
        if c not in columns:
            extra = ""
            if c in ("lo", "hi"):
                extra = " (meta.uncertainty declares an interval: add lo and hi columns)"
            raise ExchangeError(f"{rep} {table}: missing column {c!r}{extra}")
        if not ok(columns[c]):
            raise ExchangeError(f"{rep} {table}: column {c!r} has type {columns[c]}")
    for c, t in columns.items():
        if c in required:
            continue
        if c not in optional:
            hint = ""
            if c in ("lo", "hi"):
                hint = "; lo/hi need meta.uncertainty = {'method': ..., 'level': ...}"
            elif rep in TIME_SERIES:
                hint = "; string label columns must be listed in meta.labels"
            raise ExchangeError(
                f"{rep} {table}: unexpected column {c!r} (refused rather than dropped){hint}"
            )
        if not optional[c](t):
            raise ExchangeError(f"{rep} {table}: column {c!r} has type {t}")
