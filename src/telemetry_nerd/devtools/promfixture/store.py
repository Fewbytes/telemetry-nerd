"""In-memory sample store for the fixture PromQL source (bead y7hb).

Series are keyed by their full label set (including `__name__`); samples are kept sorted by
timestamp, and a re-imported timestamp replaces the old value (VictoriaMetrics' import does the
same with dedup). Exposition text is the import format (`name{l="v"} value ts_ms`, as
`devtools.synthetic.exposition` writes it); `# HELP` / `# TYPE` lines become metadata.
"""

from __future__ import annotations

import bisect
import math
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field

Labels = Mapping[str, str]
LabelKey = tuple[tuple[str, str], ...]


def label_key(labels: Labels) -> LabelKey:
    return tuple(sorted(labels.items()))


@dataclass
class Series:
    labels: dict[str, str]
    ts: list[int] = field(default_factory=list)  # ms, ascending
    vs: list[float] = field(default_factory=list)

    def add(self, t: int, v: float) -> None:
        if not self.ts or t > self.ts[-1]:
            self.ts.append(t)
            self.vs.append(v)
            return
        i = bisect.bisect_left(self.ts, t)
        if i < len(self.ts) and self.ts[i] == t:
            self.vs[i] = v
        else:
            self.ts.insert(i, t)
            self.vs.insert(i, v)

    def window(self, lo_excl: int, hi_incl: int) -> tuple[list[int], list[float]]:
        """Samples with lo_excl < ts <= hi_incl."""
        i = bisect.bisect_right(self.ts, lo_excl)
        j = bisect.bisect_right(self.ts, hi_incl)
        return self.ts[i:j], self.vs[i:j]

    def has_samples(self, start_ms: int | None, end_ms: int | None) -> bool:
        lo = -math.inf if start_ms is None else start_ms
        hi = math.inf if end_ms is None else end_ms
        i = bisect.bisect_left(self.ts, lo)
        return i < len(self.ts) and self.ts[i] <= hi


@dataclass(frozen=True)
class Matcher:
    name: str
    op: str  # one of = != =~ !~
    value: str

    def matches(self, labels: Labels) -> bool:
        got = labels.get(self.name, "")
        if self.op == "=":
            return got == self.value
        if self.op == "!=":
            return got != self.value
        hit = re.fullmatch(self.value, got) is not None
        return hit if self.op == "=~" else not hit


class Store:
    def __init__(self) -> None:
        self._series: dict[LabelKey, Series] = {}
        #: metric name -> {"type", "help", "unit"} (from # TYPE / # HELP lines)
        self.metadata: dict[str, dict[str, str]] = {}

    def __iter__(self) -> Iterator[Series]:
        return iter(self._series.values())

    def __len__(self) -> int:
        return len(self._series)

    def add(self, labels: Labels, samples: Iterable[tuple[int, float]]) -> None:
        key = label_key(labels)
        s = self._series.get(key)
        if s is None:
            s = self._series[key] = Series(dict(labels))
        for t, v in samples:
            s.add(t, v)

    def select(
        self,
        matchers: Iterable[Matcher],
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[Series]:
        ms = list(matchers)
        out = [s for s in self._series.values() if all(m.matches(s.labels) for m in ms)]
        if start_ms is not None or end_ms is not None:
            out = [s for s in out if s.has_samples(start_ms, end_ms)]
        return out

    def delete(self, matchers: Iterable[Matcher]) -> int:
        doomed = [label_key(s.labels) for s in self.select(matchers)]
        for k in doomed:
            del self._series[k]
        return len(doomed)

    def import_text(self, text: str, default_ts_ms: int) -> int:
        """Import exposition lines; returns the number of samples read."""
        batch: dict[LabelKey, list[tuple[int, float]]] = {}
        n = 0
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                self._meta_line(line)
                continue
            labels, value, ts = parse_sample_line(line)
            batch.setdefault(label_key(labels), []).append(
                (default_ts_ms if ts is None else ts, value)
            )
            n += 1
        for key, samples in batch.items():
            self.add(dict(key), samples)
        return n

    def _meta_line(self, line: str) -> None:
        parts = line[1:].strip().split(None, 2)
        if len(parts) < 3 or parts[0] not in ("HELP", "TYPE", "UNIT"):
            return
        kind, name, rest = parts
        entry = self.metadata.setdefault(name, {"type": "unknown", "help": "", "unit": ""})
        entry[kind.lower()] = rest


_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*,?')
_UNESCAPE = {"n": "\n", "\\": "\\", '"': '"'}


def _unescape(v: str) -> str:
    return re.sub(r"\\(.)", lambda m: _UNESCAPE.get(m.group(1), m.group(0)), v)


def parse_value(text: str) -> float:
    low = text.lower()
    if low in ("+inf", "inf"):
        return math.inf
    if low == "-inf":
        return -math.inf
    return float(text)


def parse_sample_line(line: str) -> tuple[dict[str, str], float, int | None]:
    m = _NAME.match(line)
    if m is None:
        raise ValueError(f"cannot parse sample line: {line[:120]!r}")
    labels = {"__name__": m.group(0)}
    pos = m.end()
    if pos < len(line) and line[pos] == "{":
        end = line.index("}", pos)  # label values with "}" are not used by fixtures
        body = line[pos + 1 : end]
        for lm in _LABEL.finditer(body):
            labels[lm.group(1)] = _unescape(lm.group(2))
        pos = end + 1
    rest = line[pos:].split()
    if not rest:
        raise ValueError(f"sample line without a value: {line[:120]!r}")
    value = parse_value(rest[0])
    ts = int(float(rest[1])) if len(rest) > 1 else None
    return labels, value, ts
