"""Turn a live eval run directory into regression fixtures (bead d77.3).

  uv run scripts/trim_eval_run.py build/evals/<scenario>-<ts> <fixture-name> "<provenance note>"

Writes tests/fixtures/evals/<fixture-name>.{snapshot,truth}.json and <fixture-name>.stream.jsonl:
the snapshot without its UI event log, the ground truth as run, and the stream-json transcript
cut to what the scorer and the caps accounting read (message ids, tool names, short inputs and
results, the result line). Tool rounds and num_turns parse the same from the trimmed stream.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EV = ROOT / "tests" / "fixtures" / "evals"
CUT = 300


def _cut(s: str, n: int = CUT) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def trim_event(ev: dict) -> dict | None:
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "init":
        return {"type": t, "subtype": "init", "model": ev.get("model"),
                "mcp_servers": ev.get("mcp_servers", [])}  # fmt: skip
    if t == "assistant":
        msg = ev.get("message") or {}
        content = []
        for c in msg.get("content", []):
            if c.get("type") == "tool_use":
                content.append({"type": "tool_use", "id": c.get("id"), "name": c.get("name"),
                                "input": c.get("input")})  # fmt: skip
            elif c.get("type") == "text":
                content.append({"type": "text", "text": c.get("text", "")})
        return {"type": t, "message": {"id": msg.get("id"), "content": content}}
    if t == "user":
        content = []
        for c in (ev.get("message") or {}).get("content", []):
            if isinstance(c, dict) and c.get("type") == "tool_result":
                raw = c.get("content")
                text = (
                    raw
                    if isinstance(raw, str)
                    else " ".join(x.get("text", "") for x in raw or [] if isinstance(x, dict))
                )
                content.append({"type": "tool_result", "tool_use_id": c.get("tool_use_id"),
                                "is_error": c.get("is_error", False), "content": _cut(text)})  # fmt: skip
        return {"type": t, "message": {"content": content}} if content else None
    if t == "result":
        keep = ("subtype", "num_turns", "total_cost_usd", "duration_ms", "is_error", "result",
                "stop_reason")  # fmt: skip
        return {"type": t, **{k: ev[k] for k in keep if k in ev}}
    return None


def main(run_dir: str, name: str, note: str) -> None:
    d = Path(run_dir)
    snap = json.loads((d / "snapshot.json").read_text())
    snap.pop("events", None)
    snap = {"_fixture": note, **snap}
    (EV / f"{name}.snapshot.json").write_text(json.dumps(snap, indent=1) + "\n")
    shutil.copy2(d / "truth.json", EV / f"{name}.truth.json")
    out = []
    for line in (d / "transcript.jsonl").read_text().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (t := trim_event(ev)) is not None:
            out.append(json.dumps(t, ensure_ascii=False))
    (EV / f"{name}.stream.jsonl").write_text("\n".join(out) + "\n")
    print(f"wrote {name}.snapshot.json, .truth.json, .stream.jsonl ({len(out)} events)")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    main(*sys.argv[1:])
