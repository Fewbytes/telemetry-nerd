"""Kernel manager (b98.3): real IPython kernel subprocesses, so these are marked slow.

No fixed sleeps: waits poll a condition against a deadline.
"""

from __future__ import annotations

import ast
import asyncio
import os
import signal
import subprocess
import sys
import time

import pytest
import pytest_asyncio
from starlette.testclient import TestClient

from telemetry_nerd.api.app import create_app
from telemetry_nerd.config import Settings
from telemetry_nerd.kernels import runtime
from telemetry_nerd.kernels.launch import set_memory_limit
from telemetry_nerd.kernels.manager import KernelConfig, KernelManager, _Output
from tests.unit.fakes import make_service

pytestmark = pytest.mark.slow

LINUX = sys.platform.startswith("linux")


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    # A zombie (dead, not yet reaped) still answers kill(0); ask ps for its state.
    out = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    return out.stdout.strip().startswith("Z") or out.returncode != 0


def _wait_gone(pid: int, within_s: float = 10.0) -> bool:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        if _gone(pid):
            return True
        time.sleep(0.05)
    return False


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def mgr(tmp_path_factory):
    m = KernelManager(KernelConfig(root=tmp_path_factory.mktemp("kernels"), interrupt_grace_s=1))
    yield m
    await m.aclose()


module_loop = pytest.mark.asyncio(loop_scope="module")


@module_loop
async def test_state_persists_and_output_is_captured(mgr):
    r = await mgr.execute("persist", "x = 41\nprint('hello')\nx + 1")
    assert (r.status, r.stdout, r.result) == ("ok", "hello\n", "42")
    r = await mgr.execute("persist", "import sys; print('err', file=sys.stderr); x")
    assert (r.status, r.stderr, r.result, r.restarted) == ("ok", "err\n", "41", False)


@module_loop
async def test_run_env_and_cwd_are_applied_per_run(mgr, tmp_path):
    code = "import os; (os.environ.get('TN_RUN_DIR'), os.environ.get('TN_WORKSPACE'), os.getcwd())"
    r = await mgr.execute("env", code, env={"TN_RUN_DIR": str(tmp_path)}, cwd=tmp_path)
    assert r.ok
    assert ast.literal_eval(r.result) == (str(tmp_path), "env", os.path.realpath(tmp_path))
    r = await mgr.execute("env", code)  # next run: previous run's keys are removed
    run_dir, ws, cwd = ast.literal_eval(r.result)
    assert run_dir is None and ws == "env" and cwd != os.path.realpath(tmp_path)


@module_loop
async def test_on_run_hooks_fire_before_each_run(mgr):
    setup = (
        "from telemetry_nerd.kernels.runtime import on_run\n"
        "import os\nseen = []\n"
        "on_run(lambda: seen.append(os.environ.get('TN_RUN_DIR')))"
    )
    assert (await mgr.execute("hooks", setup)).ok
    await mgr.execute("hooks", "pass", env={"TN_RUN_DIR": "/r/1"})
    r = await mgr.execute("hooks", "seen", env={"TN_RUN_DIR": "/r/2"})
    assert r.result == "['/r/1', '/r/2']"


@module_loop
async def test_error_returns_traceback_and_keeps_kernel(mgr):
    await mgr.execute("err", "y = 7")
    r = await mgr.execute("err", "def f():\n    return 1 / 0\nf()")
    assert r.status == "error" and r.error == "ZeroDivisionError: division by zero"
    assert "\x1b[" not in r.traceback and "return 1 / 0" in r.traceback
    assert (await mgr.execute("err", "y")).result == "7"


@module_loop
async def test_infinite_loop_is_interrupted_at_timeout(mgr):
    await mgr.execute("loop", "z = 3")
    r = await mgr.execute("loop", "print('start')\nwhile True: pass", timeout_s=0.5)
    assert (r.status, r.restarted, r.stdout) == ("timeout", False, "start\n")
    assert "KeyboardInterrupt" in r.traceback
    assert (await mgr.execute("loop", "z")).result == "3"  # interrupt kept the state


@module_loop
async def test_unresponsive_cell_is_killed_and_kernel_restarted(mgr):
    await mgr.execute("stuck", "w = 1")
    pid = mgr.kernel_pid("stuck")
    code = (
        "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "while True: time.sleep(0.01)"
    )
    r = await mgr.execute("stuck", code, timeout_s=0.3)
    assert (r.status, r.restarted) == ("timeout", True)
    assert _wait_gone(pid)
    assert mgr.kernel_pid("stuck") not in (None, pid)
    assert (await mgr.execute("stuck", "w")).error == "NameError: name 'w' is not defined"


@module_loop
@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("import os; os._exit(3)", "exit code 3"),
        ("import ctypes; ctypes.string_at(0)", "SIGSEGV"),
    ],
)
async def test_hard_crash_fails_run_and_restarts_kernel(mgr, code, reason):
    ws = f"crash-{reason[-2:]}"
    await mgr.execute(ws, "v = 1")
    pid = mgr.kernel_pid(ws)
    r = await mgr.execute(ws, code)
    assert (r.status, r.restarted) == ("crashed", True)
    assert reason in r.error or (reason == "SIGSEGV" and "SIGBUS" in r.error)
    assert mgr.kernel_pid(ws) != pid
    assert (await mgr.execute(ws, "1 + 1")).result == "2"


