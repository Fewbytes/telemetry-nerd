"""Tier-2 code execution: one IPython kernel subprocess per workspace (spec §5.2).

`manager` is the daemon side (`KernelManager.execute`), `launch` is the kernel process entry
point (limits + parent-death handling, then ipykernel) and `runtime` is imported inside the
kernel to apply per-run state. Kept import-free so the kernel side stays light.
"""
