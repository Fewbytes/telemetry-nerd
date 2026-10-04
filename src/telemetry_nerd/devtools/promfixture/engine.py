"""A small PromQL evaluator over the fixture store (bead y7hb).

Not a full engine: the subset the daemon and the e2e specs issue, with Prometheus semantics
(5m lookback, left-open range windows `(t - r, t]`, extrapolated `rate`/`increase`, subqueries
aligned to multiples of their step). Parsing is `promql_parser` (the Rust PromQL parser); the
AST is converted once into the small node classes below and evaluated per timestamp.

Anything outside the subset raises `Unsupported`, which the server answers as an execution
error naming the construct, so a spec that needs more fails loudly instead of reading wrong data.
"""

from __future__ import annotations

import itertools
import math
import re
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta

import promql_parser as pp

from telemetry_nerd.devtools.promfixture.store import Labels, Matcher, Series, Store, label_key

LOOKBACK_MS = 300_000
DEFAULT_SUBQUERY_STEP_MS = 60_000

Sample = tuple[dict[str, str], float]
Vector = list[Sample]
RangeSeries = tuple[dict[str, str], list[int], list[float]]
Matrix = list[RangeSeries]


class Unsupported(Exception):
    """The expression uses PromQL the fixture engine does not implement."""


class EvalError(Exception):
    """The expression is valid but cannot be evaluated (Prometheus would refuse it too)."""


# --- AST ------------------------------------------------------------------------------------


@dataclass
class Num:
    value: float


@dataclass
class Str:
    value: str


@dataclass
class At:
    kind: str  # "at" | "start" | "end"
    ms: int = 0


@dataclass(eq=False)
class VSel:
    matchers: list[Matcher]
    offset_ms: int = 0
    at: At | None = None
    _cache: list[Series] | None = field(default=None, repr=False)

    def series(self, store: Store) -> list[Series]:
        if self._cache is None:
            self._cache = store.select(self.matchers)
        return self._cache


@dataclass(eq=False)
class MSel:
    sel: VSel
    range_ms: int


@dataclass(eq=False)
class Sub:
    expr: Node
    range_ms: int
    step_ms: int
    offset_ms: int = 0
    at: At | None = None


@dataclass(eq=False)
class Call:
    fn: str
    args: list[Node]


@dataclass(eq=False)
class Agg:
    op: str
    expr: Node
    param: Node | None
    labels: list[str] | None  # None: no grouping clause
    without: bool


@dataclass(eq=False)
class Bin:
    op: str
    lhs: Node
    rhs: Node
    bool_: bool = False
    card: str = "one-to-one"  # one-to-one | many-to-one | one-to-many
    on: bool | None = None  # True: on(...), False: ignoring(...), None: neither
    match_labels: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)


@dataclass(eq=False)
class Neg:
    expr: Node


Node = Num | Str | VSel | MSel | Sub | Call | Agg | Bin | Neg


def _ms(d: timedelta | None) -> int:
    return 0 if d is None else round(d.total_seconds() * 1000)


def _at(a: object) -> At | None:
    if a is None:
        return None
    kind = str(a.type).rsplit(".", 1)[-1].lower()  # type: ignore[attr-defined]
    if kind == "start":
        return At("start")
    if kind == "end":
        return At("end")
    return At("at", round(a.at.timestamp() * 1000))  # type: ignore[attr-defined]


_OPS = {"Equal": "=", "NotEqual": "!=", "Re": "=~", "NotRe": "!~"}


def _vsel(v: pp.VectorSelector) -> VSel:
    matchers = [
        Matcher(m.name, _OPS[str(m.op).rsplit(".", 1)[-1]], m.value) for m in v.matchers.matchers
    ]
    if v.matchers.or_matchers:
        raise Unsupported("`or` inside a selector")
    if v.name is not None:
        matchers.insert(0, Matcher("__name__", "=", v.name))
    return VSel(matchers, _ms(v.offset), _at(v.at))


