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
import re
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from ..utils import CommandResult, command_exists, console, force_rmtree, run_command

# (label, argv, head-that-must-exist). Every entry is a CACHE: clearing it can
# never break an installed project — the worst case is a slower next install.
# NOTE: pnpm and yarn are handled by DIRECTORY clearing below, not here — their
# own `clean`/`prune` commands are unreliable for reclaiming space (pnpm's
# prune only touches the current store version and leaves gigabytes in older
# vN folders; yarn's cache lingers after yarn itself is uninstalled). Clearing
# the cache directory is a pure cache operation and reclaims it in full.
_CACHE_CLEANERS: List[Tuple[str, List[str], str]] = [
    ("pip download cache", [sys.executable, "-m", "pip", "cache", "purge"], ""),
    ("npm cache", ["npm", "cache", "clean", "--force"], "npm"),
    ("uv cache (interpreters & wheels)", ["uv", "cache", "clean"], "uv"),
]


def _npm_cache_dir() -> Optional[Path]:
    """The directory npm uses for its cache (holds the `_npx` run cache)."""
    if not command_exists("npm"):
        return None
    result = run_command(["npm", "config", "get", "cache"])
    path = result.stdout.strip()
    return Path(path) if result.ok and path and path.lower() != "undefined" else None


def _pnpm_store_dir() -> Optional[Path]:
    """The pnpm content-addressable STORE root (parent of the v3/v10/v11… dirs).

    pnpm keeps a separate store per format version; over pnpm upgrades these
    accumulate (v10 + v11 + v3 …) and `pnpm store prune` only ever touches the
    CURRENT one, stranding gigabytes. So we clear the whole store root. Asks
    pnpm where the store is (authoritative), then falls back to the per-OS
    default location for when pnpm itself is no longer on PATH but its store
    lingers.
    """
    if command_exists("pnpm"):
        result = run_command(["pnpm", "store", "path"])
        raw = result.stdout.strip()
        if result.ok and raw:
            store_path = Path(raw)
            # `pnpm store path` → …/store/v3 ; clear the parent `store` root so
            # every version (v3/v10/v11…) goes, not just the current one.
            if re.fullmatch(r"v\d+", store_path.name):
                return store_path.parent
            return store_path
    home = Path.home()
    candidates = []
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "pnpm" / "store")
    candidates += [
        home / ".local" / "share" / "pnpm" / "store",  # Linux (XDG)
        home / "Library" / "pnpm" / "store",           # macOS
        home / ".pnpm-store",                           # legacy / custom
    ]
    return next((c for c in candidates if c.exists()), None)


def _yarn_cache_dirs() -> List[Path]:
    """Yarn cache directories (classic + Berry global). NOT yarn's global
    package installs or config — only the re-downloadable package cache."""
    home = Path.home()
    dirs: List[Path] = []
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Yarn" / "Cache")   # classic (Windows)
    dirs += [
        home / "Library" / "Caches" / "Yarn",             # classic (macOS)
        home / ".cache" / "yarn",                         # classic (Linux)
        home / ".yarn" / "berry" / "cache",               # Berry global cache
    ]
    return dirs


def _extra_cache_dirs() -> List[Tuple[str, Path]]:
    """Re-downloadable cache dirs cleared by removing the directory itself —
    for caches no tool `clean` command reliably reclaims (or where the tool
    that made them is no longer installed to run its cleaner)."""
    dirs: List[Tuple[str, Path]] = []
    npm_cache = _npm_cache_dir()
    if npm_cache:
        dirs.append(("npx run cache", npm_cache / "_npx"))
    # cargo keeps downloaded (.crate) + extracted crate sources here; both are
    # re-fetched on the next build. Keep bin/ (installed tools) and the index.
    cargo = Path.home() / ".cargo" / "registry"
    dirs.append(("cargo downloaded crates", cargo / "cache"))
    dirs.append(("cargo crate sources", cargo / "src"))
    # pnpm store (often the single biggest cache — gigabytes across versions).
    pnpm_store = _pnpm_store_dir()
    if pnpm_store:
        dirs.append(("pnpm store (all versions)", pnpm_store))
    # yarn cache (survives even after yarn is uninstalled).
    for yarn_cache in _yarn_cache_dirs():
        dirs.append(("yarn cache", yarn_cache))
    return dirs


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

    # Caches no built-in "clean" command reaches:
    #  * npm's `_npx` dir — `npm cache clean` only clears `_cacache`, never the
    #    npx run cache (which can be hundreds of MB after installs), and
    #  * cargo's downloaded/extracted crate sources (from Rust/Tauri builds).
    # Both are pure caches (re-downloaded on demand), so removing them is safe.
    for label, path in _extra_cache_dirs():
        if path.exists():
            console.print(f"  Clearing {label}…")
            details.append((label, force_rmtree(path)))

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
