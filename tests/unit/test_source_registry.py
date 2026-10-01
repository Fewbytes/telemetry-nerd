import pytest

from telemetry_nerd.sources.base import SourceError
from telemetry_nerd.sources.registry import SourceRegistry
from telemetry_nerd.sources.spec import AuthRef, MissingSecret, SourceSpec
from telemetry_nerd.workspace.db import open_workspace_db
from tests.unit.fakes import FakeSource


def fake_factory(spec: SourceSpec):
    return FakeSource(name=spec.name, identity=f"fake|{spec.url}")


def make(tmp_path, factory=fake_factory):
    return SourceRegistry(open_workspace_db(tmp_path / "w.db"), factory, clock=lambda: 1)


def spec(name="play", url="https://play.test/prom", **kw):
    return SourceSpec(name=name, url=url, **kw)


def test_add_get_iterate_and_persist(tmp_path):
    reg = make(tmp_path)
    s = spec()
    reg.add(s, reg.build(s))
    assert set(reg) == {"play"} and len(reg) == 1
    assert reg["play"].name == "play"
    assert reg.get("nope") is None

    again = make(tmp_path)
    again.load()
    assert set(again) == {"play"}
    assert again.spec("play") == s


def test_add_existing_name_requires_replace(tmp_path):
    reg = make(tmp_path)
    s = spec()
    reg.add(s, reg.build(s))
    with pytest.raises(SourceError, match="already exists") as e:
        reg.add(s, reg.build(s))
    assert "replace" in (e.value.hint or "")
    old = reg["play"]
    s2 = spec(url="https://other.test")
    assert reg.add(s2, reg.build(s2), replace=True) is old
    assert reg.spec("play") == s2


def test_remove_deletes_persisted_spec(tmp_path):
    reg = make(tmp_path)
    s = spec()
    reg.add(s, reg.build(s))
    assert reg.remove("play") is not None
    assert "play" not in reg
    fresh = make(tmp_path)
    fresh.load()
    assert "play" not in fresh


def test_remove_unknown_raises_with_hint(tmp_path):
    with pytest.raises(SourceError) as e:
        make(tmp_path).remove("ghost")
    assert "source_list" in (e.value.hint or "")


def test_attach_is_in_memory_only(tmp_path):
    reg = make(tmp_path)
    reg.attach("default", FakeSource(name="default"))
    assert "default" in reg
    fresh = make(tmp_path)
    fresh.load()
    assert "default" not in fresh


def test_load_keeps_specs_whose_secret_is_missing_as_broken(tmp_path):
    def strict(s: SourceSpec):
        if s.auth:
            raise MissingSecret("secret not found: environment variable TN_X is unset", hint="h")
        return fake_factory(s)

    reg = make(tmp_path, strict)
    s = spec(auth=AuthRef(env="TN_X"))
    reg.add(s, FakeSource(name="play"))  # persisted while the secret existed
    fresh = make(tmp_path, strict)
    fresh.load()
    assert "play" not in fresh  # not live
    [entry] = fresh.describe()
    assert entry["name"] == "play"
    assert "TN_X" in entry["broken"]


def test_describe_is_public_and_marks_settings_owned(tmp_path):
    reg = make(tmp_path)
    reg.attach("default", FakeSource(name="default"), spec("default", "http://127.0.0.1:8428"))
    s = spec()
    reg.add(s, reg.build(s))
    by_name = {d["name"]: d for d in reg.describe()}
    assert by_name["default"]["managed_by"] == "settings"
    assert by_name["play"]["managed_by"] == "runtime"
    assert by_name["play"]["url"] == "https://play.test/prom"
    assert by_name["play"]["broken"] is None
