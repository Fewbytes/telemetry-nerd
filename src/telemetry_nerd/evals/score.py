"""Score a workspace snapshot against ground truth (pure; spec §10, bead d77.3).

Input: a snapshot, as `live.collect` writes it::

    {"workspace": <GET /api/workspace>, "exprs": {"p3": ["<promql>", ...], "ds7": [...]},
     "transcript": {"result": "<Claude's final answer>", ...}}

(a bare GET /api/workspace body is accepted too). `exprs` maps panel and dataset ids to the
expressions behind them, so evidence can say which entities it covers.

Criteria (each pass / fail / n/a, with the reason and the objects behind it):

per finding
- scoped: source, selector, a time range overlapping the run, a step and an aggregation; and every
  entity the claim names is covered by the scope selector or by the cited evidence (a claim about
  `payment` computed over a selector with no service matcher is an *unscoped claim*).
- evidenced: at least one statistic or panel; cited panels exist; uncertainty flags honest (a
  statistic of unknown uncertainty, or a server-derived evidence flag, is repeated in the claim or
  caveats).
- correct: never blames an unaffected control (causal wording about it, a control-only scope
  asserting a change); directions agree with the expected signals per entity and measure.
- source label: an incident finding (names an implicated entity, asserts a change) labels its
  statistics with an expected source (special cause for a demo fault); common cause alone is wrong.
  The daemon's structured verdicts count too (bead qxp): `scope_check.not_covered` entities are
  unscoped, and a `source_undetermined` source flag lists `undetermined` among the sources.

workspace
- special-cause windows (queue-sim overload; n/a without a finding citing check_littles_law):
  a finding citing the check says that windows of the episode were special cause (transient,
  promoted, a load peak; a special_cause statistic of the check), whatever the verdict word
  (`consistent` + promoted windows is a valid outcome, vayr); "no flagged windows" fails.
- findings present; root cause named by an in-window finding; annotations: at least one onset
  within fault_window.start +- tolerance and none off target; unscoped claims (findings and the
  final transcript) == 0; a root-cause hypothesis supported; no hypothesis blaming a control
  supported; every supported hypothesis has a finding for it and an alternative considered
  (another hypothesis refuted / inconclusive with a finding linked or a stated reason, or
  `alternatives_considered`). Finding-hypothesis links are read from the structured
  `hypotheses` list (the single `hypothesis` + `stance` of older snapshots too).

The text checks (entity mentions, direction and causal wording) are deliberately simple word rules,
reported with the sentence they fired on so a reader can overrule them.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Literal

from telemetry_nerd.evals.truth import COMMON, SPECIAL, Truth

Status = Literal["pass", "fail", "n/a"]

#: d77 acceptance: findings scoped and evidenced, annotations within tolerance, no unscoped claims
ACCEPTANCE = ("findings_scoped", "findings_evidenced", "annotation_onset", "zero_unscoped_claims")

_UP = re.compile(
    r"\b(increas\w*|rose|rise[sn]?|rising|spik\w*|jump\w*|higher|grew|grow\w*|elevated|surg\w*|"
    r"climb\w*|doubl\w*|tripl\w*|went up|up from|exceed\w*|burst\w*|failed|failing|"
    r"started failing|returned (?:\w+ ){0,2}[45]\d\ds?|started returning|shift\w* up|"
    # errors appearing is an onset (eval round 4, payment f1: "first errors 16:41")
    r"first (?:\w+ )?errors?|errors? (?:\w+ ){0,4}(?:appear\w*|began|started|emerged))\b",
    re.IGNORECASE,
)
_DOWN = re.compile(
    r"\b(decreas\w*|drop\w*|fell|fall\w*|lower|declin\w*|reduc\w*|went down|down from|dipp?\w*|"
    r"shr[iu]nk\w*|collaps\w*|shift\w* down)\b",
    re.IGNORECASE,
)
_FLAT = re.compile(
    r"\b(no change|unchanged|flat|stable|stayed|remain\w*|nothing|within|common[ _]cause|normal|"
    r"clean|unaffected|ruled? out|no (?:\w+ ){0,3}(?:errors?|increase|change|shift|signal)|"
    r"not (?:\w+ ){0,2}(?:affected|elevated|increase\w*|change\w*|involved|the cause)|zero)\b",
    re.IGNORECASE,
)
_CAUSAL = re.compile(
    r"\b(root[ -]cause\w*|caus\w*|culprit|responsible|originat\w*|triggered|because|due to|"
    r"source of the|is failing|was failing|broke|broken|(?:that|this|which) is why|led to|"
    r"resulted in)\b",
    re.IGNORECASE,
)
_HEDGE = re.compile(
    r"\b(hypothes\w*|might|may|possibl\w*|perhaps|unclear|unexplained|unknown|not yet|"
    r"would need|untested|to test|candidate)\b",
    re.IGNORECASE,
)
#: the analyst saying what it cannot know: "I cannot confirm X because ..." explains a limit of
#: the evidence, not a cause in the system
_EPISTEMIC = re.compile(
    r"\b(?:cannot|can't|can ?not|unable to|could not|did not|didn't) "
    r"(?:\w+ ){0,2}?(?:confirm|determine|establish|verify|tell|attribute|link|tie|find|see)\w*\b",
    re.IGNORECASE,
)
#: a claim that an entity does not exist in the data ("there is no payment service"): a
#: claim about the source like any other, it needs a cited object (a query, a gap)
_ABSENCE = re.compile(
    # "no service label" names a label, not a missing service (eval round 4, transcript)
    r"\b(?:there (?:is|are|was|were) no|no (?:\w+ ){0,3}(?:services\b|service\b(?!\s*(?:labels?|matchers?)\b))|"
    r"(?:does|do|did)(?: not|n't) (?:exist|emit|report|appear)|not present|absent|"
    r"(?:is|are) missing)",
    re.IGNORECASE,
)
_NEGATED_CAUSE = re.compile(r"\b(not|no|never|ruled? out|rather than|instead of)\b", re.IGNORECASE)
_CITES = re.compile(r"\b[fhpag]\d+\b")
_UNCERTAIN = re.compile(r"uncertain|lower bound|at least|unknown|not propagated", re.IGNORECASE)
_MEASURES = (
    (
        "errors",
        re.compile(r"\berrors?\b|\bfail\w*|\bexception|status_code_error|5xx", re.IGNORECASE),
    ),
    (
        "latency",
        re.compile(r"latenc\w*|\bslow\w*|duration|\bp\d{2}\b|response time", re.IGNORECASE),
    ),
    ("cpu", re.compile(r"\bcpu\b|utili[sz]ation|event[- ]loop", re.IGNORECASE)),
    (
        "rate",
        re.compile(
            r"\brates?\b|throughput|requests?/s|traffic|calls|\bload\b|req/s", re.IGNORECASE
        ),
    ),
)
_SENTENCES = re.compile(r"(?<=[.;!?])\s+|\n+")
#: clauses of a sentence: "X returned 422s from 13:24 to 13:28, then zero" asserts a change in
#: its first clause even though the second says what it returned to
#: a Little's law finding reporting special-cause windows (vayr): the clause must not negate it
_SPECIAL_WINDOWS = re.compile(
    r"special[- _]cause|transient|promot\w*|load[- ]peak|(?:left|leaving|leaves) steady state|"
    r"backlog (?:grew|grows|growth|built)",
    re.IGNORECASE,
)
_NEGATION = re.compile(r"\b(no|not|none|never|without|cannot|nor)\b", re.IGNORECASE)
_CLAUSES = re.compile(r",\s*(?:then|and then|and|after which|before|until)\b|;|\(")
#: a heading or label line over a list of what is not known ("**Not established**",
#: "Unknowns:"): the items under it name open questions, not claims (eval round 3)
_UNKNOWN_HEAD = re.compile(
    r"^\W*(?:not (?:yet )?(?:established|known|tested|explained|measured)|unknowns?|unexplained|"
    r"open questions?|unmeasurable|what remains|remaining questions?|gaps?)\b",
    re.IGNORECASE,
)
_HEADING = re.compile(r"^\s*(?:#+\s+.*|\*\*[^*]+\*\*:?|[A-Z][^.!?]{0,60}:)\s*$")
_MATCHER = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"')
_BY = re.compile(r"\b(?:by)\s*\(([^)]*)\)", re.IGNORECASE)
_MAX_RANGE_MS = 7 * 24 * 3600 * 1000


# --- text helpers


def _variants(entity: str) -> list[str]:
    out = {entity, entity.replace("-", " "), entity.replace("-", ""), entity.replace("-", "_")}
    return sorted(out, key=len, reverse=True)


def mentions(text: str, entity: str) -> bool:
    """Does `text` name `entity` (payment, product-catalog / product catalog, checkout-2)?"""
    alts = "|".join(re.escape(v) for v in _variants(entity))
    return (
        re.search(rf"(?<![A-Za-z0-9_])(?:{alts})(?:service)?(?![A-Za-z0-9_])", text, re.IGNORECASE)
        is not None
    )


def named(text: str, entities: tuple[str, ...] | frozenset[str]) -> set[str]:
    return {e for e in entities if mentions(text, e)}


#: a descriptive root-cause term negated by the words just before it: "at unchanged arrival
#: rate", "not an arrival surge", "no load spike" name the competing cause, not the root cause
#: (eval round 3: h2 "the service slowing down at unchanged arrival rate" scored as root cause)
_TERM_NEGATOR = re.compile(
    r"\b(?:unchanged|constant|steady|flat|stable|same|normal|no|not|without|never|"
    r"rather than|instead of|ruled? out)\b(?:\W+\w+){0,2}\W*$",
    re.IGNORECASE,
)


def _names_phrase(text: str, term: str) -> bool:
    """`term` (a descriptive phrase, matched as a substring) occurs at least once un-negated."""
    low, t = text.lower(), term.lower()
    i = low.find(t)
    while i >= 0:
        if not _TERM_NEGATOR.search(low[:i]):
            return True
        i = low.find(t, i + 1)
    return False


#: an entity named only to deny it: "an arrival surge, not a payment fault" (round 3, h2)
_ENTITY_NEGATOR = re.compile(
    r"\b(?:not|never|without|rather than|instead of|ruled? out)\b(?:\W+\w+){0,1}\W*$",
    re.IGNORECASE,
)


def _names_entity(text: str, entity: str) -> bool:
    """`entity` is named at least once other than right after a negation."""
    alts = "|".join(re.escape(v) for v in _variants(entity))
    rx = rf"(?<![A-Za-z0-9_])(?:{alts})(?:service)?(?![A-Za-z0-9_])"
    return any(
        not _ENTITY_NEGATOR.search(text[: m.start()]) for m in re.finditer(rx, text, re.IGNORECASE)
    )


def names_term(text: str, terms: tuple[str, ...], entities: tuple[str, ...]) -> bool:
    """A root-cause term in prose, un-negated: entity names by word, other terms as substrings."""
    return any(_names_entity(text, t) if t in entities else _names_phrase(text, t) for t in terms)


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCES.split(text or "") if s.strip()]


def measure_in(sentence: str) -> str | None:
    for name, rx in _MEASURES:
        if rx.search(sentence):
            return name
    return None


def asserts_change(text: str) -> bool:
    return bool(_UP.search(text) or _DOWN.search(text)) and not _FLAT.search(text)


# --- selectors


@dataclass(frozen=True)
class _M:
    label: str
    op: str
    value: str

    def matches(self, v: str) -> bool:
        if self.op == "=":
            return v == self.value
        if self.op == "!=":
            return v != self.value
        try:
            hit = re.fullmatch(self.value, v, re.DOTALL) is not None
        except re.error:
            return False
        return hit if self.op == "=~" else not hit


def matchers(expr: str) -> list[_M]:
    return [_M(lbl, op, val.replace('\\"', '"')) for lbl, op, val in _MATCHER.findall(expr or "")]


def _value_names(value: str, entity: str) -> bool:
    """A label value names an entity: equal, or a path ending in it (job="demo/payment")."""
    return value == entity or value.endswith("/" + entity)


def coverage(expr: str, truth: Truth) -> tuple[set[str], bool]:
    """Entities an expression's series cover, and whether the expression restricts them at all.

    Matchers on the entity label (or the usual service labels) pick entities; an aggregation
    `by (<entity label>)` keeps every entity apart (covers all). No matcher and no such grouping:
    the expression mixes every entity (covers none of them specifically)."""
    labels = {truth.entity_label, *truth.extra_labels}
    found = matchers(expr)
    ms = [m for m in found if m.label in labels]
    ents = truth.entities
    sole: set[str] = set()
    if truth.sole in ents:
        if not all(m.matches(truth.sole) for m in found if m.label == truth.sole_label):
            return set(), True  # a matcher excludes the source's only service: nothing of it
        if not ms:
            sole = {truth.sole}  # every series is the sole entity's unless members are picked
    if ms:
        out = set()
        for e in ents:
            if e == truth.sole:
                continue
            pos = [m for m in ms if m.op in ("=", "=~")]
            neg = [m for m in ms if m.op in ("!=", "!~")]
            ok_pos = not pos or any(
                (m.matches(e) or (m.op == "=" and _value_names(m.value, e))) for m in pos
            )
            ok_neg = all(m.matches(e) for m in neg)
            if ok_pos and ok_neg:
                out.add(e)
        return out, True
    grouped = any(lbl.strip() in labels for g in _BY.findall(expr or "") for lbl in g.split(","))
    return (set(ents), True) if grouped else (sole, bool(sole))


# --- results


@dataclass
class Check:
    id: str
    status: Status
    detail: str = ""
    objects: list[str] = field(default_factory=list)


@dataclass
class FindingScore:
    id: str
    claim: str
    scoped: bool
    evidenced: bool
    uncertainty_honest: bool
    in_window: bool
    names_root_cause: bool
    incident: bool
    entities: list[str]
    uncovered: list[str]
    blames_control: list[str]
    direction_errors: list[str]
    sources: list[str]
    source_ok: bool | None
    problems: list[str]


@dataclass
class AnnotationScore:
    id: str
    kind: str
    label: str
    start_ms: int | None
    end_ms: int | None
    onset_delta_s: float | None
    end_delta_s: float | None
    verdict: Literal["onset_ok", "end_ok", "baseline", "off", "not_timed"]


@dataclass
class HypothesisScore:
    id: str
    statement: str
    status: str
    role: Literal["root_cause", "blames_control", "other"]
    evidence_for: int
    evidence_against: int
    alternatives_considered: bool = False  # a note, or another hypothesis ruled out


@dataclass
class Report:
    scenario: str
    kind: str
    checks: list[Check]
    findings: list[FindingScore]
    annotations: list[AnnotationScore]
    hypotheses: list[HypothesisScore]
    unscoped_claims: list[dict]
    score: float
    passed: int
    applicable: int
    acceptance: bool
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def check(self, cid: str) -> Check:
        return next(c for c in self.checks if c.id == cid)


# --- scoring


def _workspace(snapshot: dict) -> dict:
    return snapshot.get("workspace", snapshot)


def _overlaps(a0: int, a1: int, b0: int, b1: int) -> bool:
    return a0 < b1 and b0 < a1


def _evidence_exprs(f: dict, exprs: dict[str, list[str]]) -> list[str]:
    out = []
    for e in f.get("evidence", []):
        if e.get("kind") == "panel":
            out += exprs.get(e.get("panel", ""), [])
        elif e.get("kind") == "statistic":
            out += exprs.get(e.get("dataset", ""), [])
            params = e.get("params") or {}
            out += [str(params[k]) for k in ("selector", "expr", "query") if k in params]
    return out


def score_finding(
    f: dict, truth: Truth, exprs: dict[str, list[str]], panels: set[str]
) -> FindingScore:
    claim = f.get("claim", "")
    text = " ".join([claim, *f.get("caveats", [])])
    scope = f.get("scope") or {}
    problems: list[str] = []
    # --- scope
    tr = scope.get("time_range") or {}
    t0, t1 = tr.get("start_ms"), tr.get("end_ms")
    margin = 3600_000
    fields_ok = all(scope.get(k) for k in ("source", "selector", "step", "aggregation"))
    if not fields_ok:
        problems.append("scope incomplete (source, selector, step, aggregation)")
    time_ok = (
        isinstance(t0, int)
        and isinstance(t1, int)
        and t1 > t0
        and t1 - t0 <= _MAX_RANGE_MS
        and _overlaps(t0, t1, truth.run_start_ms - margin, truth.run_end_ms + margin)
    )
    if not time_ok:
        problems.append("time range missing, longer than 7 days, or outside the run")
    ents = sorted(named(claim, truth.entities))
    sel_cov, _ = coverage(scope.get("selector", "") + " " + scope.get("aggregation", ""), truth)
    ev_cov: set[str] = set()
    for x in _evidence_exprs(f, exprs):
        ev_cov |= coverage(x, truth)[0]
    uncovered = set(ents) - sel_cov - ev_cov
    # the daemon's own verdict on the claim's scope against its evidence (bead qxp)
    for item in (f.get("scope_check") or {}).get("not_covered", []):
        value = item.split("=", 1)[-1].strip('"')
        # ground truth overrules the daemon only for the sole entity its own coverage found:
        # pooling every series of a one-service source covers that service (eval round 3, f3)
        known = {truth.sole} & (sel_cov | ev_cov)
        uncovered |= {e for e in truth.entities if _value_names(value, e)} - known
    uncovered = sorted(uncovered)
    if uncovered:
        problems.append(f"claim names {', '.join(uncovered)} outside its scope and evidence")
    scoped = fields_ok and time_ok and not uncovered
    # --- evidence
    ev = f.get("evidence", [])
    data_ev = [e for e in ev if e.get("kind") in ("statistic", "panel")]
    dangling = [e["panel"] for e in ev if e.get("kind") == "panel" and e.get("panel") not in panels]
    if not data_ev:
        problems.append("no statistic or panel among the evidence")
    if dangling:
        problems.append(f"cites missing panels {dangling}")
    evidenced = bool(data_ev) and not dangling
    flagged = bool(f.get("evidence_flags")) or any(
        e.get("kind") == "statistic" and e.get("uncertainty_unknown") for e in ev
    )
    honest = not flagged or bool(_UNCERTAIN.search(text))
    if not honest:
        problems.append("uncertainty flags not repeated in the claim or caveats")
    # --- correctness
    in_window = time_ok and _overlaps(
        t0,
        t1,
        truth.fault_start_ms - int(truth.tol_start_s * 1000),
        truth.fault_end_ms + int(truth.tol_end_s * 1000),
    )
    rc = names_term(claim, truth.root_cause_terms, truth.entities)
    blames: list[str] = []
    dir_errs: list[str] = []
    for s in sentences(claim):
        here = named(s, truth.entities)
        ctrl = here & set(truth.control)
        calm = (
            _NEGATED_CAUSE.search(s) or _HEDGE.search(s) or _EPISTEMIC.search(s) or _FLAT.search(s)
        )
        if ctrl and _CAUSAL.search(s) and not calm:
            blames += [f"{c}: {s}" for c in sorted(ctrl)]
        m = measure_in(s)
        if m is None:
            continue
        up, down, flat = bool(_UP.search(s)), bool(_DOWN.search(s)), bool(_FLAT.search(s))
        for e in sorted(here):
            want = truth.expected_direction(e, m)
            if want == "up" and down and not up:
                dir_errs.append(f"{e} {m}: expected up; {s}")
            elif want == "down" and up and not down:
                dir_errs.append(f"{e} {m}: expected down; {s}")
            elif want == "flat" and (up or down) and not flat and e in truth.control:
                dir_errs.append(f"{e} {m}: expected flat; {s}")
    # a control-only scope asserting a change blames the control
    control_only = sel_cov and sel_cov <= set(truth.control)
    if control_only and asserts_change(claim) and not against_only(f):
        blames.append(f"{', '.join(sorted(sel_cov))}: change asserted on a control-only scope")
    if blames:
        problems.append("blames an unaffected control")
    if dir_errs:
        problems.append("direction disagrees with ground truth")
    # an incident finding: some sentence names an implicated entity (or the root cause) and
    # asserts a change (judged per sentence: long claims also say what did not change)
    # (the entity per sentence, the change per clause: "Span errors on payment (charge),
    # checkout (...) rose to 0.5/s ..., and were zero from 14:58", eval round 3)
    incident = not against_only(f) and any(
        (named(s, truth.implicated) or names_term(s, truth.root_cause_terms, truth.entities))
        and any(asserts_change(c) for c in [s, *_CLAUSES.split(s)])
        for s in sentences(claim)
    )
    sources = list(
        dict.fromkeys(e["source"] for e in ev if e.get("kind") == "statistic" and e.get("source"))
    )
    if any(x.get("flag") == "source_undetermined" for x in f.get("source_flags") or []):
        sources = list(dict.fromkeys([*sources, "undetermined"]))  # as the daemon lists them
    source_ok: bool | None = None
    if incident and sources:
        source_ok = any(s in truth.expected_sources for s in sources)
    elif sources and not incident and set(ents) and set(ents) <= set(truth.control):
        source_ok = SPECIAL not in sources  # a control said to be calm, with a special cause?
    if incident and sources == [COMMON]:
        source_ok = False
    if source_ok is False:
        problems.append(f"source label {sources} not in {list(truth.expected_sources)}")
    return FindingScore(
        id=f.get("id", "?"),
        claim=claim,
        scoped=scoped,
        evidenced=evidenced,
        uncertainty_honest=honest,
        in_window=in_window,
        names_root_cause=rc,
        incident=incident,
        entities=ents,
        uncovered=uncovered,
        blames_control=blames,
        direction_errors=dir_errs,
        sources=sources,
        source_ok=source_ok,
        problems=problems,
    )


_BASELINE = re.compile(r"baseline|reference|normal|before|pre-?incident|healthy", re.IGNORECASE)


def score_annotation(a: dict, truth: Truth) -> AnnotationScore:
    t0, t1 = a.get("t_start_ms"), a.get("t_end_ms")
    label = a.get("label", "")
    if t0 is None:
        return AnnotationScore(a.get("id", "?"), a.get("kind", ""), label, None, None, None,
                               None, "not_timed")  # fmt: skip
    d0 = (t0 - truth.fault_start_ms) / 1000
    d1 = (t1 - truth.fault_end_ms) / 1000 if t1 is not None else None
    d_end = (t0 - truth.fault_end_ms) / 1000
    if abs(d0) <= truth.tol_start_s and (t1 is not None or abs(d0) <= abs(d_end)):
        verdict = "onset_ok"  # a region is judged by its start; a point by the nearer edge
    elif t1 is None and abs(d_end) <= truth.tol_end_s:
        verdict = "end_ok"  # a point marking the recovery
    elif _BASELINE.search(label) or (
        t1 is not None and t1 <= truth.fault_start_ms + truth.tol_start_s * 1000
    ):
        verdict = "baseline"
    else:
        verdict = "off"
    return AnnotationScore(a.get("id", "?"), a.get("kind", ""), label, t0, t1, d0, d1, verdict)


def _subject(text: str, truth: Truth) -> str | None:
    """The entity a statement is about: the first one it names, un-negated. "payment Charge calls
    were the slow dependency of checkout" proposes payment as the cause (eval round 3, h2)."""
    first: tuple[int, str] | None = None
    for e in truth.entities:
        alts = "|".join(re.escape(v) for v in _variants(e))
        rx = rf"(?<![A-Za-z0-9_])(?:{alts})(?:service)?(?![A-Za-z0-9_])"
        for m in re.finditer(rx, text, re.IGNORECASE):
            if not _ENTITY_NEGATOR.search(text[: m.start()]):
                if first is None or m.start() < first[0]:
                    first = (m.start(), e)
                break
    return first[1] if first else None