def convert(e: object) -> Node:
    """promql_parser AST -> evaluator nodes."""
    kind = type(e).__name__
    if kind == "NumberLiteral":
        return Num(float(e.val))  # type: ignore[attr-defined]
    if kind == "StringLiteral":
        return Str(e.val)  # type: ignore[attr-defined]
    if kind == "ParenExpr":
        return convert(e.expr)  # type: ignore[attr-defined]
    if kind == "UnaryExpr":
        return Neg(convert(e.expr))  # type: ignore[attr-defined]
    if kind == "VectorSelector":
        return _vsel(e)  # type: ignore[arg-type]
    if kind == "MatrixSelector":
        return MSel(_vsel(e.vector_selector), _ms(e.range))  # type: ignore[attr-defined]
    if kind == "SubqueryExpr":
        step = _ms(e.step) or DEFAULT_SUBQUERY_STEP_MS  # type: ignore[attr-defined]
        return Sub(convert(e.expr), _ms(e.range), step, _ms(e.offset), _at(e.at))  # type: ignore[attr-defined]
    if kind == "Call":
        return Call(e.func.name, [convert(a) for a in e.args])  # type: ignore[attr-defined]
    if kind == "AggregateExpr":
        mod = e.modifier  # type: ignore[attr-defined]
        labels = None if mod is None else list(mod.labels)
        without = mod is not None and str(mod.type).endswith("Without")
        param = None if e.param is None else convert(e.param)  # type: ignore[attr-defined]
        return Agg(str(e.op), convert(e.expr), param, labels, without)  # type: ignore[attr-defined]
    if kind == "BinaryExpr":
        node = Bin(str(e.op).strip(), convert(e.lhs), convert(e.rhs))  # type: ignore[attr-defined]
        mod = e.modifier  # type: ignore[attr-defined]
        if mod is not None:
            node.bool_ = bool(mod.return_bool)
            card = str(mod.card).rsplit(".", 1)[-1]
            node.card = {"ManyToOne": "many-to-one", "OneToMany": "one-to-many"}.get(
                card, "one-to-one"
            )
            if mod.matching is not None:
                node.on = str(mod.matching.type).endswith("Include")
                node.match_labels = list(mod.matching.labels)
            if node.card != "one-to-one":
                node.include = list(mod.group_labels or [])
        return node
    raise Unsupported(f"expression kind {kind}")


def parse(expr: str) -> Node:
    try:
        ast = pp.parse(expr)
    except ValueError as e:
        raise EvalError(f"parse error: {e}") from e
    return convert(ast)


# --- helpers ---------------------------------------------------------------------------------


def _drop_name(labels: Labels) -> dict[str, str]:
    return {k: v for k, v in labels.items() if k != "__name__"}


def _scalar(v: object, what: str) -> float:
    if isinstance(v, float):
        return v
    raise EvalError(f"expected a scalar for {what}")


def _vector(v: object, what: str) -> Vector:
    if isinstance(v, list) and (not v or isinstance(v[0][1], float)):
        return v  # type: ignore[return-value]
    raise EvalError(f"expected an instant vector for {what}")


def _matrix(v: object, what: str) -> Matrix:
    if isinstance(v, _M):
        return v.series
    raise EvalError(f"expected a range vector for {what}")


@dataclass
class _M:
    """Range-vector result (wrapped so it is not confused with an instant vector)."""

    series: Matrix
    #: range start (exclusive) and end (inclusive) of every window, ms
    lo: int
    hi: int


def _extrapolated(ts: list[int], vs: list[float], lo: int, hi: int, counter: bool, rate: bool):
    """Prometheus extrapolatedRate (rate / increase / delta)."""
    if len(ts) < 2:
        return None
    result = vs[-1] - vs[0]
    if counter:
        prev = vs[0]
        for v in vs[1:]:
            if v < prev:
                result += prev
            prev = v
    dur_start = (ts[0] - lo) / 1000
    dur_end = (hi - ts[-1]) / 1000
    sampled = (ts[-1] - ts[0]) / 1000
    avg = sampled / (len(ts) - 1)
    # Prometheus 3 order: a gap >= 1.1 average spacings means the series starts/ends inside the
    # range (extrapolate half a spacing); then a counter is never extrapolated below zero
    threshold = avg * 1.1
    if dur_start >= threshold:
        dur_start = avg / 2
    if counter and result > 0 and vs[0] >= 0:
        dur_start = min(dur_start, sampled * (vs[0] / result))
    if dur_end >= threshold:
        dur_end = avg / 2
    interval = sampled + dur_start + dur_end
    factor = interval / sampled
    if rate:
        factor /= (hi - lo) / 1000
    return result * factor


