"""Install OS-level system packages (the things pip/npm can't provide).

Some projects need binaries that live outside the language ecosystem — ffmpeg,
libpq, redis, and so on. This module maps a logical package name to the right
command for the user's package manager and installs it *with explicit consent*.

Design notes for contributors:
  * We never install silently. ``ensure_packages`` always asks first (unless
    ``assume_yes`` is passed) because installing system software is a
    privileged, hard-to-undo action.
  * ``PACKAGE_MAP`` translates a generic name into the manager-specific name
    where they differ (e.g. apt calls it ``ffmpeg`` but some packages differ).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, List, Optional

from ..utils import (
    CommandResult,
    command_exists,
    console,
    detect_package_manager,
    run_command,
)

# How to invoke each supported package manager to install a package.
# The {pkg} placeholder is replaced with the resolved package name.
INSTALL_TEMPLATES = {
    "brew": ["brew", "install", "{pkg}"],
    "apt": ["sudo", "apt-get", "install", "-y", "{pkg}"],
    "apt-get": ["sudo", "apt-get", "install", "-y", "{pkg}"],
    "dnf": ["sudo", "dnf", "install", "-y", "{pkg}"],
    "yum": ["sudo", "yum", "install", "-y", "{pkg}"],
    "pacman": ["sudo", "pacman", "-S", "--noconfirm", "{pkg}"],
    "choco": ["choco", "install", "-y", "{pkg}"],
    "winget": ["winget", "install", "--accept-package-agreements",
               "--accept-source-agreements", "{pkg}"],
    "scoop": ["scoop", "install", "{pkg}"],
}

# Optional per-manager name overrides for packages whose name differs from the
# generic one. Extend this as you discover mismatches. Structure:
#   { "<generic name>": { "<manager>": "<manager-specific name>" } }
PACKAGE_MAP = {
    "postgresql": {"apt": "postgresql", "brew": "postgresql", "choco": "postgresql"},
    "redis": {"apt": "redis-server", "brew": "redis", "choco": "redis-64"},
}

# Language runtimes are NOT installed as system packages — they're handled by
# the per-project version managers (uv for Python, fnm/nvm for Node). A README
# that lists "Python 3.10+" as a prerequisite must not trigger
# `choco install Python 3.10+`. We drop these from the system-package list.
RUNTIME_NAMES = {
    "python", "python3", "python2", "py", "pip", "pip3",
    "node", "nodejs", "node.js", "npm", "npx", "yarn", "pnpm",
    "deno", "bun",
}

# Normalise common human/README spellings to the package-manager id used below.
# Applied after stripping version specifiers (see _normalize_package).
NAME_ALIASES = {
    "node.js": "nodejs",
    "node": "nodejs",
    "postgres": "postgresql",
    "postgresql server": "postgresql",
    "imagemagick": "imagemagick",
    "ffmpeg": "ffmpeg",
}


def _normalize_package(raw: str) -> Optional[str]:
    """Clean a README-extracted package name into something installable.

    Strips version requirements and noise ("Node.js 18+" -> "nodejs",
    "Python 3.10+" -> "python", "FFmpeg" -> "ffmpeg"), applies known aliases,
    and returns None for entries that aren't real, installable system packages.
    """
    if not raw:
        return None
    name = raw.strip().lower()
    # Remove version specifiers and trailing requirement noise:
    #   "node.js 18+", "python >= 3.10", "ffmpeg (latest)", "redis v7"
    name = re.split(r"[><=~]|\bv?\d", name, maxsplit=1)[0]
    name = name.replace("(latest)", "").strip(" .,-")
    name = NAME_ALIASES.get(name, name)
    # Reject empties or anything with whitespace left (likely a phrase, not a pkg).
    if not name or " " in name:
        # Try once more: collapse internal spaces for known multi-word aliases.
        collapsed = NAME_ALIASES.get(raw.strip().lower())
        return collapsed
    return name


def normalize_packages(packages: List[str]) -> tuple[List[str], List[str]]:
    """Split raw package names into (installable, skipped-runtimes).

    Returns a tuple ``(to_install, runtimes_skipped)`` where ``to_install`` is the
    cleaned, de-duplicated list of real system packages and ``runtimes_skipped``
    lists language runtimes that were dropped (so we can tell the user they're
    handled elsewhere).
    """
    to_install: List[str] = []
    runtimes: List[str] = []
    for raw in packages:
        cleaned = _normalize_package(raw)
        if cleaned is None:
            continue
        if cleaned in RUNTIME_NAMES:
            runtimes.append(cleaned)
            continue
        if cleaned not in to_install:
            to_install.append(cleaned)
    return to_install, runtimes


def resolve_package_name(generic: str, manager: str) -> str:
    """Translate a generic package name into the manager-specific name."""
    return PACKAGE_MAP.get(generic, {}).get(manager, generic)


# Build/orchestration tools DevReady may need to *run* a project's own setup
# (e.g. a Makefile). Maps the tool to its package id per package manager so we
# can auto-install it. Unlisted managers fall back to the tool's own name.
TOOL_PACKAGES = {
    "make": {
        "choco": "make", "scoop": "make", "winget": "ezwinports.make",
        "brew": "make", "apt": "make", "apt-get": "make", "dnf": "make",
        "yum": "make", "pacman": "make",
    },
    "just": {"choco": "just", "scoop": "just", "brew": "just", "pacman": "just"},
    "task": {"scoop": "task", "brew": "go-task", "choco": "go-task"},
    # Node.js — installed when a project needs npm/node but neither is present.
    # The package bundles npm, so installing this gives us both.
    "node": {
        "choco": "nodejs-lts", "scoop": "nodejs-lts", "winget": "OpenJS.NodeJS.LTS",
        "brew": "node", "apt": "nodejs", "apt-get": "nodejs", "dnf": "nodejs",
        "yum": "nodejs", "pacman": "nodejs",
    },
    # fnm — fast Node version manager, auto-installed when a project pins a Node
    # version the current one doesn't meet, so DevReady can use the right Node
    # per project. (apt/dnf don't package it; there it falls back gracefully.)
    "fnm": {
        "choco": "fnm", "scoop": "fnm", "winget": "Schniz.fnm",
        "brew": "fnm", "pacman": "fnm",
    },
    # Language toolchains — auto-installed when a project needs one but it isn't
    # present, so DevReady can set up Rust/Go/Ruby/PHP/Java/.NET projects without
    # the user pre-installing the toolchain. Package names are best-effort per
    # manager; where a mapping is missing, install_tool falls back to the name.
    "cargo": {  # Rust (rustup provides cargo)
        "choco": "rust", "scoop": "rust", "winget": "Rustlang.Rustup",
        "brew": "rust", "apt": "cargo", "apt-get": "cargo", "dnf": "cargo",
        "yum": "cargo", "pacman": "rust",
    },
    "go": {
        "choco": "golang", "scoop": "go", "winget": "GoLang.Go",
        "brew": "go", "apt": "golang-go", "apt-get": "golang-go", "dnf": "golang",
        "yum": "golang", "pacman": "go",
    },
    "ruby": {
        "choco": "ruby", "scoop": "ruby", "winget": "RubyInstallerTeam.Ruby.3.3",
        "brew": "ruby", "apt": "ruby-full", "apt-get": "ruby-full", "dnf": "ruby",
        "yum": "ruby", "pacman": "ruby",
    },
    "composer": {  # PHP dependency manager (also pulls php where packaged together)
        "choco": "composer", "scoop": "composer", "winget": "PHP.Composer",
        "brew": "composer", "apt": "composer", "apt-get": "composer",
        "dnf": "composer", "yum": "composer", "pacman": "composer",
    },
    "php": {  # PHP runtime — composer is just a .phar that needs php to run.
        "choco": "php", "scoop": "php", "winget": "PHP.PHP",
        "brew": "php", "apt": "php-cli", "apt-get": "php-cli", "dnf": "php-cli",
        "yum": "php-cli", "pacman": "php",
    },
    "dotnet": {
        "choco": "dotnet-sdk", "scoop": "dotnet-sdk", "winget": "Microsoft.DotNet.SDK.8",
        "brew": "dotnet", "apt": "dotnet-sdk-8.0", "apt-get": "dotnet-sdk-8.0",
        "dnf": "dotnet-sdk-8.0", "yum": "dotnet-sdk-8.0", "pacman": "dotnet-sdk",
    },
    "mvn": {  # Java / Maven
        "choco": "maven", "scoop": "maven", "winget": "Apache.Maven",
        "brew": "maven", "apt": "maven", "apt-get": "maven", "dnf": "maven",
        "yum": "maven", "pacman": "maven",
    },
    "gradle": {  # Java / Gradle
        "choco": "gradle", "scoop": "gradle", "winget": "Gradle.Gradle",
        "brew": "gradle", "apt": "gradle", "apt-get": "gradle", "dnf": "gradle",
        "yum": "gradle", "pacman": "gradle",
    },
    # Docker — auto-installed when a project needs it to run (compose file, or the
    # README/guide says so). On Windows/macOS this is Docker Desktop (needs admin
    # + a running app); on Linux it's the engine packages. See ensure_docker().
    "docker": {
        "choco": "docker-desktop", "winget": "Docker.DockerDesktop",
        "brew": "docker", "apt": "docker.io", "apt-get": "docker.io",
        "dnf": "docker", "yum": "docker", "pacman": "docker",
    },
    # Podman — a daemonless, Docker-compatible container engine used as a NO-ADMIN
    # fallback when Docker can't be installed (scoop/brew/apt need no elevation).
    # See ensure_container_runtime().
    "podman": {
        "scoop": "podman", "winget": "RedHat.Podman", "choco": "podman-cli",
        "brew": "podman", "apt": "podman", "apt-get": "podman", "dnf": "podman",
        "yum": "podman", "pacman": "podman",
    },
}

# Where each package manager drops executables, so we can make a freshly
# installed tool visible to the *current* process without opening a new shell.
_MANAGER_BIN_DIRS = {
    "choco": [r"C:\ProgramData\chocolatey\bin"],
    "scoop": [os.path.expanduser(r"~\scoop\shims")],
    "winget": [os.path.expanduser(r"~\AppData\Local\Microsoft\WinGet\Links")],
    "brew": ["/opt/homebrew/bin", "/usr/local/bin"],
}

# Install locations that aren't a manager's shim dir but where common tools land
# (e.g. the Node installer drops node/npm here regardless of installer). Adding
# these lets DevReady see a just-installed Node without a new terminal.
_COMMON_TOOL_DIRS = [
    r"C:\Program Files\nodejs",
    os.path.expanduser(r"~\AppData\Roaming\npm"),
]


def _refresh_windows_path_from_registry() -> None:
    """Re-read the persisted PATH from the Windows registry into this process.

    Windows installers (choco/winget/the Node MSI) update the PATH stored in the
    registry, but a process that's already running keeps its old copy. Merging
    the registry's machine + user PATH back in lets DevReady use a tool that was
    just installed, without telling the user to open a new terminal.
    """
    if os.name != "nt":
        return
    try:
        import winreg
    except ImportError:
        return

    sources = [
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ]
    current = os.environ.get("PATH", "")
    known = set(current.split(os.pathsep))
    for root, subkey in sources:
        try:
            with winreg.OpenKey(root, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        for directory in value.split(os.pathsep):
            if directory and directory not in known:
                current += os.pathsep + directory
                known.add(directory)
    os.environ["PATH"] = current


def refresh_path() -> None:
    """Re-discover tools that are installed but not on this process's PATH.

    Adds every known package-manager shim dir and common tool install dir, then
    (on Windows) merges the persisted registry PATH. This recovers tools that
    *are* installed but invisible to the running process — e.g. when the GUI
    server was launched from an environment with a stale or partial PATH.
    """
    path = os.environ.get("PATH", "")
    extra = [d for dirs in _MANAGER_BIN_DIRS.values() for d in dirs] + _COMMON_TOOL_DIRS
    for directory in extra:
        if directory and Path(directory).exists() and directory not in path:
            path = path + os.pathsep + directory
    os.environ["PATH"] = path
    _refresh_windows_path_from_registry()


def _refresh_path(manager: str) -> None:
    """Make a freshly installed tool visible to the current process (see refresh_path)."""
    refresh_path()


def is_elevated() -> bool:
    """Return True if DevReady is running with admin (Windows) / root (POSIX) rights.

    Used to decide install strategy: choco installs into ``C:\\ProgramData`` and
    needs admin, so on a normal user account we route around it (winget/scoop/
    direct download) instead of wasting time on choco's admin prompt and failing.
    """
    if os.name == "nt":
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    try:
        return os.geteuid() == 0  # type: ignore[attr-defined]
    except AttributeError:
        return False


# Package managers that write to machine-wide locations and therefore need admin
# rights on Windows. We try these only when DevReady is elevated; otherwise we
# prefer the user-scope managers and direct downloads, which never need admin.
_ADMIN_MANAGERS = {"choco"}


def _direct_installer(name: str) -> Optional[Callable[[], bool]]:
    """Return a no-package-manager, no-admin installer for *name*, if one exists.

    These download a binary straight from the project's releases into a user dir,
    so they work on a locked-down machine with no package manager at all.
    """
    if os.name == "nt" and name == "fnm":
        return _install_fnm_direct_windows
    return None


def _install_with_manager(name: str, manager: str) -> bool:
    """Try installing *name* with a specific package manager. Returns True on success."""
    pkg = TOOL_PACKAGES.get(name, {}).get(manager, name)
    command = [part.replace("{pkg}", pkg) for part in INSTALL_TEMPLATES[manager]]
    console.print(f"  Installing [bold]{name}[/bold] via [bold]{manager}[/bold]…")
    result = run_command(command, capture=False)
    if not result.ok:
        return False
    _refresh_path(manager)
    return command_exists(name)


def _install_fnm_direct_windows() -> bool:
    """Download fnm.exe from GitHub releases — no admin, no package manager required.

    Extracts to ``~/.devready/bin/`` and prepends that directory to PATH so the
    binary is usable immediately in the current process.
    """
    if os.name != "nt":
        return False

    fnm_dir = Path.home() / ".devready" / "bin"
    fnm_dir.mkdir(parents=True, exist_ok=True)
    fnm_exe = fnm_dir / "fnm.exe"

    # Prepend our bin dir so shutil.which finds it after extraction.
    path = os.environ.get("PATH", "")
    if str(fnm_dir) not in path:
        os.environ["PATH"] = str(fnm_dir) + os.pathsep + path

    if fnm_exe.exists():
        return True  # already downloaded in a previous run

    console.print("  Downloading fnm from GitHub releases (no admin required)…")
    try:
        api_url = "https://api.github.com/repos/Schniz/fnm/releases/latest"
        req = urllib.request.Request(api_url, headers={"User-Agent": "devready/1"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        asset_url = next(
            (a["browser_download_url"] for a in data.get("assets", [])
             if "windows" in a["name"].lower() and a["name"].endswith(".zip")),
            None,
        )
        if not asset_url:
            return False

        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = Path(tmpdir) / "fnm.zip"
            urllib.request.urlretrieve(asset_url, zip_path)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(tmpdir)
            found = next(Path(tmpdir).rglob("fnm.exe"), None)
            if found:
                shutil.copy2(found, fnm_exe)

        if fnm_exe.exists():
            console.print(f"  [success]fnm downloaded to {fnm_dir}.[/success]")
            return True
    except Exception as exc:
        console.print(f"  [warning]Direct fnm download failed: {exc}[/warning]")
    return False


def _installer_managers() -> List[str]:
    """Return package managers to try, in order, honouring elevation.

    On Windows this prefers the user-scope managers (winget, scoop) and DROPS
    choco entirely when not elevated — choco's non-admin warning prompt (a 20s
    blocking question) can't be answered in the GUI, and it needs admin anyway.
    choco is included (last) only when DevReady is actually elevated. On other
    OSes it's just the detected system manager (apt/brew/…, which use sudo).
    """
    if os.name == "nt":
        present = [m for m in ("winget", "scoop", "choco") if command_exists(m)]
        if not is_elevated():
            present = [m for m in present if m not in _ADMIN_MANAGERS]
        return present
    primary = detect_package_manager()
    return [primary] if primary else []


def install_tool(name: str) -> bool:
    """Install a single tool, picking a strategy that doesn't need admin if possible.

    DevReady is meant to "just work" without the user issuing commands, so it
    chooses the install path intelligently:

      * On Windows, when *not* running as administrator, it prefers the
        user-scope managers (winget, scoop) and direct binary downloads — these
        never need admin. choco (which writes to ``C:\\ProgramData`` and shows a
        blocking non-admin prompt) is used only when DevReady is elevated.
      * When admin really is the only way to install something, it says so
        clearly: re-run from an elevated terminal.

    Returns True when the tool is usable afterwards. Callers are expected to have
    already obtained user consent (DevReady asks once, then installs and
    continues the setup automatically).
    """
    if command_exists(name):
        return True

    elevated = is_elevated()
    direct = _direct_installer(name)

    for manager in _installer_managers():
        if _install_with_manager(name, manager):
            console.print(f"  [success]{name} installed via {manager}.[/success]")
            return True
        console.print(f"  [muted]{manager} didn't work — trying the next option…[/muted]")

    # Direct binary download (no admin, no package manager) — e.g. fnm.
    if direct and direct() and command_exists(name):
        console.print(f"  [success]{name} installed.[/success]")
        return True

    # Nothing worked. Explain the most useful next step, depending on *why*.
    return _report_install_failure(name, elevated)


def _report_install_failure(name: str, elevated: bool) -> bool:
    """Print the most actionable message after every install strategy failed.

    Distinguishes "no package manager at all" from "the only option needs admin",
    so the user knows exactly what to do. Always returns False.
    """
    if os.name == "nt" and not elevated:
        admin_only = [m for m in _ADMIN_MANAGERS if command_exists(m)]
        no_admin_options = (
            any(command_exists(m) for m in ("winget", "scoop"))
            or _direct_installer(name) is not None
        )
        if admin_only and not no_admin_options:
            console.print(
                f"  [warning]Installing '{name}' needs administrator rights on this machine "
                f"(only {', '.join(admin_only)} is available, and it installs system-wide).[/warning]\n"
                f"  [warning]Please re-run DevReady from an elevated terminal "
                f"(right-click → 'Run as administrator'), then start again.[/warning]"
            )
            return False

    if not any(
        command_exists(m) for m in ("winget", "scoop", "choco", "brew", "apt", "apt-get", "dnf", "yum", "pacman")
    ):
        console.print(
            f"  [warning]No supported package manager found to install '{name}'. "
            f"Please install it manually.[/warning]"
        )
    else:
        console.print(f"  [error]Could not install {name} via any available method.[/error]")
    return False


def ensure_node() -> bool:
    """Ensure ``node`` and ``npm`` are available, installing Node if they aren't.

    This mirrors how DevReady auto-provisions Python: a project that needs npm
    shouldn't dead-end just because Node isn't installed. Returns True when npm
    is usable afterwards. We check ``npm`` specifically (not just ``node``)
    because that's what the install step actually calls.
    """
    if command_exists("npm"):
        return True
    # Node may already be installed but invisible to this process's PATH (a
    # common cause of the cryptic "npm: command not found"). Try to rediscover
    # it before installing anything.
    refresh_path()
    if command_exists("npm"):
        return True
    console.print("  [warning]Node.js / npm not found — installing Node so setup can continue…[/warning]")
    install_tool("node")  # installs Node (which bundles npm) and refreshes PATH
    if command_exists("npm"):
        console.print("  [success]Node.js is ready.[/success]")
        return True
    console.print(
        "  [error]Couldn't make npm available automatically. Install Node.js from "
        "https://nodejs.org and re-run.[/error]"
    )
    return False


def docker_ready() -> bool:
    """True if docker is installed AND its daemon is responding."""
    if not command_exists("docker"):
        return False
    return run_command(["docker", "info"]).ok


def _docker_desktop_exe() -> Optional[str]:
    """Locate ``Docker Desktop.exe`` on Windows, including non-standard installs.

    Docker Desktop now installs per-user under ``%LOCALAPPDATA%\\Programs\\
    DockerDesktop`` (the docker CLI lives at ``…\\DockerDesktop\\resources\\bin\\
    docker.exe``), not just the old ``C:\\Program Files\\Docker\\Docker``. We
    derive the app from the CLI's own location and also check the standard dirs.
    """
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    program_files = os.environ.get("ProgramFiles") or r"C:\Program Files"

    # Canonical install locations first (the real top-level launcher).
    candidates: List[Path] = [
        Path(local) / "Programs" / "DockerDesktop" / "Docker Desktop.exe",
        Path(program_files) / "Docker" / "Docker" / "Docker Desktop.exe",
        Path(local) / "Docker" / "Docker Desktop.exe",
    ]
    # Then derive from the CLI location, preferring the DockerDesktop *root*
    # (…/DockerDesktop/Docker Desktop.exe) over the resources/bin subdirs.
    cli = shutil.which("docker")
    if cli:
        parents = list(Path(cli).resolve().parents)
        for idx in (2, 3, 1, 0):  # …/DockerDesktop is parents[2] of …/resources/bin/docker.exe
            if idx < len(parents):
                candidates.append(parents[idx] / "Docker Desktop.exe")

    for candidate in candidates:
        try:
            if candidate.exists():
                return str(candidate)
        except OSError:
            continue
    return None


def _start_docker_daemon() -> bool:
    """Best-effort start of the Docker engine, per OS. True if we launched it."""
    if os.name == "nt":
        exe = _docker_desktop_exe()
        if exe:
            try:
                subprocess.Popen([exe])  # launch the app (which starts the engine)
                return True
            except OSError:
                return False
        return False
    if sys.platform == "darwin":
        return run_command(["open", "-a", "Docker"]).ok
    # Linux: try the service manager (needs privileges; harmless if denied).
    return run_command(["sudo", "systemctl", "start", "docker"]).ok


def _docker_install_guidance() -> List[str]:
    """OS-specific, actionable steps to install Docker, with the download link."""
    link = "https://www.docker.com/products/docker-desktop"
    if os.name == "nt":
        return [
            "Docker Desktop must be installed to run this project. On Windows it needs",
            "administrator rights and a one-time restart (it enables WSL2/virtualization),",
            "so it can't be installed unattended. To set it up:",
            f"  1. Download Docker Desktop:  {link}",
            "  2. Run the installer, approve the admin prompt, then restart Windows.",
            "  3. Start Docker Desktop once (wait for the whale icon), then run DevReady again.",
            "  Advanced: from an *Administrator* terminal, run  winget install Docker.DockerDesktop",
        ]
    if sys.platform == "darwin":
        return [
            "Docker Desktop must be installed to run this project:",
            f"  • Download:  {link}",
            "  • Or with Homebrew:  brew install --cask docker",
            "Then open Docker (wait for the whale icon) and run DevReady again.",
        ]
    return [
        "Docker must be installed to run this project. For example:",
        "  • Debian/Ubuntu:  sudo apt-get install -y docker.io && sudo systemctl enable --now docker",
        "  • Fedora:         sudo dnf install -y docker && sudo systemctl enable --now docker",
        "Then run DevReady again (you may need to add your user to the 'docker' group and re-login).",
    ]


def ensure_docker(
    wait_seconds: int = 180, install_if_missing: bool = True, guidance: bool = True
) -> bool:
    """Ensure Docker is installed *and* its engine is running. Returns usability.

    Treats Docker like any other dependency: if it isn't installed (and
    ``install_if_missing``), install it (Docker Desktop on Windows/macOS — may
    need admin); if it's installed but the engine is down, start it and wait up
    to ``wait_seconds`` for it to come up, reporting progress. ``guidance``
    controls whether the explicit "install Docker Desktop" steps are printed when
    it can't be made available — the runtime selector turns this off so it can
    fall back to Podman quietly first.
    """
    if docker_ready():
        return True

    if not command_exists("docker") and install_if_missing:
        console.print("  This project needs [bold]Docker[/bold] — trying to install it…")
        install_tool("docker")
        refresh_path()

    if not command_exists("docker"):
        if guidance:
            console.print(
                "  [warning]Docker isn't available yet — it can't be installed unattended here.[/warning]"
            )
            for line in _docker_install_guidance():
                console.print(f"  {line}")
        return False

    # Installed but the engine may be stopped — start it and wait (with progress).
    started = _start_docker_daemon()
    if started:
        console.print(
            "  Starting Docker Desktop — the engine can take a few minutes to come up "
            "on first start. Waiting…"
        )
    elif guidance:
        console.print(
            "  [warning]Couldn't find Docker Desktop to start it automatically — "
            "please open Docker Desktop. Waiting for its engine…[/warning]"
        )

    deadline = time.time() + wait_seconds
    next_notice = time.time() + 30
    while time.time() < deadline:
        if docker_ready():
            console.print("  [success]Docker engine is ready.[/success]")
            return True
        if time.time() >= next_notice:
            remaining = max(0, int(deadline - time.time()))
            console.print(f"  [muted]…still waiting for the Docker engine (up to ~{remaining}s more)…[/muted]")
            next_notice = time.time() + 30
        time.sleep(3)

    if guidance:
        console.print(
            "  [warning]Docker's engine didn't become ready in time. Once Docker Desktop "
            "shows 'running', re-run and it'll launch.[/warning]"
        )
    return False


# -----------------------------------------------------------------------------
# Podman — a no-admin, Docker-compatible fallback container engine
# -----------------------------------------------------------------------------
def podman_ready() -> bool:
    """True if podman is installed AND able to run containers (engine up)."""
    if not command_exists("podman"):
        return False
    return run_command(["podman", "info"]).ok


def _ensure_podman_machine() -> bool:
    """On Windows/macOS, make sure a Podman 'machine' (its small Linux VM) is up.

    Podman is daemonless and runs containers in a lightweight VM on Windows/macOS;
    Linux runs them natively (no machine needed). Creates a machine on first use
    (downloads a small image) and starts it. Best-effort; returns whether podman
    can run containers afterwards.
    """
    if os.name != "nt" and sys.platform != "darwin":
        return podman_ready()  # Linux: native, rootless — no VM to manage

    listing = run_command(["podman", "machine", "list", "--format", "{{.Name}}"])
    has_machine = bool(listing.ok and listing.stdout.strip())
    if not has_machine:
        console.print("  Creating a Podman machine (one-time; downloads a small Linux image)…")
        if not run_command(["podman", "machine", "init"], capture=False).ok:
            return False
    # Start it (a no-op if already running) and confirm it works.
    run_command(["podman", "machine", "start"], capture=False)
    return podman_ready()


def ensure_podman() -> bool:
    """Ensure Podman is installed and able to run containers. No admin required.

    Installs podman via a user-scope manager (scoop/brew/apt) if missing, then
    brings up its machine (Windows/macOS). Returns True when containers can run.
    """
    if podman_ready():
        return True
    if not command_exists("podman"):
        console.print("  Installing [bold]Podman[/bold] (a no-admin Docker alternative)…")
        install_tool("podman")
        refresh_path()
    if not command_exists("podman"):
        return False
    return _ensure_podman_machine()


def _make_docker_shim() -> Optional[str]:
    """Create a ``docker`` command that forwards to ``podman``; return its dir.

    Projects (and their ``make dev`` / compose scripts) call ``docker`` directly.
    Podman is CLI-compatible, so a tiny shim named ``docker`` that execs ``podman``
    — placed first on PATH — lets those commands run unchanged. Returns the dir to
    prepend to PATH, or None on failure.
    """
    bin_dir = Path.home() / ".devready" / "bin"
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            (bin_dir / "docker.cmd").write_text("@echo off\r\npodman %*\r\n", encoding="utf-8")
        else:
            shim = bin_dir / "docker"
            shim.write_text('#!/bin/sh\nexec podman "$@"\n', encoding="utf-8")
            os.chmod(shim, 0o755)
    except OSError:
        return None
    return str(bin_dir)


def ensure_container_runtime() -> "tuple[Optional[str], Optional[str]]":
    """Ensure a usable container engine, preferring Docker, falling back to Podman.

    Returns ``(runtime_name, path_prefix)``:
      * ``("docker", None)`` — Docker is (now) running; use it as-is.
      * ``("podman", <dir>)`` — Podman is running; prepend ``<dir>`` to PATH so the
        ``docker`` shim there routes the project's docker commands to Podman.
      * ``(None, None)`` — neither could be set up; guidance was printed.

    The Podman path needs no administrator rights, so projects that need a
    container engine can still run on a locked-down machine.
    """
    # 1. Use Docker if it's already installed — just start it (no admin to start).
    #    We don't trigger the admin-heavy Docker *install* here; Podman is the
    #    no-admin route when Docker isn't present.
    if ensure_docker(install_if_missing=False, guidance=False):
        return ("docker", None)

    # 2. Fall back to Podman (installs + runs without admin).
    console.print(
        "  [info]Docker isn't available — setting up Podman instead "
        "(a no-admin, Docker-compatible engine)…[/info]"
    )
    if ensure_podman():
        shim_dir = _make_docker_shim()
        console.print("  [success]Podman is ready — using it as the container engine.[/success]")
        return ("podman", shim_dir)

    # 3. Neither worked — explain how to get a container engine.
    console.print("  [warning]Couldn't set up a container engine automatically.[/warning]")
    for line in _docker_install_guidance():
        console.print(f"  {line}")
    return (None, None)


def is_installed(binary: str) -> bool:
    """Quick check: is the package's binary already on PATH?

    Many system packages expose a same-named binary (ffmpeg, redis-cli…), so
    this is a cheap way to skip work that's already done.
    """
    return command_exists(binary)


def ensure_packages(
    packages: List[str],
    *,
    assume_yes: bool = False,
) -> List[CommandResult]:
    """Ensure each requested system package is installed.

    Args:
        packages: Generic package names (e.g. ["ffmpeg", "postgresql"]).
        assume_yes: Skip the confirmation prompt (used by non-interactive runs).

    Returns:
        A list of :class:`CommandResult` for each install that was attempted.
        Packages already present, or skipped by the user, produce no result.
    """
    results: List[CommandResult] = []
    if not packages:
        return results

    # Clean the raw README-extracted names and drop language runtimes (those are
    # set up by the per-project version managers, not the system installer).
    packages, runtimes_skipped = normalize_packages(packages)
    if runtimes_skipped:
        console.print(
            f"  [muted]Skipping runtime(s) {', '.join(sorted(set(runtimes_skipped)))} — "
            f"handled per-project, not via the system package manager.[/muted]"
        )
    if not packages:
        return results

    # Elevation-aware manager order — never choco when non-admin (its blocking
    # non-admin prompt can't be answered in the GUI).
    managers = _installer_managers()
    if not managers:
        console.print(
            "  [warning]No no-admin package manager available to install: "
            f"{', '.join(packages)}.[/warning]\n"
            "  [warning]Install them manually, or re-run DevReady as administrator.[/warning]"
        )
        return results

    for generic in packages:
        # Skip anything already available — its binary is on PATH.
        if is_installed(generic):
            console.print(f"  [muted]{generic} already installed — skipping.[/muted]")
            continue

        # Ask once before changing the system, unless explicitly told not to.
        if not assume_yes:
            console.print(f"  Install [bold]{generic}[/bold] via [bold]{managers[0]}[/bold]?")
            answer = console.input("    Proceed? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                console.print(f"  [muted]Skipped {generic}.[/muted]")
                continue

        console.print(f"  Installing [bold]{generic}[/bold]…")
        installed_ok = False
        for manager in managers:
            pkg = resolve_package_name(generic, manager)
            command = [part.replace("{pkg}", pkg) for part in INSTALL_TEMPLATES[manager]]
            result = run_command(command, capture=False)  # streamed
            results.append(result)
            if result.ok:
                _refresh_path(manager)
                console.print(f"  [success]Installed {generic} via {manager}.[/success]")
                installed_ok = True
                break
            console.print(f"  [muted]{manager} couldn't install {generic} — trying the next option…[/muted]")
        if not installed_ok:
            console.print(
                f"  [warning]Couldn't install {generic} automatically — the app may still "
                f"work without it, or install it manually.[/warning]"
            )

    return results
