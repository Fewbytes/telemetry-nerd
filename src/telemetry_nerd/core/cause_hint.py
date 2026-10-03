"""A gentle nudge after finding_create (bead t75): a special-cause finding whose subject no open
hypothesis names. Investigations found the episode, then only tested question-framing or decoy
hypotheses; the hint asks for a cause hypothesis naming the subject, with a competitor. Never a
refusal: the finding is recorded either way."""

from __future__ import annotations

import re

from telemetry_nerd.analysis.sources import SPECIAL
from telemetry_nerd.workspace.models import Finding, Hypothesis

_NAMED = re.compile(r'^[\w.]+="(.*)"$')


def subjects(f: Finding) -> list[str]:
    """The entity values the finding names (label="value" -> value), as the scope check read them."""
    named = f.scope_check.named if f.scope_check else []
    out = []
    for n in named:
        m = _NAMED.match(n)
        out.append(m[1] if m else n)
    return list(dict.fromkeys(v for v in out if v))


def cause_hint(f: Finding, hypotheses: list[Hypothesis]) -> str | None:
    if SPECIAL not in f.sources:
        return None
    if any(x.stance == "for" for x in f.hypotheses):
        return None  # it already backs a hypothesis
    live = [h for h in hypotheses if h.status != "refuted"]
    subj = subjects(f)
    if subj:
        text = " ".join(h.statement.lower() for h in live)
        if any(v.lower() in text for v in subj):
            return None
    elif live:
        return None  # no subject to compare with: an open hypothesis may well be about it
    who = ", ".join(subj[:3]) or "the episode"
    return (
        f"{f.id} records a special-cause change ({who}) and no open hypothesis names "
        f"{'it' if len(subj) <= 1 else 'them'}. Open a cause hypothesis for this episode "
        f"(hypothesis_create naming the concrete subject: {who}, a flag, a deploy, a load "
        "change), open at least one competing cause, and test both with findings "
        "(stance for/against). A question-framing or decoy hypothesis does not explain it."
    )
