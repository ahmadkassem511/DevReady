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


def test_format_bytes():
    from devready.environment.cleanup import format_bytes

    assert format_bytes(0) == "0 B"
    assert format_bytes(1536) == "1.5 KB"
    assert format_bytes(3 * 1024 ** 3) == "3.0 GB"