def _quantile(q: float, values: Sequence[float]) -> float:
    if not values:
        return math.nan
    if q < 0:
        return -math.inf
    if q > 1:
        return math.inf
    s = sorted(values)
    rank = q * (len(s) - 1)
    lo = math.floor(rank)
    hi = min(lo + 1, len(s) - 1)
    w = rank - lo
    return s[lo] * (1 - w) + s[hi] * w


def _linreg(ts: list[int], vs: list[float], at_ms: int) -> tuple[float, float]:
    xs = [(t - at_ms) / 1000 for t in ts]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(vs) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, vs, strict=True))
    var = sum((x - mx) ** 2 for x in xs)
    slope = cov / var if var else 0.0
    return slope, my - slope * mx


def _stdvar(vs: Sequence[float]) -> float:
    return statistics.pvariance(vs) if vs else math.nan


def _over_time(
    name: str,
) -> Callable[[list[int], list[float], int, int, list[float]], float | None]:
    simple: dict[str, Callable[[list[float]], float]] = {
        "avg_over_time": lambda v: sum(v) / len(v),
        "min_over_time": min,
        "max_over_time": max,
        "sum_over_time": sum,
        "count_over_time": lambda v: float(len(v)),
        "last_over_time": lambda v: v[-1],
        "first_over_time": lambda v: v[0],
        "present_over_time": lambda v: 1.0,
        "stddev_over_time": lambda v: math.sqrt(_stdvar(v)),
        "stdvar_over_time": _stdvar,
    }
    if name in simple:
        f = simple[name]
        return lambda ts, vs, lo, hi, extra: f(vs) if vs else None
    if name == "quantile_over_time":
        return lambda ts, vs, lo, hi, extra: _quantile(extra[0], vs) if vs else None
    if name in ("rate", "increase", "delta"):
        counter = name != "delta"
        rate = name == "rate"
        return lambda ts, vs, lo, hi, extra: _extrapolated(ts, vs, lo, hi, counter, rate)
    if name in ("irate", "idelta"):

        def inst(ts, vs, lo, hi, extra):
            if len(vs) < 2:
                return None
            d = vs[-1] - vs[-2]
            if name == "idelta":
                return d
            if d < 0:
                d = vs[-1]
            dt = (ts[-1] - ts[-2]) / 1000
            return d / dt if dt else None

        return inst
    if name == "changes":
        return lambda ts, vs, lo, hi, extra: (
            float(sum(1 for a, b in itertools.pairwise(vs) if a != b)) if vs else None
        )
    if name == "resets":
        return lambda ts, vs, lo, hi, extra: (
            float(sum(1 for a, b in itertools.pairwise(vs) if b < a)) if vs else None
        )
    if name == "deriv":
        return lambda ts, vs, lo, hi, extra: _linreg(ts, vs, ts[0])[0] if len(vs) >= 2 else None
    if name == "predict_linear":

        def predict(ts, vs, lo, hi, extra):
            if len(vs) < 2:
                return None
            slope, icpt = _linreg(ts, vs, hi)
            return icpt + slope * extra[0]

        return predict
    raise Unsupported(f"function {name}")


_RANGE_FNS = frozenset(
    {
        "avg_over_time",
        "min_over_time",
        "max_over_time",
        "sum_over_time",
        "count_over_time",
        "last_over_time",
        "first_over_time",
        "present_over_time",
        "stddev_over_time",
        "stdvar_over_time",
        "quantile_over_time",
        "rate",
        "increase",
        "delta",
        "irate",
        "idelta",
        "changes",
        "resets",
        "deriv",
        "predict_linear",
    }
)
_KEEP_NAME = frozenset({"last_over_time", "first_over_time"})

_ELEMENTWISE: dict[str, Callable[[float], float]] = {
    "abs": abs,
    "ceil": lambda v: float(math.ceil(v)) if math.isfinite(v) else v,
    "floor": lambda v: float(math.floor(v)) if math.isfinite(v) else v,
    "exp": lambda v: math.exp(v) if v < 709 else math.inf,
    "ln": lambda v: math.log(v) if v > 0 else (-math.inf if v == 0 else math.nan),
    "log2": lambda v: math.log2(v) if v > 0 else (-math.inf if v == 0 else math.nan),
    "log10": lambda v: math.log10(v) if v > 0 else (-math.inf if v == 0 else math.nan),
    "sqrt": lambda v: math.sqrt(v) if v >= 0 else math.nan,
    "sgn": lambda v: float((v > 0) - (v < 0)) if not math.isnan(v) else v,
    "deg": math.degrees,
    "rad": math.radians,
}


