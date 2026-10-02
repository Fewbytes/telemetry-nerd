"""Kernel-side per-run state. The daemon runs `begin_run(...)` (silently) before each run.

Kernels are persistent, so per-run settings (e.g. `TN_RUN_DIR`) cannot be fixed at process
start: `begin_run` sets this run's environment, removes the previous run's keys that are not
set again, resets the working directory and calls the registered `on_run` hooks. Libraries
that cache per-run state (the `tn` client) either read `os.environ` lazily or register a hook:

    from telemetry_nerd.kernels.runtime import on_run

    @on_run
    def _reset() -> None: ...

Stdlib only: it is imported inside the kernel.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable

_hooks: list[Callable[[], None]] = []
_applied: set[str] = set()
_home = os.getcwd()


def on_run(fn: Callable[[], None]) -> Callable[[], None]:
    """Register `fn` to run at the start of every run (after env and cwd are set)."""
    if fn not in _hooks:
        _hooks.append(fn)
    return fn


def begin_run(payload: str) -> None:
    """Apply a run's settings. `payload` is JSON {"env": {str: str}, "cwd": str | null}."""
    global _applied
    spec = json.loads(payload)
    env: dict[str, str] = spec.get("env") or {}
    for key in _applied - env.keys():
        os.environ.pop(key, None)
    os.environ.update(env)
    _applied = set(env)
    os.chdir(spec.get("cwd") or _home)
    for hook in _hooks:
        hook()


def preamble(env: dict[str, str], cwd: str | None) -> str:
    """The code the daemon runs before a user cell."""
    payload = json.dumps({"env": env, "cwd": cwd})
    return (
        "import telemetry_nerd.kernels.runtime as _tn_runtime\n"
        f"_tn_runtime.begin_run({payload!r})\n"
        "del _tn_runtime\n"
    )
