"""Daemon side of tier-2 execution: one IPython kernel subprocess per workspace (spec §5.2).

`KernelManager.execute(workspace_id, code, env=..., cwd=..., timeout_s=...)` runs code in the
workspace's kernel (started lazily with the daemon's interpreter) and always returns an
`ExecResult`; a runaway, crashing or memory-hungry run never raises into the daemon.

Guarantees:
- One run at a time per kernel. Concurrent `execute` calls for the same workspace queue (FIFO
  on an asyncio lock); the timeout clock starts when a run gets the kernel, not while queued.
  Different workspaces run in parallel.
- Timeout: SIGINT to the kernel's process group, then `interrupt_grace_s` for the cell to
  stop. If it stops, the kernel and its state survive (`status="timeout"`,
  `restarted=False`); otherwise the kernel is killed and restarted (`restarted=True`).
- Crash (os._exit, segfault, OOM kill): `status="crashed"`, kernel restarted, daemon unaffected.
- Memory: RLIMIT_AS on Linux (`MemoryError` inside the cell); not enforced on macOS.
- Idle kernels are shut down after `idle_timeout_s`, and starting a kernel shuts down the
  least recently used idle ones beyond `max_live` (hopping workspaces does not pile up
  kernels); `aclose()` kills all kernels (process groups, so user subprocesses too).
  Parent-death handling: see `kernels.launch`.
- stdout/stderr are bounded (head + tail kept, `truncated=True`).
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import os
import re
import signal
import sys
import time
import uuid
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self

from telemetry_nerd.kernels.runtime import preamble

if TYPE_CHECKING:
    from jupyter_client.asynchronous.client import AsyncKernelClient
    from jupyter_client.manager import AsyncKernelManager

    from telemetry_nerd.config import Settings

log = logging.getLogger(__name__)

ExecStatus = Literal["ok", "error", "timeout", "crashed"]

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")
_LIVENESS_POLL_S = 0.05


@dataclass(frozen=True)
class ExecResult:
    status: ExecStatus
    stdout: str = ""
    stderr: str = ""
    #: text/plain repr of the cell's last expression (like a notebook's Out[]), if any
    result: str | None = None
    #: one-line reason for a non-ok status ("ZeroDivisionError: division by zero", ...)
    error: str | None = None
    traceback: str | None = None
    duration_s: float = 0.0
    #: the kernel was restarted, so in-memory state (variables, imports) is gone
    restarted: bool = False
    #: stdout or stderr was cut to `output_limit`
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class KernelConfig:
    #: connection files, kernel logs, IPYTHONDIR, initial working directory
    root: Path
    idle_timeout_s: float = 30 * 60
    run_timeout_s: float = 120.0
    interrupt_grace_s: float = 5.0
    startup_timeout_s: float = 60.0
    #: RLIMIT_AS per kernel on Linux; 0 disables
    memory_limit_mb: int = 4096
    #: characters kept per stream (stdout, stderr)
    output_limit: int = 32_000
    #: extra environment for every kernel
    env: Mapping[str, str] = field(default_factory=dict)
    #: size cap of kernel-<ws>.log, enforced when a kernel starts: a larger log is rotated to
    #: kernel-<ws>.log.1 (one generation kept), so disk use per workspace is bounded by about
    #: two caps plus what one kernel lifetime writes
    log_max_bytes: int = 1_000_000
    #: live kernels kept, most recently used first (a running one is never stopped); 0: no cap
    max_live: int = 2

    @classmethod
    def from_settings(cls, settings: Settings) -> KernelConfig:
        return cls(
            root=settings.data_dir / "kernels",
            idle_timeout_s=settings.kernel_idle_timeout_s,
            run_timeout_s=settings.kernel_run_timeout_s,
            memory_limit_mb=settings.kernel_memory_limit_mb,
            max_live=settings.kernel_max_live,
        )


def rotate_log(path: Path, max_bytes: int) -> None:
    """Move `path` to `path`.1 (replacing the older generation) when it exceeds `max_bytes`."""
    try:
        if path.stat().st_size > max_bytes:
            path.replace(path.with_name(path.name + ".1"))
    except FileNotFoundError:
        pass


class KernelStartError(RuntimeError):
    pass


class _Output:
    """Bounded capture of one stream: keeps the head and the tail."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.head: list[str] = []
        self.head_len = 0
        self.tail = ""
        self.dropped = 0

    def write(self, text: str) -> None:
        room = self.limit // 2 - self.head_len
        if room > 0:
            self.head.append(text[:room])
            self.head_len += min(room, len(text))
            text = text[room:]
        if not text:
            return
        self.tail += text
        keep = self.limit - self.limit // 2
        if len(self.tail) > keep:
            self.dropped += len(self.tail) - keep
            self.tail = self.tail[-keep:]

    def value(self) -> str:
        head = "".join(self.head)
        if not self.dropped:
            return head + self.tail
        return f"{head}\n... [{self.dropped} characters truncated] ...\n{self.tail}"


