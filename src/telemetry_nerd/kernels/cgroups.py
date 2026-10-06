"""cgroup v2 memory limiting for a kernel subprocess: defense in depth alongside RLIMIT_AS
(see `kernels.launch.set_memory_limit`). Stdlib only -- imported by `launch` before ipykernel.

Linux only. A cgroup v2 `memory.max` breach SIGKILLs every process in the cgroup; there is no
catchable exception (verified: a write that crosses `memory.max` is charged at page-fault time
and the kernel's OOM killer fires for the cgroup, not a syscall-time `ENOMEM`). That is *not*
what the "memory bomb" tests want (a clean, recoverable `MemoryError` so the workspace's kernel
survives one oversized allocation) -- RLIMIT_AS gives that, because it is checked synchronously
at `mmap()` time, strictly before a cgroup could ever charge/kill for the same allocation.

Cgroups earn their place anyway, as a harder-to-evade backstop `launch.set_memory_limit` layers
underneath RLIMIT_AS at the same budget:
- RLIMIT_AS is a per-process rlimit. A kernel whose user code forks N subprocesses gives each
  of them its own independent budget; a cgroup containing the kernel and everything it forks
  caps their combined memory.
- RLIMIT_AS counts reserved virtual address space, which shared/memory-mapped pages can distort
  (inflating or failing to reflect real usage). A cgroup's `memory.max` tracks real charged
  memory regardless of how it was mapped.

So: RLIMIT_AS catches the common case (one allocation that is obviously too big) cleanly and
first; the cgroup is a hard ceiling for whatever RLIMIT_AS misses, where an OOM kill
(`status="crashed"`, kernel restarted) is the correct, honest outcome.

Delegation, verified on a real Linux host (bd telemetry-nerd-36jm): cgroup v2 refuses to enable
a controller in a cgroup's `cgroup.subtree_control` while that *same* cgroup still has live
member processes directly in it ("no internal process constraint") -- including the process
doing the enabling. A freshly spawned daemon is, by default, a lone direct member of whatever
cgroup its own parent put it in, so it cannot delegate to children without first moving itself
out of the way. `ensure_kernels_base` does that once, early in the daemon's life: it relocates
the daemon into a `daemon` leaf, which empties its original cgroup so a `kernels` sibling can
be delegated the memory controller. Each kernel subprocess (inheriting the daemon's `daemon`
cgroup at spawn) then just creates its own leaf under that already-delegated `kernels` and
moves itself there (`kernel_base_from_inside` + `create` + `join_self`) -- no further
subtree_control juggling needed, since `kernels` itself never holds a direct member.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path

_CGROOT = Path("/sys/fs/cgroup")
_PREFIX = "tn-kernel-"
_UNSET = object()
_kernels_base: Path | None | object = _UNSET


def own_base_dir() -> Path | None:
    """The cgroup v2 directory this process currently lives in, or None if cgroup v2 with a
    memory controller isn't even mounted (or this isn't Linux). Living there is a candidate for
    delegation, not a guarantee: whether we can actually subdivide it (permissions, a read-only
    /sys/fs/cgroup in some containers, siblings already there) is only proven by actually doing
    it -- any failure along the way just means `launch.set_memory_limit` falls back to
    RLIMIT_AS alone."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        if "memory" not in (_CGROOT / "cgroup.controllers").read_text().split():
            return None
        rel = _own_relative_path()
    except OSError:
        return None
    if not rel:
        return None
    return _CGROOT if rel == "/" else _CGROOT / rel.lstrip("/")


def _own_relative_path() -> str | None:
    """This process's cgroup v2 path, from /proc/self/cgroup ("0::/some/path" for the unified
    hierarchy; other lines are cgroup v1 controllers we don't use)."""
    try:
        lines = Path("/proc/self/cgroup").read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        hid, _, rest = line.partition(":")
        if hid == "0":
            return rest.partition(":")[2]
    return None


def _has_memory(subtree_control: Path) -> bool:
    try:
        return "memory" in subtree_control.read_text().split()
    except OSError:
        return False


