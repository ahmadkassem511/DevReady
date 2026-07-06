"""Tests for the disk-space cleanup module."""

import pytest

from devready.utils import CommandResult


@pytest.fixture(autouse=True)
def _isolate_caches(tmp_path, monkeypatch):
    """SAFETY: cleanup_caches() deletes real cache directories. Every test must
    resolve cache locations to a throwaway tmp dir, never the developer's real
    ~/.cargo, %LOCALAPPDATA%\\pnpm\\store, etc. This fixture redirects both the
    home dir and LOCALAPPDATA for the whole module so no test can nuke a real
    cache even if it forgets to isolate itself."""
    import devready.environment.cleanup as cu

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(cu.Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))


def test_cleanup_runs_only_installed_tools(monkeypatch):
    import devready.environment.cleanup as cu

    ran = []
    monkeypatch.setattr(cu, "command_exists", lambda n: n in ("npm", "docker"))
    monkeypatch.setattr(
        cu, "run_command",
        lambda cmd, **k: ran.append(list(cmd)) or CommandResult("x", 0),
    )
    monkeypatch.setattr(cu, "free_disk_bytes", lambda: 0)

    report = cu.cleanup_caches()
    labels = [label for label, _ in report["details"]]
    assert "pip download cache" in labels     # always: runs via sys.executable
    assert "npm cache" in labels              # npm exists
    assert "uv cache (interpreters & wheels)" not in labels  # uv missing -> skipped
    assert "Docker unused data" in labels     # docker exists + engine "up" (mocked ok)
    # Deep-clean extras must NOT run by default.
    assert not any(c[:3] == ["docker", "image", "prune"] for c in ran)


def test_cleanup_deep_prunes_images_and_volumes(monkeypatch):
    import devready.environment.cleanup as cu

    ran = []
    monkeypatch.setattr(cu, "command_exists", lambda n: n == "docker")
    monkeypatch.setattr(
        cu, "run_command",
        lambda cmd, **k: ran.append(list(cmd)) or CommandResult("x", 0),
    )
    monkeypatch.setattr(cu, "free_disk_bytes", lambda: 0)

    cu.cleanup_caches(deep=True)
    assert ["docker", "image", "prune", "-a", "-f"] in ran
    assert ["docker", "volume", "prune", "-f"] in ran


def test_cleanup_skips_docker_when_engine_down(monkeypatch):
    import devready.environment.cleanup as cu

    ran = []

    def fake_run(cmd, **k):
        ran.append(list(cmd))
        # docker info fails -> engine down; everything else succeeds
        ok = 1 if cmd[:2] == ["docker", "info"] else 0
        return CommandResult("x", ok)

    monkeypatch.setattr(cu, "command_exists", lambda n: n == "docker")
    monkeypatch.setattr(cu, "run_command", fake_run)
    monkeypatch.setattr(cu, "free_disk_bytes", lambda: 0)

    report = cu.cleanup_caches()
    assert not any(c[:3] == ["docker", "system", "prune"] for c in ran)
    assert "Docker unused data" not in [label for label, _ in report["details"]]


def test_cleanup_clears_npx_and_cargo_caches(monkeypatch, tmp_path):
    # Regression: `npm cache clean` reports success but never clears the npx
    # run cache (_npx) or cargo's crate caches — seen live leaving ~1.5 GB
    # behind. cleanup_caches must remove those re-downloadable dirs too.
    import devready.environment.cleanup as cu

    npm_cache = tmp_path / "npm-cache"
    (npm_cache / "_npx" / "abc").mkdir(parents=True)
    (npm_cache / "_npx" / "abc" / "pkg.tgz").write_text("data")
    cargo = (tmp_path / "home") / ".cargo" / "registry"  # home is isolated by the fixture
    (cargo / "cache").mkdir(parents=True)
    (cargo / "cache" / "x.crate").write_text("data")
    (cargo / "src").mkdir(parents=True)
    (cargo / "src" / "y.rs").write_text("data")

    monkeypatch.setattr(cu, "command_exists", lambda n: n == "npm")
    monkeypatch.setattr(cu, "run_command", lambda cmd, **k: cu.CommandResult(
        "x", 0, stdout=str(npm_cache) if cmd[:3] == ["npm", "config", "get"] else ""))
    monkeypatch.setattr(cu, "free_disk_bytes", lambda: 0)

    report = cu.cleanup_caches()
    labels = [label for label, _ in report["details"]]
    assert "npx run cache" in labels
    assert "cargo downloaded crates" in labels
    assert "cargo crate sources" in labels
    assert not (npm_cache / "_npx").exists()      # actually removed
    assert not (cargo / "cache").exists()
    assert not (cargo / "src").exists()


