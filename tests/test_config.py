"""Tests for the persistent Config layer.

We redirect the home directory to a temp path so the real ``~/.devready`` is
never touched during testing.
"""

import json

import devready.config as config_module
from devready.config import DEFAULT_MODEL, Config


def _redirect_home(tmp_path, monkeypatch):
    """Point Config's directory helpers at a temp dir for the duration of a test."""
    monkeypatch.setattr(config_module.Path, "home", lambda: tmp_path)


def test_defaults_when_no_file(tmp_path, monkeypatch):
    _redirect_home(tmp_path, monkeypatch)
    # Ensure no inherited env key influences the test.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    config = Config.load()
    assert config.llm.provider == "openrouter"
    assert config.llm.model == DEFAULT_MODEL
    assert config.llm.is_configured is False


def test_set_llm_persists_to_disk(tmp_path, monkeypatch):
    _redirect_home(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    config = Config.load()
    config.set_llm("openrouter", api_key="sk-or-test", model="some/model")

    # Reload from disk to prove it round-trips.
    reloaded = Config.load()
    assert reloaded.llm.api_key == "sk-or-test"
    assert reloaded.llm.model == "some/model"
    assert reloaded.llm.is_configured is True


def test_env_var_overrides_stored_key(tmp_path, monkeypatch):
    _redirect_home(tmp_path, monkeypatch)
    config = Config.load()
    config.set_llm("openrouter", api_key="sk-or-fromfile")

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fromenv")
    reloaded = Config.load()
    assert reloaded.llm.api_key == "sk-or-fromenv"


def test_register_and_list_projects(tmp_path, monkeypatch):
    _redirect_home(tmp_path, monkeypatch)
    from devready.config import list_projects, register_project

    register_project(tmp_path / "proj-a")
    register_project(tmp_path / "proj-b")
    # Re-registering proj-a should move it to the front, not duplicate it.
    register_project(tmp_path / "proj-a")

    projects = list_projects()
    paths = [p["path"] for p in projects]
    assert len(paths) == 2  # no duplicates
    assert paths[0] == str((tmp_path / "proj-a").resolve())  # most recent first
    assert all("last_setup" in p for p in projects)


def test_corrupt_config_falls_back_to_defaults(tmp_path, monkeypatch):
    _redirect_home(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    cfg_dir = tmp_path / ".devready"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text("{ not valid json")

    config = Config.load()  # should not raise
    assert config.llm.model == DEFAULT_MODEL
