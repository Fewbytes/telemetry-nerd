"""Which evidence series a finding's `scope.selector` names (spec 2026-10-02 §4.4).

The selector's label matchers (=, !=, =~, !~; a bare metric name or `{"name"}` is a `__name__`
matcher) are applied to the evidence series' labels with Prometheus semantics: a regex is fully
anchored and `.` matches a newline (Prometheus compiles `^(?s:re)$`), and a missing label reads as
the empty string. Read from a plain selector, or from the one selector inside an expression that
keeps its series' labels (rate(x{pod="a"}[5m]), sum by (pod) (...), x offset 5m); an expression
whose matchers cannot be tied to its output series (binary operators, label_replace, count_values,
several selectors) is not read at all: the claim's scope is *undetermined* (said as such, a warning;
coverage is then checked over every evidence series). A grouping written without parentheses
(`x{...} by status_code`, not PromQL) is read as the grouping it means, with a note.

The evidence's own expression pins labels its series no longer carry (`sum by (status_code)
(rate(x{service_name="payment"}[1m]))` is about payment only): `pinned_matchers` reads them so a
claim matcher on an aggregated-away label is checked against them instead of skipped."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Literal

_IDENT = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
_LABEL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_OP = re.compile(r"=~|!~|!=|=")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "`": "`"}
# modifiers that move or pin the evaluation time but keep the series: removed before reading
_MODIFIERS = re.compile(
    r"\boffset\s+-?[0-9a-zA-Z.]+|@\s*(?:start\s*\(\s*\)|end\s*\(\s*\)|[0-9.eE+-]+)",
    re.IGNORECASE,  # PromQL keywords are case-insensitive
)
_GROUPING = re.compile(r"\b(?:by|without)\s*\([^()]*\)", re.IGNORECASE)
# `by a, b` without parentheses (not PromQL; read as the grouping it means, with a note)
_LOOSE_GROUPING = re.compile(
    r"\b(by|without)\s+([A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*)(?!\s*\()",
    re.IGNORECASE,
)
# outside strings, selectors and ranges: anything that combines series or rewrites their labels
_COMBINES = re.compile(
    r"[-+*/%^<>=!]|\b(?:and|or|unless|on|ignoring|group_left|group_right|label_replace|label_join"
    r"|count_values|absent|absent_over_time|vector|scalar)\b",
    re.IGNORECASE,
)
_BARE = re.compile(rf"(?<![0-9.A-Za-z_:]){_IDENT.pattern}")  # not the exponent of 1e3
_KEYWORDS = {"by", "without", "bool", "inf", "nan", "offset"}
# RE2 syntax Python's re reads differently or not at all: refuse rather than guess
_RE2_ONLY = re.compile(r"\[:\^?[a-z]+:\]|\\[pP]")


class Unreadable(ValueError):
    """The selector cannot be read into matchers; the message says why (for the caveat)."""


@dataclass(frozen=True)
class Matcher:
    label: str
    op: str
    value: str
    _re: re.Pattern | None = field(default=None, compare=False, repr=False)

    def matches(self, v: str) -> bool:
        if self.op == "=":
            return v == self.value
        if self.op == "!=":
            return v != self.value
        hit = self._re is not None and self._re.fullmatch(v) is not None
        return hit if self.op == "=~" else not hit

    def __str__(self) -> str:
        value = self.value.replace("\\", "\\\\").replace('"', '\\"')
        return f'{self.label}{self.op}"{value}"'


def _matcher(label: str, op: str, value: str) -> Matcher:
    if op not in ("=~", "!~"):
        return Matcher(label, op, value)
    if _RE2_ONLY.search(value):
        raise Unreadable(f"regex {value!r} uses RE2 syntax (POSIX class or \\p) this check does "
                         "not evaluate")  # fmt: skip
    try:
        return Matcher(label, op, value, re.compile(value, re.DOTALL))
    except re.error as e:
        raise Unreadable(f"regex {value!r} is invalid ({e})") from e


def _string(s: str, i: int) -> tuple[str, int]:
    """PromQL string literal at s[i] ("...", '...' with escapes, `...` raw) -> (value, end)."""
    q = s[i]
    out: list[str] = []
    j = i + 1
    while j < len(s):
        c = s[j]
        if c == q:
            return "".join(out), j + 1
        if c == "\\" and q != "`" and j + 1 < len(s):
            out.append(_ESCAPES.get(s[j + 1], "\\" + s[j + 1]))
            j += 2
            continue
        out.append(c)
        j += 1
    raise Unreadable("unterminated string")


def _body(s: str) -> list[Matcher]:
    """Matchers inside one `{...}` (without the braces)."""
    out: list[Matcher] = []
    i, n = 0, len(s)
    while True:
        while i < n and s[i] in " \t\n,":
            i += 1
        if i >= n:
            return out
        if s[i] in "\"'`":
            label, i = _string(s, i)
        elif m := _LABEL.match(s, i):
            label, i = m.group(), m.end()
        else:
            raise Unreadable(f"cannot read a label at {s[i:]!r}")
        while i < n and s[i] in " \t\n":
            i += 1
        op = _OP.match(s, i)
        if op is None:  # a bare quoted name: {"metric.name"} (Prometheus 3)
            out.append(Matcher("__name__", "=", label))
            continue
        i = op.end()
        while i < n and s[i] in " \t\n":
            i += 1
        if i >= n or s[i] not in "\"'`":
            raise Unreadable("matcher value must be a string")
        value, i = _string(s, i)
        out.append(_matcher(label, op.group(), value))


def _selectors(expr: str) -> tuple[list[tuple[str | None, str]], str]:
    """(metric name before it or None, body) of every `{...}` in expr, and expr with strings,
    selectors and `[...]` ranges blanked out (what is left of the expression's structure)."""
    found: list[tuple[str | None, str]] = []
    rest: list[str] = []
    i, n = 0, len(expr)
    while i < n:
        c = expr[i]
        if c in "\"'`":
            _, i = _string(expr, i)
            rest.append(" ")
        elif c == "{":
            j, depth = i + 1, 1
            while depth:
                if j >= n:
                    raise Unreadable("unbalanced braces")
                if expr[j] in "\"'`":
                    _, j = _string(expr, j)
                    continue
                depth += {"{": 1, "}": -1}.get(expr[j], 0)
                j += 1
            head = "".join(rest).rstrip()
            m = re.search(rf"({_IDENT.pattern})$", head)
            name = m.group(1) if m else None
            if m:
                rest = [head[: m.start()]]
            found.append((name, expr[i + 1 : j - 1]))
            rest.append(" ")
            i = j
        elif c == "[":
            j = expr.find("]", i)
            if j < 0:
                raise Unreadable("unbalanced brackets")
            rest.append(" ")
            i = j + 1
        else:
            rest.append(c)
            i += 1
    return found, "".join(rest)


def _bare_names(rest: str) -> list[str]:
    """Metric names written without braces: identifiers that are not calls or keywords."""
    return [
        m.group()
        for m in _BARE.finditer(rest)
        if m.group().lower() not in _KEYWORDS and not rest[m.end() :].lstrip().startswith("(")
    ]


Level = Literal["info", "warn"]


@dataclass
class SelectorRead:
    matchers: list[Matcher] | None  # None: not read, the scope is undetermined
    why: str | None = None  # why it was not read
    notes: list[str] = field(default_factory=list)  # how it was read, when not plainly


def read_selector(selector: str) -> SelectorRead:
    notes: list[str] = []
    try:
        found, rest = _selectors(selector)
        rest = _GROUPING.sub(" ", _MODIFIERS.sub(" ", rest))
        if loose := list(_LOOSE_GROUPING.finditer(rest)):
            for m in loose:
                notes.append(f"scope.selector is not PromQL ('{m.group(1)} {m.group(2)}' needs "
                             f"parentheses); read as grouping {m.group(1).lower()} "
                             f"({m.group(2)})")  # fmt: skip
            rest = _LOOSE_GROUPING.sub(" ", rest)
        names = _bare_names(rest)
        if len(found) + len(names) > 1:
            raise Unreadable("several selectors")
        if _COMBINES.search(rest):
            raise Unreadable("operators or label rewriting")
        if not found:  # rate(x[5m]), up offset 5m; no name at all: every evidence series
            return SelectorRead([Matcher("__name__", "=", n) for n in names], notes=notes)
        name, body = found[0]
        ms = _body(body)
    except Unreadable as e:
        return SelectorRead(None, str(e))
    return SelectorRead(([Matcher("__name__", "=", name)] if name else []) + ms, notes=notes)


def pinned_matchers(expr: str | None) -> list[Matcher]:
    """Label matchers (not `__name__`) every series of an evidence expression satisfies: those
    of its one selector, or those every alternative of a combined expression shares (q1p:
    `sum(x{s="a"}) / sum(y{s="a"})` pins s="a"); [] when it cannot be read (a code output)."""
    if not expr:
        return []
    read = read_expr(expr)
    if not read.alternatives:
        return []
    first, *others = read.alternatives
    return [m for m in first if m.label != "__name__" and all(m in o for o in others)]


def undetermined_note(why: str | None) -> str:
    return (f"scope undetermined: scope.selector could not be read into label matchers ({why}); "
            "coverage was checked over every evidence series instead, so which series the claim "
            "is about is not established")  # fmt: skip


@dataclass
class ClaimSeries:
    ids: list[str]  # evidence series the claim is about
    notes: list[tuple[Level, str]]  # how the selector was applied, when not plainly
    mismatch: str | None = None  # the selector names series this evidence does not contain
    # "metric": this evidence is another metric (cited alongside); "labels": the metric matches
    # (or is unknown) but no series has the claimed labels
    mismatch_kind: Literal["metric", "labels"] | None = None
    # the selector has label matchers but none could be applied here: the claimed series are
    # not checked by this evidence at all
    labels_unchecked: bool = False
    # scope.selector could not be read: which series the claim is about is not established
    undetermined: bool = False


def claim_series(
    selector: str | None,
    labels: Mapping[str, Mapping[str, str]],
    metric: str | None = None,
    pinned: Sequence[Matcher] = (),
) -> ClaimSeries:
    """Evidence series (ids of `labels`) the selector names. Series labels carry no `__name__`
    (a dataset drops it): a `__name__` matcher is checked against `metric`, the dataset's metric
    when its expression has one, else not checked (warn). A matcher on a label no evidence series
    carries (aggregated away) is moot when the empty value satisfies it, else not applied (warn):
    the evidence cannot show it, and Prometheus would read it as empty and drop them all. Unless the
    evidence's expression pins that label (`pinned`, from `pinned_matchers`): an equality pin is
    checked (a different value is a mismatch), a regex pin leaves it unchecked (warn)."""
    ids = sorted(labels)
    if selector is None:
        return ClaimSeries(ids, [])
    read = read_expr(selector)
    if read.alternatives is None:
        return ClaimSeries(ids, [("warn", undetermined_note(read.why))], undetermined=True)
    notes: list[tuple[Level, str]] = [("info", n) for n in read.notes]
    results = [_claim_series(alt, labels, metric, pinned) for alt in read.alternatives]
    if len(results) == 1:
        (r,) = results
        r.notes = notes + r.notes
        return r
    # several alternatives (`a or b`, "x, y"): the series any of them names
    hits = [r for r in results if r.mismatch is None]
    if not hits:
        r = next((x for x in results if x.mismatch_kind == "labels"), results[0])
        r.notes = notes + r.notes
        return r
    chosen = sorted({i for r in hits for i in r.ids})
    extra = list(dict.fromkeys(n for r in hits for n in r.notes))
    return ClaimSeries(
        chosen, notes + extra, labels_unchecked=all(r.labels_unchecked for r in hits)
    )


def _claim_series(
    matchers: Sequence[Matcher],
    labels: Mapping[str, Mapping[str, str]],
    metric: str | None,
    pinned: Sequence[Matcher],
) -> ClaimSeries:
    """claim_series for one alternative: the series satisfying all of `matchers`."""
    ids = sorted(labels)
    notes: list[tuple[Level, str]] = []
    carried = {k for lb in labels.values() for k in lb}
    applied: list[Matcher] = []
    for m in matchers:
        pins = [p for p in pinned if p.label == m.label] if m.label not in carried else []
        if m in pins:  # the evidence's expression fixes exactly what the claim names
            applied.append(m)
            continue
        if eq := [p for p in pins if p.op == "="]:
            if all(m.matches(p.value) for p in eq):
                applied.append(m)
                continue
            why = (f"scope.selector names {m}; this evidence's expression fixes "
                   f"{', '.join(map(str, eq))}")  # fmt: skip
            return ClaimSeries([], notes, why, "labels")
        if pins and not m.matches(""):
            why = (f"{m} not checked: this evidence's expression restricts {m.label!r} by "
                   f"{', '.join(map(str, pins))}, which this check does not compare")  # fmt: skip
            notes.append(("warn", why))
            continue
        if m.label == "__name__" and m.label not in carried:
            if metric is None:
                why = (f"{m} not checked: the evidence series carry no metric name, so the "
                       "claim's metric is not confirmed")  # fmt: skip
                notes.append(("warn", why))
            elif not m.matches(metric):
                why = f"scope.selector names {m}; this evidence is metric {metric!r}"
                return ClaimSeries([], notes, why, "metric")
        elif m.label in carried:
            applied.append(m)
        elif not m.matches(""):
            why = (f"{m} not applied: no evidence series carries label {m.label!r} (aggregated "
                   "away?), so the claim's scope is not checked against it")  # fmt: skip
            notes.append(("warn", why))
    on_series = [m for m in applied if m.label in carried]
    chosen = [s for s in ids if all(m.matches(labels[s].get(m.label, "")) for m in on_series)]
    if not chosen:
        named = ", ".join(str(m) for m in on_series)
        why = f"no evidence series matches {named} from scope.selector"
        return ClaimSeries([], notes, why, "labels")
    wanted = [m for m in matchers if m.label != "__name__" and not m.matches("")]
    unchecked = bool(wanted) and not any(m in applied for m in wanted)
    return ClaimSeries(chosen, notes, labels_unchecked=unchecked)


# --- whole expressions: binary operators, functions, several selectors (q1p) -----------------
#
# `read_selector` reads one selector. An evidence expression is often more: a ratio of two
# aggregations (`sum(rate(x_sum[1m])) / sum(rate(x_count[1m]))`), `a or b`, a list of
# selectors written as scope.selector ("x, y, z"). `parse_expr` reads such an expression into a
# tree of selectors (leaves), numbers and binary operators with their vector matching; functions
# and aggregations are read through to their one vector argument (they keep or pool series, never
# rename labels), except those that rewrite labels (label_replace, count_values...), which stay
# unread (`Opaque`). Callers then combine what each leaf says: the series of `a / b` are those
# matched on both sides (an intersection), of `a or b` those of either (a union).

#: binary operators, lowest precedence first (PromQL)
_PREC = {"or": 1, "and": 2, "unless": 2, "==": 3, "!=": 3, "<=": 3, ">=": 3, "<": 3, ">": 3,
         "+": 4, "-": 4, "*": 5, "/": 5, "%": 5, "atan2": 5, "^": 6}  # fmt: skip
_SYMBOLS = ("==", "!=", "<=", ">=", "<", ">", "+", "-", "*", "/", "%", "^")
_WORD_OP = re.compile(r"(?<![A-Za-z0-9_:])(or|and|unless|atan2)(?![A-Za-z0-9_:])", re.IGNORECASE)
_MATCHING = re.compile(
    r"\s*(?P<bool>bool\b)?\s*(?:(?P<kind>on|ignoring)\s*\((?P<labels>[^()]*)\))?"
    r"\s*(?:(?P<group>group_left|group_right)\s*(?:\((?P<extra>[^()]*)\))?)?",
    re.IGNORECASE,
)
_NUMBER = re.compile(
    r"^[+-]?(?:0x[0-9a-f]+|inf|nan|(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)$", re.IGNORECASE
)
_CALL = re.compile(rf"^({_IDENT.pattern})\s*(?:(?:by|without)\s*\([^()]*\)\s*)?\(")
_TRAILING = re.compile(r"^\s*(?:(?:by|without)\s*\([^()]*\))?\s*$", re.IGNORECASE)
#: functions whose output labels are not their input's: what they cover is not read
_REWRITES = frozenset({"label_replace", "label_join", "count_values", "absent",
                       "absent_over_time", "scalar"})  # fmt: skip


@dataclass(frozen=True)
class Leaf:
    expr: str
    matchers: tuple[Matcher, ...]


@dataclass(frozen=True)
class Num:
    """A number or a function of no series (vector(1), time()): restricts nothing."""


@dataclass(frozen=True)
class Opaque:
    why: str


@dataclass(frozen=True)
class BinOp:
    op: str  # lower-case; "," for a list of selectors (read as `or`)
    left: Node
    right: Node
    on: tuple[str, ...] | None = None
    ignoring: tuple[str, ...] | None = None
    group: str | None = None  # group_left | group_right

    def matched(self, label: str) -> bool:
        """Is `label` matched across the operator (both sides agree on its value)?"""
        if self.on is not None:
            return label in self.on
        if self.ignoring is not None:
            return label not in self.ignoring
        return True


Node = Leaf | Num | Opaque | BinOp


def _mask(s: str) -> tuple[str, list[int]]:
    """s with strings, `{...}` and `[...]` blanked out, and the paren depth at each char."""
    out: list[str] = []
    depth: list[int] = []
    d, i, n = 0, 0, len(s)
    while i < n:
        c = s[i]
        if c in "\"'`":
            _, j = _string(s, i)
            out.append(" " * (j - i))
            depth += [d] * (j - i)
            i = j
            continue
        if c in "{[":
            close = "}" if c == "{" else "]"
            j = i + 1
            while j < n and s[j] != close:
                if s[j] in "\"'`":
                    _, j = _string(s, j)
                    continue
                j += 1
            if j >= n:
                raise Unreadable("unbalanced braces" if c == "{" else "unbalanced brackets")
            out.append(" " * (j + 1 - i))
            depth += [d] * (j + 1 - i)
            i = j + 1
            continue
        if c == ")":
            d -= 1
            if d < 0:
                raise Unreadable("unbalanced parentheses")
        out.append(c)
        depth.append(d)
        if c == "(":
            d += 1
        i += 1
    if d:
        raise Unreadable("unbalanced parentheses")
    return "".join(out), depth


def _unary(flat: str, i: int) -> bool:
    """Is the +/- at flat[i] a sign (start, after an operator, '(' or ',', `offset`, or the
    exponent of a number like 1e-3)?"""
    if re.search(r"(?<![A-Za-z0-9_:.])\d+\.?\d*[eE]$", flat[:i]):
        return True
    before = flat[:i].rstrip()
    if not before or before[-1] in "(,+-*/%^<>=!":
        return True
    word = re.search(r"([A-Za-z_]+)$", before)
    return bool(word) and word.group(1).lower() in ("or", "and", "unless", "atan2", "bool",
                                                    "offset")  # fmt: skip


def _operators(flat: str, depth: list[int]) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for m in _WORD_OP.finditer(flat):
        if depth[m.start()] == 0:
            found.append((m.start(), m.group(1).lower()))
    i, n = 0, len(flat)
    while i < n:
        if depth[i] != 0:
            i += 1
            continue
        two = flat[i : i + 2]
        if two in ("==", "!=", "<=", ">="):
            found.append((i, two))
            i += 2
            continue
        c = flat[i]
        if c in "<>*/%^" or (c in "+-" and not _unary(flat, i)):
            found.append((i, c))
        i += 1
    return sorted(found)


def _split_commas(s: str) -> list[str]:
    flat, depth = _mask(s)
    cuts = [i for i, c in enumerate(flat) if c == "," and depth[i] == 0]
    bounds = [-1, *cuts, len(s)]
    return [s[a + 1 : b] for a, b in pairwise(bounds)]


def _labels(text: str | None) -> tuple[str, ...]:
    return tuple(x.strip() for x in (text or "").split(",") if x.strip())


def parse_expr(expr: str, _top: bool = True) -> Node:
    """Read a PromQL expression (or a comma-separated list of selectors, at the top) into a
    tree; raises Unreadable with why when it cannot be read."""
    s = expr.strip()
    if not s:
        raise Unreadable("empty expression")
    flat, depth = _mask(s)
    if _top and any(c == "," and depth[i] == 0 for i, c in enumerate(flat)):
        parts = [parse_expr(p, False) for p in _split_commas(s)]
        node = parts[0]
        for p in parts[1:]:
            node = BinOp(",", node, p)
        return node
    ops = _operators(flat, depth)
    if ops:
        low = min(_PREC[o] for _, o in ops)
        at = [x for x in ops if _PREC[x[1]] == low]
        i, op = at[0] if at[0][1] == "^" else at[-1]  # ^ is right-associative
        left, right = s[:i], s[i + len(op) :]
        m = _MATCHING.match(right)
        rest = right[m.end() :] if m else right
        if not left.strip():
            raise Unreadable(f"operator {op!r} has no left side")
        kind = (m.group("kind") or "").lower() if m else ""
        labels = _labels(m.group("labels")) if m else ()
        group = (m.group("group") or "").lower() if m else ""
        return BinOp(
            op,
            parse_expr(left, False),
            parse_expr(rest, False),
            on=labels if kind == "on" else None,
            ignoring=labels if kind == "ignoring" else None,
            group=group or None,
        )
    if s[0] in "+-":
        return parse_expr(s[1:], False)
    if s[0] == "(" and _close(flat, depth, 0) == len(s) - 1:
        return parse_expr(s[1:-1], False)
    if _NUMBER.match(s) or s[0] in "\"'`":
        return Num()
    call = _CALL.match(s)
    if call:
        open_at = call.end() - 1
        close = _close(flat, depth, open_at)
        if close < 0 or not _TRAILING.match(_MODIFIERS.sub(" ", flat[close + 1 :])):
            raise Unreadable(f"cannot read {s!r}")
        name = call.group(1).lower()
        if name in _REWRITES:
            return Opaque(f"{name} rewrites or drops series labels")
        inner = s[open_at + 1 : close]
        args = [parse_expr(a, False) for a in _split_commas(inner)] if inner.strip() else []
        vectors = [a for a in args if not isinstance(a, Num)]
        if not vectors:
            return Num()
        if len(vectors) > 1:
            return Opaque(f"{name} takes several series arguments")
        return vectors[0]
    read = read_selector(s)
    if read.matchers is None:
        raise Unreadable(read.why or f"cannot read {s!r}")
    return Leaf(s, tuple(read.matchers))


def _close(flat: str, depth: list[int], at: int) -> int:
    """Index of the ')' matching the '(' at flat[at], or -1."""
    for j in range(at + 1, len(flat)):
        if flat[j] == ")" and depth[j] == depth[at]:
            return j
    return -1


def leaves(node: Node) -> list[Leaf]:
    if isinstance(node, Leaf):
        return [node]
    if isinstance(node, BinOp):
        return leaves(node.left) + leaves(node.right)
    return []


def leaf_matchers(expr: str) -> list[Matcher]:
    """Every matcher of every selector in expr (any side of any operator); [] when the
    expression cannot be read."""
    try:
        return [m for lf in leaves(parse_expr(expr)) for m in lf.matchers]
    except Unreadable:
        return []


_KEEPS_NAME = frozenset({"and", "unless", "or", ","})


def _alternatives(node: Node) -> list[list[Matcher]]:
    """The matchers every output series satisfies, per alternative (a series is named by the
    expression when it satisfies one alternative). Raises Unreadable for an Opaque part."""
    if isinstance(node, Leaf):
        return [list(node.matchers)]
    if isinstance(node, Num):
        return [[]]
    if isinstance(node, Opaque):
        raise Unreadable(node.why)
    left, right = _alternatives(node.left), _alternatives(node.right)
    if node.op in ("or", ","):
        return left + right
    if node.op == "unless":
        return left
    keep_name = node.op in _KEEPS_NAME or node.op in ("==", "!=", "<=", ">=", "<", ">")
    many_right = node.group == "group_right"
    out = []
    for a in left:
        for b in right:
            many, one = (b, a) if many_right else (a, b)
            ms = [m for m in many if m.label != "__name__" or (keep_name and not many_right)]
            ms += [m for m in one if m.label != "__name__" and node.matched(m.label)]
            out.append(list(dict.fromkeys(ms)))
    return out


@dataclass
class ExprRead:
    alternatives: list[list[Matcher]] | None  # None: not read
    why: str | None = None
    notes: list[str] = field(default_factory=list)


def read_expr(expr: str) -> ExprRead:
    """Like `read_selector`, for any expression `parse_expr` reads: the matchers its output
    series satisfy, per alternative (one for a selector or a ratio, several for `a or b` or a
    list of selectors)."""
    one = read_selector(expr)
    if one.matchers is not None:
        return ExprRead([one.matchers], notes=one.notes)
    try:
        node = parse_expr(expr)
        alts = _alternatives(node)
    except Unreadable as e:
        return ExprRead(None, str(e))
    notes = []
    if isinstance(node, BinOp) and node.op == ",":
        notes.append("scope.selector lists several selectors: read as any of them")
    return ExprRead(alts, notes=notes)
