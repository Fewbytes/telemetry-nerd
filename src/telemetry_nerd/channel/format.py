"""Human-readable one-liners for events; the channel payload Claude receives."""

from __future__ import annotations

import json

from telemetry_nerd.core.events import Event
from telemetry_nerd.model.time import iso

_DECIDED = {"approve": "approved", "reject": "rejected"}


def _decided(p: dict) -> str:
    """The verb of a retrospective decision: approved, approved (edited) or rejected."""
    return "approved (edited)" if p.get("edited") else _DECIDED[p["decision"]]


def _comment(p: dict) -> str:
    return f": {p['comment']}" if p.get("comment") else ""


def _iso_z(ms: int) -> str:
    return iso(ms).replace("+00:00", "Z")


def _selection(sel: dict | None) -> str:
    if not sel:
        return ""
    return f" [{_iso_z(sel['start_ms'])}–{_iso_z(sel['end_ms'])}]"


def describe_event(e: Event) -> str:
    p = e.payload
    who = e.actor
    match e.type:
        case "thread.message":
            where = f" about {p['anchor']}" if p.get("anchor") else ""
            return f'{who} asked in {p["thread"]}{where}{_selection(p.get("selection"))}: "{p["text"]}"'
        case "finding.verdict":
            return f'{who} {p["verdict"]} {e.object_id} ("{p.get("claim", "")}"){_comment(p)}'
        case "hypothesis.status_changed":
            return (
                f"{who} set {e.object_id} {p['from']} → {p['to']}"
                + (f": {p['note']}" if p.get("note") else "")
                + (f" (reason: {p['reason']})" if p.get("reason") else "")
            )
        case "annotation.created":
            on = f" on {p['panel']}" if p.get("panel") else ""
            label = f' "{p["label"]}"' if p.get("label") else ""
            return f"{who} added {p['kind']} {e.object_id}{on}{label}"
        case "panel.created":
            return f"{who} opened {e.object_id} ({p.get('question', '')})"
        case "panel.y_view_selected":
            via = f" (Claude's {p['suggestion']}: {p['reason']})" if p.get("suggestion") else ""
            return f'{who} switched {e.object_id} y-view to "{p["label"]}"{via}'
        case "panel.y_view_suggested":
            v = p["view"]
            return f'{who} suggested y-view {v["id"]} "{v["label"]}" on {e.object_id}: {v.get("reason", "")}'
        case "panel.marginal_set":
            if not p.get("reference"):
                return f"{who} turned off the marginal histogram on {e.object_id}"
            why = f": {p['reason']}" if p.get("reason") else ""
            return f"{who} showed {e.object_id} marginal vs {p['label']}{why}"
        case "panel.data_view_selected":
            return (
                f'{who} switched {e.object_id} to "{p["view"]}" '
                f"(Claude's default: {p['default']}, {p['filter']})"
            )
        case "panel.closed":
            return f"{who} closed {e.object_id}"
        case "panel.rescoped":
            return f"{who} rescoped {p['from']} to {e.object_id}"
        case "catalog.claimed":
            return (
                f"{who} set {p['field']} of {p['metric']} on {p['source']} "
                f"to {json.dumps(p['value'], ensure_ascii=False)}"
            )
        case "catalog.family_confirmed":
            return f"{who} confirmed the name family {p['template']} ({p['members']} metrics)"
        case "catalog.family_split":
            return f"{who} split the name family {p['template']}: {p['released']} metrics are ordinary again"
        case "relation.claimed":
            verb = "retracted" if p.get("retracted") else "asserted"
            return f"{who} {verb} {p['subject']} {p['kind']} {p['object']}"
        case "binding.claimed":
            verb = "retracted" if p.get("retracted") else "set"
            roles = ", ".join(f"{r}={m or '?'}" for r, m in p["roles"].items())
            return f"{who} {verb} {p['kind']} binding {p['key']}: {roles}"
        case "object.highlighted":
            note = p.get("note")
            return f"{who} highlighted {e.object_id}" + (f': "{note}"' if note else "")
        case "code.started":
            inputs = ", ".join(p.get("inputs") or []) or "no inputs"
            again = f" (re-run of {p['rerun_of']})" if p.get("rerun_of") else ""
            return f"{who} ran code {e.object_id} on {inputs}{again}"
        case "code.finished":
            outs = ", ".join(p.get("outputs") or [])
            tail = f" → {outs}" if outs else ""
            err = f": {p['error']}" if p.get("error") else ""
            return f"code {e.object_id} {p.get('status')} in {p.get('duration_s')} s{tail}{err}"
        case "workspace.opened":
            title = f' "{p["title"]}"'
            if p.get("created"):
                q = f' (question: "{p["question"]}")' if p.get("question") else ""
                head = f"{who} opened a new workspace {e.object_id}{title}{q}"
            else:
                head = f"{who} reopened workspace {e.object_id}{title}"
            return head + (f"; previous {p['from']}" if p.get("from") else "")
        case "workspace.updated":
            parts = []
            if "title" in p:
                parts.append(f'renamed {e.object_id} to "{p["title"]}"')
            if "question" in p:
                parts.append(
                    f'changed the question of {e.object_id} to "{p["question"]}"'
                    if p["question"]
                    else f"cleared the question of {e.object_id}"
                )
            if "archived" in p:
                parts.append(f"{'archived' if p['archived'] else 'unarchived'} {e.object_id}")
            return f"{who} " + "; ".join(parts) if parts else f"{who} updated {e.object_id}"
        case "proposal.decided":
            what = f"{p['field']} of {p['metric']} = {json.dumps(p['value'], ensure_ascii=False)}"
            return f"{who} {_decided(p)} catalog proposal {e.object_id} ({what}){_comment(p)}"
        case "lesson.decided":
            return f'{who} {_decided(p)} lesson {e.object_id} ("{p["text"]}"){_comment(p)}'
        case "lesson.refuted":
            by = f" citing {', '.join(p['evidence'])}" if p.get("evidence") else ""
            return f'{who} refuted lesson {e.object_id} ("{p["text"]}"){by}: {p["reason"]}'
        case "lesson.proposed":
            return f'{who} proposed lesson {e.object_id}: "{p["text"]}"'
        case "proposal.created":
            value = json.dumps(p["value"], ensure_ascii=False)
            return f"{who} proposed {e.object_id}: {p['field']} of {p['metric']} = {value}"
        case "focus.changed":
            return f"{who} focused {_selection(p).strip()}"
        case _:
            return f"{who} {e.type} {e.object_id or ''}".rstrip()


def format_channel(intentional: list[Event], ambient: list[Event]) -> tuple[str, dict[str, str]]:
    first = intentional[0]
    current = intentional[-1].workspace

    def line(e: Event) -> str:
        text = describe_event(e)
        return text if e.workspace == current else f"[{e.workspace}] {text}"

    lines = [line(e) for e in intentional]
    if ambient:
        lines.append("ambient: " + "; ".join(line(e) for e in ambient))
    meta = {
        "workspace": current,
        "event": first.type,
        "seqs": ",".join(str(e.seq) for e in intentional),
    }
    anchor = first.payload.get("anchor") or first.payload.get("panel")
    if isinstance(anchor, str) and anchor.startswith("p"):
        meta["panel"] = anchor
    if first.type == "thread.message":
        meta["thread"] = str(first.payload["thread"])
    return "\n".join(lines), meta
