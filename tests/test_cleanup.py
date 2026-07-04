"""Tests for the disk-space cleanup module."""

from devready.utils import CommandResult


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
    cargo = tmp_path / "home" / ".cargo" / "registry"
    (cargo / "cache").mkdir(parents=True)
    (cargo / "cache" / "x.crate").write_text("data")
    (cargo / "src").mkdir(parents=True)
    (cargo / "src" / "y.rs").write_text("data")

    monkeypatch.setattr(cu, "command_exists", lambda n: n == "npm")
    monkeypatch.setattr(cu, "run_command", lambda cmd, **k: cu.CommandResult(
        "x", 0, stdout=str(npm_cache) if cmd[:3] == ["npm", "config", "get"] else ""))
    monkeypatch.setattr(cu.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(cu, "free_disk_bytes", lambda: 0)

    report = cu.cleanup_caches()
    labels = [label for label, _ in report["details"]]
    assert "npx run cache" in labels
    assert "cargo downloaded crates" in labels
    assert "cargo crate sources" in labels
    assert not (npm_cache / "_npx").exists()      # actually removed
    assert not (cargo / "cache").exists()
    assert not (cargo / "src").exists()


def test_format_bytes():
    from devready.environment.cleanup import format_bytes

    assert format_bytes(0) == "0 B"
    assert format_bytes(1536) == "1.5 KB"
    assert format_bytes(3 * 1024 ** 3) == "3.0 GB"
