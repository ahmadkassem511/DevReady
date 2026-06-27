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


def test_linux_only_packages_dropped_off_linux(monkeypatch):
    # libfuse2 etc. are Linux apt libs — on Windows/macOS they must be dropped,
    # not handed to scoop/winget (which can't install them).
    import devready.environment.system_deps as sd

    monkeypatch.setattr(sd.sys, "platform", "win32")
    to_install, _ = sd.normalize_packages(["libfuse2", "build-essential", "ffmpeg"])
    assert to_install == ["ffmpeg"]

    monkeypatch.setattr(sd.sys, "platform", "linux")
    to_install, _ = sd.normalize_packages(["libfuse2", "ffmpeg"])
    assert "libfuse2" in to_install  # on Linux it's a real, installable package


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
    monkeypatch.setattr(
        utils.shutil,
        "which",
        lambda name, path=None: r"C:\Program Files\nodejs\npm.CMD" if name == "npm" else None,
    )

    assert utils._resolve_windows_executable(["npm", "install"]) == [r"C:\Program Files\nodejs\npm.CMD", "install"]
    # Unknown command is left unchanged (so the normal 127 path still applies).
    assert utils._resolve_windows_executable(["mystery-tool"]) == ["mystery-tool"]
    # A shell string is never touched.
    assert utils._resolve_windows_executable("npm install") == "npm install"


def test_non_windows_executable_resolution_is_noop(monkeypatch):
    import devready.utils as utils

    monkeypatch.setattr(utils.sys, "platform", "linux")
    assert utils._resolve_windows_executable(["npm", "install"]) == ["npm", "install"]


def test_bash_resolves_to_git_bash_not_wsl_stub(monkeypatch):
    # ROOT FIX: `bash scripts/setup.sh` must run Git Bash, not the System32 WSL
    # launcher (which dies with execvpe(/bin/bash) when no distro is installed).
    import devready.utils as utils

    monkeypatch.setattr(utils.sys, "platform", "win32")
    monkeypatch.setattr(utils, "git_bash", lambda: r"C:\Program Files\Git\bin\bash.exe")
    assert utils._resolve_windows_executable(["bash", "scripts/setup.sh"]) == [
        r"C:\Program Files\Git\bin\bash.exe", "scripts/setup.sh"
    ]
    assert utils._resolve_windows_executable(["sh", "-c", "x"])[0] == r"C:\Program Files\Git\bin\bash.exe"


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


def test_ensure_packages_skips_choco_when_not_elevated(monkeypatch):
    # The bug: README system packages went through choco (non-admin 20s prompt).
    # Non-admin with only choco available -> no manager used, nothing invoked.
    import devready.environment.system_deps as sd

    monkeypatch.setattr(sd.os, "name", "nt")
    monkeypatch.setattr(sd, "is_elevated", lambda: False)
    monkeypatch.setattr(sd, "command_exists", lambda n: n == "choco")
    ran = []
    monkeypatch.setattr(sd, "run_command", lambda *a, **k: ran.append(a))

    assert sd.ensure_packages(["ffmpeg"], assume_yes=True) == []
    assert ran == []  # choco was NEVER invoked


def test_ensure_packages_uses_winget_when_not_elevated(monkeypatch):
    import devready.environment.system_deps as sd
    from devready.utils import CommandResult

    monkeypatch.setattr(sd.os, "name", "nt")
    monkeypatch.setattr(sd, "is_elevated", lambda: False)
    monkeypatch.setattr(sd, "command_exists", lambda n: n == "winget")
    monkeypatch.setattr(sd, "_refresh_path", lambda m=None: None)
    ran = []
    monkeypatch.setattr(
        sd, "run_command", lambda cmd, **k: ran.append(cmd) or CommandResult(command="x", returncode=0)
    )

    sd.ensure_packages(["ffmpeg"], assume_yes=True)
    assert any(c[0] == "winget" for c in ran)
    assert not any(c[0] == "choco" for c in ran)


def test_docker_in_tool_packages():
    from devready.environment.system_deps import TOOL_PACKAGES

    assert TOOL_PACKAGES["docker"]["winget"] == "Docker.DockerDesktop"
    assert TOOL_PACKAGES["docker"]["choco"] == "docker-desktop"
    assert TOOL_PACKAGES["docker"]["apt"] == "docker.io"


def test_ensure_docker_true_when_daemon_already_running(monkeypatch):
    # If docker is installed and `docker info` succeeds, ensure_docker is a no-op.
    import devready.environment.system_deps as sd

    monkeypatch.setattr(sd, "command_exists", lambda n: n == "docker")
    monkeypatch.setattr(
        sd, "run_command", lambda *a, **k: sd.CommandResult(command="docker info", returncode=0)
    )
    installed = []
    monkeypatch.setattr(sd, "install_tool", lambda name: installed.append(name) or True)
    assert sd.ensure_docker() is True
    assert installed == []  # nothing installed; it was already ready


