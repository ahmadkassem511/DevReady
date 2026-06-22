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

from typing import List

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
    "winget": ["winget", "install", "--accept-package-agreements", "{pkg}"],
    "scoop": ["scoop", "install", "{pkg}"],
}

# Optional per-manager name overrides for packages whose name differs from the
# generic one. Extend this as you discover mismatches. Structure:
#   { "<generic name>": { "<manager>": "<manager-specific name>" } }
PACKAGE_MAP = {
    "postgresql": {"apt": "postgresql", "brew": "postgresql", "choco": "postgresql"},
    "redis": {"apt": "redis-server", "brew": "redis", "choco": "redis-64"},
}


def resolve_package_name(generic: str, manager: str) -> str:
    """Translate a generic package name into the manager-specific name."""
    return PACKAGE_MAP.get(generic, {}).get(manager, generic)


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
