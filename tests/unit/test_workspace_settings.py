from telemetry_nerd.workspace.db import open_workspace_db
from telemetry_nerd.workspace.store import WorkspaceStore


def test_get_setting_returns_default_when_unset(tmp_path):
    store = WorkspaceStore(open_workspace_db(tmp_path / "workspace.db"))
    assert store.get_setting("default_range") is None
    assert store.get_setting("default_range", "now-1h") == "now-1h"


def test_set_setting_then_get_returns_the_new_value(tmp_path):
    store = WorkspaceStore(open_workspace_db(tmp_path / "workspace.db"))
    store.set_setting("default_range", "now-3h")
    assert store.get_setting("default_range") == "now-3h"


def test_set_setting_overwrites_an_existing_value(tmp_path):
    store = WorkspaceStore(open_workspace_db(tmp_path / "workspace.db"))
    store.set_setting("default_range", "now-3h")
    store.set_setting("default_range", "now-6h")
    assert store.get_setting("default_range") == "now-6h"