def _arith(op: str, a: float, b: float) -> float:
    try:
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            if b == 0:  # IEEE 754, as Go: x/0 = ±Inf, 0/0 = NaN
                if a == 0 or math.isnan(a):
                    return math.nan
                return math.copysign(math.inf, a) * math.copysign(1, b)
            return a / b
        if op == "%":
            return math.fmod(a, b) if b != 0 else math.nan
        if op == "^":
            return a**b
        if op == "atan2":
            return math.atan2(a, b)
    except (OverflowError, ValueError, ZeroDivisionError):
        return math.nan
    raise Unsupported(f"operator {op}")


_CMP: dict[str, Callable[[float, float], bool]] = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
}
_SET_OPS = frozenset({"and", "or", "unless"})


def _bucket_quantile(q: float, buckets: list[tuple[float, float]]) -> float:
    """Prometheus bucketQuantile over (upper bound, cumulative count)."""
    if math.isnan(q):
        return math.nan
    if q < 0:
        return -math.inf
    if q > 1:
        return math.inf
    buckets = sorted(buckets)
    if not buckets or not math.isinf(buckets[-1][0]):
        return math.nan
    fixed: list[tuple[float, float]] = []
    top = -math.inf
    for ub, c in buckets:  # ensure monotonic counts
        top = max(top, c)
        fixed.append((ub, top))
    buckets = fixed
    if len(buckets) < 2:
        return math.nan
    total = buckets[-1][1]
    if total == 0:
        return math.nan
    rank = q * total
    b = next(i for i, (_, c) in enumerate(buckets) if c >= rank)
    if b == len(buckets) - 1:
        return buckets[-2][0]
    if b == 0 and buckets[0][0] <= 0:
        return buckets[0][0]
    start, end, count = 0.0, buckets[b][0], buckets[b][1]
    if b > 0:
        start = buckets[b - 1][0]
        count -= buckets[b - 1][1]
        rank -= buckets[b - 1][1]
    return start + (end - start) * (rank / count) if count else start


# --- evaluation ------------------------------------------------------------------------------


Value = float | str | Vector | _M


