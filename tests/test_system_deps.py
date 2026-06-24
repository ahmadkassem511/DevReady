"""Tests for system-package name normalization.

The README parser often yields human-written prerequisite names like
"Python 3.10+" or "Node.js 18+". These must be cleaned (and language runtimes
dropped) before we ever hand them to a package manager — otherwise we'd run
nonsense like `choco install Python 3.10+`.
"""

from devready.environment.system_deps import _normalize_package, normalize_packages


def test_strips_version_specifiers():
    assert _normalize_package("FFmpeg") == "ffmpeg"
    assert _normalize_package("redis v7") == "redis"
    assert _normalize_package("PostgreSQL >= 14") == "postgresql"
    assert _normalize_package("ImageMagick (latest)") == "imagemagick"


def test_node_aliases_to_nodejs():
    assert _normalize_package("Node.js 18+") == "nodejs"
    assert _normalize_package("node") == "nodejs"


def test_runtimes_are_dropped_from_install_list():
    to_install, runtimes = normalize_packages(["Python 3.10+", "Node.js 18+", "FFmpeg"])
    # Only the real system package remains; runtimes are reported separately.
    assert to_install == ["ffmpeg"]
    assert "python" in runtimes
    assert "nodejs" in runtimes


def test_deduplicates_installable_packages():
    to_install, _ = normalize_packages(["ffmpeg", "FFmpeg", "ffmpeg (latest)"])
    assert to_install == ["ffmpeg"]


def test_empty_input():
    assert normalize_packages([]) == ([], [])


def test_install_tool_noop_when_already_present():
    # Python is guaranteed to be on PATH while the tests run, so install_tool
    # must short-circuit to True without trying to install anything.
    from devready.environment.system_deps import install_tool

    assert install_tool("python") is True


def test_tool_packages_have_make_mappings():
    # `make` is the canonical auto-install case; ensure it maps for common managers.
    from devready.environment.system_deps import TOOL_PACKAGES

    assert TOOL_PACKAGES["make"]["choco"] == "make"
    assert TOOL_PACKAGES["make"]["brew"] == "make"
    assert TOOL_PACKAGES["make"]["apt"] == "make"


def test_windows_executable_resolution(monkeypatch):
    # On Windows, a bare 'npm' must be resolved to its real .cmd path so
    # subprocess can launch it (CreateProcess won't find a .cmd by bare name).
    import devready.utils as utils

    monkeypatch.setattr(utils.sys, "platform", "win32")
    monkeypatch.setattr(utils.shutil, "which", lambda name: r"C:\Program Files\nodejs\npm.CMD" if name == "npm" else None)

    assert utils._resolve_windows_executable(["npm", "install"]) == [r"C:\Program Files\nodejs\npm.CMD", "install"]
    # Unknown command is left unchanged (so the normal 127 path still applies).
    assert utils._resolve_windows_executable(["mystery-tool"]) == ["mystery-tool"]
    # A shell string is never touched.
    assert utils._resolve_windows_executable("npm install") == "npm install"


def test_non_windows_executable_resolution_is_noop(monkeypatch):
    import devready.utils as utils

    monkeypatch.setattr(utils.sys, "platform", "linux")
    assert utils._resolve_windows_executable(["npm", "install"]) == ["npm", "install"]


def test_node_has_tool_mappings():
    # Node must be auto-installable across the common managers (it bundles npm).
    from devready.environment.system_deps import TOOL_PACKAGES

    assert TOOL_PACKAGES["node"]["winget"] == "OpenJS.NodeJS.LTS"
    assert TOOL_PACKAGES["node"]["choco"] == "nodejs-lts"
    assert TOOL_PACKAGES["node"]["brew"] == "node"


def test_all_language_toolchains_are_installable():
    # Every language DevReady supports must have an auto-install mapping so a
    # missing toolchain never dead-ends the setup.
    from devready.environment.system_deps import TOOL_PACKAGES

    for runner in ("cargo", "go", "ruby", "composer", "dotnet", "mvn", "gradle"):
        assert runner in TOOL_PACKAGES, f"{runner} has no auto-install mapping"
        # Each must at least cover brew (mac) and apt (linux).
        assert "brew" in TOOL_PACKAGES[runner]
        assert "apt" in TOOL_PACKAGES[runner]


def test_fnm_is_installable_on_windows_and_mac():
    # fnm powers per-project Node versions; it must be auto-installable where
    # it's packaged (Windows managers + brew).
    from devready.environment.system_deps import TOOL_PACKAGES

    assert TOOL_PACKAGES["fnm"]["winget"] == "Schniz.fnm"
    assert TOOL_PACKAGES["fnm"]["choco"] == "fnm"
    assert TOOL_PACKAGES["fnm"]["brew"] == "fnm"