def links(f: dict) -> list[dict]:
    """A finding's hypothesis links [{id, stance}] (aiy), read from the structured list, or from
    the single `hypothesis` + `stance` of findings recorded before it."""
    if isinstance(f.get("hypotheses"), list):
        return [x for x in f["hypotheses"] if isinstance(x, dict)]
    if f.get("hypothesis") and f.get("stance"):
        return [{"id": f["hypothesis"], "stance": f["stance"]}]
    return []


def against_only(f: dict) -> bool:
    """The finding only argues against hypotheses (not an incident claim of its own)."""
    ls = links(f)
    return bool(ls) and all(x.get("stance") == "against" for x in ls)


def ruled_out(h: dict) -> bool:
    """Refuted or inconclusive with something behind it: a linked finding or a stated reason
    (aiy: a status alone is not a ruled-out alternative)."""
    return h.get("status") in ("refuted", "inconclusive") and bool(
        h.get("evidence_against") or h.get("evidence_for")
        or (h.get("status_reason") or "").strip()
    )  # fmt: skip


def score_hypothesis(h: dict, truth: Truth, others: list[dict] | None = None) -> HypothesisScore:
    st = h.get("statement", "")
    if names_term(st, truth.root_cause_terms, truth.entities):
        role = "root_cause"
    elif (
        named(st, truth.control)
        and _CAUSAL.search(st)
        or (named(st, truth.entities) and named(st, truth.entities) <= set(truth.control))
        or _subject(st, truth) in truth.control
    ):
        role = "blames_control"
    else:
        role = "other"
    return HypothesisScore(
        h.get("id", "?"),
        st,
        h.get("status", "proposed"),
        role,
        len(h.get("evidence_for", [])),
        len(h.get("evidence_against", [])),
        bool((h.get("alternatives_considered") or "").strip())
        or any(o.get("id") != h.get("id") and ruled_out(o) for o in others or []),
    )


