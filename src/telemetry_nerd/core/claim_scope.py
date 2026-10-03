"""Which evidence series a finding's `scope.selector` names (spec 2026-10-02 §4.4).

The selector's label matchers (=, !=, =~, !~; a bare metric name or `{"name"}` is a `__name__`
matcher) are applied to the evidence series' labels with Prometheus semantics: a regex is fully
anchored and a missing label reads as the empty string. Read from a plain selector, or from the one
selector inside an expression that keeps its series' labels (rate(x{pod="a"}[5m]), sum by (pod)
(...)); an expression whose matchers cannot be tied to its output series (binary operators,
label_replace, several selectors) is not read at all and the claim is judged over every evidence
series, saying so."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

_IDENT = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
_LABEL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_OP = re.compile(r"=~|!~|!=|=")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "`": "`"}
# outside strings, selectors and ranges: anything that combines series or rewrites their labels
_COMBINES = re.compile(
    r"[-+*/%^<>=!]|\b(?:and|or|unless|on|ignoring|group_left|group_right|label_replace|label_join"
    r"|absent|absent_over_time|vector|scalar)\b"
)


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
        return f'{self.label}{self.op}"{self.value}"'


def _matcher(label: str, op: str, value: str) -> Matcher:
    if op in ("=~", "!~"):
        return Matcher(label, op, value, re.compile(value))  # re.error: caller falls back
    return Matcher(label, op, value)


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
    raise ValueError("unterminated string")


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
            raise ValueError(f"cannot read a label at {s[i:]!r}")
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
            raise ValueError("matcher value must be a string")
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
                    raise ValueError("unbalanced braces")
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
                raise ValueError("unbalanced brackets")
            rest.append(" ")
            i = j + 1
        else:
            rest.append(c)
            i += 1
    return found, "".join(rest)


@dataclass
class SelectorRead:
    matchers: list[Matcher] | None  # None: not read, judge every evidence series
    note: str | None = None  # how it was read, when that is worth saying


def read_selector(selector: str) -> SelectorRead:
    try:
        found, rest = _selectors(selector)
    except (ValueError, re.error):
        found, rest = [], "!"
    plain = rest.strip()
    if not found and _IDENT.fullmatch(plain) and plain not in ("and", "or", "unless"):
        return SelectorRead([Matcher("__name__", "=", plain)])  # a bare metric name
    if len(found) > 1 or _COMBINES.search(rest):
        return SelectorRead(None, _NOT_READ)
    if not found:  # e.g. rate(x[5m]): no label matchers, every evidence series
        return SelectorRead([])
    name, body = found[0]
    try:
        ms = _body(body)
    except (ValueError, re.error):
        return SelectorRead(None, _NOT_READ)
    if name is not None:
        ms.insert(0, Matcher("__name__", "=", name))
    note = None if not plain else "matchers read from the one selector inside the expression"
    return SelectorRead(ms, note)


_NOT_READ = (
    "scope.selector is not one selector whose label matchers name evidence series (operators, "
    "label rewriting or several selectors), so the claim is judged over every evidence series"
)


@dataclass
class ClaimSeries:
    ids: list[str]  # evidence series the claim is about
    notes: list[str]  # how the selector was applied, when not plainly
    mismatch: str | None = None  # the selector names series this evidence does not contain


def claim_series(
    selector: str | None, labels: Mapping[str, Mapping[str, str]], metric: str | None = None
) -> ClaimSeries:
    """Evidence series (ids of `labels`) the selector names. Series labels carry no `__name__`
    (a dataset drops it): a `__name__` matcher is checked against `metric`, the dataset's metric
    when its expression has one, else not applied. A matcher on a label no evidence series carries
    (aggregated away) is not applied either, unless the empty value satisfies it (then it is
    moot): the evidence cannot show it, and Prometheus would read it as empty and drop them all."""
    ids = sorted(labels)
    if selector is None:
        return ClaimSeries(ids, [])
    read = read_selector(selector)
    if read.matchers is None:
        return ClaimSeries(ids, [read.note or _NOT_READ])
    notes = [read.note] if read.note else []
    carried = {k for lb in labels.values() for k in lb}
    applied: list[Matcher] = []
    for m in read.matchers:
        if m.label in carried or m.matches(""):
            applied.append(m)
        elif m.label == "__name__":
            if metric is None:
                notes.append(f"{m} not checked (the evidence series carry no metric name)")
            elif not m.matches(metric):
                return ClaimSeries([], notes, f"scope.selector names {m}; this evidence is "
                                   f"metric {metric!r}")  # fmt: skip
        else:
            notes.append(f"{m} not applied: no evidence series carries label {m.label!r} "
                         "(aggregated away?)")  # fmt: skip
    chosen = [s for s in ids if all(m.matches(labels[s].get(m.label, "")) for m in applied)]
    if not chosen:
        named = ", ".join(str(m) for m in applied)
        return ClaimSeries([], notes, f"no evidence series matches {named} from scope.selector")
    return ClaimSeries(chosen, notes)