@module_loop
async def test_kernel_that_died_while_idle_is_replaced(mgr):
    await mgr.execute("idle-death", "1")
    pid = mgr.kernel_pid("idle-death")
    os.kill(pid, signal.SIGKILL)
    assert _wait_gone(pid)
    r = await mgr.execute("idle-death", "2")
    assert (r.status, r.result, r.restarted) == ("ok", "2", True)


@module_loop
async def test_output_is_bounded(mgr):
    r = await mgr.execute("big", "print('a' * 200_000)\nprint('END')")
    assert r.ok and r.truncated
    assert len(r.stdout) < mgr.config.output_limit + 100
    assert r.stdout.startswith("aaa") and r.stdout.endswith("END\n")


@module_loop
async def test_concurrent_runs_on_one_kernel_are_queued(mgr):
    await mgr.execute("queue", "n = 0")
    rs = await asyncio.gather(
        *(mgr.execute("queue", "import time; time.sleep(0.02); n += 1; n") for _ in range(5))
    )
    assert sorted(int(r.result) for r in rs) == [1, 2, 3, 4, 5]


@module_loop
async def test_workspaces_get_separate_kernels(mgr):
    await mgr.execute("sep-a", "q = 'a'")
    r = await mgr.execute("sep-b", "q")
    assert r.status == "error" and mgr.kernel_pid("sep-a") != mgr.kernel_pid("sep-b")


async def test_idle_kernels_are_reaped(tmp_path):
    now = [0.0]
    async with KernelManager(
        KernelConfig(root=tmp_path, idle_timeout_s=1e6), clock=lambda: now[0]
    ) as m:
        await m.execute("a", "1")
        pid = m.kernel_pid("a")
        assert await m.reap_idle() == []
        now[0] += 2e6
        assert await m.reap_idle() == ["a"]
        assert m.workspaces == [] and _wait_gone(pid)
        assert (await m.execute("a", "3")).result == "3"  # lazily started again


async def test_live_kernels_are_capped_least_recently_used_first(tmp_path):
    now = [0.0]
    async with KernelManager(KernelConfig(root=tmp_path, max_live=2), clock=lambda: now[0]) as m:
        for ws in ("a", "b"):
            now[0] += 1
            await m.execute(ws, "1")
        now[0] += 1
        await m.execute("a", "x = 1")  # a is now the most recently used
        pid_b = m.kernel_pid("b")
        now[0] += 1
        await m.execute("c", "1")
        assert sorted(m.workspaces) == ["a", "c"] and _wait_gone(pid_b)
        assert (await m.execute("a", "x")).result == "1"  # the survivor kept its state


async def test_eviction_skips_a_kernel_with_a_queued_run(tmp_path, monkeypatch):
    """Between a run releasing the lock and the next queued run waking, the lock reads as
    free: eviction must still see the queued run and leave that kernel alone."""
    from types import SimpleNamespace

    from telemetry_nerd.kernels.manager import ExecResult, _Kernel

    m = KernelManager(KernelConfig(root=tmp_path, max_live=2))

    async def alive():
        return True

    async def run(k, code, timeout, deadline, silent=False):
        return ExecResult("ok")

    async def stop(ws):
        m._kernels.pop(ws, None)

    monkeypatch.setattr(m, "_run", run)
    monkeypatch.setattr(m, "_stop", stop)
    for i, ws in enumerate(("a", "b", "c")):
        m._kernels[ws] = _Kernel(ws, SimpleNamespace(is_alive=alive), None, None, float(i))
    lock = m._lock("a")
    await lock.acquire()  # a's current run
    queued = asyncio.ensure_future(m.execute("a", "1"))  # a's next run waits for the lock
    for _ in range(3):
        await asyncio.sleep(0)
    lock.release()  # locked() is False now, but the queued run has not woken yet
    await m._evict(keep="c")
    assert (await queued).ok
    assert sorted(m._kernels) == ["a", "c"]  # the idle b went, not a with its pending run


async def test_aclose_kills_kernels_and_their_subprocesses(tmp_path):
    m = KernelManager(KernelConfig(root=tmp_path))
    r = await m.execute(
        "a", "import subprocess, sys\np = subprocess.Popen(['sleep', '600'])\np.pid"
    )
    child, pid = int(r.result), m.kernel_pid("a")
    await m.aclose()
    assert _wait_gone(pid) and _wait_gone(child)
    assert not list(tmp_path.glob("kernel-*.json"))  # connection files cleaned up
    with pytest.raises(RuntimeError):
        await m.execute("a", "1")


async def test_aclose_during_a_run_fails_it_without_restarting(tmp_path):
    m = KernelManager(KernelConfig(root=tmp_path))
    await m.execute("a", "1")
    pid = m.kernel_pid("a")
    run = asyncio.ensure_future(m.execute("a", "while True: pass", timeout_s=60))
    await asyncio.sleep(0)  # let the run start (no timing dependence: either order is valid)
    await m.aclose()
    r = await run
    assert r.status == "crashed" and not r.restarted
    assert m.workspaces == [] and _wait_gone(pid)