def test_docker_install_guidance_is_os_specific(monkeypatch):
    import devready.environment.system_deps as sd

    monkeypatch.setattr(sd.os, "name", "nt")
    lines = sd._docker_install_guidance()
    text = "\n".join(lines).lower()
    assert "docker.com/products/docker-desktop" in text
    assert "administrator" in text and "restart" in text  # the Windows reality

    monkeypatch.setattr(sd.os, "name", "posix")
    monkeypatch.setattr(sd.sys, "platform", "linux")
    text = "\n".join(sd._docker_install_guidance()).lower()
    assert "apt-get install" in text or "dnf install" in text


def test_docker_desktop_exe_derives_from_cli(tmp_path, monkeypatch):
    # Docker Desktop installs per-user at …/Programs/DockerDesktop with the CLI at
    # …/DockerDesktop/resources/bin/docker.exe. We must find the root launcher.
    import devready.environment.system_deps as sd

    root = tmp_path / "Programs" / "DockerDesktop"
    (root / "resources" / "bin").mkdir(parents=True)
    cli = root / "resources" / "bin" / "docker.exe"
    cli.write_text("")
    app = root / "Docker Desktop.exe"
    app.write_text("")

    monkeypatch.setattr(sd.shutil, "which", lambda n: str(cli) if n == "docker" else None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nope"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "nope2"))

    assert sd._docker_desktop_exe() == str(app)


def test_ensure_docker_installs_when_missing(monkeypatch):
    # docker missing -> install attempted; if still missing afterwards, returns False.
    import devready.environment.system_deps as sd

    monkeypatch.setattr(sd, "command_exists", lambda n: False)
    monkeypatch.setattr(sd, "run_command", lambda *a, **k: sd.CommandResult(command="x", returncode=1))
    installed = []
    monkeypatch.setattr(sd, "install_tool", lambda name: installed.append(name) or False)
    monkeypatch.setattr(sd, "refresh_path", lambda: None)
    assert sd.ensure_docker() is False
    assert installed == ["docker"]


# -- Podman fallback ----------------------------------------------------------
def test_podman_in_tool_packages():
    from devready.environment.system_deps import TOOL_PACKAGES

    assert TOOL_PACKAGES["podman"]["scoop"] == "podman"   # no-admin on Windows
    assert "brew" in TOOL_PACKAGES["podman"]
    assert "apt" in TOOL_PACKAGES["podman"]


def test_container_runtime_prefers_docker_when_ready(monkeypatch):
    import devready.environment.system_deps as sd

    monkeypatch.setattr(sd, "ensure_docker", lambda **k: True)
    assert sd.ensure_container_runtime() == ("docker", None)


def test_container_runtime_falls_back_to_podman(monkeypatch, tmp_path):
    import devready.environment.system_deps as sd

    monkeypatch.setattr(sd, "ensure_docker", lambda **k: False)   # docker not usable
    monkeypatch.setattr(sd, "ensure_podman", lambda: True)         # podman works (no admin)
    monkeypatch.setattr(sd, "_make_docker_shim", lambda: str(tmp_path / "bin"))
    name, prefix = sd.ensure_container_runtime()
    assert name == "podman"
    assert prefix == str(tmp_path / "bin")


def test_container_runtime_none_when_neither_available(monkeypatch):
    import devready.environment.system_deps as sd

    monkeypatch.setattr(sd, "ensure_docker", lambda **k: False)
    monkeypatch.setattr(sd, "ensure_podman", lambda: False)
    assert sd.ensure_container_runtime() == (None, None)


def test_ensure_podman_skips_install_on_windows_when_absent(monkeypatch):
    # On Windows, Podman's VM needs the same admin/WSL2 as Docker — don't install
    # it speculatively (avoids the slow `podman machine init` that fails on HCS).
    import devready.environment.system_deps as sd

    monkeypatch.setattr(sd.os, "name", "nt")
    monkeypatch.setattr(sd, "podman_ready", lambda: False)
    monkeypatch.setattr(sd, "command_exists", lambda n: False)  # podman not installed
    installed = []
    monkeypatch.setattr(sd, "install_tool", lambda name: installed.append(name) or True)

    assert sd.ensure_podman() is False
    assert installed == []  # never tried to install podman


def test_ensure_podman_machine_no_init_on_windows_without_machine(monkeypatch):
    # Windows + no existing machine -> skip (no slow `podman machine init`).
    import devready.environment.system_deps as sd
    from devready.utils import CommandResult

    monkeypatch.setattr(sd.os, "name", "nt")
    ran = []

    def fake_run(cmd, **k):
        ran.append(cmd)
        return CommandResult(command="x", returncode=0, stdout="")  # `machine list` empty

    monkeypatch.setattr(sd, "run_command", fake_run)
    assert sd._ensure_podman_machine() is False
    assert not any("init" in c for c in ran)  # never attempted machine init


def test_make_docker_shim_forwards_to_podman(tmp_path, monkeypatch):
    import devready.environment.system_deps as sd

    monkeypatch.setattr(sd.Path, "home", lambda: tmp_path)
    bin_dir = sd._make_docker_shim()
    assert bin_dir == str(tmp_path / ".devready" / "bin")
    shim = tmp_path / ".devready" / "bin" / ("docker.cmd" if sd.os.name == "nt" else "docker")
    assert shim.exists()
    assert "podman" in shim.read_text()
