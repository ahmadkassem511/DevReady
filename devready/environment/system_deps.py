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

import os
import re
from pathlib import Path
from typing import List, Optional

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


def install_tool(name: str) -> bool:
    """Install a single tool via the system package manager. No prompt here.

    Callers are expected to have already gotten the user's consent (DevReady
    asks once, then installs the tool *and* continues the setup). Returns True
    when the tool is available afterwards.
    """
    if command_exists(name):
        return True

    manager = detect_package_manager()
    if manager is None:
        console.print(
            f"  [warning]No supported package manager found to install '{name}'. "
            f"Please install it manually.[/warning]"
        )
        return False

    pkg = TOOL_PACKAGES.get(name, {}).get(manager, name)
    command = [part.replace("{pkg}", pkg) for part in INSTALL_TEMPLATES[manager]]
    console.print(f"  Installing [bold]{name}[/bold] via [bold]{manager}[/bold]…")
    result = run_command(command, capture=False)
    if not result.ok:
        console.print(f"  [error]Failed to install {name} (exit {result.returncode}).[/error]")
        return False

    # Make it usable in this same run.
    _refresh_path(manager)
    if command_exists(name):
        console.print(f"  [success]{name} installed.[/success]")
        return True
    console.print(
        f"  [warning]{name} was installed but isn't visible yet on PATH. "
        f"Open a new terminal and re-run [bold]devready start[/bold].[/warning]"
    )
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

    manager = detect_package_manager()
    if manager is None:
        console.print(
            "[warning]No supported package manager found. Please install these "
            f"manually: {', '.join(packages)}[/warning]"
        )
        return results

    template = INSTALL_TEMPLATES[manager]

    for generic in packages:
        # Skip anything already available — its binary is on PATH.
        if is_installed(generic):
            console.print(f"  [muted]{generic} already installed — skipping.[/muted]")
            continue

        pkg = resolve_package_name(generic, manager)
        command = [part.replace("{pkg}", pkg) for part in template]

        # Ask before changing the system, unless explicitly told not to.
        if not assume_yes:
            console.print(f"  Install [bold]{generic}[/bold] via [bold]{manager}[/bold]?")
            console.print(f"    [muted]{' '.join(command)}[/muted]")
            answer = console.input("    Proceed? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                console.print(f"  [muted]Skipped {generic}.[/muted]")
                continue

        console.print(f"  Installing [bold]{generic}[/bold]…")
        # Stream output so the user sees progress for slow installs.
        result = run_command(command, capture=False)
        results.append(result)
        if result.ok:
            console.print(f"  [success]Installed {generic}.[/success]")
        else:
            console.print(f"  [error]Failed to install {generic} (exit {result.returncode}).[/error]")

    return results