def _enable_memory(subtree_control: Path) -> bool:
    if _has_memory(subtree_control):
        return True
    try:
        subtree_control.write_text("+memory")
    except OSError:
        return False
    return _has_memory(subtree_control)


def ensure_kernels_base() -> Path | None:
    """Daemon-side; call once per daemon process (cached for its lifetime, idempotent). Makes a
    `kernels` cgroup available for kernel subprocesses to subdivide further, relocating this
    process into a `daemon` leaf first if that is what it takes to delegate (see module
    docstring). Returns the `kernels` directory, or None if cgroup v2 isn't usable here."""
    global _kernels_base
    if _kernels_base is _UNSET:
        _kernels_base = _ensure_kernels_base_once()
    return _kernels_base  # type: ignore[return-value]


def _ensure_kernels_base_once() -> Path | None:
    base = own_base_dir()
    if base is None:
        return None
    kernels = base / "kernels"
    if base.name == "daemon" and _has_memory(kernels / "cgroup.subtree_control"):
        return kernels  # an earlier call in this process already set this up
    daemon = base / "daemon"
    try:
        daemon.mkdir(exist_ok=True)
        (daemon / "cgroup.procs").write_text(str(os.getpid()))  # empties `base` of members
        if not _enable_memory(base / "cgroup.subtree_control"):
            raise OSError("memory controller not delegated to us")
        kernels.mkdir(exist_ok=True)
        if not (kernels / "memory.max").exists() or not _enable_memory(
            kernels / "cgroup.subtree_control"
        ):
            raise OSError("memory controller not delegated to the kernels subtree")
    except OSError:
        with contextlib.suppress(OSError):
            kernels.rmdir()
        return None
    return kernels


def kernel_base_from_inside() -> Path | None:
    """Kernel-side (`launch.py`): the `kernels` directory our parent (the daemon, after it ran
    `ensure_kernels_base`) delegated the memory controller to, inferred from our own inherited
    cgroup -- a freshly spawned kernel is still in the daemon's `daemon` leaf at this point, so
    `kernels` is the sibling the daemon already set up. None if the daemon never did that
    (cgroup v2 unusable there) or we are not actually in a `daemon` leaf."""
    base = own_base_dir()
    if base is None or base.name != "daemon":
        return None
    kernels = base.parent / "kernels"
    return kernels if _has_memory(kernels / "cgroup.subtree_control") else None


def kernel_dir(base: Path, kernel_id: str) -> Path:
    """The cgroup directory `create` would use (or did use) for `kernel_id` under `base`."""
    return base / f"{_PREFIX}{kernel_id}"


def create(base: Path, kernel_id: str, memory_mb: int) -> Path | None:
    """Create and configure `base`/tn-kernel-<kernel_id>, where `base` already has the memory
    controller delegated to it (e.g. from `kernel_base_from_inside`). Returns the directory on
    success, or None (cleaning up any partial directory) on any failure -- e.g. no permission,
    or `base` turning out not to be delegated after all."""
    child = kernel_dir(base, kernel_id)
    try:
        child.mkdir(exist_ok=True)
        memmax = child / "memory.max"
        if not memmax.exists():  # controller isn't actually delegated here
            raise OSError("cgroup v2 memory controller not delegated")
        memmax.write_text(str(memory_mb * 1024 * 1024))
    except OSError:
        with contextlib.suppress(OSError):
            child.rmdir()
        return None
    return child


def join_self(cgroup_dir: Path) -> bool:
    """Move this process into `cgroup_dir`."""
    try:
        (cgroup_dir / "cgroup.procs").write_text(str(os.getpid()))
    except OSError:
        return False
    return True


def remove(cgroup_dir: Path, *, retries: int = 10, delay_s: float = 0.02) -> None:
    """Best-effort cleanup once every process that was in `cgroup_dir` has exited. A cgroup can
    only be removed when empty; the kernel's own exit/kill races slightly with this, hence the
    short retry -- it is not a correctness issue if this gives up (an orphaned empty directory
    costs nothing but a little cgroupfs bookkeeping)."""
    for _ in range(retries):
        try:
            cgroup_dir.rmdir()
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(delay_s)
    with contextlib.suppress(OSError):
        cgroup_dir.rmdir()