def transcript_unscoped(text: str, truth: Truth) -> list[tuple[str, str]]:
    """Sentences of the final answer, with why, that make a claim about a named entity without
    citing a workspace object (f3, h1, p2, a1, g1) and without hedging: a cause (not the
    analyst's own limits: "cannot confirm X because ..."), or that the entity is absent from the
    data."""
    out = []
    open_list = False  # under a heading listing what is not known
    for line in (text or "").splitlines():
        if _HEADING.match(line) or _UNKNOWN_HEAD.match(line):
            open_list = bool(_UNKNOWN_HEAD.match(line))
            # a label with its list on the same line: "Not established: the root cause ..."
            if open_list or not line.strip().endswith(":"):
                continue
        if open_list and not re.match(r"^\s*(?:[-*+]|\d+[.)])\s", line) and line.strip():
            open_list = False  # prose after the list ends it
        if open_list:
            continue
        out += _unscoped_in(line, truth)
    return out


def _unscoped_in(text: str, truth: Truth) -> list[tuple[str, str]]:
    out = []
    for s in sentences(text):
        if not named(s, truth.entities) or _CITES.search(s) or _HEDGE.search(s):
            continue
        if _CAUSAL.search(s) and not _EPISTEMIC.search(s):
            out.append((s, "causal claim citing no object"))
        elif _ABSENCE.search(s):
            out.append((s, "absence claim citing no object"))
    return out