def test_ensure_node_short_circuits_when_npm_present(monkeypatch):
    # When npm is already on PATH, ensure_node returns True without installing.
    import devready.environment.system_deps as sd

    monkeypatch.setattr(sd, "command_exists", lambda name: name == "npm")
    called = {"install": False}
    monkeypatch.setattr(sd, "install_tool", lambda name: called.__setitem__("install", True) or True)

    assert sd.ensure_node() is True
    assert called["install"] is False


def test_ensure_node_installs_when_missing(monkeypatch):
    # When npm is missing, ensure_node installs Node, then re-checks for npm.
    import devready.environment.system_deps as sd

    state = {"npm_present": False}
    monkeypatch.setattr(sd, "command_exists", lambda name: state["npm_present"] if name == "npm" else False)

    def fake_install(name):
        assert name == "node"
        state["npm_present"] = True  # the install made npm available
        return True

    monkeypatch.setattr(sd, "install_tool", fake_install)
    assert sd.ensure_node() is True


def test_ensure_node_reports_failure(monkeypatch):
    # If npm still isn't there after an install attempt, ensure_node returns False.
    import devready.environment.system_deps as sd

    monkeypatch.setattr(sd, "command_exists", lambda name: False)
    monkeypatch.setattr(sd, "install_tool", lambda name: False)
    assert sd.ensure_node() is False


def test_non_admin_skips_choco_and_uses_winget(monkeypatch):
    # On a normal (non-admin) Windows account, choco needs admin and should be
    # skipped entirely — DevReady installs via winget without ever touching choco.
    import devready.environment.system_deps as sd

    available = {"choco", "winget"}
    attempted = []

    monkeypatch.setattr(sd.os, "name", "nt")
    monkeypatch.setattr(sd, "is_elevated", lambda: False)

    def fake_run_command(cmd, **kwargs):
        manager = cmd[0] if isinstance(cmd, list) else cmd.split()[0]
        attempted.append(manager)
        from devready.utils import CommandResult

        return CommandResult(command=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sd, "run_command", fake_run_command)
    monkeypatch.setattr(sd, "_refresh_path", lambda m: None)

    def tracked_exists(name):
        if name == "fnm":
            return "winget" in attempted  # appears after winget runs
        return name in available

    monkeypatch.setattr(sd, "command_exists", tracked_exists)

    assert sd.install_tool("fnm") is True
    assert "winget" in attempted
    assert "choco" not in attempted  # never tried — it needs admin


def test_elevated_tries_choco_after_user_scope_managers(monkeypatch):
    # When elevated, choco is allowed — but still after the no-admin managers.
    import devready.environment.system_deps as sd

    available = {"choco"}  # only choco present this time
    attempted = []

    monkeypatch.setattr(sd.os, "name", "nt")
    monkeypatch.setattr(sd, "is_elevated", lambda: True)

    def fake_run_command(cmd, **kwargs):
        manager = cmd[0] if isinstance(cmd, list) else cmd.split()[0]
        attempted.append(manager)
        from devready.utils import CommandResult

        return CommandResult(command=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sd, "run_command", fake_run_command)
    monkeypatch.setattr(sd, "_refresh_path", lambda m: None)

    def tracked_exists(name):
        if name == "make":
            return "choco" in attempted
        return name in available

    monkeypatch.setattr(sd, "command_exists", tracked_exists)

    assert sd.install_tool("make") is True
    assert "choco" in attempted


def test_non_admin_with_only_choco_does_not_attempt_install(monkeypatch):
    # A tool with no no-admin path, only choco available, not elevated: DevReady
    # must NOT try choco (it would fail) — it returns False so the caller can
    # surface the "run as administrator" guidance.
    import devready.environment.system_deps as sd

    monkeypatch.setattr(sd.os, "name", "nt")
    monkeypatch.setattr(sd, "is_elevated", lambda: False)
    # Only choco is present; 'make' has no direct-download installer.
    monkeypatch.setattr(sd, "command_exists", lambda name: name == "choco")

    ran = {"called": False}
    monkeypatch.setattr(sd, "run_command", lambda *a, **k: ran.__setitem__("called", True))

    assert sd.install_tool("make") is False
    assert ran["called"] is False  # choco skipped because not elevated


def test_fnm_has_direct_installer_on_windows(monkeypatch):
    import devready.environment.system_deps as sd

    monkeypatch.setattr(sd.os, "name", "nt")
    assert sd._direct_installer("fnm") is sd._install_fnm_direct_windows
    assert sd._direct_installer("make") is None


def test_fnm_direct_download_skipped_on_non_windows(monkeypatch):
    import devready.environment.system_deps as sd

    monkeypatch.setattr(sd.os, "name", "posix")
    assert sd._install_fnm_direct_windows() is False
