"""Which evidence series a finding's `scope.selector` names (spec 2026-10-02 §4.4).

The selector's label matchers (=, !=, =~, !~; a bare metric name or `{"name"}` is a `__name__`
matcher) are applied to the evidence series' labels with Prometheus semantics: a regex is fully
anchored and `.` matches a newline (Prometheus compiles `^(?s:re)$`), and a missing label reads as
the empty string. Read from a plain selector, or from the one selector inside an expression that
keeps its series' labels (rate(x{pod="a"}[5m]), sum by (pod) (...), x offset 5m); an expression
whose matchers cannot be tied to its output series (binary operators, label_replace, count_values,
several selectors) is not read at all and the claim is judged over every evidence series, saying
so."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
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
    matchers: list[Matcher] | None  # None: not read, judge every evidence series
    why: str | None = None  # why it was not read


def read_selector(selector: str) -> SelectorRead:
    try:
        found, rest = _selectors(selector)
        rest = _GROUPING.sub(" ", _MODIFIERS.sub(" ", rest))
        names = _bare_names(rest)
        if len(found) + len(names) > 1:
            raise Unreadable("several selectors")
        if _COMBINES.search(rest):
            raise Unreadable("operators or label rewriting")
        if not found:  # rate(x[5m]), up offset 5m; no name at all: every evidence series
            return SelectorRead([Matcher("__name__", "=", n) for n in names])
        name, body = found[0]
        ms = _body(body)
    except Unreadable as e:
        return SelectorRead(None, str(e))
    return SelectorRead(([Matcher("__name__", "=", name)] if name else []) + ms)


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


def claim_series(
    selector: str | None, labels: Mapping[str, Mapping[str, str]], metric: str | None = None
) -> ClaimSeries:
    """Evidence series (ids of `labels`) the selector names. Series labels carry no `__name__`
    (a dataset drops it): a `__name__` matcher is checked against `metric`, the dataset's metric
    when its expression has one, else not checked (warn). A matcher on a label no evidence series
    carries (aggregated away) is moot when the empty value satisfies it, else not applied (warn):
    the evidence cannot show it, and Prometheus would read it as empty and drop them all."""
    ids = sorted(labels)
    if selector is None:
        return ClaimSeries(ids, [])
    read = read_selector(selector)
    if read.matchers is None:
        why = (f"scope.selector not read into label matchers ({read.why}): the claim is judged "
               "over every evidence series")  # fmt: skip
        return ClaimSeries(ids, [("info", why)])
    notes: list[tuple[Level, str]] = []
    carried = {k for lb in labels.values() for k in lb}
    applied: list[Matcher] = []
    for m in read.matchers:
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
    chosen = [s for s in ids if all(m.matches(labels[s].get(m.label, "")) for m in applied)]
    if not chosen:
        named = ", ".join(str(m) for m in applied)
        why = f"no evidence series matches {named} from scope.selector"
        return ClaimSeries([], notes, why, "labels")
    wanted = [m for m in read.matchers if m.label != "__name__" and not m.matches("")]
    unchecked = bool(wanted) and not any(m in applied for m in wanted)
    return ClaimSeries(chosen, notes, labels_unchecked=unchecked)
