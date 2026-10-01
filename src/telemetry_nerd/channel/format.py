"""Human-readable one-liners for events; the channel payload Claude receives."""

from __future__ import annotations

import json

from telemetry_nerd.core.events import Event
from telemetry_nerd.model.time import iso


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
            tail = f": {p['comment']}" if p.get("comment") else ""
            return f'{who} {p["verdict"]} {e.object_id} ("{p.get("claim", "")}"){tail}'
        case "hypothesis.status_changed":
            return f"{who} set {e.object_id} {p['from']} → {p['to']}" + (
                f": {p['note']}" if p.get("note") else ""
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
        case "catalog.claimed":
            return (
                f"{who} set {p['field']} of {p['metric']} on {p['source']} "
                f"to {json.dumps(p['value'], ensure_ascii=False)}"
            )
        case "object.highlighted":
            note = p.get("note")
            return f"{who} highlighted {e.object_id}" + (f': "{note}"' if note else "")
        case "focus.changed":
            return f"{who} focused {_selection(p).strip()}"
        case _:
            return f"{who} {e.type} {e.object_id or ''}".rstrip()


def format_channel(intentional: list[Event], ambient: list[Event]) -> tuple[str, dict[str, str]]:
    first = intentional[0]
    lines = [describe_event(e) for e in intentional]
    if ambient:
        lines.append("ambient: " + "; ".join(describe_event(e) for e in ambient))
    meta = {
        "workspace": "w1",
        "event": first.type,
        "seqs": ",".join(str(e.seq) for e in intentional),
    }
    anchor = first.payload.get("anchor") or first.payload.get("panel")
    if isinstance(anchor, str) and anchor.startswith("p"):
        meta["panel"] = anchor
    if first.type == "thread.message":
        meta["thread"] = str(first.payload["thread"])
    return "\n".join(lines), meta
