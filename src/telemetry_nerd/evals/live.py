"""Side effects of a live eval: an isolated daemon, headless Claude Code, snapshot collection.

Nothing here runs in `just test` except the pure helpers (`claude_command`, `parse_stream`,
`exprs_from`); the rest needs a live daemon and, for `run_claude`, spends the user's tokens.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path

import httpx

#: tools of the plugin's MCP server, as Claude Code names them for a --plugin-dir plugin
PLUGIN_MCP = "mcp__plugin_telemetry-nerd_telemetry-nerd"
#: built-in tools an eval run never needs (the investigation is MCP-only)
DENIED = (
    "Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Agent", "Task",
    "PowerShell", "REPL", "Read", "Grep", "Glob", "LS",
)  # fmt: skip
#: what a staged plugin copy holds (no sources, tests or ground truth for Claude to read)
PLUGIN_PARTS = (".claude-plugin", "commands", "skills", "hooks", ".mcp.json", "scripts/tn-launch")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_daemon(
    root: Path, source_url: str, data_dir: Path, port: int, log: Path, flavor: str
) -> subprocess.Popen:
    """`telemetry-nerd serve` from this checkout on `port` with its own data dir."""
    data_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "telemetry_nerd.cli", "serve", "--port", str(port),
        "--data-dir", str(data_dir), "--source-url", source_url, "--source-flavor", flavor,
    ]  # fmt: skip
    env = {**os.environ, "TN_DATA_DIR": str(data_dir)}
    env.pop("TN_DAEMON_URL", None)
    fh = log.open("w")
    proc = subprocess.Popen(cmd, cwd=root, stdout=fh, stderr=subprocess.STDOUT, env=env)
    url = f"http://127.0.0.1:{port}"
    for _ in range(120):
        if proc.poll() is not None:
            raise RuntimeError(f"daemon exited ({proc.returncode}); see {log}")
        try:
            if httpx.get(f"{url}/api/health", timeout=1).status_code < 500:
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    proc.terminate()
    raise RuntimeError(f"daemon did not become healthy on {url}; see {log}")


def stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


async def _mcp(url: str, tool: str, args: dict) -> str:
    from mcp import Client
    from mcp.types import TextContent

    async with Client(f"{url}/mcp") as client:
        res = await client.call_tool(tool, args)
    text = "\n".join(c.text for c in res.content if isinstance(c, TextContent))
    if res.is_error:
        raise RuntimeError(f"{tool}: {text[:400]}")
    return text


def learn_source(url: str, source: str = "default") -> dict:
    """What `/telemetry-nerd:connect` + learn would leave behind: the catalog of the source.
    Done by the harness (no tokens) so the run starts where a connected user would."""
    import asyncio

    return json.loads(asyncio.run(_mcp(url, "source_learn", {"source": source})))


# --- collection


def exprs_from(obj: object, out: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """Every {id-ish: expr} pair in a JSON tree: dataset and panel ids to their expressions."""
    out = {} if out is None else out
    if isinstance(obj, dict):
        expr = obj.get("expr")
        if isinstance(expr, str):
            for k in ("dataset", "dataset_id", "id", "panel", "panel_id"):
                v = obj.get(k)
                if isinstance(v, str) and v:
                    out.setdefault(v, [])
                    if expr not in out[v]:
                        out[v].append(expr)
        for v in obj.values():
            exprs_from(v, out)
    elif isinstance(obj, list):
        for v in obj:
            exprs_from(v, out)
    return out


def collect(url: str) -> dict:
    """The workspace as scored: GET /api/workspace, the event log, and panel/dataset exprs."""
    with httpx.Client(base_url=url, timeout=30) as c:
        ws = c.get("/api/workspace").json()
        events = c.get("/api/events", params={"limit": 1000}).json().get("events", [])
        exprs = exprs_from(events)
        for e in events:  # dataset.created: {object_id: "d1", payload: {"expr": ...}}
            expr = (e.get("payload") or {}).get("expr")
            if isinstance(expr, str) and e.get("object_id"):
                exprs.setdefault(e["object_id"], [])
                if expr not in exprs[e["object_id"]]:
                    exprs[e["object_id"]].append(expr)
        for p in ws.get("panels", []):
            try:
                data = c.get(f"/api/panels/{p['id']}/data", params={"width": 50}).json()
            except (httpx.HTTPError, ValueError):
                continue
            found = exprs_from(data)
            mine = [e for v in found.values() for e in v]
            for ds in p.get("dataset_ids", []):
                mine += exprs.get(ds, [])
            if isinstance(data, dict) and isinstance(data.get("expr"), str):
                mine.append(data["expr"])
            exprs[p["id"]] = list(dict.fromkeys(mine))
            exprs.update({k: v for k, v in found.items() if k not in exprs})
    return {"workspace": ws, "exprs": exprs, "events": events}


# --- headless Claude Code


def claude_command(
    prompt: str,
    plugin_dir: Path,
    *,
    model: str = "sonnet",
    max_turns: int = 40,
    max_budget_usd: float = 5.0,
    append_system: str = "",
) -> list[str]:
    """`claude -p` with the plugin, MCP tools of the plugin's server only, no built-in tools that
    write or run code, no user/project settings (run it from an empty directory)."""
    exe = shutil.which("claude") or "claude"
    cmd = [
        exe, "-p", prompt,
        "--plugin-dir", str(plugin_dir),
        "--model", model,
        "--max-turns", str(max_turns),
        "--max-budget-usd", f"{max_budget_usd:g}",
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "dontAsk",
        "--setting-sources", "project",
        "--no-session-persistence",
        "--allowedTools", PLUGIN_MCP, f"{PLUGIN_MCP}__*", "Skill", "ToolSearch",
        "--disallowedTools", *DENIED,
    ]  # fmt: skip
    if append_system:
        cmd += ["--append-system-prompt", append_system]
    return cmd


def stage_plugin(root: Path, dest: Path) -> Path:
    """Copy the plugin's parts (not the repo) to `dest`: Claude gets the commands, skills, hooks
    and MCP config, never the scenario ground truth or the tests that sit in the checkout."""
    for part in PLUGIN_PARTS:
        src, dst = root / part, dest / part
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        elif src.exists():
            shutil.copy2(src, dst)
    return dest


def claude_env(root: Path, daemon_url: str) -> dict:
    """The bridge talks to the eval daemon (TN_DAEMON_URL: never autostarts one) and runs this
    checkout's code: its venv's `telemetry-nerd` comes first on PATH for scripts/tn-launch."""
    env = {**os.environ, "TN_DAEMON_URL": daemon_url}
    env["PATH"] = f"{root / '.venv' / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    for k in ("TN_USE_SOURCE", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(k, None)
    return env


class StreamTally:
    """`parse_stream`, one line at a time, so `run_claude` can act while the run goes on.

    Turn counts (zqa). Claude Code's result `num_turns` counted the prompt plus every tool result
    in both live runs of 2026-10-03 (46 = 1 + 45 results, 37 = 1 + 36), so with parallel tool
    calls it exceeds `--max-turns`, which limits tool-use round trips (model responses that call
    tools). `tool_rounds` counts those: the distinct assistant message ids holding a tool_use
    (stream-json emits each content block of one response as its own event, same id)."""

    def __init__(self) -> None:
        self.out: dict = {"tool_calls": [], "mcp_servers": [], "tools": [], "result": "",
                          "errors": [], "tool_results": 0}  # fmt: skip
        self._messages: set[str] = set()
        self._rounds: set[str] = set()
        self._anon = 0

    @property
    def tool_rounds(self) -> int:
        return len(self._rounds)

    def feed(self, raw: str) -> dict | None:
        """Count one stream-json line; returns the event, or None when it is not JSON."""
        out = self.out
        raw = raw.strip()
        if not raw:
            return None
        try:
            ev = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(ev, dict):
            return None
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            out["mcp_servers"] = ev.get("mcp_servers", [])
            out["tools"] = ev.get("tools", [])
            out["model"] = ev.get("model")
        elif t == "assistant":
            msg = ev.get("message") or {}
            mid = msg.get("id")
            if not isinstance(mid, str):
                self._anon += 1
                mid = f"_anon{self._anon}"
            self._messages.add(mid)
            for block in msg.get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    out["tool_calls"].append(block.get("name"))
                    self._rounds.add(mid)
        elif t == "user":
            for block in (ev.get("message") or {}).get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    out["tool_results"] += 1
                    if block.get("is_error"):
                        c = block.get("content")
                        text = c if isinstance(c, str) else json.dumps(c)[:300]
                        out["errors"].append(text[:300])
        elif t == "result":
            for k in ("result", "total_cost_usd", "num_turns", "duration_ms", "is_error",
                      "subtype", "stop_reason", "permission_denials", "usage"):  # fmt: skip
                if k in ev:
                    out[k] = ev[k]
        return ev

    def summary(self) -> dict:
        out = dict(self.out)
        out["assistant_messages"] = len(self._messages)
        out["tool_rounds"] = self.tool_rounds
        out["tool_counts"] = {
            n: out["tool_calls"].count(n) for n in dict.fromkeys(out["tool_calls"])
        }
        out["outside_plugin"] = sorted(
            {n for n in out["tool_calls"] if n and not n.startswith(PLUGIN_MCP) and n != "Skill"}
        )
        return out


def parse_stream(lines: Iterable[str]) -> dict:
    """Summarise a stream-json transcript: init (tools, MCP servers), tool calls, denials, turn
    counts (`StreamTally`), the final result (text, cost, turns)."""
    tally = StreamTally()
    for raw in lines:
        tally.feed(raw)
    return tally.summary()


def caps(run: dict, max_turns: int | None, max_budget_usd: float | None) -> dict:
    """The run against its caps: tool-use rounds against --max-turns (Claude Code's num_turns is
    not that count, see `StreamTally`), cost against --max-budget-usd (checked by the CLI after
    a turn, so it can overshoot by one), and what stopped it."""
    rounds, cost = run.get("tool_rounds"), run.get("total_cost_usd")
    return {
        "max_turns": max_turns,
        "tool_rounds": rounds,
        "num_turns_reported": run.get("num_turns"),
        "tool_results": run.get("tool_results"),
        "turns_exceeded": None if max_turns is None or rounds is None else rounds > max_turns,
        "max_budget_usd": max_budget_usd,
        "cost_usd": cost,
        "budget_exceeded": (
            None if max_budget_usd is None or cost is None else cost > max_budget_usd
        ),
        "stopped_by": _stopped_by(run.get("subtype"), run.get("aborted")),
        "note": "num_turns (Claude Code) counts the prompt and each tool result; --max-turns "
        "limits tool-use rounds",
    }


def _stopped_by(subtype: str | None, aborted: str | None) -> str | None:
    if subtype == "error_max_turns":
        return "max_turns"
    if subtype == "error_max_budget_usd":
        return "max_budget"
    if aborted:
        return "harness"
    return "finished" if subtype == "success" else subtype


def mcp_connected(init_servers: list[dict]) -> bool:
    return any(
        "telemetry-nerd" in (s.get("name") or "") and s.get("status") == "connected"
        for s in init_servers
    )


_DENIED_RX = re.compile(r"permission|haven't granted|not allowed|denied", re.IGNORECASE)
MAX_DENIALS = 3


def run_claude(
    cmd: list[str],
    env: dict,
    cwd: Path,
    transcript: Path,
    timeout_s: float,
    max_turns: int | None = None,
    max_budget_usd: float | None = None,
) -> dict:
    """Run headless Claude, streaming its transcript to a file. Stops it at once when the init
    event shows the telemetry-nerd MCP server is not connected (nothing to evaluate: no tokens
    spent past the first request), after `timeout_s` wall-clock seconds, and when the stream
    shows more than `max_turns` tool-use rounds (a backstop to the CLI's --max-turns, counted
    the way it counts). The result's `caps` records rounds and cost against the caps."""
    t0 = time.time()
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )  # fmt: skip
    aborted = None
    denials = 0
    tally = StreamTally()
    assert proc.stdout is not None
    with transcript.open("w") as fh:
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        while True:
            if time.time() - t0 > timeout_s:
                aborted = f"wall-clock timeout {timeout_s:.0f}s"
                break
            if not sel.select(timeout=1.0):
                if proc.poll() is not None:
                    break
                continue
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            fh.write(line)
            fh.flush()
            errors_before = len(tally.out["errors"])
            ev = tally.feed(line)
            init = bool(ev) and ev.get("type") == "system" and ev.get("subtype") == "init"
            if init and not mcp_connected(tally.out["mcp_servers"]):
                aborted = f"telemetry-nerd MCP server not connected: {tally.out['mcp_servers']}"
                break
            new_errors = tally.out["errors"][errors_before:]
            denials += sum(_DENIED_RX.search(e) is not None for e in new_errors)
            if denials >= MAX_DENIALS:
                aborted = f"{denials} permission denials: the tool allowlist does not match"
                break
            if max_turns is not None and tally.tool_rounds > max_turns:
                aborted = (
                    f"turn cap: {tally.tool_rounds} tool-use rounds > max_turns {max_turns} "
                    "(harness guard; the CLI did not stop it)"
                )
                break
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    err = proc.stderr.read() if proc.stderr else ""
    out = tally.summary()
    out["aborted"] = aborted
    out["caps"] = caps(out, max_turns, max_budget_usd)
    out["exit_code"] = proc.returncode
    out["stderr_tail"] = err[-2000:]
    out["duration_s"] = round(time.time() - t0, 1)
    return out