def test_pnpm_store_dir_clears_all_versions(monkeypatch, tmp_path):
    # `pnpm store path` reports only the CURRENT version (…/store/v3), but the
    # gigabytes live in older v10/v11 dirs — seen live: prune reclaimed 0.
    # _pnpm_store_dir must return the whole `store` root so every version goes.
    import devready.environment.cleanup as cu

    store = tmp_path / "pnpm" / "store"
    (store / "v3").mkdir(parents=True)
    monkeypatch.setattr(cu, "command_exists", lambda n: n == "pnpm")
    monkeypatch.setattr(
        cu, "run_command",
        lambda cmd, **k: CommandResult("x", 0, stdout=str(store / "v3")),
    )
    assert cu._pnpm_store_dir() == store


def test_pnpm_store_dir_falls_back_to_default_when_pnpm_absent(monkeypatch, tmp_path):
    # yarn/pnpm scenario: the tool is gone but its multi-GB store lingers.
    import devready.environment.cleanup as cu

    monkeypatch.setattr(cu, "command_exists", lambda n: False)
    monkeypatch.setattr(cu.os, "name", "nt")
    local = tmp_path / "Local"
    (local / "pnpm" / "store").mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert cu._pnpm_store_dir() == local / "pnpm" / "store"


def test_cleanup_clears_pnpm_store_and_yarn_cache(monkeypatch, tmp_path):
    import devready.environment.cleanup as cu

    local = tmp_path / "Local"
    store = local / "pnpm" / "store"
    (store / "v11").mkdir(parents=True)
    (store / "v11" / "big.dat").write_text("data")
    yarn_cache = local / "Yarn" / "Cache"
    yarn_cache.mkdir(parents=True)
    (yarn_cache / "pkg.tgz").write_text("data")

    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(cu.os, "name", "nt")
    monkeypatch.setattr(cu, "command_exists", lambda n: n == "pnpm")
    monkeypatch.setattr(
        cu, "run_command",
        lambda cmd, **k: CommandResult("x", 0, stdout=str(store / "v3")),
    )
    monkeypatch.setattr(cu.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(cu, "free_disk_bytes", lambda: 0)

    report = cu.cleanup_caches()
    labels = [label for label, _ in report["details"]]
    assert "pnpm store (all versions)" in labels
    assert "yarn cache" in labels
    assert not store.exists()          # whole store root gone (all versions)
    assert not yarn_cache.exists()     # yarn cache gone even without yarn installed


def test_cleanup_no_longer_runs_ineffective_pnpm_prune(monkeypatch):
    # Regression guard: pnpm's own `store prune` is unreliable (leaves old
    # versions) — cleanup must not depend on it.
    import devready.environment.cleanup as cu

    ran = []
    monkeypatch.setattr(cu, "command_exists", lambda n: n in ("pnpm", "yarn"))
    monkeypatch.setattr(
        cu, "run_command",
        lambda cmd, **k: ran.append(list(cmd)) or CommandResult("x", 0, stdout=""),
    )
    monkeypatch.setattr(cu, "free_disk_bytes", lambda: 0)
    cu.cleanup_caches()
    assert ["pnpm", "store", "prune"] not in ran
    assert ["yarn", "cache", "clean"] not in ran


def test_format_bytes():
    from devready.environment.cleanup import format_bytes

    assert format_bytes(0) == "0 B"
    assert format_bytes(1536) == "1.5 KB"
    assert format_bytes(3 * 1024 ** 3) == "3.0 GB"
