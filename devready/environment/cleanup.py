"""Reclaim disk space taken by the tool caches DevReady's installs fill up.

Deleting a project's folder is not enough to get the space back: the heavy
bytes usually live in SHARED caches outside the project — pip keeps every
downloaded wheel (a single torch is multi-GB), npm keeps every package
tarball, uv keeps interpreters and wheels, and Docker keeps images, stopped
containers and build cache. This module purges those caches safely (they are
all pure caches — the only cost of clearing them is a re-download next time)
and reports how much space actually came back.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import List, Tuple

from ..utils import CommandResult, command_exists, console, run_command

# (label, argv, head-that-must-exist). Every entry is a CACHE: clearing it can
# never break an installed project — the worst case is a slower next install.
_CACHE_CLEANERS: List[Tuple[str, List[str], str]] = [
    ("pip download cache", [sys.executable, "-m", "pip", "cache", "purge"], ""),
    ("npm cache", ["npm", "cache", "clean", "--force"], "npm"),
    ("uv cache (interpreters & wheels)", ["uv", "cache", "clean"], "uv"),
    ("pnpm store", ["pnpm", "store", "prune"], "pnpm"),
    ("yarn cache", ["yarn", "cache", "clean"], "yarn"),
]


def free_disk_bytes() -> int:
    """Free bytes on the drive that holds the user's home (where caches live)."""
    try:
        return shutil.disk_usage(Path.home()).free
    except OSError:
        return 0


def _docker_env() -> dict | None:
    """An env where `docker` resolves even on the Podman-shim path, or None
    when no docker command exists at all."""
    shim_dir = Path.home() / ".devready" / "bin"
    has_shim = (shim_dir / "docker").exists() or (shim_dir / "docker.cmd").exists()
    if not command_exists("docker") and not has_shim:
        return None
    env = os.environ.copy()
    if shim_dir.exists():
        env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
    return env


def cleanup_caches(deep: bool = False) -> dict:
    """Purge the shared tool caches and Docker's unused data. Returns a report.

    ``deep`` additionally removes ALL unused Docker images (not just dangling
    ones) and unused volumes — bigger wins, but the next container run
    re-downloads its image.

    Returns ``{"freed_bytes": int, "details": [(label, ok), ...]}``.
    """
    before = free_disk_bytes()
    details: List[Tuple[str, bool]] = []

    for label, argv, head in _CACHE_CLEANERS:
        if head and not command_exists(head):
            continue  # tool not installed -> its cache can't exist
        console.print(f"  Clearing {label}…")
        result: CommandResult = run_command(argv)
        details.append((label, result.ok))

    env = _docker_env()
    if env is not None:
        # Only prune when an engine is actually answering — otherwise the CLI
        # just errors after a timeout.
        if run_command(["docker", "info"], env=env).ok:
            console.print("  Removing Docker's unused data (stopped containers, dangling images, build cache)…")
            details.append(("Docker unused data", run_command(["docker", "system", "prune", "-f"], env=env).ok))
            if deep:
                console.print("  Deep clean: removing ALL unused Docker images and volumes…")
                details.append(("Docker unused images (all)", run_command(["docker", "image", "prune", "-a", "-f"], env=env).ok))
                details.append(("Docker unused volumes", run_command(["docker", "volume", "prune", "-f"], env=env).ok))

    freed = max(0, free_disk_bytes() - before)
    return {"freed_bytes": freed, "details": details}


def format_bytes(count: int) -> str:
    """Human-friendly size: 1234567890 -> '1.1 GB'."""
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"