@dataclass
class _Capture:
    stdout: _Output
    stderr: _Output
    result: str | None = None


@dataclass
class _Kernel:
    workspace_id: str
    km: AsyncKernelManager
    kc: AsyncKernelClient
    pgid: int | None
    last_used: float


def _kill_group(pgid: int | None) -> None:
    if pgid is None:
        return
    with contextlib.suppress(OSError):
        os.killpg(pgid, signal.SIGKILL)


_live_managers: weakref.WeakSet[KernelManager] = weakref.WeakSet()


@atexit.register
def _kill_all_at_exit() -> None:
    """Last resort when the daemon exits without `aclose()` (not reached on SIGKILL)."""
    for mgr in list(_live_managers):
        mgr.kill_all_now()


class KernelManager:
    def __init__(
        self,
        config: KernelConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        python: str = sys.executable,
    ) -> None:
        self.config = config
        self._clock = clock
        self._python = python
        self._kernels: dict[str, _Kernel] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # runs started or queued per workspace: a released lock can still have a run about
        # to take it, so lock.locked() alone does not say a kernel is idle
        self._pending: dict[str, int] = {}
        self._reaper: asyncio.Task[None] | None = None
        self._closed = False

    # -- public API ---------------------------------------------------------------------------

    async def execute(
        self,
        workspace_id: str,
        code: str,
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
        timeout_s: float | None = None,
    ) -> ExecResult:
        """Run `code` in the workspace's kernel. `env` is this run's environment (e.g.
        `TN_RUN_DIR`), applied inside the kernel before the code runs; keys from the previous
        run that are not repeated are removed. `cwd` is the run's working directory (default:
        the kernel's home under `config.root`)."""
        if self._closed:
            raise RuntimeError("kernel manager is closed")
        timeout = self.config.run_timeout_s if timeout_s is None else timeout_s
        self._pending[workspace_id] = self._pending.get(workspace_id, 0) + 1
        try:
            return await self._execute(workspace_id, code, env, cwd, timeout)
        finally:
            if (n := self._pending[workspace_id] - 1) > 0:
                self._pending[workspace_id] = n
            else:
                del self._pending[workspace_id]

    async def _execute(
        self,
        workspace_id: str,
        code: str,
        env: Mapping[str, str] | None,
        cwd: str | os.PathLike[str] | None,
        timeout: float,
    ) -> ExecResult:
        async with self._lock(workspace_id):
            t0 = self._clock()
            restarted = False
            k = self._kernels.get(workspace_id)
            if k is not None and not await k.km.is_alive():
                await self._stop(workspace_id)  # died while idle (e.g. OOM-killed)
                k, restarted = None, True
            if k is None:
                try:
                    k = await self._start(workspace_id)
                except KernelStartError as e:
                    return ExecResult(
                        "crashed",
                        error=f"kernel failed to start: {e}",
                        duration_s=self._clock() - t0,
                        restarted=restarted,
                    )
                await self._evict(keep=workspace_id)
            deadline = time.monotonic() + timeout
            pre = preamble(dict(env or {}), None if cwd is None else os.fspath(cwd))
            try:
                res = await self._run(k, pre, timeout, deadline, silent=True)
                if res.status == "ok":
                    res = await self._run(k, code, timeout, deadline)
            except Exception as e:  # broken channels, closed manager: fail the run, not the caller
                log.warning("kernels: run in %s failed", workspace_id, exc_info=True)
                res = await self._crashed(k, f"{type(e).__name__}: {e}")
            if self._kernels.get(workspace_id) is k:
                k.last_used = self._clock()
            return replace(res, duration_s=self._clock() - t0, restarted=res.restarted or restarted)

    async def shutdown(self, workspace_id: str) -> None:
        """Kill the workspace's kernel (if any); the next `execute` starts a fresh one."""
        async with self._lock(workspace_id):
            await self._stop(workspace_id)

    async def reap_idle(self) -> list[str]:
        """Shut down kernels idle for `idle_timeout_s`; returns their workspace ids."""
        reaped = []
        for ws, k in list(self._kernels.items()):
            lock = self._lock(ws)
            if lock.locked() or self._clock() - k.last_used < self.config.idle_timeout_s:
                continue
            async with lock:
                if self._kernels.get(ws) is k:
                    await self._stop(ws)
                    reaped.append(ws)
        if reaped:
            log.info("kernels: shut down idle kernels for %s", ", ".join(reaped))
        return reaped

    async def _evict(self, keep: str) -> None:
        """Shut down the least recently used idle kernels beyond `max_live`."""
        cap = self.config.max_live
        if cap <= 0:
            return
        idle = sorted(
            (k for ws, k in self._kernels.items() if ws != keep), key=lambda k: k.last_used
        )
        evicted = []
        for k in idle:
            if len(self._kernels) <= cap:
                break
            ws, used = k.workspace_id, k.last_used
            if self._pending.get(ws) or self._lock(ws).locked():  # running or queued: it stays
                continue
            async with self._lock(ws):
                # used while this waited for the lock: no longer the least recently used
                if self._kernels.get(ws) is k and k.last_used == used and not self._pending.get(ws):
                    await self._stop(ws)
                    evicted.append(ws)
        if evicted:
            log.info("kernels: over %d live kernels; shut down %s", cap, ", ".join(evicted))

    def kernel_pid(self, workspace_id: str) -> int | None:
        k = self._kernels.get(workspace_id)
        return None if k is None else k.km.provisioner.pid  # type: ignore[union-attr]

    @property
    def workspaces(self) -> list[str]:
        return list(self._kernels)

    async def aclose(self) -> None:
        """Kill every kernel. Idempotent; called from the daemon's shutdown path."""
        self._closed = True
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        await asyncio.gather(*(self._stop(ws) for ws in list(self._kernels)))
        _live_managers.discard(self)

    def kill_all_now(self) -> None:
        """Synchronous SIGKILL of every kernel process group (atexit / emergency path)."""
        for k in list(self._kernels.values()):
            _kill_group(k.pgid)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- kernel lifecycle ---------------------------------------------------------------------

    def _lock(self, workspace_id: str) -> asyncio.Lock:
        lock = self._locks.get(workspace_id)
        if lock is None:
            lock = self._locks[workspace_id] = asyncio.Lock()
        return lock

    def _kernel_env(self, workspace_id: str) -> dict[str, str]:
        # Daemon settings (TN_*) are not the kernel's business; the run sets its own.
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TN_", "JPY_"))}
        env.update(
            IPYTHONDIR=str(self.config.root / "ipython"),
            MPLBACKEND="Agg",
            PYTHONUNBUFFERED="1",
            TN_WORKSPACE=workspace_id,
        )
        env.update(self.config.env)
        return env

    def _argv(self) -> list[str]:
        return [
            self._python,
            "-m",
            "telemetry_nerd.kernels.launch",
            "--parent-pid",
            str(os.getpid()),
            "--memory-mb",
            str(self.config.memory_limit_mb),
            "--",
            "-f",
            "{connection_file}",
            "--HistoryManager.enabled=False",
            "--IPKernelApp.log_level=WARN",
        ]

    async def _start(self, workspace_id: str) -> _Kernel:
        from jupyter_client.kernelspec import KernelSpec
        from jupyter_client.manager import AsyncKernelManager

        name = _SAFE.sub("_", workspace_id)
        home = self.config.root / "home" / name
        home.mkdir(parents=True, exist_ok=True)
        (self.config.root / "ipython").mkdir(parents=True, exist_ok=True)
        conn = self.config.root / f"kernel-{name}-{uuid.uuid4().hex[:8]}.json"
        spec = KernelSpec(
            argv=self._argv(),
            display_name="telemetry-nerd",
            language="python",
            interrupt_mode="signal",
        )
        km = AsyncKernelManager(
            kernel_name="telemetry-nerd",
            kernel_spec_manager=_fixed_spec_manager(spec),
            connection_file=str(conn),
        )
        log_path = self.config.root / f"kernel-{name}.log"
        rotate_log(log_path, self.config.log_max_bytes)
        logf = open(log_path, "ab")  # noqa: ASYNC230, SIM115
        try:
            await km.start_kernel(
                env=self._kernel_env(workspace_id), cwd=str(home), stdout=logf, stderr=logf
            )
        except Exception as e:
            raise KernelStartError(str(e)) from e
        finally:
            logf.close()  # the child holds its own descriptor
        kc = km.client()
        kc.start_channels()
        try:
            await kc.wait_for_ready(timeout=self.config.startup_timeout_s)
        except (RuntimeError, TimeoutError) as e:
            kc.stop_channels()
            with contextlib.suppress(Exception):
                await km.shutdown_kernel(now=True)
            raise KernelStartError(f"{e} (see {self.config.root}/kernel-{name}.log)") from e
        k = _Kernel(workspace_id, km, kc, km.provisioner.pgid, self._clock())  # type: ignore[union-attr]
        self._kernels[workspace_id] = k
        _live_managers.add(self)
        self._ensure_reaper()
        log.info("kernels: started kernel pid %s for %s", km.provisioner.pid, workspace_id)  # type: ignore[union-attr]
        return k

    async def _stop(self, workspace_id: str) -> None:
        k = self._kernels.pop(workspace_id, None)
        if k is None:
            return
        k.kc.stop_channels()
        try:
            await k.km.shutdown_kernel(now=True)  # SIGKILL to the process group
        except Exception:
            log.warning("kernels: shutdown of %s failed; killing", workspace_id, exc_info=True)
        _kill_group(k.pgid)  # anything left in the group (user subprocesses)
        with contextlib.suppress(OSError):
            Path(k.km.connection_file).unlink(missing_ok=True)

    async def _restart(self, k: _Kernel) -> bool:
        await self._stop(k.workspace_id)
        if self._closed:
            return False
        try:
            await self._start(k.workspace_id)
        except KernelStartError:
            log.warning("kernels: restart of %s failed", k.workspace_id, exc_info=True)
            return False
        return True

    def _ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.get_running_loop().create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        interval = min(60.0, max(0.05, self.config.idle_timeout_s / 4))
        while True:
            await asyncio.sleep(interval)
            try:
                await self.reap_idle()
            except Exception:
                log.warning("kernels: idle reaper failed", exc_info=True)

    # -- one execute request ------------------------------------------------------------------

    async def _run(
        self, k: _Kernel, code: str, timeout: float, deadline: float, *, silent: bool = False
    ) -> ExecResult:
        cap = _Capture(_Output(self.config.output_limit), _Output(self.config.output_limit))
        msg_id = k.kc.execute(
            code, silent=silent, store_history=False, allow_stdin=False, stop_on_error=True
        )
        work = asyncio.ensure_future(self._collect(k, msg_id, cap))
        death = asyncio.ensure_future(self._wait_dead(k))
        try:
            await asyncio.wait(
                {work, death},
                timeout=max(0.0, deadline - time.monotonic()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if (reply := _reply(work)) is not None:
                return _from_reply(reply, cap)
            if work.done() or death.done():  # the kernel died, or its channels broke
                return _with_output(await self._crashed(k), cap)
            # Timed out: interrupt, give the cell a grace period, else kill + restart.
            timeout_msg = f"timed out after {timeout:g}s"
            with contextlib.suppress(Exception):
                await k.km.interrupt_kernel()
            await asyncio.wait(
                {work, death},
                timeout=self.config.interrupt_grace_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if (reply := _reply(work)) is not None:
                res = _from_reply(reply, cap)
                return replace(res, status="timeout", error=f"{timeout_msg}; interrupted")
            restarted = await self._restart(k)
            return _with_output(
                ExecResult(
                    "timeout",
                    error=f"{timeout_msg}; did not respond to interrupt, kernel "
                    + ("restarted" if restarted else "killed"),
                    restarted=restarted,
                ),
                cap,
            )
        finally:
            for t in (work, death):
                t.cancel()
            await asyncio.gather(work, death, return_exceptions=True)

    async def _crashed(self, k: _Kernel, reason: str | None = None) -> ExecResult:
        if reason is None:
            proc = k.km.provisioner.process if k.km.provisioner else None  # type: ignore[attr-defined]
            reason = _exit_reason(None if proc is None else proc.poll())
        restarted = await self._restart(k)
        return ExecResult("crashed", error=f"kernel died ({reason})", restarted=restarted)

    async def _collect(self, k: _Kernel, msg_id: str, cap: _Capture) -> dict[str, Any]:
        async def iopub() -> None:
            while True:
                msg = await k.kc.get_iopub_msg()
                if msg.get("parent_header", {}).get("msg_id") != msg_id:
                    continue
                kind, content = msg["msg_type"], msg["content"]
                if kind == "stream":
                    out = cap.stderr if content.get("name") == "stderr" else cap.stdout
                    out.write(content.get("text", ""))
                elif kind == "execute_result":
                    cap.result = content.get("data", {}).get("text/plain")
                elif kind == "display_data":
                    text = content.get("data", {}).get("text/plain")
                    if text:
                        cap.stdout.write(text + "\n")
                elif kind == "status" and content.get("execution_state") == "idle":
                    return

        async def shell() -> dict[str, Any]:
            while True:
                msg = await k.kc.get_shell_msg()
                if (
                    msg.get("parent_header", {}).get("msg_id") == msg_id
                    and msg["msg_type"] == "execute_reply"
                ):
                    return msg["content"]

        _, reply = await asyncio.gather(iopub(), shell())
        return reply

    async def _wait_dead(self, k: _Kernel) -> None:
        while await k.km.is_alive():
            await asyncio.sleep(_LIVENESS_POLL_S)


def _fixed_spec_manager(spec: Any) -> Any:
    """A jupyter_client KernelSpecManager that always returns our launcher's spec."""
    from jupyter_client.kernelspec import KernelSpecManager

    class _FixedSpecManager(KernelSpecManager):
        def get_kernel_spec(self, kernel_name: str, *a: Any, **kw: Any) -> Any:
            return spec

    return _FixedSpecManager()


def _reply(work: asyncio.Future[dict[str, Any]]) -> dict[str, Any] | None:
    """The execute_reply if the run finished cleanly (not if the channels failed)."""
    if not work.done() or work.cancelled() or work.exception() is not None:
        return None
    return work.result()


def _with_output(res: ExecResult, cap: _Capture) -> ExecResult:
    return replace(
        res,
        stdout=cap.stdout.value(),
        stderr=cap.stderr.value(),
        result=cap.result,
        truncated=bool(cap.stdout.dropped or cap.stderr.dropped),
    )


def _from_reply(reply: dict[str, Any], cap: _Capture) -> ExecResult:
    if reply.get("status") == "ok":
        return _with_output(ExecResult("ok"), cap)
    ename, evalue = reply.get("ename", "Error"), reply.get("evalue", "")
    tb = "\n".join(_ANSI.sub("", line) for line in reply.get("traceback") or [])
    return _with_output(
        ExecResult("error", error=f"{ename}: {evalue}" if evalue else ename, traceback=tb or None),
        cap,
    )


def _exit_reason(returncode: int | None) -> str:
    if returncode is None:
        return "exit status unknown"
    if returncode < 0:
        try:
            return f"killed by {signal.Signals(-returncode).name}"
        except ValueError:
            return f"killed by signal {-returncode}"
    return f"exit code {returncode}"
