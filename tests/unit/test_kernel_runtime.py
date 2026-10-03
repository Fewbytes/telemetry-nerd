"""The kernel preamble binds tn, pl and np for every run (1w7), without a kernel."""

from telemetry_nerd.kernels import runtime


def test_preamble_binds_tn_pl_np_unless_rebound(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "_applied", set())
    monkeypatch.chdir(tmp_path)
    ns: dict = {"np": "mine"}
    try:
        exec(runtime.preamble({}, str(tmp_path)), ns)  # noqa: S102
    finally:
        runtime.begin_run('{"env": {}, "cwd": null}')
    assert ns["tn"].__name__ == "telemetry_nerd.tn"
    assert ns["pl"].__name__ == "polars"
    assert ns["np"] == "mine"  # the code's own binding wins
    assert "_tn_runtime" not in ns


def test_provide_skips_modules_that_do_not_import(monkeypatch):
    monkeypatch.setattr(runtime, "PROVIDED", {"nope": "no_such_module_xyz", "np": "numpy"})
    ns: dict = {}
    runtime.provide(ns)
    assert "nope" not in ns and ns["np"].__name__ == "numpy"
