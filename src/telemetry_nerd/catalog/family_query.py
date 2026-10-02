"""Query a name-template family as one metric (bead 2as.26). Pure.

A family's members encode a dimension in the metric name (`airflow_ti_finish_*_removed`). Writing
the template where a metric name goes selects every member and exposes the encoded text as the
`dimension` label, so `sum by (dimension)`, `fleet(by=[dimension])` and ordinary grouping work.

    airflow_ti_finish_*_removed{job="x"}
      -> label_replace({__name__=~"airflow_ti_finish_.+_removed",job="x"}, "dimension", "$1",
                       "__name__", "airflow_ti_finish_(.+)_removed")

Range functions drop the metric name, so the dimension would be lost (and members would collide on
equal label sets): `rate(<template>[5m])` is only possible where the engine can keep names
(MetricsQL `keep_metric_names`); on plain Prometheus it is refused with that reason."""

from __future__ import annotations

import re
from collections.abc import Callable

DIMENSION_LABEL = "dimension"
SLOT = "*"
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`[^`]*`')
_IDENT = r"[A-Za-z_:][A-Za-z0-9_:]*\*[A-Za-z0-9_:*]*"
_RANGE_CALL = re.compile(
    rf"(?P<fn>[A-Za-z_][A-Za-z0-9_]*)\(\s*(?P<t>{_IDENT})\s*(?P<m>\{{[^{{}}]*\}})?\s*(?P<r>\[[^\]]+\])\s*\)"
)
_INSTANT = re.compile(rf"(?<![A-Za-z0-9_:\"'\[])(?P<t>{_IDENT})\s*(?P<m>\{{[^{{}}]*\}})?")


class FamilyQueryRefused(ValueError):
    """A template used where it cannot be made to work; the message says how to proceed."""


def name_regex(template: str) -> tuple[str, str]:
    """(PromQL name matcher, capture regex) for a template with one slot."""
    if template.count(SLOT) != 1:
        raise FamilyQueryRefused(f"{template!r} must have exactly one slot ({SLOT})")
    pre, suf = (re.escape(p) for p in template.split(SLOT))
    return f"{pre}.+{suf}", f"{pre}(.+){suf}"


def _selector(template: str, matchers: str | None) -> str:
    match, _ = name_regex(template)
    inner = (matchers or "").strip()[1:-1].strip()
    return f'{{__name__=~"{match}"{"," + inner if inner else ""}}}'


def _replace(selector_or_call: str, template: str) -> str:
    _, cap = name_regex(template)
    return f'label_replace({selector_or_call}, "{DIMENSION_LABEL}", "$1", "__name__", "{cap}")'


def rewrite(
    expr: str, is_family: Callable[[str], bool], *, keeps_names: bool
) -> tuple[str, list[str]]:
    """`expr` with known family templates expanded, and the templates used. Text inside string
    literals is never touched. Unknown `*` identifiers are left for the source to reject."""
    used: list[str] = []
    literals: list[str] = []

    def mask(m: re.Match) -> str:  # a literal inside a matcher must not break the matcher scan
        literals.append(m.group(0))
        return f"\x00{len(literals) - 1}\x00"

    text = _rewrite_code(_STRING.sub(mask, expr), is_family, keeps_names, used)
    return re.sub("\x00(\\d+)\x00", lambda m: literals[int(m.group(1))], text), used


def _rewrite_code(text: str, is_family, keeps_names: bool, used: list[str]) -> str:
    def call(m: re.Match) -> str:
        t = m.group("t")
        if not is_family(t):
            return m.group(0)
        if not keeps_names:
            raise FamilyQueryRefused(
                f"{m.group('fn')}({t}[...]) would drop the metric name, losing the {DIMENSION_LABEL} "
                "and merging members. A range function over a family needs MetricsQL "
                "(keep_metric_names): use a victoriametrics source, or query members one by one"
            )
        used.append(t)
        sel = _selector(t, m.group("m"))
        return _replace(f"{m.group('fn')}({sel}{m.group('r')}) keep_metric_names", t)

    text = _RANGE_CALL.sub(call, text)

    def instant(m: re.Match) -> str:
        t = m.group("t")
        if not is_family(t):
            return m.group(0)
        used.append(t)
        return _replace(_selector(t, m.group("m")), t)

    return _INSTANT.sub(instant, text)
