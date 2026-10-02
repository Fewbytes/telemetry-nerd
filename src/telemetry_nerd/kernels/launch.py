"""Kernel subprocess entry point: resource limits and parent-death handling, then ipykernel.

    python -m telemetry_nerd.kernels.launch --parent-pid PID [--memory-mb N] -- <ipykernel args>

Runs before ipykernel is imported and must stay stdlib-only (no daemon packages).

Parent death (the daemon is SIGKILLed or crashes, so it cannot shut kernels down):
- Linux: PR_SET_PDEATHSIG=SIGKILL makes the OS kill the kernel the moment the daemon dies.
- All POSIX: a forked watchdog process polls the daemon pid and SIGKILLs the kernel's process
  group (kernel + anything user code spawned) when it is gone. It is a separate process, so a
  kernel stuck in C code holding the GIL is still killed.
- ipykernel's own parent poller (JPY_PARENT_PID) is a third, in-kernel line of defence.
"""

from __future__ import annotations

import argparse
import faulthandler
import os
import signal
import sys
import time

WATCHDOG_POLL_S = 0.5


def _parse(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    p = argparse.ArgumentParser(prog="telemetry_nerd.kernels.launch")
    p.add_argument("--parent-pid", type=int, required=True)
    p.add_argument("--memory-mb", type=int, default=0)
    if "--" in argv:
        i = argv.index("--")
        return p.parse_args(argv[:i]), argv[i + 1 :]
    return p.parse_known_args(argv)


def set_memory_limit(memory_mb: int) -> bool:
    """Cap the address space (RLIMIT_AS) on Linux. macOS does not enforce RLIMIT_AS, so it is
    left alone there. Returns whether a limit was applied."""
    if memory_mb <= 0 or not sys.platform.startswith("linux"):
        return False
    import resource

    limit = memory_mb * 1024 * 1024
    _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    if hard != resource.RLIM_INFINITY:
        limit = min(limit, hard)
    resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
    return True


def _set_pdeathsig() -> None:
    if not sys.platform.startswith("linux"):
        return
    import ctypes

    pr_set_pdeathsig = 1
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(pr_set_pdeathsig, int(signal.SIGKILL), 0, 0, 0)
    except (OSError, AttributeError):
        pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _watchdog(parent_pid: int) -> None:
    """Child of the kernel, in its process group: when the daemon is gone, SIGKILL the group.

    It never exits on its own while the daemon lives: the daemon stops a kernel by killing its
    whole group, which takes the watchdog with it. (Exiting when the kernel dies would leave
    the kernel's own subprocesses behind on Linux, where PDEATHSIG kills only the kernel.)
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # interrupts go to the whole process group
    try:
        os.closerange(3, 1024)
    except OSError:
        pass
    while _alive(parent_pid):
        time.sleep(WATCHDOG_POLL_S)
    try:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    finally:
        os._exit(0)


def main(argv: list[str] | None = None) -> None:
    args, rest = _parse(sys.argv[1:] if argv is None else argv)
    _set_pdeathsig()
    if os.getppid() != args.parent_pid:
        os._exit(1)  # the daemon died before we got here
    if hasattr(os, "fork") and os.fork() == 0:  # no threads exist yet, so forking is safe
        _watchdog(args.parent_pid)
    set_memory_limit(args.memory_mb)
    faulthandler.enable()  # a native crash leaves a Python traceback in the kernel log
    sys.argv = ["ipykernel_launcher", *rest]
    from ipykernel.kernelapp import launch_new_instance

    launch_new_instance()


if __name__ == "__main__":
    main()