_DAEMON = """
import asyncio, sys
from pathlib import Path
from telemetry_nerd.kernels.manager import KernelConfig, KernelManager

async def main():
    m = KernelManager(KernelConfig(root=Path(sys.argv[1])))
    r = await m.execute("w", "import subprocess; subprocess.Popen(['sleep', '600']).pid")
    print(m.kernel_pid("w"), r.result, flush=True)
    await asyncio.Event().wait()

asyncio.run(main())
"""


def test_no_orphans_when_daemon_is_sigkilled(tmp_path):
    daemon = subprocess.Popen(
        [sys.executable, "-c", _DAEMON, str(tmp_path)], stdout=subprocess.PIPE, text=True
    )
    try:
        kernel, child = map(int, daemon.stdout.readline().split())
    finally:
        daemon.kill()
        daemon.wait()
    assert _wait_gone(kernel), "kernel outlived its SIGKILLed daemon"
    assert _wait_gone(child), "kernel's subprocess outlived the daemon"


async def test_kernel_start_failure_is_a_crashed_result(tmp_path):
    async with KernelManager(KernelConfig(root=tmp_path), python="/nonexistent/python") as m:
        r = await m.execute("a", "1")
    assert r.status == "crashed" and r.error.startswith("kernel failed to start")


@pytest.mark.skipif(not LINUX, reason="RLIMIT_AS is only enforced on Linux")
async def test_memory_bomb_is_limited_on_linux(tmp_path):
    async with KernelManager(KernelConfig(root=tmp_path, memory_limit_mb=1024)) as m:
        r = await m.execute("a", "b = bytearray(4 * 1024**3)")
        assert r.status == "error" and r.error.startswith("MemoryError")
        assert (await m.execute("a", "1")).ok  # the kernel survives


def test_memory_limit_is_skipped_off_linux():
    assert set_memory_limit(0) is False
    if not LINUX:
        assert set_memory_limit(1024) is False


def test_output_keeps_head_and_tail():
    out = _Output(10)
    for chunk in ["abc", "defgh", "ijklmnop", "XYZ"]:
        out.write(chunk)
    assert out.value() == "abcde\n... [9 characters truncated] ...\nopXYZ"


def test_preamble_round_trips_env_and_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "_applied", set())
    monkeypatch.chdir(tmp_path)
    ns: dict = {}
    exec(runtime.preamble({"TN_RUN_DIR": "/x'y\"z"}, str(tmp_path)), ns)  # noqa: S102
    try:
        assert os.environ["TN_RUN_DIR"] == "/x'y\"z" and "_tn_runtime" not in ns
    finally:
        runtime.begin_run('{"env": {}, "cwd": null}')
    assert "TN_RUN_DIR" not in os.environ


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("TN_KERNEL_IDLE_TIMEOUT_S", "90")
    monkeypatch.setenv("TN_KERNEL_MEMORY_LIMIT_MB", "0")
    monkeypatch.setenv("TN_KERNEL_MAX_LIVE", "3")
    s = Settings.from_env()
    cfg = KernelConfig.from_settings(s)
    assert (cfg.idle_timeout_s, cfg.memory_limit_mb, cfg.root) == (90, 0, s.data_dir / "kernels")
    assert cfg.max_live == 3 and KernelConfig(root=s.data_dir).max_live == 2


def test_app_shutdown_closes_kernels(tmp_path):
    closed = []

    class _Kernels:
        async def aclose(self):
            closed.append(True)

    service = make_service(tmp_path)
    service.kernels = _Kernels()
    with TestClient(create_app(service, allowed_hosts=["testserver"])):
        assert closed == []
    assert closed == [True]


def test_rotate_log_caps_kernel_log(tmp_path):
    from telemetry_nerd.kernels.manager import rotate_log

    log = tmp_path / "kernel-a.log"
    rotate_log(log, 10)  # absent: nothing to do
    log.write_bytes(b"x" * 10)
    rotate_log(log, 10)  # at the cap: kept
    assert log.read_bytes() == b"x" * 10 and not (tmp_path / "kernel-a.log.1").exists()
    log.write_bytes(b"y" * 11)
    rotate_log(log, 10)
    assert not log.exists() and (tmp_path / "kernel-a.log.1").read_bytes() == b"y" * 11
    log.write_bytes(b"z" * 20)
    rotate_log(log, 10)  # one generation only: the older one is replaced
    assert (tmp_path / "kernel-a.log.1").read_bytes() == b"z" * 20


async def test_kernel_start_rotates_oversized_log(tmp_path):
    (tmp_path / "kernel-a.log").write_bytes(b"old" * 100)
    async with KernelManager(KernelConfig(root=tmp_path, log_max_bytes=10)) as m:
        assert (await m.execute("a", "1")).result == "1"
    assert (tmp_path / "kernel-a.log.1").read_bytes() == b"old" * 100
    assert (tmp_path / "kernel-a.log").stat().st_size < 300