def _littles_statistics(f: dict) -> list[dict]:
    return [
        e
        for e in f.get("evidence", [])
        if e.get("kind") == "statistic"
        and (
            str(e.get("name", "")).startswith("littles_law")
            or "check_littles_law" in str(e.get("method", ""))
        )
    ]


def reports_special_windows(f: dict) -> bool:
    """Does a finding citing check_littles_law report special-cause windows? A special_cause
    statistic of the check, or a clause naming them that does not negate them ("no flagged
    windows, no transient" does not; "consistent overall; the 14:37 window promoted to special
    cause at a load peak" does)."""
    if any(e.get("source") == SPECIAL for e in _littles_statistics(f)):
        return True
    text = f.get("claim", "")
    for s in sentences(text):
        for c in re.split(r"[,;:(]|\bbut\b|\bwhile\b", s):
            if (m := _SPECIAL_WINDOWS.search(c)) and not _NEGATION.search(c[: m.start()]):
                return True
    return False


def _c(cid: str, ok: bool | None, detail: str, objs: list[str] | None = None) -> Check:
    status: Status = "n/a" if ok is None else ("pass" if ok else "fail")
    return Check(cid, status, detail, objs or [])


def score(snapshot: dict, truth: Truth) -> Report:
    ws = _workspace(snapshot)
    exprs: dict[str, list[str]] = snapshot.get("exprs", {}) or {}
    panels = {p["id"] for p in ws.get("panels", [])}
    fs = [score_finding(f, truth, exprs, panels) for f in ws.get("findings", [])]
    anns = [
        score_annotation(a, truth)
        for a in ws.get("annotations", [])
        if not a.get("deleted") and a.get("author") != "user"
    ]
    all_h = ws.get("hypotheses", [])
    hyps = [score_hypothesis(h, truth, all_h) for h in all_h]
    result = ((snapshot.get("transcript") or {}).get("result")) or ""
    t_unscoped = transcript_unscoped(result, truth)
    unscoped = [
        {"where": f.id, "claim": f.claim, "why": f"names {', '.join(f.uncovered)} outside scope"}
        for f in fs
        if f.uncovered
    ] + [{"where": "transcript", "claim": s, "why": why} for s, why in t_unscoped]

    has = bool(fs)
    checks = [
        _c("findings_present", has, f"{len(fs)} findings"),
        _c(
            "findings_scoped",
            has and all(f.scoped for f in fs),
            "every finding scoped (fields, time in run, claim within scope)",
            [f.id for f in fs if not f.scoped],
        ),
        _c(
            "findings_evidenced",
            has and all(f.evidenced for f in fs),
            "every finding cites a statistic or an existing panel",
            [f.id for f in fs if not f.evidenced],
        ),
        _c(
            "uncertainty_honest",
            all(f.uncertainty_honest for f in fs) if has else None,
            "unknown / unpropagated uncertainty repeated in claim or caveats",
            [f.id for f in fs if not f.uncertainty_honest],
        ),
    ]
    rc = [f.id for f in fs if f.names_root_cause and f.in_window]
    checks.append(
        _c(
            "root_cause_named",
            bool(rc),
            f"an in-window finding names the root cause ({' / '.join(truth.root_cause_terms)})",
            rc,
        )
    )
    blamed = [f.id for f in fs if f.blames_control]
    hyp_blame = [h.id for h in hyps if h.role == "blames_control" and h.status == "supported"]
    checks.append(
        _c(
            "no_control_blamed",
            not blamed and not hyp_blame if truth.control else None,
            f"controls never blamed: {', '.join(truth.control) or '-'}",
            blamed + hyp_blame,
        )
    )
    checks.append(
        _c(
            "directions_consistent",
            all(not f.direction_errors for f in fs) if has else None,
            "stated directions agree with the expected signals",
            [f.id for f in fs if f.direction_errors],
        )
    )
    judged = [f for f in fs if f.source_ok is not None]
    incident = [f for f in fs if f.incident]
    checks.append(
        _c(
            "source_label",
            (all(f.source_ok for f in judged) and any(f.source_ok for f in incident))
            if incident
            else None,
            f"incident findings labelled {' / '.join(truth.expected_sources)}",
            [f.id for f in judged if not f.source_ok]
            or ([f.id for f in incident] if not any(f.source_ok for f in incident) else []),
        )
    )
    raw_fs = {f.get("id", "?"): f for f in ws.get("findings", [])}
    littles = [f for f in fs if _littles_statistics(raw_fs[f.id]) and f.in_window]
    special_ok = [f.id for f in littles if reports_special_windows(raw_fs[f.id])]
    checks.append(
        _c(
            "special_cause_windows",
            bool(special_ok) if truth.expect_special_windows and littles else None,
            "a Little's law finding reports the episode's special-cause windows "
            "(transient or promoted), whatever the verdict word",
            special_ok or [f.id for f in littles],
        )
    )
    onset = [a.id for a in anns if a.verdict == "onset_ok"]
    off = [a.id for a in anns if a.verdict == "off"]
    if truth.no_onset:
        ann_ok: bool | None = None if not off else False
        detail = "fault spans the whole run: no onset to annotate"
    else:
        ann_ok = bool(onset) and not off
        detail = (
            f"onset within +-{truth.tol_start_s:g}s of fault start; none off target "
            f"({len(onset)} ok, {len(off)} off, {len(anns)} annotations)"
        )
    checks.append(_c("annotation_onset", ann_ok, detail, onset + off))
    checks.append(
        _c(
            "zero_unscoped_claims",
            not unscoped,
            f"{len(unscoped)} unscoped claims",
            [u["where"] for u in unscoped],
        )
    )
    rc_h = [h for h in hyps if h.role == "root_cause"]
    checks.append(
        _c(
            "root_cause_hypothesis_supported",
            any(h.status == "supported" for h in rc_h),
            "a hypothesis naming the root cause is supported"
            + (f" (statuses: {', '.join(h.status for h in rc_h)})" if rc_h else " (none)"),
            [h.id for h in rc_h],
        )
    )
    decoys = [h for h in hyps if h.role == "blames_control"]
    checks.append(
        _c(
            "decoys_not_supported",
            all(h.status in ("refuted", "inconclusive") for h in decoys) if decoys else None,
            "hypotheses blaming a control end refuted or inconclusive",
            [h.id for h in decoys if h.status not in ("refuted", "inconclusive")],
        )
    )
    supported = [h for h in hyps if h.status == "supported"]
    undisciplined = [h.id for h in supported if not h.evidence_for or not h.alternatives_considered]
    checks.append(
        _c(
            "supported_hypotheses_disciplined",
            not undisciplined if supported else None,
            "every supported hypothesis has a finding for it and an alternative considered",
            undisciplined,
        )
    )
    app = [c for c in checks if c.status != "n/a"]
    passed = sum(c.status == "pass" for c in app)
    acc = all(c.status == "pass" for c in checks if c.id in ACCEPTANCE and c.status != "n/a")
    return Report(
        scenario=truth.scenario,
        kind=truth.kind,
        checks=checks,
        findings=fs,
        annotations=anns,
        hypotheses=hyps,
        unscoped_claims=unscoped,
        score=round(passed / len(app), 3) if app else 0.0,
        passed=passed,
        applicable=len(app),
        acceptance=acc and has,
        meta={
            "fault_window_ms": [truth.fault_start_ms, truth.fault_end_ms],
            "tolerance_s": [truth.tol_start_s, truth.tol_end_s],
            "root_cause_terms": list(truth.root_cause_terms),
            "controls": list(truth.control),
            "hypothesis_statuses": {h.id: h.status for h in hyps},
            "gaps": len(ws.get("gaps", [])),
            "panels": len(panels),
        },
    )