class Engine:
    def __init__(
        self,
        store: Store,
        lookback_ms: int = LOOKBACK_MS,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.store = store
        self.lookback_ms = lookback_ms
        #: samples after clock() are not visible yet (a live TSDB has no future data); None: all
        self.clock = clock

    def visible(self, t: int) -> int:
        return t if self.clock is None else min(t, self.clock())

    # public API -------------------------------------------------------------------------------

    def instant(self, expr: str, t_ms: int) -> tuple[str, object]:
        """(resultType, result) as the Prometheus API shapes them (values still floats)."""
        node = parse(expr)
        ctx = _Ctx(self, t_ms, t_ms)
        v = ctx.eval(node, t_ms)
        if isinstance(v, float):
            return "scalar", v
        if isinstance(v, str):
            return "string", v
        if isinstance(v, _M):
            return "matrix", [(lb, ts, vs) for lb, ts, vs in v.series if ts]
        return "vector", _check_unique(v)

    def range(self, expr: str, start_ms: int, end_ms: int, step_ms: int) -> Matrix:
        node = parse(expr)
        ctx = _Ctx(self, start_ms, end_ms)
        out: dict[tuple, RangeSeries] = {}
        t = start_ms
        while t <= end_ms:
            v = ctx.eval(node, t)
            if isinstance(v, float):
                v = [({}, v)]
            elif not isinstance(v, list):
                raise EvalError(
                    "invalid expression type for range query, must be Scalar or instant Vector"
                )
            for labels, value in _check_unique(v):
                key = label_key(labels)
                row = out.get(key)
                if row is None:
                    row = out[key] = (labels, [], [])
                row[1].append(t)
                row[2].append(value)
            t += step_ms
        return list(out.values())


def _check_unique(v: Vector) -> Vector:
    seen: set[tuple] = set()
    for labels, _ in v:
        key = label_key(labels)
        if key in seen:
            raise EvalError("vector cannot contain metrics with the same labelset")
        seen.add(key)
    return v


class _Ctx:
    def __init__(self, engine: Engine, start_ms: int, end_ms: int) -> None:
        self.e = engine
        self.start_ms = start_ms
        self.end_ms = end_ms
        self._memo: dict[tuple[int, int], Value] = {}

    def _time(self, t: int, at: At | None, offset_ms: int) -> int:
        if at is not None:
            t = {"start": self.start_ms, "end": self.end_ms}.get(at.kind, at.ms)
        return t - offset_ms

    def eval(self, n: Node, t: int) -> Value:
        if isinstance(n, Num):
            return n.value
        if isinstance(n, Str):
            return n.value
        if isinstance(n, VSel):
            te = self._time(t, n.at, n.offset_ms)
            out: Vector = []
            for s in n.series(self.e.store):
                _, vs = s.window(te - self.e.lookback_ms, self.e.visible(te))
                if vs:
                    out.append((s.labels, vs[-1]))
            return out
        if isinstance(n, MSel):
            te = self._time(t, n.sel.at, n.sel.offset_ms)
            lo = te - n.range_ms
            hi = self.e.visible(te)
            series = [(s.labels, *s.window(lo, hi)) for s in n.sel.series(self.e.store)]
            return _M([s for s in series if s[1]], lo, te)
        if isinstance(n, Sub):
            return self._subquery(n, t)
        if isinstance(n, Neg):
            v = self.eval(n.expr, t)
            if isinstance(v, float):
                return -v
            return [(_drop_name(lb), -x) for lb, x in _vector(v, "unary minus")]
        if isinstance(n, Call):
            return self._call(n, t)
        if isinstance(n, Agg):
            return self._agg(n, t)
        if isinstance(n, Bin):
            return self._bin(n, t)
        raise Unsupported(type(n).__name__)

    def _subquery(self, n: Sub, t: int) -> _M:
        te = self._time(t, n.at, n.offset_ms)
        lo = te - n.range_ms
        first = (lo // n.step_ms + 1) * n.step_ms
        rows: dict[tuple, RangeSeries] = {}
        for p in range(first, te + 1, n.step_ms):
            key = (id(n), p)
            v = self._memo.get(key)
            if v is None:
                v = self._memo[key] = self.eval(n.expr, p)
            if isinstance(v, float):
                v = [({}, v)]
            for labels, value in _vector(v, "subquery"):
                k = label_key(labels)
                row = rows.get(k)
                if row is None:
                    row = rows[k] = (labels, [], [])
                row[1].append(p)
                row[2].append(value)
        return _M(list(rows.values()), lo, te)

    # functions --------------------------------------------------------------------------------

    def _call(self, n: Call, t: int) -> Value:
        fn = n.fn
        if fn in _RANGE_FNS:
            extra: list[float] = []
            if fn == "quantile_over_time":
                extra = [_scalar(self.eval(n.args[0], t), fn)]
                mv = self.eval(n.args[1], t)
            elif fn == "predict_linear":
                mv = self.eval(n.args[0], t)
                extra = [_scalar(self.eval(n.args[1], t), fn)]
            else:
                mv = self.eval(n.args[0], t)
            if not isinstance(mv, _M):
                raise EvalError(f"{fn} expects a range vector")
            f = _over_time(fn)
            out: Vector = []
            for labels, ts, vs in mv.series:
                r = f(ts, vs, mv.lo, mv.hi, extra)
                if r is not None:
                    out.append((dict(labels) if fn in _KEEP_NAME else _drop_name(labels), r))
            return out
        if fn in _ELEMENTWISE:
            g = _ELEMENTWISE[fn]
            return [(_drop_name(lb), g(v)) for lb, v in _vector(self.eval(n.args[0], t), fn)]
        if fn in ("clamp", "clamp_min", "clamp_max"):
            vec = _vector(self.eval(n.args[0], t), fn)
            bounds = [_scalar(self.eval(a, t), fn) for a in n.args[1:]]
            lo = bounds[0] if fn in ("clamp", "clamp_min") else -math.inf
            hi = bounds[-1] if fn in ("clamp", "clamp_max") else math.inf
            if lo > hi:
                return []
            return [(_drop_name(lb), max(lo, min(hi, v))) for lb, v in vec]
        if fn == "round":
            vec = _vector(self.eval(n.args[0], t), fn)
            to = _scalar(self.eval(n.args[1], t), fn) if len(n.args) > 1 else 1.0
            return [(_drop_name(lb), math.floor(v / to + 0.5) * to) for lb, v in vec]
        if fn == "histogram_quantile":
            q = _scalar(self.eval(n.args[0], t), fn)
            groups: dict[tuple, tuple[dict[str, str], list[tuple[float, float]]]] = {}
            for lb, v in _vector(self.eval(n.args[1], t), fn):
                if "le" not in lb:
                    continue
                rest = {k: x for k, x in lb.items() if k not in ("le", "__name__")}
                g = groups.setdefault(label_key(rest), (rest, []))
                g[1].append((float(lb["le"]), v))
            return [(lb, _bucket_quantile(q, bs)) for lb, bs in groups.values()]
        if fn == "time":
            return t / 1000
        if fn == "vector":
            return [({}, _scalar(self.eval(n.args[0], t), fn))]
        if fn == "scalar":
            vec = _vector(self.eval(n.args[0], t), fn)
            return vec[0][1] if len(vec) == 1 else math.nan
        if fn in ("sort", "sort_desc"):
            vec = _vector(self.eval(n.args[0], t), fn)
            return sorted(vec, key=lambda s: s[1], reverse=fn == "sort_desc")
        if fn == "timestamp":
            arg = n.args[0]
            if not isinstance(arg, VSel):
                raise Unsupported("timestamp() of a non-selector")
            te = self._time(t, arg.at, arg.offset_ms)
            out = []
            for s in arg.series(self.e.store):
                ts, _ = s.window(te - self.e.lookback_ms, self.e.visible(te))
                if ts:
                    out.append((_drop_name(s.labels), ts[-1] / 1000))
            return out
        if fn == "label_replace":
            vec = _vector(self.eval(n.args[0], t), fn)
            dst, repl, src, regex = (str(self.eval(a, t)) for a in n.args[1:5])
            pat = re.compile(regex)
            out = []
            for lb, v in vec:
                m = pat.fullmatch(lb.get(src, ""))
                if m is None:
                    out.append((lb, v))
                    continue
                new = dict(lb)
                value = m.expand(re.sub(r"\$(\d+)|\$\{(\w+)\}", r"\\g<\1\2>", repl))
                if value:
                    new[dst] = value
                else:
                    new.pop(dst, None)
                out.append((new, v))
            return out
        if fn == "absent":
            vec = _vector(self.eval(n.args[0], t), fn)
            if vec:
                return []
            arg = n.args[0]
            labels = {}
            if isinstance(arg, VSel):
                labels = {
                    m.name: m.value for m in arg.matchers if m.op == "=" and m.name != "__name__"
                }
            return [(labels, 1.0)]
        raise Unsupported(f"function {fn}")

    # aggregation ------------------------------------------------------------------------------

    def _agg(self, n: Agg, t: int) -> Vector:
        vec = _vector(self.eval(n.expr, t), n.op)
        param = None if n.param is None else self.eval(n.param, t)

        def group_labels(lb: Labels) -> dict[str, str]:
            if n.labels is None:
                return {}
            if n.without:
                drop = set(n.labels) | {"__name__"}
                return {k: v for k, v in lb.items() if k not in drop}
            return {k: lb[k] for k in n.labels if k in lb}

        groups: dict[tuple, tuple[dict[str, str], list[Sample]]] = {}
        for lb, v in vec:
            gl = group_labels(lb)
            groups.setdefault(label_key(gl), (gl, []))[1].append((lb, v))
        op = n.op
        out: Vector = []
        for gl, members in groups.values():
            vals = [v for _, v in members]
            if op in ("topk", "bottomk"):
                k = int(_scalar(param, op))
                ranked = sorted(
                    members,
                    key=lambda s: (math.isnan(s[1]), -s[1] if op == "topk" else s[1]),
                )
                out.extend(ranked[: max(k, 0)])
                continue
            if op == "sum":
                r = sum(vals)
            elif op == "avg":
                r = sum(vals) / len(vals)
            elif op == "min":
                r = min(vals)
            elif op == "max":
                r = max(vals)
            elif op == "count":
                r = float(len(vals))
            elif op == "group":
                r = 1.0
            elif op == "stddev":
                r = math.sqrt(_stdvar(vals))
            elif op == "stdvar":
                r = _stdvar(vals)
            elif op == "quantile":
                r = _quantile(_scalar(param, op), vals)
            else:
                raise Unsupported(f"aggregation {op}")
            out.append((gl, float(r)))
        return out

    # binary operators -------------------------------------------------------------------------

    def _bin(self, n: Bin, t: int) -> Value:
        lhs = self.eval(n.lhs, t)
        rhs = self.eval(n.rhs, t)
        op = n.op
        if isinstance(lhs, float) and isinstance(rhs, float):
            if op in _CMP:
                return float(_CMP[op](lhs, rhs))
            return _arith(op, lhs, rhs)
        if isinstance(lhs, float) or isinstance(rhs, float):
            return self._vec_scalar(n, lhs, rhs)
        lv = _vector(lhs, op)
        rv = _vector(rhs, op)
        if op in _SET_OPS:
            return self._set_op(n, lv, rv)
        return self._vec_vec(n, lv, rv)

    def _vec_scalar(self, n: Bin, lhs: Value, rhs: Value) -> Vector:
        scalar_left = isinstance(lhs, float)
        vec = _vector(rhs if scalar_left else lhs, n.op)
        s = lhs if scalar_left else rhs
        assert isinstance(s, float)
        out: Vector = []
        for lb, v in vec:
            a, b = (s, v) if scalar_left else (v, s)
            if n.op in _CMP:
                hit = _CMP[n.op](a, b)
                if n.bool_:
                    out.append((_drop_name(lb), float(hit)))
                elif hit:
                    out.append((lb, v))
            else:
                out.append((_drop_name(lb), _arith(n.op, a, b)))
        return out

    def _sig(self, n: Bin, lb: Labels) -> tuple:
        if n.on is True:
            return tuple((k, lb.get(k, "")) for k in sorted(n.match_labels))
        drop = set(n.match_labels) | {"__name__"}
        return tuple(sorted((k, v) for k, v in lb.items() if k not in drop))

    def _set_op(self, n: Bin, lv: Vector, rv: Vector) -> Vector:
        rsigs = {self._sig(n, lb) for lb, _ in rv}
        if n.op == "and":
            return [s for s in lv if self._sig(n, s[0]) in rsigs]
        if n.op == "unless":
            return [s for s in lv if self._sig(n, s[0]) not in rsigs]
        lsigs = {self._sig(n, lb) for lb, _ in lv}
        return lv + [s for s in rv if self._sig(n, s[0]) not in lsigs]

    def _vec_vec(self, n: Bin, lv: Vector, rv: Vector) -> Vector:
        many, one = (lv, rv) if n.card != "one-to-many" else (rv, lv)
        one_by_sig: dict[tuple, Sample] = {}
        for s in one:
            sig = self._sig(n, s[0])
            if sig in one_by_sig:
                side = "right" if n.card != "one-to-many" else "left"
                raise EvalError(
                    f"found duplicate series for the match group {dict(sig)} on the {side} "
                    "hand-side of the operation: many-to-many matching not allowed"
                )
            one_by_sig[sig] = s
        seen: set[tuple] = set()
        out: Vector = []
        for mlb, mv in many:
            sig = self._sig(n, mlb)
            match = one_by_sig.get(sig)
            if match is None:
                continue
            if n.card == "one-to-one":
                if sig in seen:
                    raise EvalError(
                        "multiple matches for labels: many-to-one matching must be explicit "
                        "(group_left/group_right)"
                    )
                seen.add(sig)
            olb, ov = match
            a, b = (mv, ov) if n.card != "one-to-many" else (ov, mv)
            if n.op in _CMP:
                hit = _CMP[n.op](a, b)
                if not n.bool_ and not hit:
                    continue
                value = float(hit) if n.bool_ else a
            else:
                value = _arith(n.op, a, b)
            labels = dict(mlb)
            if n.op not in _CMP or n.bool_:
                labels.pop("__name__", None)
            if n.card == "one-to-one":
                if n.on is True:
                    labels = {k: labels[k] for k in n.match_labels if k in labels}
                else:
                    for k in n.match_labels:
                        labels.pop(k, None)
            else:
                for k in n.include:
                    if olb.get(k):
                        labels[k] = olb[k]
                    else:
                        labels.pop(k, None)
            out.append((labels, value))
        return out
